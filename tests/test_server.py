import asyncio
import tempfile
import json
import io
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from akm.audit import write_log, list_logs
from akm.db import get_connection, init_db, get_keys_log_path, get_db_path
from akm.health import HealthMonitor
from akm.server import app, _default_image_generation_model, _image_supported_models_from_config


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    conn = get_connection()
    init_db(conn)
    conn.close()
    monkeypatch.setattr("akm.server._stats_cache", {})
    # 为 lifespan 未生效的测试环境提供模拟 http_client 和 plugin_manager
    app.state.http_client = AsyncMock()
    app.state.plugin_manager = None
    app.state.health_monitor = HealthMonitor()
    yield


@pytest.mark.asyncio
async def test_chat_completions_success(monkeypatch):
    """正常请求返回上游响应"""
    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None):
        return {
            "status_code": 200,
            "body": '{"choices":[{"message":{"content":"hello"}}]}',
            "key_alias": "test-key",
            "provider": "openai",
            "model": "gpt-4",
            "error": "",
            "latency_ms": 100,
        }
    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "hello"


@pytest.mark.asyncio
async def test_chat_completions_no_keys(monkeypatch):
    """没有可用 key 时返回 503"""
    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None):
        return {
            "status_code": 503,
            "body": "",
            "key_alias": "",
            "provider": "",
            "model": "gpt-4",
            "error": "没有可用的 API key",
            "latency_ms": 0,
        }
    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 503
    data = resp.json()
    assert "没有可用" in data["detail"]


@pytest.mark.asyncio
async def test_embeddings_forward_success(monkeypatch):
    """/v1/embeddings 应复用通用转发链路返回普通 JSON。"""

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None):
        assert api_path == "embeddings"
        return {
            "status_code": 200,
            "body": '{"object":"list","data":[{"object":"embedding","embedding":[0.1,0.2],"index":0}],"model":"text-embedding-3-small","usage":{"prompt_tokens":8,"total_tokens":8}}',
            "key_alias": "embed-key",
            "provider": "openai",
            "model": "text-embedding-3-small",
            "error": "",
            "latency_ms": 80,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "text-embedding-3-small", "input": "hello"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["object"] == "embedding"
    assert data["model"] == "text-embedding-3-small"


@pytest.mark.asyncio
async def test_image_generations_forward_success(monkeypatch):
    """/v1/images/generations 应复用通用转发链路返回普通 JSON。"""

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None):
        assert api_path == "images/generations"
        return {
            "status_code": 200,
            "body": '{"created":123,"data":[{"url":"https://example.com/image.png"}]}',
            "key_alias": "image-key",
            "provider": "openai",
            "model": "gpt-image-1",
            "error": "",
            "latency_ms": 90,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/generations",
            json={"model": "gpt-image-1", "prompt": "a cat"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["url"] == "https://example.com/image.png"


@pytest.mark.asyncio
async def test_image_generations_uses_default_model_when_omitted(monkeypatch):
    """图片生成接口未显式传 model 时，应自动回填 gpt-image-2。"""

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None):
        assert api_path == "images/generations"
        assert body["model"] == "gpt-image-2"
        return {
            "status_code": 200,
            "body": '{"created":123,"data":[{"url":"https://example.com/default-image.png"}]}',
            "key_alias": "image-key",
            "provider": "openai",
            "model": body["model"],
            "error": "",
            "latency_ms": 90,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "log_request_body": False,
            "log_response_body": False,
            "stream_capture_max_bytes": 262144,
            "image_supported_models": "gpt-image-2,gpt-image-3",
        },
    )
    monkeypatch.setattr(
        "akm.server.list_keys",
        lambda: [
            {"alias": "image-key", "provider": "openai", "status": "active", "models": "gpt-image-2"},
        ],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/generations",
            json={"prompt": "a cat"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["url"] == "https://example.com/default-image.png"


@pytest.mark.asyncio
async def test_image_generations_returns_clear_error_when_default_model_unavailable(monkeypatch):
    """未传 model 且当前没有 key 支持 gpt-image-2 时，应直接返回可读错误。"""

    monkeypatch.setattr(
        "akm.server.list_keys",
        lambda: [
            {"alias": "k1", "provider": "openai", "status": "active", "models": "gpt-4.1"},
        ],
    )
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "log_request_body": False,
            "log_response_body": False,
            "stream_capture_max_bytes": 262144,
            "image_supported_models": "gpt-image-2,gpt-image-3",
        },
    )
    monkeypatch.setattr("akm.server.forward_request", AsyncMock())
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/generations",
            json={"prompt": "a cat"},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert "gpt-image-2" in body["detail"]


