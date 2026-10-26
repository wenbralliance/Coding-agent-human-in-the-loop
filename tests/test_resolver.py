import asyncio
import logging

from app.memory.store import MemoryStore
from app.memory.resolver import resolve_and_save_memory
from app.db.connection import db_session

logging.basicConfig(level=logging.INFO, format="%(message)s")


def clear_db():
    """清空所有记忆和向量，保证测试环境干净"""
    with db_session() as db:
        db.execute("DELETE FROM memories")
        db.execute("DELETE FROM memory_vectors")
        db.execute("DELETE FROM memory_sources")
        db.execute("DELETE FROM memory_events")

async def main():
    store = MemoryStore()

    print("=== 第 1 次：新增偏好 ===")
    await resolve_and_save_memory(store, "preference", "用户偏好使用 Python 语言")

    print("\n=== 第 2 次：完全重复 ===")
    await resolve_and_save_memory(store, "preference", "用户最喜欢用 Python")

    print("\n=== 第 3 次：矛盾替代 ===")
    await resolve_and_save_memory(store, "preference", "用户转用 Go 语言，不再用 Python 了")


if __name__ == "__main__":
    clear_db()
    asyncio.run(main())