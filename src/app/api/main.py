import json
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage

from app.graph.build import build_graph
from app.memory.store import MemoryStore
from app.api.schemas import ChatRequest
from app.api.settings import router as settings_router

app = FastAPI(title="Coding Agent Local Engine")
graph = build_graph()
store = MemoryStore()

app.include_router(settings_router)
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
            # 👇 同时监听 "updates" (节点结束) 和 "custom" (节点里的 writer 发出的事件)
            async for mode, chunk in graph.astream(state, stream_mode=["updates", "custom"]):

                # 1. 收到 agent 节点发来的文本
                if mode == "custom":
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        yield f"data: {json.dumps(chunk)}\n\n"

                # 2. 收到工具执行结果
                elif mode == "updates":
                    for node_name, node_output in chunk.items():
                        if not isinstance(node_output, dict):
                            continue
                        if node_name == "tools":
                            for m in node_output.get("messages", []):
                                if m.type == "tool":
                                    # 通知前端：工具跑完了！
                                    yield f"data: {json.dumps({'type': 'tool_end', 'content': m.content})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")