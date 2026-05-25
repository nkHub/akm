"""审计日志：写入、查询、清理"""

import asyncio
from datetime import datetime
from akm.db import get_connection


def write_log(data: dict) -> None:
    """写入一条审计日志（同步版本）"""
    _do_write(data)


def _do_write(data: dict) -> None:
    """执行实际写入操作"""
    conn = get_connection()
    conn.execute(
        """INSERT INTO audit_logs
           (timestamp, provider, key_alias, model, request_body,
            response_body, status_code, latency_ms, error)
            VALUES (datetime('now', 'localtime'), ?, ?, ?, ?, ?, ?, ?, ?)""",
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


async def write_log_async(data: dict) -> None:
    """异步写入审计日志（在线程池中执行，避免阻塞事件循环）"""
    await asyncio.to_thread(_do_write, data)


def list_logs(
    provider: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order: str = "DESC",
    hide_empty: bool = False,
    status: str = "all",
    key_alias: str = "",
) -> list[dict]:
    """查询日志，支持分页、供应商筛选、排序、过滤空记录和状态筛选

    order: "ASC" 按时间正序（旧→新），"DESC" 倒序（新→旧），默认 DESC
    hide_empty: True 时过滤掉没有 request_body 的记录（纯错误/空记录）
    status: "all" 全部, "success" 仅成功(2xx), "failed" 仅失败(非2xx)
    key_alias: 按 Key 别名筛选
    """
    order_clause = "ORDER BY id ASC" if order.upper() == "ASC" else "ORDER BY id DESC"
    filters = ""
    params = []
    if hide_empty:
        filters += " AND request_body != ''"
    if status == "success":
        filters += " AND status_code >= 200 AND status_code < 300"
    elif status == "failed":
        filters += " AND (status_code < 200 OR status_code >= 300)"
    if key_alias:
        filters += " AND key_alias = ?"
        params.append(key_alias)
    conn = get_connection()
    if provider:
        rows = conn.execute(
            f"""SELECT * FROM audit_logs
               WHERE provider = ? {filters}
               {order_clause} LIMIT ? OFFSET ?""",
            (provider, *params, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT * FROM audit_logs
               WHERE 1=1 {filters}
               {order_clause} LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_logs(
    provider: str | None = None,
    hide_empty: bool = False,
    status: str = "all",
    key_alias: str = "",
) -> int:
    """统计日志总数，可按供应商筛选，可选过滤空记录和状态"""
    filters = ""
    params = []
    if hide_empty:
        filters += " AND request_body != ''"
    if status == "success":
        filters += " AND status_code >= 200 AND status_code < 300"
    elif status == "failed":
        filters += " AND (status_code < 200 OR status_code >= 300)"
    if key_alias:
        filters += " AND key_alias = ?"
        params.append(key_alias)
    conn = get_connection()
    if provider:
        row = conn.execute(
            f"SELECT COUNT(*) FROM audit_logs WHERE provider = ? {filters}",
            (provider, *params),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT COUNT(*) FROM audit_logs WHERE 1=1 {filters}",
            (*params,),
        ).fetchone()
    conn.close()
    return row[0] if row else 0


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
