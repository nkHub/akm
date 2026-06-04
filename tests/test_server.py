import tempfile
import json
from pathlib import Path
import pytest
import httpx
from unittest.mock import AsyncMock
from httpx import ASGITransport, AsyncClient
from akm.db import get_connection, init_db, get_keys_log_path, get_db_path
from akm.server import app
from akm.audit import write_log, list_logs


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
    assert events == ["key_created", "key_status_changed", "key_deleted"]
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


def test_estimate_tokens_light_when_usage_missing():
    from akm.server import _estimate_tokens_light
    req = {"model": "x", "messages": [{"role": "user", "content": "你好，帮我总结一下这段文本"}]}
    out = _estimate_tokens_light(req, "")
    assert out["prompt_tokens"] > 0
    assert out["completion_tokens"] == 0
    assert out["total_tokens"] == out["prompt_tokens"]


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
async def test_api_stats_ignores_estimated_usage_tokens():
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
    assert data["total_prompt_tokens"] == 50
    assert data["total_completion_tokens"] == 20
    assert data["total_tokens"] == 100
    assert data["total_cached_tokens"] == 30


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
async def test_plugin_config_api_roundtrip():
    class DummyPM:
        def __init__(self):
            self.saved = None

        def get_config(self, name):
            return {"enabled": True, "keyword_rules": "secret=***"} if name == "data_filter_guard" else None

        def set_config(self, name, data):
            self.saved = (name, data)
            return {"ok": True}

    app.state.plugin_manager = DummyPM()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        get_resp = await client.get("/api/plugin-config/data_filter_guard")
        post_resp = await client.post("/api/plugin-config/data_filter_guard", json={"enabled": False})

    assert get_resp.status_code == 200
    assert get_resp.json()["enabled"] is True
    assert post_resp.status_code == 200
    assert app.state.plugin_manager.saved == ("data_filter_guard", {"enabled": False})


@pytest.mark.asyncio
async def test_api_logs_keeps_security_headers_for_frontend():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("akm.server.list_logs", lambda **kwargs: [{
        "request_headers": '{"x-akm-security":"warn:(?i)curl.*bash","x-akm-flags":"security_response_warned"}',
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

    monkeypatch.undo()
    assert resp.status_code == 200
    data = resp.json()
    row = data["data"][0]
    assert "x-akm-security" in row["request_headers"]
    assert "security_response_warned" in row["request_headers"]
