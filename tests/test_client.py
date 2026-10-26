import asyncio
from app.llm.client import chat_async

async def main():
    print("发起请求...")
    try:
        result = await chat_async([
            {"role": "user", "content": "用一句话介绍你自己"}
        ])
        print(f"回复内容: {result['content']}")
        print(f"Token 消耗: {result['usage']}")
    except Exception as e:
        print(f"请求失败: {e}")

if __name__ == "__main__":
    asyncio.run(main())