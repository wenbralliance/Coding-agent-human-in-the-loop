import struct
from app.db.connection import db_session
from app.llm.embedding import embed_async


def vec_to_bytes(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


class MemoryRetriever:
    def __init__(self):
        pass

    def get_recent_messages(self, session_id: str, limit: int = 10) -> list[dict]:
        """
        获取当前会话的最近 N 条消息（L0 短期记忆）。
        """
        with db_session() as db:
            rows = db.execute(
                """SELECT role, content FROM messages 
                   WHERE session_id = ? 
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit)
            ).fetchall()

        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def search_memories(self, query: str, top_k: int = 3) -> list[dict]:
        """
        根据用户的提问，从长期记忆库中召回最相关的信息（L3）。
        """
        if not query.strip():
            return []

        query_vec = await embed_async(query)
        vec_bytes = vec_to_bytes(query_vec)

        # sqlite-vec 要求使用 k = ? 而不是 LIMIT
        sql = """
            SELECT m.content, m.type, v.distance 
            FROM memory_vectors v
            JOIN memories m ON v.memory_id = m.id
            WHERE v.embedding MATCH ? 
              AND k = ?
              AND m.status = 'active'
            ORDER BY v.distance
        """

        with db_session() as db:
            rows = db.execute(sql, (vec_bytes, top_k)).fetchall()

        results = []
        for r in rows:
            if r["distance"] < 1.2:
                results.append({
                    "content": r["content"],
                    "type": r["type"],
                    "distance": r["distance"]
                })
        return results