from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from app.config import settings


class TaskCapacityError(RuntimeError):
    def __init__(self, message: str, reason: str):
        self.reason = reason
        super().__init__(message)


@dataclass
class CapacityToken:
    session_id: str
    waited_ms: int


class TaskCapacityManager:
    """Global task cap plus one executing graph stream per session."""

    def __init__(self, max_concurrent: int) -> None:
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._guard:
            return self._session_locks.setdefault(session_id, asyncio.Lock())

    @asynccontextmanager
    async def slot(self, session_id: str) -> AsyncIterator[CapacityToken]:
        started_at = time.monotonic()
        session_lock = await self._session_lock(session_id)
        try:
            await asyncio.wait_for(
                session_lock.acquire(),
                timeout=settings.TASK_QUEUE_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise TaskCapacityError(
                "当前会话已有任务在运行，请等待完成或停止该任务。",
                "session_busy",
            ) from exc

        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=settings.TASK_QUEUE_TIMEOUT,
                )
            except asyncio.TimeoutError as exc:
                raise TaskCapacityError(
                    "当前执行队列已满，请稍后重试。",
                    "queue_full",
                ) from exc
            try:
                waited_ms = int((time.monotonic() - started_at) * 1000)
                yield CapacityToken(session_id=session_id, waited_ms=waited_ms)
            finally:
                self._semaphore.release()
        finally:
            session_lock.release()

    @property
    def available_slots(self) -> int:
        return self._semaphore._value


task_capacity = TaskCapacityManager(settings.MAX_CONCURRENT_TASKS)
