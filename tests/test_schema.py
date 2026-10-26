from app.db.connection import db_session
from app.db.schema import create_all_tables

with db_session() as db:
    print("1. 开始建表...")
    create_all_tables(db)
    print("2. 建表完成")

    # 查询所有表
    rows = db.execute("""
        SELECT name FROM sqlite_master 
        WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """).fetchall()

    tables = [r["name"] for r in rows]
    print(f"3. 当前数据库包含 {len(tables)} 张表:")
    for t in tables:
        print(f"   - {t}")

    # 测试 FTS 触发器
    print("\n4. 测试 FTS 触发器...")
    db.execute("INSERT INTO memories(id, type, content) VALUES (?, ?, ?)",
               ("m_001", "fact", "用户偏好 Python"))

    # 查 FTS 表
    r = db.execute("SELECT * FROM memories_fts WHERE memories_fts MATCH 'Python'").fetchone()
    if r:
        print(f"   FTS 命中: memory_id={r['memory_id']}")