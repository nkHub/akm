from types import SimpleNamespace

import pytest

from akm.agent_runtime.tools import (
    build_builtin_tools,
    build_workspace_tools,
    reset_request_workspace_root,
    set_request_workspace_root,
)


def _handlers(app):
    return {tool.name: tool.handler for tool in build_builtin_tools(app)}


def test_builtin_tools_only_expose_non_sensitive_key_metadata(monkeypatch):
    """Key 查询工具不得把解密后的 API Key 传入模型上下文。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.list_keys",
        lambda: [{"alias": "primary", "provider": "openai", "api_key": "sk-secret", "base_url": "https://api.example.com", "models": "gpt-4o", "priority": 1, "status": "active"}],
    )
    app = SimpleNamespace(state=SimpleNamespace())

    result = _handlers(app)["akm_list_keys"]()

    assert result == [{"alias": "primary", "provider": "openai", "models": ["gpt-4o"], "priority": 1, "status": "active"}]
    assert "api_key" not in result[0]
    assert "base_url" not in result[0]


def test_keys_summary_reports_total_and_models(monkeypatch):
    """Key 汇总工具必须返回总数与每个 Key 的模型清单，且不含密钥。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.list_keys",
        lambda: [
            {"alias": "primary", "provider": "openai", "api_key": "sk-secret", "base_url": "https://api.example.com", "models": "gpt-4o,gpt-4o-mini", "priority": 1, "status": "active"},
            {"alias": "backup", "provider": "deepseek", "api_key": "sk-secret2", "base_url": "https://api.deepseek.com", "models": "deepseek-chat", "priority": 2, "status": "active"},
        ],
    )
    app = SimpleNamespace(state=SimpleNamespace())

    result = _handlers(app)["akm_get_keys_summary"]()

    assert result == {
        "total": 2,
        "keys": [
            {"alias": "primary", "provider": "openai", "models": ["gpt-4o", "gpt-4o-mini"], "status": "active"},
            {"alias": "backup", "provider": "deepseek", "models": ["deepseek-chat"], "status": "active"},
        ],
    }
    for key in result["keys"]:
        assert "api_key" not in key
        assert "base_url" not in key


@pytest.mark.asyncio
async def test_builtin_log_tool_limits_query_and_omits_bodies(monkeypatch):
    """日志工具必须限制查询范围，并剔除可能包含敏感内容的字段。"""
    captured = {}

    async def list_logs_async(**kwargs):
        captured.update(kwargs)
        return [{"id": 1, "timestamp": "2026-07-31", "provider": "openai", "key_alias": "primary", "model": "gpt-4o", "status_code": 200, "latency_ms": 12, "prompt_tokens": 3, "completion_tokens": 4, "request_body": "secret", "response_body": "secret", "request_headers": "secret"}]

    monkeypatch.setattr("akm.agent_runtime.tools.list_logs_async", list_logs_async)
    app = SimpleNamespace(state=SimpleNamespace())

    result = await _handlers(app)["akm_list_logs"](limit=100, days=99)

    assert captured == {"limit": 50, "status": "all", "days": 30, "key_alias": ""}
    assert result == [{"id": 1, "timestamp": "2026-07-31", "provider": "openai", "key_alias": "primary", "model": "gpt-4o", "status_code": 200, "latency_ms": 12, "prompt_tokens": 3, "completion_tokens": 4, "error": ""}]


def test_builtin_status_tool_returns_runtime_summaries():
    """状态工具聚合运行摘要，不访问与排障无关的配置或密钥。"""
    app = SimpleNamespace(
        state=SimpleNamespace(
            health_monitor=SimpleNamespace(detail_payload=lambda: {"status": "healthy"}),
            audit_log_queue=SimpleNamespace(qsize=lambda: 2, maxsize=512, dropped_count=1, failure_count=0, worker_alive=lambda: True),
            plugin_manager=SimpleNamespace(plugins={"guard": SimpleNamespace(enabled=True, runtime_ready=True)}),
        )
    )

    assert _handlers(app)["akm_get_status"]() == {
        "health": {"status": "healthy"},
        "audit_queue": {"size": 2, "maxsize": 512, "dropped_count": 1, "failure_count": 0, "worker_alive": True},
        "plugins": [{"name": "guard", "enabled": True, "runtime_ready": True}],
    }


