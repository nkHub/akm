"""FastAPI 服务：接收 OpenAI 兼容请求并代理转发"""

import json
import logging
import asyncio
import time
import os
import sys
import traceback
import gc
import resource
import threading
from datetime import datetime
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request, Query, UploadFile
from fastapi.responses import JSONResponse, Response, HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile as StarletteUploadFile
from akm import __version__
from akm.proxy import forward_request, test_key_connectivity
from akm.key_pool import (
    list_keys, add_key, get_key, set_api_key,
    set_priority, set_base_url, set_status, remove_key,
    set_provider, set_models, set_auth_header, set_provider_models, key_model_list,
)
from akm.audit import (
    write_log_async,
    list_logs_async,
    count_logs_async,
    clean_logs_async,
    clean_log_bodies_async,
    AuditLogQueue,
)
from akm.config import load_config, save_config, get as config_get
from akm.agent import register_agent, unregister_agent, list_agents, load_custom_agents, get_agent
from akm.plugins.plugin_manager import PluginManager
from akm.db import get_keys_log_path
from akm.health import HealthMonitor
from akm.usage_flags import (
    FLAG_COUNT_TOKENS_FALLBACK,
    FLAG_USAGE_FALLBACK_ADAPTER,
    FLAG_USAGE_ESTIMATED_LIGHT,
    FLAG_MISSING_USAGE_UPSTREAM,
    FLAG_LOOP_GUARD_TRIGGERED,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：维护共享 HTTP 连接池，避免每次请求重新建立 TCP 连接"""
    load_custom_agents()  # 启动时加载自定义 Agent
    # 确保数据库及表存在（打包环境不经过 CLI，需要在此初始化）
    from akm.db import get_connection, init_db
    conn = get_connection()
    init_db(conn)
    conn.close()
    # 初始化插件管理器
    plugin_manager = PluginManager()
    await plugin_manager.load_all(app, conn)
    app.state.plugin_manager = plugin_manager
    health_monitor = HealthMonitor()
    app.state.health_monitor = health_monitor
    app.state.health_task = asyncio.create_task(health_monitor.run_heartbeat())
    audit_queue = AuditLogQueue(maxsize=512)
    await audit_queue.start()
    app.state.audit_log_queue = audit_queue
    app.state.http_client_lock = asyncio.Lock()
    app.state.http_client = _build_shared_http_client()
    try:
        yield
    finally:
        await app.state.audit_log_queue.stop()
        app.state.health_task.cancel()
        try:
            await app.state.health_task
        except asyncio.CancelledError:
            pass
        await app.state.http_client.aclose()


app = FastAPI(title="AI Key Manager", version=__version__, lifespan=lifespan)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理：500 时返回详细报错信息，方便本地排查"""
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "traceback": traceback.format_exc()},
    )
logger = logging.getLogger("akm")

DEFAULT_IMAGE_GENERATION_MODEL = "gpt-image-2"

# 转换告警码 -> 可读文案（供日志 API 补充派生字段）
_CONV_WARNING_LABELS = {
    "responses_include_not_fully_mapped": "include 未完整映射",
    "responses_store_not_mapped": "store 未映射",
    "responses_reasoning_summary_not_mapped": "reasoning.summary 未映射",
    "responses_parallel_tool_calls_not_mapped": "parallel_tool_calls 未映射",
}

# 静态文件
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# 简易模板引擎 — 读取模板文件并做变量替换
import re as _re
_tpl_dir = os.path.join(os.path.dirname(__file__), "templates")


def _build_trace_headers(request: Request) -> tuple[dict, str]:
    """提取请求头中的来源线索，并返回对象与 JSON 文本。"""
    trace_headers = {k.lower(): v for k, v in request.headers.items()}
    trace_keys = [
        "user-agent", "x-request-id", "x-stainless-os", "x-stainless-lang",
        "x-stainless-package-version", "x-stainless-runtime", "x-stainless-runtime-version",
        "x-forwarded-for", "x-real-ip", "origin", "referer", "host",
    ]
    trace_headers_json = json.dumps(
        {k: trace_headers[k] for k in trace_keys if k in trace_headers},
        ensure_ascii=False,
    )
    return trace_headers, trace_headers_json


def _safe_request_body_for_log(body) -> str:
    """把请求体转换成适合审计日志落库的稳定 JSON 文本。

    设计目标：
    1. 普通 JSON 请求保持原样 `json.dumps`；
    2. multipart 请求中如果包含 bytes / 文件对象元组，不再直接触发序列化异常；
    3. 对文件内容只保留文件名、content_type、字节数等元信息，避免日志里塞入二进制大对象。
    """
    def _normalize(value):
        if isinstance(value, bytes):
            return {"__type__": "bytes", "size": len(value)}
        if isinstance(value, tuple) and len(value) >= 3 and isinstance(value[1], (bytes, bytearray)):
            return {
                "filename": value[0],
                "size": len(value[1]),
                "content_type": value[2],
            }
        if isinstance(value, dict):
            return {str(k): _normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_normalize(v) for v in value]
        return value

    return json.dumps(_normalize(body), ensure_ascii=False)


def _image_supported_models_from_config(cfg: dict | None = None) -> list[str]:
    """从全局配置解析图片模型列表。"""
    config_data = cfg or load_config()
    raw = str(config_data.get("image_supported_models") or "gpt-image-2").strip()
    models = [item.strip() for item in raw.split(",") if item.strip()]
    return models or ["gpt-image-2"]


def _default_image_generation_model(cfg: dict | None = None) -> str:
    """返回图片接口默认回填使用的模型（取配置列表首项）。"""
    return _image_supported_models_from_config(cfg)[0]


def _has_active_key_for_image_model(model: str) -> bool:
    """判断当前是否存在 active key 支持指定图片模型。"""
    target = str(model or "").strip()
    if not target:
        return False
    for key in list_keys():
        if key.get("status") != "active":
            continue
        if target in set(key_model_list(key)):
            return True
    return False


def _normalize_models_input(models: str) -> str:
    """规范模型输入，统一去除空白和多余逗号。"""
    raw = str(models or "").strip()
    if not raw:
        return "*"
    if raw == "*":
        return "*"
    return ",".join(m.strip() for m in raw.split(",") if m.strip())


