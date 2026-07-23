"""本地限流插件。

仅在当前 AKM 进程内生效：固定窗口 RPM/RPH + 可选并发上限。
超限时在 on_request 阶段直接 block（HTTP 429），不占用上游配额。
服务重启后计数清零。
"""

from __future__ import annotations

import json
import time

from akm.plugins import PluginBase


class Plugin(PluginBase):
    """按全局 / 模型 / 用户维度限制请求速率与并发。"""

    async def on_load(self):
        """初始化进程内计数桶。"""
        # (scope_key, window_name, window_id) -> count
        self._windows: dict[tuple[str, str, int], int] = {}
        # scope_key -> in-flight count
        self._inflight: dict[str, int] = {}

    def _settings(self) -> dict:
        """读取最新配置。"""
        cfg = self.config or {}
        scope = str(cfg.get("scope", "global") or "global").strip().lower()
        if scope not in ("global", "model", "user"):
            scope = "global"
        return {
            "enabled": cfg.get("enabled", True) is True,
            "scope": scope,
            "max_rpm": max(0, int(cfg.get("max_requests_per_minute", 60) or 0)),
            "max_rph": max(0, int(cfg.get("max_requests_per_hour", 0) or 0)),
            "max_concurrent": max(0, int(cfg.get("max_concurrent", 0) or 0)),
            "block_message": str(
                cfg.get("block_message", "请求过于频繁，已被本地限流插件拦截。")
                or "请求过于频繁，已被本地限流插件拦截。"
            ),
        }

    def _scope_key(self, request: dict, scope: str) -> str:
        """根据配置维度构造限流键。"""
        if scope == "model":
            return f"model:{request.get('model', '') or ''}"
        if scope == "user":
            user = request.get("user")
            if user is None or user == "":
                return "user:anonymous"
            return f"user:{user}"
        return "global"

    def _prune_windows(self) -> None:
        """丢弃过旧窗口，minute 保留最近 2 分钟，hour 保留最近 2 小时。"""
        now = int(time.time())
        minute_cutoff = (now // 60) - 2
        hour_cutoff = (now // 3600) - 2
        self._windows = {
            key: value
            for key, value in self._windows.items()
            if (key[1] == "minute" and key[2] >= minute_cutoff)
            or (key[1] == "hour" and key[2] >= hour_cutoff)
        }

    def _window_count(self, scope_key: str, name: str, window_seconds: int) -> int:
        """返回当前固定窗口计数。"""
        self._prune_windows()
        window_id = int(time.time() // window_seconds)
        return self._windows.get((scope_key, name, window_id), 0)

    def _bump_window(self, scope_key: str, name: str, window_seconds: int) -> int:
        """递增窗口计数并返回新值。"""
        self._prune_windows()
        window_id = int(time.time() // window_seconds)
        key = (scope_key, name, window_id)
        self._windows[key] = self._windows.get(key, 0) + 1
        return self._windows[key]

    def _block(self, ctx, message: str, reason: str) -> dict:
        """构造 on_request 阻断结构并写入 ctx.action。"""
        body = json.dumps(
            {
                "error": {
                    "message": message,
                    "type": "rate_limit_exceeded",
                    "code": "rate_limit_guard",
                    "param": reason,
                }
            },
            ensure_ascii=False,
        )
        return ctx.set_block(
            status_code=429,
            error=message,
            body=body,
            security_action="rate_limit",
            security_reason=reason,
        )

    async def on_request(self, ctx) -> dict | None:
        """检查 RPM/RPH/并发；通过则预占并发与窗口计数。"""
        request = ctx.request
        if not isinstance(request, dict):
            return None
        cfg = self._settings()
        if not cfg["enabled"]:
            return None
        if not (cfg["max_rpm"] or cfg["max_rph"] or cfg["max_concurrent"]):
            return None

        scope_key = self._scope_key(request, cfg["scope"])

        if cfg["max_rpm"]:
            current = self._window_count(scope_key, "minute", 60)
            if current >= cfg["max_rpm"]:
                return self._block(ctx, cfg["block_message"], f"rpm:{cfg['max_rpm']}")
        if cfg["max_rph"]:
            current = self._window_count(scope_key, "hour", 3600)
            if current >= cfg["max_rph"]:
                return self._block(ctx, cfg["block_message"], f"rph:{cfg['max_rph']}")
        if cfg["max_concurrent"]:
            inflight = self._inflight.get(scope_key, 0)
            if inflight >= cfg["max_concurrent"]:
                return self._block(ctx, cfg["block_message"], f"concurrent:{cfg['max_concurrent']}")

        if cfg["max_rpm"]:
            self._bump_window(scope_key, "minute", 60)
        if cfg["max_rph"]:
            self._bump_window(scope_key, "hour", 3600)
        if cfg["max_concurrent"]:
            self._inflight[scope_key] = self._inflight.get(scope_key, 0) + 1
            # 并发槽位记在 bag，on_response 释放
            ctx.bag_set("rate_limit_guard.slot", scope_key)
        return None

    async def on_response(self, ctx):
        """释放并发槽位（无论成功失败）。"""
        slot = ctx.bag_get("rate_limit_guard.slot")
        if not slot:
            return None
        current = self._inflight.get(slot, 0) - 1
        if current <= 0:
            self._inflight.pop(slot, None)
        else:
            self._inflight[slot] = current
        return None
