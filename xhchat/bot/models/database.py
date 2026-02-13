"""对话历史存储"""
import sys
import sqlite3
from pathlib import Path


def get_connection():
    """获取数据库连接。
    Windows: 使用绝对路径（以 xhchat 包目录为基准）。
    Linux/Ubuntu: 使用相对路径 data/bot.db（以当前工作目录为基准）。
    """
    if sys.platform == "win32":
        # Windows: 绝对路径，基于 xhchat 包位置
        base = Path(__file__).resolve().parent.parent.parent  # xhchat 目录
        db_path = base / "data" / "bot.db"
    else:
        # Ubuntu/Linux: 相对路径，以当前工作目录为基准
        db_path = Path("data/bot.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表"""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_chat_user 
        ON messages(chat_id, user_id, created_at DESC)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_settings (
            chat_id INTEGER PRIMARY KEY,
            custom_prompt TEXT,
            ai_provider TEXT NOT NULL DEFAULT 'kimi',
            model_name TEXT,
            openai_base_url TEXT,
            openai_api_key TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_activity (
            chat_id INTEGER PRIMARY KEY,
            last_admin_message_at TIMESTAMP,
            last_warm_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sticker_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def add_message(chat_id: int, user_id: int, role: str, content: str):
    """添加一条消息"""
    conn = get_connection()
    conn.execute(
        "INSERT INTO messages (chat_id, user_id, role, content) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, role, content),
    )
    conn.commit()
    conn.close()


def get_recent_messages(chat_id: int, user_id: int, limit: int = 10):
    """获取最近的对话消息，用于构造上下文"""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT role, content FROM messages
        WHERE chat_id = ? AND user_id = ?
        ORDER BY created_at DESC LIMIT ?
        """,
        (chat_id, user_id, limit * 2),  # user + assistant 各 limit 条
    ).fetchall()
    conn.close()
    # 按时间正序返回
    result = [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
    return result


def clear_context(chat_id: int, user_id: int):
    """清除某用户的对话历史"""
    conn = get_connection()
    conn.execute(
        "DELETE FROM messages WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    conn.commit()
    conn.close()


def get_group_settings(chat_id: int):
    """获取群组配置，不存在返回 None"""
    conn = get_connection()
    row = conn.execute(
        "SELECT custom_prompt, ai_provider, model_name, openai_base_url, openai_api_key FROM group_settings WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_group_settings(chat_id: int, custom_prompt=None, ai_provider=None, model_name=None, openai_base_url=None, openai_api_key=None):
    """更新群组配置，None 表示不修改。用空字符串可清除某字段"""
    conn = get_connection()
    existing = conn.execute("SELECT * FROM group_settings WHERE chat_id = ?", (chat_id,)).fetchone()
    vals = {
        "custom_prompt": custom_prompt,
        "ai_provider": ai_provider,
        "model_name": model_name,
        "openai_base_url": openai_base_url,
        "openai_api_key": openai_api_key,
    }
    if existing:
        row = dict(existing)
        for k, v in vals.items():
            if v is not None:
                row[k] = v if v != "" else None
        conn.execute(
            """
            UPDATE group_settings SET
                custom_prompt = ?, ai_provider = ?, model_name = ?,
                openai_base_url = ?, openai_api_key = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = ?
            """,
            (row["custom_prompt"], row["ai_provider"], row["model_name"],
             row["openai_base_url"], row["openai_api_key"], chat_id),
        )
    else:
        c = vals["custom_prompt"] if vals["custom_prompt"] is not None else None
        a = vals["ai_provider"] or "kimi"
        m = vals["model_name"] if vals["model_name"] else None
        b = vals["openai_base_url"] if vals["openai_base_url"] else None
        k = vals["openai_api_key"] if vals["openai_api_key"] else None
        conn.execute(
            "INSERT INTO group_settings (chat_id, custom_prompt, ai_provider, model_name, openai_base_url, openai_api_key) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, c, a, m, b, k),
        )
    conn.commit()
    conn.close()


def clear_group_model(chat_id: int):
    """清除群组的模型配置，恢复用全局。保留 custom_prompt。"""
    conn = get_connection()
    conn.execute(
        "UPDATE group_settings SET ai_provider=NULL, model_name=NULL, openai_base_url=NULL, openai_api_key=NULL, updated_at=CURRENT_TIMESTAMP WHERE chat_id=?",
        (chat_id,),
    )
    conn.commit()
    conn.close()


def update_admin_activity(chat_id: int):
    """记录管理员发言时间"""
    conn = get_connection()
    now = _utc_now()
    conn.execute(
        """
        INSERT INTO group_activity (chat_id, last_admin_message_at, last_warm_at, updated_at)
        VALUES (?, ?, NULL, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            last_admin_message_at = excluded.last_admin_message_at,
            updated_at = excluded.updated_at
        """,
        (chat_id, now, now),
    )
    conn.commit()
    conn.close()


def get_group_activity(chat_id: int):
    """获取群活动记录"""
    conn = get_connection()
    row = conn.execute(
        "SELECT last_admin_message_at, last_warm_at FROM group_activity WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_warm_at(chat_id: int):
    """记录暖群时间"""
    conn = get_connection()
    now = _utc_now()
    conn.execute(
        """
        INSERT INTO group_activity (chat_id, last_admin_message_at, last_warm_at, updated_at)
        VALUES (?, NULL, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            last_warm_at = excluded.last_warm_at,
            updated_at = excluded.updated_at
        """,
        (chat_id, now, now),
    )
    conn.commit()
    conn.close()


def get_sticker_ids() -> list[str]:
    """获取贴纸池中所有 file_id（暖群和回复共用）"""
    conn = get_connection()
    rows = conn.execute("SELECT file_id FROM sticker_pool ORDER BY id").fetchall()
    conn.close()
    return [row["file_id"] for row in rows]


def add_sticker(file_id: str) -> bool:
    """添加贴纸，已存在则返回 False"""
    conn = get_connection()
    try:
        conn.execute("INSERT INTO sticker_pool (file_id) VALUES (?)", (file_id,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    finally:
        conn.close()


def remove_sticker_by_index(index: int) -> bool:
    """按序号删除贴纸（1-based），成功返回 True"""
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM sticker_pool ORDER BY id LIMIT 1 OFFSET ?", (index - 1,)
    ).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute("DELETE FROM sticker_pool WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return True


def remove_sticker_by_file_id(file_id: str) -> bool:
    """按 file_id 删除贴纸，成功返回 True"""
    conn = get_connection()
    cur = conn.execute("DELETE FROM sticker_pool WHERE file_id = ?", (file_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def has_sticker(file_id: str) -> bool:
    """贴纸是否在贴纸池中"""
    conn = get_connection()
    row = conn.execute("SELECT 1 FROM sticker_pool WHERE file_id = ?", (file_id,)).fetchone()
    conn.close()
    return row is not None


def _utc_now() -> str:
    """返回 UTC 时间字符串"""
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
