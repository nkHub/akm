from types import SimpleNamespace

import pytest

from akm.agent_runtime.tools import (
    build_builtin_tools,
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
