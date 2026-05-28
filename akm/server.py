"""FastAPI 服务：接收 OpenAI 兼容请求并代理转发"""

import json
import logging
import asyncio
import time
import os
import sys
import traceback
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request, Query, UploadFile
from fastapi.responses import JSONResponse, Response, HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from akm.proxy import forward_request, test_key_connectivity
from akm.key_pool import (
    list_keys, add_key, get_key, set_api_key,
    set_priority, set_base_url, set_status, remove_key,
)
from akm.audit import write_log_async, list_logs, count_logs
from akm.config import load_config, save_config, get as config_get
from akm.agent import register_agent, unregister_agent, list_agents, load_custom_agents
from akm.plugins.plugin_manager import PluginManager


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
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    app.state.http_client = httpx.AsyncClient(
        limits=limits,
        timeout=httpx.Timeout(120.0, connect=10.0),
    )
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(title="AI Key Manager", version="0.1.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理：500 时返回详细报错信息，方便本地排查"""
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "traceback": traceback.format_exc()},
    )
logger = logging.getLogger("akm")

# 静态文件
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

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
    if "provider" in body and body["provider"]:
        from akm.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE keys SET provider = ? WHERE alias = ?", (body["provider"], alias))
        conn.commit()
        conn.close()
    if "models" in body:
        from akm.db import get_connection
        conn = get_connection()
        # 规范 models 字段：去除每个模型名前后空格
        models = ",".join(m.strip() for m in body["models"].split(",") if m.strip()) if body["models"] != "*" else body["models"]
        conn.execute("UPDATE keys SET models = ? WHERE alias = ?", (models, alias))
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


@app.get("/api/keys/export")
async def api_export_keys():
    """导出所有 Key 配置（含完整 api_key），用于备份迁移"""
    keys = list_keys()
    # list_keys 返回的是脱敏后的 api_key，需要重新获取完整值
    full_keys = []
    for k in keys:
        full = get_key(k["alias"])
        if full:
            full_keys.append(full)
    return {"data": full_keys}


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
        # 安全提取缓存 token：上游可能返回非字典类型（字符串、null 等）
        cached = 0
        for key in ("prompt_tokens_details", "input_tokens_details"):
            details = usage.get(key)
            if isinstance(details, dict):
                cached = details.get("cached_tokens", 0)
                if cached:
                    break
        if total == 0 and (prompt > 0 or completion > 0):
            total = prompt + completion
        if total > 0:
            return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total, "cached_tokens": cached}
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
            except (json.JSONDecodeError, TypeError):
                pass
    if usage:
        return _parse_usage(usage)

    return None


# ── 统计内存缓存（30 秒过期，减少重复解析）──
_stats_cache: dict[str, tuple[float, dict]] = {}

@app.get("/api/stats")
async def api_stats(days: int = Query(default=1, ge=1, le=365)):
    """Token 统计概览，可按天数筛选（30 秒内存缓存）"""
    return _get_stats(days)