def test_builtin_time_tool_returns_current_time_fields():
    """时间工具返回本地 ISO、UTC ISO、UNIX 时间戳与时区字段。"""
    app = SimpleNamespace(state=SimpleNamespace())

    result = _handlers(app)["akm_get_time"]()

    assert set(result) == {"iso", "utc_iso", "unix", "timezone"}
    # ISO 时间可解析，且 unix 时间戳与当前时刻偏差在合理范围内
    import datetime

    parsed = datetime.datetime.fromisoformat(result["iso"])
    assert result["utc_iso"].endswith("+00:00")
    assert abs(result["unix"] - datetime.datetime.now().timestamp()) < 60


def test_builtin_usage_stats_default_windows_without_cost(monkeypatch):
    """默认返回 1/7/30 三窗口；费用未开启时不含 total_cost 与单价表。"""
    calls = []

    def fake_get_stats(days: int):
        calls.append(days)
        return {
            "total_requests": days,
            "total_prompt_tokens": days * 10,
            "total_completion_tokens": days * 2,
            "total_tokens": days * 12,
            "total_cached_tokens": 1,
            "by_model": {
                "gpt-4": {
                    "prompt": 10,
                    "completion": 2,
                    "total": 12,
                    "cached": 1,
                    "requests": 1,
                }
            },
            "by_provider": {"openai": {"prompt": 10, "completion": 2, "total": 12, "cached": 1, "requests": 1}},
            "by_key": {"primary": {"prompt": 10, "completion": 2, "total": 12, "cached": 1, "requests": 1}},
            "cached_at": "2026-08-06 12:00:00",
        }

    monkeypatch.setattr("akm.server._get_stats", fake_get_stats)
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"cost_stats_enabled": False, "cost_pricing_table": "gpt-4=1/0.1/2"},
    )
    app = SimpleNamespace(state=SimpleNamespace())

    result = _handlers(app)["akm_get_usage_stats"]()

    assert calls == [1, 7, 30]
    assert set(result["windows"]) == {"1", "7", "30"}
    assert result["windows"]["1"]["total_requests"] == 1
    assert result["windows"]["7"]["total_tokens"] == 84
    assert "total_cost" not in result["windows"]["1"]
    assert "pricing" not in result
    assert result["cost_stats_enabled"] is False
    assert "cost_stats_enabled=true" in result["cost_note"]


def test_builtin_usage_stats_single_window_with_cost(monkeypatch):
    """days=7 只查 7 天；开启费用时返回 total_cost 与 pricing 单价表。"""
    calls = []

    def fake_get_stats(days: int):
        calls.append(days)
        return {
            "total_requests": 3,
            "total_prompt_tokens": 100,
            "total_completion_tokens": 20,
            "total_tokens": 120,
            "total_cached_tokens": 5,
            "total_cost": 0.12,
            "cost_currency": "$",
            "costs_by_currency": {"$": 0.12},
            "by_model": {
                "gpt-4": {
                    "prompt": 100,
                    "completion": 20,
                    "total": 120,
                    "cached": 5,
                    "requests": 3,
                    "cost": 0.12,
                    "currency": "$",
                }
            },
            "by_provider": {},
            "by_key": {},
            "cached_at": "2026-08-06 12:00:00",
        }

    monkeypatch.setattr("akm.server._get_stats", fake_get_stats)
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {
            "cost_stats_enabled": True,
            "cost_pricing_table": "gpt-4=1/0.1/2\n*=0/0/0",
        },
    )
    app = SimpleNamespace(state=SimpleNamespace())

    result = _handlers(app)["akm_get_usage_stats"](days=7)

    assert calls == [7]
    assert set(result["windows"]) == {"7"}
    win = result["windows"]["7"]
    assert win["total_cost"] == 0.12
    assert win["cost_currency"] == "$"
    assert win["by_model"]["gpt-4"]["cost"] == 0.12
    assert result["cost_stats_enabled"] is True
    assert result["pricing"]["rules"][0]["pattern"] == "gpt-4"
    assert result["pricing"]["rules"][0]["input_per_1m"] == 1.0
    assert result["pricing_unit"].startswith("USD per 1M")
    assert "不能替代供应商账单" in result["cost_note"]


