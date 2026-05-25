"""代理转发：将请求转发到上游 AI API，含重试和故障切换逻辑"""

import time
import json
import asyncio
import httpx
from akm.key_pool import pick_key_async, pick_wildcard_key_async, mark_rate_limited, set_status
from akm.db import get_connection


# 最大尝试 key 数量，防止无限循环
MAX_KEY_TRIES = 20
# 5xx 最大重试次数（单个 key）
MAX_RETRIES_PER_KEY = 2
# 重试退避基础等待秒数
RETRY_BACKOFF_BASE = 0.5


def _diagnose_no_key(model: str) -> str:
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
        elif active == 0:
            parts.append("所有 Key 均被禁用或限流")
        else:
            parts.append("没有 Key 的 models 匹配该模型，也没有 models='*' 的通配 Key")
    return " | ".join(parts)


def _sse_to_json(sse_text: str) -> str:
    """将 SSE 流式响应文本转换为标准 JSON 响应格式"""
    content = ""
    reasoning = ""
    model = ""
    msg_id = ""
    usage = None
    finish_reason = "stop"

    for line in sse_text.split("\n"):
        line = line.strip()
        if not line.startswith("data: ") or line.startswith("data: [DONE]"):
            continue
        try:
            chunk = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if not model:
            model = chunk.get("model", "")
        if not msg_id:
            msg_id = chunk.get("id", "")
        if "usage" in chunk and chunk["usage"]:
            usage = chunk["usage"]
        if chunk.get("choices"):
            delta = chunk["choices"][0].get("delta", {})
            if delta.get("content"):
                content += delta["content"]
            if delta.get("reasoning_content"):
                reasoning += delta["reasoning_content"]
            if chunk["choices"][0].get("finish_reason"):
                finish_reason = chunk["choices"][0]["finish_reason"]

    result = {
        "id": msg_id,
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
    }
    if reasoning:
        result["choices"][0]["message"]["reasoning_content"] = reasoning
    if usage:
        result["usage"] = usage
    return json.dumps(result, ensure_ascii=False)


def _build_upstream_url(base_url: str | None, api_path: str = "chat/completions") -> str:
    """从供应商 base_url 拼接 API 路径"""
    if not base_url:
        base_url = "https://api.openai.com"  # 兜底默认值
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/{api_path}"
    return f"{base}/v1/{api_path}"