def _parse_provider_model_ids(payload: dict) -> list[str]:
    """从 `/models` 响应中提取模型 id 列表。

    兼容常见 OpenAI 风格：
    - {"data": [{"id": "gpt-4"}, ...]}
    - {"data": ["gpt-4", ...]}
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    seen = set()
    result = []
    for item in data:
        if isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
        else:
            model_id = str(item or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        result.append(model_id)
    return result


async def _fetch_provider_models(provider: str, api_key: str, base_url: str | None, auth_header: str) -> list[str]:
    """同步拉取指定 key 对应提供商的 `/models` 列表。

    这里显式复用 Agent 的 URL 拼接和认证头逻辑，避免管理侧与转发链路
    出现两套不一致的 provider 配置解释方式。
    """
    key_for_request = {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "auth_header": auth_header,
    }
    agent = get_agent(provider)
    url = agent.resolve_url(key_for_request, "models")
    headers = agent.build_headers(key_for_request, "models")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        body = resp.text[:300].replace("\n", " ")
        raise ValueError(f"同步提供商模型列表失败: HTTP {resp.status_code} {body}".strip())
    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise ValueError("同步提供商模型列表失败: 响应不是合法 JSON") from exc
    model_ids = _parse_provider_model_ids(payload)
    if not model_ids:
        raise ValueError("同步提供商模型列表失败: /models 未返回可识别的模型列表")
    return model_ids


async def _resolve_key_save_payload(body: dict, existing: dict | None = None) -> dict:
    """解析并校验 key 保存参数，同时同步 provider 模型列表。"""
    alias = str(body.get("alias") or (existing or {}).get("alias") or "").strip()
    provider = str(body.get("provider") or (existing or {}).get("provider") or "").strip()
    api_key = str(body.get("api_key") or (existing or {}).get("api_key") or "").strip()
    base_url = body.get("base_url")
    if base_url is None:
        base_url = (existing or {}).get("base_url") or None
    else:
        base_url = str(base_url).strip() or None
    default_auth_header = get_agent(provider).default_auth_header if provider else "Bearer {api_key}"
    auth_header_raw = body.get("auth_header")
    if auth_header_raw is not None and str(auth_header_raw).strip():
        auth_header = str(auth_header_raw).strip()
    else:
        auth_header = str(default_auth_header or "Bearer {api_key}").strip() or "Bearer {api_key}"
    priority = int(body.get("priority", (existing or {}).get("priority", 0)) or 0)
    models = _normalize_models_input(body.get("models", (existing or {}).get("models", "*")))

    if not alias or not provider or not api_key:
        raise ValueError("alias、provider、api_key 为必填项")
    if models != "*" and any(part.strip() == "*" for part in models.split(",")):
        raise ValueError("星号不能和自定义模型同时使用")

    provider_models = await _fetch_provider_models(provider, api_key, base_url, auth_header)
    return {
        "alias": alias,
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "auth_header": auth_header,
        "priority": priority,
        "models": models,
        "provider_models": provider_models,
    }

def _render_template(name: str, **kwargs) -> str:
    """读取模板文件，替换 {{ var }} 占位符，支持 {% extends %}, {% include %}, {% block %}"""
    # 为静态资源提供默认版本参数，前端可用于 querystring 破缓存，避免替换 logo 后仍命中旧缓存。
    kwargs.setdefault("asset_version", __version__)
    # 为所有页面统一注入当前应用版本，供 header/about 等模板直接展示。
    kwargs.setdefault("version", __version__)
    _file_cache: dict[str, str] = {}

    def _load(path: str) -> str:
        if path not in _file_cache:
            full = os.path.join(_tpl_dir, path)
            with open(full, "r", encoding="utf-8") as f:
                _file_cache[path] = f.read()
        return _file_cache[path]

    def _resolve(tpl: str) -> str:
        content = _load(tpl)

        # 1. 处理 extends — 加载父模板原始内容再合并 block
        m = _re.search(r'\{%\s*extends\s+"([^"]+)"\s*%\}', content)
        if m:
            parent_raw = _load(m.group(1))
            # 先处理父模板中的 include
            def _inc(match):
                return _load(match.group(1))
            parent_raw = _re.sub(r'\{%\s*include\s+"([^"]+)"\s*%\}', _inc, parent_raw)

            # 收集子模板的所有 block
            blocks = {}
            for bm in _re.finditer(r'\{%\s*block\s+(\w+)\s*%\}(.*?)\{%\s*endblock\s*%\}', content, _re.DOTALL):
                blocks[bm.group(1)] = bm.group(2)

            # 替换父模板中的 block — 子模板有则替换，无则保留父模板内容
            def _replace_block(match):
                name = match.group(1)
                inner = match.group(2)
                if name in blocks:
                    return blocks[name]
                return inner
            content = _re.sub(
                r'\{%\s*block\s+(\w+)\s*%\}(.*?)\{%\s*endblock\s*%\}',
                _replace_block,
                parent_raw,
                flags=_re.DOTALL,
            )

        # 2. 处理 include（非 extends 情况下的 include）
        def _include(match):
            return _load(match.group(1))
        content = _re.sub(r'\{%\s*include\s+"([^"]+)"\s*%\}', _include, content)

        # 3. 清理残留的 block/endblock 标签
        content = _re.sub(r'\{%\s*block\s+\w+\s*%\}', '', content)
        content = _re.sub(r'\{%\s*endblock\s*%\}', '', content)

        # 4. 条件表达式
        def _cond(match):
            tv, var, val, fv = match.group(1), match.group(2), match.group(3), match.group(4)
            return tv if str(kwargs.get(var, '')) == val else fv
        content = _re.sub(
            r"\{\{\s*'([^']+)'\s+if\s+(\w+)\s*==\s*'([^']+)'\s+else\s+'([^']+)'\s*\}\}",
            _cond, content,
        )

        # 5. 变量替换
        content = _re.sub(
            r'\{\{\s*(\w+)\s*\}\}',
            lambda m: str(kwargs.get(m.group(1), '')),
            content,
        )

        return content

    return _resolve(name)


class _BoundedStreamCapture:
    """有界流式捕获器：保留头尾两段，中间超出部分用标记替代。

    设计目标：
    1. 避免长流把完整响应一直堆在内存里。
    2. 尽量保留 SSE 首尾信息，兼顾 usage 提取和事后排障。
    3. 当达到上限后继续消费流，但不再无限增长内存占用。
    """

    _TRUNCATED_TEMPLATE = "\n[... stream truncated by akm: omitted {omitted} bytes ...]\n"

    def __init__(self, max_bytes: int):
        self.max_bytes = max(1024, int(max_bytes or 262144))
        self._head_limit = max(512, self.max_bytes // 2)
        self._tail_limit = max(512, self.max_bytes - self._head_limit)
        self._head_parts: list[bytes] = []
        self._head_size = 0
        self._tail_parts: list[bytes] = []
        self._tail_size = 0
        self._total_seen = 0

    def append(self, chunk: bytes) -> None:
        """追加新分块：优先填充 head，超出后滚动维护 tail。"""
        data = chunk if isinstance(chunk, bytes) else bytes(chunk)
        if not data:
            return
        self._total_seen += len(data)

        remaining_head = self._head_limit - self._head_size
        if remaining_head > 0:
            head_piece = data[:remaining_head]
            if head_piece:
                self._head_parts.append(head_piece)
                self._head_size += len(head_piece)
            data = data[len(head_piece):]

        if not data:
            return

        self._tail_parts.append(data)
        self._tail_size += len(data)
        while self._tail_size > self._tail_limit and self._tail_parts:
            overflow = self._tail_size - self._tail_limit
            first = self._tail_parts[0]
            if len(first) <= overflow:
                self._tail_parts.pop(0)
                self._tail_size -= len(first)
            else:
                self._tail_parts[0] = first[overflow:]
                self._tail_size -= overflow

    def build_text(self) -> str:
        """生成供 token 提取和审计使用的截断文本。"""
        head = b"".join(self._head_parts)
        tail = b"".join(self._tail_parts)
        if self._total_seen <= self.max_bytes:
            return (head + tail).decode("utf-8", errors="replace")

        omitted = max(0, self._total_seen - len(head) - len(tail))
        marker = self._TRUNCATED_TEMPLATE.format(omitted=omitted).encode("utf-8")
        return (head + marker + tail).decode("utf-8", errors="replace")

    @property
    def truncated(self) -> bool:
        return self._total_seen > self.max_bytes


def _create_monitored_task(app: FastAPI, coro):
    """兼容旧逻辑的后台任务包装，当前仅保留给非审计类任务使用。"""
    monitor = getattr(app.state, "health_monitor", None)
    if monitor is not None:
        monitor.audit_task_started()

    async def _runner():
        ok = True
        try:
            await coro
        except Exception:
            ok = False
            raise
        finally:
            if monitor is not None:
                monitor.audit_task_finished(ok)

    return asyncio.create_task(_runner())


def _get_health_monitor(app: FastAPI) -> HealthMonitor | None:
    """统一获取健康监护实例，避免各链路散落 getattr 细节。"""
    monitor = getattr(app.state, "health_monitor", None)
    if isinstance(monitor, HealthMonitor):
        return monitor
    return None


def _runtime_memory_rss_bytes() -> int:
    """返回当前进程 RSS，按平台统一转换为字节。"""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss = int(getattr(usage, "ru_maxrss", 0) or 0)
    # macOS 返回字节，Linux 常见返回 KB；当前环境是 darwin，但这里保留兼容转换。
    if sys.platform.startswith("linux"):
        return rss * 1024
    return rss


def _count_open_fds() -> int | None:
    """尽量返回当前进程打开的 fd 数；当前平台不支持时返回 None。"""
    fd_dir = "/dev/fd"
    try:
        return len(os.listdir(fd_dir))
    except OSError:
        return None


def _build_runtime_debug_payload(app: FastAPI) -> dict:
    """构建轻量运行时快照，帮助复现“假死/卡死”时快速定责。"""
    monitor = _get_health_monitor(app)
    audit_queue = getattr(app.state, "audit_log_queue", None)
    http_client = getattr(app.state, "http_client", None)
    return {
        "process": {
            "pid": os.getpid(),
            "platform": sys.platform,
            "python_version": sys.version.split()[0],
            "rss_bytes": _runtime_memory_rss_bytes(),
            "thread_count": threading.active_count(),
            "gc_counts": list(gc.get_count()),
            "open_fds": _count_open_fds(),
        },
        "health": monitor.detail_payload() if monitor is not None else {"status": "unknown", "reasons": [], "metrics": {}},
        "audit_queue": {
            "enabled": audit_queue is not None,
            "size": audit_queue.qsize() if audit_queue is not None else 0,
            "dropped": getattr(audit_queue, "dropped_count", 0),
            "failures": getattr(audit_queue, "failure_count", 0),
            "last_error": getattr(audit_queue, "last_error", ""),
        },
        "http_client": {
            "configured": http_client is not None,
            "class": http_client.__class__.__name__ if http_client is not None else "",
        },
    }


async def _submit_audit_log(app: FastAPI, data: dict) -> bool:
    """将审计日志提交到有界队列，并同步刷新监护指标。"""
    audit_queue = getattr(app.state, "audit_log_queue", None)
    monitor = _get_health_monitor(app)
    if audit_queue is None:
        if monitor is not None:
            monitor.audit_task_started()
        ok = True
        try:
            await write_log_async(data)
        except Exception:
            ok = False
            raise
        finally:
            if monitor is not None:
                monitor.audit_task_finished(ok)
        return True

    accepted = await audit_queue.submit(data)
    if monitor is not None:
        monitor.set_audit_backlog(
            pending=audit_queue.qsize(),
            dropped=audit_queue.dropped_count,
            failures=audit_queue.failure_count,
        )
    return accepted


def _build_shared_http_client() -> httpx.AsyncClient:
    """统一构建共享 httpx 客户端，便于后续软重建时复用相同参数。"""
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    return httpx.AsyncClient(
        limits=limits,
        timeout=httpx.Timeout(120.0, connect=10.0),
    )


async def _recreate_shared_http_client(app: FastAPI, reason: str) -> bool:
    """软重建共享 http_client，并通过锁避免并发请求重复执行该操作。"""
    lock = getattr(app.state, "http_client_lock", None)
    monitor = _get_health_monitor(app)
    if lock is None:
        lock = asyncio.Lock()
        app.state.http_client_lock = lock
    async with lock:
        if monitor is not None and not monitor.should_recreate_http_client():
            return False
        old_client = getattr(app.state, "http_client", None)
        new_client = _build_shared_http_client()
        app.state.http_client = new_client
        if old_client is not None:
            try:
                await old_client.aclose()
            except Exception:
                pass
        if monitor is not None:
            monitor.record_http_client_recreated(reason)
        return True


@app.get("/health")
async def health():
    """健康检查"""
    monitor = getattr(app.state, "health_monitor", None)
    if monitor is None:
        return {"status": "ok"}
    return monitor.live_payload()


@app.get("/health/live")
async def health_live():
    """存活探针：只回答进程是否仍在提供 HTTP 服务。"""
    monitor = getattr(app.state, "health_monitor", None)
    if monitor is None:
        return {"status": "ok"}
    return monitor.live_payload()


@app.get("/health/ready")
async def health_ready():
    """就绪探针：回答当前是否适合继续接收新流量。"""
    monitor = getattr(app.state, "health_monitor", None)
    if monitor is None:
        return {"status": "healthy", "ready": True, "reasons": []}
    body, status_code = monitor.ready_payload()
    return JSONResponse(status_code=status_code, content=body)


@app.get("/health/detail")
async def health_detail():
    """详细探针：返回聚合状态和关键运行时指标。"""
    monitor = getattr(app.state, "health_monitor", None)
    if monitor is None:
        return {"status": "healthy", "reasons": [], "metrics": {}}
    return monitor.detail_payload()


@app.get("/debug/runtime")
async def debug_runtime():
    """运行时诊断快照：用于排查“服务假活着”“整服卡死”等问题。"""
    return _build_runtime_debug_payload(app)


@app.get("/debug/runtime/history")
async def debug_runtime_history(limit: int = Query(default=50, ge=1, le=200)):
    """最近运行时事件环形缓冲：用于复盘自愈、退化和失败链路。"""
    monitor = _get_health_monitor(app)
    if monitor is None:
        return {"total_buffered": 0, "limit": limit, "events": []}
    return monitor.recent_events_payload(limit=limit)


@app.get("/api/version")
async def api_version():
    """返回当前应用版本，供前端页面和外部脚本统一读取。"""
    return {"version": __version__}


@app.get("/favicon.ico")
@app.get("/logo.png")
async def favicon():
    """提供 logo 作为网页图标"""
    # 开发环境：项目根目录；打包环境：Resources 目录
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "logo.png"),
    ]
    if hasattr(sys, "frozen") or "Python" not in sys.executable:
        candidates.insert(0, os.path.join(os.path.dirname(sys.executable), "..", "Resources", "logo.png"))
    for path in candidates:
        if os.path.exists(path):
            return FileResponse(path, media_type="image/png")
    return Response(status_code=404)


# ── Key 管理 API ───────────────────────────────────────────

def _mask_key(api_key: str) -> str:
    """脱敏显示 API key，只保留前 8 位"""
    if not api_key:
        return ""
    return api_key[:8] + "..." if len(api_key) > 8 else api_key


def _sanitize_key_snapshot(key: dict | None) -> dict:
    """生成适合写入 keys.log 的 Key 审计快照，显式排除敏感字段。"""
    if not isinstance(key, dict):
        return {}
    return {
        "alias": str(key.get("alias") or ""),
        "provider": str(key.get("provider") or ""),
        "base_url": str(key.get("base_url") or ""),
        "models": str(key.get("models") or ""),
        "provider_models": key.get("provider_models") if isinstance(key.get("provider_models"), list) else [],
        "auth_header": str(key.get("auth_header") or ""),
        "priority": int(key.get("priority", 0) or 0),
        "status": str(key.get("status") or ""),
    }


def _write_key_change_log(event: str, alias: str, details: dict | None = None) -> None:
    """将 Key 配置/状态审计事件追加写入 ~/.akm/keys.log。"""
    payload = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "category": "key_audit",
        "scope": "configuration",
        "event": str(event or "unknown"),
        "alias": str(alias or ""),
        "details": details or {},
    }
    with open(get_keys_log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


@app.get("/api/keys")
async def api_list_keys():
    """列出所有 key（api_key 脱敏）"""
    keys = list_keys()
    from akm.db import get_connection
    conn = get_connection()
    latency_rows = conn.execute(
        """
        SELECT
            k.alias AS alias,
            (
                SELECT AVG(x.latency_ms)
                FROM (
                    SELECT latency_ms
                    FROM audit_logs
                    WHERE key_alias = k.alias AND status_code = 200 AND latency_ms > 0
                    ORDER BY id DESC
                    LIMIT 10
                ) AS x
            ) AS recent_avg_latency_ms,
            (
                SELECT COUNT(1)
                FROM (
                    SELECT 1
                    FROM audit_logs
                    WHERE key_alias = k.alias AND status_code = 200 AND latency_ms > 0
                    ORDER BY id DESC
                    LIMIT 10
                ) AS y
            ) AS recent_latency_sample_count
        FROM keys AS k
        """
    ).fetchall()
    conn.close()
    latency_map = {
        row["alias"]: {
            "recent_avg_latency_ms": round(float(row["recent_avg_latency_ms"]), 1)
            if row["recent_avg_latency_ms"] is not None else None,
            "recent_latency_sample_count": int(row["recent_latency_sample_count"] or 0),
        }
        for row in latency_rows
    }
    for k in keys:
        k["api_key"] = _mask_key(k["api_key"])
        latency = latency_map.get(k["alias"], {})
        k["recent_avg_latency_ms"] = latency.get("recent_avg_latency_ms")
        k["recent_latency_sample_count"] = latency.get("recent_latency_sample_count", 0)
    return {"data": keys}


@app.post("/api/keys")
async def api_add_key(request: Request):
    """添加一个新的 API key"""
    body = await request.json()

    try:
        payload = await _resolve_key_save_payload(body)
        add_key(
            alias=payload["alias"],
            provider=payload["provider"],
            api_key=payload["api_key"],
            base_url=payload["base_url"],
            models=payload["models"],
            provider_models=payload["provider_models"],
            auth_header=payload["auth_header"],
            priority=payload["priority"],
        )
        created = get_key(payload["alias"])
        _write_key_change_log(
            event="key.config.created",
            alias=payload["alias"],
            details={
                "after": _sanitize_key_snapshot(created),
                "api_key_updated": True,
            },
        )
        return {"ok": True, "alias": payload["alias"], "provider_models_count": len(payload["provider_models"])}
    except ValueError as e:
        message = str(e)
        status_code = 409 if "已存在" in message else 400
        return JSONResponse(status_code=status_code, content={"detail": message})


@app.put("/api/keys/{alias}")
async def api_update_key(alias: str, request: Request):
    """更新 key 的配置（api_key、priority、base_url、models、auth_header）"""
    existing = get_key(alias)
    if existing is None:
        return JSONResponse(status_code=404, content={"detail": f"Key '{alias}' 不存在"})

    body = await request.json()

    try:
        payload = await _resolve_key_save_payload(body, existing)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})

    before = _sanitize_key_snapshot(existing)

    set_provider(alias, payload["provider"])
    set_base_url(alias, payload["base_url"] or "")
    set_auth_header(alias, payload["auth_header"])
    set_models(alias, payload["models"])
    set_priority(alias, payload["priority"])
    set_provider_models(alias, payload["provider_models"])
    if "api_key" in body and body.get("api_key"):
        set_api_key(alias, payload["api_key"])

    after = get_key(alias)
    _write_key_change_log(
        event="key.config.updated",
        alias=alias,
        details={
            "before": before,
            "after": _sanitize_key_snapshot(after),
            "api_key_updated": bool("api_key" in body and body.get("api_key")),
        },
    )

    return {"ok": True, "alias": alias, "provider_models_count": len(payload["provider_models"])}


@app.patch("/api/keys/{alias}/status")
async def api_toggle_status(alias: str, request: Request):
    """切换 key 状态（active / disabled）"""
    existing = get_key(alias)
    if existing is None:
        return JSONResponse(status_code=404, content={"detail": f"Key '{alias}' 不存在"})

    body = await request.json()
    status = body.get("status", "active")
    if status not in ("active", "disabled"):
        return JSONResponse(status_code=400, content={"detail": "status 必须为 active 或 disabled"})

    before_status = existing.get("status", "")
    set_status(alias, status)
    _write_key_change_log(
        event="key.status.changed",
        alias=alias,
        details={
            "before_status": before_status,
            "after_status": status,
        },
    )
    return {"ok": True, "alias": alias, "status": status}


@app.delete("/api/keys/{alias}")
async def api_delete_key(alias: str):
    """删除指定 key"""
    existing = get_key(alias)
    if remove_key(alias):
        _write_key_change_log(
            event="key.config.deleted",
            alias=alias,
            details={
                "before": _sanitize_key_snapshot(existing),
            },
        )
        return {"ok": True, "alias": alias}
    return JSONResponse(status_code=404, content={"detail": f"Key '{alias}' 不存在"})


@app.post("/api/keys/{alias}/test")
async def api_test_key(alias: str):
    """测试 key 连通性"""
    key = get_key(alias)
    if key is None:
        return JSONResponse(status_code=404, content={"detail": f"Key '{alias}' 不存在"})
    result = await test_key_connectivity(key)
    return result


@app.get("/api/keys/export")
async def api_export_keys():
    """导出所有 Key 配置（含完整 api_key），用于备份迁移"""
    keys = list_keys()
    # 导出仅保留持久化字段，避免把 model_list 这类派生字段混入备份。
    full_keys = []
    for k in keys:
        full = get_key(k["alias"])
        if full:
            full.pop("model_list", None)
            full_keys.append(full)
    return {"data": full_keys}


@app.post("/api/keys/refresh-models")
async def api_refresh_key_provider_models():
    """批量刷新所有 key 的提供商模型列表。"""
    refreshed = 0
    failed = []
    for key in list_keys():
        try:
            before_models = list(key.get("provider_models") or [])
            provider_models = await _fetch_provider_models(
                key.get("provider", ""),
                key.get("api_key", ""),
                key.get("base_url") or None,
                key.get("auth_header") or "Bearer {api_key}",
            )
            set_provider_models(key["alias"], provider_models)
            _write_key_change_log(
                event="key.models.refresh_succeeded",
                alias=key["alias"],
                details={
                    "before_provider_models": before_models,
                    "after_provider_models": provider_models,
                    "before_count": len(before_models),
                    "after_count": len(provider_models),
                },
            )
            refreshed += 1
        except ValueError as exc:
            _write_key_change_log(
                event="key.models.refresh_failed",
                alias=key["alias"],
                details={
                    "error": str(exc),
                },
            )
            failed.append({
                "alias": key["alias"],
                "error": str(exc),
            })
    return {
        "ok": not failed,
        "refreshed": refreshed,
        "failed": failed,
    }


# ── 统计 API ───────────────────────────────────────────────

def _extract_tokens(response_body: str) -> dict | None:
    """从响应体中提取 token 用量，兼容 chat/completions 和 responses 格式"""
    if not response_body:
        return None

    def _parse_usage(usage: dict) -> dict | None:
        """从 usage 对象提取 token 数，兼容两种字段名和缓存 token"""
        total = usage.get("total_tokens", 0)
        prompt = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
        completion = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
        # 安全提取缓存 token：先查 Chat 格式的 *_details，再查 Messages 格式的直接 cached_tokens 字段
        cached = usage.get("cached_tokens", 0) or 0
        # Anthropic Messages: cache_read_input_tokens 表示缓存命中读取 token
        if not cached:
            cached = usage.get("cache_read_input_tokens", 0) or 0
        if not cached:
            for key in ("prompt_tokens_details", "input_tokens_details"):
                details = usage.get(key)
                if isinstance(details, dict):
                    cached = details.get("cached_tokens", 0)
                    if cached:
                        break
        if total == 0 and (prompt > 0 or completion > 0):
            total = prompt + completion
        if total > 0:
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "cached_tokens": cached,
                "cache_creation_tokens": cache_creation,
            }
        return None

    # 1. 尝试直接解析为 JSON
    try:
        data = json.loads(response_body)
        usage = data.get("usage", {})
        result = _parse_usage(usage)
        if result:
            return result
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. 尝试解析 SSE 流式响应（多行 data: {...} 格式）
    usage = None
    msg_input_tokens = 0
    msg_output_tokens = 0
    msg_cached_tokens = 0
    for line in response_body.split("\n"):
        line = line.strip()
        if line.startswith("data: ") and not line.startswith("data: [DONE]"):
            try:
                chunk = json.loads(line[6:])
                if "usage" in chunk:
                    usage = chunk["usage"]
                # responses SSE 格式: response.completed 事件中包含 usage
                if chunk.get("type") == "response.completed" and chunk.get("response", {}).get("usage"):
                    usage = chunk["response"]["usage"]

                # messages SSE: message_start.message.usage.input_tokens
                if chunk.get("type") == "message_start":
                    msg = chunk.get("message", {}) if isinstance(chunk.get("message"), dict) else {}
                    u = msg.get("usage", {}) if isinstance(msg.get("usage"), dict) else {}
                    msg_input_tokens = u.get("input_tokens", msg_input_tokens) or msg_input_tokens

                # messages SSE: message_delta.usage.output_tokens/cached_tokens
                if chunk.get("type") == "message_delta":
                    u = chunk.get("usage", {}) if isinstance(chunk.get("usage"), dict) else {}
                    msg_output_tokens = u.get("output_tokens", msg_output_tokens) or msg_output_tokens
                    msg_cached_tokens = u.get("cached_tokens", msg_cached_tokens) or msg_cached_tokens
            except (json.JSONDecodeError, TypeError):
                pass
    if usage:
        parsed = _parse_usage(usage)
        if parsed:
            # messages SSE 常见情况：message_delta.usage 只有 output_tokens，
            # input_tokens 需要从 message_start.message.usage 补齐。
            if parsed.get("prompt_tokens", 0) == 0 and msg_input_tokens > 0:
                parsed["prompt_tokens"] = msg_input_tokens
                parsed["total_tokens"] = parsed.get("prompt_tokens", 0) + parsed.get("completion_tokens", 0)
                if msg_cached_tokens > 0:
                    parsed["cached_tokens"] = msg_cached_tokens
            return parsed

    # messages SSE 兜底：即便上游没给标准 usage，也尽量从 message_start/message_delta 拼回 token 信息
    if msg_input_tokens > 0 or msg_output_tokens > 0:
        total = msg_input_tokens + msg_output_tokens
        return {
            "prompt_tokens": msg_input_tokens,
            "completion_tokens": msg_output_tokens,
            "total_tokens": total,
            "cached_tokens": msg_cached_tokens,
        }

    return None


def _estimate_tokens_light(request_body: dict, response_body: str = "") -> dict:
    """轻量 token 估算兜底（仅在真实 usage 缺失时使用）。

    说明：
    - 这是近似估算，不用于计费，仅用于日志可观测性避免全 0。
    - 估算策略尽量保守：按字符长度折算，英文/JSON 取约 4 字符/Token。
    """
    try:
        req_len = len(json.dumps(request_body or {}, ensure_ascii=False))
    except Exception:
        req_len = len(str(request_body or ""))

    prompt_tokens = max(1, req_len // 4) if req_len > 0 else 0

    resp_len = len(response_body or "")
    # 响应中包含大量 SSE 包装字段，做更保守折算，避免估算过高
    completion_tokens = max(0, resp_len // 8)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cached_tokens": 0,
        "cache_creation_tokens": 0,
    }


async def _try_count_tokens_fallback(
    request: Request,
    request_body: dict,
    api_path: str,
    key_alias: str,
    provider: str,
) -> dict | None:
    """在 messages+anthropic 场景尝试用 /messages/count_tokens 回填输入 token。"""
    if api_path != "messages" or provider != "anthropic" or not key_alias:
        return None
    if not isinstance(request_body.get("messages"), list):
        return None

    key = get_key(key_alias)
    if not key:
        return None

    agent = get_agent(key.get("provider", "anthropic"))
    url = agent.resolve_url(key, "messages/count_tokens")
    headers = agent.build_headers(key, "messages")

    count_body = {
        "model": request_body.get("model", ""),
        "messages": request_body.get("messages", []),
    }
    if "system" in request_body:
        count_body["system"] = request_body.get("system")
    if "tools" in request_body:
        count_body["tools"] = request_body.get("tools")

    try:
        resp = await request.app.state.http_client.post(url, json=count_body, headers=headers, timeout=20)
        if resp.status_code != 200:
            return None
        data = resp.json() if resp.content else {}
        input_tokens = int(data.get("input_tokens", 0) or 0)
        if input_tokens <= 0:
            return None
        return {
            "prompt_tokens": input_tokens,
            "completion_tokens": 0,
            "total_tokens": input_tokens,
            "cached_tokens": 0,
            "cache_creation_tokens": 0,
        }
    except Exception:
        return None


async def _build_usage_metrics(
    request: Request,
    request_body: dict,
    response_body: str,
    api_path: str,
    key_alias: str,
    provider: str,
    adapter,
) -> tuple[dict, list[str]]:
    """统一 usage 构建器：标准解析 -> CountTokens -> Adapter -> 轻量估算。"""
    flags: list[str] = []
    tokens = _extract_tokens(response_body) or {}

    has_tokens = (tokens.get("prompt_tokens", 0) > 0 or tokens.get("completion_tokens", 0) > 0)
    if not has_tokens:
        ct = await _try_count_tokens_fallback(request, request_body, api_path, key_alias, provider)
        if ct:
            tokens = ct
            flags.append(FLAG_COUNT_TOKENS_FALLBACK)
            has_tokens = True

    if not has_tokens and adapter:
        last_usage = getattr(adapter, "_last_usage_tokens", None)
        if isinstance(last_usage, dict):
            p = int(last_usage.get("prompt_tokens", 0) or 0)
            c = int(last_usage.get("completion_tokens", 0) or 0)
            t = int(last_usage.get("total_tokens", 0) or (p + c))
            cached = int(last_usage.get("cached_tokens", 0) or 0)
            if p > 0 or c > 0:
                tokens = {
                    "prompt_tokens": p,
                    "completion_tokens": c,
                    "total_tokens": t,
                    "cached_tokens": cached,
                    "cache_creation_tokens": int(last_usage.get("cache_creation_tokens", 0) or 0),
                }
                flags.append(FLAG_USAGE_FALLBACK_ADAPTER)
                has_tokens = True

    if not has_tokens:
        tokens = _estimate_tokens_light(request_body, response_body)
        flags.append(FLAG_USAGE_ESTIMATED_LIGHT)

    return tokens, flags


# ── 统计内存缓存（30 秒过期，减少重复解析）──
_stats_cache: dict[str, tuple[float, dict]] = {}

@app.get("/api/stats")
async def api_stats(days: int = Query(default=1, ge=1, le=365)):
    """Token 统计概览，可按天数筛选（30 秒内存缓存）"""
    return await asyncio.to_thread(_get_stats, days)


def _get_stats(days: int) -> dict:
    """带缓存的统计查询"""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    cfg = load_config()
    include_estimated_usage = bool(cfg.get("stats_include_estimated_usage", False))
    cache_key = f"days={days}|estimated={1 if include_estimated_usage else 0}"
    now = time.time()
    if cache_key in _stats_cache:
        ts, data = _stats_cache[cache_key]
        if now - ts < 60:
            result = dict(data)
            result["cached_at"] = datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")
            return result

    from akm.db import get_connection
    conn = get_connection()
    # 自然日范围：1=今天，7=最近7个自然日（含今天），30 同理。
    day_offset = 1 - days
    rows = conn.execute(
        """SELECT prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                  provider, model, key_alias, timestamp, response_body, request_headers, status_code
           FROM audit_logs
           WHERE timestamp >= datetime(date('now', 'localtime', ? || ' days'))
           ORDER BY id DESC""",
        (str(day_offset),),
    ).fetchall()
    conn.close()

    total_requests = 0
    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    total_cached = 0
    by_provider = {}
    by_model = {}
    by_key = {}
    daily = {}

    for row in rows:
        r = dict(row)
        key_alias = str(r.get("key_alias") or "").strip()
        # 首页统计只关注真正落到某个 key 上的请求。
        # 没有 key_alias 的记录通常是选 key 失败或前置报错，不应混入总量与分组统计。
        if not key_alias:
            continue
        status_code = int(r.get("status_code", 0) or 0)
        p = r.get("prompt_tokens", 0) or 0
        c = r.get("completion_tokens", 0) or 0
        t = r.get("total_tokens", 0) or 0
        cached = r.get("cached_tokens", 0) or 0
        ignore_estimated_tokens = False
        try:
            headers_obj = json.loads(r.get("request_headers") or "{}")
            flags_raw = str(headers_obj.get("x-akm-flags") or "")
            flags = {x.strip() for x in flags_raw.split(",") if x.strip()}
            ignore_estimated_tokens = (not include_estimated_usage) and (FLAG_USAGE_ESTIMATED_LIGHT in flags)
        except (json.JSONDecodeError, TypeError):
            ignore_estimated_tokens = False
        # 兼容旧数据：列值为 0 但有 response_body 时，仍从 body 提取
        if not ignore_estimated_tokens and not t and r.get("response_body"):
            tokens = _extract_tokens(r["response_body"])
            if tokens:
                p = tokens.get("prompt_tokens", 0) or p
                c = tokens.get("completion_tokens", 0) or c
                t = tokens.get("total_tokens", 0) or t
                cached = tokens.get("cached_tokens", 0) or cached
        if ignore_estimated_tokens:
            p = 0
            c = 0
            t = 0
            cached = 0

        # 首页 requests 口径与 token 口径保持一致：
        # 1. 始终排除失败请求；
        # 2. 当隐藏 estimated token 时，同时排除 estimated 请求，避免“请求数算了、token 没算”带来的错觉。
        include_request = (200 <= status_code < 300) and (not ignore_estimated_tokens)
        if not include_request:
            continue

        total_requests += 1

        provider = r.get("provider", "unknown")
        model = r.get("model", "unknown")
        ts = str(r.get("timestamp", ""))[:10]

        total_prompt += p - cached
        total_completion += c
        total_tokens += t
        total_cached += cached

        # 按供应商
        if provider not in by_provider:
            by_provider[provider] = {"prompt": 0, "completion": 0, "total": 0, "cached": 0, "requests": 0}
        by_provider[provider]["prompt"] += p - cached
        by_provider[provider]["completion"] += c
        by_provider[provider]["total"] += t
        by_provider[provider]["cached"] += cached
        by_provider[provider]["requests"] += 1

        # 按模型
        if model not in by_model:
            by_model[model] = {"prompt": 0, "completion": 0, "total": 0, "cached": 0, "requests": 0}
        by_model[model]["prompt"] += p - cached
        by_model[model]["completion"] += c
        by_model[model]["total"] += t
        by_model[model]["cached"] += cached
        by_model[model]["requests"] += 1

        # 按 key
        if key_alias not in by_key:
            by_key[key_alias] = {"prompt": 0, "completion": 0, "total": 0, "cached": 0, "requests": 0}
        by_key[key_alias]["prompt"] += p - cached
        by_key[key_alias]["completion"] += c
        by_key[key_alias]["total"] += t
        by_key[key_alias]["cached"] += cached
        by_key[key_alias]["requests"] += 1

        # 按天
        if ts not in daily:
            daily[ts] = {"prompt": 0, "completion": 0, "total": 0, "cached": 0, "requests": 0}
        daily[ts]["prompt"] += p - cached
        daily[ts]["completion"] += c
        daily[ts]["total"] += t
        daily[ts]["cached"] += cached
        daily[ts]["requests"] += 1

    result = {
        "total_requests": total_requests,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "total_cached_tokens": total_cached,
        "by_provider": by_provider,
        "by_model": by_model,
        "by_key": by_key,
        "daily": dict(sorted(daily.items())),
    }
    now_ts = time.time()
    result["cached_at"] = datetime.fromtimestamp(now_ts, tz).strftime("%Y-%m-%d %H:%M:%S")
    _stats_cache[cache_key] = (now_ts, result)
    return result


@app.get("/api/logs")
async def api_logs(
    limit: int = Query(default=12, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    provider: str = Query(default=None),
    order: str = Query(default="DESC"),
    hide_empty: bool = Query(default=False),
    hide_est: bool = Query(default=False),
    status: str = Query(default="all"),
    key_alias: str = Query(default=""),
    days: int = Query(default=0, ge=0, le=365),
):
    """查询审计日志 API，支持分页、排序、过滤空记录、状态筛选、Key筛选和时间范围，返回 JSON"""
    logs = await list_logs_async(provider=provider, limit=limit, offset=offset, order=order, hide_empty=hide_empty, hide_est=hide_est, status=status, key_alias=key_alias, days=days)
    total = await count_logs_async(provider=provider, hide_empty=hide_empty, hide_est=hide_est, status=status, key_alias=key_alias, days=days)
    # 为每条日志附加 token 用量信息（优先读列，兼容旧数据才解析 body）
    for log in logs:
        p = log.get("prompt_tokens", 0) or 0
        c = log.get("completion_tokens", 0) or 0
        t = log.get("total_tokens", 0) or 0
        cached = log.get("cached_tokens", 0) or 0
        cache_creation = log.get("cache_creation_tokens", 0) or 0
        if not t and log.get("response_body"):
            tokens = _extract_tokens(log["response_body"])
            if tokens:
                p = tokens.get("prompt_tokens", 0) or p
                c = tokens.get("completion_tokens", 0) or c
                t = tokens.get("total_tokens", 0) or t
                cached = tokens.get("cached_tokens", 0) or cached
                cache_creation = tokens.get("cache_creation_tokens", 0) or cache_creation
        log["prompt_tokens"] = p
        log["completion_tokens"] = c
        log["total_tokens"] = t
        log["cached_tokens"] = cached
        log["cache_creation_tokens"] = cache_creation

        # 补充转换告警派生字段，前端可直接展示可读文本，避免重复解析逻辑
        conv_codes: list[str] = []
        conv_labels: list[str] = []
        try:
            headers_obj = json.loads(log.get("request_headers") or "{}")
            raw = headers_obj.get("x-akm-conv-warnings", "")
            if isinstance(raw, str) and raw.strip():
                conv_codes = [x.strip() for x in raw.split(",") if x.strip()]
                conv_labels = [_CONV_WARNING_LABELS.get(code, code) for code in conv_codes]
        except (json.JSONDecodeError, TypeError):
            conv_codes = []
            conv_labels = []
        log["conv_warning_codes"] = conv_codes
        log["conv_warning_labels"] = conv_labels
    return {"data": logs, "total": total}


@app.get("/api/logs/size")
async def api_logs_size():
    """返回本地缓存占用（数据库 + WAL/SHM + .log 文件）大小。"""
    from akm.db import DB_DIR, get_db_path

    db_path = get_db_path()
    db_files = [db_path, f"{db_path}-wal", f"{db_path}-shm"]
    db_size = 0
    for path in db_files:
        try:
            db_size += os.path.getsize(path)
        except OSError:
            pass

    log_size = 0
    try:
        for entry in os.listdir(DB_DIR):
            if not entry.endswith(".log"):
                continue
            path = os.path.join(DB_DIR, entry)
            if not os.path.isfile(path):
                continue
            try:
                log_size += os.path.getsize(path)
            except OSError:
                pass
    except OSError:
        pass

    return {
        "size": db_size + log_size,
        "cache_size": db_size + log_size,
        "db_size": db_size,
        "log_size": log_size,
    }


@app.post("/api/logs/clean")
async def api_clean_logs(request: Request):
    """清空审计日志 API"""
    from datetime import datetime as _dt
    body = await request.json()
    if body.get("all") is True:
        before = "2999-01-01"
    else:
        before = body.get("before", _dt.now().strftime("%Y-%m-%d"))
    try:
        count = await clean_logs_async(before)
        return {"ok": True, "deleted": count}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})


@app.post("/api/logs/clean-bodies")
async def api_clean_log_bodies():
    """清空审计日志请求体/响应体内容，保留统计字段与元数据"""
    count = await clean_log_bodies_async()
    return {"ok": True, "updated": count}


@app.get("/api/config")
async def api_get_config():
    """获取配置"""
    return load_config()


@app.post("/api/config")
async def api_save_config(request: Request):
    """保存配置"""
    body = await request.json()
    save_config(body)
    return {"ok": True}


@app.get("/api/agents")
async def api_list_agents():
    """列出所有 Agent（内置 + 自定义）"""
    return {"data": list_agents()}


@app.post("/api/agents")
async def api_add_agent(request: Request):
    """添加自定义 Agent"""
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"detail": "供应商名称不能为空"})
    try:
        register_agent(
            name=name,
            default_base_url=body.get("default_base_url", ""),
            default_auth_header=body.get("default_auth_header", "Bearer {api_key}"),
            supports_responses=body.get("supports_responses", False),
            supports_chat=body.get("supports_chat", True),
            supports_messages=body.get("supports_messages", False),
            messages_use_anthropic_path=body.get("messages_use_anthropic_path", False),
        )
        return {"ok": True, "name": name}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})


@app.delete("/api/agents/{name}")
async def api_delete_agent(name: str):
    """删除自定义 Agent"""
    try:
        unregister_agent(name)
        return {"ok": True}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})


# ── 插件管理 API ─────────────────────────────────────────────

@app.get("/api/plugins")
async def list_plugins(request: Request):
    """返回插件列表（含启用/禁用状态）"""
    pm = request.app.state.plugin_manager
    return pm.get_plugin_list()


@app.post("/api/plugins/upload")
async def upload_plugin(file: UploadFile, request: Request):
    """上传 .zip 插件包，解压到 ~/.akm/plugins/"""
    pm = request.app.state.plugin_manager
    result = await pm.install_plugin(file)
    if result.get("ok"):
        return result
    return JSONResponse(result, status_code=400)


@app.post("/api/plugins/{name}/enable")
async def enable_plugin(name: str, request: Request):
    """启用插件"""
    pm = request.app.state.plugin_manager
    result = pm.toggle_plugin(name, True)
    if result.get("ok"):
        return result
    return JSONResponse(result, status_code=400)


@app.post("/api/plugins/{name}/disable")
async def disable_plugin(name: str, request: Request):
    """禁用插件"""
    pm = request.app.state.plugin_manager
    result = pm.toggle_plugin(name, False)
    if result.get("ok"):
        return result
    return JSONResponse(result, status_code=400)


@app.delete("/api/plugins/{name}")
async def delete_plugin(name: str, request: Request):
    """删除第三方插件"""
    pm = request.app.state.plugin_manager
    result = pm.delete_plugin(name)
    if result.get("ok"):
        return result
    return JSONResponse(result, status_code=400)


@app.get("/api/plugin-menu")
async def plugin_menu(request: Request):
    """插件菜单（供侧边栏动态注入）"""
    return request.app.state.plugin_manager.get_menu()


@app.get("/api/plugin-config/{name}")
async def plugin_get_config(name: str, request: Request):
    """读取插件配置"""
    pm = request.app.state.plugin_manager
    cfg = pm.get_config(name)
    if cfg is None:
        return JSONResponse({"error": "插件不存在"}, status_code=404)
    return cfg


@app.post("/api/plugin-config/{name}")
async def plugin_save_config(name: str, request: Request):
    """保存插件配置"""
    pm = request.app.state.plugin_manager
    body = await request.json()
    return pm.set_config(name, body)


@app.get("/logs")
async def log_viewer(request: Request):
    """审计日志查看页面"""
    return HTMLResponse(_render_template("logs.html", title="审计", active="logs"))


@app.get("/keys")
async def keys_page(request: Request):
    """Key 管理页面"""
    return HTMLResponse(_render_template("keys.html", title="Key管理", active="keys"))


@app.get("/settings")
async def settings_page(request: Request):
    """设置页面"""
    return HTMLResponse(_render_template("settings.html", title="设置", active="settings"))


@app.get("/about")
async def about_page(request: Request):
    """关于页面"""
    return HTMLResponse(_render_template("about.html", title="关于", active="about", version=__version__))


@app.get("/admin")
async def admin_page(request: Request):
    """统计页面"""
    return HTMLResponse(_render_template("dashboard.html", title="统计", active="admin"))


@app.get("/plugins")
async def plugins_page(request: Request):
    """插件管理页面"""
    return HTMLResponse(_render_template("plugins.html", title="插件管理", active="plugins"))


@app.get("/plugins/{name}")
async def plugin_view(name: str, request: Request):
    """插件前端页面 — 返回插件的 views/index.html"""
    pm = request.app.state.plugin_manager
    plugin = pm.plugins.get(name)
    if not plugin or not plugin.enabled:
        return JSONResponse({"error": "插件不存在或未启用"}, status_code=404)
    index_path = plugin._static_dir / "index.html"
    if not index_path.exists():
        return JSONResponse({"error": "该插件无前端界面"}, status_code=404)
    return FileResponse(str(index_path))


@app.get("/v1/models")
@app.get("/models")
async def list_models():
    """返回所有 active key 支持的模型列表（OpenAI 兼容格式）"""
    data = []
    seen = set()
    for k in list_keys():
        if k["status"] != "active":
            continue
        candidates = key_model_list(k)
        for m in candidates:
            m = m.strip()
            if m and m not in seen:
                seen.add(m)
                data.append({
                    "id": m,
                    "object": "model",
                    "owned_by": k["provider"],
                })
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 兼容的 chat completions 端点"""
    return await _handle_ai_request(request, "chat/completions")


