import sqlite3
from contextlib import contextmanager
from pathlib import Path
import sqlite_vec
from app.config import APP_HOME, settings

# 项目根目录
PROJECT_ROOT = APP_HOME


def get_db() -> sqlite3.Connection:
    # 1. 组合路径
    db_path = Path(settings.DB_PATH)

    # 如果是相对路径，拼到项目根上
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    # 2. 确保目录存在
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 3. 建立连接
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    # 4. 加载 sqlite-vec 扩展
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)

    # 5. 设置 PRAGMA
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")

    return db


@contextmanager
def db_session():
    db = get_db()
    try:
        yield db
        db.commit()      # 👈 成功执行完 with 块，提交事务
    except Exception as e:
        db.rollback()    # 👈 出现异常，回滚
        raise e
    finally:
        db.close()
