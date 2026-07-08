"""用量查询：后端 HTTP 请求执行 + 前端 JS 提取器

查询脚本格式（JS 配置 JSON）:
{
  "request": {
    "url": "{{baseUrl}}/v1/usage",
    "method": "GET",
    "headers": {"Authorization": "Bearer {{apiKey}}"}
  },
  "extractor": "function(response) { ... }"
}

模板变量：{{baseUrl}} -> key 的 base_url，{{apiKey}} -> key 的 api_key

后端仅负责发送 HTTP 请求并返回原始响应，不做任何字段提取。
数据提取完全由前端的 JS extractor 脚本负责。
"""

import json
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx


def _render_template(template: str, key: dict) -> str:
    """渲染模板变量 {{baseUrl}} 和 {{apiKey}}"""
    result = template
    base_url = (key.get("base_url") or "").strip().rstrip("/")
    api_key = key.get("api_key", "") or ""
    result = result.replace("{{baseUrl}}", base_url)
    result = result.replace("{{apiKey}}", api_key)
    return result


def _render_headers(headers: dict, key: dict) -> dict:
    """渲染请求头中的模板变量"""
    return {k: _render_template(v, key) for k, v in headers.items()}


async def execute_query_script(key: dict, script_config: dict, http_client=None) -> dict:
    """根据脚本配置执行一次 HTTP 请求，返回原始响应

    参数:
        key: key 完整信息（含 api_key, base_url 等）
        script_config: 查询脚本配置 JSON
        http_client: 可选，复用已有的 httpx 客户端

    返回统一结果 dict，包含:
        ok, status_code, latency_ms, queried_at, error, raw_response
    """
    req_cfg = script_config.get("request", {})
    if not req_cfg:
        return _query_result(False, error="脚本缺少 request 配置")

    url_template = req_cfg.get("url", "")
    if not url_template:
        return _query_result(False, error="脚本缺少 request.url")

    method = req_cfg.get("method", "GET").upper()
    headers_template = req_cfg.get("headers", {})

    url = _render_template(url_template, key)
    headers = _render_headers(headers_template, key)

    start = time.time()
    own_client = http_client is None
    if own_client:
        limits = httpx.Limits(max_keepalive_connections=2, max_connections=4)
        timeout = httpx.Timeout(30.0, connect=10.0)
        http_client = httpx.AsyncClient(limits=limits, timeout=timeout)

    try:
        if method == "GET":
            resp = await http_client.get(url, headers=headers)
        elif method == "POST":
            body = req_cfg.get("body", {})
            resp = await http_client.post(url, headers=headers, json=body)
        else:
            resp = await http_client.request(method, url, headers=headers)

        latency_ms = int((time.time() - start) * 1000)
        status_code = resp.status_code
        raw_text = resp.text or ""
        try:
            raw_json = json.loads(raw_text) if raw_text else {}
        except json.JSONDecodeError:
            raw_json = {"_raw_text": raw_text}

        http_ok = 200 <= status_code < 300

        result = _query_result(
            ok=http_ok,
            status_code=status_code,
            latency_ms=latency_ms,
            raw_response=raw_json,
            error="" if http_ok else f"HTTP {status_code}: {raw_text[:200]}",
        )
    except httpx.TimeoutException:
        result = _query_result(False, error="请求超时")
    except Exception as e:
        result = _query_result(False, error=str(e))
    finally:
        if own_client:
            await http_client.aclose()

    return result


def _query_result(
    ok: bool,
    status_code: int = 0,
    latency_ms: int = 0,
    raw_response=None,
    error: str = "",
) -> dict:
    """构建统一的查询结果 dict（无 extracted 字段，全部由前端 extractor 决定）"""
    return {
        "ok": ok,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "queried_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "error": error,
        "raw_response": raw_response,
    }
