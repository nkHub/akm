"""本地精确响应缓存插件。

对非流式、无工具调用的请求，按规范化请求体哈希做进程内缓存。
命中时通过 on_request block 直接返回缓存正文（HTTP 200），不再访问上游。
注意：proxy 对 on_request block 的生命周期事件标记为 phase=on_request，
不影响客户端拿到的 body/status。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict

from akm.plugins import PluginBase


class Plugin(PluginBase):
    """精确匹配缓存：相同 model + 规范化请求体 → 复用响应。"""

    async def on_load(self):
        """初始化有序缓存字典（插入序用于 LRU 淘汰）。"""
        self._cache: OrderedDict[str, dict] = OrderedDict()

    def _settings(self) -> dict:
        cfg = self.config or {}
        models_raw = str(cfg.get("include_models", "") or "")
        models = []
        for line in models_raw.replace("\r", "\n").split("\n"):
            for part in line.split(","):
                item = part.strip()
                if item:
                    models.append(item)
        return {
            "enabled": cfg.get("enabled", True) is True,
            "ttl_seconds": max(1, int(cfg.get("ttl_seconds", 300) or 300)),
            "max_entries": max(1, int(cfg.get("max_entries", 256) or 256)),
            "max_body_bytes": max(1024, int(cfg.get("max_body_bytes", 262144) or 262144)),
            "skip_stream": cfg.get("skip_stream", True) is True,
            "skip_tools": cfg.get("skip_tools", True) is True,
            "include_models": models,
        }

    def _model_allowed(self, model: str, patterns: list[str]) -> bool:
        """模型白名单；空列表表示全部允许。"""
        if not patterns:
            return True
        name = str(model or "")
        for pattern in patterns:
            if pattern.endswith("*"):
                if name.startswith(pattern[:-1]):
                    return True
            elif name == pattern:
                return True
        return False

    def _is_stream_request(self, request: dict) -> bool:
        return request.get("stream") is True

    def _has_tools(self, request: dict) -> bool:
        for key in ("tools", "functions"):
            value = request.get(key)
            if isinstance(value, list) and value:
                return True
        tool_choice = request.get("tool_choice")
        # none/auto/缺省允许缓存；对象或其它字符串视为工具相关请求
        if isinstance(tool_choice, dict):
            return True
        if isinstance(tool_choice, str) and tool_choice not in ("", "none", "auto"):
            return True
        return False

    def _canonical_payload(self, request: dict) -> dict:
        """去掉仅本地字段与不影响上游语义的噪声，生成缓存键材料。"""
        skip_keys = {
            "stream",
            "stream_options",
            "user",  # 用户标识通常不影响模型输出；纳入会降低命中率
        }
        payload = {}
        for key, value in request.items():
            key_s = str(key)
            if key_s.startswith("__akm_"):
                continue
            if key_s in skip_keys:
                continue
            payload[key_s] = value
        return payload

    def _cache_key(self, request: dict) -> str:
        payload = self._canonical_payload(request)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _purge_expired(self, now: float, ttl: int):
        expired = [key for key, item in self._cache.items() if now - item["ts"] >= ttl]
        for key in expired:
            self._cache.pop(key, None)

    def _get(self, key: str, ttl: int) -> dict | None:
        now = time.time()
        self._purge_expired(now, ttl)
        item = self._cache.get(key)
        if not item:
            return None
        if now - item["ts"] >= ttl:
            self._cache.pop(key, None)
            return None
        # LRU：命中后移到末尾
        self._cache.move_to_end(key)
        return item

    def _put(self, key: str, body: str, status_code: int, api_path: str, model: str, max_entries: int):
        self._cache[key] = {
            "ts": time.time(),
            "body": body,
            "status_code": status_code,
            "api_path": api_path,
            "model": model,
        }
        self._cache.move_to_end(key)
        while len(self._cache) > max_entries:
            self._cache.popitem(last=False)

    async def on_request(self, request) -> dict | None:
        """缓存命中则短路返回；未命中则在请求上记录 cache key 供响应写入。"""
        if not isinstance(request, dict):
            return None
        cfg = self._settings()
        if not cfg["enabled"]:
            return None
        if not self._model_allowed(str(request.get("model", "") or ""), cfg["include_models"]):
            return None
        if cfg["skip_stream"] and self._is_stream_request(request):
            return None
        if cfg["skip_tools"] and self._has_tools(request):
            return None

        key = self._cache_key(request)
        hit = self._get(key, cfg["ttl_seconds"])
        if hit:
            body = hit["body"]
            # 在 JSON 响应中附加缓存标记（若可解析）
            marked = body
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    data["x_akm_cache"] = "HIT"
                    marked = json.dumps(data, ensure_ascii=False)
            except (TypeError, ValueError, json.JSONDecodeError):
                marked = body
            return {
                "__akm_action__": "block",
                "status_code": int(hit.get("status_code", 200) or 200),
                "body": marked,
                "error": "cache_hit",
                "security_action": "cache_hit",
                "security_reason": f"cache_proxy:{key[:12]}",
            }

        request["__akm_cache_key__"] = key
        request["__akm_cache_eligible__"] = True
        return request

    async def on_response(self, request, response):
        """成功非流式响应写入缓存。"""
        if not isinstance(request, dict) or not isinstance(response, dict):
            return None
        cfg = self._settings()
        if not cfg["enabled"]:
            return None
        if not request.get("__akm_cache_eligible__"):
            return None
        if not response.get("ok"):
            return None
        if response.get("stream") is True:
            return None
        if response.get("security_action") and response.get("security_action") not in ("", "cache_hit"):
            # 安全插件已改写的响应不缓存
            return None

        body = str(response.get("response_body", "") or "")
        if not body:
            return None
        if len(body.encode("utf-8")) > cfg["max_body_bytes"]:
            return None

        key = str(request.get("__akm_cache_key__") or "")
        if not key:
            return None
        self._put(
            key,
            body,
            int(response.get("status_code", 200) or 200),
            str(response.get("api_path", "") or ""),
            str(response.get("model", request.get("model", "")) or ""),
            cfg["max_entries"],
        )
        return None
