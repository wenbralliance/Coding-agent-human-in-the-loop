import asyncio
import logging
from langchain_core.messages import SystemMessage, AIMessage, ToolMessage
from langgraph.types import StreamWriter

from app.graph.state import AgentState
from app.memory.retriever import MemoryRetriever
from app.memory.store import MemoryStore
from app.llm.client import chat_stream_async
from app.tools import registry

logger = logging.getLogger(__name__)

# 全局实例
retriever = MemoryRetriever()
store = MemoryStore()


async def prepare_context(state: AgentState) -> dict:
    """
    节点 1：准备上下文。
    拉取 L0 历史消息，召回 L3 长期记忆，拼装 System Prompt。
    """
    session_id = state["session_id"]
    user_input = state["user_input"]

    # 1. 拿 L0 最近 10 条历史消息
    history = retriever.get_recent_messages(session_id, limit=10)

    # 2. 拿 L3 语义相关记忆
    memories = await retriever.search_memories(user_input, top_k=3)

    # 3. 拼装 System Prompt (强制工具调用 + 结合记忆)
    system_prompt = """你是一个 Coding 助手。你具备读写文件和运行代码的能力。

【强制规则】
1. 当用户要求“写代码”、“生成脚本”、“修改文件”时，**必须**调用 `write_file` 工具将代码写入文件，绝对不能只在对话中输出代码！
2. 当用户要求“运行”时，**必须**调用 `run_file` 工具。
3. 写完后，请调用 `run_file` 验证。
4. 即使你觉得代码很简单，也必须走工具调用流程。
5. 本环境仅支持运行 Python 脚本，请使用 Python 编写代码。
"""

    if memories:
        mem_text = "\n".join(f"- [{m['type']}] {m['content']}" for m in memories)
        system_prompt += f"\n\n已知用户信息（请结合这些信息回答）：\n{mem_text}"

    # 4. 把用户最新说的话存进数据库（L0）
    store.add_message(session_id, "user", user_input)

    # 5. 组装给大模型的 messages
    msgs = [SystemMessage(content=system_prompt)]
    msgs.extend(state["messages"])

    return {"messages": msgs}


async def agent_node(state: AgentState, writer: StreamWriter) -> dict:
    """
    节点 2：调用大模型。
    改用 chat_stream_async，一边生成一边通过 writer 吐给前端。
    """
    messages = state["messages"]
    tools_schema = registry.get_all_schemas()

    # 把 LangChain 的 Message 对象转成 OpenAI 格式的 dict
    openai_msgs = []
    for m in messages:
        if isinstance(m, SystemMessage):
            openai_msgs.append({"role": "system", "content": m.content})
        elif isinstance(m, AIMessage):
            msg_dict = {"role": "assistant", "content": m.content or ""}
            if getattr(m, "tool_calls", None):
                msg_dict["tool_calls"] = m.tool_calls
            openai_msgs.append(msg_dict)
        elif isinstance(m, ToolMessage):
            openai_msgs.append({
                "role": "tool",
                "tool_call_id": m.tool_call_id,
                "content": m.content
            })
        else:
            # HumanMessage
            openai_msgs.append({"role": "user", "content": m.content})

    # 👇 核心改造：使用流式接口
    full_content = ""
    tool_calls = []

    async for chunk in chat_stream_async(openai_msgs, tools=tools_schema):
        if chunk["type"] == "content":
            text = chunk["text"]
            full_content += text
            # 把碎片实时推给前端
            writer({"type": "text", "content": text})

        elif chunk["type"] == "done":
            tool_calls = chunk.get("tool_calls") or []

        elif chunk["type"] == "error":
            logger.error(f"流式生成错误: {chunk.get('message')}")
            writer({"type": "error", "content": chunk.get("message")})

    # 组装 AIMessage 存回状态
    ai_msg = AIMessage(content=full_content)
    if tool_calls:
        # 把原生返回的 tool_calls 挂到 LangChain 的 AIMessage 上
        ai_msg.tool_calls = tool_calls

    return {"messages": [ai_msg]}


async def tools_node(state: AgentState) -> dict:
    """
    节点 3：执行工具。
    遍历大模型返回的 tool_calls，执行并返回 ToolMessage。
    """
    messages = state["messages"]
    last_msg = messages[-1]

    tool_calls = getattr(last_msg, "tool_calls", None)
    if not tool_calls:
        return {"messages": []}

    results = []
    for tc in tool_calls:
        # 注意：这里的 tc 可能是不同格式，兼容处理
        tool_name = tc.get("function", {}).get("name") or tc.get("name")
        tool_args = tc.get("function", {}).get("arguments") or tc.get("args")
        tool_id = tc.get("id")

        # 执行工具
        logger.info(f"执行工具: {tool_name}")
        result = await registry.execute(tool_name, tool_args)

        # 封装成 ToolMessage 存回状态
        results.append(ToolMessage(
            content=result,
            tool_call_id=tool_id,
            name=tool_name
        ))

    return {"messages": results}


async def save_and_extract(state: AgentState) -> dict:
    """
    节点 4：保存对话，并在后台异步提取记忆。
    """
    session_id = state["session_id"]
    user_input = state["user_input"]
    messages = state["messages"]

    # 找最后一条 AI 的回复
    ai_content = ""
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            ai_content = m.content
            break

    if ai_content:
        # 1. 存入 L0 历史
        store.add_message(session_id, "assistant", ai_content)

        # 2. 后台异步提取记忆
        asyncio.create_task(extract_memory_background(user_input, ai_content))

    return {}


async def extract_memory_background(user_input: str, ai_content: str):
    """后台任务：提取候选记忆，通过 Resolver 冲突消解后存入"""
    from app.memory.extractor import extract_memories
    from app.memory.resolver import resolve_and_save_memory

    try:
        # 1. 提取候选
        candidates = await extract_memories(user_input, ai_content)

        # 2. 遍历，逐条消解并入库
        for c in candidates:
            await resolve_and_save_memory(
                store=store,
                memory_type=c["type"],
                content=c["content"]
            )
    except Exception as e:
        logger.error(f"[Background] 记忆提取与消解失败: {e}")