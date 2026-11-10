from __future__ import annotations

import json

from app.graph.todo import (
    TodoItem,
    TodoReadArgs,
    TodoWriteArgs,
    todo_payload,
    todo_registry,
    validate_todo_items,
)
from app.tools.base import tool
from app.tools.process_manager import get_current_session


@tool(
    TodoWriteArgs,
    "创建或完整更新任务 todo_list。开始编码任务时先调用本工具；用 depends_on 声明依赖，"
    "无依赖且不会修改同一资源的步骤可标记 parallelizable=true。",
)
def todo_write(items: list[TodoItem]) -> str:
    validate_todo_items(items)
    session_id = get_current_session() or "unknown"
    todo_registry.set(session_id, items)
    return json.dumps(todo_payload(items), ensure_ascii=False)


@tool(TodoReadArgs, "读取当前会话的 todo_list。")
def todo_read() -> str:
    session_id = get_current_session() or "unknown"
    current = todo_registry.get(session_id)
    return json.dumps(todo_payload(current), ensure_ascii=False)
