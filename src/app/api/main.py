import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel



from app.graph.state import AgentState
from app.graph.nodes import prepare_context, agent_node, tools_node, tool_limit, save_and_extract
from app.graph.edges import route_after_agent, route_after_tools
from app.graph.todo import todo_registry
from app.memory.store import MemoryStore
from app.api.schemas import ChatRequest
from app.api.settings import router as settings_router
from app.config import settings
from app.db.connection import db_session
from app.db.schema import create_all_tables
from app.llm.resilience import get_llm_circuit_snapshot
from app.llm.tracing import langgraph_trace_config, tracing_enabled
from app.observability import (
    finish_task_run,
    get_metrics_summary,
    new_run_id,
    record_audit,
    run_context,
    start_task_run,
)
from app.release import should_rollback_canary, workflow_version_for_session
from app.runtime.concurrency import TaskCapacityError, task_capacity
from app.tools.bash import bash_sessions, find_bash_executable
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
        db.execute("""
            DELETE FROM sessions
            WHERE NOT EXISTS (
                SELECT 1 FROM messages WHERE messages.session_id = sessions.id
            )
        """)
    try:
        yield
    finally:
        bash_sessions.close_all()
        process_registry.stop_all()


app = FastAPI(
    title="Coding Agent Local Engine",
    version=settings.WORKFLOW_VERSION,
    lifespan=lifespan,
)
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
builder.add_conditional_edges(
    "tools",
    route_after_tools,
    {"agent": "agent", "limit": "limit"},
)
builder.add_edge("limit", "save")
builder.add_edge("save", END)

memory = MemorySaver()
graph = builder.compile(checkpointer=memory)

