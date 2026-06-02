import pytest
from unittest.mock import AsyncMock, MagicMock
import httpx
from akm.proxy import forward_request, test_key_connectivity
from akm.agent import AGENT_REGISTRY


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
    async def send_side_effect(req, stream=False):
        if not responses:
            raise StopIteration("no more mock responses")
        return responses.pop(0)

    client_mock.send = AsyncMock(side_effect=send_side_effect)


@pytest.mark.asyncio
async def test_forward_success(monkeypatch):
    """正常转发成功返回"""
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "ok", "provider": "openai", "api_key": "sk-xxx",
        "base_url": "https://api.openai.com",
    }))
    mock_client = AsyncMock()
    _make_send_mock(mock_client, [FakeStreamResponse(200, '{"choices":[{"message":{"content":"hi"}}]}')])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
        client=mock_client,
    )
    assert result["status_code"] == 200
    assert result["key_alias"] == "ok"


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
    _make_send_mock(mock_client, [FakeStreamResponse(200, "hello")])

    result = await forward_request(
        body={"model": "claude-3", "input": "hi", "stream": False},
        client=mock_client,
        api_path="responses",
        plugin_manager=DummyPM(),
    )

    assert result["status_code"] == 200
    # 两段 convert_response：second 后 first，最终应为 hello|chat|resp
    assert result["body"] == "hello|chat|resp"


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
