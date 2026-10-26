import asyncio
from app.memory.store import MemoryStore
from app.memory.extractor import extract_and_save_memories


async def main():
    store = MemoryStore()

    print("=== 测试提取 ===")
    user_msg = "你好，我叫小明。我主要用 Python 写代码，喜欢用类型注解。我下周三要去面试。"
    ai_msg = "你好小明，祝你面试顺利！"

    # 调用提取
    memories = await extract_and_save_memories(store, user_msg, ai_msg)

    print(f"\n共提取到 {len(memories)} 条:")
    for m in memories:
        print(f"  - [{m['type']}] {m['content']}")


if __name__ == "__main__":
    asyncio.run(main())