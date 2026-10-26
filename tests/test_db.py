from app.db.connection import db_session

with db_session() as db:
    print("1. 连接成功")

    r = db.execute("SELECT vec_version()").fetchone()
    print(f"2. sqlite-vec 版本: {r[0]}")

    r = db.execute("PRAGMA journal_mode").fetchone()
    print(f"3. journal_mode: {r[0]}")

    r = db.execute("PRAGMA foreign_keys").fetchone()
    print(f"4. foreign_keys: {r[0]}")

    db.execute("CREATE TABLE IF NOT EXISTS _test(id INTEGER)")
    db.execute("DROP TABLE _test")
    print("5. 建表成功")