def test_builtin_usage_stats_registers_tool():
    """akm_get_usage_stats 应注册，days 限定 0/1/7/30。"""
    app = SimpleNamespace(state=SimpleNamespace())
    tools = {tool.name: tool for tool in build_builtin_tools(app)}
    assert "akm_get_usage_stats" in tools
    days_schema = tools["akm_get_usage_stats"].parameters["properties"]["days"]
    assert days_schema["enum"] == [0, 1, 7, 30]


class _FakeKbResponse:
    """模拟 httpx 响应：raise_for_status + json。"""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status = status_code

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def json(self):
        return self._payload


class _FakeKbClient:
    """模拟 httpx.AsyncClient：async 上下文管理器 + post，记录调用参数。"""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status = status_code
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return _FakeKbResponse(self._payload, self._status)


def _install_fake_kb_client(monkeypatch, payload, status_code=200):
    """把 tools.httpx.AsyncClient 换成 fake，返回客户端实例供断言。"""
    import httpx as real_httpx

    client = _FakeKbClient(payload, status_code)
    monkeypatch.setattr(
        "akm.agent_runtime.tools.httpx",
        SimpleNamespace(AsyncClient=lambda timeout: client),
    )
    return client


@pytest.mark.asyncio
async def test_builtin_kb_search_returns_trimmed_hits(monkeypatch):
    """知识库查询工具应把完整 chunk 精简为标题/文件名/分数/截断正文。"""
    long_text = "长" * 900
    client = _install_fake_kb_client(
        monkeypatch,
        {
            "ok": True,
            "hits": [
                {
                    "title": "使用说明",
                    "file_name": "docs/usage.md",
                    "score": 0.91,
                    "chunk_text": long_text,
                    "vector_score": 0.9,
                },
                {"file_name": "notes.md", "chunk_text": "没有标题的片段", "vector_score": 0.5},
            ],
        },
    )
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr("akm.agent_runtime.tools.load_config", lambda: {})
    token = set_request_workspace_root("")
    try:
        result = await _handlers(app)["akm_search_kb"](question="怎么用？")
    finally:
        reset_request_workspace_root(token)

    assert client.calls[0]["url"].endswith("/api/markdown-kb/query")
    assert client.calls[0]["json"]["question"] == "怎么用？"
    payload = _handlers(app) and client.calls[0]["json"]
    assert payload["top_k"] == 5
    assert payload["ignore_workspace"] is True  # 空请求工作区时仍默认全库检索
    results = __import__("json").loads(result)["results"]
    assert results[0]["title"] == "使用说明"
    assert results[0]["file_name"] == "docs/usage.md"
    assert results[0]["score"] == 0.91
    assert len(results[0]["content"]) == 500
    assert results[1]["title"] == "notes.md"
    assert results[1]["score"] == 0.5


