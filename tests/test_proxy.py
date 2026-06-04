import pytest
import tempfile
from unittest.mock import AsyncMock, MagicMock
import httpx
from akm.proxy import forward_request, test_key_connectivity, _diagnose_no_key
from akm.agent import AGENT_REGISTRY
from akm.db import get_connection, init_db
from akm.key_pool import add_key, set_status


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    """每个测试使用独立数据库，避免诊断类测试互相污染。"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool.SECRET_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool._cipher", None)
    conn = get_connection()
    init_db(conn)
    conn.close()


class FakeStreamResponse:
    """模拟 httpx 流式响应，兼容 client.send(req, stream=True)"""

    def __init__(self, status_code, body_text=""):
        self.status_code = status_code
        self._body = body_text.encode("utf-8") if body_text else b""

    async def aiter_bytes(self):
        """模拟流式读取，为简单起见整块返回"""
        if self._body:
            yield self._body

    async def aclose(self):
        pass

    async def aread(self):
        return self._body


class FakeTestResponse:
    """模拟 test_key_connectivity 使用的 httpx 响应对象。"""

    def __init__(self, status_code, body_text=""):
        self.status_code = status_code
        self.text = body_text

    def json(self):
        import json
        return json.loads(self.text)


def _make_send_mock(client_mock, responses):
    """让 client.send 按顺序返回 FakeStreamResponse"""
    calls = []

    client_mock.build_request = MagicMock(
        side_effect=lambda method, url, json=None, headers=None, timeout=None: httpx.Request(
            method, url, json=json, headers=headers
        )
    )

    async def send_side_effect(req, stream=False):
        calls.append({"req": req, "stream": stream})
        if not responses:
            raise StopIteration("no more mock responses")
        return responses.pop(0)

    client_mock.send = AsyncMock(side_effect=send_side_effect)
    return calls


@pytest.mark.asyncio
async def test_forward_success(monkeypatch):
    """正常转发成功返回"""
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "ok", "provider": "openai", "api_key": "sk-xxx",
        "base_url": "https://api.openai.com",
    }))
    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"choices":[{"message":{"content":"hi"}}]}')])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
        client=mock_client,
    )
    assert result["status_code"] == 200
    assert result["key_alias"] == "ok"
    assert send_calls[0]["stream"] is False
    assert send_calls[0]["req"].content.decode("utf-8").find('"stream":false') != -1


@pytest.mark.asyncio
async def test_forward_429_switches_key(monkeypatch):
    """429 限流后切换下一个 key"""
    keys_called = []

    async def pick_key_mock(model, exclude_aliases=None):
        keys_called.append(model)
        if len(keys_called) == 1:
            return {"alias": "k1", "provider": "openai", "api_key": "sk-a",
                    "base_url": "https://api.openai.com"}
        else:
            return {"alias": "k2", "provider": "openai", "api_key": "sk-b",
                    "base_url": "https://api.openai.com"}

    monkeypatch.setattr("akm.proxy.pick_key_async", pick_key_mock)
    monkeypatch.setattr("akm.proxy.mark_rate_limited", lambda alias: None)

    mock_client = AsyncMock()
    _make_send_mock(mock_client, [
        FakeStreamResponse(429),
        FakeStreamResponse(200, '{"choices":[{"message":{"content":"ok"}}]}'),
    ])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
        client=mock_client,
    )
    assert result["status_code"] == 200
    assert result["key_alias"] == "k2"
    assert len(keys_called) >= 2


@pytest.mark.asyncio
async def test_forward_402_disables_key(monkeypatch):
    """402 余额不足后禁用 key 并切换"""
    keys_called = []

    async def pick_key_mock(model, exclude_aliases=None):
        keys_called.append(model)
        if len(keys_called) == 1:
            return {"alias": "k1", "provider": "openai", "api_key": "sk-a",
                    "base_url": "https://api.openai.com"}
        else:
            return {"alias": "k2", "provider": "openai", "api_key": "sk-b",
                    "base_url": "https://api.openai.com"}

    monkeypatch.setattr("akm.proxy.pick_key_async", pick_key_mock)
    monkeypatch.setattr("akm.proxy.set_status", lambda alias, status: None)

    mock_client = AsyncMock()
    _make_send_mock(mock_client, [
        FakeStreamResponse(402),
        FakeStreamResponse(200, '{"choices":[{"message":{"content":"ok"}}]}'),
    ])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
        client=mock_client,
    )
    assert result["status_code"] == 200
    assert result["key_alias"] == "k2"


@pytest.mark.asyncio
async def test_forward_401_disables_key(monkeypatch):
    """401 认证失败后禁用 key 并切换"""
    keys_called = []

    async def pick_key_mock(model, exclude_aliases=None):
        keys_called.append(model)
        if len(keys_called) == 1:
            return {"alias": "k1", "provider": "openai", "api_key": "sk-a",
                    "base_url": "https://api.openai.com"}
        else:
            return {"alias": "k2", "provider": "openai", "api_key": "sk-b",
                    "base_url": "https://api.openai.com"}

    monkeypatch.setattr("akm.proxy.pick_key_async", pick_key_mock)
    monkeypatch.setattr("akm.proxy.set_status", lambda alias, status: None)

    mock_client = AsyncMock()
    _make_send_mock(mock_client, [
        FakeStreamResponse(401),
        FakeStreamResponse(200, '{"choices":[{"message":{"content":"ok"}}]}'),
    ])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
        client=mock_client,
    )
    assert result["status_code"] == 200
    assert result["key_alias"] == "k2"


@pytest.mark.asyncio
async def test_forward_all_keys_exhausted(monkeypatch):
    """所有 key 都不可用时返回 503"""
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value=None))
    monkeypatch.setattr("akm.proxy.pick_wildcard_key_async", AsyncMock(return_value=None))

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
        client=AsyncMock(),
    )
    assert result["status_code"] == 503
    assert "没有可用" in result["error"]


def test_diagnose_no_key_ignores_wildcard_without_provider_models():
    """未同步 provider_models 的 wildcard key 不应再被诊断为模型匹配。"""
    add_key("wild", "openai", "sk-wild", models="*")
    add_key("disabled-exact", "openai", "sk-exact", models="gpt-4")
    set_status("disabled-exact", "disabled")

    message = _diagnose_no_key("gpt-4")

    assert "模型匹配但不可用: disabled-exact" in message
    assert "wildcard_no_provider_models" in message
    assert "模型匹配但不可用: disabled-exact, wild" not in message


def test_diagnose_no_key_includes_candidate_reasons():
    """失败诊断应包含每个候选 key 的判定原因，便于事后复查。"""
    add_key("wild-empty", "openai", "sk-wild", models="*")
    add_key("disabled-exact", "openai", "sk-exact", models="gpt-4")
    add_key("active-miss", "openai", "sk-miss", models="gpt-5")
    set_status("disabled-exact", "disabled")

    message = _diagnose_no_key("gpt-4")

    assert "候选判定:" in message
    assert "active-miss:model_not_matched" in message
    assert "disabled-exact:disabled" in message
    assert "wild-empty:wildcard_no_provider_models" in message


@pytest.mark.asyncio
async def test_forward_request_can_be_blocked_by_on_request_plugin(monkeypatch):
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "unused", "provider": "openai", "api_key": "sk-xxx",
        "base_url": "https://api.openai.com",
    }))

    class DummyPM:
        async def run_hook(self, hook, **kwargs):
            if hook == "on_request":
                return {
                    "request": kwargs["request"],
                    "on_request_block": {
                        "__akm_action__": "block",
                        "status_code": 400,
                        "error": "blocked by guard",
                        "security_action": "block",
                        "security_reason": "request_code_secret:messages[0].content",
                        "body": '{"error":"blocked by guard"}',
                    },
                }
            return kwargs

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
        client=AsyncMock(),
        plugin_manager=DummyPM(),
    )
    assert result["status_code"] == 400
    assert result["error"] == "blocked by guard"
    assert result["security_action"] == "block"


@pytest.mark.asyncio
async def test_forward_responses_to_messages_with_chained_adapter(monkeypatch):
    """responses 在 messages-only provider 下可通过两段转换器链路转发"""

    class DummyRespToChat:
        _source_format = "responses"

        def convert_request(self, body):
            out = dict(body)
            out["_from_resp"] = True
            return out

        def convert_response(self, body):
            return body + "|resp"

    class DummyChatToMsg:
        _source_format = "chat"

        def convert_request(self, body):
            out = dict(body)
            out["_to_msg"] = True
            return out

        def convert_response(self, body):
            return body + "|chat"

    class DummyPM:
        def get_converter(self, from_fmt, to_fmt):
            if (from_fmt, to_fmt) == ("responses", "chat"):
                return DummyRespToChat()
            if (from_fmt, to_fmt) == ("chat", "messages"):
                return DummyChatToMsg()
            return None

        async def run_hook(self, hook, **kwargs):
            return kwargs

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "k1",
        "provider": "anthropic",
        "api_key": "sk-ant",
        "base_url": "https://api.anthropic.com",
    }))

    # 构造上游成功返回（非流式路径）
    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, "hello")])

    result = await forward_request(
        body={"model": "claude-3", "input": "hi", "stream": False},
        client=mock_client,
        api_path="responses",
        plugin_manager=DummyPM(),
    )

    assert result["status_code"] == 200
    # 两段 convert_response：second 后 first，最终应为 hello|chat|resp
    assert result["body"] == "hello|chat|resp"
    assert send_calls[0]["stream"] is False
    assert send_calls[0]["req"].content.decode("utf-8").find('"stream":false') != -1


@pytest.mark.asyncio
async def test_forward_streaming_request_still_forces_upstream_sse(monkeypatch):
    """客户端要求流式时，仍应继续向上游发起 SSE 请求。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "ok", "provider": "openai", "api_key": "sk-xxx",
        "base_url": "https://api.openai.com",
    }))

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"choices":[{"delta":{"content":"hi"}}]}')])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        client=mock_client,
    )

    assert result["status_code"] == 200
    assert result["stream"] is True
    assert send_calls[0]["stream"] is True
    payload = send_calls[0]["req"].content.decode("utf-8")
    assert '"stream":true' in payload
    assert '"include_usage":true' in payload


