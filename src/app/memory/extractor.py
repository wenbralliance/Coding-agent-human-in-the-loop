import json
import logging
from app.llm.client import chat_async

logger = logging.getLogger(__name__)

# 提取记忆的 Prompt
EXTRACT_PROMPT = """你是一个记忆提取器。从下面的对话中提取值得长期记住的用户信息。

规则：
1. 只提取关于“用户”的稳定信息。
2. 提取类型只能从以下三种选：
   - preference：用户的偏好、习惯（如：“用户偏好 Python”、“用户喜欢简洁的回答”）
   - fact：用户的持久事实（如：“用户叫小明”、“用户是后端工程师”）
   - event：有时间性的短期状态或计划（如：“用户下周三面试”）
3. 不要提取：寒暄、临时提问、Agent 自己的话。
4. content 必须以“用户”开头。
5. 如果没有值得提取的信息，返回空数组 []。

输出格式要求：
只输出纯 JSON 数组，不要任何 markdown 代码块格式（不要写 ```json）。例如：
[
  {{"type": "fact", "content": "用户叫小明"}},
  {{"type": "preference", "content": "用户偏好 Python"}}
]

对话内容：
User: {user_msg}
Assistant: {ai_msg}
"""


async def extract_memories(user_msg: str, ai_msg: str) -> list[dict]:
    """
    从一轮对话中提取候选记忆。
    返回列表，如：[{"type": "fact", "content": "用户叫小明"}]
    注意：此函数不负责存库，存库由 resolver 负责。
    """
    # 1. 调用 LLM 提取
    prompt = EXTRACT_PROMPT.format(user_msg=user_msg, ai_msg=ai_msg)
    response = await chat_async([{"role": "user", "content": prompt}])
    content = response["content"].strip()

    # 2. 清理 JSON 格式（防止 LLM 画蛇添足加 markdown 代码块）
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()

    # 3. 解析并返回
    candidates = []
    try:
        parsed = json.loads(content)
        if not isinstance(parsed, list):
            return []

        for c in parsed:
            m_type = c.get("type")
            m_content = c.get("content")

            if not m_type or not m_content:
                continue
            if m_type not in ("preference", "fact", "event"):
                continue

            candidates.append({
                "type": m_type,
                "content": m_content
            })

        if candidates:
            logger.info(f"[Extractor] 成功提取 {len(candidates)} 条候选记忆。")

        return candidates

    except json.JSONDecodeError:
        logger.error(f"[Extractor] JSON 解析失败，原始输出: {content}")
        return []
    except Exception as e:
        logger.error(f"[Extractor] 记忆提取发生错误: {e}")
        return []