@pytest.mark.asyncio
async def test_builtin_kb_search_clamps_top_k_and_requires_question(monkeypatch):
    """top_k 应被钳制到 1..20，空 question 直接返回错误且不发起请求。"""
    client = _install_fake_kb_client(monkeypatch, {"ok": True, "hits": []})
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_search_kb"]

    await handler(question="查询", top_k=999)
    assert client.calls[0]["json"]["top_k"] == 20

    await handler(question="", top_k=3)
    assert client.calls[0]["json"]["top_k"] == 20  # 未发起第二次请求
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_builtin_kb_search_uses_current_request_workspace(monkeypatch):
    """Agent 请求中的知识库检索必须固定为当前工作区，不能由模型指定路径。"""
    client = _install_fake_kb_client(monkeypatch, {"ok": True, "hits": []})
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_search_kb"]
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_workspace_root": "/global/workspace"},
    )
    token = set_request_workspace_root("/global/workspace")
    try:
        await handler(question="组件")
    finally:
        reset_request_workspace_root(token)

    payload = client.calls[0]["json"]
    assert payload["workspace_root"] == "/global/workspace"
    assert "ignore_workspace" not in payload


@pytest.mark.asyncio
async def test_builtin_kb_search_error_paths(monkeypatch):
    """HTTP 异常与接口返回 ok=false 时应返回结构化错误。"""
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_search_kb"]

    _install_fake_kb_client(monkeypatch, {"error": "知识库未初始化"}, status_code=200)
    out = await handler(question="x")
    assert "知识库未初始化" in out

    _install_fake_kb_client(monkeypatch, {}, status_code=500)
    out = await handler(question="x")
    assert "error" in __import__("json").loads(out)


@pytest.mark.asyncio
async def test_builtin_kb_search_registers_tool():
    """akm_search_kb 应注册为工具，question 必填。"""
    app = SimpleNamespace(state=SimpleNamespace())
    tools = {tool.name: tool for tool in build_builtin_tools(app)}
    assert "akm_search_kb" in tools
    assert tools["akm_search_kb"].parameters["required"] == ["question"]
    assert tools["akm_search_kb"].parameters["properties"]["top_k"]["description"]