@pytest.mark.asyncio
async def test_image_edits_forward_success(monkeypatch):
    """/v1/images/edits 应接收 multipart/form-data 并复用通用转发链路。"""

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None):
        assert api_path == "images/edits"
        assert body["model"] == "gpt-image-2"
        assert body["__akm_multipart__"] is True
        assert body["__akm_form_fields__"]["prompt"] == "edit cat"
        assert body["__akm_form_files__"]["image"][0] == "cat.png"
        return {
            "status_code": 200,
            "body": '{"created":123,"data":[{"url":"https://example.com/edited.png"}]}',
            "key_alias": "image-key",
            "provider": "openai",
            "model": body["model"],
            "error": "",
            "latency_ms": 90,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/edits",
            data={"model": "gpt-image-2", "prompt": "edit cat"},
            files={"image": ("cat.png", io.BytesIO(b"fake-bytes"), "image/png")},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["url"] == "https://example.com/edited.png"


@pytest.mark.asyncio
async def test_image_edits_uses_default_model_when_omitted(monkeypatch):
    """图片编辑接口未显式传 model 时，应在存在可用 key 时自动回填 gpt-image-2。"""

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None):
        assert api_path == "images/edits"
        assert body["model"] == "gpt-image-2"
        assert body["__akm_form_fields__"]["model"] == "gpt-image-2"
        return {
            "status_code": 200,
            "body": '{"created":123,"data":[{"url":"https://example.com/default-edit.png"}]}',
            "key_alias": "image-key",
            "provider": "openai",
            "model": body["model"],
            "error": "",
            "latency_ms": 90,
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "log_request_body": False,
            "log_response_body": False,
            "stream_capture_max_bytes": 262144,
            "image_supported_models": "gpt-image-2,gpt-image-3",
        },
    )
    monkeypatch.setattr(
        "akm.server.list_keys",
        lambda: [
            {"alias": "image-key", "provider": "openai", "status": "active", "models": "gpt-image-2"},
        ],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/edits",
            data={"prompt": "edit cat"},
            files={"image": ("cat.png", io.BytesIO(b"fake-bytes"), "image/png")},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["url"] == "https://example.com/default-edit.png"


@pytest.mark.asyncio
async def test_image_edits_returns_clear_error_when_default_model_unavailable(monkeypatch):
    """图片编辑未传 model 且当前没有 key 支持 gpt-image-2 时，应直接返回可读错误。"""

    monkeypatch.setattr(
        "akm.server.list_keys",
        lambda: [
            {"alias": "k1", "provider": "openai", "status": "active", "models": "gpt-4.1"},
        ],
    )
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "log_request_body": False,
            "log_response_body": False,
            "stream_capture_max_bytes": 262144,
            "image_supported_models": "gpt-image-2,gpt-image-3",
        },
    )
    monkeypatch.setattr("akm.server.forward_request", AsyncMock())
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/images/edits",
            data={"prompt": "edit cat"},
            files={"image": ("cat.png", io.BytesIO(b"fake-bytes"), "image/png")},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert "gpt-image-2" in body["detail"]


def test_image_supported_models_from_config_supports_multiple_values():
    models = _image_supported_models_from_config({"image_supported_models": "gpt-image-2, gpt-image-3 , gpt-image-fast"})
    assert models == ["gpt-image-2", "gpt-image-3", "gpt-image-fast"]


