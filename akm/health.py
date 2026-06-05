"""服务健康监护：聚合运行时指标，并提供轻量心跳巡检。"""

import asyncio
import time
from collections import deque
from datetime import datetime

from akm.db import get_connection


class HealthMonitor:
    """聚合服务健康状态，并维护后台心跳巡检。"""

    LOOP_LAG_WARN_MS = 200
    LOOP_LAG_DEGRADED_MS = 1000
    LOOP_LAG_UNHEALTHY_MS = 3000
    AUDIT_TASKS_DEGRADED = 300
    AUDIT_TASKS_UNHEALTHY = 1000
    DB_FAILS_DEGRADED = 3
    DB_FAILS_UNHEALTHY = 10
    UPSTREAM_FAILS_DEGRADED = 10
    UPSTREAM_FAILS_RECREATE = 20
    HEARTBEAT_INTERVAL_SEC = 2.0
    HISTORY_LIMIT = 200

    def __init__(self):
        now = time.time()
        self.started_at = now
        self.last_heartbeat_at = now
        self.last_loop_tick_at = now
        self.event_loop_lag_ms = 0
        self.max_event_loop_lag_ms = 0

        self.inflight_requests = 0
        self.active_streams = 0
        self.pending_audit_tasks = 0
        self.audit_task_failures = 0
        self.audit_queue_dropped = 0

        self.db_last_ok_at = 0.0
        self.db_last_latency_ms = 0
        self.db_consecutive_failures = 0
        self.db_last_error = ""

        self.consecutive_upstream_failures = 0
        self.last_upstream_error = ""
        self.last_upstream_success_at = 0.0
        self.http_client_recreate_count = 0
        self.http_client_last_recreated_at = 0.0
        self.http_client_last_recreate_reason = ""
        self._recent_events = deque(maxlen=self.HISTORY_LIMIT)
        self._last_status = "healthy"
        self._last_reasons: list[str] = []

    def _append_event(self, event_type: str, payload: dict | None = None) -> None:
        """向环形缓冲追加一条最近事件，供 `/debug/runtime/history` 排障使用。"""
        self._recent_events.append({
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": str(event_type or "unknown"),
            "payload": payload or {},
        })

    def recent_events_payload(self, limit: int = 50) -> dict:
        """返回最近事件列表，默认只给最近 50 条，避免调试端点过大。"""
        safe_limit = max(1, min(int(limit or 50), self.HISTORY_LIMIT))
        items = list(self._recent_events)[-safe_limit:]
        return {
            "total_buffered": len(self._recent_events),
            "limit": safe_limit,
            "events": items,
        }

    def request_started(self) -> None:
        self.inflight_requests += 1

    def request_finished(self) -> None:
        if self.inflight_requests > 0:
            self.inflight_requests -= 1

    def stream_started(self) -> None:
        self.active_streams += 1

    def stream_finished(self) -> None:
        if self.active_streams > 0:
            self.active_streams -= 1

    def audit_task_started(self) -> None:
        self.pending_audit_tasks += 1

    def audit_task_finished(self, ok: bool) -> None:
        if self.pending_audit_tasks > 0:
            self.pending_audit_tasks -= 1
        if not ok:
            self.audit_task_failures += 1

    def set_audit_backlog(self, pending: int, dropped: int = 0, failures: int | None = None) -> None:
        """由有界队列统一回填 backlog 状态，避免主链路手工维护计数漂移。"""
        prev_dropped = self.audit_queue_dropped
        self.pending_audit_tasks = max(0, int(pending or 0))
        self.audit_queue_dropped = max(0, int(dropped or 0))
        if failures is not None:
            self.audit_task_failures = max(0, int(failures or 0))
        if self.audit_queue_dropped > prev_dropped:
            self._append_event(
                "audit.queue.dropped",
                {
                    "pending": self.pending_audit_tasks,
                    "dropped": self.audit_queue_dropped,
                    "failures": self.audit_task_failures,
                },
            )

    def record_upstream_success(self) -> None:
        self.consecutive_upstream_failures = 0
        self.last_upstream_error = ""
        self.last_upstream_success_at = time.time()

    def record_upstream_failure(self, error: str) -> None:
        self.consecutive_upstream_failures += 1
        self.last_upstream_error = str(error or "")

    def should_recreate_http_client(self) -> bool:
        """根据上游连续失败次数判断是否值得做一次软重建。"""
        return self.consecutive_upstream_failures >= self.UPSTREAM_FAILS_RECREATE

    def record_http_client_recreated(self, reason: str) -> None:
        """记录一次连接池软重建，并重置失败计数，避免短时间重复触发。"""
        self.http_client_recreate_count += 1
        self.http_client_last_recreated_at = time.time()
        self.http_client_last_recreate_reason = str(reason or "")
        self.consecutive_upstream_failures = 0
        self._append_event(
            "http_client.recreated",
            {
                "count": self.http_client_recreate_count,
                "reason": self.http_client_last_recreate_reason,
            },
        )

    async def run_heartbeat(self, interval_sec: float | None = None) -> None:
        """后台巡检：检测事件循环卡顿，并做轻量 DB 探针。"""
        interval = float(interval_sec or self.HEARTBEAT_INTERVAL_SEC)
        expected_tick = time.monotonic() + interval
        while True:
            await asyncio.sleep(interval)
            now_monotonic = time.monotonic()
            lag_ms = max(0, int((now_monotonic - expected_tick) * 1000))
            expected_tick = now_monotonic + interval

            self.last_heartbeat_at = time.time()
            self.last_loop_tick_at = self.last_heartbeat_at
            self.event_loop_lag_ms = lag_ms
            if lag_ms > self.max_event_loop_lag_ms:
                self.max_event_loop_lag_ms = lag_ms

            await asyncio.to_thread(self._probe_db)

    def _probe_db(self) -> None:
        """执行极轻量 DB 探针，避免把重查询混进心跳。"""
        t0 = time.time()
        try:
            conn = get_connection()
            conn.execute("SELECT 1").fetchone()
            conn.close()
            self.db_last_ok_at = time.time()
            self.db_last_latency_ms = int((time.time() - t0) * 1000)
            self.db_consecutive_failures = 0
            self.db_last_error = ""
        except Exception as exc:
            self.db_consecutive_failures += 1
            self.db_last_error = str(exc)
            self._append_event(
                "db.probe.failed",
                {
                    "consecutive_failures": self.db_consecutive_failures,
                    "error": self.db_last_error,
                },
            )

    def _collect_status(self) -> tuple[str, list[str]]:
        """根据当前指标生成聚合状态，规则尽量保守。"""
        reasons = []
        status = "healthy"

        if self.event_loop_lag_ms >= self.LOOP_LAG_UNHEALTHY_MS:
            reasons.append("event_loop_lag_critical")
            status = "unhealthy"
        elif self.event_loop_lag_ms >= self.LOOP_LAG_DEGRADED_MS:
            reasons.append("event_loop_lag_high")
            status = "degraded"
        elif self.event_loop_lag_ms >= self.LOOP_LAG_WARN_MS:
            reasons.append("event_loop_lag_warn")

        if self.pending_audit_tasks >= self.AUDIT_TASKS_UNHEALTHY:
            reasons.append("audit_backlog_critical")
            status = "unhealthy"
        elif self.pending_audit_tasks >= self.AUDIT_TASKS_DEGRADED and status != "unhealthy":
            reasons.append("audit_backlog_high")
            status = "degraded"

        if self.audit_queue_dropped > 0 and status == "healthy":
            reasons.append("audit_queue_dropped")
            status = "degraded"

        if self.db_consecutive_failures >= self.DB_FAILS_UNHEALTHY:
            reasons.append("db_probe_failed_critical")
            status = "unhealthy"
        elif self.db_consecutive_failures >= self.DB_FAILS_DEGRADED and status == "healthy":
            reasons.append("db_probe_failed")
            status = "degraded"

        if self.consecutive_upstream_failures >= self.UPSTREAM_FAILS_DEGRADED and status == "healthy":
            reasons.append("upstream_failures_high")
            status = "degraded"

        return status, reasons

    def _sync_status_event(self, status: str, reasons: list[str]) -> None:
        """仅在健康状态发生变化时写入事件，避免把 history 刷成心跳噪声。"""
        if status == self._last_status and reasons == self._last_reasons:
            return
        self._append_event(
            "health.status.changed",
            {
                "before_status": self._last_status,
                "after_status": status,
                "before_reasons": list(self._last_reasons),
                "after_reasons": list(reasons),
            },
        )
        self._last_status = status
        self._last_reasons = list(reasons)

    def live_payload(self) -> dict:
        return {"status": "ok"}

    def ready_payload(self) -> tuple[dict, int]:
        status, reasons = self._collect_status()
        self._sync_status_event(status, reasons)
        body = {"status": status, "ready": status != "unhealthy", "reasons": reasons}
        return body, (200 if status != "unhealthy" else 503)

    def detail_payload(self) -> dict:
        status, reasons = self._collect_status()
        self._sync_status_event(status, reasons)
        return {
            "status": status,
            "reasons": reasons,
            "metrics": {
                "started_at": datetime.fromtimestamp(self.started_at).astimezone().isoformat(timespec="seconds"),
                "uptime_sec": int(max(0, time.time() - self.started_at)),
                "last_heartbeat_at": datetime.fromtimestamp(self.last_heartbeat_at).astimezone().isoformat(timespec="seconds"),
                "event_loop_lag_ms": self.event_loop_lag_ms,
                "max_event_loop_lag_ms": self.max_event_loop_lag_ms,
                "inflight_requests": self.inflight_requests,
                "active_streams": self.active_streams,
                "pending_audit_tasks": self.pending_audit_tasks,
                "audit_task_failures": self.audit_task_failures,
                "audit_queue_dropped": self.audit_queue_dropped,
                "db_last_ok_at": datetime.fromtimestamp(self.db_last_ok_at).astimezone().isoformat(timespec="seconds") if self.db_last_ok_at else "",
                "db_last_latency_ms": self.db_last_latency_ms,
                "db_consecutive_failures": self.db_consecutive_failures,
                "db_last_error": self.db_last_error,
                "consecutive_upstream_failures": self.consecutive_upstream_failures,
                "last_upstream_error": self.last_upstream_error,
                "last_upstream_success_at": datetime.fromtimestamp(self.last_upstream_success_at).astimezone().isoformat(timespec="seconds") if self.last_upstream_success_at else "",
                "http_client_recreate_count": self.http_client_recreate_count,
                "http_client_last_recreated_at": datetime.fromtimestamp(self.http_client_last_recreated_at).astimezone().isoformat(timespec="seconds") if self.http_client_last_recreated_at else "",
                "http_client_last_recreate_reason": self.http_client_last_recreate_reason,
            },
        }