def test_builtin_get_config_redacts_secret_fields(monkeypatch):
    """配置读取工具必须把密钥类字段改为已配置标记，不得明文透出。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {
            "server_port": 8800,
            "agent_api_token": "sk-token-secret",
            "tavily_api_key": "tv-secret",
            "agent_email_smtp_password": "smtp-secret",
            "agent_write_tools_enabled": True,
        },
    )
    app = SimpleNamespace(state=SimpleNamespace())

    result = _handlers(app)["akm_get_config"]()

    assert result["server_port"] == 8800
    assert result["agent_api_token"] == "已配置"
    assert result["tavily_api_key"] == "已配置"
    assert result["agent_email_smtp_password"] == "已配置"
    assert "sk-token-secret" not in str(result)
    assert "tv-secret" not in str(result)
    assert "smtp-secret" not in str(result)


def test_builtin_list_plugins_returns_non_sensitive_summary():
    """插件列表工具只返回非敏感摘要字段，不暴露 settings 等内部结构。"""
    app = SimpleNamespace(
        state=SimpleNamespace(
            plugin_manager=SimpleNamespace(
                get_plugin_list=lambda: [
                    {
                        "name": "guard",
                        "version": "1.0.0",
                        "category": "security",
                        "description": "Guard",
                        "builtin": True,
                        "enabled": True,
                        "source": "builtin",
                        "settings": [{"key": "secret", "default": "x"}],
                        "hooks": ["on_load"],
                    },
                ]
            )
        )
    )

    result = _handlers(app)["akm_list_plugins"]()

    assert result == [
        {
            "name": "guard",
            "version": "1.0.0",
            "category": "security",
            "description": "Guard",
            "builtin": True,
            "enabled": True,
            "source": "builtin",
        }
    ]


def test_builtin_list_plugins_graceful_without_manager():
    """插件管理器未就绪时返回空列表，不抛异常。"""
    app = SimpleNamespace(state=SimpleNamespace(plugin_manager=None))

    assert _handlers(app)["akm_list_plugins"]() == []


def test_builtin_sessions_list_and_load(monkeypatch):
    """会话列表返回元信息；加载按会话名读取最近消息，非法名被拒绝。"""
    fake_sessions = [
        {"name": "20260805-142301", "created_at": "2026-08-05T14:23:01", "updated_at": "2026-08-05T14:30:12", "message_count": 3, "model": "gpt-4o"},
    ]

    class FakeStore:
        def __init__(self, base_dir=None):
            pass

        def list(self):
            return fake_sessions

        def load(self, name):
            if name == "20260805-142301":
                return {
                    "name": name,
                    "model": "gpt-4o",
                    "created_at": "2026-08-05T14:23:01",
                    "updated_at": "2026-08-05T14:30:12",
                    "messages": [
                        {"role": "user", "content": "旧消息"},
                        {"role": "assistant", "content": "回复1"},
                        {"role": "user", "content": "最近问题"},
                    ],
                }
            return None

    monkeypatch.setattr("akm.agent_runtime.sessions.SessionStore", FakeStore)
    app = SimpleNamespace(state=SimpleNamespace())
    handlers = _handlers(app)

    listed = handlers["akm_list_sessions"]()
    assert listed == fake_sessions
    assert "content" not in listed[0]

    loaded = handlers["akm_load_session"](name="20260805-142301", limit=2)
    assert loaded["message_count"] == 3
    assert [m["content"] for m in loaded["messages"]] == ["回复1", "最近问题"]

    missing = handlers["akm_load_session"](name="nope")
    assert missing["error"]

    bad = handlers["akm_load_session"](name="../evil")
    assert bad["error"]


def test_builtin_readonly_tools_register():
    """配置、插件、会话读取工具均应注册，名称符合内置工具前缀。"""
    app = SimpleNamespace(state=SimpleNamespace())
    tools = {tool.name: tool for tool in build_builtin_tools(app)}

    for name in ("akm_get_config", "akm_list_plugins", "akm_list_sessions", "akm_load_session"):
        assert name in tools
    assert tools["akm_load_session"].parameters["required"] == ["name"]


def test_builtin_task_tools_register():
    """定时任务工具（列表/创建/删除）应注册为内置工具。"""
    app = SimpleNamespace(state=SimpleNamespace())
    tools = {tool.name: tool for tool in build_builtin_tools(app)}

    for name in ("akm_list_tasks", "akm_create_task", "akm_delete_task"):
        assert name in tools
    assert tools["akm_create_task"].parameters["required"] == ["name", "task_type"]
    assert tools["akm_delete_task"].parameters["required"] == ["task_id"]


def test_builtin_task_create_list_delete(monkeypatch, tmp_path):
    """任务工具应在隔离库上完成创建、列表与删除闭环。"""
    import akm.db as db
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    conn = db.get_connection()
    db.init_db(conn)

    app = SimpleNamespace(state=SimpleNamespace())
    handlers = _handlers(app)

    created = handlers["akm_create_task"](
        name="每日统计",
        task_type="agent_call",
        payload={"messages": [{"role": "user", "content": "hi"}]},
        interval_sec=3600,
    )
    assert "error" not in created
    assert created["name"] == "每日统计"
    assert created["task_type"] == "agent_call"
    assert created["enabled"] is True
    assert created["next_run_at"]

    listed = handlers["akm_list_tasks"]()
    assert len(listed) == 1
    assert listed[0]["name"] == "每日统计"

    filtered = handlers["akm_list_tasks"](task_type="usage_query")
    assert filtered == []

    deleted = handlers["akm_delete_task"](task_id=created["id"])
    assert deleted["deleted"] is True
    assert handlers["akm_list_tasks"]() == []


def test_builtin_task_tools_validate(monkeypatch, tmp_path):
    """非法类型与非法 id 应返回错误而非抛出异常。"""
    import akm.db as db
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    conn = db.get_connection()
    db.init_db(conn)

    app = SimpleNamespace(state=SimpleNamespace())
    handlers = _handlers(app)

    bad_type = handlers["akm_create_task"](name="x", task_type="not-a-type")
    assert "error" in bad_type

    missing = handlers["akm_delete_task"](task_id="")
    assert "error" in missing



def _workspace_handlers(monkeypatch, tmp_path, *, write_tools=True):
    """构造工作区文件工具 handlers，并注入 tmp_path 作为请求级工作区。"""
    import json as _json

    from pathlib import Path

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    cfg = {"agent_workspace_root": str(ws)}
    if write_tools:
        cfg["agent_write_tools_enabled"] = True
    monkeypatch.setattr("akm.agent_runtime.tools.load_config", lambda: cfg)
    token = set_request_workspace_root(str(ws))
    handlers = {tool.name: tool.handler for tool in build_workspace_tools()}
    return ws, token, handlers


@pytest.mark.asyncio
async def test_builtin_read_file_rejects_oversized_file(tmp_path, monkeypatch):
    """读文件工具必须对超过 st_size 预检阈值的文件直接拒绝，避免全量读入内存。"""
    import json as _json

    ws, token, handlers = _workspace_handlers(monkeypatch, tmp_path)
    try:
        (ws / "big.txt").write_text("x" * 100)
        monkeypatch.setattr("akm.agent_runtime.tools._WORKSPACE_READ_MAX_FILE_BYTES", 50)
        result = _json.loads(await handlers["akm_read_file"](path="big.txt"))
    finally:
        reset_request_workspace_root(token)
    assert "文件过大" in result["error"]


@pytest.mark.asyncio
async def test_builtin_read_file_paginates_and_truncates(tmp_path, monkeypatch):
    """分页应按行返回；超出字节上限时标记 truncated 且行保持完整。"""
    import json as _json

    ws, token, handlers = _workspace_handlers(monkeypatch, tmp_path)
    try:
        (ws / "lines.txt").write_text("\n".join(f"line-{i}" for i in range(20)) + "\n")
        handler = handlers["akm_read_file"]
        r = _json.loads(await handler(path="lines.txt", offset=10, limit=3))
        assert r["start_line"] == 10
        assert r["content"] == "line-10\nline-11\nline-12\n"
        assert r["truncated"] is False
        assert r["total_lines"] == 20

        monkeypatch.setattr("akm.agent_runtime.tools._WORKSPACE_READ_MAX_BYTES", 10)
        r2 = _json.loads(await handler(path="lines.txt", offset=0, limit=-1))
        assert r2["truncated"] is True
        assert r2["content"].endswith("\n")  # 截断按整行进行，不切半个字符
    finally:
        reset_request_workspace_root(token)


@pytest.mark.asyncio
async def test_builtin_glob_skips_symlink_escaping_workspace(tmp_path, monkeypatch):
    """glob 命中工作区内指向外部文件的软链接时必须跳过，不得泄露外部文件名。"""
    import json as _json
    import os

    ws, token, handlers = _workspace_handlers(monkeypatch, tmp_path)
    try:
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("secret")
        (ws / "leak.txt").symlink_to(outside)
        (ws / "real.txt").write_text("real")
        result = _json.loads(await handlers["akm_glob"](pattern="*.txt"))
    finally:
        reset_request_workspace_root(token)
    assert "leak.txt" not in result["matches"]
    assert "real.txt" in result["matches"]


@pytest.mark.asyncio
async def test_builtin_grep_skips_oversized_file(tmp_path, monkeypatch):
    """grep 对超过单文件扫描上限的文件应跳过，避免逐行正则匹配超大文件。"""
    import json as _json

    ws, token, handlers = _workspace_handlers(monkeypatch, tmp_path)
    try:
        (ws / "big.log").write_text("needle " + "x" * 500)
        (ws / "small.txt").write_text("no match here")
        monkeypatch.setattr("akm.agent_runtime.tools._WORKSPACE_GREP_MAX_FILE_BYTES", 100)
        result = _json.loads(await handlers["akm_grep"](pattern="needle"))
    finally:
        reset_request_workspace_root(token)
    assert result["results"] == []


@pytest.mark.asyncio
async def test_builtin_write_file_rejects_oversized_and_writes_atomically(tmp_path, monkeypatch):
    """写文件工具应拒绝超限内容；正常覆盖写入内容正确且不留临时文件。"""
    import json as _json

    ws, token, handlers = _workspace_handlers(monkeypatch, tmp_path)
    try:
        handler = handlers["akm_write_file"]
        monkeypatch.setattr("akm.agent_runtime.tools._WORKSPACE_WRITE_MAX_BYTES", 10)
        r = _json.loads(await handler(path="a.txt", content="x" * 20))
        assert "过大" in r["error"]

        monkeypatch.setattr("akm.agent_runtime.tools._WORKSPACE_WRITE_MAX_BYTES", 1024)
        r2 = _json.loads(await handler(path="a.txt", content="hello"))
        assert r2["ok"] is True
        assert r2["bytes_written"] == 5
        assert (ws / "a.txt").read_text(encoding="utf-8") == "hello"
        assert not list(ws.glob(".akm-write-*"))
    finally:
        reset_request_workspace_root(token)


@pytest.mark.asyncio
async def test_builtin_edit_file_rejects_oversized_result(tmp_path, monkeypatch):
    """编辑后文件超过写入上限时应拒绝，且原文件保持不变。"""
    import json as _json

    ws, token, handlers = _workspace_handlers(monkeypatch, tmp_path)
    try:
        (ws / "f.txt").write_text("hello world")
        monkeypatch.setattr("akm.agent_runtime.tools._WORKSPACE_WRITE_MAX_BYTES", 8)
        r = _json.loads(await handlers["akm_edit_file"](
            path="f.txt", old_string="hello", new_string="hello world extra content"
        ))
        assert "过大" in r["error"]
        assert (ws / "f.txt").read_text(encoding="utf-8") == "hello world"
    finally:
        reset_request_workspace_root(token)


# ── akm_send_email 发送邮件工具 ──

_EMAIL_CONFIG = {
    "agent_email_enabled": True,
    "agent_email_smtp_host": "smtp.example.com",
    "agent_email_smtp_port": 465,
    "agent_email_smtp_user": "sender@example.com",
    "agent_email_smtp_password": "smtp-pass",
    "agent_email_from": "",
    "agent_email_smtp_ssl": True,
}


@pytest.mark.asyncio
async def test_send_email_requires_feature_flag(monkeypatch):
    """未启用 agent_email_enabled 时调用发送邮件工具必须抛 PermissionError。"""
    monkeypatch.setattr("akm.agent_runtime.tools.load_config", lambda: _EMAIL_CONFIG)
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_send_email"]
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_email_enabled": False},
    )
    with pytest.raises(PermissionError):
        await handler(to="a@b.com", subject="s", body="b")


@pytest.mark.asyncio
async def test_send_email_rejects_missing_smtp_config(monkeypatch):
    """SMTP host/user 未配置时应返回明确错误而非抛异常。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_email_enabled": True},
    )
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_send_email"]

    r = await handler(to="a@b.com", subject="s", body="b")

    assert r["ok"] is False
    assert "SMTP 未配置" in r["error"]


