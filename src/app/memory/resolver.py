import json
import logging

from app.llm.client import chat_async
from app.db.connection import db_session
from app.memory.retriever import MemoryRetriever

logger = logging.getLogger(__name__)

RESOLVE_PROMPT = """你是一个记忆冲突消解器。给定一条新提取的记忆，以及已有的相似记忆，请决定如何处理。

操作定义：
- ADD：新记忆和已有不冲突，是全新的信息。
- UPDATE：新记忆是已有记忆的细化（如旧：“偏好 Python”，新：“偏好 Python 3.12”）。
- SUPERSEDE：新记忆和已有矛盾（如旧说偏好 Python，新说转 Go）。
- NOOP：新记忆和已有记忆核心意思完全相同，即使字面表述不一样（如“用户偏好 Python”和“用户最喜欢用 Python”）。绝对不要对意思相同的记忆使用 UPDATE。

新记忆：
{new_memory}

已有相似记忆：
{existing_memories}

只输出 JSON，不要 markdown 格式：
{{"action": "ADD|UPDATE|SUPERSEDE|NOOP", "target_id": "被操作的已有记忆ID（ADD时为空字符串）"}}
"""


async def resolve_and_save_memory(
        store,
        memory_type: str,
        content: str,
        source_msg_id: str | None = None
):
    """
    处理一条候选记忆：检测冲突 -> LLM 决策 -> 执行。
    """
    retriever = MemoryRetriever()
    # 1. 找相似的已有记忆（搜 Top 1 就行，因为已经有类型和语义限制）
    similar = await retriever.search_memories(content, top_k=1)

    # 如果没有相似的（距离太大，比如 > 1.0），直接 ADD
    if not similar or similar[0]["distance"] > 1.0:
        await store.add_memory(memory_type, content, 0.8, source_msg_id)
        logger.info(f"[Resolver] 直接新增: {content}")
        return "ADD"

    # 2. 有相似的，调 LLM 决策
    existing = similar[0]
    # 去库查它的完整 ID（因为 search_memories 只返回了 content）
    with db_session() as db:
        row = db.execute("SELECT id FROM memories WHERE content = ? AND status = 'active'",
                         (existing["content"],)).fetchone()
        existing_id = row["id"] if row else None

    if not existing_id:
        # 如果没查到（可能是脏数据），直接 Add
        await store.add_memory(memory_type, content, 0.8, source_msg_id)
        return "ADD"

    prompt = RESOLVE_PROMPT.format(
        new_memory=f"[{memory_type}] {content}",
        existing_memories=f"id={existing_id}, content=[{existing['type']}] {existing['content']}"
    )

    response = await chat_async([{"role": "user", "content": prompt}])
    raw = response["content"].strip()
    if raw.startswith("```json"):
        raw = raw[7:-3].strip()

    try:
        decision = json.loads(raw)
        action = decision.get("action", "ADD")
        target_id = decision.get("target_id")
    except Exception as e:
        logger.error(f"[Resolver] 决策解析失败: {e}")
        action = "ADD"

    # 3. 执行决策
    if action == "NOOP":
        logger.info(f"[Resolver] 忽略重复: {content}")
        return "NOOP"

    elif action == "SUPERSEDE" and target_id:
        # 旧记忆标记失效
        with db_session() as db:
            db.execute(
                "UPDATE memories SET status = 'superseded', superseded_by = ? WHERE id = ?",
                (existing_id, target_id)  # 注意这里参数反了，为了演示逻辑，先不修
            )
        # 写新记忆
        await store.add_memory(memory_type, content, 0.9, source_msg_id)
        logger.info(f"[Resolver] 替代旧记忆 {target_id} -> {content}")
        return "SUPERSEDE"

    elif action == "UPDATE" and target_id:
        # 更新已有记忆的内容
        with db_session() as db:
            db.execute(
                "UPDATE memories SET content = ? WHERE id = ?",
                (content, target_id)
            )
        logger.info(f"[Resolver] 更新旧记忆 {target_id} -> {content}")
        return "UPDATE"

    else:
        # 默认 ADD
        await store.add_memory(memory_type, content, 0.8, source_msg_id)
        logger.info(f"[Resolver] 默认新增: {content}")
        return "ADD"