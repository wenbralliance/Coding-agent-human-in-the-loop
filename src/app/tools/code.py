import ast
from pydantic import BaseModel, Field
from app.tools.base import tool
from app.llm.client import chat_async
from app.tools.process_manager import get_current_session


class AnalyzeCodeArgs(BaseModel):
    code: str = Field(description="要分析的 Python 代码字符串")


class ReviewCodeArgs(BaseModel):
    code: str = Field(description="要审查的代码字符串")
    language: str = Field(default="python", description="代码语言")


@tool(AnalyzeCodeArgs, "静态分析 Python 代码：语法检查、提取函数和类名，不执行代码")
def analyze_code(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"语法错误：第 {e.lineno} 行，{e.msg}"

    imports = []
    functions = []
    classes = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imports.append(n.name)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.FunctionDef):
            functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)

    return f"""语法正确。
导入：{', '.join(imports) or '无'}
函数：{', '.join(functions) or '无'}
类：{', '.join(classes) or '无'}"""


@tool(ReviewCodeArgs, "使用 LLM 审查代码，指出 bug、性能问题和改进建议")
async def review_code(code: str, language: str = "python") -> str:
    prompt = f"""你是一个代码审查专家。审查下面的 {language} 代码。
指出：
1. 潜在 bug
2. 性能问题
3. 可读性问题
4. 最佳实践建议

代码：
{code}

用简洁的条目输出，每条带严重程度（高/中/低）。"""

    resp = await chat_async(
        [{"role": "user", "content": prompt}],
        session_id=get_current_session() or None,
        call_type="review",
    )
    return resp["content"]
