from app.db.connection import db_session

with db_session() as db:
    print("正在清空脏的历史记录...")
    db.execute("DELETE FROM sessions")
    db.execute("DELETE FROM messages")
    db.execute("DELETE FROM memories")
    db.execute("DELETE FROM memory_vectors")
    print("✅ 所有历史对话和记忆已彻底清空！")