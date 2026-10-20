from app.db.connection import db_session

with db_session() as db:
    db.execute("DELETE FROM memories")
    db.execute("DELETE FROM memory_vectors")
    db.execute("DELETE FROM memory_sources")
    db.execute("DELETE FROM memory_events")
    print("长期记忆已全部清空！")