import asyncio
from app.llm.client import chat_stream_async


async def main():
    print("开始流式请求...\n")

    messages = [
        {"role": "user", "content": "用3句话介绍一下 Python 语言"}
    ]

    # 使用 async for 迭代流式结果
    async for chunk in chat_stream_async(messages):
        if chunk["type"] == "content":
            # 模拟打字机效果，不换行，直接打印
            print(chunk["text"], end="", flush=True)

        elif chunk["type"] == "done":
            print("\n\n=== 生成完毕 ===")
            print(f"完整内容长度: {len(chunk['content'])}")
            print(f"Token 统计: {chunk['usage']}")

        elif chunk["type"] == "error":
            print(f"\n\n[错误]: {chunk['message']}")


if __name__ == "__main__":
    asyncio.run(main())