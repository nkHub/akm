"""FastAPI 服务：接收 OpenAI 兼容请求并代理转发"""

import json
import logging
import asyncio
import os
import httpx
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, Response, HTMLResponse
from fastapi.templating import Jinja2Templates
from akm.proxy import forward_request, test_key_connectivity
from akm.key_pool import (
    list_keys, add_key, get_key, set_api_key,
    set_priority, set_base_url, set_status, remove_key,
)
from akm.audit import write_log, list_logs, count_logs

app = FastAPI(title="AI Key Manager", version="0.1.0")
logger = logging.getLogger("akm")

# 简易模板引擎 — 读取模板文件并做变量替换
import re as _re
_tpl_dir = os.path.join(os.path.dirname(__file__), "templates")

def _render_template(name: str, **kwargs) -> str:
    """读取模板文件，替换 {{ var }} 占位符，支持 {% extends %}, {% include %}, {% block %}"""
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


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok"}


# ── Key 管理 API ───────────────────────────────────────────

def _mask_key(api_key: str) -> str:
    """脱敏显示 API key，只保留前 8 位"""
    if not api_key:
        return ""
    return api_key[:8] + "..." if len(api_key) > 8 else api_key


@app.get("/api/keys")
async def api_list_keys():
    """列出所有 key（api_key 脱敏）"""
    keys = list_keys()
    for k in keys:
        k["api_key"] = _mask_key(k["api_key"])
    return {"data": keys}


@app.post("/api/keys")
async def api_add_key(request: Request):
    """添加一个新的 API key"""
    body = await request.json()
    alias = body.get("alias", "").strip()
    provider = body.get("provider", "").strip()
    api_key = body.get("api_key", "").strip()

    if not alias or not provider or not api_key:
        return JSONResponse(status_code=400, content={"detail": "alias、provider、api_key 为必填项"})

    try:
        add_key(
            alias=alias,
            provider=provider,
            api_key=api_key,
            base_url=body.get("base_url") or None,
            models=body.get("models", "*"),
            auth_header=body.get("auth_header", "Bearer {api_key}"),
            priority=body.get("priority", 0),
        )
        return {"ok": True, "alias": alias}
    except ValueError as e:
        return JSONResponse(status_code=409, content={"detail": str(e)})


@app.put("/api/keys/{alias}")
async def api_update_key(alias: str, request: Request):
    """更新 key 的配置（api_key、priority、base_url、models、auth_header）"""
    existing = get_key(alias)
    if existing is None:
        return JSONResponse(status_code=404, content={"detail": f"Key '{alias}' 不存在"})

    body = await request.json()

    if "api_key" in body and body["api_key"]:
        set_api_key(alias, body["api_key"])
    if "priority" in body:
        set_priority(alias, body["priority"])
    if "base_url" in body:
        set_base_url(alias, body["base_url"])
    if "models" in body:
        from akm.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE keys SET models = ? WHERE alias = ?", (body["models"], alias))
        conn.commit()
        conn.close()
    if "auth_header" in body:
        from akm.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE keys SET auth_header = ? WHERE alias = ?", (body["auth_header"], alias))
        conn.commit()
        conn.close()

    return {"ok": True, "alias": alias}


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

    set_status(alias, status)
    return {"ok": True, "alias": alias, "status": status}


@app.delete("/api/keys/{alias}")
async def api_delete_key(alias: str):
    """删除指定 key"""
    if remove_key(alias):
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


# ── 统计 API ───────────────────────────────────────────────

def _extract_tokens(response_body: str) -> dict | None:
    """从响应体中提取 token 用量，支持普通 JSON 和 SSE 流式响应"""
    if not response_body:
        return None

    # 1. 尝试直接解析为 JSON
    try:
        data = json.loads(response_body)
        usage = data.get("usage", {})
        total = usage.get("total_tokens", 0)
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        if total == 0 and (prompt > 0 or completion > 0):
            total = prompt + completion
        if total > 0:
            return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. 尝试解析 SSE 流式响应（多行 data: {...} 格式）
    usage = None
    for line in response_body.split("\n"):
        line = line.strip()
        if line.startswith("data: ") and not line.startswith("data: [DONE]"):
            try:
                chunk = json.loads(line[6:])  # 去掉 "data: " 前缀
                if "usage" in chunk:
                    usage = chunk["usage"]
            except (json.JSONDecodeError, TypeError):
                pass
    if usage:
        total = usage.get("total_tokens", 0)
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        if total == 0 and (prompt > 0 or completion > 0):
            total = prompt + completion
        if total > 0:
            return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}

    return None


