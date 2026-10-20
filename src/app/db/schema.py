import sqlite3


def create_all_tables(db: sqlite3.Connection) -> None:
    """创建所有数据表、虚拟表、索引和触发器"""

    # ============================================================
    # 1. 普通关系表
    # ============================================================
    db.executescript("""
        -- 会话表
        CREATE TABLE IF NOT EXISTS sessions (
            id            TEXT PRIMARY KEY,
            title         TEXT,
            user_id       TEXT NOT NULL DEFAULT 'default_user',
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- 消息表（对话原文和工具摘要）
        CREATE TABLE IF NOT EXISTS messages (
            id            TEXT PRIMARY KEY,
            session_id    TEXT NOT NULL,
            role          TEXT NOT NULL,      -- user / assistant / tool
            content       TEXT NOT NULL,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, created_at);

        -- 长期记忆表
        CREATE TABLE IF NOT EXISTS memories (
            id                  TEXT PRIMARY KEY,
            user_id             TEXT NOT NULL DEFAULT 'default_user',
            type                TEXT NOT NULL,    -- preference / fact / event
            content             TEXT NOT NULL,
            importance          REAL DEFAULT 0.5,
            confidence          REAL DEFAULT 0.8,
            status              TEXT DEFAULT 'active',  -- active / superseded
            superseded_by       TEXT,
            valid_until         DATETIME,
            last_accessed_at    DATETIME,
            created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_memories_user_status
            ON memories(user_id, status);

        -- 记忆溯源表
        CREATE TABLE IF NOT EXISTS memory_sources (
            memory_id       TEXT NOT NULL,
            message_id      TEXT NOT NULL,
            PRIMARY KEY (memory_id, message_id)
        );

        -- 记忆事件日志
        CREATE TABLE IF NOT EXISTS memory_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id       TEXT NOT NULL,
            action          TEXT NOT NULL,    -- ADD / UPDATE / SUPERSEDE
            old_content     TEXT,
            new_content     TEXT,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- Token 追踪表
        CREATE TABLE IF NOT EXISTS llm_calls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT,
            call_type       TEXT NOT NULL,    -- main / extract / embed
            model           TEXT NOT NULL,
            input_tokens    INTEGER DEFAULT 0,
            output_tokens   INTEGER DEFAULT 0,
            cost_usd        REAL DEFAULT 0,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # ============================================================
    # 2. FTS5 全文检索虚拟表（用于混合检索）
    # ============================================================
    db.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content,
            memory_id UNINDEXED,
            tokenize='unicode61'
        );
    """)

    # ============================================================
    # 3. 向量虚拟表（sqlite-vec）
    # ============================================================
    # 注意：必须在加载 sqlite-vec 扩展后建
    db.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
            memory_id TEXT PRIMARY KEY,
            embedding FLOAT[1024]
        );
    """)

    # ============================================================
    # 4. 触发器（同步 FTS5）
    # ============================================================
    db.executescript("""
        -- 新增记忆时，同步写入 FTS
        CREATE TRIGGER IF NOT EXISTS trg_memories_fts_insert
        AFTER INSERT ON memories
        BEGIN
            INSERT INTO memories_fts(content, memory_id)
            VALUES (new.content, new.id);
        END;

        -- 更新记忆时，更新 FTS
        CREATE TRIGGER IF NOT EXISTS trg_memories_fts_update
        AFTER UPDATE OF content ON memories
        BEGIN
            DELETE FROM memories_fts WHERE memory_id = old.id;
            INSERT INTO memories_fts(content, memory_id)
            VALUES (new.content, new.id);
        END;
    """)

    db.commit()

if __name__ == '__main__':
    from app.db.connection import get_db

    create_all_tables(get_db())