def test_default_image_generation_model_uses_first_configured_value():
    model = _default_image_generation_model({"image_supported_models": "gpt-image-2,gpt-image-3"})
    assert model == "gpt-image-2"


@pytest.mark.asyncio
async def test_non_stream_audit_log_prefers_forwarded_request_body(monkeypatch):
    """审计日志应优先记录 proxy 返回的实际转发请求体，而不是入口原始 body。"""

    captured = {}

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None):
        return {
            "status_code": 200,
            "body": '{"choices":[{"message":{"content":"ok"}}]}',
            "request_body_for_log": '{"messages":[{"content":"__AKM_EMAIL_deadbeefcafe__"}]}',
            "key_alias": "test-key",
            "provider": "openai",
            "model": "gpt-4",
            "error": "",
            "latency_ms": 50,
        }

    async def fake_submit(app_obj, data):
        captured.update(data)

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server._submit_audit_log", fake_submit)
    monkeypatch.setattr("akm.server.load_config", lambda: {"log_request_body": True, "log_response_body": False, "stream_capture_max_bytes": 262144})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "a@test.com"}]},
        )

    assert resp.status_code == 200
    assert captured["request_body"] == '{"messages":[{"content":"__AKM_EMAIL_deadbeefcafe__"}]}'
    assert "a@test.com" not in captured["request_body"]


@pytest.mark.asyncio
async def test_stream_audit_log_prefers_forwarded_request_body(monkeypatch):
    """流式审计日志同样应优先记录实际转发请求体。"""

    captured = {}

    class DummyResp:
        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            return None

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None):
        return {
            "stream": True,
            "status_code": 200,
            "response": DummyResp(),
            "adapter": None,
            "request_body_for_log": '{"messages":[{"content":"__AKM_PHONE_deadbeefcafe__"}],"stream":true}',
            "key_alias": "stream-key",
            "provider": "openai",
            "model": "gpt-4",
        }

    async def fake_submit(app_obj, data):
        captured.update(data)

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server._submit_audit_log", fake_submit)
    monkeypatch.setattr("akm.server.load_config", lambda: {"log_request_body": True, "log_response_body": False, "stream_capture_max_bytes": 262144})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "13800138000"}], "stream": True},
        ) as resp:
            assert resp.status_code == 200
            async for _ in resp.aiter_text():
                pass

    assert captured["request_body"] == '{"messages":[{"content":"__AKM_PHONE_deadbeefcafe__"}],"stream":true}'
    assert "13800138000" not in captured["request_body"]


@pytest.mark.asyncio
async def test_streaming_response_emits_on_response_only_after_stream_finishes(monkeypatch):
    """流式请求结束后应由 server 侧统一触发一次 on_response。"""

    class DummyResp:
        def __init__(self):
            self.closed = False

        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            self.closed = True

    class DummyPM:
        def __init__(self):
            self.events = []
            self.plugins = {}

        async def run_hook(self, hook, **kwargs):
            if hook == "on_response":
                self.events.append(kwargs)
            return kwargs

    pm = DummyPM()
    app.state.plugin_manager = pm

    upstream_resp = DummyResp()

    async def mock_forward(body, client, log_callback=None, api_path="chat/completions", plugin_manager=None):
        return {
            "stream": True,
            "status_code": 200,
            "response": upstream_resp,
            "adapter": None,
            "key_alias": "stream-key",
            "provider": "openai",
            "model": "gpt-4",
        }

    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log_async", AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as resp:
            assert resp.status_code == 200
            chunks = []
            async for chunk in resp.aiter_text():
                chunks.append(chunk)

    assert any("data: [DONE]" in chunk for chunk in chunks)
    assert upstream_resp.closed is True
    assert len(pm.events) == 1
    meta = pm.events[0]["response"]
    assert meta["ok"] is True
    assert meta["stream"] is True
    assert meta["key_alias"] == "stream-key"


