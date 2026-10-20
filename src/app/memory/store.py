import uuid
import struct
from app.db.connection import db_session
from app.llm.embedding import embed_async


def vec_to_bytes(vec: list[float]) -> bytes:
    """
    将浮点数列表转换为 sqlite-vec 能识别的二进制字节串。
    sqlite-vec 要求向量以 float32 数组的二进制形式存入。
    """
    return struct.pack(f"{len(vec)}f", *vec)


class MemoryStore:
    def __init__(self):
        pass

    def create_session(self, title: str = "新对话") -> str:
        """创建一个新会话，返回生成的 session_id。"""
        session_id = f"s_{uuid.uuid4().hex[:8]}"
        with db_session() as db:
            db.execute(
                "INSERT INTO sessions(id, title) VALUES (?, ?)",
                (session_id, title)
            )
        return session_id

    def delete_session(self, session_id: str) -> dict | None:
        """删除会话及其本地关联数据，返回删除统计。"""
        with db_session() as db:
            exists = db.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not exists:
                return None

            deleted_sources = db.execute(
                """
                DELETE FROM memory_sources
                WHERE message_id IN (
                    SELECT id FROM messages WHERE session_id = ?
                )
                """,
                (session_id,),
            ).rowcount
            deleted_messages = db.execute(
                "DELETE FROM messages WHERE session_id = ?",
                (session_id,),
            ).rowcount
            deleted_llm_calls = db.execute(
                "DELETE FROM llm_calls WHERE session_id = ?",
                (session_id,),
            ).rowcount
            db.execute(
                "DELETE FROM sessions WHERE id = ?",
                (session_id,),
            )

        return {
            "deleted_messages": deleted_messages,
            "deleted_llm_calls": deleted_llm_calls,
            "deleted_memory_sources": deleted_sources,
        }

    def add_message(self, session_id: str, role: str, content: str) -> str:
        """存一条消息，返回 msg_id"""
        msg_id = f"m_{uuid.uuid4().hex[:8]}"
        with db_session() as db:
            # 1. 插入消息
            db.execute(
                "INSERT INTO messages(id, session_id, role, content) VALUES (?, ?, ?, ?)",
                (msg_id, session_id, role, content)
            )

            # 👇 2. 如果是用户发的消息，并且会话标题还是默认的，就把标题改成这句话
            if role == "user":
                db.execute(
                    "UPDATE sessions SET title = ? WHERE id = ? AND title = '新对话'",
                    (content[:20], session_id)  # 取前20个字符作为标题
                )
        return msg_id

    async def add_memory(
            self,
            memory_type: str,
            content: str,
            importance: float = 0.5,
            source_msg_id: str | None = None  # 👈 新增参数
    ) -> str:
        """
        存长期记忆（自动向量化）。增加溯源信息。
        """
        memory_id = f"mem_{uuid.uuid4().hex[:8]}"

        embedding = await embed_async(content)
        vec_bytes = vec_to_bytes(embedding)

        with db_session() as db:
            db.execute(
                "INSERT INTO memories(id, type, content, importance) VALUES (?, ?, ?, ?)",
                (memory_id, memory_type, content, importance)
            )
            db.execute(
                "INSERT INTO memory_vectors(memory_id, embedding) VALUES (?, ?)",
                (memory_id, vec_bytes)
            )
            # 👇 新增溯源记录
            if source_msg_id:
                db.execute(
                    "INSERT INTO memory_sources(memory_id, message_id) VALUES (?, ?)",
                    (memory_id, source_msg_id)
                )

        return memory_id