@pytest.mark.asyncio
async def test_forward_embeddings_request_does_not_inject_stream(monkeypatch):
    """embeddings 转发不应强行注入 stream 字段，也不应走 SSE 请求。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "embed", "provider": "openai", "api_key": "sk-embed",
        "base_url": "https://api.openai.com",
    }))

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"object":"list","data":[],"model":"text-embedding-3-small"}')])

    result = await forward_request(
        body={"model": "text-embedding-3-small", "input": "hello"},
        client=mock_client,
        api_path="embeddings",
    )

    assert result["status_code"] == 200
    assert send_calls[0]["stream"] is False
    payload = send_calls[0]["req"].content.decode("utf-8")
    assert '"stream":' not in payload


@pytest.mark.asyncio
async def test_forward_emits_on_response_meta_for_failure_and_success(monkeypatch):
    """验证 proxy 会在失败与成功路径都触发 on_response 元信息。"""
    keys_called = []

    async def pick_key_mock(model, exclude_aliases=None):
        keys_called.append(model)
        if len(keys_called) == 1:
            return {
                "alias": "k1",
                "provider": "openai",
                "api_key": "sk-a",
                "base_url": "https://api.openai.com",
            }
        return {
            "alias": "k2",
            "provider": "openai",
            "api_key": "sk-b",
            "base_url": "https://api.openai.com",
        }

    monkeypatch.setattr("akm.proxy.pick_key_async", pick_key_mock)
    monkeypatch.setattr("akm.proxy.mark_rate_limited", lambda alias: None)

    class DummyPM:
        def __init__(self):
            self.events = []

        def get_converter(self, from_fmt, to_fmt):
            return None

        async def run_hook(self, hook, **kwargs):
            if hook == "on_response":
                self.events.append(kwargs)
            return kwargs

    pm = DummyPM()

    mock_client = AsyncMock()
    _make_send_mock(mock_client, [
        FakeStreamResponse(429),
        FakeStreamResponse(200, '{"choices":[{"message":{"content":"ok"}}]}'),
    ])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
        client=mock_client,
        plugin_manager=pm,
    )

    assert result["status_code"] == 200
    assert len(pm.events) >= 2

    # 第一条事件应来自 429 错误路径
    first = pm.events[0]["response"]
    assert first["ok"] is False
    assert first["phase"] == "upstream"
    assert first["status_code"] == 429
    assert first["key_alias"] == "k1"
    assert first["action"] == "block"

    # 最后一条事件应来自成功路径
    last = pm.events[-1]["response"]
    assert last["ok"] is True
    assert last["phase"] == "upstream"
    assert last["status_code"] == 200
    assert last["key_alias"] == "k2"


@pytest.mark.asyncio
async def test_forward_allows_on_response_to_rewrite_non_stream_body(monkeypatch):
    """on_response 可对非流式成功响应做正文改写。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "k1", "provider": "openai", "api_key": "sk-a",
        "base_url": "https://api.openai.com",
    }))

    class DummyPM:
        def get_converter(self, from_fmt, to_fmt):
            return None

        async def run_hook(self, hook, **kwargs):
            if hook == "on_response" and kwargs["response"].get("ok"):
                resp = dict(kwargs["response"])
                resp["response_body"] = '{"choices":[{"message":{"content":"blocked"}}]}'
                return {"request": kwargs["request"], "response": resp}
            return kwargs

    mock_client = AsyncMock()
    _make_send_mock(mock_client, [FakeStreamResponse(200, '{"choices":[{"message":{"content":"ok"}}]}')])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}], "stream": False},
        client=mock_client,
        plugin_manager=DummyPM(),
    )

    assert result["status_code"] == 200
    assert result["body"] == '{"choices":[{"message":{"content":"blocked"}}]}'


