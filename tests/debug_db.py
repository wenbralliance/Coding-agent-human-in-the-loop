from app.db.connection import db_session

with db_session() as db:
    print("=== messages 表 ===")
    msgs = db.execute("SELECT * FROM messages").fetchall()
    print(f"共 {len(msgs)} 条")
    for m in msgs:
        print(dict(m))

    print("\n=== memories 表 ===")
    mems = db.execute("SELECT id, type, content, status FROM memories").fetchall()
    print(f"共 {len(mems)} 条")
    for m in mems:
        print(dict(m))

    print("\n=== memory_vectors 表 ===")
    vecs = db.execute("SELECT memory_id, length(embedding) as len FROM memory_vectors").fetchall()
    print(f"共 {len(vecs)} 条")
    for v in vecs:
        print(dict(v))