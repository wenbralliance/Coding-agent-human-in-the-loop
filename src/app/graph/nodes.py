import asyncio
import json
import logging
import uuid

from langchain_core.messages import SystemMessage, AIMessage, ToolMessage
from langgraph.types import StreamWriter

from app.config import settings
from app.graph.state import AgentState
from app.memory.retriever import MemoryRetriever
from app.memory.store import MemoryStore
from app.llm.client import LLMError, chat_stream_async
from app.tools import registry
from app.tools.batch import describe_edit_batch
from app.tools.process_manager import tool_session

logger = logging.getLogger(__name__)

retriever = MemoryRetriever()
store = MemoryStore()


async def prepare_context(state: AgentState, writer: StreamWriter) -> dict:
    session_id = state["session_id"]
    user_input = state["user_input"]

    history = retriever.get_recent_messages(session_id, limit=10)
    try:
        memories = await retriever.search_memories(user_input, top_k=3)
    except Exception as exc:
        logger.warning("长期记忆召回失败，已降级继续执行: %s", exc)
        memories = []
        writer({
            "type": "warning",
            "content": f"长期记忆暂不可用，已继续执行当前任务：{exc}",
        })

    system_prompt = """你是一个执行型 Coding Agent。目标是直接完成用户任务，并尽量简洁地汇报结果。

    【工作规则】
    1. 用户要求编写或修改代码时，使用对应工具完成文件操作；写文件后必须调用 `run_file` 验证。
    2. 每个用户请求最多执行 12 轮工具调用，优先完成正式实现和必要验证。
    3. 只有拿到工具结果后才给出最终答复。

    【测试规则】
    1. 允许主动编写测试脚本，不需要询问用户。测试是正常的验证工作，不属于多余调试。
    2. 测试文件统一命名为 `test_<主模块>.py` 或 `tests/test_<主模块>.py`。
       同一任务只创建一个测试文件，后续修复时修改并复用，不要创建 debug1.py、debug2.py 等散落文件。
    3. 简单脚本优先直接运行验证；涉及输入校验、随机结果、边界条件或 GUI 交互时，应编写一个测试脚本。
    4. GUI 测试必须使用隐藏窗口并快速退出：
       创建窗口、验证控件和核心逻辑、调用一次 `update_idletasks()`，最后 `destroy()`。
       测试脚本不要进入永久运行的 `mainloop()`。
    5. 测试失败时修改现有测试或主程序后重新运行；测试通过后不要重复运行同一个测试。
    6. 推荐控制在 12 轮内：
       普通程序：写入主程序 → 写入测试 → 运行测试 → 修复或答复。
       GUI 程序：写入主程序 → 写入测试 → 运行测试 → 启动 GUI → 简短答复。
    7. 不要写一个只输出固定文字、固定数字或直接打印“验证成功”的脚本来冒充测试；
       测试必须实际调用被测函数或 GUI 控件并检查结果。

    【批量文件操作】
    1. 需要读取多个文件时优先一次调用 `read_files`，不要逐文件发起多轮读取。
    2. 新建两个及以上文件时必须一次调用 `write_files`，禁止连续多次调用 `write_file`。
       只有单个文件的一处小修改使用 `edit_file`。
    3. 同一文件多处修改，或需要同时修改代码、测试和 README 时，必须优先使用
       `apply_edit_batch` 一次提交完整操作列表。
    4. `apply_edit_batch` 会在后端统一校验路径、旧文本匹配、Python 语法和修改预算，
       任何一项失败都不会写入部分结果。
    5. 提交 batch 后立即运行测试；不要在没有验证时继续追加零散编辑。

    【GUI 启动】
    1. 隐藏窗口冒烟测试通过后，再使用 `run_file(mode="gui")` 启动正式窗口一次。
    2. GUI 主程序不要传 `mode="script"`；不确定时省略 mode，让 `auto` 自动判断。
    3. 收到 `gui_started` 后直接汇报完成，不要再启动或重复测试。

    【工具结果处理】
    - `completed`：当前这次脚本或测试运行成功。若正式 GUI 还没启动，继续完成剩余步骤；否则直接答复。
    - `gui_started`：正式窗口已成功启动，任务成功，不要再修改或重复运行。
    - `runtime_error`：根据错误定位并修复代码。
    - `dependency_error` 或 `environment_error`：不要盲目修改业务代码，先处理依赖、导入路径或运行环境。
    - `timeout`：检查死循环、阻塞调用或运行模式是否选择错误；GUI 正常存活不应视为超时。
    - 不要复述思考过程、工具参数、工具返回全文或已经完成的操作清单。

    【最终答复】
    - 默认控制在 1 到 3 句话，最多 6 行。
    - 只保留：完成了什么、关键文件路径、运行是否成功；失败时保留关键错误。
    - 用户没有明确要求时，不要粘贴完整源码，不要逐行解释代码。
    - 运行成功且用户没有要求查看输出时，只写“运行成功”，不要粘贴完整 stdout。
    - 默认使用纯文本，不要使用 Markdown 加粗、标题或代码围栏。
    - 只有用户明确要求时，才提供完整代码、详细解释或完整日志。
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
        async for event in chat_stream_async(
            openai_msgs,
            tools=tools_schema,
            session_id=state["session_id"],
            call_type="main",
        ):
            event_type = event.get("type")
            if event_type == "content":
                text = event.get("text") or ""
                full_content += text
                writer({"type": "text", "content": text})
            elif event_type == "done":
                full_content = event.get("content") or full_content
                tool_calls = event.get("tool_calls") or []
            elif event_type == "error":
                raise LLMError(event.get("message") or "模型流式调用失败")
    except Exception as exc:
        logger.error("模型调用异常: %s", exc)
        writer({"type": "error", "content": str(exc)})

    ai_msg = AIMessage(content=full_content)

    if tool_calls:
        for tc in tool_calls:
            if isinstance(tc, dict):
                if not tc.get("id"): tc["id"] = f"call_{uuid.uuid4().hex[:8]}"
                args = tc.get("function", {}).get("arguments", "{}")
                if not isinstance(args, str): tc["function"]["arguments"] = json.dumps(args)
        ai_msg.tool_calls = tool_calls

    return {"messages": [ai_msg]}


async def tool_limit(state: AgentState, writer: StreamWriter) -> dict:
    content = (
        f"已达到 {settings.MAX_TOOL_ROUNDS} 轮工具调用上限，任务尚未确认完成。"
        "请缩小任务范围后继续，或检查运行环境是否阻止了验证。"
    )
    writer({"type": "limit", "content": content})
    return {"messages": [AIMessage(content=content)]}


async def tools_node(state: AgentState) -> dict:
    messages = state["messages"]
    last_msg = messages[-1]

    tool_calls = getattr(last_msg, "tool_calls", None)
    if not tool_calls:
        return {
            "messages": [],
            "edit_batches": int(state.get("edit_batches", 0)),
            "edit_operations": int(state.get("edit_operations", 0)),
            "edited_files": list(state.get("edited_files", [])),
        }

    next_round = int(state.get("tool_rounds", 0)) + 1
    edit_batches = int(state.get("edit_batches", 0))
    edit_operations = int(state.get("edit_operations", 0))
    edited_files = set(state.get("edited_files", []))
    results = []
    with tool_session(state["session_id"]):
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
            if tool_name in {"apply_edit_batch", "write_files"}:
                if edit_batches >= settings.MAX_EDIT_BATCHES:
                    result = json.dumps({
                        "status": "rejected",
                        "applied": False,
                        "error": (
                            f"当前请求已达到 {settings.MAX_EDIT_BATCHES} 次批量编辑上限"
                        ),
                    }, ensure_ascii=False)
                else:
                    edit_batches += 1
                    try:
                        operation_count, batch_files = describe_edit_batch(tool_args_str)
                    except Exception:
                        operation_count = 0
                        batch_files = set()
                    if (
                        edit_operations + operation_count
                        > settings.MAX_EDIT_OPERATIONS_PER_REQUEST
                    ):
                        result = json.dumps({
                            "status": "rejected",
                            "applied": False,
                            "error": (
                                "批量操作数将超过当前请求上限 "
                                f"{settings.MAX_EDIT_OPERATIONS_PER_REQUEST}"
                            ),
                        }, ensure_ascii=False)
                    elif (
                        len(edited_files | batch_files)
                        > settings.MAX_EDIT_FILES_PER_REQUEST
                    ):
                        result = json.dumps({
                            "status": "rejected",
                            "applied": False,
                            "error": (
                                "修改文件数将超过当前请求上限 "
                                f"{settings.MAX_EDIT_FILES_PER_REQUEST}"
                            ),
                        }, ensure_ascii=False)
                    else:
                        result = await registry.execute(tool_name, tool_args_str)
                        try:
                            result_payload = json.loads(result)
                        except Exception:
                            result_payload = {}
                        if result_payload.get("status") == "applied":
                            edit_operations += operation_count
                            edited_files.update(batch_files)
            else:
                result = await registry.execute(tool_name, tool_args_str)

            results.append(ToolMessage(
                content=str(result),
                tool_call_id=str(tool_id),
                name=str(tool_name)
            ))

    return {
        "messages": results,
        "tool_rounds": next_round,
        "edit_batches": edit_batches,
        "edit_operations": edit_operations,
        "edited_files": sorted(edited_files),
    }


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
