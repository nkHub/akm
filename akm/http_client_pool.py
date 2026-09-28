"""按上游路由隔离的 HTTP client 池。"""

import asyncio
import os
import re
import ssl
import time
import traceback
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from akm.error_log import write_error_log


def _ca_file_candidates() -> list[str]:
    """返回可用的 CA 文件候选路径，按稳定性从高到低排列。

    说明：certifi 在首次调用 where() 时会把 cacert.pem 解包到系统临时目录，并把
    路径缓存在模块级变量里。当该临时文件被系统清理后，后续再用这个旧路径构造
    ssl 上下文就会抛 FileNotFoundError。因此这里优先使用更稳定的来源：
    1) SSL_CERT_FILE：打包后的 .app 在启动时会指向自带的 openssl.ca/cert.pem；
    2) certifi.where()：开发环境或未注入环境变量时的默认信任库。
    """
    candidates: list[str] = []
    env_ca = (os.environ.get("SSL_CERT_FILE") or "").strip()
    if env_ca:
        candidates.append(env_ca)
    try:
        import certifi

        candidates.append(certifi.where())
    except Exception:
        # certifi 缺失或解包失败时忽略，交给后续的系统默认 CA 兜底
        pass
    return candidates


def build_upstream_ssl_context() -> ssl.SSLContext:
    """构建访问上游时使用的 TLS 上下文。

    逐个尝试候选 CA 文件，返回第一个可成功加载的上下文；全部不可用时回退到
    系统默认 CA（ssl.create_default_context()），从而彻底避开 certifi 缓存路径
    失效这一根因，保证 client 构造期不再因证书文件丢失而抛 FileNotFoundError。
    """
    for cafile in _ca_file_candidates():
        if not cafile or not os.path.isfile(cafile):
            continue
        try:
            return ssl.create_default_context(cafile=cafile)
        except (OSError, ssl.SSLError):
            # 文件存在但不可读/内容非法时继续尝试下一个候选
            continue
    return ssl.create_default_context()


# 解析 httpcore 连接的 info() 文本（形如 "https://host:443, HTTP/1.1, IDLE, Request Count: 9"）。
_HTTP_INFO_PROTOCOL = re.compile(r"(HTTP/\d(?:\.\d)?)")
_HTTP_INFO_REQUEST_COUNT = re.compile(r"Request Count:\s*(\d+)")

# 连接状态分类的稳定顺序与中文标签（快照与页面共用同一套口径）
_CONNECTION_STATE_KEYS = ("active", "idle", "connecting", "failed", "expired", "closed", "unknown")


@dataclass
class _PoolEntry:
    client: httpx.AsyncClient
    last_used_at: float
    created_at: float
    pool_key: str
    provider: str
    key_alias: str
    model: str
    api_path: str
    routed_requests: int = 0


def _pool_object(client) -> object | None:
    """取出 httpx client 底层的 httpcore 连接池；取不到时返回 None。

    连接池详情页要展示「真正活着」的 TCP 连接、并支持只关空闲连接，因此这里
    访问 httpx 的私有传输对象。所有调用点都做了兜底：拿不到就退化为只展示 AKM
    侧统计，绝不影响转发链路。
    """
    transport = getattr(client, "_transport", None)
    pool = getattr(transport, "_pool", None)
    return pool if pool is not None else None


def _pool_connections(pool) -> list:
    """返回池内连接对象列表；池已关闭或对象异常时返回空列表。"""
    try:
        conns = getattr(pool, "connections", None)
        return list(conns) if conns is not None else []
    except Exception:
        return []


def _probe_connection(conn) -> dict:
    """把单个 httpcore 连接归类成一个可展示的状态条目。"""
    info = ""
    try:
        info = str(conn.info() or "")
    except Exception:
        info = ""

    state = "unknown"
    try:
        inner = getattr(conn, "_connection", None)
        if inner is None:
            # 尚未建连：区分「正在连接」与「连接失败」
            state = "failed" if getattr(conn, "_connect_failed", False) else "connecting"
        elif conn.is_closed():
            state = "closed"
        elif conn.has_expired():
            state = "expired"
        elif conn.is_idle():
            state = "idle"
        else:
            state = "active"
    except Exception:
        state = "unknown"

    origin = ""
    raw_origin = getattr(conn, "_origin", None)
    if raw_origin is not None:
        try:
            origin = str(raw_origin)
        except Exception:
            origin = ""
    if not origin and info:
        origin = info.split(",", 1)[0].strip().strip("'\"")

    protocol = ""
    match = _HTTP_INFO_PROTOCOL.search(info)
    if match:
        protocol = match.group(1)

    request_count = 0
    match = _HTTP_INFO_REQUEST_COUNT.search(info)
    if match:
        try:
            request_count = int(match.group(1))
        except ValueError:
            request_count = 0

    return {
        "state": state,
        "origin": origin,
        "protocol": protocol,
        "request_count": request_count,
        "info": info,
    }


