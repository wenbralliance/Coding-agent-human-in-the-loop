import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator

import httpx
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from app.config import settings
from app.llm.resilience import (
    CircuitOpenError,
    get_llm_breaker,
)
from app.llm.tracing import configure_langsmith
from app.llm.tracker import record_llm_call


logger = logging.getLogger(__name__)
configure_langsmith()


class LLMError(Exception):
    """LLM 调用相关的自定义异常。"""


class RetryableLLMError(LLMError):
    """A transient failure that can be retried."""


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
    }


def _payload(messages: list[dict], tools: list[dict] | None, *, stream: bool) -> dict:
    payload: dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": settings.LLM_TEMPERATURE,
        "max_tokens": settings.LLM_MAX_TOKENS,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    return payload


def _extract_usage(usage_data: dict) -> dict:
    details = usage_data.get("prompt_tokens_details") or {}
    return {
        "input_tokens": usage_data.get("prompt_tokens", 0),
        "output_tokens": usage_data.get("completion_tokens", 0),
        "total_tokens": usage_data.get("total_tokens", 0),
        "cached_tokens": (
            usage_data.get("prompt_cache_hit_tokens")
            or details.get("cached_tokens", 0)
        ),
        "cache_miss_tokens": usage_data.get("prompt_cache_miss_tokens", 0),
    }


def _build_usage_metadata(usage: dict) -> dict:
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    total_tokens = int(
        usage.get("total_tokens", 0) or (input_tokens + output_tokens)
    )
    cached_tokens = int(usage.get("cached_tokens", 0) or 0)
    uncached_tokens = max(0, input_tokens - cached_tokens)

    input_cost = (
        uncached_tokens * settings.LLM_INPUT_COST_PER_MILLION
        + cached_tokens * settings.LLM_CACHED_INPUT_COST_PER_MILLION
    ) / 1_000_000
    output_cost = (
        output_tokens * settings.LLM_OUTPUT_COST_PER_MILLION
    ) / 1_000_000

    metadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_cost": round(input_cost, 10),
        "output_cost": round(output_cost, 10),
        "total_cost": round(input_cost + output_cost, 10),
    }
    if cached_tokens:
        metadata["input_token_details"] = {"cache_read": cached_tokens}
    return metadata


def _attach_langsmith_metadata(usage_metadata: dict | None = None) -> None:
    try:
        run_tree = get_current_run_tree()
        if run_tree is None:
            return
        if getattr(run_tree, "run_type", None) != "llm":
            return
        run_tree.metadata["ls_provider"] = settings.LLM_PROVIDER
        run_tree.metadata["ls_model_name"] = settings.LLM_MODEL
        if usage_metadata:
            run_tree.metadata["usage_metadata"] = usage_metadata
    except Exception:
        logger.debug("无法附加 LangSmith usage metadata", exc_info=True)


def _record_usage(
    *,
    session_id: str | None,
    call_type: str,
    usage: dict,
) -> None:
    try:
        record_llm_call(
            call_type=call_type,
            model=settings.LLM_MODEL,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            session_id=session_id,
        )
    except Exception as exc:
        logger.warning("记录 LLM Token 使用失败: %s", exc)


def _format_http_error(status_code: int, body: str) -> str:
    return f"LLM API 错误: HTTP {status_code} - {body[:2000]}"


def _process_chat_trace_output(outputs: dict) -> dict:
    if not isinstance(outputs, dict):
        return outputs
    usage = outputs.get("usage")
    if not isinstance(usage, dict):
        return outputs
    traced_outputs = dict(outputs)
    traced_outputs["usage_metadata"] = _build_usage_metadata(usage)
    return traced_outputs


@traceable(
    run_type="llm",
    name="llm.chat",
    process_outputs=_process_chat_trace_output,
)
async def chat_async(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    session_id: str | None = None,
    call_type: str = "main",
) -> dict:
    """异步非流式 LLM 调用，包含重试、熔断和 LangSmith 追踪。"""
    _attach_langsmith_metadata()
    url = f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions"
    payload = _payload(messages, tools, stream=False)
    breaker = get_llm_breaker()
    max_retries = max(0, settings.LLM_MAX_RETRIES)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            breaker.before_request()
            async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
                response = await client.post(url, headers=_headers(), json=payload)

            if response.status_code == 200:
                try:
                    data = response.json()
                    choice = data["choices"][0]
                    message = choice["message"]
                except Exception as exc:
                    breaker.record_failure()
                    raise RetryableLLMError(
                        f"LLM 返回了无法解析的响应: {exc}"
                    ) from exc

                breaker.record_success()
                usage = _extract_usage(data.get("usage", {}))
                usage_metadata = _build_usage_metadata(usage)
                _attach_langsmith_metadata(usage_metadata)
                _record_usage(
                    session_id=session_id,
                    call_type=call_type,
                    usage=usage,
                )
                return {
                    "content": message.get("content") or "",
                    "tool_calls": message.get("tool_calls"),
                    "usage": usage,
                }

            error_text = _format_http_error(response.status_code, response.text)
            if _is_retryable_status(response.status_code):
                breaker.record_failure()
                raise RetryableLLMError(error_text)

            # A valid 4xx response proves the service is reachable.
            breaker.record_success()
            raise LLMError(error_text)

        except CircuitOpenError as exc:
            raise LLMError(str(exc)) from exc
        except RetryableLLMError as exc:
            last_error = exc
            logger.warning(
                "LLM 临时失败 (尝试 %s/%s): %s",
                attempt + 1,
                max_retries + 1,
                exc,
            )
        except httpx.RequestError as exc:
            last_error = exc
            breaker.record_failure()
            logger.warning(
                "LLM 网络请求失败 (尝试 %s/%s): %s",
                attempt + 1,
                max_retries + 1,
                exc,
            )
        except LLMError:
            raise
        except Exception as exc:
            breaker.record_failure()
            last_error = exc
            logger.exception("LLM 调用发生未知错误")

        if attempt < max_retries:
            await asyncio.sleep(settings.LLM_RETRY_BACKOFF * (2 ** attempt))

    raise LLMError(
        f"LLM 调用失败，已达最大重试次数 ({max_retries})。最后错误: {last_error}"
    )