@pytest.mark.asyncio
async def test_health_endpoint():
    """健康检查端点"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_debug_runtime_exposes_core_runtime_fields():
    """运行时诊断端点应返回进程、健康和队列等核心观测字段。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/debug/runtime")

    assert resp.status_code == 200
    data = resp.json()
    assert "process" in data
    assert "health" in data
    assert "audit_queue" in data
    assert "http_client" in data
    assert data["process"]["pid"] > 0
    assert "rss_bytes" in data["process"]
    assert "thread_count" in data["process"]


@pytest.mark.asyncio
async def test_debug_runtime_history_returns_recent_monitor_events():
    """运行时事件历史端点应返回最近的自愈与退化事件。"""
    monitor = HealthMonitor()
    monitor.record_http_client_recreated("test recreate")
    monitor.set_audit_backlog(pending=12, dropped=2, failures=1)
    monitor.db_consecutive_failures = 3
    monitor.db_last_error = "db locked"
    monitor._append_event(
        "db.probe.failed",
        {"consecutive_failures": monitor.db_consecutive_failures, "error": monitor.db_last_error},
    )
    monitor.pending_audit_tasks = 350
    monitor.ready_payload()
    app.state.health_monitor = monitor

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/debug/runtime/history?limit=10")

    assert resp.status_code == 200
    payload = resp.json()
    events = payload["events"]
    event_names = [item["event"] for item in events]
    assert "http_client.recreated" in event_names
    assert "audit.queue.dropped" in event_names
    assert "db.probe.failed" in event_names
    assert "health.status.changed" in event_names


@pytest.mark.asyncio
async def test_health_ready_and_detail_endpoints_reflect_monitor_state():
    """监护端点应返回 ready/detail 状态与关键指标。"""
    monitor = HealthMonitor()
    monitor.pending_audit_tasks = 350
    monitor.consecutive_upstream_failures = 12
    app.state.health_monitor = monitor

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ready_resp = await client.get("/health/ready")
        detail_resp = await client.get("/health/detail")

    assert ready_resp.status_code == 200
    ready = ready_resp.json()
    assert ready["status"] == "degraded"
    assert "audit_backlog_high" in ready["reasons"]

    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["status"] == "degraded"
    assert detail["metrics"]["pending_audit_tasks"] == 350
    assert detail["metrics"]["consecutive_upstream_failures"] == 12


@pytest.mark.asyncio
async def test_health_detail_exposes_audit_queue_drop_signal():
    """审计队列发生丢弃时，detail 端点应暴露该降级信号。"""
    monitor = HealthMonitor()
    monitor.set_audit_backlog(pending=0, dropped=3, failures=1)
    app.state.health_monitor = monitor

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail_resp = await client.get("/health/detail")

    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["status"] == "degraded"
    assert "audit_queue_dropped" in detail["reasons"]
    assert detail["metrics"]["audit_queue_dropped"] == 3


@pytest.mark.asyncio
async def test_health_detail_exposes_http_client_recreate_metrics():
    """detail 端点应暴露共享客户端的软重建状态。"""
    monitor = HealthMonitor()
    monitor.record_http_client_recreated("too many upstream timeouts")
    app.state.health_monitor = monitor

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail_resp = await client.get("/health/detail")

    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["metrics"]["http_client_recreate_count"] == 1
    assert detail["metrics"]["http_client_last_recreate_reason"] == "too many upstream timeouts"
    assert detail["metrics"]["http_client_last_recreated_at"] != ""


@pytest.mark.asyncio
async def test_health_ready_returns_503_when_db_probe_is_critical():
    """当 DB 探针连续失败过多时，就绪探针应返回 503。"""
    monitor = HealthMonitor()
    monitor.db_consecutive_failures = 10
    app.state.health_monitor = monitor

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ready_resp = await client.get("/health/ready")

    assert ready_resp.status_code == 503
    body = ready_resp.json()
    assert body["status"] == "unhealthy"
    assert body["ready"] is False


