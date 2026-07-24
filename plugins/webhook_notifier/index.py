"""上游事件的异步 Webhook / 浏览器通知插件。"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from akm.plugins import PluginBase


router = APIRouter()
_plugin_instance: "Plugin | None" = None


def _plugin() -> "Plugin":
    """取得由 PluginManager 完成上下文注入的唯一插件实例。"""
    if _plugin_instance is None or not _plugin_instance.enabled:
        raise HTTPException(status_code=503, detail="webhook_notifier 插件未启用")
    return _plugin_instance


@router.get("/status")
async def status():
    """返回插件运行快照：通道开关、缓冲规模与订阅者数量。"""
    return _plugin().status()


@router.get("/recent")
async def recent(limit: int = Query(default=50, ge=1, le=200)):
    """返回最近通知事件（不含请求正文），供页面首屏回放。"""
    return _plugin().recent_events(limit=limit)


@router.get("/events")
async def events_stream(after_id: int = Query(default=0, ge=0)):
    """浏览器通知 SSE：先补发 after_id 之后的缓冲，再实时推送。

    页面保持连接期间即可调用 Notification API 弹系统通知；
    关闭标签页后订阅自动结束，不影响转发主链路。
    """
    return await _plugin().events_stream(after_id=after_id)


class Plugin(PluginBase):
    """基于 on_response 元信息发送失败、安全与慢请求通知。

    支持两条互不阻塞的通道：
    1. 可选 HTTP Webhook（飞书 / 企微 / Slack / generic）；
    2. 可选浏览器通知：事件写入进程内环形缓冲，经 SSE 推给管理台页面。
    """

    router = router

    async def on_load(self):
        """初始化进程内去重表、后台任务、浏览器事件缓冲与订阅者集合。"""
        global _plugin_instance
        _plugin_instance = self
        self._last_sent: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_audit_queue_dropped = 0
        # 浏览器通知：自增序号 + 环形缓冲 + 活跃 SSE 订阅队列
        self._event_seq = 0
        self._browser_events: deque[dict[str, Any]] = deque(maxlen=100)
        self._subscribers: set[asyncio.Queue] = set()

    async def on_unload(self):
        """服务停止时取消尚未开始的通知任务，并清空浏览器订阅者。"""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        # 向所有 SSE 队列投递结束哨兵，避免生成器挂起
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(None)
            except Exception:
                pass
        self._subscribers.clear()
        global _plugin_instance
        if _plugin_instance is self:
            _plugin_instance = None

    def _settings(self) -> dict:
        """读取最新配置，保存后下一次事件立即采用新策略。"""
        cfg = self.config or {}
        return {
            "enabled": cfg.get("enabled", True) is True,
            "url": str(cfg.get("webhook_url", "") or "").strip(),
            "format": str(cfg.get("payload_format", "generic") or "generic").strip().lower(),
            # 浏览器通知默认开启：仅需启用插件并打开订阅页即可收到系统弹窗
            "browser_notifications": cfg.get("browser_notifications", True) is True,
            "notify_failures": cfg.get("notify_failures", True) is True,
            "notify_security": cfg.get("notify_security_events", True) is True,
            "notify_audit_drops": cfg.get("notify_audit_queue_drops", True) is True,
            "slow_threshold": max(0, int(cfg.get("slow_request_threshold_ms", 0) or 0)),
            "cooldown": max(0, int(cfg.get("cooldown_seconds", 300) or 0)),
            "timeout": min(30, max(1, int(cfg.get("timeout_seconds", 5) or 5))),
            "max_pending": min(256, max(1, int(cfg.get("max_pending_notifications", 32) or 32))),
        }

    def _event(self, response: dict, cfg: dict) -> tuple[str, str] | None:
        """将结构化响应元信息收敛为一个优先级最高的通知事件。"""
        security_action = str(response.get("security_action", "") or "")
        if security_action and cfg["notify_security"]:
            return "security", security_action
        if not response.get("ok") and cfg["notify_failures"]:
            phase = str(response.get("phase", "upstream") or "upstream")
            status = int(response.get("status_code", 0) or 0)
            return "failure", f"{phase}:{status}"
        latency = int(response.get("latency_ms", 0) or 0)
        if cfg["slow_threshold"] and response.get("ok") and latency >= cfg["slow_threshold"]:
            return "slow", str(cfg["slow_threshold"])
        return None

    def _build_message(self, event: str, subtype: str, response: dict) -> tuple[str, str, dict]:
        """生成标题、纯文本正文与结构化 details（Webhook 与浏览器共用）。"""
        status = int(response.get("status_code", 0) or 0)
        latency = int(response.get("latency_ms", 0) or 0)
        details = {
            "event": event,
            "subtype": subtype,
            "status_code": status,
            "phase": str(response.get("phase", "") or ""),
            "key_alias": str(response.get("key_alias", "") or ""),
            "provider": str(response.get("provider", "") or ""),
            "model": str(response.get("model", "") or ""),
            "api_path": str(response.get("api_path", "") or ""),
            "latency_ms": latency,
            "error": str(response.get("error", "") or "")[:1000],
            "security_reason": str(response.get("security_reason", "") or "")[:1000],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "audit_queue_dropped": int(response.get("audit_queue_dropped", 0) or 0),
        }
        title_map = {
            "failure": "AKM 上游请求失败",
            "security": "AKM 安全事件",
            "slow": "AKM 慢请求",
            "audit_drop": "AKM 审计队列丢弃",
        }
        title = title_map[event]
        text = "\n".join([
            title,
            f"事件: {event}/{subtype}",
            f"模型: {details['model']} | Key: {details['key_alias']} | Provider: {details['provider']}",
            f"接口: {details['api_path']} | 状态: {status} | 耗时: {latency}ms",
            f"原因: {details['error'] or details['security_reason'] or '-'}",
        ])
        return title, text, details

    def _build_payload(self, event: str, subtype: str, response: dict, fmt: str) -> dict:
        """生成通用或常见协作工具兼容的纯文本消息体。"""
        title, text, details = self._build_message(event, subtype, response)
        if fmt == "feishu":
            return {"msg_type": "text", "content": {"text": text}}
        if fmt == "wecom":
            return {"msgtype": "text", "text": {"content": text}}
        if fmt == "slack":
            return {"text": text}
        return {"event": event, "title": title, "text": text, "details": details}

    def _publish_browser(self, event: str, subtype: str, response: dict) -> dict:
        """写入浏览器通知缓冲并广播给所有 SSE 订阅者。"""
        title, text, details = self._build_message(event, subtype, response)
        self._event_seq += 1
        item = {
            "id": self._event_seq,
            "event": event,
            "subtype": subtype,
            "title": title,
            "text": text,
            "details": details,
            "ts": time.time(),
        }
        self._browser_events.append(item)
        # 非阻塞广播：队列满则丢弃该订阅者本条，避免拖慢 on_response
        dead: list[asyncio.Queue] = []
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                dead.append(queue)
            except Exception:
                dead.append(queue)
        for queue in dead:
            self._subscribers.discard(queue)
        return item

    def status(self) -> dict:
        """管理台 /status 快照。"""
        cfg = self._settings()
        return {
            "ok": True,
            "plugin": self.name,
            "enabled": cfg["enabled"],
            "webhook_configured": cfg["url"].startswith(("http://", "https://")),
            "browser_notifications": cfg["browser_notifications"],
            "payload_format": cfg["format"],
            "cooldown_seconds": cfg["cooldown"],
            "buffered_events": len(self._browser_events),
            "subscribers": len(self._subscribers),
            "pending_webhook_tasks": len(self._tasks),
            "last_event_id": self._event_seq,
        }

    def recent_events(self, limit: int = 50) -> dict:
        """返回环形缓冲中最近 limit 条事件。"""
        items = list(self._browser_events)
        if limit < len(items):
            items = items[-limit:]
        return {
            "ok": True,
            "count": len(items),
            "last_event_id": self._event_seq,
            "events": items,
        }

    async def events_stream(self, after_id: int = 0) -> StreamingResponse:
        """建立 SSE 长连接：先补发缓冲中 after_id 之后的事件，再实时推送。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)

        async def generate():
            try:
                # 首包：补发缓冲中尚未送达的事件（断线重连用 after_id）
                for item in list(self._browser_events):
                    if int(item.get("id", 0) or 0) > after_id:
                        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                # 心跳 + 实时事件
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=25.0)
                    except asyncio.TimeoutError:
                        # 注释：浏览器/代理常会因空闲断开 SSE，定期注释行心跳保活
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        break
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                raise
            finally:
                self._subscribers.discard(queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def _send(self, url: str, payload: dict, timeout: int):
        """独立发送任务；异常只写日志，不会传播回代理调用。"""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload)
                if response.status_code >= 400:
                    self.logger.warning("[webhook_notifier] Webhook 返回 HTTP %s", response.status_code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning("[webhook_notifier] Webhook 发送失败: %s", exc)

    def _schedule(self, url: str, payload: dict, timeout: int, max_pending: int):
        """异步调度 Webhook 通知，任务完成后自动从集合移除。"""
        if len(self._tasks) >= max_pending:
            self.logger.warning("[webhook_notifier] 待发送通知已达上限，丢弃本次事件")
            return
        task = asyncio.create_task(self._send(url, payload, timeout))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def on_response(self, ctx):
        """按事件类型和冷却窗口决定是否发送 Webhook / 浏览器通知。"""
        response = ctx.response
        if not isinstance(response, dict):
            return None
        cfg = self._settings()
        if not cfg["enabled"]:
            return None

        webhook_ok = cfg["url"].startswith(("http://", "https://"))
        browser_ok = cfg["browser_notifications"]
        # 两条通道都未配置时直接跳过，避免无意义的事件分类
        if not webhook_ok and not browser_ok:
            return None

        monitor = getattr(getattr(self.app, "state", None), "health_monitor", None)
        dropped = int(getattr(monitor, "audit_queue_dropped", 0) or 0)
        events: list[tuple[str, str, dict]] = []
        if cfg["notify_audit_drops"] and dropped > self._last_audit_queue_dropped:
            events.append(("audit_drop", "audit_queue_dropped", {**response, "audit_queue_dropped": dropped}))
        self._last_audit_queue_dropped = max(self._last_audit_queue_dropped, dropped)
        matched = self._event(response, cfg)
        if matched is not None:
            events.append((matched[0], matched[1], response))
        if not events:
            return None

        for event, subtype, event_response in events:
            dedupe_key = ":".join([
                event,
                subtype,
                str(event_response.get("key_alias", "") or ""),
                str(event_response.get("model", "") or ""),
            ])
            now = time.monotonic()
            previous = self._last_sent.get(dedupe_key, 0.0)
            if cfg["cooldown"] and now - previous < cfg["cooldown"]:
                continue
            self._last_sent[dedupe_key] = now

            # 浏览器通道：进程内缓冲 + SSE，不走网络，不占用 max_pending
            if browser_ok:
                try:
                    self._publish_browser(event, subtype, event_response)
                except Exception as exc:
                    self.logger.warning("[webhook_notifier] 浏览器通知发布失败: %s", exc)

            # Webhook 通道：仍异步调度，故障不影响转发
            if webhook_ok:
                self._schedule(
                    cfg["url"],
                    self._build_payload(event, subtype, event_response, cfg["format"]),
                    cfg["timeout"],
                    cfg["max_pending"],
                )
        return None
