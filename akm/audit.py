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
             response_body, status_code, latency_ms, error,
             request_headers,
             prompt_tokens, completion_tokens, total_tokens, cached_tokens, cache_creation_tokens)
             VALUES (datetime('now', 'localtime'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get("provider", ""),
            data.get("key_alias", ""),
            data.get("model", ""),
            data.get("request_body", ""),
            data.get("response_body", ""),
            data.get("status_code", 0),
            data.get("latency_ms", 0),
            data.get("error", ""),
            data.get("request_headers", ""),
            data.get("prompt_tokens", 0),
            data.get("completion_tokens", 0),
            data.get("total_tokens", 0),
            data.get("cached_tokens", 0),
            data.get("cache_creation_tokens", 0),
        ),
    )
    conn.commit()
    conn.close()


async def write_log_async(data: dict) -> None:
    """异步写入审计日志（在线程池中执行，避免阻塞事件循环）"""
    await asyncio.to_thread(_do_write, data)


class AuditLogQueue:
    """有界审计队列：使用固定 worker 消化写日志任务，避免无限 create_task 堆积。

    设计目标：
    1. 日志写入不阻塞主请求链路。
    2. 高峰期不再为每条日志单独创建后台任务，避免任务数量失控。
    3. 队列满时优先丢弃新增日志，并把背压信息暴露给健康监护层。
    """

    def __init__(self, maxsize: int = 512):
        self.maxsize = max(1, int(maxsize or 512))
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=self.maxsize)
        self._worker_task: asyncio.Task | None = None
        self._stopped = False
        self.dropped_count = 0
        self.failure_count = 0
        self.last_error = ""
        self.last_success_at = 0.0

    def qsize(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        """启动单 worker 消费队列。"""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stopped = False
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """停止 worker，并尽量等待队列中已提交任务完成。"""
        self._stopped = True
        await self._queue.join()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def submit(self, data: dict) -> bool:
        """提交一条日志；满载时直接丢弃并返回 False。"""
        if self._stopped:
            self.dropped_count += 1
            return False
        try:
            self._queue.put_nowait(dict(data))
            return True
        except asyncio.QueueFull:
            self.dropped_count += 1
            return False

    async def _worker_loop(self) -> None:
        """持续消费队列，顺序写库，减少并发写 SQLite 的冲突。"""
        while True:
            item = await self._queue.get()
            try:
                await write_log_async(item)
                self.last_success_at = asyncio.get_running_loop().time()
            except Exception as exc:
                self.failure_count += 1
                self.last_error = str(exc)
            finally:
                self._queue.task_done()


async def list_logs_async(
    provider: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order: str = "DESC",
    hide_empty: bool = False,
    status: str = "all",
    key_alias: str = "",
    days: int = 0,
) -> list[dict]:
    """异步查询日志，避免管理台轮询时阻塞事件循环。"""
    return await asyncio.to_thread(
        list_logs,
        provider,
        limit,
        offset,
        order,
        hide_empty,
        status,
        key_alias,
        days,
    )


async def count_logs_async(
    provider: str | None = None,
    hide_empty: bool = False,
    status: str = "all",
    key_alias: str = "",
    days: int = 0,
) -> int:
    """异步统计日志数量，避免同步 COUNT 阻塞请求处理。"""
    return await asyncio.to_thread(
        count_logs,
        provider,
        hide_empty,
        status,
        key_alias,
        days,
    )


def list_logs(
    provider: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order: str = "DESC",
    hide_empty: bool = False,
    status: str = "all",
    key_alias: str = "",
    days: int = 0,
) -> list[dict]:
    """查询日志，支持分页、供应商筛选、排序、过滤空记录、状态筛选和时间范围

    order: "ASC" 按时间正序（旧→新），"DESC" 倒序（新→旧），默认 DESC
    hide_empty: True 时过滤掉没有 request_body 的记录（纯错误/空记录）
    status: "all" 全部, "success" 仅成功(2xx), "failed" 仅失败(非2xx)
    key_alias: 按 Key 别名筛选
    days: 时间范围（天），0 表示不限制
    """
    order_clause = "ORDER BY id ASC" if order.upper() == "ASC" else "ORDER BY id DESC"
    filters = ""
    params = []
    if days > 0:
        # 自然日范围：1=今天，7=最近7个自然日（含今天），30 同理。
        # days=1 -> offset=0；days=7 -> offset=-6。
        day_offset = 1 - days
        filters += " AND timestamp >= datetime(date('now', 'localtime', ? || ' days'))"
        params.append(str(day_offset))
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
    days: int = 0,
) -> int:
    """统计日志总数，可按供应商筛选，可选过滤空记录、状态和时间范围"""
    filters = ""
    params = []
    if days > 0:
        # 自然日范围：1=今天，7=最近7个自然日（含今天），30 同理。
        day_offset = 1 - days
        filters += " AND timestamp >= datetime(date('now', 'localtime', ? || ' days'))"
        params.append(str(day_offset))
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
    # 回收被删除数据占用的磁盘空间
    conn.execute("VACUUM")
    conn.close()
    return count


async def clean_logs_async(before: str) -> int:
    """异步清理日志，避免 DELETE/VACUUM 长时间占用事件循环。"""
    return await asyncio.to_thread(clean_logs, before)


def clean_log_bodies() -> int:
    """清空所有审计日志的请求体/响应体内容，返回影响条数"""
    conn = get_connection()
    cursor = conn.execute(
        """UPDATE audit_logs
           SET request_body = '', response_body = ''
           WHERE request_body != '' OR response_body != ''"""
    )
    conn.commit()
    count = cursor.rowcount
    # 释放清空文本后仍占用的磁盘空间
    conn.execute("VACUUM")
    conn.close()
    return count


async def clean_log_bodies_async() -> int:
    """异步清空日志体，避免大批量 UPDATE/VACUUM 阻塞服务。"""
    return await asyncio.to_thread(clean_log_bodies)
