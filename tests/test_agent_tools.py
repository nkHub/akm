import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

from akm.agent_runtime.tools import (
    build_builtin_tools,
    build_workspace_tools,
    reset_request_agent_model,
    reset_request_subagent_depth,
    reset_request_workspace_root,
    set_request_agent_model,
    set_request_subagent_depth,
    set_request_workspace_root,
    subagent_kill_tool,
    subagent_list_tool,
    subagent_spawn_tool,
    subagent_status_tool,
    subagent_wait_tool,
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

    @property
    def status_code(self):
        return self._status

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
    """未显式传 workspace_root 时，检索自动固定为当前 Agent 工作区。"""
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
async def test_builtin_kb_search_explicit_workspace_root_overrides(monkeypatch):
    """显式传 workspace_root 时按指定目录检索，覆盖自动工作区判定。"""
    client = _install_fake_kb_client(monkeypatch, {"ok": True, "hits": []})
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_search_kb"]
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_workspace_root": "/global/workspace"},
    )
    token = set_request_workspace_root("/global/workspace")
    try:
        await handler(question="组件", workspace_root="/docs/kb")
    finally:
        reset_request_workspace_root(token)

    payload = client.calls[0]["json"]
    assert payload["workspace_root"] == "/docs/kb"
    assert "ignore_workspace" not in payload


@pytest.mark.asyncio
async def test_builtin_kb_search_400_no_match_is_friendly(monkeypatch):
    """插件返回 400（锁定目录无绑定文档/无匹配）时应转为空结果+提示而非报错。"""
    client = _install_fake_kb_client(
        monkeypatch,
        {"detail": "当前工作目录下没有匹配的知识文档，请先为该工作目录配置并重建索引"},
        status_code=400,
    )
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_search_kb"]
    token = set_request_workspace_root("")
    try:
        result = __import__("json").loads(await handler(question="腾讯企业邮箱"))
    finally:
        reset_request_workspace_root(token)

    assert client.calls[0]["json"]["question"] == "腾讯企业邮箱"
    assert result["results"] == []
    assert "没有匹配" in result["message"]
    assert "error" not in result


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
    assert "workspace_root" in tools["akm_search_kb"].parameters["properties"]


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


def test_builtin_native_tools_register(monkeypatch):
    """原生系统工具应全部注册，akm_clipboard_set 需要 content、akm_open 需要 target。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_native_tools_enabled": True},
    )
    app = SimpleNamespace(state=SimpleNamespace())
    tools = {tool.name: tool for tool in build_builtin_tools(app)}
    for name in ("akm_clipboard_get", "akm_clipboard_set", "akm_system_info", "akm_open", "akm_frontmost_app"):
        assert name in tools
    assert tools["akm_clipboard_set"].parameters["required"] == ["content"]
    assert tools["akm_open"].parameters["required"] == ["target"]
    assert tools["akm_open"].parameters["properties"]["kind"]["enum"] == ["url", "path", "app"]


def test_builtin_native_tools_not_registered_when_disabled(monkeypatch):
    """agent_native_tools_enabled=false 时原生工具不应注册。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_native_tools_enabled": False},
    )
    app = SimpleNamespace(state=SimpleNamespace())
    tools = {tool.name for tool in build_builtin_tools(app)}
    for name in ("akm_clipboard_get", "akm_clipboard_set", "akm_system_info", "akm_open", "akm_frontmost_app"):
        assert name not in tools


@pytest.mark.asyncio
async def test_clipboard_get_returns_content(monkeypatch):
    """akm_clipboard_get 应返回剪贴板内容与长度。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_native_tools_enabled": True},
    )
    monkeypatch.setattr("akm.agent_runtime.tools._clipboard_read_text", lambda: "你好 world")
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_clipboard_get"]

    r = await handler()

    assert r["ok"] is True
    assert r["content"] == "你好 world"
    assert r["length"] == 8
    assert r["truncated"] is False


@pytest.mark.asyncio
async def test_clipboard_set_writes_content(monkeypatch):
    """akm_clipboard_set 应调用底层写入并返回 ok。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_native_tools_enabled": True},
    )
    written = []
    monkeypatch.setattr("akm.agent_runtime.tools._clipboard_write_text", lambda c: written.append(c))
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_clipboard_set"]

    r = await handler(content="待复制文本")

    assert r["ok"] is True
    assert written == ["待复制文本"]


