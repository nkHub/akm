"""代理转发：将请求转发到上游 AI API，含重试和故障切换逻辑"""

import time
import json
import httpx
from akm.key_pool import pick_key_async, mark_rate_limited, set_status


# 最大尝试 key 数量，防止无限循环
MAX_KEY_TRIES = 20
# 5xx 最大重试次数（单个 key）
MAX_RETRIES_PER_KEY = 2


def _build_upstream_url(base_url: str) -> str:
    """从供应商 base_url 拼接 chat/completions 路径"""
    base = base_url.rstrip("/")
    # 如果 base_url 已经包含 /v1，则直接追加
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


async def forward_request(
    body: dict,
    client: httpx.AsyncClient,
    log_callback=None,
) -> dict:
    """转发请求到上游 AI API，自动处理故障切换

    返回: {"status_code": int, "body": str, "key_alias": str,
           "provider": str, "model": str, "error": str, "latency_ms": int}
    """
    model = body.get("model", "")
    tries = 0

    while tries < MAX_KEY_TRIES:
        key = await pick_key_async(model)
        if key is None:
            return {
                "status_code": 503,
                "body": "",
                "key_alias": "",
                "provider": "",
                "model": model,
                "error": "没有可用的 API key",
                "latency_ms": 0,
            }

        tries += 1
        url = _build_upstream_url(key["base_url"])
        auth_template = key.get("auth_header", "Bearer {api_key}")
        headers = {
            "Authorization": auth_template.format(api_key=key["api_key"]),
            "Content-Type": "application/json",
        }

        last_error = ""
        for attempt in range(1 + MAX_RETRIES_PER_KEY):
            t0 = time.time()
            try:
                resp = await client.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=120,
                )
                latency = int((time.time() - t0) * 1000)
                resp_body = resp.text

                if resp.status_code == 429:
                    mark_rate_limited(key["alias"])
                    last_error = f"429 Too Many Requests (key: {key['alias']})"
                    break  # 跳出重试循环，换 key

                if resp.status_code == 402:
                    set_status(key["alias"], "disabled")
                    last_error = f"402 Payment Required (key: {key['alias']} 已禁用)"
                    break

                if resp.status_code in (401, 403):
                    set_status(key["alias"], "disabled")
                    last_error = f"{resp.status_code} 认证失败 (key: {key['alias']} 已禁用)"
                    break

                if 500 <= resp.status_code < 600:
                    last_error = f"{resp.status_code} Server Error"
                    if attempt < MAX_RETRIES_PER_KEY:
                        continue  # 重试同一 key
                    else:
                        break  # 重试耗尽，换 key

                # 成功
                return {
                    "status_code": resp.status_code,
                    "body": resp_body,
                    "key_alias": key["alias"],
                    "provider": key["provider"],
                    "model": model,
                    "error": "",
                    "latency_ms": latency,
                }

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = str(e)
                if attempt >= MAX_RETRIES_PER_KEY:
                    break

            except Exception as e:
                last_error = str(e)
                break

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
