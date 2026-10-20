import asyncio
import json
import logging
from typing import Any, AsyncGenerator

import httpx
from app.config import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM 调用相关的自定义异常"""
    pass


async def chat_async(
        messages: list[dict],
        tools: list[dict] | None = None,
) -> dict:
    """
    异步非流式调用 LLM API。（用于后台记忆提取、代码审查等）
    """
    url = f"{settings.LLM_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": settings.LLM_TEMPERATURE,
        "max_tokens": settings.LLM_MAX_TOKENS,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    max_retries = settings.LLM_MAX_RETRIES
    backoff = settings.LLM_RETRY_BACKOFF
    timeout = settings.LLM_TIMEOUT

    last_error = None

    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=payload)

                if response.status_code in (401, 403, 404):
                    raise LLMError(f"API 认证或请求错误: {response.status_code} - {response.text}")

                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()

                data = response.json()
                choice = data["choices"][0]
                message = choice["message"]

                usage_data = data.get("usage", {})
                usage = {
                    "input_tokens": usage_data.get("prompt_tokens", 0),
                    "output_tokens": usage_data.get("completion_tokens", 0),
                }

                return {
                    "content": message.get("content") or "",
                    "tool_calls": message.get("tool_calls"),
                    "usage": usage,
                }

        except httpx.RequestError as e:
            last_error = e
            logger.warning(f"LLM 网络请求失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")
        except httpx.HTTPStatusError as e:
            last_error = e
            logger.warning(f"LLM API 错误 (尝试 {attempt + 1}/{max_retries + 1}): {e.response.status_code}")
        except Exception as e:
            raise LLMError(f"LLM 调用发生未知错误: {e}")

        if attempt < max_retries:
            wait_time = backoff * (2 ** attempt)
            await asyncio.sleep(wait_time)

    raise LLMError(f"LLM 调用失败，已达最大重试次数 ({max_retries})。最后错误: {last_error}")


async def chat_stream_async(
        messages: list[dict],
        tools: list[dict] | None = None,
) -> AsyncGenerator[dict, None]:
    """
    异步流式调用 LLM API。（用于前端实时输出）
    yield 字典：
      - {"type": "content", "text": "..."}  文本分片
      - {"type": "done", "content": "...", "tool_calls": [...], "usage": {...}} 结束
    """
    url = f"{settings.LLM_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": settings.LLM_TEMPERATURE,
        "max_tokens": settings.LLM_MAX_TOKENS,
        "stream": True,
        "stream_options": {"include_usage": True},  # 关键：让最后返回 token 统计
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    full_content = ""
    tool_calls_data = []
    usage_data = {}

    try:
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    err = await response.aread()
                    raise LLMError(f"API 错误: {response.status_code} - {err.decode()}")

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue

                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break

                    chunk = json.loads(data_str)

                    # 记录 usage（通常只有最后一块有）
                    if chunk.get("usage"):
                        usage_data = chunk["usage"]

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})

                    # 1. 处理文本片段
                    if "content" in delta and delta["content"]:
                        text = delta["content"]
                        full_content += text
                        yield {"type": "content", "text": text}

                    # 2. 处理工具调用片段（真实场景可能需要解析拼接 JSON）
                    if "tool_calls" in delta:
                        tool_calls_data.append(delta["tool_calls"])

    except Exception as e:
        # 流式出错，把错误也 yield 出去，前端好展示
        yield {"type": "error", "message": str(e)}
        return

    # 全部生成完毕，返回最终结果
    yield {
        "type": "done",
        "content": full_content,
        "tool_calls": tool_calls_data if tool_calls_data else None,
        "usage": {
            "input_tokens": usage_data.get("prompt_tokens", 0),
            "output_tokens": usage_data.get("completion_tokens", 0),
        },
    }