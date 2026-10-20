import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel



from app.graph.state import AgentState
from app.graph.nodes import prepare_context, agent_node, tools_node, tool_limit, save_and_extract
from app.graph.edges import route_after_agent
from app.memory.store import MemoryStore
from app.api.schemas import ChatRequest
from app.api.settings import router as settings_router
from app.config import settings
from app.db.connection import db_session
from app.db.schema import create_all_tables
from app.tools.process_manager import process_registry

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command
from langchain_core.messages import HumanMessage

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with db_session() as db:
        create_all_tables(db)
    yield


app = FastAPI(title="Coding Agent Local Engine", lifespan=lifespan)
store = MemoryStore()

# 👇 重新编译图，这次必须挂载 MemorySaver（断点保存器）
builder = StateGraph(AgentState)
builder.add_node("prepare", prepare_context)
builder.add_node("agent", agent_node)
builder.add_node("tools", tools_node)
builder.add_node("limit", tool_limit)
builder.add_node("save", save_and_extract)
builder.add_edge(START, "prepare")
builder.add_edge("prepare", "agent")
builder.add_conditional_edges(
    "agent",
    route_after_agent,
    {"tools": "tools", "limit": "limit", "save": "save"},
)
builder.add_edge("tools", "agent")
builder.add_edge("limit", "save")
builder.add_edge("save", END)

memory = MemorySaver()
graph = builder.compile(checkpointer=memory)

app.include_router(settings_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with (STATIC_DIR / "index.html").open("r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "configured": bool(settings.LLM_API_KEY and settings.EMBEDDING_API_KEY),
    }


class ApproveRequest(BaseModel):
    session_id: str
    action: Literal["approve", "reject"]


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _get_interrupts(chunk: dict) -> tuple:
    """
    LangGraph 的中断位于 updates 流的顶层：
    {"__interrupt__": (Interrupt(...),)}

    同时兼容某些版本将它放在节点输出内部的结构。
    """
    interrupts = chunk.get("__interrupt__")
    if interrupts:
        return tuple(interrupts)

    for node_output in chunk.values():
        if isinstance(node_output, dict):
            interrupts = node_output.get("__interrupt__")
            if interrupts:
                return tuple(interrupts)
    return ()


async def _stream_graph(graph_input, config: dict):
    """统一处理首次对话和审批恢复的 SSE 事件。"""
    async for mode, chunk in graph.astream(
        graph_input,
        config=config,
        stream_mode=["updates", "custom"],
    ):
        if mode == "custom":
            if isinstance(chunk, dict) and chunk.get("type") in {"text", "limit"}:
                yield _sse(chunk)
            continue

        if mode != "updates" or not isinstance(chunk, dict):
            continue

        # 中断是 updates 事件本身的顶层字段，不是某个节点完成后的返回值。
        # 必须在遍历节点前检查，否则 Interrupt 元组会被 isinstance(..., dict) 过滤掉。
        interrupts = _get_interrupts(chunk)
        if interrupts:
            interrupt_obj = interrupts[0]
            yield _sse({
                "type": "interrupt",
                "data": getattr(interrupt_obj, "value", interrupt_obj),
                "interrupt_id": getattr(interrupt_obj, "id", None),
            })
            return

        for node_name, node_output in chunk.items():
            if node_name == "tools" and isinstance(node_output, dict):
                for message in node_output.get("messages", []):
                    if getattr(message, "type", "") == "tool":
                        yield _sse({
                            "type": "tool_end",
                            "name": getattr(message, "name", ""),
                            "content": message.content,
                        })

    yield _sse({"type": "done"})


def _streaming_response(graph_input, config: dict) -> StreamingResponse:
    async def event_generator():
        try:
            async for event in _stream_graph(graph_input, config):
                yield event
        except Exception as exc:
            yield _sse({"type": "error", "content": str(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    session_id = (req.session_id or "").strip()
    if not session_id:
        session_id = store.create_session("Web 会话")

    state = {
        "session_id": session_id,
        "user_input": req.message,
        "messages": [HumanMessage(content=req.message)],
        "tool_rounds": 0,
    }
    # 必须传入 config，指定 thread_id (即 session_id)，MemorySaver 才知道存哪
    config = {"configurable": {"thread_id": session_id}}
    return _streaming_response(state, config)


@app.post("/api/approve")
async def approve_endpoint(req: ApproveRequest):
    config = {"configurable": {"thread_id": req.session_id}}

    # 用 Command(resume=...) 恢复图，把审批结果传进去
    state = Command(resume={"action": req.action})
    return _streaming_response(state, config)


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


@app.get("/api/sessions/{session_id}/approval")
async def get_pending_approval(session_id: str):
    """查询指定会话是否仍在等待人工审批。"""
    config = {"configurable": {"thread_id": session_id}}
    snapshot = await graph.aget_state(config)
    interrupts = getattr(snapshot, "interrupts", ())
    if not interrupts:
        return {"pending": False}

    interrupt_obj = interrupts[0]
    return {
        "pending": True,
        "data": getattr(interrupt_obj, "value", interrupt_obj),
    }


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    """永久删除会话、消息、调用记录和对应的 LangGraph 检查点。"""
    counts = store.delete_session(session_id)
    if counts is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    await memory.adelete_thread(session_id)
    process_registry.stop_session(session_id)
    return {"status": "ok", **counts}


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
