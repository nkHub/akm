"""代理转发：将请求转发到上游 AI API，含重试和故障切换逻辑"""

import time
import json
import asyncio
import httpx
from akm.key_pool import pick_key_async, pick_wildcard_key_async, mark_rate_limited, set_status
from akm.db import get_connection
from akm.agent import get_agent


class _ChainedAdapter:
    """串联两个协议转换器，支持两段式转换（A->B->C）"""

    def __init__(self, first, second):
        self.first = first
        self.second = second
        self._source_format = getattr(first, "_source_format", "")

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

        # 第二段：上游目标协议 -> 中间协议
        mid_lines = []
        async for line in self.second.convert_sse_stream(_bytes_to_text()):
            mid_lines.append(line)

        mid_text = "".join(mid_lines)

        # 第一段：中间协议 -> 源协议
        async def _mid_iter():
            yield mid_text

        async for line in self.first.convert_sse_stream(_mid_iter()):
            yield line


# 最大尝试 key 数量，防止无限循环
MAX_KEY_TRIES = 20
# 5xx 最大重试次数（单个 key）
MAX_RETRIES_PER_KEY = 2
# 重试退避基础等待秒数
RETRY_BACKOFF_BASE = 0.5

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
    # 查有哪些 model 匹配的 key 但被禁用了
    matching_disabled = conn.execute(
        """SELECT alias FROM keys
           WHERE status != 'active'
             AND (models = '*' OR ',' || models || ',' LIKE '%,' || ? || ',%')""",
        (model,),
    ).fetchall()
    conn.close()

    parts = [f"没有可用的 API key (model={model})"]
    if total == 0:
        parts.append("数据库中没有配置任何 Key")
    else:
        parts.append(f"共{total}个Key: active={active}, disabled={disabled}, rate_limited={limited}")
        if matching_disabled:
            aliases = [r["alias"] for r in matching_disabled]
            parts.append(f"模型匹配但不可用: {', '.join(aliases)}")
        elif tried_aliases:
            parts.append(f"模型匹配 key 已尝试但全部失败: {', '.join(sorted(tried_aliases))}")
        elif active == 0:
            parts.append("所有 Key 均被禁用或限流")
        else:
            parts.append("没有 Key 的 models 匹配该模型，也没有 models='*' 的通配 Key")
    return " | ".join(parts)


async def _handle_upstream_error(
    plugin_manager,
    body: dict,
    status_code: int,
    error_type: str,
    attempt: int,
    key: dict,
) -> str | None:
    """调用 on_upstream_error hook，无插件可用时返回内置兜底策略

    返回值: "retry" / "switch" / "block" / None
    """
    if plugin_manager:
        hook_result = await plugin_manager.run_hook(
            "on_upstream_error",
            request=body,
            status_code=status_code,
            error_type=error_type,
            attempt=attempt,
            key=key,
        )
        if isinstance(hook_result, dict):
            action = hook_result.get("action")
            if action is not None:
                return action

    # ── 内置兜底策略（无 error_handler 插件或插件返回 None 时生效）──
    max_retries = MAX_RETRIES_PER_KEY
    if status_code == 429:
        return "block"
    if status_code in (402, 401, 403):
        return "block"
    if 500 <= status_code < 600:
        return "retry" if attempt < max_retries else "switch"
    if error_type in ("connect", "timeout", "chunk") and status_code == 0:
        return "retry" if attempt < max_retries else "switch"
    return "switch"


