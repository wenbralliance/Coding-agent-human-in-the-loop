import json
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel



from app.graph.state import AgentState
from app.graph.nodes import prepare_context, agent_node, tools_node, save_and_extract
from app.graph.edges import route_after_agent
from app.graph.build import build_graph
from app.memory.store import MemoryStore
from app.api.schemas import ChatRequest
from app.api.settings import router as settings_router
from app.db.connection import db_session

from langgraph.checkpoint.memory import MemorySaver
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
    session_id = req.session_id or store.create_session("Web 会话")
    state = {
        "session_id": req.session_id,
        "user_input": req.message,
        "messages": [HumanMessage(content=req.message)]
    }
    # 必须传入 config，指定 thread_id (即 session_id)，MemorySaver 才知道存哪
    config = {"configurable": {"thread_id": session_id}}

    async def event_generator():
        try:
            async for mode, chunk in graph.astream(state, config=config, stream_mode=["updates", "custom"]):
                if mode == "custom":
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        yield f"data: {json.dumps(chunk)}\n\n"

                elif mode == "updates":
                    for node_name, node_output in chunk.items():
                        if not isinstance(node_output, dict):
                            continue

                        # 👇 拦截中断信号
                        if "__interrupt__" in node_output:
                            interrupt_obj = node_output["__interrupt__"][0]
                            yield f"data: {json.dumps({'type': 'interrupt', 'data': interrupt_obj.value})}\n\n"
                            return  # 立刻返回，断开这次流

                        if node_name == "tools":
                            for m in node_output.get("messages", []):
                                if getattr(m, "type", "") == "tool":
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
            # 使用最稳定的 v1 事件流监听
            async for event in graph.astream_events(state, config=config, version="v1"):
                kind = event["event"]

                # 1. 捕获模型流式输出文字
                if kind == "on_chat_model_stream":
                    content = event["data"]["chunk"].content
                    if content:
                        yield f"data: {json.dumps({'type': 'text', 'content': content})}\n\n"

                # 2. 捕获工具开始执行（可以让前端显示状态）
                elif kind == "on_tool_start":
                    yield f"data: {json.dumps({'type': 'tool_start', 'name': event['name']})}\n\n"

                # 3. 捕获工具执行结束
                elif kind == "on_tool_end":
                    yield f"data: {json.dumps({'type': 'tool_end', 'content': event['data'].get('output', '')})}\n\n"

                # 4. 👇 终极核心：捕获图的 interrupt 中断！
                elif kind == "on_chain_stream":
                    chunk = event["data"].get("chunk", {})
                    if "__interrupt__" in chunk:
                        interrupt_obj = chunk["__interrupt__"][0]
                        yield f"data: {json.dumps({'type': 'interrupt', 'data': interrupt_obj.value})}\n\n"
                        return  # 必须 return 结束流

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