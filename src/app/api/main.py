import json
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel


from langgraph.checkpoint.memory import MemorySaver
from app.graph.state import AgentState
from app.graph.nodes import prepare_context, agent_node, tools_node, save_and_extract
from app.graph.edges import route_after_agent
from app.graph.build import build_graph
from app.memory.store import MemoryStore
from app.api.schemas import ChatRequest
from app.api.settings import router as settings_router
from app.db.connection import db_session

from langgraph.graph import StateGraph, START, END
from langchain_core.messages import HumanMessage

app = FastAPI(title="Coding Agent Local Engine")
store = MemoryStore()

# 👇 重新编译图，这次必须挂载 MemorySaver（断点保存器）
builder = StateGraph(AgentState)
builder.add_node("prepare", prepare_context)
builder.add_node("agent", agent_node)
builder.add_node("tools", tools_node)
builder.add_node("save", save_and_extract)
builder.add_edge(START, "prepare")
builder.add_edge("prepare", "agent")
builder.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "save": "save"})
builder.add_edge("tools", "agent")
builder.add_edge("save", END)

memory = MemorySaver()
graph = builder.compile(checkpointer=memory)

app.include_router(settings_router)
app.mount("/static", StaticFiles(directory="src/app/api/static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("src/app/api/static/index.html", "r", encoding="utf-8") as f:
        return f.read()


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ApproveRequest(BaseModel):
    session_id: str
    action: str  # "approve" 或 "reject"


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    state = {
        "session_id": req.session_id,
        "user_input": req.message,
        "messages": [HumanMessage(content=req.message)]
    }
    # 必须传入 config，指定 thread_id (即 session_id)，MemorySaver 才知道存哪
    config = {"configurable": {"thread_id": req.session_id}}

    async def event_generator():
        try:
            # 同时监听 updates（节点完成）和 custom（流式文本）
            async for mode, chunk in graph.astream(state, config=config, stream_mode=["updates", "custom"]):

                # 1. 流式文本
                if mode == "custom":
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        yield f"data: {json.dumps(chunk)}\n\n"

                # 2. 节点更新（包含工具执行完成、HITL 中断）
                elif mode == "updates":
                    for node_name, node_output in chunk.items():
                        if not isinstance(node_output, dict):
                            continue

                        # 👇 核心：捕获 HITL 中断
                        if "__interrupt__" in node_output:
                            interrupt_data = node_output["__interrupt__"][0].value
                            yield f"data: {json.dumps({'type': 'interrupt', 'data': interrupt_data})}\n\n"
                            return  # 抛出中断后，当前请求立刻结束，图挂起

                        # 工具执行结果通知
                        if node_name == "tools":
                            for m in node_output.get("messages", []):
                                if m.type == "tool":
                                    yield f"data: {json.dumps({'type': 'tool_end', 'content': m.content})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/approve")
async def approve_endpoint(req: ApproveRequest):
    config = {"configurable": {"thread_id": req.session_id}}

    # 用 Command(resume=...) 恢复图，把审批结果传进去
    from langgraph.types import Command
    state = Command(resume={"action": req.action})

    async def event_generator():
        try:
            async for mode, chunk in graph.astream(state, config=config, stream_mode=["updates", "custom"]):

                # 1. 拦截打字机文本流
                if mode == "custom":
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        yield f"data: {json.dumps(chunk)}\n\n"

                # 2. 拦截节点更新和中断信号
                elif mode == "updates":
                    for node_name, node_output in chunk.items():
                        if not isinstance(node_output, dict):
                            continue

                        # 👇 核心修复：捕获 LangGraph 的 interrupt 信号！
                        if "__interrupt__" in node_output:
                            # node_output["__interrupt__"] 是一个元组，取第 0 个元素的 value
                            interrupt_data = node_output["__interrupt__"][0].value
                            yield f"data: {json.dumps({'type': 'interrupt', 'data': interrupt_data})}\n\n"
                            return  # 抛出中断后，直接结束当前流，等待用户点批准

                        # 拦截工具执行结果
                        if node_name == "tools":
                            for m in node_output.get("messages", []):
                                if m.type == "tool":
                                    yield f"data: {json.dumps({'type': 'tool_end', 'content': m.content})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


class CreateSessionReq(BaseModel):
    session_id: str

@app.get("/api/sessions")
def get_sessions():
    """获取所有会话记录"""
    with db_session() as db:
        rows = db.execute("""
            SELECT s.id, s.title, s.created_at,
                   (SELECT content FROM messages WHERE session_id = s.id ORDER BY created_at ASC LIMIT 1) as first_msg
            FROM sessions s
            ORDER BY s.created_at DESC
        """).fetchall()

        sessions = []
        for r in rows:
            title = r["title"] if r["title"] and r["title"] != "Web 会话" else r["first_msg"]
            if title:
                title = title[:20] + "..." if len(title) > 20 else title
            else:
                title = "新对话"
            sessions.append({"id": r["id"], "title": title, "created_at": r["created_at"]})
        return sessions


@app.get("/api/sessions/{session_id}/messages")
def get_session_messages(session_id: str):
    """获取指定会话的所有历史消息"""
    with db_session() as db:
        rows = db.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,)
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

@app.post("/api/sessions")
def create_session(req: CreateSessionReq):
    """显式创建一个会话记录"""
    from app.db.connection import db_session
    with db_session() as db:
        # 如果不存在才插入
        db.execute(
            "INSERT OR IGNORE INTO sessions(id, title) VALUES (?, ?)",
            (req.session_id, "新对话")
        )
    return {"status": "ok"}