@app.post("/v1/messages")
@app.post("/messages")
async def messages(request: Request):
    """Anthropic Messages API 兼容端点"""
    return await _handle_ai_request(request, "messages")


@app.post("/v1/responses")
@app.post("/responses")
async def responses(request: Request):
    """OpenAI Responses API 端点"""
    return await _handle_ai_request(request, "responses")


@app.post("/v1/embeddings")
@app.post("/embeddings")
async def embeddings(request: Request):
    """OpenAI Embeddings API 端点。"""
    return await _handle_ai_request(request, "embeddings")


@app.post("/v1/images/generations")
@app.post("/images/generations")
async def image_generations(request: Request):
    """OpenAI Images Generations API 端点。

    当前按纯透传处理：
    1. 不参与协议转换；
    2. 不走流式链路；
    3. 直接把上游 JSON 原样返回给客户端。
    """
    logger.info(
        "[images/generations] route hit content-type=%s ua=%s",
        request.headers.get("content-type", ""),
        request.headers.get("user-agent", ""),
    )
    return await _handle_ai_request(request, "images/generations")


@app.post("/v1/images/edits")
@app.post("/images/edits")
async def image_edits(request: Request):
    """OpenAI Images Edits API 端点。

    当前按 multipart 纯透传处理：
    1. 接收 `multipart/form-data`；
    2. 将图片文件与普通表单字段原样转发给上游；
    3. 不参与协议转换。
    """
    return await _handle_ai_request(request, "images/edits")


