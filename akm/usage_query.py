"""用量查询：后端 HTTP 请求执行 + JS 提取器

查询脚本格式（JS 配置 JSON）:
{
  "request": {
    "url": "{{baseUrl}}/v1/usage",
    "method": "GET",
    "headers": {"Authorization": "Bearer {{apiKey}}"}
  },
  "extractor": "function(response) { ... }"
}

模板变量：{{baseUrl}} -> key 的 base_url，{{apiKey}} -> key 的 api_key，{{date}} -> 当前日期 YYYY-MM-DD

后端负责发送 HTTP 请求并尝试在本地执行 JS extractor。
extractor 执行优先使用 Node.js 子进程，不可用时回退到内置 dukpy 或标记失败。
"""

import json
import re
import subprocess
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

# ── JS extractor 后端执行 ─────────────────────────────────────

_JS_TEMPLATE = """
const __response__ = {response_json};
const __extractor__ = {extractor_js};
const __result__ = __extractor__(__response__);
console.log(JSON.stringify(__result__));
""".strip()


def _execute_extractor_via_node(extractor_js: str, raw_response: dict) -> dict | None:
    """通过 Node.js 子进程执行 JS extractor，返回提取结果"""
    script = _JS_TEMPLATE.format(
        response_json=json.dumps(raw_response, ensure_ascii=False),
        extractor_js=extractor_js,
    )
    try:
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return None


def _execute_extractor_builtin(extractor_js: str, raw_response: dict) -> dict | None:
    """尝试解析常见简单 extractor 模式，在 Python 端直接提取

    仅支持纯字段映射型 extractor，不支持带分支/循环/方法调用的复杂逻辑。
    如果无法安全解析则返回 None。
    """
    # 提取 extractor 函数体：function(response) { return { ... } }
    m = re.search(
        r"function\s*\(\s*response\s*\)\s*\{(.*)\}",
        extractor_js, re.DOTALL | re.MULTILINE,
    )
    if not m:
        return None
    body = m.group(1).strip()
    # 只处理简单 return { ... } 形式
    ret_m = re.match(r"return\s*(\{.*\});?\s*$", body, re.DOTALL)
    if not ret_m:
        return None
    obj_literal = ret_m.group(1)

    # 将 JS 对象字面量转为 Python dict
    result = {}
    # 匹配 key: value 对（简单值，不支持嵌套对象/函数调用）
    pair_pattern = re.compile(
        r"(\w+)\s*:\s*"
        r"("
        r"(?:response\?\.\w+(?:\??\.\w+)*)"
        r"|(?:\w+(?:\??\.\w+)*)"
        r"|\"(?:[^\"\\]|\\.)*\""
        r"|'(?:[^'\\]|\\.)*'"
        r"|true|false|null"
        r"|-?\d+(?:\.\d+)?"
        r")",
    )
    # 处理 ?? 链和可选链表达式
    # 将 response?.a?.b ?? fallback 拆开
    for m_pair in pair_pattern.finditer(obj_literal):
        key = m_pair.group(1)
        val_expr = m_pair.group(2).strip()

        # 解析 ?? 表达式
        parts = re.split(r"\?\?\s*", val_expr)
        resolved = None
        for part in parts:
            part = part.strip()
            val = _resolve_js_expr(part, raw_response)
            if val is not None and val != "" and val != 0:
                resolved = val
                break
        if resolved is None:
            resolved = _resolve_js_expr(parts[-1].strip(), raw_response)
        result[key] = resolved

    if not result:
        return None
    return result


def _resolve_js_expr(expr: str, raw_response: dict):
    """解析简单 JS 表达式（可选链 + 属性访问）"""
    expr = expr.strip()

    # boolean/null 字面量
    if expr == "true":
        return True
    if expr == "false":
        return False
    if expr == "null":
        return None
    if expr == "undefined":
        return None

    # 数字
    try:
        return int(expr)
    except (ValueError, TypeError):
        pass
    try:
        return float(expr)
    except (ValueError, TypeError):
        pass

    # 字符串字面量
    str_m = re.match(r'^"([^"]*)"$', expr) or re.match(r"^'([^']*)'$", expr)
    if str_m:
        return str_m.group(1)

    # response?.a?.b?.c 或 response?.a[0]?.b 可选链
    chain = expr
    current = raw_response
    if chain.startswith("response"):
        chain = chain[len("response"):]
    else:
        return None

    # 分段：?.prop 或 ?.[index] 或 ?.prop?.prop
    segments = re.findall(r"\?\.(\w+)|\[\s*(\d+)\s*\]|\.(\w+)", chain)
    if not segments and chain.startswith("?."):
        # 去掉开头的 ?.
        pass

    for seg in segments:
        prop_name = seg[0] or seg[2]
        index = seg[1]
        if current is None or not isinstance(current, dict):
            return None
        if index:
            # 数组索引 - response 可能不是数组，但尝试处理 balance_infos[0]
            val = current.get(prop_name) if prop_name else current
            if isinstance(val, list) and index:
                idx = int(index)
                current = val[idx] if idx < len(val) else None
            else:
                current = None
        elif prop_name:
            current = current.get(prop_name)
        if current is None:
            return None
    return current


def _execute_extractor(extractor_js: str, raw_response: dict) -> dict | None:
    """执行 JS extractor：优先 Node.js 子进程，回退内置解析"""
    if not extractor_js or not raw_response:
        return None
    if not isinstance(raw_response, dict):
        return None
    # 优先 Node.js（最可靠）
    result = _execute_extractor_via_node(extractor_js, raw_response)
    if result is not None:
        return result
    # 回退：内置 Python 解析（仅支持简单模式）
    result = _execute_extractor_builtin(extractor_js, raw_response)
    return result


def _render_template(template: str, key: dict) -> str:
    """渲染模板变量 {{baseUrl}}、{{apiKey}} 和 {{date}}"""
    result = template
    base_url = (key.get("base_url") or "").strip().rstrip("/")
    api_key = key.get("api_key", "") or ""
    result = result.replace("{{baseUrl}}", base_url)
    result = result.replace("{{apiKey}}", api_key)
    result = result.replace("{{date}}", datetime.now().strftime("%Y-%m-%d"))
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