@pytest.mark.asyncio
async def test_send_email_sends_via_smtp_ssl(monkeypatch):
    """完整 SMTP 配置下应通过 smtplib 真实构造邮件并返回 Message-ID。"""
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **kw):
            self.msg = None

        def login(self, *a, **kw):
            pass

        def send_message(self, msg, *a, **kw):
            self.msg = msg

        def quit(self, *a, **kw):
            pass

    monkeypatch.setattr("akm.agent_runtime.tools.load_config", lambda: _EMAIL_CONFIG)
    monkeypatch.setattr(
        "akm.agent_runtime.tools.smtplib.SMTP_SSL",
        lambda *a, **kw: sent.setdefault("server", FakeSMTP()),
    )
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_send_email"]

    r = await handler(to="receiver@example.com", subject="测试主题", body="正文内容")

    assert r["ok"] is True
    assert r["message_id"]
    assert r["from"] == "sender@example.com"
    assert r["to"] == "receiver@example.com"
    assert r["subject"] == "测试主题"
    msg = sent["server"].msg
    assert msg["To"] == "receiver@example.com"
    assert msg["Subject"] == "测试主题"
    assert msg.get_content().rstrip("\n") == "正文内容"


@pytest.mark.asyncio
async def test_send_email_rejects_invalid_recipient(monkeypatch):
    """非法收件人邮箱格式应被拒绝。"""
    monkeypatch.setattr("akm.agent_runtime.tools.load_config", lambda: _EMAIL_CONFIG)
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_send_email"]

    r = await handler(to="not-an-email", subject="s", body="b")

    assert r["ok"] is False
    assert "格式不合法" in r["error"]


