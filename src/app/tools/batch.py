from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from app.config import settings
from app.tools.base import tool
from app.tools.file import get_workspace_dir


class ReadFilesArgs(BaseModel):
    paths: list[str] = Field(description="要批量读取的 workspace 文件路径")
    max_chars_per_file: int = Field(
        default=20000,
        ge=100,
        description="单个文件最多返回的字符数",
    )


class TextEdit(BaseModel):
    path: str = Field(description="要修改的文件路径")
    old_text: str = Field(description="文件中必须唯一匹配的旧文本")
    new_text: str = Field(description="替换后的新文本")
    expected_matches: int = Field(
        default=1,
        ge=1,
        description="旧文本应该出现的次数",
    )
    expected_sha256: str | None = Field(
        default=None,
        description="可选的原文件 SHA256，用于防止基于过期内容修改",
    )


class FileWrite(BaseModel):
    path: str = Field(description="要创建或完整覆盖的文件路径")
    content: str = Field(description="完整文件内容")
    expected_sha256: str | None = Field(
        default=None,
        description="覆盖已有文件时的可选 SHA256 校验",
    )


class FileDelete(BaseModel):
    path: str = Field(description="要删除的文件路径")
    expected_sha256: str | None = Field(
        default=None,
        description="删除前可选 SHA256 校验",
    )


class EditBatchArgs(BaseModel):
    summary: str = Field(description="本次批量修改的简短说明")
    edits: list[TextEdit] = Field(default_factory=list)
    writes: list[FileWrite] = Field(default_factory=list)
    deletes: list[FileDelete] = Field(default_factory=list)


class FileContent(BaseModel):
    filename: str = Field(description="要创建或覆盖的文件路径")
    content: str = Field(description="完整文件内容")


class WriteFilesArgs(BaseModel):
    files: list[FileContent] = Field(
        min_length=1,
        description="一次写入的文件列表",
    )


class BatchEditError(ValueError):
    pass


