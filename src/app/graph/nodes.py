import asyncio
import hashlib
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
from app.observability import record_alert, record_tool_call
from app.security import assess_bash_command
from app.tools import registry
from app.tools.file import get_workspace_dir
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

    system_prompt = f"""你是一个执行型本地 Coding Agent。目标是在受限 workspace 内直接完成任务，并简洁汇报结果。

    【任务计划】
    1. 对任何编码、修改、诊断或运行任务，第一次工具调用必须是 `todo_write`。
    2. todo 步骤必须可验证；用 depends_on 声明前后依赖。无依赖且不会修改同一资源的步骤标记 parallelizable=true。
    3. 完成步骤后再次调用 `todo_write` 更新完整列表，不要遗漏未完成项。

    【Git Bash】
    1. 所有文件创建、修改、删除、搜索、构建和测试都使用 `bash_exec`；旧的写入工具已被移除。
    2. 一次调用可提交完整脚本，例如先写入文件和测试，再运行测试，减少工具轮次。
    3. 命令行状态在同一会话内保留。仅在确实需要隔离时设置 parallel=true。
    4. 每个 bash_exec 必须提供当前 todo_id。同一轮中依赖已满足、彼此独立的步骤可设置
       parallel=true 并行执行；同一个 todo 的多个命令仍按顺序执行。
    5. `rm`、Git 破坏性命令、代码执行、网络访问、workspace 外路径会触发审批；不要试图用变量、别名或解释器绕过风险检查。

    【执行规则】
    1. 每个请求最多 {settings.MAX_TOOL_ROUNDS} 轮工具调用。
    2. 不得重复执行完全相同的工具参数；同一指纹重复会触发死循环保护。
    3. 只有拿到工具结果后才给出最终答复。
    4. 测试必须实际调用被测逻辑，不要创建只输出固定成功文字的伪测试。
    5. GUI 程序先做隐藏窗口冒烟测试，再使用 `bash_exec` 启动正式窗口一次。

    【工具结果】
    - `completed`：命令成功。
    - `runtime_error`：根据 stderr 定位并修复。
    - `timeout`、`session_lost`：缩小命令、避免交互式进程，并重新执行必要步骤。
    - `rejected`、`permission_denied`：调整方案或说明受阻原因，不要绕过安全策略。
    - `loop_detected`：立即停止重复调用并调整方案。

    【最终答复】
    - 默认 1 到 3 句话，最多 6 行。
    - 只保留完成内容、关键文件路径、验证结果；失败时保留关键错误。
    - 不粘贴完整源码、思考过程或完整日志。
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
    llm_failed = False

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
        llm_failed = True

    ai_msg = AIMessage(content=full_content)

    if tool_calls:
        for tc in tool_calls:
            if isinstance(tc, dict):
                if not tc.get("id"): tc["id"] = f"call_{uuid.uuid4().hex[:8]}"
                args = tc.get("function", {}).get("arguments", "{}")
                if not isinstance(args, str): tc["function"]["arguments"] = json.dumps(args)
        ai_msg.tool_calls = tool_calls

    update: dict = {"messages": [ai_msg]}
    if llm_failed:
        update["termination_reason"] = "llm_error"
    return update


async def tool_limit(state: AgentState, writer: StreamWriter) -> dict:
    content = (
        f"已达到 {settings.MAX_TOOL_ROUNDS} 轮工具调用上限，任务尚未确认完成。"
        "请缩小任务范围后继续，或检查运行环境是否阻止了验证。"
    )
    writer({"type": "limit", "content": content})
    return {
        "messages": [AIMessage(content=content)],
        "termination_reason": state.get("termination_reason") or "tool_limit",
    }


def _normalize_tool_call(tool_call) -> tuple[str, str, str, dict]:
    if isinstance(tool_call, dict):
        func_info = tool_call.get("function", {})
        name = str(func_info.get("name") or "")
        raw_args = func_info.get("arguments")
        tool_id = str(tool_call.get("id") or "")
    else:
        name = str(getattr(tool_call, "name", "") or "")
        raw_args = getattr(tool_call, "args", "{}")
        tool_id = str(getattr(tool_call, "id", "") or "")

    if isinstance(raw_args, dict):
        arguments = json.dumps(raw_args, ensure_ascii=False, sort_keys=True)
        parsed = raw_args
    else:
        arguments = str(raw_args) if raw_args else "{}"
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return name, arguments, tool_id or f"call_{uuid.uuid4().hex[:8]}", parsed


def _tool_signature(name: str, arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
        if name == "bash_exec" and isinstance(parsed, dict):
            parsed.pop("todo_id", None)
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, json.JSONDecodeError):
        canonical = arguments
    return hashlib.sha256(f"{name}\0{canonical}".encode("utf-8")).hexdigest()


def _parallel_safe_call(name: str, args: dict, todos: dict[str, dict]) -> bool:
    if name in {"read_file", "read_files", "list_files", "grep_content", "analyze_code"}:
        return True
    if name != "bash_exec" or not bool(args.get("parallel")):
        return False
    todo_id = str(args.get("todo_id") or "")
    todo = todos.get(todo_id)
    if not todo or not bool(todo.get("parallelizable")):
        return False
    if any(
        todos.get(dependency, {}).get("status") != "completed"
        for dependency in todo.get("depends_on", [])
    ):
        return False
    command = str(args.get("command") or "")
    if not command:
        return False
    workspace = get_workspace_dir().resolve()
    raw_cwd = str(args.get("cwd") or "")
    task_cwd = (workspace / raw_cwd).resolve() if raw_cwd else workspace
    if not task_cwd.is_relative_to(workspace):
        return False
    assessment = assess_bash_command(command, task_cwd, workspace)
    return (
        not assessment.blocked
        and not assessment.needs_approval
        and assessment.risk_level in {"low", "medium"}
    )


def _extract_todo_items(result: str) -> list[dict]:
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return []
    if payload.get("status") != "updated":
        return []
    items = payload.get("items")
    return items if isinstance(items, list) else []


async def tools_node(state: AgentState) -> dict:
    messages = state["messages"]
    last_msg = messages[-1]
    tool_calls = getattr(last_msg, "tool_calls", None) or []
    next_round = int(state.get("tool_rounds", 0)) + 1
    if not tool_calls:
        return {"messages": [], "tool_rounds": next_round}

    normalized = [_normalize_tool_call(tool_call) for tool_call in tool_calls]
    current_todos = list(state.get("todos", []))

    if not current_todos and not any(name == "todo_write" for name, *_ in normalized):
        reminder = json.dumps(
            {
                "status": "plan_required",
                "error_code": "TODO_REQUIRED",
                "message": "开始执行前必须先调用 todo_write 创建任务步骤。",
            },
            ensure_ascii=False,
        )
        results = [
            ToolMessage(
                content=reminder,
                tool_call_id=tool_id,
                name=name or "unknown",
            )
            for name, _arguments, tool_id, _args in normalized
        ]
        return {
            "messages": results,
            "tool_rounds": next_round,
            "termination_reason": "todo_required",
        }

    previous_signatures = list(state.get("recent_tool_signatures", []))
    signature_counts: dict[str, int] = {}
    for signature in previous_signatures:
        signature_counts[signature] = signature_counts.get(signature, 0) + 1

    signatures: list[str] = []
    loop_indices: set[int] = set()
    dependency_indices: set[int] = set()
    threshold = max(2, settings.LOOP_REPEAT_THRESHOLD)
    for index, (name, arguments, _tool_id, _args) in enumerate(normalized):
        signature = _tool_signature(name, arguments)
        signatures.append(signature)
        signature_counts[signature] = signature_counts.get(signature, 0) + 1
        if signature_counts[signature] >= threshold:
            loop_indices.add(index)

    results: list[ToolMessage | None] = [None] * len(normalized)
    todo_map = {
        str(item.get("id")): item
        for item in current_todos
        if isinstance(item, dict) and item.get("id")
    }
    for index, (name, _arguments, tool_id, args) in enumerate(normalized):
        if index in loop_indices or name != "bash_exec":
            continue
        todo_id = str(args.get("todo_id") or "")
        todo = todo_map.get(todo_id)
        missing = [
            dependency
            for dependency in (todo or {}).get("depends_on", [])
            if todo_map.get(dependency, {}).get("status") != "completed"
        ]
        if not todo_id or todo is None:
            reason = "bash_exec 必须提供有效的 todo_id，先更新 todo_list。"
        elif missing:
            reason = f"依赖步骤尚未完成: {', '.join(missing)}"
        else:
            continue
        dependency_indices.add(index)
        payload = json.dumps(
            {
                "status": "dependency_blocked",
                "error_code": "TODO_DEPENDENCY_NOT_READY",
                "message": reason,
            },
            ensure_ascii=False,
        )
        results[index] = ToolMessage(
            content=payload,
            tool_call_id=tool_id,
            name=name,
        )
        record_tool_call(
            name,
            _arguments,
            status="error",
            error_code="TODO_DEPENDENCY_NOT_READY",
        )

    for index in sorted(loop_indices):
        name, arguments, tool_id, _args = normalized[index]
        payload = json.dumps(
            {
                "status": "loop_detected",
                "error_code": "LOOP_DETECTED",
                "message": (
                    f"相同工具调用已达到 {threshold} 次，已阻止继续执行。"
                    "请修改参数、改用其他工具或结束任务。"
                ),
            },
            ensure_ascii=False,
        )
        results[index] = ToolMessage(
            content=payload,
            tool_call_id=tool_id,
            name=name,
        )
        record_tool_call(
            name,
            arguments,
            status="error",
            error_code="LOOP_DETECTED",
        )

    async def execute_one(index: int) -> None:
        name, arguments, tool_id, _args = normalized[index]
        if results[index] is not None:
            return
        logger.info("执行工具: %s, 参数: %s", name, arguments)
        result = await registry.execute(name, arguments)
        results[index] = ToolMessage(
            content=str(result),
            tool_call_id=tool_id,
            name=name or "unknown",
        )

    todo_call_counts: dict[str, int] = {}
    for name, _arguments, _tool_id, args in normalized:
        if name == "bash_exec":
            todo_id = str(args.get("todo_id") or "")
            if todo_id:
                todo_call_counts[todo_id] = todo_call_counts.get(todo_id, 0) + 1

    parallel_indices = [
        index
        for index, (name, _arguments, _tool_id, args) in enumerate(normalized)
        if (
            index not in loop_indices
            and index not in dependency_indices
            and _parallel_safe_call(name, args, todo_map)
            and (
                name != "bash_exec"
                or todo_call_counts.get(str(args.get("todo_id") or ""), 0) == 1
            )
        )
    ]
    with tool_session(state["session_id"]):
        semaphore = asyncio.Semaphore(max(1, settings.MAX_PARALLEL_TOOLS))

        async def run_parallel(index: int) -> None:
            async with semaphore:
                await execute_one(index)

        index = 0
        while index < len(normalized):
            if index in loop_indices:
                index += 1
                continue
            if index in dependency_indices:
                index += 1
                continue
            if index not in parallel_indices:
                await execute_one(index)
                index += 1
                continue

            batch: list[int] = []
            while index < len(normalized) and index in parallel_indices:
                if index not in loop_indices:
                    batch.append(index)
                index += 1
            if batch:
                await asyncio.gather(*(run_parallel(item) for item in batch))

    final_results = [result for result in results if result is not None]
    for index, result in enumerate(results):
        if result is None:
            continue
        name = normalized[index][0]
        if name in {"todo_write", "todo_read"}:
            items = _extract_todo_items(str(result.content))
            if items:
                current_todos = items

    current_loop = bool(loop_indices)
    loop_detected = current_loop or bool(state.get("loop_detected", False))
    recovery_count = int(state.get("loop_recovery_count", 0))
    termination_reason = ""
    if current_loop:
        record_alert(
            "AGENT_LOOP_DETECTED",
            "critical",
            "相同工具调用指纹触发死循环保护。",
            float(len(loop_indices)),
            float(threshold),
        )
        if recovery_count < 1:
            recovery_count += 1
            termination_reason = "loop_recovery"
        else:
            termination_reason = "loop_detected"

    return {
        "messages": final_results,
        "tool_rounds": next_round,
        "todos": current_todos,
        "recent_tool_signatures": (previous_signatures + signatures)[-50:],
        "loop_detected": loop_detected,
        "loop_recovery_count": recovery_count,
        "termination_reason": termination_reason,
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