@pytest.mark.asyncio
async def test_send_email_rejects_oversize_body(monkeypatch):
    """邮件正文超过单次上限时应被拒绝。"""
    monkeypatch.setattr("akm.agent_runtime.tools.load_config", lambda: _EMAIL_CONFIG)
    monkeypatch.setattr("akm.agent_runtime.tools._WORKSPACE_WRITE_MAX_BYTES", 100)
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_send_email"]

    r = await handler(to="a@b.com", subject="s", body="x" * 200)

    assert r["ok"] is False
    assert "过大" in r["error"]


def test_builtin_notification_registers_tool(monkeypatch):
    """akm_send_notification 应注册为工具，title/message 必填。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_notify_enabled": True},
    )
    app = SimpleNamespace(state=SimpleNamespace())
    tools = {tool.name: tool for tool in build_builtin_tools(app)}
    assert "akm_send_notification" in tools
    assert tools["akm_send_notification"].parameters["required"] == ["title", "message"]


@pytest.mark.asyncio
async def test_send_notification_uses_host_notify(monkeypatch):
    """宿主注入 host_notify 时通知应走该回调，并返回 ok。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_notify_enabled": True},
    )
    received = {}
    app = SimpleNamespace(
        state=SimpleNamespace(host_notify=lambda t, s, m: received.update(t=t, s=s, m=m))
    )
    handler = _handlers(app)["akm_send_notification"]

    r = await handler(title="任务完成", message="测试正文", subtitle="sub")

    assert r["ok"] is True
    assert received["t"] == "任务完成"
    assert received["s"] == "sub"
    assert received["m"] == "测试正文"


