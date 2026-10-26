import asyncio
from app.memory.store import MemoryStore
from app.memory.retriever import MemoryRetriever


async def main():
    store = MemoryStore()
    retriever = MemoryRetriever()

    print("=== 1. 测试会话和消息（L0） ===")
    session_id = store.create_session("编程测试会话")
    print(f"创建会话: {session_id}")

    store.add_message(session_id, "user", "你好，我叫小明")
    store.add_message(session_id, "assistant", "你好小明，有什么可以帮你？")

    recent = retriever.get_recent_messages(session_id, limit=5)
    print(f"拉取到 {len(recent)} 条消息:")
    for msg in recent:
        print(f"  [{msg['role']}] {msg['content']}")

    print("\n=== 2. 测试长期记忆写入（L3） ===")
    await store.add_memory("preference", "用户偏好使用 Python 语言", 0.8)
    await store.add_memory("fact", "用户最近在做 LangGraph 项目", 0.7)
    await store.add_memory("event", "用户下周三要参加面试", 0.6)
    print("成功写入 3 条长期记忆（含向量）")

    print("\n=== 3. 测试语义检索 ===")
    queries = [
        "我最喜欢什么语言？",  # 应该命中"偏好 Python"
        "我在搞什么框架？",  # 应该命中"做 LangGraph"
        "今天天气怎么样？"  # 不应该命中任何记忆
    ]

    for q in queries:
        print(f"\n查询: {q}")
        results = await retriever.search_memories(q, top_k=2)
        if not results:
            print("  未找到相关记忆")
        for r in results:
            print(f"  - [{r['type']}] {r['content']} (距离: {r['distance']:.4f})")


if __name__ == "__main__":
    asyncio.run(main())