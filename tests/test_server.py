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
    # 为 lifespan 未生效的测试环境提供模拟 http_client
    app.state.http_client = AsyncMock()
    yield


@pytest.mark.asyncio
async def test_chat_completions_success(monkeypatch):
    """正常请求返回上游响应"""
    async def mock_forward(body, client, log_callback=None, api_path="chat/completions"):
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
    async def mock_forward(body, client, log_callback=None, api_path="chat/completions"):
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
