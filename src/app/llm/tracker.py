from app.db.connection import db_session


def record_llm_call(
        call_type: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        session_id: str | None = None,
        cost_usd: float = 0.0,
):
    """
    记录一次 LLM 调用的 Token 消耗。

    参数:
        call_type: 调用类型 (如 "main" 主对话, "extract" 提取记忆, "review" 代码审查)
        model: 模型名
        input_tokens: 输入 token
        output_tokens: 输出 token
        session_id: 归属的会话 ID
        cost_usd: 成本（如果暂时没算，可以传 0）
    """
    with db_session() as db:
        db.execute(
            """INSERT INTO llm_calls 
               (session_id, call_type, model, input_tokens, output_tokens, cost_usd) 
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, call_type, model, input_tokens, output_tokens, cost_usd)
        )