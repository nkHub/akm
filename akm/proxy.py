"""代理转发：将请求转发到上游 AI API，含重试和故障切换逻辑"""

import time
import json
import asyncio
from contextlib import suppress
import httpx

from akm.config import resolve_http_proxy_url, load_config
from akm.key_pool import (
    pick_key_async,
    pick_wildcard_key_async,
    mark_rate_limited,
    set_status,
    key_model_list,
)
from akm.db import get_connection
from akm.agent import BUILTIN_AGENTS, get_agent
from akm.plugins.context import RequestContext
from akm.error_log import write_error_log


# 原生透传模式下需要跳过的请求头：认证头与传输基础设施头由本服务负责重建，
# 透传原始值会覆盖服务已注入的密钥/Content-Type/User-Agent，或与 httpx 自身管理冲突。
_NATIVE_PASSTHROUGH_SKIP = {
    "authorization",
    "proxy-authorization",
    "host",
    "content-length",
    "connection",
    "accept-encoding",
    "content-type",
    "user-agent",
    "transfer-encoding",
    "upgrade",
}

# 插件覆写上游请求头时仍需排除的头：只拦认证与传输基础设施头，避免插件覆盖密钥或破坏 httpx 传输。
# 与原生透传不同，这里刻意放行 user-agent / content-type / accept 等业务头，
# 以便客户端模拟类插件能改写身份与内容协商头。
_PLUGIN_HEADER_SKIP = {
    "authorization",
    "proxy-authorization",
    "host",
    "content-length",
    "connection",
    "accept-encoding",
    "transfer-encoding",
    "upgrade",
}


class _ChainedAdapter:
    """串联两个协议转换器，支持两段式转换（A->B->C）"""

    def __init__(self, first, second):
        self.first = first
        self.second = second
        self._source_format = getattr(first, "_source_format", "")

    def set_request_context(self, **kwargs):
        """把请求上下文透传给链式转换中的每一段适配器。"""
        if hasattr(self.first, "set_request_context"):
            self.first.set_request_context(**kwargs)
        if hasattr(self.second, "set_request_context"):
            self.second.set_request_context(**kwargs)

    def convert_request(self, body: dict) -> dict:
        return self.second.convert_request(self.first.convert_request(body))

    def convert_response(self, body: str) -> str:
        return self.first.convert_response(self.second.convert_response(body))

    async def convert_sse_stream(self, upstream_stream):
        # 先把 bytes 流解码为文本流，供第二段适配器消费
        async def _bytes_to_text():
            async for chunk in upstream_stream:
                if isinstance(chunk, bytes):
                    yield chunk.decode("utf-8", errors="replace")
                else:
                    yield str(chunk)

        # 第二段：上游目标协议 -> 中间协议。
        # 之前这里会先把整段中间流全部攒进内存，再一次性喂给第一段，
        # 导致链式协议转换场景下首字节被整段响应拖住，用户体感就像
        # “一顿一顿地吐字”。这里改成基于队列的流式桥接，让第二段产出
        # 的每一小段能尽快继续流向第一段，恢复真正的边收边转边发。
        mid_queue: asyncio.Queue[str | BaseException | object] = asyncio.Queue()
        sentinel = object()

        async def _produce_mid_stream():
            try:
                async for line in self.second.convert_sse_stream(_bytes_to_text()):
                    await mid_queue.put(line if isinstance(line, str) else str(line))
            except Exception as exc:
                await mid_queue.put(exc)
            finally:
                await mid_queue.put(sentinel)

        async def _mid_iter():
            while True:
                item = await mid_queue.get()
                if item is sentinel:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item

        producer = asyncio.create_task(_produce_mid_stream())
        try:
            async for line in self.first.convert_sse_stream(_mid_iter()):
                yield line
        finally:
            if not producer.done():
                producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer


# 最大尝试 key 数量，防止无限循环（可通过 config.json 覆盖）
def _proxy_max_key_tries() -> int:
    return max(1, int(load_config().get("proxy_max_key_tries", 20) or 20))

# 5xx 最大重试次数（单个 key）
def _proxy_max_retries_per_key() -> int:
    return max(0, int(load_config().get("proxy_max_retries_per_key", 2) or 2))

# 重试退避基础等待秒数
def _proxy_retry_backoff_base() -> float:
    return max(0.1, float(load_config().get("proxy_retry_backoff_base_sec", 0.5) or 0.5))

# 默认转发超时（秒），图片接口另有独立超时
def _proxy_default_timeout() -> float:
    return max(30.0, float(load_config().get("proxy_default_timeout_sec", 120.0) or 120.0))

# 流式首字节超时（秒）：上游返回 2xx 后迟迟不产出第一个响应体字节时视为
# “假成功”（常见于 gs-codex 等上游对流式攒批），终止本 Key 并切换下一个。
# 0 表示关闭该保护（不预读，恢复旧行为）。
def _proxy_first_byte_timeout() -> float:
    val = load_config().get("proxy_first_byte_timeout_sec", 12.0)
    return max(0.0, float(val if val not in (None, "") else 12.0))

