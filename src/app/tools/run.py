import subprocess
import sys
from pydantic import BaseModel, Field
from app.tools.base import tool
from app.config import settings
from app.tools.file import get_workspace_dir
from langgraph.types import interrupt  # 👈 引入中断工具


class RunFileArgs(BaseModel):
    filename: str = Field(description="要运行的 Python 文件名")


@tool(RunFileArgs, "在沙箱中运行 workspace 下的 Python 文件，返回输出结果")
def run_file(filename: str) -> str:
    workspace = get_workspace_dir()
    target = (workspace / filename).resolve()

    if not str(target).startswith(str(workspace.resolve())):
        return "错误：不允许运行 workspace 以外的文件。"
    if not target.exists():
        return f"错误：文件 {filename} 不存在。"

    # 1. 读取要运行的代码
    code_content = target.read_text(encoding="utf-8")

    # 2. 🛑 核心：触发人工审批中断！
    # 这里的字典数据会被传给前端，图会在这里暂停
    approval_data = interrupt({
        "filename": filename,
        "code": code_content
    })

    # 3. 当图被恢复时，前端传来的审批结果会赋给 approval_data
    if approval_data.get("action") != "approve":
        return "用户拒绝了执行。"

    # 4. 用户批准了，正式进入沙箱运行
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(target)],
            capture_output=True,
            text=True,
            timeout=settings.SANDBOX_TIMEOUT,
            env={"PATH": ""},
            cwd=str(workspace),
        )
        output = result.stdout or ""
        error = result.stderr or ""

        if error:
            err_lines = error.strip().split("\n")
            short_err = "\n".join(err_lines[-5:])
            return f"输出：\n{output}\n\n错误：\n{short_err}"
        return f"输出：\n{output}" if output else "（无输出）"
    except subprocess.TimeoutExpired:
        return f"执行超时（超过 {settings.SANDBOX_TIMEOUT} 秒）"