@pytest.mark.asyncio
async def test_send_notification_falls_back_to_rumps(monkeypatch):
    """无宿主回调时通知应回退 rumps.notification。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_notify_enabled": True},
    )
    called = []
    import rumps

    monkeypatch.setattr(rumps, "notification", lambda t, s, m: called.append((t, s, m)))
    app = SimpleNamespace(state=SimpleNamespace(host_notify=None))
    handler = _handlers(app)["akm_send_notification"]

    r = await handler(title="标题", message="内容")

    assert r["ok"] is True
    assert called == [("标题", "", "内容")]


@pytest.mark.asyncio
async def test_send_notification_requires_title_and_message(monkeypatch):
    """title 与 message 不能为空。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_notify_enabled": True},
    )
    app = SimpleNamespace(state=SimpleNamespace(host_notify=None))
    handler = _handlers(app)["akm_send_notification"]

    r = await handler(title="", message="内容")

    assert r["ok"] is False
    assert "不能为空" in r["error"]


def test_send_notification_not_registered_when_disabled(monkeypatch):
    """agent_notify_enabled=false 时 akm_send_notification 不应注册为工具。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_notify_enabled": False},
    )
    app = SimpleNamespace(state=SimpleNamespace())
    tools = {tool.name for tool in build_builtin_tools(app)}
    assert "akm_send_notification" not in tools