# 敏感请求头名（小写比较）：值一律掩码，避免 API Key / Cookie 等凭据落入审计日志
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "x-akm-api-key",
}


def redact_headers(headers: dict | None) -> dict:
    """请求头脱敏：敏感头保留键名但掩码值，其余头原样保留。

    用于审计日志记录“客户端发起的请求头”和“实际发往上游的请求头”，
    既能事后追溯请求链路，又不把 API Key / Cookie 等凭据明文写入日志。
    Authorization 会保留 scheme（如 Bearer），只掩码凭据本身。
    """
    result = {}
    for key, value in (headers or {}).items():
        lower_key = str(key).lower()
        if lower_key in SENSITIVE_HEADER_NAMES:
            if lower_key == "authorization" and isinstance(value, str) and " " in value:
                result[key] = f"{value.split(' ', 1)[0]} ***"
            else:
                result[key] = "***"
        else:
            result[key] = value
    return result

def _diagnose_no_key(model: str, tried_aliases: set[str] | None = None) -> str:
    """诊断为什么没有可用的 key，返回详细错误信息"""
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM keys WHERE status = 'active'"
    ).fetchone()[0]
    disabled = conn.execute(
        "SELECT COUNT(*) FROM keys WHERE status = 'disabled'"
    ).fetchone()[0]
    limited = conn.execute(
        "SELECT COUNT(*) FROM keys WHERE status = 'rate_limited'"
    ).fetchone()[0]
    # 这里显式展开每个 key 的候选判定结果，便于事后复查“为什么当时没有选中某个 key”。
    matching_disabled = []
    candidate_details = []
    for row in conn.execute(
        "SELECT alias, models, provider_models, status FROM keys ORDER BY alias ASC"
    ).fetchall():
        item = dict(row)
        alias = str(item.get("alias") or "")
        status = str(item.get("status") or "")
        models = str(item.get("models") or "").strip()
        model_list = key_model_list(item)
        if status != "active" and model in set(model_list):
            matching_disabled.append(alias)

        if not model_list:
            if models == "*":
                reason = "wildcard_no_provider_models"
            else:
                reason = "empty_models"
        elif model not in set(model_list):
            reason = "model_not_matched"
        elif status == "disabled":
            reason = "disabled"
        elif status == "rate_limited":
            reason = "rate_limited"
        elif tried_aliases and alias in tried_aliases:
            reason = "tried_and_failed"
        elif status == "active":
            reason = "eligible"
        else:
            reason = status or "unknown"
        candidate_details.append(f"{alias}:{reason}")
    conn.close()

    parts = [f"没有可用的 API key (model={model})"]
    if total == 0:
        parts.append("数据库中没有配置任何 Key")
    else:
        parts.append(f"共{total}个Key: active={active}, disabled={disabled}, rate_limited={limited}")
        if matching_disabled:
            parts.append(f"模型匹配但不可用: {', '.join(matching_disabled)}")
        elif tried_aliases:
            parts.append(f"模型匹配 key 已尝试但全部失败: {', '.join(sorted(tried_aliases))}")
        elif active == 0:
            parts.append("所有 Key 均被禁用或限流")
        else:
            parts.append("没有 Key 的 models 匹配该模型，也没有 provider_models 包含该模型的 wildcard Key")
        if candidate_details:
            parts.append(f"候选判定: {', '.join(candidate_details)}")
    return " | ".join(parts)


async def _handle_upstream_error(
    plugin_manager,
    ctx: RequestContext,
    status_code: int,
    error_type: str,
    attempt: int,
    key: dict,
) -> str | None:
    """调用 on_upstream_error hook，无插件可用时返回内置兜底策略

    返回值: "retry" / "switch" / "block" / "fallback" / None
    """
    if plugin_manager:
        hook_result = await plugin_manager.run_hook(
            "on_upstream_error",
            ctx=ctx,
            status_code=status_code,
            error_type=error_type,
            attempt=attempt,
            key=key,
        )
        if isinstance(hook_result, dict):
            action = hook_result.get("action")
            if action is not None:
                return action
        # PluginManager 对 on_upstream_error 的标准返回值就是首个非空
        # 字符串动作。此前这里只读取 dict，导致 error_handler、第三方
        # fallback_router 等插件即使已返回策略，仍被后面的硬编码兜底覆盖。
        if isinstance(hook_result, str):
            return hook_result

    # ── 内置兜底策略（无 error_handler 插件或插件返回 None 时生效）──
    max_retries = _proxy_max_retries_per_key()
    if status_code == 429:
        return "block"
    if status_code in (402, 401, 403):
        return "block"
    if 500 <= status_code < 600:
        return "retry" if attempt < max_retries else "switch"
    if error_type in ("connect", "timeout", "chunk") and status_code == 0:
        return "retry" if attempt < max_retries else "switch"
    return "switch"