@pytest.mark.asyncio
async def test_clipboard_set_rejects_empty_content(monkeypatch):
    """content 为空时应被拒绝。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_native_tools_enabled": True},
    )
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_clipboard_set"]

    r = await handler(content="   ")

    assert r["ok"] is False
    assert "不能为空" in r["error"]


@pytest.mark.asyncio
async def test_system_info_returns_collected(monkeypatch):
    """akm_system_info 应返回底层采集的系统信息。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_native_tools_enabled": True},
    )
    monkeypatch.setattr(
        "akm.agent_runtime.tools._collect_system_info",
        lambda: {"arch": "arm64", "hostname": "mbp", "macos": {"ProductVersion": "15.5"}},
    )
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_system_info"]

    r = await handler()

    assert r["ok"] is True
    assert r["arch"] == "arm64"
    assert r["hostname"] == "mbp"
    assert r["macos"]["ProductVersion"] == "15.5"


@pytest.mark.asyncio
async def test_open_rejects_unsafe_targets(monkeypatch):
    """非法 kind / 非 http(s) URL / 带路径分隔符的应用名应被拒绝。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_native_tools_enabled": True},
    )
    monkeypatch.setattr("akm.agent_runtime.tools._workspace_root", lambda: "/tmp/ws")
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_open"]

    r = await handler(kind="ftp", target="ftp://x")
    assert r["ok"] is False
    assert "kind" in r["error"]

    r = await handler(kind="url", target="file:///etc/passwd")
    assert r["ok"] is False
    assert "http" in r["error"]

    r = await handler(kind="app", target="/Applications/Safari.app")
    assert r["ok"] is False
    assert "路径分隔符" in r["error"]


@pytest.mark.asyncio
async def test_open_url_and_app(monkeypatch):
    """合法的 http(s) URL 与应用名应调用底层打开逻辑。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_native_tools_enabled": True},
    )
    opened = []
    monkeypatch.setattr("akm.agent_runtime.tools._open_target", lambda k, t: opened.append((k, t)) or True)
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_open"]

    r = await handler(kind="url", target="https://example.com")
    assert r["ok"] is True
    assert opened == [("url", "https://example.com")]

    r = await handler(kind="app", target="Safari")
    assert r["ok"] is True
    assert opened == [("url", "https://example.com"), ("app", "Safari")]


@pytest.mark.asyncio
async def test_frontmost_app_returns_info(monkeypatch):
    """akm_frontmost_app 应返回前台应用信息。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_native_tools_enabled": True},
    )
    monkeypatch.setattr(
        "akm.agent_runtime.tools._frontmost_app_info",
        lambda: {"name": "Safari", "bundle_id": "com.apple.Safari", "pid": 123},
    )
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_frontmost_app"]

    r = await handler()

    assert r["ok"] is True
    assert r["app"]["name"] == "Safari"


@pytest.mark.asyncio
async def test_frontmost_app_none_when_no_gui(monkeypatch):
    """无前台应用时应返回 app 为 null 的提示。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_native_tools_enabled": True},
    )
    monkeypatch.setattr("akm.agent_runtime.tools._frontmost_app_info", lambda: None)
    app = SimpleNamespace(state=SimpleNamespace())
    handler = _handlers(app)["akm_frontmost_app"]

    r = await handler()

    assert r["ok"] is True
    assert r["app"] is None
    assert "无前台应用" in r["message"]


