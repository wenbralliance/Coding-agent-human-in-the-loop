from pathlib import Path
from pydantic import BaseModel, Field
from app.tools.base import tool
from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_DIR = PROJECT_ROOT / settings.WORKSPACE_PATH
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


class WriteFileArgs(BaseModel):
    filename: str = Field(description="文件名，如 solution.py")
    content: str = Field(description="要写入的代码内容")


class ReadFileArgs(BaseModel):
    filename: str = Field(description="文件名，如 solution.py")


class ListFilesArgs(BaseModel):
    pass


class EditFileArgs(BaseModel):
    filename: str = Field(description="要修改的文件名")
    old_content: str = Field(description="要被替换的旧代码（必须唯一匹配）")
    new_content: str = Field(description="替换后的新代码")


class GrepArgs(BaseModel):
    keyword: str = Field(description="要搜索的关键词或代码片段")


@tool(WriteFileArgs, "将代码内容写入 workspace 下的文件")
def write_file(filename: str, content: str) -> str:
    target = (WORKSPACE_DIR / filename).resolve()
    if not str(target).startswith(str(WORKSPACE_DIR.resolve())):
        return "错误：不允许写入 workspace 以外的路径。"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"成功写入文件：{filename}（{len(content)} 字符）"


@tool(ReadFileArgs, "读取 workspace 下文件的内容")
def read_file(filename: str) -> str:
    target = (WORKSPACE_DIR / filename).resolve()
    if not str(target).startswith(str(WORKSPACE_DIR.resolve())):
        return "错误：不允许读取 workspace 以外的路径。"
    if not target.exists():
        return f"错误：文件 {filename} 不存在。"
    return target.read_text(encoding="utf-8")


@tool(ListFilesArgs, "列出 workspace 下的所有文件")
def list_files() -> str:
    files = list(WORKSPACE_DIR.glob("*"))
    if not files:
        return "workspace 为空。"
    return "\n".join(f"- {f.name} ({f.stat().st_size} 字节)" for f in files)


@tool(EditFileArgs, "替换文件中的一段内容（旧内容必须唯一匹配）")
def edit_file(filename: str, old_content: str, new_content: str) -> str:
    target = (WORKSPACE_DIR / filename).resolve()
    if not str(target).startswith(str(WORKSPACE_DIR.resolve())):
        return "错误：不允许修改 workspace 以外的路径。"
    if not target.exists():
        return f"错误：文件 {filename} 不存在。"

    content = target.read_text(encoding="utf-8")
    count = content.count(old_content)
    if count == 0:
        return "错误：未找到要替换的旧内容。请先用 read_file 确认原文。"
    if count > 1:
        return f"错误：找到 {count} 处匹配，不唯一。请提供更长的上下文。"

    new_full = content.replace(old_content, new_content, 1)
    target.write_text(new_full, encoding="utf-8")
    return f"成功修改文件：{filename}"


@tool(GrepArgs, "在 workspace 的文件中搜索关键词，返回匹配的行和文件名")
def grep_content(keyword: str) -> str:
    results = []
    for f in WORKSPACE_DIR.glob("*"):
        if f.is_file():
            try:
                content = f.read_text(encoding="utf-8")
                for i, line in enumerate(content.splitlines(), 1):
                    if keyword in line:
                        results.append(f"{f.name}:{i}: {line.strip()}")
            except Exception:
                pass
    if not results:
        return f"未找到包含 '{keyword}' 的内容。"
    return "\n".join(results[:50])  # 最多返回 50 行防爆上下文