"""SQLite 数据库连接和建表"""

import os
import sqlite3
from pathlib import Path

# 数据目录：~/.akm/
DB_DIR = os.path.expanduser("~/.akm")


def get_keys_log_path() -> str:
    """返回 Key 变更日志文件路径，并确保目录存在。"""
    os.makedirs(DB_DIR, exist_ok=True)
    return os.path.join(DB_DIR, "keys.log")


def get_db_path() -> str:
    """返回数据库文件完整路径，并确保目录存在"""
    os.makedirs(DB_DIR, exist_ok=True)
    return os.path.join(DB_DIR, "akm.db")


def get_connection() -> sqlite3.Connection:
    """获取数据库连接，启用 WAL 模式和外键"""
    conn = sqlite3.connect(get_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """创建 keys 和 audit_logs 表（如果不存在）"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS keys (
            alias       TEXT PRIMARY KEY,
            provider    TEXT NOT NULL,
            api_key     TEXT NOT NULL,
            base_url    TEXT,
            models      TEXT DEFAULT '*',
            provider_models TEXT DEFAULT '',
            auth_header TEXT DEFAULT 'Bearer {api_key}',
            priority    INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'active',
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            provider        TEXT DEFAULT '',
            key_alias       TEXT DEFAULT '',
            model           TEXT DEFAULT '',
            request_body    TEXT DEFAULT '',
            response_body   TEXT DEFAULT '',
            status_code     INTEGER DEFAULT 0,
            latency_ms      INTEGER DEFAULT 0,
            error           TEXT DEFAULT '',
            request_headers TEXT DEFAULT '',
            prompt_tokens     INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens      INTEGER DEFAULT 0,
            cached_tokens     INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_audit_timestamp
            ON audit_logs(timestamp);
    """)
    # 迁移旧表，添加新列（忽略已存在的错误）
    _migrate_audit_columns(conn)
    conn.commit()


def _migrate_audit_columns(conn: sqlite3.Connection) -> None:
    """增量迁移：为旧数据库添加新列"""
    # keys 表 — auth_header
    try:
        conn.execute("ALTER TABLE keys ADD COLUMN auth_header TEXT DEFAULT 'Bearer {api_key}'")
    except sqlite3.OperationalError:
        pass
    # keys 表 — provider_models 列
    try:
        conn.execute("ALTER TABLE keys ADD COLUMN provider_models TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # audit_logs 表 — request_headers 列
    try:
        conn.execute("ALTER TABLE audit_logs ADD COLUMN request_headers TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # audit_logs 表 — token 列
    for col, default in [
        ("prompt_tokens", "0"),
        ("completion_tokens", "0"),
        ("total_tokens", "0"),
        ("cached_tokens", "0"),
        ("cache_creation_tokens", "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE audit_logs ADD COLUMN {col} INTEGER DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    # keys 表 — 用量查询配置列
    _migrate_key_usage_columns(conn)


def _migrate_key_usage_columns(conn: sqlite3.Connection) -> None:
    """迁移：keys 表添加用量查询相关列"""
    for col, col_type, default in [
        ("usage_query_script", "TEXT", "''"),
        ("usage_query_interval_m", "INTEGER", "5"),
        ("usage_queried_at", "TEXT", "''"),
        ("usage_data", "TEXT", "''"),
        ("usage_error", "TEXT", "''"),
        ("usage_query_endpoint", "TEXT", "''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE keys ADD COLUMN {col} {col_type} DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
