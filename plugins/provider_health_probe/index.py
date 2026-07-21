"""供应商 Key 健康探测应用插件。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException

from akm.key_pool import get_key, list_keys
from akm.plugins import PluginBase
from akm.proxy import test_key_connectivity


router = APIRouter()
_plugin_instance: "Plugin | None" = None


def _plugin() -> "Plugin":
    """取得由 PluginManager 完成上下文注入的唯一插件实例。"""
    if _plugin_instance is None or not _plugin_instance.enabled:
        raise HTTPException(status_code=503, detail="provider_health_probe 插件未启用")
    return _plugin_instance


@router.get("/status")
async def status():
    """返回最近探测结果，不包含 API Key、请求体或上游 URL。"""
    return _plugin().status()


@router.post("/probe")
async def probe(payload: dict = Body(default={})):
    """手动探测指定 aliases 或全部 active Key。"""
    aliases = payload.get("aliases") if isinstance(payload.get("aliases"), list) else None
    include_inactive = bool(payload.get("include_inactive", False))
    allow_fallback = payload.get("allow_protocol_fallback")
    return await _plugin().probe(aliases=aliases, include_inactive=include_inactive, allow_fallback=allow_fallback)


class Plugin(PluginBase):
    """维护最近探测快照，并可按配置启动低频后台探测。"""

    router = router

    async def on_load(self):
        """初始化快照并按需启动定时任务。"""
        global _plugin_instance
        _plugin_instance = self
        self._results: dict[str, dict] = {}
        self._probe_task: asyncio.Task | None = None
        interval = self._interval()
        if interval > 0:
            self._probe_task = asyncio.create_task(self._run_periodic(interval))

    async def on_unload(self):
        """关闭插件时取消定时任务，不等待未完成探测阻塞服务退出。"""
        if self._probe_task is not None:
            self._probe_task.cancel()
            self._probe_task = None

    def _interval(self) -> int:
        """将间隔收敛到合理范围，0 保持手动模式。"""
        return min(86400, max(0, int((self.config or {}).get("probe_interval_seconds", 0) or 0)))

    def _concurrency(self) -> int:
        """限制探测并发，保护正常转发使用的上游连接。"""
        return min(20, max(1, int((self.config or {}).get("max_concurrency", 3) or 3)))

    async def _run_periodic(self, interval: int):
        """低频循环；单轮失败记录到日志后继续下一轮。"""
        while True:
            try:
                await self.probe()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning("[provider_health_probe] 定时探测失败: %s", exc)
                await asyncio.sleep(interval)

    def status(self) -> dict:
        """返回按 Key 聚合的最近结果和基础统计。"""
        results = [self._results[name] for name in sorted(self._results)]
        return {
            "ok": True,
            "total": len(results),
            "healthy": sum(1 for item in results if item.get("ok")),
            "unhealthy": sum(1 for item in results if not item.get("ok")),
            "results": results,
        }

    async def probe(self, aliases=None, include_inactive: bool = False, allow_fallback=None) -> dict:
        """并发受控地探测候选 Key，并仅保存可安全展示的结果字段。"""
        requested = {str(alias) for alias in aliases or [] if str(alias)}
        candidates = [key for key in list_keys() if (not requested or key.get("alias") in requested)]
        if not include_inactive:
            candidates = [key for key in candidates if key.get("status") == "active"]
        allow = bool((self.config or {}).get("allow_protocol_fallback", False)) if allow_fallback is None else bool(allow_fallback)
        semaphore = asyncio.Semaphore(self._concurrency())

        async def check(summary: dict):
            alias = str(summary.get("alias", "") or "")
            full_key = get_key(alias)
            if full_key is None:
                return None
            async with semaphore:
                result = await test_key_connectivity(full_key, allow_fallback=allow)
            snapshot = {
                "alias": alias,
                "provider": str(summary.get("provider", "") or ""),
                "status": str(summary.get("status", "") or ""),
                "ok": bool(result.get("ok", False)),
                "status_code": int(result.get("status_code", 0) or 0),
                "latency_ms": int(result.get("latency_ms", 0) or 0),
                "model": str(result.get("model", "") or ""),
                "api_path": str(result.get("api_path", "") or ""),
                "fallback_used": bool(result.get("fallback_used", False)),
                "error": str(result.get("error", "") or "")[:1000],
                "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
            self._results[alias] = snapshot
            return snapshot

        checked = [item for item in await asyncio.gather(*(check(key) for key in candidates)) if item is not None]
        return {"ok": True, "checked": len(checked), "results": checked}