app.include_router(settings_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with (STATIC_DIR / "index.html").open("r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/health")
def health():
    try:
        bash_path = str(find_bash_executable())
        bash_available = True
    except Exception:
        bash_path = ""
        bash_available = False
    return {
        "status": "ok",
        "configured": bool(settings.LLM_API_KEY and settings.EMBEDDING_API_KEY),
        "version": settings.WORKFLOW_VERSION,
        "canary_percent": settings.CANARY_PERCENT,
        "canary_rollback": bool(
            settings.CANARY_VERSION.strip()
            and should_rollback_canary(
                settings.CANARY_VERSION.strip(),
                settings.WORKFLOW_VERSION,
            )
        ),
        "langsmith_tracing": tracing_enabled(),
        "llm_circuit": get_llm_circuit_snapshot(),
        "bash": {
            "available": bash_available,
            "executable": bash_path,
            "sessions": bash_sessions.snapshot()["count"],
        },
        "capacity": {
            "available_slots": task_capacity.available_slots,
            "max_concurrent": settings.MAX_CONCURRENT_TASKS,
        },
    }


class ApproveRequest(BaseModel):
    session_id: str
    action: Literal["approve", "reject"]


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _run_config(session_id: str) -> dict:
    config = {"configurable": {"thread_id": session_id}}
    config.update(langgraph_trace_config(session_id))
    return config


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


async def _task_state(config: dict) -> dict:
    try:
        snapshot = await graph.aget_state(config)
        return dict(snapshot.values or {})
    except Exception:
        return {}


async def _finish_run_from_state(
    run_id: str,
    *,
    status: str,
    started_at: float,
    config: dict,
) -> None:
    values = await _task_state(config)
    termination_reason = str(values.get("termination_reason") or "")
    effective_status = status
    if status == "completed" and termination_reason in {
        "llm_error",
        "tool_limit",
        "loop_detected",
        "todo_required",
    }:
        effective_status = "failed"
    todos = values.get("todos", [])
    finish_task_run(
        run_id,
        status=effective_status,
        termination_reason=termination_reason,
        tool_rounds=int(values.get("tool_rounds", 0)),
        todo_total=len(todos),
        todo_completed=sum(
            item.get("status") == "completed"
            for item in todos
            if isinstance(item, dict)
        ),
        loop_detected=bool(values.get("loop_detected", False)),
        duration_ms=int((time.monotonic() - started_at) * 1000),
    )


async def _stream_graph(graph_input, config: dict):
    """统一处理首次对话和审批恢复的 SSE 事件。"""
    async for mode, chunk in graph.astream(
        graph_input,
        config=config,
        stream_mode=["updates", "custom"],
    ):
        if mode == "custom":
            if isinstance(chunk, dict) and chunk.get("type") in {
                "text",
                "warning",
                "limit",
                "error",
            }:
                yield _sse(chunk)
            continue

        if mode != "updates" or not isinstance(chunk, dict):
            continue

        interrupts = _get_interrupts(chunk)
        if interrupts:
            interrupt_obj = interrupts[0]
            yield _sse(
                {
                    "type": "interrupt",
                    "data": getattr(interrupt_obj, "value", interrupt_obj),
                    "interrupt_id": getattr(interrupt_obj, "id", None),
                }
            )
            return

        for node_name, node_output in chunk.items():
            if node_name == "tools" and isinstance(node_output, dict):
                for message in node_output.get("messages", []):
                    if getattr(message, "type", "") == "tool":
                        yield _sse(
                            {
                                "type": "tool_end",
                                "name": getattr(message, "name", ""),
                                "content": message.content,
                            }
                        )

    yield _sse({"type": "done"})
    return


def _streaming_response(
    graph_input,
    config: dict,
    *,
    session_id: str,
    run_id: str,
    workflow_version: str,
) -> StreamingResponse:
    async def event_generator():
        started_at = time.monotonic()
        try:
            async with task_capacity.slot(session_id) as acquired:
                with run_context(run_id, session_id, workflow_version):
                    stream_status = "completed"
                    async for event in _stream_graph(graph_input, config):
                        if '"type": "interrupt"' in event:
                            stream_status = "paused"
                        yield event
                    await _finish_run_from_state(
                        run_id,
                        status=stream_status,
                        started_at=started_at,
                        config=config,
                    )
        except TaskCapacityError as exc:
            yield _sse(
                {
                    "type": "error",
                    "code": exc.reason,
                    "content": str(exc),
                }
            )
            await _finish_run_from_state(
                run_id,
                status="failed",
                started_at=started_at,
                config=config,
            )
        except asyncio.CancelledError:
            await _finish_run_from_state(
                run_id,
                status="cancelled",
                started_at=started_at,
                config=config,
            )
            raise
        except Exception as exc:
            yield _sse({"type": "error", "code": "unhandled_error", "content": str(exc)})
            await _finish_run_from_state(
                run_id,
                status="failed",
                started_at=started_at,
                config=config,
            )

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
    else:
        store.ensure_session(session_id)

    todo_registry.clear(session_id)
    run_id = new_run_id()
    workflow_version = workflow_version_for_session(session_id)
    start_task_run(run_id, session_id, workflow_version, req.message)
    state = {
        "session_id": session_id,
        "run_id": run_id,
        "workflow_version": workflow_version,
        "user_input": req.message,
        "messages": [HumanMessage(content=req.message)],
        "tool_rounds": 0,
        "todos": [],
        "recent_tool_signatures": [],
        "loop_detected": False,
        "loop_recovery_count": 0,
        "termination_reason": "",
    }
    # 必须传入 config，指定 thread_id (即 session_id)，MemorySaver 才知道存哪
    config = _run_config(session_id)
    return _streaming_response(
        state,
        config,
        session_id=session_id,
        run_id=run_id,
        workflow_version=workflow_version,
    )


@app.post("/api/approve")
async def approve_endpoint(req: ApproveRequest):
    config = _run_config(req.session_id)
    current_state = await _task_state(config)
    run_id = str(current_state.get("run_id") or new_run_id())
    workflow_version = str(
        current_state.get("workflow_version")
        or workflow_version_for_session(req.session_id)
    )

    # 用 Command(resume=...) 恢复图，把审批结果传进去
    state = Command(resume={"action": req.action})
    return _streaming_response(
        state,
        config,
        session_id=req.session_id,
        run_id=run_id,
        workflow_version=workflow_version,
    )


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
            WHERE EXISTS (
                SELECT 1 FROM messages m WHERE m.session_id = s.id
            )
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


@app.get("/api/sessions/{session_id}/todos")
def get_session_todos(session_id: str):
    items = todo_registry.get(session_id)
    return {
        "session_id": session_id,
        "items": [item.model_dump() for item in items],
    }


@app.get("/api/metrics")
def metrics():
    return get_metrics_summary()


@app.post("/api/tasks/{session_id}/cancel")
async def cancel_task(session_id: str):
    shell_closed = bash_sessions.close(session_id)
    pids = process_registry.stop_session(session_id)
    todo_registry.clear(session_id)
    await memory.adelete_thread(session_id)
    record_audit(
        action="task_cancel",
        resource=session_id,
        decision="cancelled",
        detail={"shell_closed": shell_closed, "stopped_pids": pids},
    )
    return {
        "status": "cancelled",
        "shell_closed": shell_closed,
        "stopped_pids": pids,
    }


@app.post("/api/bash/{session_id}/reset")
def reset_bash_session(session_id: str):
    closed = bash_sessions.close(session_id)
    return {"status": "closed" if closed else "not_running"}


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    """永久删除会话、消息、调用记录和对应的 LangGraph 检查点。"""
    counts = store.delete_session(session_id)
    if counts is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    await memory.adelete_thread(session_id)
    process_registry.stop_session(session_id)
    bash_sessions.close(session_id)
    todo_registry.clear(session_id)
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
