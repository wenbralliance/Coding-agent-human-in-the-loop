from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.config import APP_HOME, settings
from app.tools.base import tool


PROJECT_ROOT = APP_HOME


def get_workspace_dir() -> Path:
    """Return the configured workspace and create it when needed."""
    configured = Path(settings.WORKSPACE_PATH)
    target = configured if configured.is_absolute() else PROJECT_ROOT / configured
    target.mkdir(parents=True, exist_ok=True)
    return target.resolve()


def resolve_workspace_path(raw_path: str) -> Path:
    workspace = get_workspace_dir()
    target = (workspace / raw_path).resolve()
    if not target.is_relative_to(workspace):
        raise ValueError("不允许访问 workspace 以外的路径。")
    return target


class ReadFileArgs(BaseModel):
    filename: str = Field(description="workspace 下的相对文件路径")


class ListFilesArgs(BaseModel):
    pass


class GrepArgs(BaseModel):
    keyword: str = Field(min_length=1, max_length=500)


@tool(
    ReadFileArgs,
    "读取 workspace 下单个文件。批量读取优先使用 read_files。",
    retryable=True,
)
def read_file(filename: str) -> str:
    target = resolve_workspace_path(filename)
    if not target.is_file():
        return f"错误：文件 {filename} 不存在。"
    return target.read_text(encoding="utf-8")


@tool(ListFilesArgs, "列出 workspace 顶层文件及大小。", retryable=True)
def list_files() -> str:
    workspace = get_workspace_dir()
    files = sorted(workspace.glob("*"))
    if not files:
        return "workspace 为空。"
    return "\n".join(
        f"- {path.name} ({path.stat().st_size} 字节)"
        for path in files
        if path.is_file()
    )


@tool(GrepArgs, "在 workspace 顶层文本文件中搜索关键词。", retryable=True)
def grep_content(keyword: str) -> str:
    workspace = get_workspace_dir()
    results: list[str] = []
    for path in sorted(workspace.glob("*")):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(content.splitlines(), 1):
            if keyword in line:
                results.append(f"{path.name}:{line_number}: {line.strip()}")
                if len(results) >= 50:
                    return "\n".join(results)
    if not results:
        return f"未找到包含 '{keyword}' 的内容。"
    return "\n".join(results)
