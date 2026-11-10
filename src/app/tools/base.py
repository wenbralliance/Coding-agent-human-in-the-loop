import asyncio
import inspect
import json
import logging
import time
from json import JSONDecodeError

from typing import Any, Callable, get_type_hints
from pydantic import BaseModel, ValidationError
from langsmith import trace

from langgraph.errors import GraphInterrupt
from app.config import settings
from app.observability import record_tool_call
from app.tools.process_manager import get_current_session


logger = logging.getLogger(__name__)


class Tool:
    """工具包装类"""

    def __init__(
        self,
        func: Callable,
        args_schema: type[BaseModel],
        description: str,
        *,
        retryable: bool = False,
        timeout: float | None = None,
    ):
        self.func = func
        self.args_schema = args_schema
        self.name = func.__name__
        self.description = description
        self.retryable = retryable
        self.timeout = timeout

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
        started_at = time.monotonic()
        status = "ok"
        error_code = ""
        interrupted = False
        async with trace(
            f"tool.{self.name}",
            run_type="tool",
            inputs={"arguments": arguments_json},
            metadata={
                "tool_name": self.name,
                "session_id": get_current_session(),
            },
            exceptions_to_handle=(GraphInterrupt,),
        ) as run:
            try:
                args_dict = json.loads(arguments_json)
                validated = self.args_schema(**args_dict)
                if inspect.iscoroutinefunction(self.func):
                    call = self.func(**validated.model_dump())
                    result = (
                        await asyncio.wait_for(call, timeout=self.timeout)
                        if self.timeout is not None
                        else await call
                    )
                else:
                    result = self.func(**validated.model_dump())
                try:
                    result_payload = json.loads(result)
                except (TypeError, JSONDecodeError):
                    result_payload = {}
                if isinstance(result_payload, dict):
                    result_status = str(result_payload.get("status") or "")
                    if result_status in {
                        "rejected",
                        "permission_denied",
                        "invalid_arguments",
                        "timeout",
                        "tool_error",
                        "loop_detected",
                        "dependency_blocked",
                        "bash_unavailable",
                        "session_lost",
                    }:
                        status = "error"
                        error_code = str(
                            result_payload.get("error_code")
                            or result_status.upper()
                        )
                run.end(outputs={"result": result})
                return result
            except GraphInterrupt:
                # 🛑 如果是人工审批中断，必须向上抛出，不能吞掉！
                interrupted = True
                raise
            except asyncio.TimeoutError:
                status = "error"
                error_code = "TIMEOUT"
                result = json.dumps(
                    {
                        "status": "timeout",
                        "error_code": error_code,
                        "message": f"工具 {self.name} 执行超时",
                        "retryable": self.retryable,
                    },
                    ensure_ascii=False,
                )
            except (JSONDecodeError, ValidationError) as exc:
                status = "error"
                error_code = "INVALID_ARGUMENT"
                result = json.dumps(
                    {
                        "status": "invalid_arguments",
                        "error_code": error_code,
                        "message": str(exc),
                        "retryable": False,
                    },
                    ensure_ascii=False,
                )
            except PermissionError as exc:
                status = "error"
                error_code = "PERMISSION_DENIED"
                result = json.dumps(
                    {
                        "status": "permission_denied",
                        "error_code": error_code,
                        "message": str(exc),
                        "retryable": False,
                    },
                    ensure_ascii=False,
                )
            except Exception as e:
                status = "error"
                error_code = (
                    "NOT_FOUND"
                    if isinstance(e, FileNotFoundError)
                    else "TOOL_ERROR"
                )
                result = json.dumps(
                    {
                        "status": "tool_error",
                        "error_code": error_code,
                        "message": str(e),
                        "retryable": self.retryable,
                    },
                    ensure_ascii=False,
                )
                run.end(outputs={"result": result, "status": "error"})
            finally:
                if not interrupted:
                    record_tool_call(
                        self.name,
                        arguments_json,
                        status=status,
                        error_code=error_code,
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                    )
            return result


def tool(
    args_schema: type[BaseModel],
    description: str,
    *,
    retryable: bool = False,
    timeout: float | None = None,
):
    """
    装饰器：把一个普通函数变成 Tool 对象。

    用法：
        @tool(WriteFileArgs, "写整个文件到 workspace")
        def write_file(filename: str, content: str) -> str:
            ...
    """
    def decorator(func: Callable) -> Tool:
        return Tool(
            func,
            args_schema,
            description,
            retryable=retryable,
            timeout=timeout,
        )
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
        tool_obj = self.tools[name]
        retries = max(0, settings.TOOL_MAX_RETRIES) if tool_obj.retryable else 0
        last_result = ""
        for attempt in range(retries + 1):
            last_result = await tool_obj.execute(arguments_json)
            if attempt >= retries:
                break
            try:
                payload = json.loads(last_result)
            except (TypeError, JSONDecodeError):
                break
            if payload.get("retryable") is not True:
                break
            if payload.get("status") not in {"tool_error", "timeout"}:
                break
            await asyncio.sleep(
                settings.LLM_RETRY_BACKOFF * (2 ** attempt)
                + (0.05 * (attempt + 1))
            )
        return last_result
