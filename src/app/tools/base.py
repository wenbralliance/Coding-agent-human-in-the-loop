import asyncio
import inspect
import json

from typing import Any, Callable, get_type_hints
from pydantic import BaseModel

from langgraph.errors import GraphInterrupt


class Tool:
    """工具包装类"""

    def __init__(self, func: Callable, args_schema: type[BaseModel], description: str):
        self.func = func
        self.args_schema = args_schema
        self.name = func.__name__
        self.description = description

    @property
    def schema(self) -> dict:
        """生成 OpenAI Function Calling 格式的 Schema"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_schema.model_json_schema(),
            },
        }

    async def execute(self, arguments_json: str) -> str:
        try:
            args_dict = json.loads(arguments_json)
            validated = self.args_schema(**args_dict)
            if inspect.iscoroutinefunction(self.func):
                return await self.func(**validated.model_dump())
            return self.func(**validated.model_dump())
        except GraphInterrupt:
            # 🛑 如果是人工审批中断，必须向上抛出，不能吞掉！
            raise
        except Exception as e:
            return f"工具执行失败: {e}"


def tool(args_schema: type[BaseModel], description: str):
    """
    装饰器：把一个普通函数变成 Tool 对象。

    用法：
        @tool(WriteFileArgs, "写整个文件到 workspace")
        def write_file(filename: str, content: str) -> str:
            ...
    """
    def decorator(func: Callable) -> Tool:
        return Tool(func, args_schema, description)
    return decorator


class ToolRegistry:
    """工具注册表"""

    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def register(self, tool_obj: Tool):
        """注册单个工具"""
        self.tools[tool_obj.name] = tool_obj

    def get_all_schemas(self) -> list[dict]:
        """获取所有工具的 Schema（发给模型）"""
        return [t.schema for t in self.tools.values()]

    async def execute(self, name: str, arguments_json: str) -> str:
        """根据模型返回的工具名和参数，执行工具"""
        if name not in self.tools:
            return f"错误：未知的工具 '{name}'"
        return await self.tools[name].execute(arguments_json)