def _json_result(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _rejected_result(summary: str, operations: int, error: str) -> str:
    return _json_result({
        "status": "rejected",
        "summary": summary,
        "total_operations": operations,
        "applied": False,
        "error": error,
    })


def _resolve_path(workspace: Path, raw_path: str) -> Path:
    if not raw_path or not raw_path.strip():
        raise BatchEditError("文件路径不能为空")
    path = (workspace / raw_path).resolve()
    if not path.is_relative_to(workspace):
        raise BatchEditError(f"不允许访问 workspace 以外的路径: {raw_path}")
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise BatchEditError(f"目标不是文件: {path}")
    return path.read_bytes()


def _validate_text(path: Path, content: str) -> None:
    suffix = path.suffix.lower()
    if suffix == ".py":
        compile(content, str(path), "exec")
    elif suffix == ".json":
        json.loads(content)


def _operation_count(arguments: EditBatchArgs) -> int:
    return len(arguments.edits) + len(arguments.writes) + len(arguments.deletes)


def describe_edit_batch(arguments_json: str) -> tuple[int, set[str]]:
    """Return (operations, unique_paths) without applying any changes."""
    data = json.loads(arguments_json)
    files = data.get("files") or []
    paths = {
        item.get("path")
        for key in ("edits", "writes", "deletes")
        for item in (data.get(key) or [])
        if isinstance(item, dict) and item.get("path")
    }
    paths.update(
        item.get("filename")
        for item in files
        if isinstance(item, dict) and item.get("filename")
    )
    operations = sum(
        len(data.get(key) or [])
        for key in ("edits", "writes", "deletes")
    ) + len(files)
    return operations, {path for path in paths if path}


@tool(ReadFilesArgs, "一次读取多个 workspace 文件，返回内容、大小和 SHA256")
def read_files(paths: list[str], max_chars_per_file: int = 20000) -> str:
    workspace = get_workspace_dir().resolve()
    if len(paths) > settings.MAX_READ_FILES:
        raise ValueError(
            f"单次最多读取 {settings.MAX_READ_FILES} 个文件，当前为 {len(paths)} 个"
        )

    files = []
    used_chars = 0
    for raw_path in paths:
        try:
            path = _resolve_path(workspace, raw_path)
            data = _read_bytes(path)
            if data is None:
                files.append({
                    "path": raw_path,
                    "status": "not_found",
                })
                continue

            text = data.decode("utf-8")
            remaining = max(0, settings.MAX_READ_CHARS_PER_CALL - used_chars)
            limit = min(max_chars_per_file, remaining)
            content = text[:limit]
            used_chars += len(content)
            files.append({
                "path": raw_path,
                "status": "ok",
                "size": len(data),
                "sha256": _sha256(data),
                "truncated": len(content) < len(text),
                "content": content,
            })
        except (OSError, UnicodeDecodeError, BatchEditError) as exc:
            files.append({
                "path": raw_path,
                "status": "error",
                "error": str(exc),
            })
        if used_chars >= settings.MAX_READ_CHARS_PER_CALL:
            break

    return _json_result({
        "status": "ok",
        "file_count": len(files),
        "total_chars": used_chars,
        "files": files,
    })


@tool(
    EditBatchArgs,
    "事务化批量编辑多个文件。一次提交 replacements、完整写入和删除，失败时不留下部分修改。",
)
def apply_edit_batch(
    summary: str,
    edits: list[TextEdit] | None = None,
    writes: list[FileWrite] | None = None,
    deletes: list[FileDelete] | None = None,
) -> str:
    arguments = EditBatchArgs(
        summary=summary,
        edits=edits or [],
        writes=writes or [],
        deletes=deletes or [],
    )
    workspace = get_workspace_dir().resolve()
    operation_count = _operation_count(arguments)
    if operation_count == 0:
        return _json_result({
            "status": "no_changes",
            "summary": summary,
            "message": "没有提交任何编辑操作",
        })
    if operation_count > settings.MAX_EDIT_OPERATIONS_PER_REQUEST:
        return _rejected_result(
            summary,
            operation_count,
            f"批量操作数超过上限 {settings.MAX_EDIT_OPERATIONS_PER_REQUEST}"
        )

    try:
        edit_paths = {str(_resolve_path(workspace, item.path)) for item in arguments.edits}
        write_paths = {str(_resolve_path(workspace, item.path)) for item in arguments.writes}
        delete_paths = {str(_resolve_path(workspace, item.path)) for item in arguments.deletes}
    except BatchEditError as exc:
        return _rejected_result(summary, operation_count, str(exc))
    all_paths = edit_paths | write_paths | delete_paths
    if len(all_paths) > settings.MAX_EDIT_FILES_PER_REQUEST:
        return _rejected_result(
            summary,
            operation_count,
            f"修改文件数超过上限 {settings.MAX_EDIT_FILES_PER_REQUEST}"
        )
    if edit_paths & write_paths or edit_paths & delete_paths or write_paths & delete_paths:
        return _rejected_result(
            summary,
            operation_count,
            "同一路径不能同时出现在 edits、writes、deletes 中",
        )

    initial_bytes: dict[Path, bytes | None] = {}
    final_contents: dict[Path, str | None] = {}
    per_file_operations: dict[Path, int] = {}
    staged: dict[Path, Path] = {}

    try:
        # Preflight all operations before touching the file system.
        for item in arguments.deletes:
            path = _resolve_path(workspace, item.path)
            original = _read_bytes(path)
            if original is None:
                raise BatchEditError(f"要删除的文件不存在: {item.path}")
            if item.expected_sha256 and _sha256(original) != item.expected_sha256.lower():
                raise BatchEditError(f"文件 SHA256 不匹配: {item.path}")
            initial_bytes[path] = original
            final_contents[path] = None
            per_file_operations[path] = per_file_operations.get(path, 0) + 1

        for item in arguments.writes:
            path = _resolve_path(workspace, item.path)
            original = _read_bytes(path)
            if item.expected_sha256:
                if original is None or _sha256(original) != item.expected_sha256.lower():
                    raise BatchEditError(f"文件 SHA256 不匹配: {item.path}")
            initial_bytes.setdefault(path, original)
            final_contents[path] = item.content
            per_file_operations[path] = per_file_operations.get(path, 0) + 1

        for item in arguments.edits:
            path = _resolve_path(workspace, item.path)
            if path not in initial_bytes:
                original = _read_bytes(path)
                if original is None:
                    raise BatchEditError(f"要编辑的文件不存在: {item.path}")
                if item.expected_sha256 and _sha256(original) != item.expected_sha256.lower():
                    raise BatchEditError(f"文件 SHA256 不匹配: {item.path}")
                initial_bytes[path] = original
            elif item.expected_sha256:
                original = initial_bytes[path]
                if original is None or _sha256(original) != item.expected_sha256.lower():
                    raise BatchEditError(f"文件 SHA256 不匹配: {item.path}")

            if path not in final_contents:
                final_contents[path] = initial_bytes[path].decode("utf-8")
            content = final_contents[path]
            if content is None:
                raise BatchEditError(f"文件已被本批次删除，不能再编辑: {item.path}")
            matches = content.count(item.old_text)
            if matches != item.expected_matches:
                raise BatchEditError(
                    f"旧文本匹配数不符: {item.path}; "
                    f"期望 {item.expected_matches}，实际 {matches}"
                )
            final_contents[path] = content.replace(
                item.old_text,
                item.new_text,
                item.expected_matches,
            )
            per_file_operations[path] = per_file_operations.get(path, 0) + 1

        changed_paths: list[Path] = []
        total_bytes = 0
        for path, content in final_contents.items():
            original = initial_bytes[path]
            if content is None:
                changed_paths.append(path)
                continue
            new_bytes = content.encode("utf-8")
            total_bytes += len(new_bytes)
            _validate_text(path, content)
            if original != new_bytes:
                changed_paths.append(path)

        if total_bytes > settings.MAX_EDIT_BATCH_BYTES:
            raise BatchEditError(
                f"批量写入内容超过 {settings.MAX_EDIT_BATCH_BYTES} 字节限制"
            )

        if not changed_paths:
            return _json_result({
                "status": "no_changes",
                "summary": summary,
                "total_operations": operation_count,
            })

        for path in changed_paths:
            content = final_contents[path]
            if content is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".agent_edit_",
                suffix=".tmp",
                dir=str(path.parent),
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            staged[path] = tmp_path

        applied: list[Path] = []
        try:
            for path in changed_paths:
                if final_contents[path] is None:
                    path.unlink()
                else:
                    os.replace(staged[path], path)
                    staged.pop(path, None)
                applied.append(path)
        except Exception:
            rollback_errors = []
            for path in reversed(applied):
                original = initial_bytes[path]
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        fd, rollback_name = tempfile.mkstemp(
                            prefix=".agent_rollback_",
                            suffix=".tmp",
                            dir=str(path.parent),
                        )
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(original)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(rollback_name, path)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{path}: {rollback_exc}")
            if rollback_errors:
                raise BatchEditError(
                    "批量写入失败且回滚不完整: " + "; ".join(rollback_errors)
                )
            raise
        finally:
            for tmp_path in staged.values():
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

        changed_files = [
            {
                "path": str(path.relative_to(workspace)).replace("\\", "/"),
                "operations": per_file_operations.get(path, 1),
                "sha256": (
                    None
                    if final_contents[path] is None
                    else _sha256(final_contents[path].encode("utf-8"))
                ),
            }
            for path in changed_paths
        ]
        return _json_result({
            "status": "applied",
            "summary": summary,
            "files_changed": len(changed_files),
            "total_operations": operation_count,
            "changed_files": changed_files,
        })

    except Exception as exc:
        for tmp_path in staged.values():
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return _rejected_result(summary, operation_count, str(exc))


@tool(
    WriteFilesArgs,
    "一次创建或覆盖多个文件。新建两个及以上文件时必须调用此工具，不要连续调用 write_file。",
)
def write_files(files: list[FileContent]) -> str:
    validated = WriteFilesArgs(files=files)
    writes = [
        FileWrite(path=item.filename, content=item.content)
        for item in validated.files
    ]
    return apply_edit_batch.func(
        summary=f"批量写入 {len(writes)} 个文件",
        edits=[],
        writes=writes,
        deletes=[],
    )
