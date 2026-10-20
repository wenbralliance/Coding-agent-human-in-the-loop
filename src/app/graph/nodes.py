import asyncio
import json
import logging
import uuid

from langchain_core.messages import SystemMessage, AIMessage, ToolMessage
from langgraph.types import StreamWriter

from app.graph.state import AgentState
from app.memory.retriever import MemoryRetriever
from app.memory.store import MemoryStore
from app.llm.client import chat_async
from app.tools import registry

logger = logging.getLogger(__name__)

retriever = MemoryRetriever()
store = MemoryStore()


async def prepare_context(state: AgentState) -> dict:
    session_id = state["session_id"]
    user_input = state["user_input"]

    history = retriever.get_recent_messages(session_id, limit=10)
    memories = await retriever.search_memories(user_input, top_k=3)

    system_prompt = """你是一个极其严格的 Coding Agent。
    你的任务是**操作文件**并**运行代码**，而不是在对话框里展示代码。

    【强制工作流】
    1. 无论用户要求写什么代码，你**必须立刻调用 `write_file` 工具**将代码写入文件。
    2. 写完后，你**必须调用 `run_file` 工具**运行它。
    3. **绝对禁止**在对话中直接输出大段代码。

    当前工作区已配置好，直接传纯文件名即可。现在开始执行任务。
    """
    if memories:
        mem_text = "\n".join(f"- [{m['type']}] {m['content']}" for m in memories)
        system_prompt += f"\n\n已知用户信息：\n{mem_text}"

    store.add_message(session_id, "user", user_input)

    msgs = [SystemMessage(content=system_prompt)]
    msgs.extend(state["messages"])
    return {"messages": msgs}


async def agent_node(state: AgentState, writer: StreamWriter) -> dict:
    messages = state["messages"]
    tools_schema = registry.get_all_schemas()

    openai_msgs = []

    # 严格的历史清洗
    i = 0
    while i < len(messages):
        m = messages[i]

        if isinstance(m, SystemMessage):
            openai_msgs.append({"role": "system", "content": str(m.content)})
            i += 1

        elif isinstance(m, AIMessage):
            msg_dict = {"role": "assistant", "content": str(m.content or "")}

            if getattr(m, "tool_calls", None) and len(m.tool_calls) > 0:
                tool_call_ids = [str(tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")) for tc in
                                 m.tool_calls]

                tool_msgs = []
                j = i + 1
                while j < len(messages) and isinstance(messages[j], ToolMessage):
                    tool_msgs.append(messages[j])
                    j += 1

                # 只有完美闭环，才保留 tool_calls
                if len(tool_msgs) == len(m.tool_calls):
                    formatted_tc = []
                    for tc in m.tool_calls:
                        tc_id = str(tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", ""))
                        tc_name = str(
                            tc.get("function", {}).get("name") if isinstance(tc, dict) else getattr(tc, "name", ""))
                        tc_args = tc.get("function", {}).get("arguments") if isinstance(tc, dict) else getattr(tc,
                                                                                                               "args",
                                                                                                               "{}")

                        if not isinstance(tc_args, str): tc_args = json.dumps(tc_args)

                        formatted_tc.append({
                            "id": tc_id, "type": "function",
                            "function": {"name": tc_name, "arguments": tc_args}
                        })
                    msg_dict["tool_calls"] = formatted_tc
                    openai_msgs.append(msg_dict)

                    for tm in tool_msgs:
                        openai_msgs.append({
                            "role": "tool",
                            "tool_call_id": str(tm.tool_call_id),
                            "content": str(tm.content)
                        })

                    i = j
                    continue
                else:
                    logger.warning("检测到残缺的工具调用，已丢弃")
                    openai_msgs.append(msg_dict)
                    i += 1
            else:
                openai_msgs.append(msg_dict)
                i += 1

        elif isinstance(m, ToolMessage):
            i += 1
        else:
            openai_msgs.append({"role": "user", "content": str(m.content)})
            i += 1

    full_content = ""
    tool_calls = []

    try:
        # 👇 核心：改用非流式的 chat_async
        response = await chat_async(openai_msgs, tools=tools_schema)
        full_content = response["content"]
        tool_calls = response.get("tool_calls") or []

        # 把生成的文本直接发给前端（虽然没有打字机效果，但一次显示完）
        if full_content:
            writer({"type": "text", "content": full_content})

    except Exception as e:
        logger.error(f"模型调用异常: {e}")
        writer({"type": "error", "content": str(e)})

    ai_msg = AIMessage(content=full_content)

    if tool_calls:
        for tc in tool_calls:
            if isinstance(tc, dict):
                if not tc.get("id"): tc["id"] = f"call_{uuid.uuid4().hex[:8]}"
                args = tc.get("function", {}).get("arguments", "{}")
                if not isinstance(args, str): tc["function"]["arguments"] = json.dumps(args)
        ai_msg.tool_calls = tool_calls

    return {"messages": [ai_msg]}


async def tools_node(state: AgentState) -> dict:
    messages = state["messages"]
    last_msg = messages[-1]

    tool_calls = getattr(last_msg, "tool_calls", None)
    if not tool_calls:
        return {"messages": []}

    results = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            func_info = tc.get("function", {})
            tool_name = func_info.get("name")
            tool_args = func_info.get("arguments")
            tool_id = tc.get("id")
        else:
            tool_name = getattr(tc, "name", None)
            tool_args = getattr(tc, "args", "{}")
            tool_id = getattr(tc, "id", "")

        if not tool_name:
            continue

        # 👇 终极兜底：如果没有 ID，生成一个
        if not tool_id:
            tool_id = f"call_{uuid.uuid4().hex[:8]}"

        if isinstance(tool_args, dict):
            tool_args_str = json.dumps(tool_args)
        else:
            tool_args_str = str(tool_args) if tool_args else "{}"

        logger.info(f"执行工具: {tool_name}, 参数: {tool_args_str}")
        result = await registry.execute(tool_name, tool_args_str)

        results.append(ToolMessage(
            content=str(result),
            tool_call_id=str(tool_id),
            name=str(tool_name)
        ))

    return {"messages": results}


async def save_and_extract(state: AgentState) -> dict:
    session_id = state["session_id"]
    user_input = state["user_input"]
    messages = state["messages"]

    ai_content = ""
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            ai_content = m.content
            break

    if ai_content:
        store.add_message(session_id, "assistant", ai_content)
        asyncio.create_task(extract_memory_background(user_input, ai_content))

    return {}


async def extract_memory_background(user_input: str, ai_content: str):
    from app.memory.extractor import extract_memories
    from app.memory.resolver import resolve_and_save_memory

    try:
        candidates = await extract_memories(user_input, ai_content)
        for c in candidates:
            await resolve_and_save_memory(
                store=store,
                memory_type=c["type"],
                content=c["content"]
            )
    except Exception as e:
        logger.error(f"后台记忆提取失败: {e}")