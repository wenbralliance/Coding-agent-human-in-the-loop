from app.graph.state import AgentState
from app.config import settings


def route_after_agent(state: AgentState) -> str:
    """
    条件路由：判断 Agent 思考完后，下一步去哪。
    如果模型决定调用工具，去 tools 节点；
    否则，说明它给出了最终回答，去 save 节点。
    """
    messages = state["messages"]
    if not messages:
        return "save"

    last_msg = messages[-1]

    # 如果最后一条消息是 AIMessage 并且包含 tool_calls，说明要调工具
    if getattr(last_msg, "tool_calls", None):
        if int(state.get("tool_rounds", 0)) >= settings.MAX_TOOL_ROUNDS:
            return "limit"
        return "tools"

    return "save"