@pytest.mark.asyncio
async def test_test_key_connectivity_openai_uses_responses_only(monkeypatch):
    """默认情况下 openai 类 key 测试时只请求 responses，不自动回退。"""

    called_urls = []

    class DummyAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            called_urls.append(url)
            return FakeTestResponse(403, '{"error":{"message":"restricted","code":"codex_access_restricted"}}')

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    result = await test_key_connectivity({
        "alias": "share",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "https://example.com",
        "models": "gpt-5.4",
    })

    assert result["ok"] is False
    assert result["api_path"] == "responses"
    assert result["attempted_paths"] == ["responses"]
    assert called_urls == ["https://example.com/v1/responses"]


@pytest.mark.asyncio
async def test_test_key_connectivity_openai_falls_back_when_enabled(monkeypatch):
    """显式开启 fallback 后，openai 类 key 可从 responses 回退到 chat/completions。"""

    responses = [
        FakeTestResponse(403, '{"error":{"message":"restricted","code":"codex_access_restricted"}}'),
        FakeTestResponse(200, '{"id":"ok"}'),
    ]

    class DummyAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            return responses.pop(0)

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    result = await test_key_connectivity({
        "alias": "share",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "https://example.com",
        "models": "gpt-5.4",
    }, allow_fallback=True)

    assert result["ok"] is True
    assert result["api_path"] == "chat/completions"
    assert result["attempted_paths"] == ["responses", "chat/completions"]
    assert result["fallback_used"] is True