def _get_stats(days: int) -> dict:
    """带缓存的统计查询"""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    cache_key = f"days={days}"
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
                  provider, model, key_alias, timestamp, response_body
           FROM audit_logs
           WHERE timestamp >= datetime(date('now', 'localtime', ? || ' days'))
           ORDER BY id DESC""",
        (str(day_offset),),
    ).fetchall()
    conn.close()

    total_requests = len(rows)
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
        p = r.get("prompt_tokens", 0) or 0
        c = r.get("completion_tokens", 0) or 0
        t = r.get("total_tokens", 0) or 0
        cached = r.get("cached_tokens", 0) or 0
        # 兼容旧数据：列值为 0 但有 response_body 时，仍从 body 提取
        if not t and r.get("response_body"):
            tokens = _extract_tokens(r["response_body"])
            if tokens:
                p = tokens.get("prompt_tokens", 0) or p
                c = tokens.get("completion_tokens", 0) or c
                t = tokens.get("total_tokens", 0) or t
                cached = tokens.get("cached_tokens", 0) or cached

        provider = r.get("provider", "unknown")
        model = r.get("model", "unknown")
        key_alias = r.get("key_alias", "unknown")
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
    status: str = Query(default="all"),
    key_alias: str = Query(default=""),
    days: int = Query(default=0, ge=0, le=365),
):
    """查询审计日志 API，支持分页、排序、过滤空记录、状态筛选、Key筛选和时间范围，返回 JSON"""
    logs = list_logs(provider=provider, limit=limit, offset=offset, order=order, hide_empty=hide_empty, status=status, key_alias=key_alias, days=days)
    total = count_logs(provider=provider, hide_empty=hide_empty, status=status, key_alias=key_alias, days=days)
    # 为每条日志附加 token 用量信息（优先读列，兼容旧数据才解析 body）
    for log in logs:
        p = log.get("prompt_tokens", 0) or 0
        c = log.get("completion_tokens", 0) or 0
        t = log.get("total_tokens", 0) or 0
        cached = log.get("cached_tokens", 0) or 0
        if not t and log.get("response_body"):
            tokens = _extract_tokens(log["response_body"])
            if tokens:
                p = tokens.get("prompt_tokens", 0) or p
                c = tokens.get("completion_tokens", 0) or c
                t = tokens.get("total_tokens", 0) or t
                cached = tokens.get("cached_tokens", 0) or cached
        log["prompt_tokens"] = p
        log["completion_tokens"] = c
        log["total_tokens"] = t
        log["cached_tokens"] = cached
    return {"data": logs, "total": total}


@app.get("/api/logs/size")
async def api_logs_size():
    """返回数据库文件大小（字节）"""
    from akm.db import get_db_path
    try:
        size = os.path.getsize(get_db_path())
        return {"size": size}
    except OSError:
        return {"size": 0}


@app.post("/api/logs/clean")
async def api_clean_logs(request: Request):
    """清空审计日志 API"""
    from datetime import datetime as _dt
    from akm.audit import clean_logs as _clean_logs
    body = await request.json()
    before = body.get("before", _dt.now().strftime("%Y-%m-%d"))
    try:
        count = _clean_logs(before)
        return {"ok": True, "deleted": count}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})


@app.post("/api/logs/clean-bodies")
async def api_clean_log_bodies():
    """清空审计日志请求体/响应体内容，保留统计字段与元数据"""
    from akm.audit import clean_log_bodies as _clean_log_bodies
    count = _clean_log_bodies()
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
    return HTMLResponse(_render_template("about.html", title="关于", active="about"))


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
    return await _handle_ai_request(request, "chat/completions")


@app.post("/v1/responses")
@app.post("/responses")
async def responses(request: Request):
    """OpenAI Responses API 端点"""
    return await _handle_ai_request(request, "responses")


async def _handle_ai_request(request: Request, api_path: str):
    """通用 AI API 请求处理：chat/completions 和 responses 复用"""
    content_type = request.headers.get("Content-Type", "")
    if "application/json" not in content_type:
        return JSONResponse(
            status_code=415,
            content={"detail": "不支持的 Content-Type，需要 application/json"},
        )

    body = await request.json()
    # ── 提取关键请求头用于溯源（User-Agent 区分 opencode/codex/curl）──
    # starlette 的 headers 是大小写不敏感的 MutableHeaders
    _trace_headers = {k.lower(): v for k, v in request.headers.items()}
    # 只保留有溯源价值的头，去除 Authorization（太长且敏感）
    _trace_keys = [
        "user-agent", "x-request-id", "x-stainless-os", "x-stainless-lang",
        "x-stainless-package-version", "x-stainless-runtime", "x-stainless-runtime-version",
        "x-forwarded-for", "x-real-ip", "origin", "referer", "host",
    ]
    request_headers_json = json.dumps(
        {k: _trace_headers[k] for k in _trace_keys if k in _trace_headers},
        ensure_ascii=False,
    )
    # 读取日志存储配置
    cfg = load_config()
    save_request_body = cfg.get("log_request_body", False)
    save_response_body = cfg.get("log_response_body", False)
    result = await forward_request(body, request.app.state.http_client, api_path=api_path)

    # ── 流式响应：逐块转发，边收边发 ──
    if result.get("stream"):
        resp = result["response"]
        adapter = result.get("adapter")  # 协议转换适配器（非 None 时需转换）
        key_alias = result["key_alias"]
        provider = result["provider"]
        model = result["model"]

        async def stream_generator():
            chunks = []
            t0 = __import__("time").time()
            stream_error = ""
            try:
                if adapter:
                    # 协议转换：边收 Chat SSE 边转 Responses SSE
                    async for line in adapter.convert_sse_stream(resp.aiter_bytes()):
                        chunk = line.encode("utf-8") if isinstance(line, str) else line
                        chunks.append(chunk)
                        yield chunk
                else:
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        yield chunk
            except Exception as e:
                stream_error = f"上游连接中断: {e}"
                logger.warning(
                    f"[{key_alias}] {provider} model={model} → {stream_error}"
                )
            finally:
                await resp.aclose()
                latency = int((__import__("time").time() - t0) * 1000)
                body_str = b"".join(chunks).decode("utf-8", errors="replace")
                status = 200 if not stream_error else 502
                tokens = _extract_tokens(body_str) or {}
                asyncio.create_task(write_log_async({
                    "provider": provider, "key_alias": key_alias, "model": model,
                    "request_body": json.dumps(body, ensure_ascii=False) if save_request_body else "",
                    "response_body": body_str if save_response_body else "", "status_code": status,
                    "latency_ms": latency, "error": stream_error,
                    "request_headers": request_headers_json,
                    "prompt_tokens": tokens.get("prompt_tokens", 0),
                    "completion_tokens": tokens.get("completion_tokens", 0),
                    "total_tokens": tokens.get("total_tokens", 0),
                    "cached_tokens": tokens.get("cached_tokens", 0),
                }))
                logger.info(
                    f"[{key_alias}] {provider} model={model} → {status} {latency}ms (stream)"
                )

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
    # 只有真正转发了的请求才写审计日志（没有可用 key 的 503 不记录）
    if result["status_code"] != 503:
        tokens = _extract_tokens(result.get("body", "")) or {}
        asyncio.create_task(write_log_async({
            "provider": result["provider"],
            "key_alias": result["key_alias"],
            "model": result["model"],
            "request_body": json.dumps(body, ensure_ascii=False) if save_request_body else "",
            "response_body": result["body"] if save_response_body else "",
            "status_code": result["status_code"],
            "latency_ms": result["latency_ms"],
            "error": result["error"],
            "request_headers": request_headers_json,
            "prompt_tokens": tokens.get("prompt_tokens", 0),
            "completion_tokens": tokens.get("completion_tokens", 0),
            "total_tokens": tokens.get("total_tokens", 0),
            "cached_tokens": tokens.get("cached_tokens", 0),
        }))

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
