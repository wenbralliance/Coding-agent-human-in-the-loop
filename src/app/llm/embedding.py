import asyncio
import logging
import httpx
from app.config import settings

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    pass


async def _call_embedding_api(texts: list[str]) -> list[list[float]]:
    """
    调用阿里 DashScope Embedding 接口。
    内部函数，处理 HTTP 请求、重试和排序。
    """
    url = f"{settings.EMBEDDING_BASE_URL}/services/embeddings/text-embedding/text-embedding"
    headers = {
        "Authorization": f"Bearer {settings.EMBEDDING_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.EMBEDDING_MODEL,
        "input": {"texts": texts},
        "parameters": {"dimension": settings.EMBEDDING_DIM},
    }

    max_retries = settings.EMBEDDING_MAX_RETRIES
    timeout = settings.EMBEDDING_TIMEOUT
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=payload)

                if response.status_code != 200:
                    raise EmbeddingError(f"API 错误: {response.status_code} - {response.text}")

                data = response.json()
                embeddings = data["output"]["embeddings"]

                # 兼容处理：阿里部分接口返回带 text_index，部分不带，没有就保持原顺序
                if embeddings and "text_index" in embeddings[0]:
                    embeddings.sort(key=lambda x: x["text_index"])

                return [item["embedding"] for item in embeddings]

        except httpx.RequestError as e:
            last_error = e
            logger.warning(f"Embedding 网络请求失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")
        except EmbeddingError as e:
            # 接口明确报错（如 Key 错、额度用完），不重试，直接抛
            raise e
        except Exception as e:
            raise EmbeddingError(f"Embedding 调用发生未知错误: {e}")

        if attempt < max_retries:
            wait_time = 1.0 * (2 ** attempt)
            await asyncio.sleep(wait_time)

    raise EmbeddingError(f"Embedding 调用失败，已达最大重试次数。最后错误: {last_error}")


async def embed_async(text: str) -> list[float]:
    """单条文本向量化"""
    results = await _call_embedding_api([text])
    return results[0]


async def embed_batch_async(texts: list[str]) -> list[list[float]]:
    """
    批量文本向量化。
    阿里限制单次最多 25 条，这里自动切片。
    """
    if not texts:
        return []

    batch_size = 25
    all_embeddings = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        batch_results = await _call_embedding_api(batch)
        all_embeddings.extend(batch_results)

    return all_embeddings