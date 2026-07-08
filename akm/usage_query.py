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

后端负责：
1. 解析 request 模板，构建并发送 HTTP 请求
2. 简单解析响应中常见的余额/用量字段（remaining, balance, isValid 等）
3. 将原始响应和解析结果一起存储

前端负责：
1. 展示脚本编辑器（JS extractor 代码编辑）
2. 手动查询时在浏览器中执行 JS extractor，展示精确提取结果
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


def _deep_find(obj, keys: list[str], default=None):
    """在嵌套的 dict/list 中递归查找第一个匹配 key 的值"""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] is not None:
                return obj[k]
        for v in obj.values():
            result = _deep_find(v, keys, None)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _deep_find(item, keys, None)
            if result is not None:
                return result
    return default


def _simple_extract(response_json) -> dict:
    """从响应中简单提取常见的余额/用量字段（兼容 ccswitch 格式）

    extractor 返回字段（均为可选）：
    - isValid: 布尔值，套餐是否有效
    - invalidMessage: 字符串，失效原因说明
    - remaining: 数字，剩余额度
    - unit: 字符串，单位
    - planName: 字符串，套餐名称
    - total: 数字，总额度
    - used: 数字，已用额度
    - extra: 字符串，扩展字段
    """
    if not isinstance(response_json, dict):
        return {}

    extracted = {}

    # 有效性标记
    is_valid = _deep_find(response_json, ["is_active", "isValid", "is_available", "active", "valid"])
    if is_valid is not None:
        extracted["isValid"] = bool(is_valid)

    # 失效原因
    invalid_msg = _deep_find(response_json, ["message", "error", "error_message", "invalid_reason"])
    if invalid_msg is not None and isinstance(invalid_msg, str):
        extracted["invalidMessage"] = invalid_msg

    # 剩余额度
    remaining = _deep_find(response_json, ["remaining", "balance", "quota_remaining", "granted_balance", "available", "credit_balance"])
    if remaining is not None:
        extracted["remaining"] = remaining

    # 单位
    unit = _deep_find(response_json, ["unit", "currency", "balance_currency", "quota_unit"])
    if unit is not None:
        extracted["unit"] = str(unit)

    # 总额度
    total = _deep_find(response_json, ["total", "total_balance", "quota_total", "total_quota", "hard_limit_usd", "soft_limit_usd"])
    if total is not None:
        extracted["total"] = total

    # 已用额度
    used = _deep_find(response_json, ["used", "usage", "total_usage", "consumed", "spent", "cost"])
    if used is not None:
        extracted["used"] = used

    # 套餐名称
    plan = _deep_find(response_json, ["plan_name", "plan", "subscription_plan", "tier", "product_name"])
    if plan is not None:
        extracted["planName"] = str(plan)

    # 额外字段
    extra = _deep_find(response_json, ["extra", "note", "status", "account_status"])
    if extra is not None:
        extracted["extra"] = str(extra)

    return extracted


async def execute_query_script(key: dict, script_config: dict, http_client=None) -> dict:
    """根据脚本配置执行一次用量查询

    参数:
        key: key 完整信息（含 api_key, base_url 等）
        script_config: 查询脚本配置 JSON
        http_client: 可选，复用已有的 httpx 客户端

    返回统一结果 dict，包含:
        ok, status_code, latency_ms, queried_at, error, raw_response, extracted
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
        extracted = _simple_extract(raw_json) if http_ok else {}
        # 以提取器的 isValid 为准，默认情况下 HTTP 2xx 即视为有效
        is_valid = extracted.get("isValid", http_ok)

        result = _query_result(
            ok=http_ok and is_valid,
            status_code=status_code,
            latency_ms=latency_ms,
            raw_response=raw_json,
            extracted=extracted,
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
    extracted=None,
    error: str = "",
) -> dict:
    """构建统一的查询结果 dict"""
    return {
        "ok": ok,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "queried_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "error": error,
        "raw_response": raw_response,
        "extracted": extracted,
    }
