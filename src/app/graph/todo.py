from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Literal

from pydantic import BaseModel, Field

TodoStatus = Literal["pending", "in_progress", "completed", "blocked"]


class TodoItem(BaseModel):
    id: str = Field(min_length=1, max_length=40, description="稳定的任务步骤 ID")
    title: str = Field(min_length=1, max_length=200, description="可验证的任务步骤")
    status: TodoStatus = "pending"
    depends_on: list[str] = Field(
        default_factory=list,
        description="必须在当前步骤之前完成的步骤 ID",
    )
    parallelizable: bool = Field(
        default=True,
        description="依赖满足后是否允许与其他步骤并行",
    )


class TodoWriteArgs(BaseModel):
    items: list[TodoItem] = Field(min_length=1, max_length=30)


class TodoReadArgs(BaseModel):
    pass


class TodoPlanError(ValueError):
    pass


def validate_todo_items(items: list[TodoItem]) -> None:
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise TodoPlanError("todo 步骤 ID 不能重复")

    known = set(ids)
    for item in items:
        unknown = set(item.depends_on) - known
        if unknown:
            raise TodoPlanError(
                f"步骤 {item.id} 依赖不存在的步骤: {sorted(unknown)}"
            )
        if item.id in item.depends_on:
            raise TodoPlanError(f"步骤 {item.id} 不能依赖自身")

    indegree = {item.id: len(set(item.depends_on)) for item in items}
    dependents: dict[str, set[str]] = defaultdict(set)
    for item in items:
        for dependency in set(item.depends_on):
            dependents[dependency].add(item.id)

    ready = deque(sorted(item_id for item_id, degree in indegree.items() if degree == 0))
    visited = 0
    while ready:
        current = ready.popleft()
        visited += 1
        for dependent in sorted(dependents[current]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if visited != len(items):
        raise TodoPlanError("todo 依赖关系存在环")

def build_execution_waves(items: list[TodoItem]) -> list[list[str]]:
    """Return topological levels; each level may execute in parallel."""
    validate_todo_items(items)
    levels: dict[str, int] = {}
    by_id = {item.id: item for item in items}
    visiting: set[str] = set()

    def level(item_id: str) -> int:
        if item_id in levels:
            return levels[item_id]
        if item_id in visiting:
            raise TodoPlanError("todo 依赖关系存在环")
        visiting.add(item_id)
        dependencies = by_id[item_id].depends_on
        value = 0 if not dependencies else max(level(dep) for dep in dependencies) + 1
        visiting.remove(item_id)
        levels[item_id] = value
        return value

    for item in items:
        level(item.id)

    grouped: dict[int, list[str]] = defaultdict(list)
    for item in items:
        grouped[levels[item.id]].append(item.id)
    return [sorted(grouped[index]) for index in sorted(grouped)]


class TodoRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plans: dict[str, list[TodoItem]] = {}

    def set(self, session_id: str, items: list[TodoItem]) -> None:
        with self._lock:
            self._plans[session_id] = list(items)

    def get(self, session_id: str) -> list[TodoItem]:
        with self._lock:
            return list(self._plans.get(session_id, []))

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._plans.pop(session_id, None)


todo_registry = TodoRegistry()


def todo_payload(items: list[TodoItem]) -> dict:
    waves = build_execution_waves(items)
    return {
        "status": "updated",
        "items": [item.model_dump() for item in items],
        "execution_waves": waves,
        "summary": {
            "total": len(items),
            "completed": sum(item.status == "completed" for item in items),
            "parallelizable": sum(item.parallelizable for item in items),
        },
    }