def _probe_pool(pool) -> dict:
    """统计一个 httpcore 连接池的实时连接分布与排队请求数。"""
    connections = [_probe_connection(conn) for conn in _pool_connections(pool)]
    counts = {key: 0 for key in _CONNECTION_STATE_KEYS}
    for item in connections:
        state = item.get("state") or "unknown"
        counts[state if state in counts else "unknown"] += 1
    counts["total"] = len(connections)

    waiting = 0
    pending = getattr(pool, "_requests", None)
    if isinstance(pending, list):
        for request in list(pending):
            try:
                if request.is_queued():
                    waiting += 1
            except Exception:
                continue

    return {"counts": counts, "waiting": waiting, "connections": connections}


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

        # ── 页面/诊断用累计统计（只增不减，池重建后随新实例归零）──
        self.instance_id = uuid.uuid4().hex[:8]
        self.created_at = time.time()
        self.routed_requests_total = 0
        self.pools_created_total = 0
        self.pools_evicted_idle_total = 0
        self.pools_evicted_lru_total = 0
        self.pools_closed_total = 0
        self.connections_closed_total = 0
        self.build_failures_total = 0
        self.last_build_error = ""

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
            # 显式传入稳定的 TLS 上下文：避免 httpx 默认走 certifi.where() 时，
            # 其临时解包出的 cacert.pem 被系统清理后构造期抛 FileNotFoundError，
            # 导致本应正常的请求被降级成“上游不可用”并频繁切换 Key。
            "verify": build_upstream_ssl_context(),
        }
        if self.proxy_url:
            # httpx 0.28+ 使用 proxy=；SOCKS 需安装 httpx[socks] / socksio
            kwargs["proxy"] = self.proxy_url
        # 统一关闭 trust_env：无论是否配置出站代理，都不读取系统环境变量里的
        # HTTP(S)_PROXY / ALL_PROXY，代理由 AKM 配置显式控制。证书已由上面的
        # verify 显式指定，不再依赖环境变量解析，因此不会再出现“环境变量指向
        # 不存在的证书文件导致 AsyncClient 构造失败”的问题。
        kwargs["trust_env"] = False
        return httpx.AsyncClient(**kwargs)

    async def get_client(self, *, provider: str, key_alias: str, model: str, api_path: str) -> httpx.AsyncClient | None:
        pool_key = self._pool_key(provider, key_alias, model, api_path)
        now = time.time()
        entry = self._entries.get(pool_key)
        if entry is not None:
            entry.last_used_at = now
            entry.routed_requests += 1
            self.routed_requests_total += 1
            return entry.client

        async with self._lock:
            entry = self._entries.get(pool_key)
            if entry is not None:
                entry.last_used_at = now
                entry.routed_requests += 1
                self.routed_requests_total += 1
                return entry.client
            await self._cleanup_locked(now)
            try:
                client = self._build_client()
            except Exception as exc:
                # AsyncClient 构造失败（如环境残留失效 SSL_CERT_FILE 指向的文件缺失、
                # 无效代理 URL 等）不能让异常击穿整个转发链路变成 500：记入 error.log
                # 后返回 None，由调用方按“该 Key 上游不可用”降级处理（记录尝试并切换
                # 下一个 Key / 走通配符兜底），与网络层失败同等对待。
                self.build_failures_total += 1
                self.last_build_error = str(exc)
                write_error_log(
                    source="http_client_pool.get_client",
                    error=f"创建上游 HTTP client 失败: {exc}",
                    traceback_str=traceback.format_exc(),
                    extra={"provider": provider, "key_alias": key_alias, "model": model, "api_path": api_path},
                )
                return None
            self._entries[pool_key] = _PoolEntry(
                client=client,
                last_used_at=now,
                created_at=now,
                pool_key=pool_key,
                provider=str(provider or ""),
                key_alias=str(key_alias or ""),
                model=str(model or ""),
                api_path=str(api_path or ""),
                routed_requests=1,
            )
            self.pools_created_total += 1
            self.routed_requests_total += 1
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
                self.pools_evicted_idle_total += 1
                await entry.client.aclose()

        while len(self._entries) >= self.max_pools:
            oldest_key = min(self._entries, key=lambda key: self._entries[key].last_used_at)
            entry = self._entries.pop(oldest_key)
            self.pools_evicted_lru_total += 1
            await entry.client.aclose()

    async def close_idle_connections(self) -> dict:
        """关闭各路由池里处于空闲状态的 TCP 连接，但保留路由池本身。

        页面上的「清理空闲连接」：不改变路由与池数量，只把 keep-alive 空闲连接
        立刻释放（等价于把自动回收提前触发一次）。若底层连接对象不可见（测试里
        的假 client、httpx 版本变化），静默跳过而不是报错。
        """
        closed = 0
        affected = 0
        for entry in list(self._entries.values()):
            pool = _pool_object(entry.client)
            if pool is None:
                continue
            count = await self._close_idle_in_pool(pool)
            if count:
                closed += count
                affected += 1
        self.connections_closed_total += closed
        return {"closed_connections": closed, "affected_pools": affected}

    async def _close_idle_in_pool(self, pool) -> int:
        """关闭单个 httpcore 池中的空闲连接，返回实际关闭数量。"""
        idle = [conn for conn in _pool_connections(pool) if _probe_connection(conn).get("state") == "idle"]
        if not idle:
            return 0
        # 同步从池的连接表里摘除，避免关闭后仍占用 max_connections 名额；
        # httpcore 自身的 _assign_requests_to_connections 只清理已摘除/已关闭的连接。
        tracked = getattr(pool, "_connections", None)
        if isinstance(tracked, list):
            for conn in idle:
                try:
                    tracked.remove(conn)
                except ValueError:
                    pass
        closed = 0
        for conn in idle:
            try:
                await conn.aclose()
                closed += 1
            except Exception:
                continue
        return closed

    async def close_pool(self, pool_key: str) -> bool:
        """按 pool_key 关闭单个路由池，返回是否命中。"""
        async with self._lock:
            entry = self._entries.pop(str(pool_key or ""), None)
        if entry is None:
            return False
        self.pools_closed_total += 1
        try:
            await entry.client.aclose()
        except Exception:
            pass
        return True

    async def evict_idle_pools(self) -> dict:
        """立刻回收空闲时长已超过 idle_ttl_sec 的路由池。"""
        now = time.time()
        async with self._lock:
            stale_keys = [
                key
                for key, entry in self._entries.items()
                if now - entry.last_used_at >= self.idle_ttl_sec
            ]
            entries = [self._entries.pop(key) for key in stale_keys]
            self.pools_evicted_idle_total += len(entries)
        for entry in entries:
            try:
                await entry.client.aclose()
            except Exception:
                continue
        return {"evicted_pools": len(entries), "max_pools": self.max_pools}

    def _proxy_display(self) -> str:
        """返回不含账号密码的代理展示串，避免把凭据暴露到页面。"""
        if not self.proxy_url:
            return ""
        try:
            parts = urlsplit(self.proxy_url)
            host = parts.hostname or ""
            if not host:
                return parts.scheme or ""
            port = f":{parts.port}" if parts.port else ""
            return f"{parts.scheme}://{host}{port}"
        except Exception:
            return ""

    def stats(self) -> dict:
        """轻量聚合统计（不探测底层连接），供 /debug/runtime 与页面摘要复用。"""
        return {
            "instance_id": self.instance_id,
            "started_at": self.created_at,
            "pool_count": len(self._entries),
            "max_pools": self.max_pools,
            "idle_ttl_sec": self.idle_ttl_sec,
            "max_connections_per_pool": self.max_connections,
            "max_keepalive_per_pool": self.max_keepalive_connections,
            "timeout_sec": self.timeout_sec,
            "connect_timeout_sec": self.connect_timeout_sec,
            # 仅暴露是否启用，避免把带账号密码的代理 URL 打进调试接口
            "proxy_enabled": bool(self.proxy_url),
            "routed_requests": self.routed_requests_total,
            "pools_created": self.pools_created_total,
            "pools_evicted_idle": self.pools_evicted_idle_total,
            "pools_evicted_lru": self.pools_evicted_lru_total,
            "pools_closed": self.pools_closed_total,
            "connections_closed": self.connections_closed_total,
            "build_failures": self.build_failures_total,
        }

    def snapshot(self) -> dict:
        """构建连接池状态快照：聚合指标 + 每个路由池的实时连接明细。

        会读取 httpx/httpcore 底层的真实连接状态（活跃 / 空闲 / 建连中 / 失败 /
        已过期）与排队请求数，因此比 stats() 重一些，仅供诊断接口与连接池页面
        调用，不在转发链路上执行。
        """
        now = time.time()
        pools: list[dict] = []
        totals = {
            "pool_count": 0,
            "live_connections": 0,
            "active_connections": 0,
            "idle_connections": 0,
            "connecting_connections": 0,
            "failed_connections": 0,
            "expired_connections": 0,
            "waiting_requests": 0,
            "routed_requests": 0,
            "stale_pools": 0,
        }

        for pool_key, entry in list(self._entries.items()):
            pool = _pool_object(entry.client)
            if pool is not None:
                probe = _probe_pool(pool)
            else:
                probe = {"counts": {key: 0 for key in _CONNECTION_STATE_KEYS} | {"total": 0}, "waiting": 0, "connections": []}
            counts = probe["counts"]
            idle_sec = max(0.0, now - entry.last_used_at)
            stale = idle_sec >= self.idle_ttl_sec

            if probe["waiting"] > 0:
                state = "saturated"
            elif counts["active"] > 0:
                state = "busy"
            elif counts["total"] > 0:
                state = "idle"
            else:
                state = "empty"

            totals["pool_count"] += 1
            totals["live_connections"] += counts["total"]
            totals["active_connections"] += counts["active"]
            totals["idle_connections"] += counts["idle"]
            totals["connecting_connections"] += counts["connecting"]
            totals["failed_connections"] += counts["failed"]
            totals["expired_connections"] += counts["expired"]
            totals["waiting_requests"] += probe["waiting"]
            totals["routed_requests"] += entry.routed_requests
            totals["stale_pools"] += 1 if stale else 0

            pools.append({
                "pool_key": pool_key,
                "provider": entry.provider,
                "key_alias": entry.key_alias,
                "model": entry.model,
                "api_path": entry.api_path,
                "state": state,
                "created_at": entry.created_at,
                "last_used_at": entry.last_used_at,
                "idle_sec": round(idle_sec, 3),
                "stale": stale,
                "routed_requests": entry.routed_requests,
                "connection_total": counts["total"],
                "active_connections": counts["active"],
                "idle_connections": counts["idle"],
                "connecting_connections": counts["connecting"],
                "failed_connections": counts["failed"],
                "expired_connections": counts["expired"],
                "unknown_connections": counts["unknown"],
                "waiting_requests": probe["waiting"],
                "max_connections": self.max_connections,
                "connections": probe["connections"][: self.max_connections],
            })

        # 最近使用优先，便于页面默认把热路由排在前面
        pools.sort(key=lambda item: item["last_used_at"], reverse=True)
        totals["pool_capacity_ratio"] = round(totals["pool_count"] / self.max_pools, 4)

        reasons: list[str] = []
        level = "healthy"
        if totals["waiting_requests"] > 0:
            level = "saturated"
            reasons.append(f"{totals['waiting_requests']} 个请求正在等待可用连接")
        elif totals["pool_count"] >= self.max_pools:
            level = "saturated"
            reasons.append("路由池数量已达上限，新建路由会触发 LRU 淘汰")
        elif totals["failed_connections"] > 0:
            level = "degraded"
            reasons.append(f"{totals['failed_connections']} 条连接建立失败")
        elif totals["active_connections"] > 0:
            level = "busy"
        elif totals["pool_count"] == 0:
            level = "idle"

        return {
            "captured_at": now,
            "instance_id": self.instance_id,
            "started_at": self.created_at,
            "uptime_sec": round(max(0.0, now - self.created_at), 3),
            "proxy_enabled": bool(self.proxy_url),
            "proxy_display": self._proxy_display(),
            "config": {
                "max_pools": self.max_pools,
                "max_connections_per_pool": self.max_connections,
                "max_keepalive_per_pool": self.max_keepalive_connections,
                "idle_ttl_sec": self.idle_ttl_sec,
                "timeout_sec": self.timeout_sec,
                "connect_timeout_sec": self.connect_timeout_sec,
            },
            "status": {"level": level, "reasons": reasons},
            "totals": totals,
            "counters": {
                "routed_requests": self.routed_requests_total,
                "pools_created": self.pools_created_total,
                "pools_evicted_idle": self.pools_evicted_idle_total,
                "pools_evicted_lru": self.pools_evicted_lru_total,
                "pools_closed": self.pools_closed_total,
                "connections_closed": self.connections_closed_total,
                "build_failures": self.build_failures_total,
                "last_build_error": self.last_build_error,
            },
            "pools": pools,
        }

    async def aclose(self) -> None:
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            await entry.client.aclose()
