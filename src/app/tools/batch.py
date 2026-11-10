from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field

from app.config import settings
from app.tools.base import tool
from app.tools.file import get_workspace_dir, resolve_workspace_path


class ReadFilesArgs(BaseModel):
    paths: list[str] = Field(
        min_length=1,
        description="要批量读取的 workspace 相对路径",
    )
    max_chars_per_file: int = Field(default=20_000, ge=100, le=100_000)


@tool(
    ReadFilesArgs,
    "一次读取多个 workspace 文件，返回内容、大小和 SHA256。",
    retryable=True,
)
def read_files(paths: list[str], max_chars_per_file: int = 20_000) -> str:
    workspace = get_workspace_dir()
    if len(paths) > settings.MAX_READ_FILES:
        raise ValueError(
            f"单次最多读取 {settings.MAX_READ_FILES} 个文件，当前为 {len(paths)} 个"
        )

    files: list[dict] = []
    used_chars = 0
    for raw_path in paths:
        try:
            path = resolve_workspace_path(raw_path)
            data = path.read_bytes()
            text = data.decode("utf-8")
            remaining = max(0, settings.MAX_READ_CHARS_PER_CALL - used_chars)
            limit = min(max_chars_per_file, remaining)
            content = text[:limit]
            used_chars += len(content)
            files.append(
                {
                    "path": raw_path,
                    "status": "ok",
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "truncated": len(content) < len(text),
                    "content": content,
                }
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            files.append(
                {
                    "path": raw_path,
                    "status": "error",
                    "error": str(exc),
                }
            )
        if used_chars >= settings.MAX_READ_CHARS_PER_CALL:
            break

    return json.dumps(
        {
            "status": "ok",
            "file_count": len(files),
            "total_chars": used_chars,
            "files": files,
        },
        ensure_ascii=False,
    )
