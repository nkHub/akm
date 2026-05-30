import tempfile
import pytest
from unittest.mock import AsyncMock
from httpx import ASGITransport, AsyncClient
from akm.db import get_connection, init_db
from akm.server import app


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    conn = get_connection()
    init_db(conn)
    conn.close()
    # 为 lifespan 未生效的测试环境提供模拟 http_client 和 plugin_manager
    app.state.http_client = AsyncMock()
    app.state.plugin_manager = None
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
async def test_health_endpoint():
    """健康检查端点"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_list_models(monkeypatch):
    """/v1/models 返回 active key 的模型列表"""
    monkeypatch.setattr("akm.server.list_keys", lambda: [
        {"alias": "k1", "provider": "openai", "status": "active", "models": "gpt-4,gpt-3.5-turbo"},
        {"alias": "k2", "provider": "deepseek", "status": "active", "models": "deepseek-chat"},
        {"alias": "k3", "provider": "openai", "status": "disabled", "models": "gpt-4"},
        {"alias": "k4", "provider": "openai", "status": "active", "models": "*"},
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
    # disabled key 的模型不出现
    # models='*' 的不出现
    assert len(data["data"]) == 3


@pytest.mark.asyncio
async def test_api_logs_adds_conv_warning_labels(monkeypatch):
    """/api/logs 返回转换告警派生字段（codes + labels）"""
    monkeypatch.setattr("akm.server.list_logs", lambda **kwargs: [{
        "request_headers": '{"x-akm-conv-warnings":"responses_store_not_mapped,responses_include_not_fully_mapped"}',
        "response_body": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }])
    monkeypatch.setattr("akm.server.count_logs", lambda **kwargs: 1)

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
