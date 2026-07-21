"""本地请求与 Token 窗口配额插件。

该插件不读取供应商账单，也不持久化额度。它只依据 AKM 已实际转发的
请求和从响应中解析到的 usage 做短窗口保护，因此适合防止某个 Key 或模型
在本地代理进程中短时间被过度消耗。
"""

from __future__ import annotations

import json
import time

from akm.plugins import PluginBase


class Plugin(PluginBase):
    """在选 Key 时预留请求额度，在响应结束后累计实际 Token。"""

    async def on_load(self):
        """初始化进程内计数器；配额状态不写入用户配置或数据库。"""
        self._buckets: dict[tuple[str, str, int], dict[str, int]] = {}

    def _settings(self) -> dict:
        """每次读取最新配置，使管理台保存后无需重启即可生效。"""
        cfg = self.config or {}
        return {
            "enabled": cfg.get("enabled", True) is True,
            "window_seconds": max(60, int(cfg.get("window_seconds", 3600) or 3600)),
            "max_requests_per_key": max(0, int(cfg.get("max_requests_per_key", 0) or 0)),
            "max_requests_per_model": max(0, int(cfg.get("max_requests_per_model", 0) or 0)),
            "max_tokens_per_key": max(0, int(cfg.get("max_tokens_per_key", 0) or 0)),
            "max_tokens_per_model": max(0, int(cfg.get("max_tokens_per_model", 0) or 0)),
        }

    def _bucket(self, scope: str, identifier: str, window_id: int) -> dict[str, int]:
        """取得指定窗口的计数桶，并在自然过期后清理旧窗口。"""
        cutoff = window_id - 2
        self._buckets = {
            key: value for key, value in self._buckets.items() if key[2] >= cutoff
        }
        return self._buckets.setdefault(
            (scope, identifier, window_id), {"requests": 0, "tokens": 0}
        )

    def _quota_error(self, scope_label: str, limit: int, metric: str) -> dict:
        """形成统一的跳过控制结构，供 proxy 尝试同模型的其他 Key。"""
        return {
            "__akm_action__": "skip_key",
            "error": f"{scope_label}{metric}已达到当前窗口上限 ({limit})，已跳过该 Key",
            "security_action": "quota",
        }

    async def on_key_selected(self, model: str, key: dict, request) -> dict | None:
        """检查并预留请求次数；达到上限时让代理重新选择另一个 Key。"""
        cfg = self._settings()
        if not cfg["enabled"]:
            return None

        window_id = int(time.time() // cfg["window_seconds"])
        alias = str((key or {}).get("alias", "") or "")
        model_name = str(model or "")
        key_bucket = self._bucket("key", alias, window_id)
        model_bucket = self._bucket("model", model_name, window_id)

        if cfg["max_requests_per_key"] and key_bucket["requests"] >= cfg["max_requests_per_key"]:
            return self._quota_error(f"Key {alias} 的", cfg["max_requests_per_key"], "请求数")
        if cfg["max_requests_per_model"] and model_bucket["requests"] >= cfg["max_requests_per_model"]:
            return self._quota_error(f"模型 {model_name} 的", cfg["max_requests_per_model"], "请求数")
        if cfg["max_tokens_per_key"] and key_bucket["tokens"] >= cfg["max_tokens_per_key"]:
            return self._quota_error(f"Key {alias} 的", cfg["max_tokens_per_key"], "Token")
        if cfg["max_tokens_per_model"] and model_bucket["tokens"] >= cfg["max_tokens_per_model"]:
            return self._quota_error(f"模型 {model_name} 的", cfg["max_tokens_per_model"], "Token")

        # 在真正发送前预留请求次数，避免并发请求同时通过同一额度检查。
        key_bucket["requests"] += 1
        model_bucket["requests"] += 1
        return None

    def _extract_total_tokens(self, response_body: str) -> int:
        """兼容 Chat、Responses、Messages 及 SSE 文本，取最大可信 total_tokens。"""
        candidates: list[int] = []

        def collect_usage(payload):
            if not isinstance(payload, dict):
                return
            usage = payload.get("usage")
            if not isinstance(usage, dict):
                return
            total = usage.get("total_tokens")
            if total is None:
                total = (usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0) + (
                    usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
                )
            try:
                candidates.append(max(0, int(total or 0)))
            except (TypeError, ValueError):
                pass

        try:
            collect_usage(json.loads(response_body))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

        # 流式响应在审计捕获中保留 SSE 行，逐个 data JSON 查找最终 usage。
        for line in str(response_body or "").splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            collect_usage(payload)
            collect_usage(payload.get("response") if isinstance(payload, dict) else None)
            collect_usage(payload.get("message") if isinstance(payload, dict) else None)
        return max(candidates, default=0)

    async def on_response(self, request, response):
        """响应结束后把成功请求的实际 Token 计入对应窗口。"""
        cfg = self._settings()
        if not cfg["enabled"] or not isinstance(response, dict) or not response.get("ok"):
            return None

        tokens = self._extract_total_tokens(str(response.get("response_body", "") or ""))
        if tokens <= 0:
            return None
        window_id = int(time.time() // cfg["window_seconds"])
        alias = str(response.get("key_alias", "") or "")
        model = str(response.get("model", "") or "")
        self._bucket("key", alias, window_id)["tokens"] += tokens
        self._bucket("model", model, window_id)["tokens"] += tokens
        return None