async def _handle_ai_request(request: Request, api_path: str):
    """通用 AI API 请求处理：chat/completions / messages / responses / embeddings / images/generations / images/edits 复用"""
    monitor = _get_health_monitor(request.app)
    if monitor is not None:
        monitor.request_started()
    content_type = request.headers.get("Content-Type", "")
    try:
        cfg = load_config()
        image_models = _image_supported_models_from_config(cfg)
        default_image_model = image_models[0]

        if api_path == "images/edits":
            if "multipart/form-data" not in content_type:
                return JSONResponse(
                    status_code=415,
                    content={"detail": "不支持的 Content-Type，需要 multipart/form-data"},
                )
            form = await request.form()
            fields: dict[str, str] = {}
            files: dict[str, tuple[str, bytes, str]] = {}
            body = {"__akm_multipart__": True, "__akm_form_fields__": fields, "__akm_form_files__": files}
            for key, value in form.multi_items():
                if isinstance(value, (UploadFile, StarletteUploadFile)):
                    content = await value.read()
                    files[key] = (
                        value.filename or "upload.bin",
                        content,
                        value.content_type or "application/octet-stream",
                    )
                else:
                    fields[key] = str(value)
            if not str(fields.get("model") or "").strip():
                if not _has_active_key_for_image_model(default_image_model):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "detail": (
                                f"未显式提供 model，且当前没有 active key 支持默认图片模型 "
                                f"'{default_image_model}'。请手动传 model，或为某个 key 同步以下任一模型：{', '.join(image_models)}。"
                            )
                        },
                    )
                fields["model"] = default_image_model
            body["model"] = str(fields.get("model") or "")
        else:
            if "application/json" not in content_type:
                return JSONResponse(
                    status_code=415,
                    content={"detail": "不支持的 Content-Type，需要 application/json"},
                )

            body = await request.json()
        if api_path == "images/generations" and not str(body.get("model") or "").strip():
            # 图片生成目前仅接了纯透传链路，这里仅在缺省时回填默认模型。
            # 但默认模型必须先确认“当前确实有 active key 支持它”，否则直接给出可读错误，
            # 避免请求继续下游后变成更难判断的 503/404/上游模型不存在错误。
            if not _has_active_key_for_image_model(default_image_model):
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": (
                            f"未显式提供 model，且当前没有 active key 支持默认图片模型 "
                            f"'{default_image_model}'。请手动传 model，或为某个 key 同步以下任一模型：{', '.join(image_models)}。"
                        )
                    },
                )
            body["model"] = default_image_model
        # ── 提取关键请求头用于溯源（User-Agent 区分 opencode/codex/curl）──
        # starlette 的 headers 是大小写不敏感的 MutableHeaders
        _trace_headers, request_headers_json = _build_trace_headers(request)
        # 读取日志存储配置
        save_request_body = cfg.get("log_request_body", False)
        save_response_body = cfg.get("log_response_body", False)
        stream_capture_max_bytes = int(cfg.get("stream_capture_max_bytes", 262144) or 262144)
        result = await forward_request(body, request.app.state.http_client, api_path=api_path, plugin_manager=request.app.state.plugin_manager)
        request_body_for_log = str(result.get("request_body_for_log", "") or "")

        # ── 503 无可用 key 时补充来源信息 ──
        if result["status_code"] == 503:
            ua = (_trace_headers.get("user-agent") or "").lower()
            source = ""
            if "opencode" in ua:
                source = "opencode"
            elif "claude-cli" in ua or "claude code" in ua:
                source = "claude"
            elif "codex" in ua:
                source = "codex"
            elif "cursor" in ua:
                source = "cursor"
            elif "curl" in ua:
                source = "curl"
            if source:
                result["error"] = f"[{source}] {result['error']}"

        if monitor is not None:
            if int(result.get("status_code", 0) or 0) == 200:
                monitor.record_upstream_success()
            elif int(result.get("status_code", 0) or 0) >= 500 or result.get("error"):
                monitor.record_upstream_failure(result.get("error", ""))
                if monitor.should_recreate_http_client():
                    await _recreate_shared_http_client(
                        request.app,
                        reason=result.get("error", "upstream_failures_high"),
                    )

        # ── 流式响应：逐块转发，边收边发 ──
        if result.get("stream"):
            resp = result["response"]
            adapter = result.get("adapter")
            key_alias = result["key_alias"]
            provider = result["provider"]
            model = result["model"]

            if monitor is not None:
                monitor.stream_started()

            async def stream_generator():
                capture = _BoundedStreamCapture(stream_capture_max_bytes)
                t0 = __import__("time").time()
                stream_error = ""
                security_action = ""
                security_reason = ""
                security_changed = False

                async def _emit_stream_response_meta(status: int, latency: int, body_str: str):
                    """在流式请求真正结束时统一触发 on_response 生命周期。"""
                    current_pm = getattr(request.app.state, "plugin_manager", None)
                    if not current_pm:
                        return {"status_code": status, "response_body": body_str}
                    meta = {
                        "ok": status == 200,
                        "phase": "upstream",
                        "status_code": status,
                        "key_alias": key_alias,
                        "provider": provider,
                        "model": model,
                        "latency_ms": latency,
                        "error": stream_error,
                        "api_path": api_path,
                        "stream": True,
                        "response_body": body_str,
                    }
                    if security_action:
                        meta["security_action"] = security_action
                    if security_reason:
                        meta["security_reason"] = security_reason
                    try:
                        hook_result = await current_pm.run_hook("on_response", request=body, response=meta)
                        if isinstance(hook_result, dict) and isinstance(hook_result.get("response"), dict):
                            return hook_result["response"]
                    except Exception:
                        pass
                    return meta

                try:
                    if adapter:
                        async for line in adapter.convert_sse_stream(resp.aiter_bytes()):
                            chunk = line.encode("utf-8") if isinstance(line, str) else line
                            capture.append(chunk)
                            yield chunk
                    else:
                        async for chunk in resp.aiter_bytes():
                            capture.append(chunk)
                            yield chunk
                except Exception as exc:
                    stream_error = f"上游连接中断: {exc}"
                    logger.warning(f"[{key_alias}] {provider} model={model} → {stream_error}")
                finally:
                    await resp.aclose()
                    if monitor is not None:
                        monitor.stream_finished()
                    latency = int((__import__("time").time() - t0) * 1000)
                    body_str = capture.build_text()
                    status = 200 if not stream_error else 502
                    response_meta = await _emit_stream_response_meta(status, latency, body_str)
                    if isinstance(response_meta, dict):
                        status = int(response_meta.get("status_code", status) or status)
                        body_str = str(response_meta.get("response_body", body_str) or "")
                        security_action_from_meta = str(response_meta.get("security_action", security_action) or "")
                        security_reason_from_meta = str(response_meta.get("security_reason", security_reason) or "")
                        if security_action_from_meta:
                            security_action = security_action_from_meta
                        if security_reason_from_meta:
                            security_reason = security_reason_from_meta
                        stream_error = str(response_meta.get("error", stream_error) or "")
                    tokens, usage_flags = await _build_usage_metrics(
                        request=request,
                        request_body=body,
                        response_body=body_str,
                        api_path=api_path,
                        key_alias=key_alias,
                        provider=provider,
                        adapter=adapter,
                    )
                    request_headers_for_log = request_headers_json
                    try:
                        headers_obj = json.loads(request_headers_json) if request_headers_json else {}
                        flags = []
                        if adapter and getattr(adapter, "_fallback_thinking_to_text", False):
                            flags.append("fallback_thinking_to_text")
                        if status == 200 and not stream_error:
                            if tokens.get("prompt_tokens", 0) == 0 and tokens.get("completion_tokens", 0) == 0:
                                flags.append(FLAG_MISSING_USAGE_UPSTREAM)
                        if capture.truncated:
                            flags.append("stream_capture_truncated")
                        flags.extend(usage_flags)
                        if adapter and getattr(adapter, "_tool_trace_events", None):
                            if any("loop_guard_drop" in x for x in getattr(adapter, "_tool_trace_events", [])):
                                flags.append(FLAG_LOOP_GUARD_TRIGGERED)
                        tool_trace = ""
                        if adapter and getattr(adapter, "_tool_trace_events", None):
                            tool_trace = "; ".join(getattr(adapter, "_tool_trace_events", []))
                            if tool_trace:
                                headers_obj["x-akm-tool-trace"] = tool_trace[:2000]
                        if adapter and getattr(adapter, "_conversion_warnings", None):
                            conv_warn = ",".join(getattr(adapter, "_conversion_warnings", []))
                            if conv_warn:
                                headers_obj["x-akm-conv-warnings"] = conv_warn[:2000]
                        if security_action:
                            headers_obj["x-akm-security"] = f"{security_action}:{security_reason}"[:2000]
                        if security_changed and security_action not in ("", "warn"):
                            flags.append("security_response_rewritten")
                        elif security_action == "warn":
                            flags.append("security_response_warned")
                        if flags:
                            headers_obj["x-akm-flags"] = ",".join(flags)
                            request_headers_for_log = json.dumps(headers_obj, ensure_ascii=False)
                        elif tool_trace or security_action:
                            request_headers_for_log = json.dumps(headers_obj, ensure_ascii=False)
                        elif adapter and getattr(adapter, "_conversion_warnings", None):
                            request_headers_for_log = json.dumps(headers_obj, ensure_ascii=False)
                    except Exception:
                        request_headers_for_log = request_headers_json
                    await _submit_audit_log(request.app, {
                        "provider": provider,
                        "key_alias": key_alias,
                        "model": model,
                        "request_body": request_body_for_log if (save_request_body and request_body_for_log) else (_safe_request_body_for_log(body) if save_request_body else ""),
                        "response_body": body_str if save_response_body else "",
                        "status_code": status,
                        "latency_ms": latency,
                        "error": stream_error or (f"{security_action}:{security_reason}" if security_action else ""),
                        "request_headers": request_headers_for_log,
                        "prompt_tokens": tokens.get("prompt_tokens", 0),
                        "completion_tokens": tokens.get("completion_tokens", 0),
                        "total_tokens": tokens.get("total_tokens", 0),
                        "cached_tokens": tokens.get("cached_tokens", 0),
                        "cache_creation_tokens": tokens.get("cache_creation_tokens", 0),
                    })
                    logger.info(f"[{key_alias}] {provider} model={model} → {status} {latency}ms (stream)")

            return StreamingResponse(
                stream_generator(),
                status_code=200,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # ── 非流式响应 ──
        tokens, usage_flags = await _build_usage_metrics(
            request=request,
            request_body=body,
            response_body=result.get("body", ""),
            api_path=api_path,
            key_alias=result.get("key_alias", ""),
            provider=result.get("provider", ""),
            adapter=result.get("adapter"),
        )
        request_headers_for_log = request_headers_json
        try:
            headers_obj = json.loads(request_headers_json) if request_headers_json else {}
            adapter_for_log = result.get("adapter")
            if adapter_for_log and getattr(adapter_for_log, "_conversion_warnings", None):
                conv_warn = ",".join(getattr(adapter_for_log, "_conversion_warnings", []))
                if conv_warn:
                    headers_obj["x-akm-conv-warnings"] = conv_warn[:2000]
                    request_headers_for_log = json.dumps(headers_obj, ensure_ascii=False)
            if usage_flags:
                prev = headers_obj.get("x-akm-flags", "")
                merged = [x.strip() for x in (prev.split(",") if prev else []) if x.strip()]
                if result.get("security_action") == "warn":
                    if "security_response_warned" not in merged:
                        merged.append("security_response_warned")
                elif result.get("security_action") in ("mask", "block"):
                    if "security_response_rewritten" not in merged:
                        merged.append("security_response_rewritten")
                if result.get("security_action") and result.get("security_reason"):
                    headers_obj["x-akm-security"] = f"{result.get('security_action')}:{result.get('security_reason')}"[:2000]
                for f in usage_flags:
                    if f not in merged:
                        merged.append(f)
                headers_obj["x-akm-flags"] = ",".join(merged)
                request_headers_for_log = json.dumps(headers_obj, ensure_ascii=False)
            elif result.get("security_action") and result.get("security_reason"):
                headers_obj["x-akm-security"] = f"{result.get('security_action')}:{result.get('security_reason')}"[:2000]
                if result.get("security_action") == "warn":
                    headers_obj["x-akm-flags"] = "security_response_warned"
                else:
                    headers_obj["x-akm-flags"] = "security_response_rewritten"
                request_headers_for_log = json.dumps(headers_obj, ensure_ascii=False)
        except Exception:
            request_headers_for_log = request_headers_json
        await _submit_audit_log(request.app, {
            "provider": result["provider"],
            "key_alias": result["key_alias"],
            "model": result["model"],
            "request_body": request_body_for_log if (save_request_body and request_body_for_log) else (_safe_request_body_for_log(body) if save_request_body else ""),
            "response_body": result["body"] if save_response_body else "",
            "status_code": result["status_code"],
            "latency_ms": result["latency_ms"],
            "error": result["error"],
            "request_headers": request_headers_for_log,
            "prompt_tokens": tokens.get("prompt_tokens", 0),
            "completion_tokens": tokens.get("completion_tokens", 0),
            "total_tokens": tokens.get("total_tokens", 0),
            "cached_tokens": tokens.get("cached_tokens", 0),
            "cache_creation_tokens": tokens.get("cache_creation_tokens", 0),
        })

        if result["key_alias"]:
            status = result["status_code"]
            elapsed = f"{result['latency_ms']}ms"
            err = f" error={result['error']}" if result["error"] else ""
            logger.info(
                f"[{result['key_alias']}] {result['provider']} "
                f"model={result['model']} → {status} {elapsed}{err}"
            )
        else:
            logger.warning(
                f"[{api_path}] model={result['model']} → {result['error']}"
            )

        if result["status_code"] == 503:
            return JSONResponse(status_code=503, content={"detail": result["error"]})
        if result["status_code"] == 502:
            return JSONResponse(status_code=502, content={"detail": result["error"]})

        return Response(
            content=result["body"],
            status_code=result["status_code"],
            media_type="application/json",
        )
    finally:
        if monitor is not None:
            monitor.request_finished()
