import asyncio
from app.llm.embedding import embed_async, embed_batch_async


async def main():
    print("=== 1. 测试单条向量化 ===")
    text = "用户偏好 Python 语言"
    vec = await embed_async(text)
    print(f"文本: {text}")
    print(f"向量维度: {len(vec)}")
    print(f"前 5 个值: {vec[:5]}")

    assert len(vec) == 1024, "维度不是 1024！"

    print("\n=== 2. 测试批量向量化 ===")
    texts = [
        "用户叫小明",
        "用户在做 LangGraph 项目",
        "用户最近在调 checkpointer",
        "用户下周要面试",
        "用户的代码风格喜欢用类型注解"
    ]
    vecs = await embed_batch_async(texts)
    print(f"批量条数: {len(vecs)}")
    for i, v in enumerate(vecs):
        print(f"  {texts[i]} -> 维度 {len(v)}")


if __name__ == "__main__":
    asyncio.run(main())