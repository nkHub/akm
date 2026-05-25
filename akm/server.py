"""FastAPI 服务：接收 OpenAI 兼容请求并代理转发"""

import json
import logging
import asyncio
import httpx
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, Response, HTMLResponse
from akm.proxy import forward_request, test_key_connectivity
from akm.key_pool import (
    list_keys, add_key, get_key, set_api_key,
    set_priority, set_base_url, set_status, remove_key,
)
from akm.audit import write_log, list_logs

app = FastAPI(title="AI Key Manager", version="0.1.0")
logger = logging.getLogger("akm")


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
    """从响应体中提取 token 用量"""
    if not response_body:
        return None
    try:
        data = json.loads(response_body)
        usage = data.get("usage", {})
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        total = usage.get("total_tokens", 0)
        if total == 0 and (prompt > 0 or completion > 0):
            total = prompt + completion
        if total > 0:
            return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
    except (json.JSONDecodeError, TypeError):
        pass
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
    limit: int = Query(default=50, ge=1, le=500),
    provider: str = Query(default=None),
):
    """查询审计日志 API，返回 JSON"""
    logs = list_logs(provider=provider, limit=limit)
    return {"data": logs, "total": len(logs)}


@app.get("/logs", response_class=HTMLResponse)
async def log_viewer():
    """审计日志查看页面"""
    return LOG_PAGE_HTML


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    """Key 管理页面"""
    return SETTINGS_PAGE_HTML


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """后台管理页面"""
    return ADMIN_PAGE_HTML


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


# ── 日志查看页面 HTML ──────────────────────────────────────

