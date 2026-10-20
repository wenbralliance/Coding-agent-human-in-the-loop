from __future__ import annotations

import asyncio
import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.config import settings
from app.tools.base import tool
from app.tools.file import get_workspace_dir
from app.tools.process_manager import (
    _terminate_process_tree,
    create_log_paths,
    get_current_session,
    process_registry,
)


RunMode = Literal["auto", "script", "gui"]


class RunFileArgs(BaseModel):
    filename: str = Field(description="要运行的 workspace 下的 Python 文件名")
    mode: RunMode = Field(
        default="auto",
        description=(
            "运行模式。auto 自动判断 GUI；script 等待脚本结束；"
            "gui 在窗口成功启动后立即返回，不等待窗口关闭。"
        ),
    )


GUI_MODULES = {
    "tkinter",
    "customtkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "wx",
    "pygame",
}

GUI_LOOP_CALLS = {"mainloop", "exec", "exec_", "MainLoop"}

ENVIRONMENT_ERROR_MARKERS = (
    "tclerror",
    "no display name",
    "cannot connect to x server",
    "dll load failed",
    "tcl_init",
    "tk_init",
    "couldn't connect to display",
)


def _result(status: str, **fields) -> str:
    payload = {"status": status, **fields}
    return json.dumps(payload, ensure_ascii=False)


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _local_module_path(module: str, workspace: Path) -> Path | None:
    if not module or module.startswith("."):
        return None
    base = workspace.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _detect_gui(
    code: str,
    source_path: Path | None = None,
    workspace: Path | None = None,
    visited: set[Path] | None = None,
) -> bool:
    """根据 GUI 框架导入、事件循环和本地模块导入判断程序是否会阻塞。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    visited = visited if visited is not None else set()
    if source_path is not None:
        resolved_source = source_path.resolve()
        if resolved_source in visited:
            return False
        visited.add(resolved_source)

    has_gui_import = False
    has_blocking_loop = False
    has_pygame_window = False
    imported_modules: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.append(alias.name)
                if alias.name.split(".")[0] in GUI_MODULES:
                    has_gui_import = True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level == 0:
                imported_modules.append(node.module)
            if node.module.split(".")[0] in GUI_MODULES:
                has_gui_import = True
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in GUI_LOOP_CALLS:
                has_blocking_loop = True
            if name == "set_mode":
                has_pygame_window = True

    if has_gui_import and (has_blocking_loop or has_pygame_window):
        return True

    if workspace is not None:
        for module in imported_modules:
            local_path = _local_module_path(module, workspace)
            if local_path is None:
                continue
            try:
                local_code = local_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _detect_gui(local_code, local_path, workspace, visited):
                return True
    return False


def _resolve_mode(
    code: str,
    requested_mode: RunMode,
    source_path: Path | None = None,
    workspace: Path | None = None,
) -> Literal["script", "gui"]:
    detected_gui = _detect_gui(code, source_path, workspace)
    # GUI 事件循环如果按普通脚本等待，必然被误报为超时。
    if detected_gui or requested_mode == "gui":
        return "gui"
    return "script"


def _build_env(workspace: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "LLM_API_KEY",
        "EMBEDDING_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
    ):
        env.pop(key, None)

    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(workspace), env.get("PYTHONPATH", "")])
    )
    return env


def _read_log(path: Path, limit: int) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(content) <= limit:
        return content
    return "...(已截断)...\n" + content[-limit:]


def _remove_logs(*paths: Path) -> None:
    for path in paths:
        for attempt in range(5):
            try:
                path.unlink(missing_ok=True)
                break
            except PermissionError:
                if attempt == 4:
                    break
                time.sleep(0.1)
            except OSError:
                break


def _classify_exit(returncode: int, stdout: str, stderr: str, **fields) -> str:
    combined = f"{stderr}\n{stdout}"
    lowered = combined.lower()

    if returncode == 0:
        return _result(
            "completed",
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            **fields,
        )

    if any(marker in lowered for marker in ENVIRONMENT_ERROR_MARKERS):
        return _result(
            "environment_error",
            exit_code=returncode,
            stdout=stdout,
            stderr=stderr,
            message="GUI/Tk 运行环境异常，不应仅凭此结果修改业务代码。",
            **fields,
        )

    missing_module = re.search(
        r"(?:ModuleNotFoundError|ImportError):.*?No module named ['\"]([^'\"]+)['\"]",
        combined,
        re.IGNORECASE,
    )
    if "modulenotfounderror" in lowered or "importerror" in lowered:
        return _result(
            "dependency_error",
            exit_code=returncode,
            module=missing_module.group(1) if missing_module else None,
            stdout=stdout,
            stderr=stderr,
            message="缺少模块或依赖，先确认安装和导入路径。",
            **fields,
        )

    return _result(
        "runtime_error",
        exit_code=returncode,
        stdout=stdout,
        stderr=stderr,
        **fields,
    )


def _launch_process(target: Path, workspace: Path):
    stdout_path, stderr_path = create_log_paths(target.name)
    stdout_handle = stdout_path.open("w", encoding="utf-8", errors="replace")
    stderr_handle = stderr_path.open("w", encoding="utf-8", errors="replace")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    try:
        process = subprocess.Popen(
            [sys.executable, "-u", str(target)],
            cwd=str(workspace),
            env=_build_env(workspace),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            creationflags=creationflags,
        )
    except OSError:
        stdout_handle.close()
        stderr_handle.close()
        _remove_logs(stdout_path, stderr_path)
        raise
    finally:
        stdout_handle.close()
        stderr_handle.close()

    return process, stdout_path, stderr_path


def execute_file(
    filename: str,
    requested_mode: RunMode = "auto",
    session_id: str | None = None,
) -> str:
    """执行已通过审批的文件，并返回结构化 JSON 结果。"""
    workspace = get_workspace_dir().resolve()
    target = (workspace / filename).resolve()

    if not target.is_relative_to(workspace):
        return _result("invalid_file", message="不允许运行 workspace 以外的文件。")
    if not target.exists():
        return _result("not_found", filename=filename, message="文件不存在。")

    code_content = target.read_text(encoding="utf-8")
    mode = _resolve_mode(code_content, requested_mode, target, workspace)
    session_id = session_id or get_current_session() or "unknown"

    # 同一会话只保留最新的 GUI 进程，避免窗口不断累积。
    if mode == "gui":
        process_registry.stop_session(session_id)

    try:
        process, stdout_path, stderr_path = _launch_process(target, workspace)
    except OSError as exc:
        return _result(
            "launch_error",
            filename=filename,
            mode=mode,
            message=str(exc),
        )

    if mode == "gui":
        try:
            process.wait(timeout=settings.GUI_STARTUP_GRACE)
        except subprocess.TimeoutExpired:
            managed = process_registry.register(
                process,
                session_id=session_id,
                filename=filename,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            return _result(
                "gui_started",
                filename=filename,
                mode="gui",
                pid=managed.pid,
                message="窗口程序已启动并保持运行，无需等待关闭。",
            )

        output = _read_log(stdout_path, settings.TOOL_OUTPUT_MAX_CHARS)
        error = _read_log(stderr_path, settings.TOOL_OUTPUT_MAX_CHARS)
        _remove_logs(stdout_path, stderr_path)
        return _classify_exit(
            process.returncode or 0,
            output,
            error,
            filename=filename,
            mode="gui",
        )

    try:
        process.wait(timeout=settings.SANDBOX_TIMEOUT)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        output = _read_log(stdout_path, settings.TOOL_OUTPUT_MAX_CHARS)
        error = _read_log(stderr_path, settings.TOOL_OUTPUT_MAX_CHARS)
        _remove_logs(stdout_path, stderr_path)
        return _result(
            "timeout",
            filename=filename,
            mode="script",
            seconds=settings.SANDBOX_TIMEOUT,
            stdout=output,
            stderr=error,
            message="脚本在规定时间内未结束，请检查阻塞逻辑。",
        )

    output = _read_log(stdout_path, settings.TOOL_OUTPUT_MAX_CHARS)
    error = _read_log(stderr_path, settings.TOOL_OUTPUT_MAX_CHARS)
    _remove_logs(stdout_path, stderr_path)
    return _classify_exit(
        process.returncode or 0,
        output,
        error,
        filename=filename,
        mode="script",
    )


@tool(
    RunFileArgs,
    "运行 workspace 下的 Python 文件。GUI 使用 mode=gui 或 auto，窗口启动后立即返回；普通脚本使用 script。",
)
async def run_file(filename: str, mode: RunMode = "auto") -> str:
    workspace = get_workspace_dir().resolve()
    target = (workspace / filename).resolve()

    if not target.is_relative_to(workspace):
        return _result("invalid_file", message="不允许运行 workspace 以外的文件。")
    if not target.exists():
        return _result("not_found", filename=filename, message="文件不存在。")

    code_content = target.read_text(encoding="utf-8")
    resolved_mode = _resolve_mode(code_content, mode, target, workspace)
    approval_data = interrupt({
        "filename": filename,
        "code": code_content,
        "mode": resolved_mode,
    })

    if not isinstance(approval_data, dict) or approval_data.get("action") != "approve":
        return _result("rejected", filename=filename, message="用户拒绝了执行。")

    return await asyncio.to_thread(
        execute_file,
        filename,
        mode,
        get_current_session(),
    )