@pytest.mark.asyncio
async def test_recreate_shared_http_client_closes_old_client_and_resets_monitor(monkeypatch):
    """连续失败触发软重建时，应替换共享客户端并关闭旧连接池。"""

    class DummyClient:
        def __init__(self, name):
            self.name = name
            self.closed = False

        async def aclose(self):
            self.closed = True

    old_client = DummyClient("old")
    new_client = DummyClient("new")

    app.state.http_client = old_client
    app.state.http_client_lock = asyncio.Lock()
    monitor = HealthMonitor()
    monitor.consecutive_upstream_failures = monitor.UPSTREAM_FAILS_RECREATE
    app.state.health_monitor = monitor

    monkeypatch.setattr("akm.server._build_shared_http_client", lambda: new_client)

    from akm.server import _recreate_shared_http_client

    changed = await _recreate_shared_http_client(app, "too many upstream failures")

    assert changed is True
    assert app.state.http_client is new_client
    assert old_client.closed is True
    assert monitor.http_client_recreate_count == 1
    assert monitor.http_client_last_recreate_reason == "too many upstream failures"
    assert monitor.consecutive_upstream_failures == 0


@pytest.mark.asyncio
async def test_list_models(monkeypatch):
    """/v1/models 返回 active key 的模型列表"""
    monkeypatch.setattr("akm.server.list_keys", lambda: [
        {"alias": "k1", "provider": "openai", "status": "active", "models": "gpt-4,gpt-3.5-turbo"},
        {"alias": "k2", "provider": "deepseek", "status": "active", "models": "deepseek-chat"},
        {"alias": "k3", "provider": "openai", "status": "disabled", "models": "gpt-4"},
        {"alias": "k4", "provider": "openai", "status": "active", "models": "*", "provider_models": ["gpt-4.1", "gpt-4.1-mini"]},
    ])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    model_ids = {m["id"] for m in data["data"]}
    assert "gpt-4" in model_ids
    assert "gpt-3.5-turbo" in model_ids
    assert "deepseek-chat" in model_ids
    assert "gpt-4.1" in model_ids
    assert "gpt-4.1-mini" in model_ids
    # disabled key 的模型不出现
    assert len(data["data"]) == 5


@pytest.mark.asyncio
async def test_api_add_key_syncs_provider_models_from_remote(monkeypatch):
    """新增 key 时应同步拉取提供商模型列表并落库。"""

    class DummyResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}]}

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            return DummyResponse()

    monkeypatch.setattr("akm.server.httpx.AsyncClient", DummyAsyncClient)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/keys",
            json={
                "alias": "sync-key",
                "provider": "openai",
                "api_key": "sk-sync",
                "base_url": "https://example.com/v1",
                "models": "*",
            },
        )

    assert resp.status_code == 200
    key = get_connection().execute("SELECT provider_models FROM keys WHERE alias = ?", ("sync-key",)).fetchone()
    assert key is not None
    assert "gpt-4.1" in key["provider_models"]
    assert "gpt-4.1-mini" in key["provider_models"]