async def _resolve_route_client(client, key: dict, model: str, upstream_api_path: str):
    """按最终路由获取隔离后的 HTTP client；测试或旧调用方仍可传普通 client。"""
    if getattr(client, "is_route_pool", False) is not True:
        return client
    get_client = getattr(client, "get_client", None)
    return await get_client(
        provider=str(key.get("provider", "") or ""),
        key_alias=str(key.get("alias", "") or ""),
        model=str(model or ""),
        api_path=str(upstream_api_path or ""),
    )


async def forward_request(
    body: dict,
    client: httpx.AsyncClient,
    log_callback=None,
    api_path: str = "chat/completions",
    plugin_manager=None,
    request_timeout: float | None = None,
    original_user_agent: str = "",
    passthrough_headers: dict | None = None,
) -> dict:
    """转发请求到上游 AI API，自动处理故障切换

    chat/messages/responses 支持流式；embeddings/rerank/images/generations/images/edits 始终走普通响应。
    request_timeout 允许调用方对单次请求超时做链路级覆盖；图片接口会传入更宽松的超时。
    """
    model = body.get("model", "")
    supports_stream = api_path in {"chat/completions", "messages", "responses"}
    client_wants_stream = body.get("stream", False) if supports_stream else False
    tries = 0
    tried_aliases: set[str] = set()
    use_fallback = False  # 精确匹配耗尽后启用通配符兜底
    selection_skip_reason = ""

    # 请求级上下文：业务 body 与插件状态分离，贯穿本次 forward 全生命周期
    ctx = RequestContext(
        body if isinstance(body, dict) else {},
        api_path=api_path,
        client_user_agent=original_user_agent or "",
    )
    model = ctx.model or model

    async def _emit_on_response_meta(meta: dict):
        """触发插件 on_response 生命周期钩子，向插件暴露请求/响应元信息。"""
        if not plugin_manager:
            return meta
        try:
            result = await plugin_manager.run_hook("on_response", ctx=ctx, response=meta)
            if isinstance(result, RequestContext) and isinstance(result.response, dict):
                return result.response
            if isinstance(result, dict) and "response" in result:
                return result["response"]
        except Exception:
            # hook 内异常由插件管理器隔离；此处双保险避免影响主链路
            pass
        return meta

    # ── 插件 hook: on_request（模型名映射等预处理）──
    if plugin_manager:
        await plugin_manager.run_hook("on_request", ctx=ctx)
        body = ctx.request
        model = ctx.model or model
        if ctx.is_block:
            blocked = ctx.action or {}
            status_code = int(blocked.get("status_code", 400) or 400)
            error = str(blocked.get("error", "请求命中安全策略，已被拦截") or "请求命中安全策略，已被拦截")
            response_body = blocked.get("body")
            if not isinstance(response_body, str) or not response_body:
                response_body = json.dumps({"error": error}, ensure_ascii=False)
            security_action = str(blocked.get("security_action", "block") or "block")
            security_reason = str(blocked.get("security_reason", "") or "")
            await _emit_on_response_meta({
                "ok": False,
                "phase": "on_request",
                "status_code": status_code,
                "key_alias": "",
                "provider": "",
                "model": model,
                "latency_ms": 0,
                "error": error,
                "api_path": api_path,
                "security_action": security_action,
                "security_reason": security_reason,
            })
            return {
                "status_code": status_code,
                "body": response_body,
                "key_alias": "",
                "provider": "",
                "model": model,
                "error": error,
                "latency_ms": 0,
                "security_action": security_action,
                "security_reason": security_reason,
                "request_context": ctx,
            }

    while tries < _proxy_max_key_tries():
        # ── 两阶段 key 选择：精确匹配 → 通配符兜底 ──
        if use_fallback:
            key = await pick_wildcard_key_async(model, list(tried_aliases))
        else:
            key = await pick_key_async(model, list(tried_aliases))

        if key is None:
            if not use_fallback:
                # 精确匹配无可用 key，尝试通配符兜底
                use_fallback = True
                continue
            # 兜底也无可用 key
            if selection_skip_reason:
                err_msg = selection_skip_reason
                status_code = 429
            else:
                err_msg = _diagnose_no_key(model, tried_aliases)
                status_code = 502 if tried_aliases else 503
            await _emit_on_response_meta({
                "ok": False,
                "phase": "select_key",
                "status_code": status_code,
                "key_alias": "",
                "provider": "",
                "model": model,
                "latency_ms": 0,
                "error": err_msg,
                "api_path": api_path,
            })
            return {
                "status_code": status_code,
                "body": "",
                "key_alias": "",
                "provider": "",
                "model": model,
                "error": err_msg,
                "latency_ms": 0,
            }

        # 候选 Key 只有真正发往上游后才应视为已尝试；插件可能在下一阶段替换它。
        if key["alias"] in tried_aliases:
            continue

        tries += 1

        # ── 插件 hook: on_key_selected（模型匹配后二次调整）──
        if plugin_manager:
            ctx.key = key
            ctx.model = model
            # 把本轮已失败（在 tried_aliases 中）的 key 透传给插件，避免旁路插件
            # 反复选中已失败的 key 导致 proxy 主循环空转（tries 不增长、请求卡死）。
            # 插件在 on_key_selected 中可通过 ctx.bag_get("proxy.tried_aliases") 读取。
            ctx.bag_set("proxy.tried_aliases", list(tried_aliases))
            await plugin_manager.run_hook("on_key_selected", ctx=ctx, model=model, key=key)
            if ctx.is_skip_key:
                skipped = ctx.action or {}
                selection_skip_reason = str(
                    skipped.get("error", "当前可用 Key 均已达到配额上限")
                    or "当前可用 Key 均已达到配额上限"
                )
                ctx.clear_action()
                # model_matcher 在 on_key_selected 阶段已为当前 key 递增 inflight，
                # 跳过时需触发 on_response 回收，防止 inflight 计数泄漏。
                inflight_key = ctx.bag_get("model_matcher.inflight_key")
                if inflight_key:
                    await _emit_on_response_meta({
                        "ok": True,
                        "phase": "skip_key",
                        "status_code": 0,
                        "key_alias": inflight_key,
                        "provider": key.get("provider", ""),
                        "model": model,
                        "latency_ms": 0,
                        "error": "",
                        "api_path": api_path,
                    })
                tried_aliases.add(key["alias"])
                continue
            if isinstance(ctx.key, dict):
                key = ctx.key

        key_alias = str(key.get("alias", "") or "")
        if not key_alias:
            last_error = "选中的 Key 缺少 alias"
            break
        if key_alias in tried_aliases:
            continue
        tried_aliases.add(key_alias)

        agent = get_agent(key.get("provider", "openai"))

        # ── 协议转换检测（embeddings / rerank / images/generations / images/edits 不参与协议转换）──
        target_api_path = agent.needs_conversion(api_path)
        adapter = None
        if api_path not in {"embeddings", "rerank", "images/generations", "images/edits"} and target_api_path and plugin_manager:
            # 从插件系统查找转换器：api_path 格式 → target_api_path 格式
            from_fmt = api_path.replace("/completions", "")
            to_fmt = target_api_path.replace("/completions", "")
            adapter = plugin_manager.get_converter(from_fmt, to_fmt)
            # 两段式兜底：responses -> chat -> messages
            if adapter is None and from_fmt == "responses" and to_fmt == "messages":
                first = plugin_manager.get_converter("responses", "chat")
                second = plugin_manager.get_converter("chat", "messages")
                if first and second:
                    adapter = _ChainedAdapter(first, second)
            if adapter is None:
                # 找不到转换器则返回明确报错
                err_msg = f"缺少 {from_fmt}→{to_fmt} 转换器"
                await _emit_on_response_meta({
                    "ok": False,
                    "phase": "converter",
                    "status_code": 400,
                    "key_alias": key.get("alias", ""),
                    "provider": key.get("provider", ""),
                    "model": model,
                    "latency_ms": 0,
                    "error": err_msg,
                    "api_path": api_path,
                    "upstream_api_path": target_api_path,
                })
                return {
                    "status_code": 400,
                    "body": json.dumps({
                        "error": f"缺少协议转换插件：需要将 {from_fmt} 请求转为 {to_fmt} 格式，但未找到启用的转换器。请前往插件管理页面开启 protocol_converter 插件。"
                    }),
                    "key_alias": key.get("alias", ""),
                    "provider": key.get("provider", ""),
                    "model": model,
                    "error": err_msg,
                    "latency_ms": 0,
                }

        # 构建上游 URL：转换后走目标路径
        upstream_api_path = target_api_path or api_path
        url = agent.resolve_url(key, upstream_api_path)
        headers = agent.build_headers(key, upstream_api_path, original_user_agent=original_user_agent)
        # 上游请求头合并策略（优先级从高到低）：
        #   1. 插件覆写：on_request 阶段经 ctx.set_upstream_headers() 写入（如客户端模拟插件），
        #      允许覆盖 User-Agent / Content-Type 等业务头，但认证头与传输基础设施头仍被排除，
        #      防止插件破坏密钥注入或 httpx 传输。
        #   2. 原生透传（use_native_user_agent=true）：把客户端携带的业务头原样带给上游，
        #      让依赖身份/会话头的网关（如 Codex 官方）能识别为原生客户端。
        #   3. build_headers 默认头。
        # 二者互斥：插件显式覆写存在时优先，避免原生透传的杂项头与模拟身份冲突。
        if ctx.upstream_headers:
            for name, value in ctx.upstream_headers.items():
                if str(name).lower() in _PLUGIN_HEADER_SKIP:
                    continue
                if value is not None:
                    headers[name] = value
        elif passthrough_headers and bool(load_config().get("use_native_user_agent", False)):
            for name, value in passthrough_headers.items():
                if str(name).lower() in _NATIVE_PASSTHROUGH_SKIP:
                    continue
                if value is not None:
                    headers[name] = value
        route_client = await _resolve_route_client(client, key, model, upstream_api_path)

        if adapter and hasattr(adapter, "set_request_context"):
            adapter.set_request_context(provider=key.get("provider", ""))

        body = ctx.request
        is_multipart_request = bool(body.get("__akm_multipart__"))
        multipart_fields = body.get("__akm_form_fields__") if is_multipart_request else None
        multipart_files = body.get("__akm_form_files__") if is_multipart_request else None

        # ── 上游请求模式跟随客户端：流式接口按需走 SSE，其他接口直接请求普通响应 ──
        # 插件跨阶段状态已迁入 RequestContext.bag，不再挂在 body 上。
        # 仍剥离遗留 __akm_* 字段，防止 multipart 传输标记或旧插件字段误入上游。
        forwardable_body = ctx.forwardable_request()
        upstream_body = adapter.convert_request(forwardable_body) if adapter else dict(forwardable_body)

        if is_multipart_request:
            # multipart 由 httpx 自动生成 boundary；若保留 application/json 或裸 multipart/form-data，
            # 上游通常会因为缺失 boundary 直接 400，因此这里显式移除 Content-Type，交给 httpx 处理。
            headers.pop("Content-Type", None)
            forwarded_request_body = json.dumps(
                {
                    **(multipart_fields or {}),
                    "__akm_files__": {
                        key_name: {
                            "filename": item[0],
                            "content_type": item[2],
                        }
                        for key_name, item in (multipart_files or {}).items()
                    },
                },
                ensure_ascii=False,
            )
        else:
            forwarded_request_body = json.dumps(upstream_body, ensure_ascii=False)

        # 实际发往上游的请求头（脱敏后），随结果回传供审计落库。
        # multipart 场景下 Content-Type 已在上方移除，交由 httpx 生成 boundary，
        # 因此这里记录的即是真实发送时的头集合。
        upstream_headers_for_log = json.dumps(redact_headers(headers), ensure_ascii=False)

        if supports_stream:
            upstream_body["stream"] = client_wants_stream
        # 对 OpenAI Chat 流式显式请求 usage，提升 token 统计稳定性。
        # 非流式返回通常会自带完整 usage，这里不额外注入 stream_options。
        if client_wants_stream and upstream_api_path == "chat/completions":
            stream_options = upstream_body.get("stream_options")
            if isinstance(stream_options, dict):
                stream_options["include_usage"] = True
            else:
                upstream_body["stream_options"] = {"include_usage": True}

        last_error = ""
        for attempt in range(1 + _proxy_max_retries_per_key()):
            t0 = time.time()
            try:
                if is_multipart_request:
                    req = route_client.build_request(
                        "POST",
                        url,
                        data=multipart_fields,
                        files=multipart_files,
                        headers=headers,
                        timeout=request_timeout or _proxy_default_timeout(),
                    )
                else:
                    req = route_client.build_request(
                        "POST",
                        url,
                        json=upstream_body,
                        headers=headers,
                        timeout=request_timeout or _proxy_default_timeout(),
                    )
                resp = await route_client.send(req, stream=client_wants_stream)
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                error_type = "timeout" if isinstance(e, httpx.TimeoutException) else "connect"
                action = await _handle_upstream_error(
                    plugin_manager, ctx, 0, error_type, attempt, key
                )
                await _emit_on_response_meta({
                    "ok": False,
                    "phase": "request",
                    "status_code": 0,
                    "key_alias": key["alias"],
                    "provider": key["provider"],
                    "model": model,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": str(e),
                    "error_type": error_type,
                    "attempt": attempt,
                    "api_path": api_path,
                    "upstream_api_path": upstream_api_path,
                    "action": action,
                })
                if action == "retry" and attempt < _proxy_max_retries_per_key():
                    await asyncio.sleep(_proxy_retry_backoff_base() * (2 ** attempt))
                    continue
                last_error = str(e)
                break
            except Exception as e:
                await _emit_on_response_meta({
                    "ok": False,
                    "phase": "request",
                    "status_code": 0,
                    "key_alias": key["alias"],
                    "provider": key["provider"],
                    "model": model,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": str(e),
                    "error_type": "unknown",
                    "attempt": attempt,
                    "api_path": api_path,
                    "upstream_api_path": upstream_api_path,
                })
                last_error = str(e)
                break

            # ── 错误状态码处理（通过 on_upstream_error hook 决定策略）──
            is_error = not 200 <= resp.status_code < 300

            if is_error:
                action = await _handle_upstream_error(
                    plugin_manager, ctx, resp.status_code, "http", attempt, key
                )
                last_error = f"{resp.status_code} (key: {key['alias']})"
                if action == "block":
                    if resp.status_code == 429:
                        mark_rate_limited(key["alias"])
                    else:
                        set_status(key["alias"], "disabled")
                await resp.aclose()
                await _emit_on_response_meta({
                    "ok": False,
                    "phase": "upstream",
                    "status_code": resp.status_code,
                    "key_alias": key["alias"],
                    "provider": key["provider"],
                    "model": model,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": last_error,
                    "error_type": "http",
                    "attempt": attempt,
                    "api_path": api_path,
                    "upstream_api_path": upstream_api_path,
                    "action": action,
                })
                if action == "retry" and attempt < _proxy_max_retries_per_key():
                    await asyncio.sleep(_proxy_retry_backoff_base() * (2 ** attempt))
                    continue
                break

            # ── 流式首字节预读 + 超时保护 ──
            # 上游 2xx 只表示“已接受请求”，不保证响应体会尽快产出。gs-codex 等上游
            # 对流式请求常“攒批”，首个字节可能延迟数十秒，客户端全程无输出体验为卡住。
            # 这里预读第一个响应体字节：成功则缓存进 first_chunk 回传（server 先输出
            # 再续读，不丢数据）；超时则关闭本上游并切换下一个 Key，避免无限挂起。
            first_chunk: bytes | None = None
            stream_aiter = None
            if client_wants_stream:
                first_byte_timeout = _proxy_first_byte_timeout()
                if first_byte_timeout > 0:
                    # 必须复用同一个 aiter_bytes 生成器：httpx 的 aiter_raw 只能消费
                    # 一次（is_stream_consumed=True），且每次调用 aiter_bytes() 都会
                    # 重建解码器（gzip/deflate 有跨块状态）。若 server 侧重新调用
                    # resp.aiter_bytes()，会抛 StreamConsumed 或解压错乱。这里创建
                    # 一次生成器并回传，server 侧从同一生成器继续迭代。
                    stream_aiter = resp.aiter_bytes()
                    try:
                        first_chunk = await asyncio.wait_for(
                            stream_aiter.__anext__(),
                            timeout=first_byte_timeout,
                        )
                        first_chunk = first_chunk or b""
                    except asyncio.TimeoutError:
                        await resp.aclose()
                        last_error = (
                            f"上游首字节超时（{first_byte_timeout}s 未产出响应体）: {key['alias']}"
                        )
                        write_error_log(
                            source="proxy.forward_request",
                            error=f"上游首字节超时（{first_byte_timeout}s 未产出响应体）: {key['alias']}",
                            extra={"provider": key.get("provider", ""), "key_alias": key["alias"], "model": model},
                        )
                        await _emit_on_response_meta({
                            "ok": False,
                            "phase": "stream_first_byte",
                            "status_code": 0,
                            "key_alias": key["alias"],
                            "provider": key["provider"],
                            "model": model,
                            "latency_ms": int((time.time() - t0) * 1000),
                            "error": last_error,
                            "error_type": "first_byte_timeout",
                            "attempt": attempt,
                            "api_path": api_path,
                            "upstream_api_path": upstream_api_path,
                        })
                        break
                    except StopAsyncIteration:
                        # 上游立即返回空流（0 字节响应体），视为已获得首字节（EOF）
                        first_chunk = b""
                    except Exception as e:
                        # 首字节等待期间上游连接断开/流异常：关闭并按失败处理，
                        # 切到下一个 Key 重试，避免冒泡成 500。
                        await resp.aclose()
                        last_error = f"上游首字节读取失败: {e}"
                        write_error_log(
                            source="proxy.forward_request",
                            error=last_error,
                            extra={"provider": key.get("provider", ""), "key_alias": key["alias"], "model": model},
                        )
                        await _emit_on_response_meta({
                            "ok": False,
                            "phase": "stream_first_byte",
                            "status_code": 0,
                            "key_alias": key["alias"],
                            "provider": key["provider"],
                            "model": model,
                            "latency_ms": int((time.time() - t0) * 1000),
                            "error": last_error,
                            "error_type": "first_byte_error",
                            "attempt": attempt,
                            "api_path": api_path,
                            "upstream_api_path": upstream_api_path,
                        })
                        break

            # ── 成功：客户端流式 → 透传或标记转换 ──
            if client_wants_stream:
                # 流式请求在这里只表示“上游已经接受并开始返回数据”，并不代表
                # 整个请求生命周期已经结束。真正的完成/失败信号要等 server.py
                # 中的 StreamingResponse 生成器退出后再统一触发 on_response，
                # 否则像 model_matcher 这类依赖该生命周期回收 in-flight 计数的
                # 插件会过早减计数，导致并发判断失真，慢请求积压时表现为整服卡住。
                #
                # request_context 必须回传：插件 bag（如 reverse_map）与改写后的
                # request 都在 ctx 上；server 侧不能读入口原始 body。
                return {
                    "stream": True,
                    "status_code": resp.status_code,
                    "response": resp,
                    "adapter": adapter,  # 非 None 时 server.py 会用转换器包装
                    "first_chunk": first_chunk,  # proxy 预读的首字节，server 生成器先输出再续读
                    "aiter": stream_aiter,  # 复用的流式生成器，server 侧从此继续迭代（避免 httpx StreamConsumed）
                    "request_body_for_log": forwarded_request_body,
                    "upstream_headers_for_log": upstream_headers_for_log,
                    "request_context": ctx,
                    "local_request": ctx.request,  # 兼容旧测试/调用方
                    "key_alias": key["alias"],
                    "provider": key["provider"],
                    "model": model,
                }

            # 非流式客户端：直接读取上游普通 JSON 响应。
            try:
                resp_body = (await resp.aread()).decode("utf-8", errors="replace")
            except Exception as e:
                action = await _handle_upstream_error(
                    plugin_manager, ctx, 0, "read", attempt, key
                )
                last_error = f"读取非流式响应失败: {e}"
                await resp.aclose()
                await _emit_on_response_meta({
                    "ok": False,
                    "phase": "read_response",
                    "status_code": 0,
                    "key_alias": key["alias"],
                    "provider": key["provider"],
                    "model": model,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": last_error,
                    "error_type": "read",
                    "attempt": attempt,
                    "api_path": api_path,
                    "upstream_api_path": upstream_api_path,
                    "action": action,
                })
                if action == "retry" and attempt < _proxy_max_retries_per_key():
                    await asyncio.sleep(_proxy_retry_backoff_base() * (2 ** attempt))
                    continue
                break
            await resp.aclose()

            latency = int((time.time() - t0) * 1000)
            json_body = resp_body
            # 上游原始响应（插件/协议转换前），随结果回传供审计记录原始响应体
            upstream_response_body_for_log = json_body
            # 协议转换：响应体格式转回客户端期望的格式
            if adapter:
                json_body = adapter.convert_response(json_body)
            response_meta = await _emit_on_response_meta({
                "ok": True,
                "phase": "upstream",
                "status_code": resp.status_code,
                "key_alias": key["alias"],
                "provider": key["provider"],
                "model": model,
                "latency_ms": latency,
                "error": "",
                "attempt": attempt,
                "api_path": api_path,
                "upstream_api_path": upstream_api_path,
                "stream": False,
                "response_body": json_body,
            })
            if isinstance(response_meta, dict):
                json_body = response_meta.get("response_body", json_body)
            return {
                "status_code": int(response_meta.get("status_code", resp.status_code)) if isinstance(response_meta, dict) else resp.status_code,
                "body": json_body,
                "adapter": adapter,
                "request_body_for_log": forwarded_request_body,
                "upstream_headers_for_log": upstream_headers_for_log,
                "upstream_response_body_for_log": upstream_response_body_for_log,
                "key_alias": key["alias"],
                "provider": key["provider"],
                "model": model,
                "error": response_meta.get("error", "") if isinstance(response_meta, dict) else "",
                "latency_ms": latency,
            }

        # 当前 key 彻底失败，日志回调记录失败尝试

        # fallback_router 在 on_upstream_error 中改写 ctx.request.model 并返回
        # fallback。这里从 ctx 同步模型，使下一轮从目标模型重新选 Key。
        ctx.sync_model_from_request()
        body = ctx.request
        next_model = str(ctx.model or model)
        if next_model != model:
            model = next_model
            tried_aliases.clear()
            use_fallback = False
            selection_skip_reason = ""

        # 当前 key 彻底失败，日志回调记录失败尝试
        if log_callback:
            log_callback({
                "provider": key["provider"],
                "key_alias": key["alias"],
                "model": model,
                "request_body": json.dumps(forwardable_body, ensure_ascii=False),
                "response_body": "",
                "status_code": 0,
                "latency_ms": 0,
                "error": last_error,
            })

    await _emit_on_response_meta({
        "ok": False,
        "phase": "exhausted",
        "status_code": 502,
        "key_alias": "",
        "provider": "",
        "model": model,
        "latency_ms": 0,
        "error": "所有 key 均已尝试但均失败",
        "api_path": api_path,
    })
    return {
        "status_code": 502,
        "body": "",
        "key_alias": "",
        "provider": "",
        "model": model,
        "error": "所有 key 均已尝试但均失败",
        "latency_ms": 0,
    }