LOG_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AKM 审计日志</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
tailwind.config = {
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        surface: { DEFAULT: '#1e1e2e', light: '#262637', hover: '#2d2d40' },
        border: { DEFAULT: '#33334d', light: '#404060' },
      }
    }
  }
}
</script>
<style>
  body { font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace; }
  .status-2xx { background: #166534; color: #86efac; }
  .status-4xx { background: #854d0e; color: #fde68a; }
  .status-5xx { background: #991b1b; color: #fecaca; }
  .status-err { background: #4a044e; color: #e9d5ff; }
  .log-row { transition: background 0.15s; }
  .body-preview { max-height: 200px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; font-size: 12px; }
  .switch-track { width: 44px; height: 24px; border-radius: 12px; transition: background 0.2s; }
  .switch-thumb { width: 18px; height: 18px; border-radius: 50%; transition: transform 0.2s; transform: translateX(3px); }
  .switch-on .switch-thumb { transform: translateX(23px); }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #404060; border-radius: 3px; }
</style>
</head>
<body class="bg-surface text-gray-200 min-h-screen">

<!-- 顶部栏 -->
<header class="sticky top-0 z-20 bg-surface/95 backdrop-blur border-b border-border">
  <div class="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between gap-4 flex-wrap">
    <div class="flex items-center gap-4">
      <h1 class="text-lg font-semibold text-white">AKM 审计日志</h1>
      <nav class="flex items-center gap-1 text-sm">
        <a href="/logs" class="px-2 py-1 rounded text-indigo-400 bg-indigo-400/10">日志</a>
        <a href="/settings" class="px-2 py-1 rounded text-gray-400 hover:text-gray-200 hover:bg-surface-light transition-colors">设置</a>
      </nav>
      <span id="log-count" class="text-xs text-gray-500 bg-surface-light px-2 py-0.5 rounded"></span>
    </div>
    <div class="flex items-center gap-4 flex-wrap">
      <!-- 供应商筛选 -->
      <select id="filter-provider"
              class="bg-surface-light border border-border text-gray-300 text-sm rounded px-3 py-1.5
                     focus:outline-none focus:border-indigo-500 cursor-pointer">
        <option value="">全部供应商</option>
      </select>
      <!-- 刷新开关 -->
      <label id="auto-refresh-toggle" class="flex items-center gap-2 cursor-pointer select-none switch-off">
        <span class="text-xs text-gray-400">自动刷新</span>
        <div class="switch-track bg-gray-600 flex items-center">
          <div class="switch-thumb bg-white"></div>
        </div>
        <span id="refresh-interval" class="text-xs text-gray-500">5s</span>
      </label>
      <!-- 手动刷新 -->
      <button id="btn-refresh" onclick="fetchLogs()"
              class="bg-indigo-600 hover:bg-indigo-500 text-white text-sm px-3 py-1.5 rounded
                     transition-colors cursor-pointer flex items-center gap-1.5">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                d="M4 4v5h5M20 20v-5h-5M4 9a9 9 0 0115.36-5.36M20 15a9 9 0 01-15.36 5.36"/>
        </svg>
        刷新
      </button>
    </div>
  </div>
</header>

<!-- 日志表格 -->
<main class="max-w-7xl mx-auto px-4 py-4">
  <div id="log-table" class="overflow-x-auto rounded-lg border border-border">
    <!-- 动态填充 -->
  </div>
  <div id="empty-state" class="hidden text-center py-16 text-gray-500">
    <svg class="w-12 h-12 mx-auto mb-3 opacity-30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
            d="M9 12h6m-3-3v6m-7 4h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/>
    </svg>
    <p>暂无日志</p>
    <p class="text-xs mt-1 text-gray-600">发送 API 请求后日志将在此显示</p>
  </div>
</main>

<!-- 详情弹窗 -->
<div id="detail-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center p-4">
  <div class="absolute inset-0 bg-black/60" onclick="closeModal()"></div>
  <div class="relative bg-surface-light border border-border rounded-lg w-full max-w-3xl max-h-[85vh] flex flex-col shadow-2xl">
    <div class="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
      <h3 id="modal-title" class="text-sm font-semibold text-white"></h3>
      <button onclick="closeModal()" class="text-gray-400 hover:text-white transition-colors cursor-pointer">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
        </svg>
      </button>
    </div>
    <div class="flex border-b border-border shrink-0">
      <button id="tab-request" onclick="switchTab('request')"
              class="flex-1 px-4 py-2 text-sm text-indigo-400 border-b-2 border-indigo-400
                     transition-colors cursor-pointer bg-surface/50">请求体</button>
      <button id="tab-response" onclick="switchTab('response')"
              class="flex-1 px-4 py-2 text-sm text-gray-400 border-b-2 border-transparent
                     transition-colors cursor-pointer">响应体</button>
    </div>
    <div class="overflow-y-auto p-4 flex-1">
      <pre id="modal-body" class="body-preview text-gray-300 text-xs leading-relaxed"></pre>
    </div>
  </div>
</div>

<script>
const API = '/api/logs';
let autoRefresh = false;
let refreshSec = 5;
let timer = null;
let currentDetail = null;

// 初始化
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('auto-refresh-toggle').addEventListener('click', toggleAutoRefresh);
  document.getElementById('filter-provider').addEventListener('change', fetchLogs);
  loadProviders();
  fetchLogs();
});

// 获取日志数据
async function fetchLogs() {
  const btn = document.getElementById('btn-refresh');
  const icon = btn.querySelector('svg');
  icon.classList.add('animate-spin');
  btn.disabled = true;

  const provider = document.getElementById('filter-provider').value;
  const url = provider ? API + '?limit=200&provider=' + encodeURIComponent(provider) : API + '?limit=200';

  try {
    const res = await fetch(url);
    const json = await res.json();
    renderTable(json.data);
    document.getElementById('log-count').textContent = json.total + ' 条';
  } catch (e) {
    console.error('获取日志失败:', e);
  }

  icon.classList.remove('animate-spin');
  btn.disabled = false;
}

// 渲染表格
function renderTable(logs) {
  const container = document.getElementById('log-table');
  const empty = document.getElementById('empty-state');

  if (!logs || logs.length === 0) {
    container.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }

  empty.classList.add('hidden');

  let html = `<table class="w-full text-sm">
    <thead>
      <tr class="bg-surface-light text-gray-400 text-xs uppercase tracking-wider">
        <th class="text-left px-4 py-2.5 font-medium">时间</th>
        <th class="text-left px-4 py-2.5 font-medium">Alias</th>
        <th class="text-left px-4 py-2.5 font-medium">供应商</th>
        <th class="text-left px-4 py-2.5 font-medium">模型</th>
        <th class="text-center px-4 py-2.5 font-medium w-20">状态</th>
        <th class="text-right px-4 py-2.5 font-medium w-24">延迟</th>
        <th class="text-center px-4 py-2.5 font-medium w-20">详情</th>
      </tr>
    </thead>
    <tbody>`;

  logs.forEach((log, i) => {
    const ts = log.timestamp || '-';
    const alias = log.key_alias || '-';
    const prov = log.provider || '-';
    const model = log.model || '-';
    const code = log.status_code;
    const lat = log.latency_ms || 0;
    const err = log.error || '';
    const hasBody = log.request_body || log.response_body;

    // 状态码样式
    let statusCls = 'status-err';
    if (code >= 200 && code < 300) statusCls = 'status-2xx';
    else if (code >= 400 && code < 500) statusCls = 'status-4xx';
    else if (code >= 500) statusCls = 'status-5xx';
    if (code === 0) statusCls = 'status-err';

    const delayStr = lat >= 1000 ? (lat / 1000).toFixed(1) + 's' : lat + 'ms';
    const delayColor = lat > 5000 ? 'text-red-400' : lat > 2000 ? 'text-yellow-400' : 'text-gray-400';

    html += `<tr class="log-row border-t border-border hover:bg-surface-hover">
      <td class="px-4 py-2.5 text-gray-400 whitespace-nowrap text-xs">${esc(ts)}</td>
      <td class="px-4 py-2.5 text-indigo-300 font-medium">${esc(alias)}</td>
      <td class="px-4 py-2.5 text-gray-300">${esc(prov)}</td>
      <td class="px-4 py-2.5 text-gray-400 text-xs">${esc(model)}</td>
      <td class="px-4 py-2.5 text-center">
        <span class="inline-block px-2 py-0.5 rounded text-xs font-medium ${statusCls}">${code || 'ERR'}</span>
      </td>
      <td class="px-4 py-2.5 text-right text-xs ${delayColor}">${delayStr}</td>
      <td class="px-4 py-2.5 text-center">
        ${hasBody ? `<button onclick="showDetail(${i})" class="text-gray-400 hover:text-indigo-400 transition-colors cursor-pointer">
          <svg class="w-4 h-4 mx-auto" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>
          </svg>
        </button>` : '<span class="text-gray-600">-</span>'}
      </td>
    </tr>`;

    // 错误行
    if (err) {
      html += `<tr class="border-t border-border bg-red-950/20">
        <td colspan="7" class="px-4 py-1.5 text-xs text-red-400">${esc(err)}</td>
      </tr>`;
    }
  });

  html += '</tbody></table>';
  container.innerHTML = html;
  window._logsData = logs;
}

// 显示详情弹窗
function showDetail(idx) {
  const log = window._logsData[idx];
  if (!log) return;
  currentDetail = log;

  document.getElementById('modal-title').textContent =
    '[' + esc(log.key_alias || '-') + '] ' + esc(log.model || '-') + ' — ' + esc(log.timestamp || '-');

  // 默认显示请求体
  switchTab('request');
  document.getElementById('detail-modal').classList.remove('hidden');
}

function switchTab(tab) {
  const log = currentDetail;
  if (!log) return;

  const tabReq = document.getElementById('tab-request');
  const tabRes = document.getElementById('tab-response');
  const bodyEl = document.getElementById('modal-body');

  if (tab === 'request') {
    tabReq.className = 'flex-1 px-4 py-2 text-sm text-indigo-400 border-b-2 border-indigo-400 transition-colors cursor-pointer bg-surface/50';
    tabRes.className = 'flex-1 px-4 py-2 text-sm text-gray-400 border-b-2 border-transparent transition-colors cursor-pointer';
    bodyEl.textContent = formatJson(log.request_body) || '(空)';
  } else {
    tabRes.className = 'flex-1 px-4 py-2 text-sm text-indigo-400 border-b-2 border-indigo-400 transition-colors cursor-pointer bg-surface/50';
    tabReq.className = 'flex-1 px-4 py-2 text-sm text-gray-400 border-b-2 border-transparent transition-colors cursor-pointer';
    bodyEl.textContent = formatJson(log.response_body) || '(空)';
  }
}

function closeModal() {
  document.getElementById('detail-modal').classList.add('hidden');
  currentDetail = null;
}

// 自动刷新开关
function toggleAutoRefresh() {
  autoRefresh = !autoRefresh;
  const toggle = document.getElementById('auto-refresh-toggle');

  if (autoRefresh) {
    toggle.classList.add('switch-on');
    toggle.classList.remove('switch-off');
    toggle.querySelector('.switch-track').classList.remove('bg-gray-600');
    toggle.querySelector('.switch-track').classList.add('bg-indigo-600');
    startTimer();
  } else {
    toggle.classList.remove('switch-on');
    toggle.classList.add('switch-off');
    toggle.querySelector('.switch-track').classList.add('bg-gray-600');
    toggle.querySelector('.switch-track').classList.remove('bg-indigo-600');
    stopTimer();
  }
}

function startTimer() {
  stopTimer();
  timer = setInterval(fetchLogs, refreshSec * 1000);
}

function stopTimer() {
  if (timer) { clearInterval(timer); timer = null; }
}

// 加载供应商列表到筛选下拉
function loadProviders() {
  fetch(API + '?limit=500')
    .then(r => r.json())
    .then(json => {
      const providers = [...new Set(json.data.map(l => l.provider).filter(Boolean))];
      const sel = document.getElementById('filter-provider');
      providers.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p;
        opt.textContent = p;
        sel.appendChild(opt);
      });
    })
    .catch(() => {});
}

// 工具函数
function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatJson(str) {
  if (!str) return '';
  try {
    return JSON.stringify(JSON.parse(str), null, 2);
  } catch {
    return str;
  }
}

// 键盘关闭弹窗
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
});
</script>