@pytest.mark.asyncio
async def test_api_add_key_rejects_wildcard_with_custom_models(monkeypatch):
    """保存 key 时，星号和自定义模型不能混用。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/keys",
            json={
                "alias": "bad-key",
                "provider": "openai",
                "api_key": "sk-bad",
                "models": "*,gpt-4.1",
            },
        )

    assert resp.status_code == 400
    assert "星号不能和自定义模型同时使用" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_api_refresh_key_provider_models(monkeypatch):
    """批量刷新 provider 模型列表接口应返回成功/失败统计。"""

    add_key = get_connection().execute
    conn = get_connection()
    conn.execute(
        "INSERT INTO keys (alias, provider, api_key, base_url, models, auth_header, priority, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("k1", "openai", "enc1", "https://example.com/v1", "*", "Bearer {api_key}", 0, "active"),
    )
    conn.execute(
        "INSERT INTO keys (alias, provider, api_key, base_url, models, auth_header, priority, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("k2", "openai", "enc2", "https://bad.example.com/v1", "*", "Bearer {api_key}", 1, "active"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("akm.key_pool._decrypt", lambda value: "sk-test")

    async def fake_fetch(provider, api_key, base_url, auth_header):
        if "bad" in str(base_url):
            raise ValueError("同步提供商模型列表失败: HTTP 500")
        return ["gpt-4.1", "gpt-4.1-mini"]

    monkeypatch.setattr("akm.server._fetch_provider_models", fake_fetch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/keys/refresh-models")

    assert resp.status_code == 200
    data = resp.json()
    assert data["refreshed"] == 1
    assert len(data["failed"]) == 1
    assert data["failed"][0]["alias"] == "k2"


@pytest.mark.asyncio
async def test_key_change_log_written_without_api_key(monkeypatch):
    """Key 变更应写入 keys.log，且不能包含 api_key 明文。"""

    class DummyResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}]}

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            return DummyResponse()

    monkeypatch.setattr("akm.server.httpx.AsyncClient", DummyAsyncClient)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/keys",
            json={
                "alias": "log-key",
                "provider": "openai",
                "api_key": "sk-secret-create",
                "base_url": "https://example.com/v1",
                "models": "*",
            },
        )
        assert create_resp.status_code == 200

        status_resp = await client.patch(
            "/api/keys/log-key/status",
            json={"status": "disabled"},
        )
        assert status_resp.status_code == 200

        delete_resp = await client.delete("/api/keys/log-key")
        assert delete_resp.status_code == 200

    log_path = Path(get_keys_log_path())
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "sk-secret-create" not in content

    rows = [json.loads(line) for line in content.splitlines() if line.strip()]
    events = [row["event"] for row in rows]
    assert events == ["key.config.created", "key.status.changed", "key.config.deleted"]
    assert all(row["category"] == "key_audit" for row in rows)
    assert all(row["scope"] == "configuration" for row in rows)
    assert rows[0]["details"]["api_key_updated"] is True
    assert rows[0]["details"]["after"]["alias"] == "log-key"
    assert rows[1]["details"]["before_status"] == "active"
    assert rows[1]["details"]["after_status"] == "disabled"
    assert rows[2]["details"]["before"]["alias"] == "log-key"


@pytest.mark.asyncio
async def test_api_export_keys_omits_model_list(monkeypatch):
    """导出备份时不应包含 model_list 这类派生字段。"""

    class DummyResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}]}

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            return DummyResponse()

    monkeypatch.setattr("akm.server.httpx.AsyncClient", DummyAsyncClient)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/keys",
            json={
                "alias": "export-key",
                "provider": "openai",
                "api_key": "sk-export",
                "base_url": "https://example.com/v1",
                "models": "*",
            },
        )
        assert create_resp.status_code == 200

        resp = await client.get("/api/keys/export")

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert "model_list" not in rows[0]
    assert rows[0]["provider_models"] == ["gpt-4.1", "gpt-4.1-mini"]


@pytest.mark.asyncio
async def test_api_logs_size_includes_db_and_log_files():
    db_path = Path(get_db_path())
    db_path.write_bytes(b"db")
    wal_path = db_path.parent / "akm.db-wal"
    shm_path = db_path.parent / "akm.db-shm"
    keys_log_path = db_path.parent / "keys.log"
    extra_log_path = db_path.parent / "extra.log"
    wal_path.write_bytes(b"wal")
    shm_path.write_bytes(b"shm")
    keys_log_path.write_text("hello", encoding="utf-8")
    extra_log_path.write_text("world!!", encoding="utf-8")

    expected_db_size = db_path.stat().st_size + wal_path.stat().st_size + shm_path.stat().st_size
    expected_log_size = keys_log_path.stat().st_size + extra_log_path.stat().st_size

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/logs/size")

    assert resp.status_code == 200
    data = resp.json()
    assert data["db_size"] == expected_db_size
    assert data["log_size"] == expected_log_size
    assert data["cache_size"] == expected_db_size + expected_log_size
    assert data["size"] == expected_db_size + expected_log_size


@pytest.mark.asyncio
async def test_api_logs_adds_conv_warning_labels(monkeypatch):
    """/api/logs 返回转换告警派生字段（codes + labels）"""
    async def fake_list_logs_async(**kwargs):
        return [{
        "request_headers": '{"x-akm-conv-warnings":"responses_store_not_mapped,responses_include_not_fully_mapped"}',
        "response_body": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }]

    async def fake_count_logs_async(**kwargs):
        return 1

    monkeypatch.setattr("akm.server.list_logs_async", fake_list_logs_async)
    monkeypatch.setattr("akm.server.count_logs_async", fake_count_logs_async)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/logs")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    row = data["data"][0]
    assert "responses_store_not_mapped" in row["conv_warning_codes"]
    assert "responses_include_not_fully_mapped" in row["conv_warning_codes"]
    assert "store 未映射" in row["conv_warning_labels"]
    assert "include 未完整映射" in row["conv_warning_labels"]


def test_extract_tokens_from_messages_sse_fallback():
    from akm.server import _extract_tokens
    sse = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"x","content":[],"usage":{"input_tokens":123}}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":7,"cached_tokens":100}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
        'data: [DONE]\n\n'
    )
    out = _extract_tokens(sse)
    assert out is not None
    assert out["prompt_tokens"] == 123
    assert out["completion_tokens"] == 7
    assert out["total_tokens"] == 130
    assert out["cached_tokens"] == 100


def test_extract_tokens_prefers_anthropic_cache_read_input_tokens():
    from akm.server import _extract_tokens
    body = '{"usage":{"input_tokens":1200,"output_tokens":80,"cache_read_input_tokens":900,"cache_creation_input_tokens":300}}'
    out = _extract_tokens(body)
    assert out is not None
    assert out["prompt_tokens"] == 1200
    assert out["completion_tokens"] == 80
    assert out["cached_tokens"] == 900
    assert out["cache_creation_tokens"] == 300


def test_estimate_tokens_light_when_usage_missing():
    from akm.server import _estimate_tokens_light
    req = {"model": "x", "messages": [{"role": "user", "content": "你好，帮我总结一下这段文本"}]}
    out = _estimate_tokens_light(req, "")
    assert out["prompt_tokens"] > 0
    assert out["completion_tokens"] == 0
    assert out["total_tokens"] == out["prompt_tokens"]


def test_bounded_stream_capture_keeps_head_and_tail_with_marker():
    from akm.server import _BoundedStreamCapture

    cap = _BoundedStreamCapture(1024)
    cap.append(b"abcdefghij" * 80)
    cap.append(b"klmnopqrst" * 80)
    cap.append(b"uvwxyz1234567890" * 80)

    text = cap.build_text()
    assert text.startswith("abcdefghij")
    assert "stream truncated by akm" in text
    assert text.endswith("uvwxyz1234567890")
    assert cap.truncated is True


@pytest.mark.asyncio
async def test_api_clean_logs_all_flag_clears_everything():
    write_log({"provider": "o", "key_alias": "k1", "model": "m", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    write_log({"provider": "o", "key_alias": "k2", "model": "m", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    assert len(list_logs(limit=10)) == 2

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/logs/clean", json={"all": True})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["deleted"] == 2
    assert len(list_logs(limit=10)) == 0


@pytest.mark.asyncio
async def test_api_list_agents_returns_messages_anthropic_switch():
    """/api/agents 返回 messages 的 /anthropic 开关状态。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/agents")

    assert resp.status_code == 200
    agents = {item["name"]: item for item in resp.json()["data"]}
    assert agents["deepseek"]["messages_use_anthropic_path"] is True
    assert agents["openai"]["messages_use_anthropic_path"] is False


