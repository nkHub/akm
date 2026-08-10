"""SQLite 数据库连接和建表"""

import json
import os
import sqlite3
import uuid
from datetime import datetime
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
            cache_creation_tokens INTEGER DEFAULT 0,
            client_request_headers TEXT DEFAULT '',
            client_request_body TEXT DEFAULT '',
            upstream_request_headers TEXT DEFAULT '',
            upstream_response_body TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_audit_timestamp
            ON audit_logs(timestamp);

        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            task_type     TEXT NOT NULL,
            interval_sec  INTEGER DEFAULT 0,
            cron          TEXT DEFAULT '',
            payload       TEXT DEFAULT '{}',
            enabled       INTEGER DEFAULT 1,
            last_run_at   TEXT DEFAULT '',
            next_run_at   TEXT DEFAULT '',
            created_at    TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at    TEXT DEFAULT (datetime('now', 'localtime'))
        );
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
    # audit_logs 表 — 三段式审计信息列（客户端请求头/客户端请求体/上游请求头）
    for col in ["client_request_headers", "client_request_body", "upstream_request_headers", "upstream_response_body"]:
        try:
            conn.execute(f"ALTER TABLE audit_logs ADD COLUMN {col} TEXT DEFAULT ''")
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
        ("usage_query_interval_m", "INTEGER", "0"),
        ("usage_queried_at", "TEXT", "''"),
        ("usage_data", "TEXT", "''"),
        ("usage_error", "TEXT", "''"),
        ("usage_query_endpoint", "TEXT", "''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE keys ADD COLUMN {col} {col_type} DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    # 修正旧版迁移残留：早期版本 usage_query_interval_m 默认值为 5，
    # SQLite 不支持 ALTER COLUMN 修改 DEFAULT，需对从未手动配置过的 Key
    # 显式重置为 0（无脚本且间隔为旧默认值 5 的视为未配置）。
    conn.execute(
        "UPDATE keys SET usage_query_interval_m = 0 WHERE usage_query_interval_m = 5 AND (usage_query_script IS NULL OR usage_query_script = '')"
    )


# ── 定时任务（scheduled_tasks）CRUD 辅助 ─────────────────────

# 合法的任务类型，用于创建/更新时校验
TASK_TYPES = ("agent_call", "usage_query")


def _now_str() -> str:
    """返回当前本地时间字符串（与表默认值格式一致）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _task_row_to_dict(row: sqlite3.Row) -> dict:
    """把 scheduled_tasks 行转成可 JSON 序列化的字典。"""
    item = dict(row)
    # payload 字段在 DB 中是 JSON 字符串，对外暴露时解析为对象
    try:
        item["payload"] = json.loads(item["payload"]) if item["payload"] else {}
    except (json.JSONDecodeError, TypeError):
        item["payload"] = {}
    item["enabled"] = bool(item.get("enabled"))
    return item


def create_task(
    name: str,
    task_type: str,
    payload: dict | None = None,
    interval_sec: int = 0,
    cron: str = "",
    enabled: bool = True,
    task_id: str | None = None,
) -> dict:
    """创建一条定时任务并返回其完整记录。

    首次创建时把 next_run_at 设为当前时间，保证启用后下一轮调度即可执行；
    之后由调度器按 interval_sec / cron 滚动计算。
    """
    if task_type not in TASK_TYPES:
        raise ValueError(f"不支持的任务类型: {task_type}")
    conn = get_connection()
    try:
        now = _now_str()
        row_id = task_id or uuid.uuid4().hex
        conn.execute(
            """INSERT INTO scheduled_tasks
               (id, name, task_type, interval_sec, cron, payload, enabled,
                last_run_at, next_run_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)""",
            (
                row_id,
                name,
                task_type,
                int(interval_sec or 0),
                cron or "",
                json.dumps(payload or {}, ensure_ascii=False),
                1 if enabled else 0,
                now,  # next_run_at 首次即当前时间，启用后可立即执行
                now,
                now,
            ),
        )
        conn.commit()
        return get_task(row_id)
    finally:
        conn.close()


def get_task(task_id: str) -> dict | None:
    """按 id 查询单条任务，不存在返回 None。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return _task_row_to_dict(row) if row else None
    finally:
        conn.close()


def list_tasks(task_type: str | None = None, enabled_only: bool = False) -> list[dict]:
    """列出任务，可选按类型过滤；enabled_only 只返回启用中的任务。"""
    conn = get_connection()
    try:
        sql = "SELECT * FROM scheduled_tasks"
        conds: list[str] = []
        args: list = []
        if task_type:
            conds.append("task_type = ?")
            args.append(task_type)
        if enabled_only:
            conds.append("enabled = 1")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, args).fetchall()
        return [_task_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def update_task(task_id: str, **fields) -> dict | None:
    """按 id 更新任务的指定字段，返回更新后的记录；不存在返回 None。

    支持字段：name / task_type / interval_sec / cron / payload / enabled /
    last_run_at / next_run_at。task_type 变更时会做合法性校验。
    """
    allowed = {
        "name", "task_type", "interval_sec", "cron", "payload",
        "enabled", "last_run_at", "next_run_at",
    }
    updates: list[str] = []
    args: list = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "task_type" and value not in TASK_TYPES:
            raise ValueError(f"不支持的任务类型: {value}")
        if key == "payload":
            value = json.dumps(value or {}, ensure_ascii=False)
        if key == "enabled":
            value = 1 if value else 0
        updates.append(f"{key} = ?")
        args.append(value)
    if not updates:
        return get_task(task_id)
    conn = get_connection()
    try:
        updates.append("updated_at = ?")
        args.append(_now_str())
        args.append(task_id)
        conn.execute(
            f"UPDATE scheduled_tasks SET {', '.join(updates)} WHERE id = ?", args
        )
        conn.commit()
        return get_task(task_id)
    finally:
        conn.close()


def delete_task(task_id: str) -> bool:
    """删除任务，返回是否真的删除了记录。"""
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
