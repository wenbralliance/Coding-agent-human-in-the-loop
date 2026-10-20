from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    # 当前会话的 ID
    session_id: str
    # 对话消息列表（LangGraph 的 add_messages 会自动把新消息追加进列表，而不是覆盖）
    messages: Annotated[list, add_messages]
    # 用户的原始输入（用于最后提取记忆）
    user_input: str
    # 当前用户请求已经执行的工具轮数
    tool_rounds: int