async def forward_request(
    body: dict,
    client: httpx.AsyncClient,
    log_callback=None,
    api_path: str = "chat/completions",
) -> dict:
    """转发请求到上游 AI API，自动处理故障切换

    客户端 stream=true 时流式返回，否则非流式返回。
    内部统一向上游发 stream=true，边收边拼，减少首 token 延迟。
    """
    model = body.get("model", "")
    client_wants_stream = body.get("stream", False)
    tries = 0
    tried_aliases: set[str] = set()
    use_fallback = False  # 精确匹配耗尽后启用通配符兜底

    while tries < MAX_KEY_TRIES:
        # ── 两阶段 key 选择：精确匹配 → 通配符兜底 ──
        if use_fallback:
            key = await pick_wildcard_key_async()
        else:
            key = await pick_key_async(model)

        if key is None:
            if not use_fallback:
                # 精确匹配无可用 key，尝试通配符兜底
                use_fallback = True
                continue
            # 兜底也无可用 key
            return {
                "status_code": 503,
                "body": "",
                "key_alias": "",
                "provider": "",
                "model": model,
                "error": _diagnose_no_key(model),
                "latency_ms": 0,
            }

        # 避免重复尝试同一个 key（5xx 不会禁用 key，可能被反复选中）
        if key["alias"] in tried_aliases:
            if not use_fallback:
                use_fallback = True
            continue
        tried_aliases.add(key["alias"])

        tries += 1
        url = _build_upstream_url(key.get("base_url") or "https://api.openai.com", api_path)
        auth_template = key.get("auth_header", "Bearer {api_key}")
        headers = {
            "Authorization": auth_template.format(api_key=key["api_key"]),
            "Content-Type": "application/json",
        }

        # ── 内部统一向上游发 stream=true，边收边拼，减少首 token 延迟 ──
        upstream_body = dict(body)
        upstream_body["stream"] = True

        last_error = ""
        for attempt in range(1 + MAX_RETRIES_PER_KEY):
            t0 = time.time()
            try:
                req = client.build_request("POST", url, json=upstream_body, headers=headers, timeout=120)
                resp = await client.send(req, stream=True)
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = str(e)
                if attempt < MAX_RETRIES_PER_KEY:
                    await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                break
            except Exception as e:
                last_error = str(e)
                break

            # ── 错误状态码处理 ──
            if resp.status_code == 429:
                mark_rate_limited(key["alias"])
                last_error = f"429 Too Many Requests (key: {key['alias']})"
                await resp.aclose()
                break

            if resp.status_code == 402:
                set_status(key["alias"], "disabled")
                last_error = f"402 Payment Required (key: {key['alias']} 已禁用)"
                await resp.aclose()
                break

            if resp.status_code in (401, 403):
                set_status(key["alias"], "disabled")
                last_error = f"{resp.status_code} 认证失败 (key: {key['alias']} 已禁用)"
                await resp.aclose()
                break

            if 500 <= resp.status_code < 600:
                last_error = f"{resp.status_code} Server Error"
                await resp.aclose()
                if attempt < MAX_RETRIES_PER_KEY:
                    await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                break

            # ── 成功：客户端流式 → 透传 resp；非流式 → 读完拼接返回 ──
            if client_wants_stream:
                return {
                    "stream": True,
                    "status_code": 200,
                    "response": resp,
                    "key_alias": key["alias"],
                    "provider": key["provider"],
                    "model": model,
                }

            # 非流式客户端：读完所有 chunk，拼接后返回
            chunks = []
            try:
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
            except Exception as e:
                last_error = f"读取流式响应失败: {e}"
                await resp.aclose()
                if attempt < MAX_RETRIES_PER_KEY:
                    await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                break
            await resp.aclose()

            latency = int((time.time() - t0) * 1000)
            resp_body = b"".join(chunks).decode("utf-8", errors="replace")
            # chat/completions 将 SSE 流转为 JSON，其他路径透传原始响应
            if api_path == "chat/completions":
                json_body = _sse_to_json(resp_body)
            else:
                json_body = resp_body
            return {
                "status_code": resp.status_code,
                "body": json_body,
                "key_alias": key["alias"],
                "provider": key["provider"],
                "model": model,
                "error": "",
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

    return {
        "status_code": 502,
        "body": "",
        "key_alias": "",
        "provider": "",
        "model": model,
        "error": "所有 key 均已尝试但均失败",
        "latency_ms": 0,
    }


async def test_key_connectivity(key: dict) -> dict:
    """测试单个 key 的连通性，发送一条最小请求

    返回: {"ok": bool, "url": str, "model": str, "status_code": int,
           "latency_ms": int, "error": str, "response_body": str}
    """
    url = _build_upstream_url(key["base_url"])
    model = key.get("models", "*").split(",")[0].strip() or "gpt-3.5-turbo"
    auth_template = key.get("auth_header", "Bearer {api_key}")
    headers = {
        "Authorization": auth_template.format(api_key=key["api_key"]),
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }

    def _result(**kw):
        base = {"ok": False, "url": url, "model": model,
                "status_code": 0, "latency_ms": 0, "error": "", "response_body": ""}
        base.update(kw)
        return base

    t0 = time.time()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=body, headers=headers, timeout=30)
            latency = int((time.time() - t0) * 1000)
            resp_text = resp.text[:500]
            if resp.status_code == 200:
                return _result(ok=True, status_code=200, latency_ms=latency)
            if resp.status_code == 429:
                return _result(status_code=429, latency_ms=latency, error="429 限流")
            if resp.status_code in (401, 403):
                return _result(status_code=resp.status_code, latency_ms=latency,
                               error="认证失败，key 无效", response_body=resp_text)
            if resp.status_code == 402:
                return _result(status_code=402, latency_ms=latency,
                               error="余额不足", response_body=resp_text)
            # 解析响应体中的错误信息
            try:
                detail = resp.json()
                err_msg = str(detail.get("error", {}).get("message", f"HTTP {resp.status_code}"))
            except Exception:
                err_msg = f"HTTP {resp.status_code}"
            return _result(status_code=resp.status_code, latency_ms=latency,
                           error=err_msg, response_body=resp_text)
    except httpx.TimeoutException:
        return _result(error="请求超时")
    except httpx.ConnectError as e:
        return _result(error=f"连接失败: {e}")
    except Exception as e:
        return _result(error=str(e))