</body>
</html>"""


# ── Key 设置页面 HTML ──────────────────────────────────────

SETTINGS_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AKM 设置</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
tailwind.config = {
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        surface: { DEFAULT: '#1e1e2e', light: '#262637', hover: '#2d2d40' },
        border: { DEFAULT: '#33334d', light: '#404060' },
      }
    }
  }
}
</script>
<style>
  body { font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace; }
  .status-active { background: #166534; color: #86efac; }
  .status-disabled { background: #991b1b; color: #fecaca; }
  .status-rate_limited { background: #854d0e; color: #fde68a; }
  .table-row { transition: background 0.15s; }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #404060; border-radius: 3px; }
  .modal-overlay { animation: fadeIn 0.15s ease; }
  .modal-panel { animation: slideUp 0.2s ease; }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
  @keyframes slideUp { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }
</style>
</head>
<body class="bg-surface text-gray-200 min-h-screen">

<!-- 顶部栏 -->
<header class="sticky top-0 z-20 bg-surface/95 backdrop-blur border-b border-border">
  <div class="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between gap-4 flex-wrap">
    <div class="flex items-center gap-4">
      <h1 class="text-lg font-semibold text-white">AKM 设置</h1>
      <nav class="flex items-center gap-1 text-sm">
        <a href="/logs" class="px-2 py-1 rounded text-gray-400 hover:text-gray-200 hover:bg-surface-light transition-colors">日志</a>
        <a href="/settings" class="px-2 py-1 rounded text-indigo-400 bg-indigo-400/10">设置</a>
      </nav>
    </div>
    <button onclick="openAddModal()"
            class="bg-indigo-600 hover:bg-indigo-500 text-white text-sm px-3 py-1.5 rounded
                   transition-colors cursor-pointer flex items-center gap-1.5">
      <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
      </svg>
      添加 Key
    </button>
  </div>
</header>

<!-- Key 列表 -->
<main class="max-w-7xl mx-auto px-4 py-4">
  <div id="key-list">
    <!-- 动态填充 -->
  </div>
  <div id="empty-state" class="hidden text-center py-16 text-gray-500">
    <svg class="w-12 h-12 mx-auto mb-3 opacity-30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
            d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-4.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"/>
    </svg>
    <p>暂无 Key</p>
    <p class="text-xs mt-1 text-gray-600">点击右上角「添加 Key」开始配置</p>
  </div>
</main>

<!-- 添加/编辑弹窗 -->
<div id="key-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center p-4 modal-overlay">
  <div class="absolute inset-0 bg-black/60" onclick="closeKeyModal()"></div>
  <div class="relative bg-surface-light border border-border rounded-lg w-full max-w-lg shadow-2xl modal-panel">
    <div class="flex items-center justify-between px-4 py-3 border-b border-border">
      <h3 id="modal-title-text" class="text-sm font-semibold text-white">添加 Key</h3>
      <button onclick="closeKeyModal()" class="text-gray-400 hover:text-white transition-colors cursor-pointer">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
        </svg>
      </button>
    </div>
    <form id="key-form" onsubmit="submitKey(event)" class="p-4 space-y-3">
      <input type="hidden" id="form-editing-alias" value="">
      <div>
        <label class="block text-xs text-gray-400 mb-1">别名 *</label>
        <input id="form-alias" required
               class="w-full bg-surface border border-border rounded px-3 py-2 text-sm text-gray-200
                      focus:outline-none focus:border-indigo-500 placeholder-gray-600"
               placeholder="例如: my-deepseek-key">
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1">供应商 *</label>
        <select id="form-provider" required
                class="w-full bg-surface border border-border rounded px-3 py-2 text-sm text-gray-200
                       focus:outline-none focus:border-indigo-500 cursor-pointer">
          <option value="">选择供应商</option>
          <option value="openai">OpenAI</option>
          <option value="deepseek">DeepSeek</option>
          <option value="codex">Codex</option>
          <option value="custom">自定义</option>
        </select>
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1">API Key *</label>
        <div class="relative">
          <input id="form-apikey"
                 class="w-full bg-surface border border-border rounded px-3 py-2 pr-10 text-sm text-gray-200
                        focus:outline-none focus:border-indigo-500 placeholder-gray-600"
                 placeholder="sk-...">
          <button type="button" onclick="toggleApiKeyVisibility()"
                  class="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-200 cursor-pointer">
            <svg id="eye-icon" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                    d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                    d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>
            </svg>
          </button>
        </div>
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1">Base URL</label>
        <input id="form-baseurl"
               class="w-full bg-surface border border-border rounded px-3 py-2 text-sm text-gray-200
                      focus:outline-none focus:border-indigo-500 placeholder-gray-600"
               placeholder="留空使用默认地址">
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1">模型（逗号分隔，* 表示全部）</label>
        <input id="form-models" value="*"
               class="w-full bg-surface border border-border rounded px-3 py-2 text-sm text-gray-200
                      focus:outline-none focus:border-indigo-500">
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1">优先级（越小越优先）</label>
        <input id="form-priority" type="number" value="0" min="0" max="999"
               class="w-full bg-surface border border-border rounded px-3 py-2 text-sm text-gray-200
                      focus:outline-none focus:border-indigo-500">
      </div>
      <div class="flex gap-2 pt-2">
        <button type="submit"
                class="flex-1 bg-indigo-600 hover:bg-indigo-500 text-white text-sm px-4 py-2 rounded
                       transition-colors cursor-pointer">
          保存
        </button>
        <button type="button" onclick="closeKeyModal()"
                class="flex-1 bg-surface border border-border text-gray-400 hover:text-gray-200 text-sm px-4 py-2 rounded
                       transition-colors cursor-pointer">
          取消
        </button>
      </div>
    </form>
  </div>
</div>

<script>
const API = '/api/keys';

document.addEventListener('DOMContentLoaded', fetchKeys);

// 供应商默认 base_url
const DEFAULT_URLS = {
  openai: 'https://api.openai.com',
  deepseek: 'https://api.deepseek.com',
  codex: 'https://api.openai.com',
};

document.getElementById('form-provider').addEventListener('change', function() {
  const val = this.value;
  const baseUrlEl = document.getElementById('form-baseurl');
  if (val in DEFAULT_URLS && !baseUrlEl.value) {
    baseUrlEl.value = DEFAULT_URLS[val];
  }
});

async function fetchKeys() {
  try {
    const res = await fetch(API);
    const json = await res.json();
    renderKeys(json.data);
  } catch (e) {
    console.error('获取 key 列表失败:', e);
  }
}

function renderKeys(keys) {
  const container = document.getElementById('key-list');
  const empty = document.getElementById('empty-state');

  if (!keys || keys.length === 0) {
    container.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }

  empty.classList.add('hidden');

  let html = '';
  keys.forEach(k => {
    const statusCls = 'status-' + k.status;
    const statusLabel = { active: '启用', disabled: '禁用', rate_limited: '限流' }[k.status] || k.status;

    html += `<div class="border border-border rounded-lg mb-3 bg-surface-light/50 overflow-hidden">
      <div class="flex items-center justify-between px-4 py-3 flex-wrap gap-2">
        <div class="flex items-center gap-3 min-w-0">
          <span class="text-indigo-300 font-medium text-sm truncate">${esc(k.alias)}</span>
          <span class="text-gray-500 text-xs">${esc(k.provider)}</span>
          <span class="text-gray-600 text-xs truncate max-w-[200px]">${esc(k.models)}</span>
          <span class="inline-block px-2 py-0.5 rounded text-xs font-medium ${statusCls}">${statusLabel}</span>
          <span class="text-gray-500 text-xs">优先级 ${k.priority}</span>
        </div>
        <div class="flex items-center gap-1.5 shrink-0">
          <span class="text-gray-600 text-xs font-mono">${esc(k.api_key || '')}</span>
          <button onclick="toggleStatus('${esc(k.alias)}', '${k.status}')"
                  class="px-2 py-1 text-xs rounded cursor-pointer transition-colors
                         ${k.status === 'active' ? 'bg-red-950/30 hover:bg-red-950/50 text-red-400' : 'bg-green-950/30 hover:bg-green-950/50 text-green-400'}">
            ${k.status === 'active' ? '禁用' : '启用'}
          </button>
          <button onclick="editKey('${esc(k.alias)}')"
                  class="px-2 py-1 text-xs rounded bg-surface hover:bg-surface-hover text-gray-400 hover:text-gray-200 cursor-pointer transition-colors">
            编辑
          </button>
          <button onclick="testKey('${esc(k.alias)}')"
                  class="px-2 py-1 text-xs rounded bg-surface hover:bg-surface-hover text-gray-400 hover:text-indigo-400 cursor-pointer transition-colors">
            测试
          </button>
          <button onclick="deleteKey('${esc(k.alias)}')"
                  class="px-2 py-1 text-xs rounded bg-surface hover:bg-red-950/50 text-gray-400 hover:text-red-400 cursor-pointer transition-colors">
            删除
          </button>
        </div>
      </div>`;

    if (k.base_url) {
      html += `<div class="px-4 pb-2 text-xs text-gray-600">URL: ${esc(k.base_url)}</div>`;
    }

    html += '</div>';
  });

  container.innerHTML = html;
}

// 添加 Key
function openAddModal() {
  document.getElementById('modal-title-text').textContent = '添加 Key';
  document.getElementById('form-editing-alias').value = '';
  document.getElementById('form-alias').value = '';
  document.getElementById('form-alias').disabled = false;
  document.getElementById('form-provider').value = '';
  document.getElementById('form-apikey').value = '';
  document.getElementById('form-baseurl').value = '';
  document.getElementById('form-models').value = '*';
  document.getElementById('form-priority').value = '0';
  document.getElementById('form-apikey').type = 'password';
  document.getElementById('key-modal').classList.remove('hidden');
}

// 编辑 Key
async function editKey(alias) {
  const res = await fetch(API);
  const json = await res.json();
  const key = json.data.find(k => k.alias === alias);
  if (!key) return;

  // 需要完整 api_key，重新获取
  document.getElementById('modal-title-text').textContent = '编辑 Key - ' + alias;
  document.getElementById('form-editing-alias').value = alias;
  document.getElementById('form-alias').value = alias;
  document.getElementById('form-alias').disabled = true;
  document.getElementById('form-provider').value = key.provider || '';
  document.getElementById('form-apikey').value = '';
  document.getElementById('form-apikey').type = 'password';
  document.getElementById('form-apikey').placeholder = '(不修改则留空)';
  document.getElementById('form-baseurl').value = key.base_url || '';
  document.getElementById('form-models').value = key.models || '*';
  document.getElementById('form-priority').value = key.priority || 0;
  document.getElementById('key-modal').classList.remove('hidden');
}

function closeKeyModal() {
  document.getElementById('key-modal').classList.add('hidden');
  document.getElementById('form-apikey').placeholder = 'sk-...';
}

function toggleApiKeyVisibility() {
  const el = document.getElementById('form-apikey');
  el.type = el.type === 'password' ? 'text' : 'password';
}

// 提交表单
async function submitKey(e) {
  e.preventDefault();
  const editingAlias = document.getElementById('form-editing-alias').value;
  const body = {
    alias: document.getElementById('form-alias').value.trim(),
    provider: document.getElementById('form-provider').value,
    api_key: document.getElementById('form-apikey').value.trim(),
    base_url: document.getElementById('form-baseurl').value.trim() || null,
    models: document.getElementById('form-models').value.trim() || '*',
    priority: parseInt(document.getElementById('form-priority').value) || 0,
  };

  const isEdit = !!editingAlias;
  const url = isEdit ? API + '/' + encodeURIComponent(editingAlias) : API;
  const method = isEdit ? 'PUT' : 'POST';

  // 编辑模式下，如果没填 api_key 就不传
  if (isEdit && !body.api_key) {
    delete body.api_key;
  }

  try {
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (res.ok) {
      closeKeyModal();
      fetchKeys();
    } else {
      alert('操作失败: ' + (data.detail || '未知错误'));
    }
  } catch (e) {
    alert('请求失败: ' + e.message);
  }
}

// 切换状态
async function toggleStatus(alias, currentStatus) {
  const newStatus = currentStatus === 'active' ? 'disabled' : 'active';
  try {
    const res = await fetch(API + '/' + encodeURIComponent(alias) + '/status', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus }),
    });
    if (res.ok) fetchKeys();
  } catch (e) {
    console.error('切换状态失败:', e);
  }
}

// 删除 Key
async function deleteKey(alias) {
  if (!confirm('确认删除 Key "' + alias + '"?')) return;
  try {
    const res = await fetch(API + '/' + encodeURIComponent(alias), { method: 'DELETE' });
    if (res.ok) fetchKeys();
  } catch (e) {
    console.error('删除失败:', e);
  }
}

// 测试 Key
async function testKey(alias) {
  const btn = event.target;
  const origText = btn.textContent;
  btn.textContent = '...';
  btn.disabled = true;

  try {
    const res = await fetch(API + '/' + encodeURIComponent(alias) + '/test', { method: 'POST' });
    const data = await res.json();
    let msg = '测试结果:\n';
    msg += 'URL: ' + (data.url || '-') + '\n';
    msg += '模型: ' + (data.model || '-') + '\n';
    if (data.ok) {
      msg += '状态: 连接成功 (' + (data.latency_ms || 0) + 'ms)';
    } else {
      msg += '状态: 失败\n';
      msg += '状态码: ' + (data.status_code || '-') + '\n';
      msg += '错误: ' + (data.error || '-');
    }
    alert(msg);
  } catch (e) {
    alert('测试请求失败: ' + e.message);
  }

  btn.textContent = origText;
  btn.disabled = false;
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
</script>

</body>
</html>"""


