import asyncio
import json
from app.tools.base import ToolRegistry
from app.tools.file import write_file, read_file, list_files

async def main():
    # 1. 注册工具
    registry = ToolRegistry()
    registry.register(write_file)
    registry.register(read_file)
    registry.register(list_files)

    # 2. 查看生成的 Schema（发给模型的东西）
    print("=== 生成的 JSON Schema ===")
    schemas = registry.get_all_schemas()
    print(json.dumps(schemas[0], indent=2, ensure_ascii=False))  # 只看第一个

    # 3. 模拟模型调用工具
    print("\n=== 模拟执行 write_file ===")
    args = json.dumps({"filename": "test.py", "content": "print('hello')"})
    result = await registry.execute("write_file", args)
    print(result)

    print("\n=== 模拟执行 read_file ===")
    args = json.dumps({"filename": "test.py"})
    result = await registry.execute("read_file", args)
    print(result)

if __name__ == "__main__":
    asyncio.run(main())