async def test_key_connectivity(key: dict, allow_fallback: bool = False) -> dict:
    """测试单个 key 的连通性，按供应商能力选择主接口。

    allow_fallback 为 true 时，允许按兼容协议继续尝试；默认关闭。

    返回: {"ok": bool, "url": str, "model": str, "api_path": str,
           "status_code": int, "latency_ms": int, "error": str,
           "response_body": str, "attempted_paths": list[str],
           "fallback_used": bool}
    """
    agent = get_agent(key.get("provider", "openai"))

    resolved_models = key_model_list(key)
    if not resolved_models:
        return {
            "ok": False,
            "url": "",
            "model": "",
            "api_path": "",
            "status_code": 0,
            "latency_ms": 0,
            "error": "该 Key 当前没有可用模型列表，请先保存或刷新模型",
            "response_body": "",
            "attempted_paths": [],
            "fallback_used": False,
        }
    model = str(resolved_models[0] or "").strip()

    is_custom_agent = agent.name not in BUILTIN_AGENTS
    if is_custom_agent:
        # 自定义供应商测试时按“第一个启用的协议能力”发起请求，
        # 与设置页中用户勾选/阅读协议能力的直觉顺序保持一致。
        if agent.supports_chat:
            candidate_paths = ["chat/completions"]
            if allow_fallback:
                if agent.supports_responses:
                    candidate_paths.append("responses")
                if agent.supports_messages:
                    candidate_paths.append("messages")
        elif agent.supports_responses:
            candidate_paths = ["responses"]
            if allow_fallback and agent.supports_messages:
                candidate_paths.append("messages")
        elif agent.supports_messages:
            candidate_paths = ["messages"]
        else:
            candidate_paths = ["chat/completions"]
    # 内置供应商：连通性测试按「Chat > Responses > Messages」顺序优先，
    # chat/completions 最通用、兼容性最好，作为连通性验证首选。
    if agent.supports_chat:
        candidate_paths = ["chat/completions"]
        if allow_fallback:
            if agent.supports_responses:
                candidate_paths.append("responses")
            if agent.supports_messages:
                candidate_paths.append("messages")
    elif agent.supports_responses:
        candidate_paths = ["responses"]
        if allow_fallback and agent.supports_messages:
            candidate_paths.append("messages")
    elif agent.supports_messages:
        candidate_paths = ["messages"]
    else:
        candidate_paths = ["chat/completions"]

    attempted_paths: list[str] = []

    def _make_body(api_path: str) -> dict:
        if api_path == "responses":
            return {
                "model": model,
                "input": "hi",
                "max_output_tokens": 16,
            }
        if api_path == "messages":
            return {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            }
        return {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }

    def _result(url: str, api_path: str, **kw):
        base = {
            "ok": False,
            "url": url,
            "model": model,
            "api_path": api_path,
            "status_code": 0,
            "latency_ms": 0,
            "error": "",
            "response_body": "",
            "attempted_paths": list(attempted_paths),
            "fallback_used": len(attempted_paths) > 1,
        }
        base.update(kw)
        return base

    _proxy = resolve_http_proxy_url()
    async with httpx.AsyncClient(**({"proxy": _proxy} if _proxy else {})) as client:
        last_result = None
        for api_path in candidate_paths:
            attempted_paths.append(api_path)
            url = agent.resolve_url(key, api_path)
            headers = agent.build_headers(key, api_path)
            body = _make_body(api_path)
            t0 = time.time()
            try:
                resp = await client.post(url, json=body, headers=headers, timeout=30)
                latency = int((time.time() - t0) * 1000)
                resp_text = resp.text[:500]
                if 200 <= resp.status_code < 300:
                    return _result(url, api_path, ok=True, status_code=resp.status_code, latency_ms=latency)
                if resp.status_code == 429:
                    return _result(url, api_path, status_code=429, latency_ms=latency, error="429 限流")
                if resp.status_code in (401, 403):
                    try:
                        detail = resp.json()
                        err_msg = str(detail.get("error", {}).get("message", "认证失败，key 无效"))
                        err_code = str(detail.get("error", {}).get("code", "") or "")
                    except Exception:
                        err_msg = "认证失败，key 无效"
                        err_code = ""
                    last_result = _result(url, api_path, status_code=resp.status_code, latency_ms=latency, error=err_msg, response_body=resp_text)
                    if allow_fallback and api_path == "responses" and resp.status_code == 403 and err_code == "codex_access_restricted":
                        continue
                    return last_result
                if resp.status_code == 402:
                    return _result(url, api_path, status_code=402, latency_ms=latency, error="余额不足", response_body=resp_text)
                try:
                    detail = resp.json()
                    err_msg = str(detail.get("error", {}).get("message", f"HTTP {resp.status_code}"))
                except Exception:
                    err_msg = f"HTTP {resp.status_code}"
                last_result = _result(url, api_path, status_code=resp.status_code, latency_ms=latency, error=err_msg, response_body=resp_text)
                if allow_fallback and resp.status_code == 404 and api_path != candidate_paths[-1]:
                    continue
                return last_result
            except httpx.TimeoutException:
                last_result = _result(url, api_path, error="请求超时")
                return last_result
            except httpx.ConnectError as e:
                write_error_log(
                    source="proxy.connectivity_test",
                    error=f"连接失败: {e}",
                    extra={"url": url, "api_path": api_path, "key_alias": key.get("alias", "")},
                )
                last_result = _result(url, api_path, error="连接失败")
                return last_result
            except Exception as e:
                write_error_log(
                    source="proxy.connectivity_test",
                    error=str(e),
                    extra={"url": url, "api_path": api_path, "key_alias": key.get("alias", "")},
                )
                last_result = _result(url, api_path, error="测试失败")
                return last_result

        return last_result or _result(agent.resolve_url(key, candidate_paths[0]), candidate_paths[0], error="测试失败")