@pytest.mark.asyncio
async def test_test_key_connectivity_deepseek_prefers_chat(monkeypatch):
    """deepseek 不支持 responses，测试时应直接选择 chat/completions。"""

    called_urls = []

    class DummyAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            called_urls.append(url)
            return FakeTestResponse(200, '{"id":"ok"}')

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    result = await test_key_connectivity({
        "alias": "gs",
        "provider": "deepseek",
        "api_key": "sk-test",
        "base_url": "https://api.deepseek.com/v1",
        "models": "deepseek-v4-pro",
    })

    assert result["ok"] is True
    assert result["api_path"] == "chat/completions"
    assert result["attempted_paths"] == ["chat/completions"]
    assert called_urls == ["https://api.deepseek.com/v1/chat/completions"]


@pytest.mark.asyncio
async def test_test_key_connectivity_anthropic_uses_messages(monkeypatch):
    """anthropic 仅支持 messages，测试时应直接走 messages。"""

    called = []

    class DummyAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            called.append((url, headers, json))
            return FakeTestResponse(200, '{"id":"ok"}')

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    result = await test_key_connectivity({
        "alias": "claude",
        "provider": "anthropic",
        "api_key": "sk-test",
        "base_url": "https://api.anthropic.com",
        "models": "claude-3-7-sonnet",
    })

    assert result["ok"] is True
    assert result["api_path"] == "messages"
    assert result["attempted_paths"] == ["messages"]
    assert called[0][0] == "https://api.anthropic.com/v1/messages"


