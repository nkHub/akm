"""审计日志：写入、查询、清理"""

from datetime import datetime
from akm.db import get_connection


def write_log(data: dict) -> None:
    """写入一条审计日志

    data 应包含: provider, key_alias, model, request_body,
                response_body, status_code, latency_ms, error
    """
    conn = get_connection()
    conn.execute(
        """INSERT INTO audit_logs
           (timestamp, provider, key_alias, model, request_body,
            response_body, status_code, latency_ms, error)
           VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get("provider", ""),
            data.get("key_alias", ""),
            data.get("model", ""),
            data.get("request_body", ""),
            data.get("response_body", ""),
            data.get("status_code", 0),
            data.get("latency_ms", 0),
            data.get("error", ""),
        ),
    )
    conn.commit()
    conn.close()


def list_logs(
    provider: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """查询最近日志，可按供应商筛选"""
    conn = get_connection()
    if provider:
        rows = conn.execute(
            """SELECT * FROM audit_logs
               WHERE provider = ?
               ORDER BY id DESC LIMIT ?""",
            (provider, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clean_logs(before: str) -> int:
    """清理指定日期之前的日志，返回删除条数

    before: YYYY-MM-DD 格式的日期字符串
    """
    try:
        datetime.strptime(before, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"日期格式错误: {before}，需要 YYYY-MM-DD")
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM audit_logs WHERE timestamp < ?", (before,)
    )
    conn.commit()
    count = cursor.rowcount
    conn.close()
    return count
