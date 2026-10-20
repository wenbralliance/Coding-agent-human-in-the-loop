import json
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage

from app.graph.build import build_graph
from app.memory.store import MemoryStore
from app.api.schemas import ChatRequest

app = FastAPI(title="Coding Agent Local Engine")
graph = build_graph()
store = MemoryStore()

app.mount("/static", StaticFiles(directory="src/app/api/static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("src/app/api/static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    session_id = req.session_id or store.create_session("Web 会话")
    state = {
        "session_id": session_id,
        "user_input": req.message,
        "messages": [HumanMessage(content=req.message)]
    }

    async def event_generator():
        try:
            async for event in graph.astream_events(state, version="v1"):
                kind = event["event"]
                name = event["name"]

                # 1. 拦截 agent 节点通过 writer 吐出的流式文本
                if kind == "on_chain_stream" and name == "agent":
                    chunk = event["data"]["chunk"]
                    # 检查是否是我们自定义的 StreamWriter 数据
                    if isinstance(chunk, dict) and "type" in chunk:
                        yield f"data: {json.dumps(chunk)}\n\n"

                # 2. 拦截工具执行状态
                elif kind == "on_tool_start":
                    yield f"data: {json.dumps({'type': 'tool_start', 'name': name})}\n\n"

                elif kind == "on_tool_end":
                    yield f"data: {json.dumps({'type': 'tool_end', 'name': name})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")