@pytest.mark.asyncio
async def test_api_stats_ignores_estimated_usage_tokens_by_default():
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 10,
        "error": "",
        "request_headers": '{"x-akm-flags":"usage_estimated_light"}',
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 10,
        "cache_creation_tokens": 0,
    })
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 12,
        "error": "",
        "request_headers": '{}',
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
        "cached_tokens": 30,
        "cache_creation_tokens": 0,
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stats?days=1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_requests"] == 1
    assert data["total_prompt_tokens"] == 50
    assert data["total_completion_tokens"] == 20
    assert data["total_tokens"] == 100
    assert data["total_cached_tokens"] == 30


@pytest.mark.asyncio
async def test_api_stats_can_include_estimated_usage_when_enabled(monkeypatch):
    monkeypatch.setattr(
        "akm.server.load_config",
        lambda: {
            "stats_include_estimated_usage": True,
            "log_request_body": False,
            "log_response_body": False,
        },
    )

    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 10,
        "error": "",
        "request_headers": '{"x-akm-flags":"usage_estimated_light"}',
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 10,
        "cache_creation_tokens": 0,
    })
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 12,
        "error": "",
        "request_headers": '{}',
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
        "cached_tokens": 30,
        "cache_creation_tokens": 0,
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stats?days=1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_requests"] == 2
    assert data["total_prompt_tokens"] == 140
    assert data["total_completion_tokens"] == 70
    assert data["total_tokens"] == 250
    assert data["total_cached_tokens"] == 40