async def forward_request(
    body: dict,
    client: httpx.AsyncClient,
    log_callback=None,
    api_path: str = "chat/completions",
    plugin_manager=None,
) -> dict:
    """转发请求到上游 AI API，自动处理故障切换

    chat/messages/responses 支持流式；embeddings 始终走普通 JSON。
    """
    model = body.get("model", "")
    supports_stream = api_path in {"chat/completions", "messages", "responses"}
    client_wants_stream = body.get("stream", False) if supports_stream else False
    tries = 0
    tried_aliases: set[str] = set()
    use_fallback = False  # 精确匹配耗尽后启用通配符兜底

    async def _emit_on_response_meta(meta: dict):
        """触发插件 on_response 生命周期钩子，向插件暴露请求/响应元信息。"""
        if not plugin_manager:
            return meta
        try:
            result = await plugin_manager.run_hook("on_response", request=body, response=meta)
            if isinstance(result, dict) and "response" in result:
                return result["response"]
        except Exception:
            # hook 内异常由插件管理器隔离；此处双保险避免影响主链路
            pass
        return meta

    # ── 插件 hook: on_request（模型名映射等预处理）──
    if plugin_manager:
        hook_result = await plugin_manager.run_hook("on_request", request=body)
        if isinstance(hook_result, dict) and "on_request_block" in hook_result:
            blocked = hook_result["on_request_block"] or {}
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
            }
        if isinstance(hook_result, dict) and "request" in hook_result:
            body = hook_result["request"]
            model = body.get("model", model)

    while tries < MAX_KEY_TRIES:
        # ── 两阶段 key 选择：精确匹配 → 通配符兜底 ──
        if use_fallback:
            key = await pick_wildcard_key_async()
        else:
            key = await pick_key_async(model, list(tried_aliases))

        if key is None:
            if not use_fallback:
                # 精确匹配无可用 key，尝试通配符兜底
                use_fallback = True
                continue
            # 兜底也无可用 key
            err_msg = _diagnose_no_key(model, tried_aliases)
            await _emit_on_response_meta({
                "ok": False,
                "phase": "select_key",
                "status_code": 502 if tried_aliases else 503,
                "key_alias": "",
                "provider": "",
                "model": model,
                "latency_ms": 0,
                "error": err_msg,
                "api_path": api_path,
            })
            return {
                "status_code": 502 if tried_aliases else 503,
                "body": "",
                "key_alias": "",
                "provider": "",
                "model": model,
                "error": err_msg,
                "latency_ms": 0,
            }

        # 避免重复尝试同一个 key（5xx 不会禁用 key，可能被反复选中）
        # 继续循环让 pick_key 返回下一个匹配的 key，而非直接跳通配符兜底
        if key["alias"] in tried_aliases:
            continue
        tried_aliases.add(key["alias"])

        tries += 1

        # ── 插件 hook: on_key_selected（模型匹配后二次调整）──
        if plugin_manager:
            result = await plugin_manager.run_hook(
                "on_key_selected", model=model, key=key, request=body
            )
            if isinstance(result, dict) and "key" in result:
                key = result["key"]

        agent = get_agent(key.get("provider", "openai"))

        # ── 协议转换检测（embeddings 不参与协议转换）──
        target_api_path = agent.needs_conversion(api_path)
        adapter = None
        if api_path != "embeddings" and target_api_path and plugin_manager:
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
        headers = agent.build_headers(key, upstream_api_path)

        # ── 上游请求模式跟随客户端：流式接口按需走 SSE，其他接口直接请求 JSON ──
        upstream_body = adapter.convert_request(body) if adapter else dict(body)

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
        for attempt in range(1 + MAX_RETRIES_PER_KEY):
            t0 = time.time()
            try:
                req = client.build_request("POST", url, json=upstream_body, headers=headers, timeout=120)
                resp = await client.send(req, stream=client_wants_stream)
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                error_type = "timeout" if isinstance(e, httpx.TimeoutException) else "connect"
                action = await _handle_upstream_error(
                    plugin_manager, body, 0, error_type, attempt, key
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
                if action == "retry" and attempt < MAX_RETRIES_PER_KEY:
                    await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
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
            is_error = resp.status_code != 200

            if is_error:
                action = await _handle_upstream_error(
                    plugin_manager, body, resp.status_code, "http", attempt, key
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
                if action == "retry" and attempt < MAX_RETRIES_PER_KEY:
                    await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                break

            # ── 成功：客户端流式 → 透传或标记转换 ──
            if client_wants_stream:
                await _emit_on_response_meta({
                    "ok": True,
                    "phase": "upstream",
                    "status_code": 200,
                    "key_alias": key["alias"],
                    "provider": key["provider"],
                    "model": model,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": "",
                    "attempt": attempt,
                    "api_path": api_path,
                    "upstream_api_path": upstream_api_path,
                    "stream": True,
                })
                return {
                    "stream": True,
                    "status_code": 200,
                    "response": resp,
                    "adapter": adapter,  # 非 None 时 server.py 会用转换器包装
                    "key_alias": key["alias"],
                    "provider": key["provider"],
                    "model": model,
                }

            # 非流式客户端：直接读取上游普通 JSON 响应。
            try:
                resp_body = (await resp.aread()).decode("utf-8", errors="replace")
            except Exception as e:
                action = await _handle_upstream_error(
                    plugin_manager, body, 0, "read", attempt, key
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
                if action == "retry" and attempt < MAX_RETRIES_PER_KEY:
                    await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                break
            await resp.aclose()

            latency = int((time.time() - t0) * 1000)
            json_body = resp_body
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
                "key_alias": key["alias"],
                "provider": key["provider"],
                "model": model,
                "error": response_meta.get("error", "") if isinstance(response_meta, dict) else "",
                "latency_ms": latency,
            }

        # 当前 key 彻底失败，日志回调记录失败尝试

        # 当前 key 彻底失败，日志回调记录失败尝试
        if log_callback:
            log_callback({
                "provider": key["provider"],
                "key_alias": key["alias"],
                "model": model,
                "request_body": json.dumps(body, ensure_ascii=False),
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
    model = key.get("models", "*").split(",")[0].strip() or "gpt-3.5-turbo"

    if agent.supports_responses:
        candidate_paths = ["responses"]
        if allow_fallback:
            if agent.supports_chat:
                candidate_paths.append("chat/completions")
            if agent.supports_messages:
                candidate_paths.append("messages")
    elif agent.supports_chat:
        candidate_paths = ["chat/completions"]
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
                "max_output_tokens": 1,
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

    async with httpx.AsyncClient() as client:
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
                if resp.status_code == 200:
                    return _result(url, api_path, ok=True, status_code=200, latency_ms=latency)
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
                last_result = _result(url, api_path, error=f"连接失败: {e}")
                return last_result
            except Exception as e:
                last_result = _result(url, api_path, error=str(e))
                return last_result

        return last_result or _result(agent.resolve_url(key, candidate_paths[0]), candidate_paths[0], error="测试失败")