# ── 后台管理页面 HTML ──────────────────────────────────────

ADMIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AKM 后台管理</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
tailwind.config = {
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        surface: { DEFAULT: '#1e1e2e', light: '#262637', hover: '#2d2d40' },
        border: { DEFAULT: '#33334d', light: '#404060' },
      }
    }
  }
}
</script>
<style>
  body { font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace; }
  .status-2xx { background: #166534; color: #86efac; }
  .status-4xx { background: #854d0e; color: #fde68a; }
  .status-5xx { background: #991b1b; color: #fecaca; }
  .status-err { background: #4a044e; color: #e9d5ff; }
  .status-active { background: #166534; color: #86efac; }
  .status-disabled { background: #991b1b; color: #fecaca; }
  .status-rate_limited { background: #854d0e; color: #fde68a; }
  .sidebar-transition { transition: width 0.2s ease; }
  .switch-track { width: 44px; height: 24px; border-radius: 12px; transition: background 0.2s; }
  .switch-thumb { width: 18px; height: 18px; border-radius: 50%; transition: transform 0.2s; transform: translateX(3px); }
  .switch-on .switch-thumb { transform: translateX(23px); }
  .drawer { transition: transform 0.25s ease; }
  .drawer-open { transform: translateX(0); }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #404060; border-radius: 3px; }
  .fade-in { animation: fadeIn 0.15s ease; }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
  @keyframes slideUp { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }
  .token-number { font-variant-numeric: tabular-nums; }
</style>
</head>
<body class="bg-surface text-gray-200 min-h-screen flex">

<!-- 侧边栏 -->
<aside id="sidebar" class="sidebar-transition bg-surface-light border-r border-border flex flex-col shrink-0 w-[220px]">
  <div class="h-14 flex items-center px-4 border-b border-border shrink-0 overflow-hidden">
    <span class="text-sm font-semibold text-white whitespace-nowrap">AKM 后台</span>
  </div>
  <nav class="flex-1 py-3 px-2 space-y-1">
    <a href="#" onclick="navigate('dashboard');return false" data-nav="dashboard"
       class="flex items-center gap-3 px-3 py-2 rounded text-sm text-indigo-400 bg-indigo-400/10 cursor-pointer whitespace-nowrap overflow-hidden">
      <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
      <span class="sidebar-label">Dashboard</span>
    </a>
    <a href="#" onclick="navigate('logs');return false" data-nav="logs"
       class="flex items-center gap-3 px-3 py-2 rounded text-sm text-gray-400 hover:bg-surface-hover hover:text-white transition-colors cursor-pointer whitespace-nowrap overflow-hidden">
      <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 012-2h2a2 2 0 010 4h-2a2 2 0 01-2-2zm0 4h6m-6 4h6m-6 4h6"/></svg>
      <span class="sidebar-label">审计日志</span>
    </a>
    <a href="#" onclick="navigate('settings');return false" data-nav="settings"
       class="flex items-center gap-3 px-3 py-2 rounded text-sm text-gray-400 hover:bg-surface-hover hover:text-white transition-colors cursor-pointer whitespace-nowrap overflow-hidden">
      <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>
      <span class="sidebar-label">设置</span>
    </a>
  </nav>
  <button id="sidebar-toggle" onclick="toggleSidebar()"
          class="h-10 flex items-center justify-center border-t border-border text-gray-500 hover:text-gray-300 transition-colors cursor-pointer shrink-0">
    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 19l-7-7 7-7m8 14l-7-7 7-7"/></svg>
  </button>
</aside>

<!-- 主内容区 -->
<div class="flex-1 flex flex-col min-w-0">
  <header class="h-14 flex items-center justify-between px-6 border-b border-border bg-surface/80 backdrop-blur shrink-0">
    <h2 id="page-title" class="text-sm font-semibold text-white">Dashboard</h2>
    <span class="text-xs text-gray-500">v0.1.0</span>
  </header>

  <main class="flex-1 overflow-y-auto p-6">

    <!-- Dashboard -->
    <div id="view-dashboard">
      <div id="stats-cards" class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <div class="bg-surface-light border border-border rounded-lg p-4">
          <div class="text-xs text-gray-500 mb-1">总请求</div>
          <div id="stat-requests" class="text-2xl font-semibold text-white token-number">-</div>
        </div>
        <div class="bg-surface-light border border-border rounded-lg p-4">
          <div class="text-xs text-gray-500 mb-1">总 Token</div>
          <div id="stat-total" class="text-2xl font-semibold text-indigo-400 token-number">-</div>
        </div>
        <div class="bg-surface-light border border-border rounded-lg p-4">
          <div class="text-xs text-gray-500 mb-1">输入 Token</div>
          <div id="stat-prompt" class="text-2xl font-semibold text-emerald-400 token-number">-</div>
        </div>
        <div class="bg-surface-light border border-border rounded-lg p-4">
          <div class="text-xs text-gray-500 mb-1">输出 Token</div>
          <div id="stat-completion" class="text-2xl font-semibold text-amber-400 token-number">-</div>
        </div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="bg-surface-light border border-border rounded-lg overflow-hidden">
          <div class="px-4 py-3 border-b border-border text-xs font-medium text-gray-400">按供应商</div>
          <div class="overflow-x-auto"><table id="tbl-provider" class="w-full text-sm"><tbody></tbody></table></div>
        </div>
        <div class="bg-surface-light border border-border rounded-lg overflow-hidden">
          <div class="px-4 py-3 border-b border-border text-xs font-medium text-gray-400">按模型</div>
          <div class="overflow-x-auto"><table id="tbl-model" class="w-full text-sm"><tbody></tbody></table></div>
        </div>
      </div>

      <div id="daily-section" class="mt-6 bg-surface-light border border-border rounded-lg overflow-hidden hidden">
        <div class="px-4 py-3 border-b border-border text-xs font-medium text-gray-400">每日用量</div>
        <div class="overflow-x-auto">
          <table id="tbl-daily" class="w-full text-sm">
            <thead><tr class="text-gray-500 text-xs"><th class="text-left px-4 py-2 font-medium">日期</th><th class="text-right px-4 py-2 font-medium">请求</th><th class="text-right px-4 py-2 font-medium">输入</th><th class="text-right px-4 py-2 font-medium">输出</th><th class="text-right px-4 py-2 font-medium">总计</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 日志 -->
    <div id="view-logs" class="hidden">
      <div class="flex items-center justify-between mb-4 flex-wrap gap-3">
        <div class="flex items-center gap-3">
          <select id="log-provider-filter"
                  class="bg-surface-light border border-border text-gray-300 text-sm rounded px-3 py-1.5 focus:outline-none focus:border-indigo-500 cursor-pointer">
            <option value="">全部供应商</option>
          </select>
          <span id="log-count" class="text-xs text-gray-500"></span>
        </div>
        <div class="flex items-center gap-3">
          <label id="auto-refresh-toggle" class="flex items-center gap-2 cursor-pointer select-none switch-off">
            <span class="text-xs text-gray-400">自动刷新</span>
            <div class="switch-track bg-gray-600 flex items-center"><div class="switch-thumb bg-white"></div></div>
            <span class="text-xs text-gray-500">5s</span>
          </label>
          <button onclick="refreshLogs()"
                  class="bg-indigo-600 hover:bg-indigo-500 text-white text-xs px-3 py-1.5 rounded transition-colors cursor-pointer">刷新</button>
        </div>
      </div>
      <div id="log-table-container" class="rounded-lg border border-border overflow-x-auto"></div>
      <div id="log-empty" class="hidden text-center py-16 text-gray-500">暂无日志</div>
    </div>

    <!-- 设置 -->
    <div id="view-settings" class="hidden">
      <div class="flex justify-end mb-4">
        <button onclick="openKeyModal(null)"
                class="bg-indigo-600 hover:bg-indigo-500 text-white text-xs px-3 py-1.5 rounded transition-colors cursor-pointer">添加 Key</button>
      </div>
      <div id="key-list-container"></div>
      <div id="key-empty" class="hidden text-center py-16 text-gray-500">暂无 Key</div>
    </div>

  </main>
</div>

<!-- 日志抽屉 -->
<div id="log-drawer-overlay" class="hidden fixed inset-0 z-40 bg-black/50 fade-in" onclick="closeDrawer()"></div>
<div id="log-drawer" class="hidden fixed top-0 right-0 z-50 h-full w-full max-w-2xl bg-surface-light border-l border-border shadow-2xl drawer flex flex-col" style="transform:translateX(100%)">
  <div class="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
    <h3 id="drawer-title" class="text-sm font-semibold text-white"></h3>
    <button onclick="closeDrawer()" class="text-gray-400 hover:text-white transition-colors cursor-pointer">
      <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
    </button>
  </div>
  <div class="flex border-b border-border shrink-0">
    <button onclick="switchDrawerTab('request')" id="dtab-request"
            class="flex-1 px-4 py-2 text-xs text-indigo-400 border-b-2 border-indigo-400 transition-colors cursor-pointer bg-surface/50">请求体</button>
    <button onclick="switchDrawerTab('response')" id="dtab-response"
            class="flex-1 px-4 py-2 text-xs text-gray-400 border-b-2 border-transparent transition-colors cursor-pointer">响应体</button>
  </div>
  <div class="flex-1 overflow-y-auto p-4">
    <pre id="drawer-body" class="text-xs text-gray-300 leading-relaxed whitespace-pre-wrap break-all"></pre>
  </div>
</div>

<!-- Key 弹窗 -->
<div id="key-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center p-4 fade-in">
  <div class="absolute inset-0 bg-black/60" onclick="closeKeyModal()"></div>
  <div class="relative bg-surface-light border border-border rounded-lg w-full max-w-lg shadow-2xl" style="animation: slideUp 0.2s ease">
    <div class="flex items-center justify-between px-4 py-3 border-b border-border">
      <h3 id="key-modal-title" class="text-sm font-semibold text-white">添加 Key</h3>
      <button onclick="closeKeyModal()" class="text-gray-400 hover:text-white transition-colors cursor-pointer">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
      </button>
    </div>
    <form onsubmit="submitKeyForm(event)" class="p-4 space-y-3">
      <input type="hidden" id="key-form-editing">
      <div><label class="block text-xs text-gray-400 mb-1">别名 *</label><input id="key-form-alias" required class="w-full bg-surface border border-border rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500 placeholder-gray-600" placeholder="例如: my-key"></div>
      <div><label class="block text-xs text-gray-400 mb-1">供应商 *</label><select id="key-form-provider" required class="w-full bg-surface border border-border rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500 cursor-pointer"><option value="">选择供应商</option><option value="openai">OpenAI</option><option value="deepseek">DeepSeek</option><option value="codex">Codex</option><option value="custom">自定义</option></select></div>
      <div><label class="block text-xs text-gray-400 mb-1">API Key *</label><div class="relative"><input id="key-form-apikey" class="w-full bg-surface border border-border rounded px-3 py-2 pr-10 text-sm text-gray-200 focus:outline-none focus:border-indigo-500 placeholder-gray-600" placeholder="sk-..."><button type="button" onclick="toggleKeyInput()" class="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-200 cursor-pointer"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg></button></div></div>
      <div><label class="block text-xs text-gray-400 mb-1">Base URL</label><input id="key-form-baseurl" class="w-full bg-surface border border-border rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500 placeholder-gray-600" placeholder="留空使用默认地址"></div>
      <div><label class="block text-xs text-gray-400 mb-1">模型（逗号分隔，* 表示全部）</label><input id="key-form-models" value="*" class="w-full bg-surface border border-border rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"></div>
      <div><label class="block text-xs text-gray-400 mb-1">优先级（越小越优先）</label><input id="key-form-priority" type="number" value="0" min="0" max="999" class="w-full bg-surface border border-border rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-indigo-500"></div>
      <div class="flex gap-2 pt-2"><button type="submit" class="flex-1 bg-indigo-600 hover:bg-indigo-500 text-white text-sm px-4 py-2 rounded transition-colors cursor-pointer">保存</button><button type="button" onclick="closeKeyModal()" class="flex-1 bg-surface border border-border text-gray-400 hover:text-gray-200 text-sm px-4 py-2 rounded transition-colors cursor-pointer">取消</button></div>
    </form>
  </div>
</div>

<script>
// ── 路由 & 侧边栏 ──────────────────────────────────────

let currentView = 'dashboard';
const views = { dashboard: 'Dashboard', logs: '审计日志', settings: '设置' };

function navigate(view) {
  view = view || 'dashboard';
  if (!views[view]) return;
  if (currentView === view) return;
  currentView = view;

  document.querySelectorAll('[data-nav]').forEach(a => {
    if (a.dataset.nav === view) {
      a.className = a.className.replace(/text-gray-\\d+/, 'text-indigo-400').replace(/bg-indigo-400\\/10/g, '');
      a.classList.add('bg-indigo-400/10');
    } else {
      a.className = a.className.replace(/text-indigo-400/, 'text-gray-400').replace(/bg-indigo-400\\/10/g, '');
    }
  });

  Object.keys(views).forEach(v => document.getElementById('view-' + v).classList.add('hidden'));
  document.getElementById('view-' + view).classList.remove('hidden');
  document.getElementById('page-title').textContent = views[view];

  if (view === 'dashboard') loadDashboard();
  if (view === 'logs') refreshLogs();
  if (view === 'settings') loadKeys();
  try { localStorage.setItem('akm_admin_view', view); } catch(e) {}
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('log-provider-filter').addEventListener('change', refreshLogs);
  document.getElementById('key-form-provider').addEventListener('change', function() {
    const urls = { openai: 'https://api.openai.com', deepseek: 'https://api.deepseek.com', codex: 'https://api.openai.com' };
    const el = document.getElementById('key-form-baseurl');
    if (this.value in urls && !el.value) el.value = urls[this.value];
  });
  document.getElementById('auto-refresh-toggle').addEventListener('click', toggleAutoRefresh);
  const saved = (() => { try { return localStorage.getItem('akm_admin_view'); } catch(e) { return null; } })();
  navigate(saved || 'dashboard');
  loadProviders();
});

function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const labels = document.querySelectorAll('.sidebar-label');
  if (sidebar.style.width === '60px') {
    sidebar.style.width = '220px';
    labels.forEach(l => l.style.display = '');
  } else {
    sidebar.style.width = '60px';
    labels.forEach(l => l.style.display = 'none');
  }
}

// ── Dashboard ──────────────────────────────────────────

async function loadDashboard() {
  try {
    const res = await fetch('/api/stats');
    const s = await res.json();
    document.getElementById('stat-requests').textContent = s.total_requests.toLocaleString();
    document.getElementById('stat-total').textContent = s.total_tokens.toLocaleString();
    document.getElementById('stat-prompt').textContent = s.total_prompt_tokens.toLocaleString();
    document.getElementById('stat-completion').textContent = s.total_completion_tokens.toLocaleString();

    let html = '';
    Object.entries(s.by_provider||{}).sort((a,b)=>b[1].total-a[1].total).forEach(([k,v]) => {
      html += '<tr class="border-t border-border hover:bg-surface-hover"><td class="px-4 py-2 text-xs text-gray-300">'+esc(k)+'</td><td class="px-4 py-2 text-xs text-right text-gray-400">'+v.prompt.toLocaleString()+'</td><td class="px-4 py-2 text-xs text-right text-gray-400">'+v.completion.toLocaleString()+'</td><td class="px-4 py-2 text-xs text-right text-indigo-300">'+v.total.toLocaleString()+'</td><td class="px-4 py-2 text-xs text-right text-gray-500">'+v.requests+'次</td></tr>';
    });
    document.getElementById('tbl-provider').querySelector('tbody').innerHTML = html || '<tr><td colspan="5" class="px-4 py-8 text-center text-gray-600 text-xs">暂无数据</td></tr>';

    html = '';
    Object.entries(s.by_model||{}).sort((a,b)=>b[1].total-a[1].total).forEach(([k,v]) => {
      html += '<tr class="border-t border-border hover:bg-surface-hover"><td class="px-4 py-2 text-xs text-gray-300">'+esc(k)+'</td><td class="px-4 py-2 text-xs text-right text-gray-400">'+v.prompt.toLocaleString()+'</td><td class="px-4 py-2 text-xs text-right text-gray-400">'+v.completion.toLocaleString()+'</td><td class="px-4 py-2 text-xs text-right text-indigo-300">'+v.total.toLocaleString()+'</td><td class="px-4 py-2 text-xs text-right text-gray-500">'+v.requests+'次</td></tr>';
    });
    document.getElementById('tbl-model').querySelector('tbody').innerHTML = html || '<tr><td colspan="5" class="px-4 py-8 text-center text-gray-600 text-xs">暂无数据</td></tr>';

    const dailyEntries = Object.entries(s.daily||{});
    document.getElementById('daily-section').classList.toggle('hidden', dailyEntries.length === 0);
    html = '';
    dailyEntries.forEach(([k,v]) => {
      html += '<tr class="border-t border-border hover:bg-surface-hover"><td class="px-4 py-2 text-xs text-gray-300">'+k+'</td><td class="px-4 py-2 text-xs text-right text-gray-500">'+v.requests+'</td><td class="px-4 py-2 text-xs text-right text-gray-400">'+v.prompt.toLocaleString()+'</td><td class="px-4 py-2 text-xs text-right text-gray-400">'+v.completion.toLocaleString()+'</td><td class="px-4 py-2 text-xs text-right text-indigo-300">'+v.total.toLocaleString()+'</td></tr>';
    });
    document.getElementById('tbl-daily').querySelector('tbody').innerHTML = html;
  } catch(e) { console.error('Dashboard:', e); }
}

// ── 审计日志 ─────────────────────────────────────────

let _logsData = [];
let autoRefresh = false, refreshTimer = null;

function toggleAutoRefresh() {
  autoRefresh = !autoRefresh;
  const t = document.getElementById('auto-refresh-toggle');
  if (autoRefresh) {
    t.classList.add('switch-on'); t.classList.remove('switch-off');
    t.querySelector('.switch-track').classList.replace('bg-gray-600','bg-indigo-600');
    refreshTimer = setInterval(refreshLogs, 5000);
  } else {
    t.classList.remove('switch-on'); t.classList.add('switch-off');
    t.querySelector('.switch-track').classList.replace('bg-indigo-600','bg-gray-600');
    clearInterval(refreshTimer); refreshTimer = null;
  }
}

async function refreshLogs() {
  const prov = document.getElementById('log-provider-filter').value;
  const url = prov ? '/api/logs?limit=200&provider='+encodeURIComponent(prov) : '/api/logs?limit=200';
  try {
    const res = await fetch(url);
    const json = await res.json();
    _logsData = json.data || [];
    document.getElementById('log-count').textContent = _logsData.length + ' 条';
    renderLogTable();
  } catch(e) { console.error(e); }
}

function loadProviders() {
  fetch('/api/logs?limit=500').then(r=>r.json()).then(json=>{
    const providers = [...new Set((json.data||[]).map(l=>l.provider).filter(Boolean))];
    const sel = document.getElementById('log-provider-filter');
    providers.forEach(p=>{ const o=document.createElement('option'); o.value=p; o.textContent=p; sel.appendChild(o); });
  }).catch(()=>{});
}

function renderLogTable() {
  const container = document.getElementById('log-table-container');
  const empty = document.getElementById('log-empty');
  if (!_logsData.length) { container.innerHTML=''; empty.classList.remove('hidden'); return; }
  empty.classList.add('hidden');

  let html = '<table class="w-full text-sm"><thead><tr class="bg-surface-light text-gray-400 text-xs"><th class="text-left px-4 py-2.5 font-medium">时间</th><th class="text-left px-4 py-2.5 font-medium">Alias</th><th class="text-left px-4 py-2.5 font-medium">供应商</th><th class="text-left px-4 py-2.5 font-medium">模型</th><th class="text-center px-4 py-2.5 font-medium w-20">状态</th><th class="text-right px-4 py-2.5 font-medium w-24">延迟</th><th class="text-center px-4 py-2.5 font-medium w-16">详情</th></tr></thead><tbody>';

  _logsData.forEach((log,i) => {
    const code = log.status_code;
    let sc = 'status-err';
    if (code>=200&&code<300) sc='status-2xx'; else if(code>=400&&code<500) sc='status-4xx'; else if(code>=500) sc='status-5xx';
    const lat = log.latency_ms||0;
    const ds = lat>=1000?(lat/1000).toFixed(1)+'s':lat+'ms';
    const dc = lat>5000?'text-red-400':lat>2000?'text-yellow-400':'text-gray-400';
    const hasBody = log.request_body||log.response_body;

    html += '<tr class="border-t border-border hover:bg-surface-hover"><td class="px-4 py-2.5 text-gray-400 whitespace-nowrap text-xs">'+esc(log.timestamp||'-')+'</td><td class="px-4 py-2.5 text-indigo-300 font-medium text-xs">'+esc(log.key_alias||'-')+'</td><td class="px-4 py-2.5 text-gray-300 text-xs">'+esc(log.provider||'-')+'</td><td class="px-4 py-2.5 text-gray-400 text-xs">'+esc(log.model||'-')+'</td><td class="px-4 py-2.5 text-center"><span class="inline-block px-2 py-0.5 rounded text-xs font-medium '+sc+'">'+(code||'ERR')+'</span></td><td class="px-4 py-2.5 text-right text-xs '+dc+'">'+ds+'</td><td class="px-4 py-2.5 text-center">'+(hasBody?'<button onclick="openDrawer('+i+')" class="text-gray-400 hover:text-indigo-400 transition-colors cursor-pointer"><svg class="w-4 h-4 mx-auto" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg></button>':'<span class="text-gray-600">-</span>')+'</td></tr>';
    if (log.error) html += '<tr class="border-t border-border bg-red-950/20"><td colspan="7" class="px-4 py-1.5 text-xs text-red-400">'+esc(log.error)+'</td></tr>';
  });
  html += '</tbody></table>';
  container.innerHTML = html;
}

// 抽屉
let _drawerLog = null;

function openDrawer(idx) {
  _drawerLog = _logsData[idx];
  if (!_drawerLog) return;
  document.getElementById('drawer-title').textContent = '['+esc(_drawerLog.key_alias||'-')+'] '+esc(_drawerLog.model||'-')+' - '+esc(_drawerLog.timestamp||'-');
  switchDrawerTab('request');
  document.getElementById('log-drawer-overlay').classList.remove('hidden');
  document.getElementById('log-drawer').classList.remove('hidden');
  setTimeout(()=>{ document.getElementById('log-drawer').classList.add('drawer-open'); }, 10);
}

function closeDrawer() {
  document.getElementById('log-drawer').classList.remove('drawer-open');
  setTimeout(()=>{ document.getElementById('log-drawer').classList.add('hidden'); document.getElementById('log-drawer-overlay').classList.add('hidden'); }, 250);
}

function switchDrawerTab(tab) {
  if (!_drawerLog) return;
  const treq = document.getElementById('dtab-request'), tres = document.getElementById('dtab-response');
  const body = document.getElementById('drawer-body');
  if (tab==='request') {
    treq.className='flex-1 px-4 py-2 text-xs text-indigo-400 border-b-2 border-indigo-400 transition-colors cursor-pointer bg-surface/50';
    tres.className='flex-1 px-4 py-2 text-xs text-gray-400 border-b-2 border-transparent transition-colors cursor-pointer';
    body.textContent = formatJson(_drawerLog.request_body)||'(空)';
  } else {
    tres.className='flex-1 px-4 py-2 text-xs text-indigo-400 border-b-2 border-indigo-400 transition-colors cursor-pointer bg-surface/50';
    treq.className='flex-1 px-4 py-2 text-xs text-gray-400 border-b-2 border-transparent transition-colors cursor-pointer';
    body.textContent = formatJson(_drawerLog.response_body)||'(空)';
  }
}

document.addEventListener('keydown', e => { if (e.key==='Escape') closeDrawer(); });

// ── 设置 ──────────────────────────────────────────────

async function loadKeys() {
  try {
    const res = await fetch('/api/keys');
    const json = await res.json();
    const keys = json.data || [];
    const container = document.getElementById('key-list-container');
    const empty = document.getElementById('key-empty');
    if (!keys.length) { container.innerHTML=''; empty.classList.remove('hidden'); return; }
    empty.classList.add('hidden');

    let html = '';
    keys.forEach(k => {
      const sc = 'status-'+k.status;
      const sl = { active:'启用', disabled:'禁用', rate_limited:'限流' }[k.status]||k.status;
      html += '<div class="border border-border rounded-lg mb-3 bg-surface-light/50 overflow-hidden">'+
        '<div class="flex items-center justify-between px-4 py-3 flex-wrap gap-2">'+
        '<div class="flex items-center gap-3 min-w-0">'+
        '<span class="text-indigo-300 font-medium text-sm truncate">'+esc(k.alias)+'</span>'+
        '<span class="text-gray-500 text-xs">'+esc(k.provider)+'</span>'+
        '<span class="text-gray-600 text-xs truncate max-w-[150px]">'+esc(k.models)+'</span>'+
        '<span class="inline-block px-2 py-0.5 rounded text-xs font-medium '+sc+'">'+sl+'</span>'+
        '<span class="text-gray-500 text-xs">优先级 '+k.priority+'</span></div>'+
        '<div class="flex items-center gap-1.5 shrink-0">'+
        '<span class="text-gray-600 text-xs font-mono">'+esc(k.api_key||'')+'</span>'+
        '<button onclick="toggleKeyStatus(\''+esc(k.alias)+'\',\''+k.status+'\')" class="px-2 py-1 text-xs rounded cursor-pointer transition-colors '+(k.status==='active'?'bg-red-950/30 hover:bg-red-950/50 text-red-400':'bg-green-950/30 hover:bg-green-950/50 text-green-400')+'">'+(k.status==='active'?'禁用':'启用')+'</button>'+
        '<button onclick="editKey(\''+esc(k.alias)+'\')" class="px-2 py-1 text-xs rounded bg-surface hover:bg-surface-hover text-gray-400 hover:text-gray-200 cursor-pointer transition-colors">编辑</button>'+
        '<button onclick="testKeyConnect(\''+esc(k.alias)+'\')" class="px-2 py-1 text-xs rounded bg-surface hover:bg-surface-hover text-gray-400 hover:text-indigo-400 cursor-pointer transition-colors">测试</button>'+
        '<button onclick="deleteKeyItem(\''+esc(k.alias)+'\')" class="px-2 py-1 text-xs rounded bg-surface hover:bg-red-950/50 text-gray-400 hover:text-red-400 cursor-pointer transition-colors">删除</button></div></div>'+
        (k.base_url?'<div class="px-4 pb-2 text-xs text-gray-600">URL: '+esc(k.base_url)+'</div>':'')+'</div>';
    });
    container.innerHTML = html;
  } catch(e) { console.error('Keys:', e); }
}

function openKeyModal(alias) {
  document.getElementById('key-form-editing').value = alias||'';
  document.getElementById('key-modal-title').textContent = alias ? '编辑 Key - '+alias : '添加 Key';
  document.getElementById('key-form-alias').value = alias||'';
  document.getElementById('key-form-alias').disabled = !!alias;
  document.getElementById('key-form-provider').value = '';
  document.getElementById('key-form-apikey').value = '';
  document.getElementById('key-form-apikey').type = 'password';
  document.getElementById('key-form-apikey').placeholder = alias ? '(不修改则留空)' : 'sk-...';
  document.getElementById('key-form-baseurl').value = '';
  document.getElementById('key-form-models').value = '*';
  document.getElementById('key-form-priority').value = '0';
  document.getElementById('key-modal').classList.remove('hidden');
}

function closeKeyModal() { document.getElementById('key-modal').classList.add('hidden'); document.getElementById('key-form-apikey').placeholder = 'sk-...'; }

function toggleKeyInput() {
  const el = document.getElementById('key-form-apikey');
  el.type = el.type==='password'?'text':'password';
}

async function editKey(alias) {
  try {
    const res = await fetch('/api/keys');
    const json = await res.json();
    const key = (json.data||[]).find(k=>k.alias===alias);
    if (!key) return;
    openKeyModal(alias);
    document.getElementById('key-form-provider').value = key.provider||'';
    document.getElementById('key-form-baseurl').value = key.base_url||'';
    document.getElementById('key-form-models').value = key.models||'*';
    document.getElementById('key-form-priority').value = key.priority||0;
  } catch(e) { console.error(e); }
}

async function submitKeyForm(e) {
  e.preventDefault();
  const editing = document.getElementById('key-form-editing').value;
  const body = {
    alias: document.getElementById('key-form-alias').value.trim(),
    provider: document.getElementById('key-form-provider').value,
    api_key: document.getElementById('key-form-apikey').value.trim(),
    base_url: document.getElementById('key-form-baseurl').value.trim()||null,
    models: document.getElementById('key-form-models').value.trim()||'*',
    priority: parseInt(document.getElementById('key-form-priority').value)||0,
  };
  if (editing && !body.api_key) delete body.api_key;

  const url = editing ? '/api/keys/'+encodeURIComponent(editing) : '/api/keys';
  const method = editing ? 'PUT' : 'POST';
  try {
    const res = await fetch(url, { method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
    const data = await res.json();
    if (res.ok) { closeKeyModal(); loadKeys(); }
    else alert('操作失败: '+(data.detail||'未知错误'));
  } catch(e) { alert('请求失败: '+e.message); }
}

async function toggleKeyStatus(alias, currentStatus) {
  const ns = currentStatus==='active'?'disabled':'active';
  try {
    const res = await fetch('/api/keys/'+encodeURIComponent(alias)+'/status', { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({status:ns}) });
    if (res.ok) loadKeys();
  } catch(e) { console.error(e); }
}

async function deleteKeyItem(alias) {
  if (!confirm('确认删除 Key "'+alias+'"?')) return;
  try { const res = await fetch('/api/keys/'+encodeURIComponent(alias), { method:'DELETE' }); if (res.ok) loadKeys(); }
  catch(e) { console.error(e); }
}

async function testKeyConnect(alias) {
  try {
    const res = await fetch('/api/keys/'+encodeURIComponent(alias)+'/test', { method:'POST' });
    const d = await res.json();
    let msg = '测试 '+alias+':\n';
    msg += 'URL: '+(d.url||'-')+'\n模型: '+(d.model||'-')+'\n';
    msg += d.ok ? '成功 ('+(d.latency_ms||0)+'ms)' : '失败\n状态码: '+(d.status_code||'-')+'\n错误: '+(d.error||'-');
    alert(msg);
  } catch(e) { alert('请求失败: '+e.message); }
}

function esc(s) { if(!s)return''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function formatJson(s) { if(!s)return''; try{return JSON.stringify(JSON.parse(s),null,2);}catch{return s;} }
</script>
</body>
</html>"""