@pytest.mark.asyncio
async def test_api_stats_ignores_rows_without_key_alias():
    write_log({
        "provider": "openai",
        "key_alias": "",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 503,
        "latency_ms": 5,
        "error": "没有可用的 API key",
        "request_headers": '{}',
        "prompt_tokens": 999,
        "completion_tokens": 888,
        "total_tokens": 1887,
        "cached_tokens": 0,
        "cache_creation_tokens": 0,
    })
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 12,
        "error": "",
        "request_headers": '{}',
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
        "cached_tokens": 30,
        "cache_creation_tokens": 0,
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stats?days=1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_requests"] == 1
    assert data["total_prompt_tokens"] == 50
    assert data["total_completion_tokens"] == 20
    assert data["total_tokens"] == 100
    assert "real-key" in data["by_key"]
    assert "" not in data["by_key"]


@pytest.mark.asyncio
async def test_api_stats_ignores_failed_rows_even_with_key_alias():
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 502,
        "latency_ms": 10,
        "error": "upstream failed",
        "request_headers": '{}',
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
        "cached_tokens": 30,
        "cache_creation_tokens": 0,
    })
    write_log({
        "provider": "openai",
        "key_alias": "real-key",
        "model": "gpt-4.1",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 12,
        "error": "",
        "request_headers": '{}',
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
        "cached_tokens": 30,
        "cache_creation_tokens": 0,
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/stats?days=1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_requests"] == 1
    assert data["by_key"]["real-key"]["requests"] == 1
    assert data["by_provider"]["openai"]["requests"] == 1


@pytest.mark.asyncio
async def test_plugin_config_api_roundtrip():
    class DummyPM:
        def __init__(self):
            self.saved = None

        def get_config(self, name):
            return {"enabled": True} if name == "protocol_converter" else None

        def set_config(self, name, data):
            self.saved = (name, data)
            return {"ok": True}

    app.state.plugin_manager = DummyPM()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        get_resp = await client.get("/api/plugin-config/protocol_converter")
        post_resp = await client.post("/api/plugin-config/protocol_converter", json={"enabled": False})

    assert get_resp.status_code == 200
    assert get_resp.json()["enabled"] is True
    assert post_resp.status_code == 200
    assert app.state.plugin_manager.saved == ("protocol_converter", {"enabled": False})


@pytest.mark.asyncio
async def test_api_logs_keeps_security_headers_for_frontend():
    monkeypatch = pytest.MonkeyPatch()
    async def fake_list_logs_async(**kwargs):
        return [{
        "request_headers": '{"x-akm-security":"warn:(?i)curl.*bash","x-akm-flags":"security_response_warned"}',
        "response_body": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }]

    async def fake_count_logs_async(**kwargs):
        return 1

    monkeypatch.setattr("akm.server.list_logs_async", fake_list_logs_async)
    monkeypatch.setattr("akm.server.count_logs_async", fake_count_logs_async)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/logs")

    monkeypatch.undo()
    assert resp.status_code == 200
    data = resp.json()
    row = data["data"][0]
    assert "x-akm-security" in row["request_headers"]
    assert "security_response_warned" in row["request_headers"]