@pytest.mark.asyncio
async def test_test_key_connectivity_messages_provider_without_anthropic_switch(monkeypatch):
    """供应商即使原生支持 messages，未开启开关时也不应自动改写到 /anthropic。"""

    called = []

    class DummyAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            called.append((url, headers, json))
            return FakeTestResponse(200, '{"id":"ok"}')

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    AGENT_REGISTRY["vendor-msg"] = AGENT_REGISTRY["openai"].__class__(
        name="vendor-msg",
        default_base_url="https://vendor.example.com",
        supports_chat=False,
        supports_messages=True,
        messages_use_anthropic_path=False,
    )

    try:
        result = await test_key_connectivity({
            "alias": "vendor-msg-key",
            "provider": "vendor-msg",
            "api_key": "sk-test",
            "base_url": "https://vendor.example.com",
            "models": "claude-like-model",
        })
    finally:
        del AGENT_REGISTRY["vendor-msg"]

    assert result["ok"] is True
    assert result["api_path"] == "messages"
    assert result["attempted_paths"] == ["messages"]
    assert called[0][0] == "https://vendor.example.com/v1/messages"
    assert called[0][1]["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_test_key_connectivity_wildcard_uses_first_provider_model(monkeypatch):
    """models='*' 时，测试请求应优先使用已同步的第一个 provider 模型。"""

    called = []

    class DummyAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            called.append(json)
            return FakeTestResponse(200, '{"id":"ok"}')

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    result = await test_key_connectivity({
        "alias": "wild",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "https://example.com/v1",
        "models": "*",
        "provider_models": ["moonshotai/kimi-k2.6:free", "openai/gpt-4.1"],
    })

    assert result["ok"] is True
    assert result["model"] == "moonshotai/kimi-k2.6:free"
    assert called[0]["model"] == "moonshotai/kimi-k2.6:free"


@pytest.mark.asyncio
async def test_test_key_connectivity_wildcard_without_provider_models_errors(monkeypatch):
    """未同步 provider 模型列表时，应明确提示先同步模型列表。"""

    result = await test_key_connectivity({
        "alias": "wild",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "https://example.com/v1",
        "models": "*",
        "provider_models": [],
    })

    assert result["ok"] is False
    assert result["model"] == ""
    assert result["attempted_paths"] == []
    assert "请先保存或刷新模型" in result["error"]
