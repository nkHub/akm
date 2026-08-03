from types import SimpleNamespace

import pytest

from akm.agent_runtime.tools import build_builtin_tools


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