def _merge_tool_call_fragment(
    buffers: dict[int, dict],
    fragment: dict,
    fallback_index: int,
) -> None:
    index = fragment.get("index", fallback_index)
    if not isinstance(index, int):
        index = fallback_index

    buffer = buffers.setdefault(
        index,
        {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if fragment.get("id"):
        buffer["id"] = fragment["id"]
    if fragment.get("type"):
        buffer["type"] = fragment["type"]

    function = fragment.get("function") or {}
    if function.get("name"):
        buffer["function"]["name"] += function["name"]
    if function.get("arguments"):
        buffer["function"]["arguments"] += function["arguments"]


def _finalize_tool_calls(buffers: dict[int, dict]) -> list[dict] | None:
    finalized = []
    for index in sorted(buffers):
        item = buffers[index]
        if not item["function"]["name"]:
            continue
        if not item["id"]:
            item["id"] = f"call_stream_{index}"
        finalized.append(item)
    return finalized or None


def _reduce_stream_chunks(chunks: list[dict]) -> dict:
    for chunk in reversed(chunks):
        if chunk.get("type") in {"done", "error"}:
            return chunk
    return {
        "type": "incomplete",
        "chunks": len(chunks),
    }


@traceable(
    run_type="llm",
    name="llm.chat_stream",
    reduce_fn=_reduce_stream_chunks,
)
async def chat_stream_async(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    session_id: str | None = None,
    call_type: str = "main",
) -> AsyncGenerator[dict, None]:
    """
    异步流式调用 LLM。

    yield:
      - {"type": "content", "text": "..."}
      - {"type": "done", "content": "...", "tool_calls": [...], "usage": {...}}
      - {"type": "error", "message": "..."}
    """
    _attach_langsmith_metadata()
    url = f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions"
    payload = _payload(messages, tools, stream=True)
    breaker = get_llm_breaker()
    max_retries = max(0, settings.LLM_MAX_RETRIES)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        started_streaming = False
        try:
            breaker.before_request()
            async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=_headers(),
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        error_text = _format_http_error(response.status_code, body)
                        if _is_retryable_status(response.status_code):
                            breaker.record_failure()
                            raise RetryableLLMError(error_text)
                        breaker.record_success()
                        yield {"type": "error", "message": error_text}
                        return

                    breaker.record_success()
                    full_content = ""
                    usage_data: dict = {}
                    tool_buffers: dict[int, dict] = {}

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if not data_str:
                            continue
                        if data_str == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError as exc:
                            yield {
                                "type": "error",
                                "message": f"LLM 流式 JSON 解析失败: {exc}",
                            }
                            return

                        if chunk.get("usage"):
                            usage_data = chunk["usage"]

                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}

                        text = delta.get("content")
                        if text:
                            started_streaming = True
                            full_content += text
                            yield {"type": "content", "text": text}

                        for fallback_index, fragment in enumerate(
                            delta.get("tool_calls") or []
                        ):
                            if isinstance(fragment, dict):
                                _merge_tool_call_fragment(
                                    tool_buffers,
                                    fragment,
                                    fallback_index,
                                )

                    usage = _extract_usage(usage_data)
                    usage_metadata = _build_usage_metadata(usage)
                    _attach_langsmith_metadata(usage_metadata)
                    _record_usage(
                        session_id=session_id,
                        call_type=call_type,
                        usage=usage,
                    )
                    yield {
                        "type": "done",
                        "content": full_content,
                        "tool_calls": _finalize_tool_calls(tool_buffers),
                        "usage": usage,
                        "usage_metadata": usage_metadata,
                    }
                    return

        except CircuitOpenError as exc:
            yield {"type": "error", "message": str(exc)}
            return
        except RetryableLLMError as exc:
            last_error = exc
            logger.warning(
                "LLM 流式临时失败 (尝试 %s/%s): %s",
                attempt + 1,
                max_retries + 1,
                exc,
            )
        except httpx.RequestError as exc:
            last_error = exc
            breaker.record_failure()
            logger.warning(
                "LLM 流式网络请求失败 (尝试 %s/%s): %s",
                attempt + 1,
                max_retries + 1,
                exc,
            )
        except Exception as exc:
            logger.exception("LLM 流式调用失败")
            if not started_streaming:
                breaker.record_failure()
            yield {"type": "error", "message": str(exc)}
            return

        if attempt < max_retries:
            await asyncio.sleep(settings.LLM_RETRY_BACKOFF * (2 ** attempt))

    yield {
        "type": "error",
        "message": (
            f"LLM 流式调用失败，已达最大重试次数 ({max_retries})。"
            f"最后错误: {last_error}"
        ),
    }