@app.get("/api/stats")
async def api_stats():
    """Token 统计概览"""
    from akm.db import get_connection
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM audit_logs ORDER BY id DESC LIMIT 2000"
    ).fetchall()
    conn.close()

    total_requests = len(rows)
    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    by_provider = {}
    by_model = {}
    by_key = {}
    daily = {}

    for row in rows:
        r = dict(row)
        tokens = _extract_tokens(r.get("response_body", ""))

        # 只统计成功的请求
        provider = r.get("provider", "unknown")
        model = r.get("model", "unknown")
        key_alias = r.get("key_alias", "unknown")
        ts = str(r.get("timestamp", ""))[:10]  # YYYY-MM-DD

        p = tokens.get("prompt_tokens", 0) if tokens else 0
        c = tokens.get("completion_tokens", 0) if tokens else 0
        t = tokens.get("total_tokens", 0) if tokens else 0

        total_prompt += p
        total_completion += c
        total_tokens += t

        # 按供应商
        if provider not in by_provider:
            by_provider[provider] = {"prompt": 0, "completion": 0, "total": 0, "requests": 0}
        by_provider[provider]["prompt"] += p
        by_provider[provider]["completion"] += c
        by_provider[provider]["total"] += t
        by_provider[provider]["requests"] += 1

        # 按模型
        if model not in by_model:
            by_model[model] = {"prompt": 0, "completion": 0, "total": 0, "requests": 0}
        by_model[model]["prompt"] += p
        by_model[model]["completion"] += c
        by_model[model]["total"] += t
        by_model[model]["requests"] += 1

        # 按 key
        if key_alias not in by_key:
            by_key[key_alias] = {"prompt": 0, "completion": 0, "total": 0, "requests": 0}
        by_key[key_alias]["prompt"] += p
        by_key[key_alias]["completion"] += c
        by_key[key_alias]["total"] += t
        by_key[key_alias]["requests"] += 1

        # 按天
        if ts not in daily:
            daily[ts] = {"prompt": 0, "completion": 0, "total": 0, "requests": 0}
        daily[ts]["prompt"] += p
        daily[ts]["completion"] += c
        daily[ts]["total"] += t
        daily[ts]["requests"] += 1

    return {
        "total_requests": total_requests,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "by_provider": by_provider,
        "by_model": by_model,
        "by_key": by_key,
        "daily": dict(sorted(daily.items())),
    }


@app.get("/api/logs")
async def api_logs(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    provider: str = Query(default=None),
):
    """查询审计日志 API，支持分页，返回 JSON"""
    logs = list_logs(provider=provider, limit=limit, offset=offset)
    total = count_logs(provider=provider)
    return {"data": logs, "total": total}


@app.get("/logs")
async def log_viewer(request: Request):
    """审计日志查看页面"""
    return HTMLResponse(_render_template("logs.html", title="审计日志", active="logs"))


@app.get("/settings")
async def settings_page(request: Request):
    """Key 管理页面"""
    return HTMLResponse(_render_template("settings.html", title="设置", active="settings"))


@app.get("/admin")
async def admin_page(request: Request):
    """后台管理 Dashboard 页面"""
    return HTMLResponse(_render_template("dashboard.html", title="统计", active="admin"))


@app.get("/v1/models")
@app.get("/models")
async def list_models():
    """返回所有 active key 支持的模型列表（OpenAI 兼容格式）"""
    data = []
    seen = set()
    for k in list_keys():
        if k["status"] != "active":
            continue
        models = k["models"]
        if models == "*":
            continue  # 通配符跳过，不枚举
        for m in models.split(","):
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
    # 检查 Content-Type 请求头，确保是 application/json
    content_type = request.headers.get("Content-Type", "")
    if "application/json" not in content_type:
        return JSONResponse(
            status_code=415,
            content={"detail": "不支持的 Content-Type，需要 application/json"},
        )

    body = await request.json()

    async with httpx.AsyncClient() as client:
        result = await forward_request(body, client)

    # 写入审计日志
    write_log({
        "provider": result["provider"],
        "key_alias": result["key_alias"],
        "model": result["model"],
        "request_body": json.dumps(body, ensure_ascii=False),
        "response_body": result["body"],
        "status_code": result["status_code"],
        "latency_ms": result["latency_ms"],
        "error": result["error"],
    })

    # 控制台打印请求日志，包含 key alias 信息
    if result["key_alias"]:
        status = result["status_code"]
        elapsed = f"{result['latency_ms']}ms"
        err = f" error={result['error']}" if result["error"] else ""
        logger.info(
            f"[{result['key_alias']}] {result['provider']} "
            f"model={result['model']} → {status} {elapsed}{err}"
        )
    else:
        logger.warning(f"请求失败 model={result['model']} → {result['error']}")

    if result["status_code"] == 503:
        return JSONResponse(
            status_code=503,
            content={"detail": result["error"]},
        )

    if result["status_code"] == 502:
        return JSONResponse(
            status_code=502,
            content={"detail": result["error"]},
        )

    # 透传上游响应
    return Response(
        content=result["body"],
        status_code=result["status_code"],
        media_type="application/json",
    )

