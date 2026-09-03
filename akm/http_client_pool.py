"""按上游路由隔离的 HTTP client 池。"""

import asyncio
import time
from dataclasses import dataclass

import httpx

from akm.error_log import write_error_log


@dataclass
class _PoolEntry:
    client: httpx.AsyncClient
    last_used_at: float


class HttpClientPoolManager:
    """懒创建并复用按路由隔离的 httpx client。

    隔离维度使用 provider/key/model/api_path，避免某个长流请求长期占用全局连接池后，
    连带影响其他 key 或模型的请求。client 只在第一次命中对应路由时创建，空闲过久或
    超过池数量上限时再关闭回收。

    proxy_url 作用于本池创建的全部 client，用于 AKM 访问上游时的出站 HTTP/SOCKS 代理。
    """

    is_route_pool = True

    def __init__(
        self,
        *,
        max_pools: int = 64,
        idle_ttl_sec: float = 120.0,
        max_connections: int = 8,
        max_keepalive_connections: int = 2,
        timeout_sec: float = 120.0,
        connect_timeout_sec: float = 10.0,
        proxy_url: str | None = None,
    ):
        self.max_pools = max(1, int(max_pools or 64))
        self.idle_ttl_sec = max(30.0, float(idle_ttl_sec or 120.0))
        self.max_connections = max(1, int(max_connections or 8))
        self.max_keepalive_connections = max(0, int(max_keepalive_connections or 2))
        self.timeout_sec = max(1.0, float(timeout_sec or 120.0))
        self.connect_timeout_sec = max(1.0, float(connect_timeout_sec or 10.0))
        # 空串与 None 均视为直连；非空则交给 httpx 作为统一出站代理
        self.proxy_url = str(proxy_url or "").strip() or None
        self._entries: dict[str, _PoolEntry] = {}
        self._lock = asyncio.Lock()

    def _pool_key(self, provider: str, key_alias: str, model: str, api_path: str) -> str:
        parts = [provider, key_alias, model, api_path]
        return ":".join(str(part or "unknown").strip() or "unknown" for part in parts)

    def _build_client(self) -> httpx.AsyncClient:
        """创建带超时、连接上限与可选出站代理的 AsyncClient。"""
        keepalive = min(self.max_keepalive_connections, self.max_connections)
        limits = httpx.Limits(max_keepalive_connections=keepalive, max_connections=self.max_connections)
        kwargs = {
            "limits": limits,
            "timeout": httpx.Timeout(self.timeout_sec, connect=self.connect_timeout_sec),
        }
        if self.proxy_url:
            # httpx 0.28+ 使用 proxy=；SOCKS 需安装 httpx[socks] / socksio
            kwargs["proxy"] = self.proxy_url
        # 统一关闭 trust_env：无论是否配置出站代理，都不读取系统环境变量里的
        # HTTP(S)_PROXY / ALL_PROXY 代理，也不读取 SSL_CERT_FILE / SSL_CERT_DIR
        # 指定的证书文件。代理由 AKM 配置显式控制；证书走 httpx 默认信任库。
        # 否则当环境变量指向不存在的证书文件时，httpx 会在 AsyncClient 构造期
        # （create_default_context）抛 FileNotFoundError，让本应在网络期的错误
        # 变成 500（实测：代理节点 + 失效 SSL_CERT_FILE 即可复现）。
        kwargs["trust_env"] = False
        return httpx.AsyncClient(**kwargs)

    async def get_client(self, *, provider: str, key_alias: str, model: str, api_path: str) -> httpx.AsyncClient | None:
        pool_key = self._pool_key(provider, key_alias, model, api_path)
        now = time.time()
        entry = self._entries.get(pool_key)
        if entry is not None:
            entry.last_used_at = now
            return entry.client

        async with self._lock:
            entry = self._entries.get(pool_key)
            if entry is not None:
                entry.last_used_at = now
                return entry.client
            await self._cleanup_locked(now)
            try:
                client = self._build_client()
            except Exception as exc:
                # AsyncClient 构造失败（如环境残留失效 SSL_CERT_FILE 指向的文件缺失、
                # 无效代理 URL 等）不能让异常击穿整个转发链路变成 500：记入 error.log
                # 后返回 None，由调用方按“该 Key 上游不可用”降级处理（记录尝试并切换
                # 下一个 Key / 走通配符兜底），与网络层失败同等对待。
                write_error_log(
                    source="http_client_pool.get_client",
                    error=f"创建上游 HTTP client 失败: {exc}",
                    extra={"provider": provider, "key_alias": key_alias, "model": model, "api_path": api_path},
                )
                return None
            self._entries[pool_key] = _PoolEntry(client=client, last_used_at=now)
            return client

    async def _cleanup_locked(self, now: float) -> None:
        stale_keys = [
            key
            for key, entry in self._entries.items()
            if now - entry.last_used_at >= self.idle_ttl_sec
        ]
        for key in stale_keys:
            entry = self._entries.pop(key, None)
            if entry is not None:
                await entry.client.aclose()

        while len(self._entries) >= self.max_pools:
            oldest_key = min(self._entries, key=lambda key: self._entries[key].last_used_at)
            entry = self._entries.pop(oldest_key)
            await entry.client.aclose()

    async def aclose(self) -> None:
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            await entry.client.aclose()

    def stats(self) -> dict:
        return {
            "pool_count": len(self._entries),
            "max_pools": self.max_pools,
            "idle_ttl_sec": self.idle_ttl_sec,
            "max_connections_per_pool": self.max_connections,
            "max_keepalive_per_pool": self.max_keepalive_connections,
            # 仅暴露是否启用，避免把带账号密码的代理 URL 打进调试接口
            "proxy_enabled": bool(self.proxy_url),
        }
