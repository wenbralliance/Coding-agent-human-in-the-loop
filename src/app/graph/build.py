from langgraph.graph import StateGraph, START, END
from app.graph.state import AgentState
from app.graph.nodes import *
from app.graph.edges import route_after_agent


def build_graph():
    builder = StateGraph(AgentState)

    # 1. 加节点
    builder.add_node("prepare", prepare_context)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_node("save", save_and_extract)

    # 2. 连边（固定路线）
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "agent")

    # 3. 条件路由（Agent 出来可能去工具，也可能去保存）
    builder.add_conditional_edges(
        "agent",
        route_after_agent,
        {
            "tools": "tools",
            "save": "save"
        }
    )

    # 4. 工具执行完，回到 agent 继续思考（ReAct 循环）
    builder.add_edge("tools", "agent")

    # 5. 保存完就结束
    builder.add_edge("save", END)

    return builder.compile()