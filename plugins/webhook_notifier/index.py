"""上游事件的异步 Webhook 通知插件。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from akm.plugins import PluginBase


class Plugin(PluginBase):
    """基于 on_response 元信息发送失败、安全与慢请求通知。"""

    async def on_load(self):
        """初始化进程内去重表和后台任务集合。"""
        self._last_sent: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_audit_queue_dropped = 0

    async def on_unload(self):
        """服务停止时取消尚未开始的通知任务，避免阻塞关闭流程。"""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    def _settings(self) -> dict:
        """读取最新配置，保存后下一次事件立即采用新策略。"""
        cfg = self.config or {}
        return {
            "enabled": cfg.get("enabled", True) is True,
            "url": str(cfg.get("webhook_url", "") or "").strip(),
            "format": str(cfg.get("payload_format", "generic") or "generic").strip().lower(),
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

    def _build_payload(self, event: str, subtype: str, response: dict, fmt: str) -> dict:
        """生成通用或常见协作工具兼容的纯文本消息体。"""
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
        }
        details["audit_queue_dropped"] = int(response.get("audit_queue_dropped", 0) or 0)
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
        if fmt == "feishu":
            return {"msg_type": "text", "content": {"text": text}}
        if fmt == "wecom":
            return {"msgtype": "text", "text": {"content": text}}
        if fmt == "slack":
            return {"text": text}
        return {"event": event, "title": title, "text": text, "details": details}

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
        """异步调度通知，任务完成后自动从集合移除。"""
        if len(self._tasks) >= max_pending:
            self.logger.warning("[webhook_notifier] 待发送通知已达上限，丢弃本次事件")
            return
        task = asyncio.create_task(self._send(url, payload, timeout))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def on_response(self, request, response):
        """按事件类型和冷却窗口决定是否发送通知。"""
        if not isinstance(response, dict):
            return None
        cfg = self._settings()
        if not cfg["enabled"] or not cfg["url"].startswith(("http://", "https://")):
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
            self._schedule(
                cfg["url"],
                self._build_payload(event, subtype, event_response, cfg["format"]),
                cfg["timeout"],
                cfg["max_pending"],
            )
        return None