def test_send_notification_not_registered_when_disabled(monkeypatch):
    """agent_notify_enabled=false 时 akm_send_notification 不应注册为工具。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_notify_enabled": False},
    )
    app = SimpleNamespace(state=SimpleNamespace())
    tools = {tool.name for tool in build_builtin_tools(app)}
    assert "akm_send_notification" not in tools


# ── flow 工作流工具（akm_flow_*）────────────────────────────────


def test_builtin_flow_tools_register():
    """flow 工作流工具（列表/读取/保存/删除/运行/运行列表）应注册为内置工具。"""
    app = SimpleNamespace(state=SimpleNamespace())
    tools = {tool.name: tool for tool in build_builtin_tools(app)}

    for name in (
        "akm_flow_list",
        "akm_flow_get",
        "akm_flow_save",
        "akm_flow_delete",
        "akm_flow_run",
        "akm_flow_runs",
    ):
        assert name in tools
    assert tools["akm_flow_save"].parameters["required"] == ["name"]
    assert tools["akm_flow_delete"].parameters["required"] == ["workflow_id"]
    assert tools["akm_flow_run"].parameters["required"] == ["workflow_id", "prompt"]


def test_builtin_flow_save_get_list_delete(monkeypatch, tmp_path):
    """flow 工具应在隔离库上完成工作流的保存、读取、列表与删除闭环。"""
    import akm.db as db
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    conn = db.get_connection()
    db.init_db(conn)
    from akm.flow.db import init_flow_db
    init_flow_db()

    app = SimpleNamespace(state=SimpleNamespace())
    handlers = _handlers(app)

    created = handlers["akm_flow_save"](
        name="需求提炼",
        description="把用户需求拆解为方案",
        nodes=[
            {"id": "n1", "type": "intake", "data": {"label": "需求输入", "modelId": "mock-coder"}},
            {"id": "n2", "type": "output", "data": {"label": "交付", "modelId": "mock-coder"}},
        ],
        edges=[{"id": "e1", "source": "n1", "target": "n2"}],
        variables={"language": "Python"},
    )
    assert "error" not in created
    wf = created["workflow"]
    assert wf["name"] == "需求提炼"
    assert len(wf["nodes"]) == 2
    assert len(wf["edges"]) == 1

    listed = handlers["akm_flow_list"]()
    assert len(listed) == 1
    assert listed[0]["name"] == "需求提炼"
    assert listed[0]["node_count"] == 2

    fetched = handlers["akm_flow_get"](workflow_id=wf["id"])
    assert fetched["workflow"]["variables"]["language"] == "Python"

    updated = handlers["akm_flow_save"](
        name="需求提炼（改）",
        workflow_id=wf["id"],
        nodes=wf["nodes"],
        edges=wf["edges"],
        variables=wf["variables"],
    )
    assert updated["workflow"]["name"] == "需求提炼（改）"
    assert len(handlers["akm_flow_list"]()) == 1

    deleted = handlers["akm_flow_delete"](workflow_id=wf["id"])
    assert deleted["deleted"] is True
    assert handlers["akm_flow_list"]() == []


@pytest.mark.asyncio
async def test_builtin_flow_tools_validate(monkeypatch, tmp_path):
    """缺参、不存在的 id 与缺 flow 引擎时应返回错误而非抛异常。"""
    import akm.db as db
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    conn = db.get_connection()
    db.init_db(conn)
    from akm.flow.db import init_flow_db
    init_flow_db()

    app = SimpleNamespace(state=SimpleNamespace())
    handlers = _handlers(app)

    assert "error" in handlers["akm_flow_save"](name="")
    assert "error" in handlers["akm_flow_delete"](workflow_id="")
    assert "error" in handlers["akm_flow_get"](workflow_id="nope")
    assert "error" in handlers["akm_flow_delete"](workflow_id="nope")
    assert "error" in await handlers["akm_flow_run"](workflow_id="nope", prompt="hi")


@pytest.mark.asyncio
async def test_builtin_flow_run_starts_engine(monkeypatch, tmp_path):
    """akm_flow_run 应调用 app.state.flow_engine.start 并返回运行摘要。"""
    import akm.db as db
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    conn = db.get_connection()
    db.init_db(conn)
    from akm.flow.db import init_flow_db
    init_flow_db()

    from akm.flow.db import insert_workflow, create_workflow_id, now_iso

    wf_id = create_workflow_id()
    insert_workflow(
        {
            "id": wf_id,
            "name": "t",
            "nodes": [{"id": "n1", "type": "output", "data": {"label": "o", "modelId": "mock-coder"}}],
            "edges": [],
            "variables": {},
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
        }
    )

    started = {}

    class _FakeEngine:
        async def start(self, wf, prompt, project_id="", requirement_id="", variables=None):
            started["wf"] = wf
            started["variables"] = variables
            return {
                "id": "run_abc",
                "workflowId": wf["id"],
                "status": "running",
                "input": {"prompt": prompt, "variables": variables or {}},
            }

    app = SimpleNamespace(state=SimpleNamespace(flow_engine=_FakeEngine()))
    handlers = _handlers(app)

    r = await handlers["akm_flow_run"](workflow_id=wf_id, prompt="帮我写个脚本", variables={"projectPath": "/tmp/x"})
    assert r["ok"] is True
    assert r["run"]["id"] == "run_abc"
    assert r["run"]["status"] == "running"
    assert started["wf"]["id"] == wf_id
    assert started["variables"] == {"projectPath": "/tmp/x"}

    missing = await handlers["akm_flow_run"](workflow_id=wf_id, prompt="")
    assert "error" in missing


def test_builtin_flow_runs_lists_records(monkeypatch, tmp_path):
    """akm_flow_runs 应返回运行记录列表与总数。"""
    import akm.db as db
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    conn = db.get_connection()
    db.init_db(conn)
    from akm.flow.db import init_flow_db
    init_flow_db()

    from akm.flow.db import insert_run

    insert_run(
        {
            "id": "run_1",
            "workflowId": "wf_1",
            "status": "succeeded",
            "input": {"prompt": "hi"},
            "data_json": {},
            "startedAt": "2026-01-01T00:00:00.000Z",
            "finishedAt": "2026-01-01T00:00:01.000Z",
        }
    )

    app = SimpleNamespace(state=SimpleNamespace())
    handlers = _handlers(app)

    listed = handlers["akm_flow_runs"]()
    assert listed["total"] == 1
    assert listed["runs"][0]["id"] == "run_1"
    assert listed["runs"][0]["status"] == "succeeded"

    filtered = handlers["akm_flow_runs"](workflow_id="wf_x")
    assert filtered["total"] == 0


def test_builtin_flow_run_get_returns_node_details(monkeypatch, tmp_path):
    """akm_flow_run_get 应返回节点级状态、错误与最近日志，用于定位卡住节点。"""
    import akm.db as db
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    conn = db.get_connection()
    db.init_db(conn)
    from akm.flow.db import init_flow_db
    init_flow_db()

    class _FakeEngine:
        def get_run(self, run_id):
            if run_id != "run_abc":
                return None
            return {
                "id": "run_abc",
                "workflowId": "wf_1",
                "status": "failed",
                "pendingHumanNodeId": None,
                "input": {"prompt": "实现登录", "variables": {"projectPath": "/tmp/x"}},
                "totals": {"tokensIn": 10, "tokensOut": 20, "costUsd": 0},
                "startedAt": "2026-01-01T00:00:00.000Z",
                "finishedAt": "2026-01-01T00:00:01.000Z",
                "workflowSnapshot": {
                    "nodes": [{"id": "n1", "type": "code", "data": {"label": "实现", "executor": "pi-agent"}}]
                },
                "nodeRuns": {
                    "n1": {
                        "status": "failed",
                        "error": "boom",
                        "tokensIn": 10,
                        "tokensOut": 5,
                        "fileDiffs": [],
                        "output": {
                            "text": "## 结论\nfail\n实现有 bug",
                            "structured": {
                                "conclusion": "fail",
                                "sections": [{"title": "结论", "body": "fail\n实现有 bug"}],
                                "files": ["src/login.ts"],
                            },
                        },
                        "logs": [{"message": "第一次尝试"}],
                    }
                },
            }

    app = SimpleNamespace(state=SimpleNamespace(flow_engine=_FakeEngine()))
    handlers = _handlers(app)

    got = handlers["akm_flow_run_get"](run_id="run_abc")
    assert got["run"]["id"] == "run_abc"
    assert got["run"]["status"] == "failed"
    assert got["run"]["variables"]["projectPath"] == "/tmp/x"
    node = got["run"]["nodes"][0]
    assert node["label"] == "实现"
    assert node["executor"] == "pi-agent"
    assert node["status"] == "failed"
    assert node["error"] == "boom"
    assert node["logs"] == ["第一次尝试"]
    assert node["outputText"] == "## 结论\nfail\n实现有 bug"
    assert node["structured"]["conclusion"] == "fail"
    assert node["structured"]["files"] == ["src/login.ts"]
    assert node["structured"]["sections"][0]["title"] == "结论"

    missing = handlers["akm_flow_run_get"](run_id="run_nope")
    assert "error" in missing


# ── 子 Agent 递归委托（akm_subagent_*）──


class _FakeSubProc:
    """模拟 asyncio.create_subprocess_exec 返回的 Process 对象。"""

    def __init__(self, pid=999999):
        self.pid = pid
        self.returncode = None

    async def wait(self):
        self.returncode = 0
        return 0


@pytest.mark.asyncio
async def test_subagent_tools_registered_and_gated(monkeypatch, tmp_path):
    """agent_subagent_enabled 默认开启时注册三个工具；显式关闭则不注册。"""
    import akm.agent_runtime.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_SUBAGENT_RUN_ROOT", str(tmp_path))
    monkeypatch.setattr(
        tools_mod, "load_config", lambda: {"agent_subagent_enabled": True}
    )
    names = {tool.name for tool in build_builtin_tools(SimpleNamespace())}
    assert {"akm_subagent_spawn", "akm_subagent_wait", "akm_subagent_kill"} <= names

    monkeypatch.setattr(
        tools_mod, "load_config", lambda: {"agent_subagent_enabled": False}
    )
    names2 = {tool.name for tool in build_builtin_tools(SimpleNamespace())}
    assert "akm_subagent_spawn" not in names2


@pytest.mark.asyncio
async def test_subagent_spawn_starts_child(monkeypatch, tmp_path):
    """主会话（depth=0）spawn 创建子进程并登记任务，深度+1 传给子进程。"""
    import akm.agent_runtime.tools as tools_mod

    captured = {}

    async def _fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeSubProc()

    monkeypatch.setattr(tools_mod, "_SUBAGENT_RUN_ROOT", str(tmp_path))
    monkeypatch.setattr(
        tools_mod, "load_config", lambda: {"agent_subagent_enabled": True}
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    tools_mod._SUBAGENT_TASKS.clear()
    try:
        out = json.loads(await subagent_spawn_tool("把目录内容整理成总结"))

        assert out["status"] == "running"
        assert out["task_id"].startswith("sub_")
        assert out["depth"] == 1
        entry = tools_mod._SUBAGENT_TASKS[out["task_id"]]
        assert entry["status"] == "running"
        assert os.path.isdir(out["workspace"])
        # 子进程用当前解释器执行 runner，剥离 PYTHONHOME/PYTHONPATH
        assert captured["args"][0] == sys.executable
        assert "PYTHONPATH" not in captured["kwargs"]["env"]
        assert "PYTHONHOME" not in captured["kwargs"]["env"]
        # 深度参数 depth+1 传入（runner 会写进 X-Akm-Subagent-Depth header）
        assert captured["args"][5] == "1"
    finally:
        tools_mod._SUBAGENT_TASKS.clear()


@pytest.mark.asyncio
async def test_subagent_spawn_inherits_parent_model(monkeypatch, tmp_path):
    """子进程未显式传模型时默认继承父会话模型；显式传则优先显式值。"""
    import akm.agent_runtime.tools as tools_mod

    captured = {}

    async def _fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeSubProc()

    monkeypatch.setattr(tools_mod, "_SUBAGENT_RUN_ROOT", str(tmp_path))
    monkeypatch.setattr(
        tools_mod, "load_config", lambda: {"agent_subagent_enabled": True}
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    tools_mod._SUBAGENT_TASKS.clear()
    try:
        # 父会话模型存在且 spawn 未传模型 → 继承父模型（args[4] 是 model 位置）
        tok = set_request_agent_model("deepseek-v4-pro")
        try:
            out = json.loads(await subagent_spawn_tool("整理文档"))
            assert out["status"] == "running"
            assert captured["args"][4] == "deepseek-v4-pro"
            assert tools_mod._SUBAGENT_TASKS[out["task_id"]]["model"] == "deepseek-v4-pro"
        finally:
            reset_request_agent_model(tok)

        # 显式传模型时优先用显式值，不覆盖父模型
        out2 = json.loads(await subagent_spawn_tool("整理文档", model="glm-5.2"))
        assert out2["status"] == "running"
        assert captured["args"][4] == "glm-5.2"
    finally:
        tools_mod._SUBAGENT_TASKS.clear()


@pytest.mark.asyncio
async def test_subagent_spawn_depth_limit_default_and_configurable(monkeypatch, tmp_path):
    """默认只开放二级（depth>=1 拒绝再开）；agent_subagent_max_depth=2 时允许再下一级。"""
    import akm.agent_runtime.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_SUBAGENT_RUN_ROOT", str(tmp_path))
    monkeypatch.setattr(
        tools_mod, "load_config", lambda: {"agent_subagent_enabled": True}
    )
    tools_mod._SUBAGENT_TASKS.clear()
    try:
        # 默认 max_depth=1：子 agent（depth=1）内再 spawn 被拒绝
        tok = set_request_subagent_depth(1)
        try:
            out = json.loads(await subagent_spawn_tool("再开一层"))
            assert "error" in out
            assert "嵌套层数已达上限" in out["error"]
        finally:
            reset_request_subagent_depth(tok)

        # 配置 max_depth=2：depth=1 可再 spawn（二级子 agent）
        monkeypatch.setattr(
            tools_mod,
            "load_config",
            lambda: {"agent_subagent_enabled": True, "agent_subagent_max_depth": 2},
        )

        async def _fake_exec2(*args, **kwargs):
            return _FakeSubProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec2)
        tok2 = set_request_subagent_depth(1)
        try:
            out2 = json.loads(await subagent_spawn_tool("可以开下一级"))
            assert out2["status"] == "running"
            assert out2["depth"] == 2
        finally:
            reset_request_subagent_depth(tok2)

        # depth=2 且 max=2：拒绝
        tok3 = set_request_subagent_depth(2)
        try:
            out3 = json.loads(await subagent_spawn_tool("再再开"))
            assert "error" in out3
        finally:
            reset_request_subagent_depth(tok3)
    finally:
        tools_mod._SUBAGENT_TASKS.clear()


@pytest.mark.asyncio
async def test_subagent_spawn_validations(monkeypatch, tmp_path):
    """prompt 空 / workspace 不存在 / 开关关闭均拒绝。"""
    import akm.agent_runtime.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_SUBAGENT_RUN_ROOT", str(tmp_path))
    monkeypatch.setattr(
        tools_mod, "load_config", lambda: {"agent_subagent_enabled": True}
    )
    tools_mod._SUBAGENT_TASKS.clear()
    try:
        out = json.loads(await subagent_spawn_tool("   "))
        assert "prompt 不能为空" in out["error"]

        out2 = json.loads(await subagent_spawn_tool("x", workspace_root=str(tmp_path / "nope")))
        assert "workspace_root 不是存在的目录" in out2["error"]

        monkeypatch.setattr(
            tools_mod, "load_config", lambda: {"agent_subagent_enabled": False}
        )
        out3 = json.loads(await subagent_spawn_tool("x"))
        assert "未启用" in out3["error"]
    finally:
        tools_mod._SUBAGENT_TASKS.clear()


@pytest.mark.asyncio
async def test_subagent_wait_reads_log_and_completed(monkeypatch, tmp_path):
    """wait 在子进程完成后读取日志返回 succeeded 与输出。"""
    import akm.agent_runtime.tools as tools_mod

    log = tmp_path / "subagent.log"
    log.write_text("子代理结果文本", encoding="utf-8")
    tools_mod._SUBAGENT_TASKS["sub_abc"] = {
        "id": "sub_abc",
        "proc": _FakeSubProc(),
        "status": "running",
        "log_path": str(log),
    }
    try:
        out = json.loads(await subagent_wait_tool("sub_abc", timeout_ms=5000))
        assert out["status"] == "succeeded"
        assert out["exit_code"] == 0
        assert "子代理结果文本" in out["output"]
    finally:
        tools_mod._SUBAGENT_TASKS.pop("sub_abc", None)


@pytest.mark.asyncio
async def test_subagent_wait_timeout_returns_running(monkeypatch, tmp_path):
    """wait 超时返回「仍在运行」而非失败，允许主 agent 稍后查询或 kill。"""
    import akm.agent_runtime.tools as tools_mod

    class _SlowSubProc:
        returncode = None

        async def wait(self):
            await asyncio.sleep(60)

    tools_mod._SUBAGENT_TASKS["sub_slow"] = {
        "id": "sub_slow",
        "proc": _SlowSubProc(),
        "status": "running",
        "log_path": str(tmp_path / "x.log"),
    }
    try:
        out = json.loads(await subagent_wait_tool("sub_slow", timeout_ms=10))
        assert out["status"] == "running"
        assert "尚未完成" in out["message"]
    finally:
        tools_mod._SUBAGENT_TASKS.pop("sub_slow", None)


@pytest.mark.asyncio
async def test_subagent_wait_not_found_and_kill(monkeypatch, tmp_path):
    """未知任务报错；kill 终止登记的子进程并置为 killed。"""
    import akm.agent_runtime.tools as tools_mod

    out = json.loads(await subagent_wait_tool("sub_missing"))
    assert "未找到" in out["error"]
    out2 = json.loads(await subagent_kill_tool("sub_missing"))
    assert "未找到" in out2["error"]

    tools_mod._SUBAGENT_TASKS["sub_kill"] = {
        "id": "sub_kill",
        "proc": _FakeSubProc(),
        "status": "running",
    }
    try:
        out3 = json.loads(await subagent_kill_tool("sub_kill"))
        assert out3["ok"] is True
        assert out3["status"] == "killed"
        assert tools_mod._SUBAGENT_TASKS["sub_kill"]["status"] == "killed"
    finally:
        tools_mod._SUBAGENT_TASKS.pop("sub_kill", None)


def test_subagent_list_lists_tasks_by_recency(monkeypatch, tmp_path):
    """list 按创建时间倒序列出全部子 agent 任务摘要。"""
    import akm.agent_runtime.tools as tools_mod

    tools_mod._SUBAGENT_TASKS.clear()
    tools_mod._SUBAGENT_TASKS["sub_old"] = {
        "id": "sub_old", "status": "running", "depth": 1, "model": "m1",
        "workspace": "/tmp/w1", "created_at": "2026-01-01T00:00:00",
    }
    tools_mod._SUBAGENT_TASKS["sub_new"] = {
        "id": "sub_new", "status": "succeeded", "depth": 1, "model": "",
        "workspace": "/tmp/w2", "created_at": "2026-01-01T00:01:00",
    }
    try:
        out = json.loads(subagent_list_tool())
        assert [t["task_id"] for t in out["tasks"]] == ["sub_new", "sub_old"]
        assert out["tasks"][0]["status"] == "succeeded"
        assert out["tasks"][1]["workspace"] == "/tmp/w1"
        # 空表场景
        tools_mod._SUBAGENT_TASKS.clear()
        out_empty = json.loads(subagent_list_tool())
        assert out_empty["tasks"] == []
    finally:
        tools_mod._SUBAGENT_TASKS.clear()


def test_subagent_status_returns_detail_and_log_tail(monkeypatch, tmp_path):
    """status 返回单任务详情与日志尾部，超长截断并标记。"""
    import akm.agent_runtime.tools as tools_mod

    log = tmp_path / "subagent.log"
    log.write_text("短日志内容", encoding="utf-8")
    tools_mod._SUBAGENT_TASKS["sub_det"] = {
        "id": "sub_det", "status": "running", "depth": 1, "model": "ds",
        "workspace": "/tmp/w", "log_path": str(log), "created_at": "2026-01-01T00:00:00",
        "proc": _FakeSubProc(),
    }
    try:
        out = json.loads(subagent_status_tool("sub_det"))
        assert out["task_id"] == "sub_det"
        assert out["depth"] == 1
        assert "短日志内容" in out["log_tail"]
        assert "log_truncated" not in out
        # 超长日志截断
        log.write_text("x" * 5000, encoding="utf-8")
        out2 = json.loads(subagent_status_tool("sub_det"))
        assert out2["log_truncated"] is True
        assert len(out2["log_tail"]) <= 2000
        # 未找到
        out3 = json.loads(subagent_status_tool("sub_missing"))
        assert "未找到" in out3["error"]
    finally:
        tools_mod._SUBAGENT_TASKS.pop("sub_det", None)

