import pytest
from akm import __version__
import tempfile
from unittest.mock import AsyncMock, MagicMock
import httpx
import asyncio
from akm.proxy import forward_request, test_key_connectivity as check_key_connectivity, _diagnose_no_key, redact_headers
from akm.agent import AGENT_REGISTRY
from akm.db import get_connection, init_db
from akm.http_client_pool import HttpClientPoolManager
from akm.key_pool import add_key, set_status


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    """每个测试使用独立数据库，避免诊断类测试互相污染。"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    monkeypatch.setattr("akm.crypto.SECRET_DIR", tmpdir)
    monkeypatch.setattr("akm.crypto._cipher", None)
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


class FakeChunkedStreamResponse(FakeStreamResponse):
    """模拟分块 SSE 响应，并支持观测是否已关闭。"""

    def __init__(self, status_code, chunks):
        self.status_code = status_code
        self._chunks = [c if isinstance(c, bytes) else str(c).encode("utf-8") for c in chunks]
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


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

    def _build_request(method, url, json=None, headers=None, timeout=None, data=None, files=None):
        req = httpx.Request(method, url, json=json, data=data, files=files, headers=headers)
        req.extensions["_akm_test_timeout"] = timeout
        return req

    client_mock.build_request = MagicMock(
        side_effect=_build_request
    )

    async def send_side_effect(req, stream=False):
        calls.append({"req": req, "stream": stream})
        if not responses:
            raise StopIteration("no more mock responses")
        return responses.pop(0)

    client_mock.send = AsyncMock(side_effect=send_side_effect)
    return calls


@pytest.mark.asyncio
async def test_http_client_pool_manager_lazily_isolates_route_clients():
    """相同路由复用同一 client，不同 key/model 路由懒创建独立 client。"""
    pool = HttpClientPoolManager(max_pools=4)
    try:
        first = await pool.get_client(provider="deepseek", key_alias="gs", model="deepseek-v4-pro", api_path="chat/completions")
        second = await pool.get_client(provider="deepseek", key_alias="gs", model="deepseek-v4-pro", api_path="chat/completions")
        third = await pool.get_client(provider="openai", key_alias="0029-pro", model="gpt-5.4", api_path="responses")

        assert first is second
        assert first is not third
        assert pool.stats()["pool_count"] == 2
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_forward_uses_route_scoped_client_after_key_selection(monkeypatch):
    """forward_request 应在 key 与上游协议确定后，按最终路由取隔离 client。"""
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "gs", "provider": "deepseek", "api_key": "sk-xxx",
        "base_url": "https://api.deepseek.com",
    }))

    route_client = AsyncMock()
    send_calls = _make_send_mock(route_client, [FakeStreamResponse(200, '{"choices":[{"message":{"content":"hi"}}]}')])

    class Pool:
        is_route_pool = True

        def __init__(self):
            self.calls = []

        async def get_client(self, **kwargs):
            self.calls.append(kwargs)
            return route_client

    pool = Pool()
    result = await forward_request(
        body={"model": "deepseek-v4-pro", "input": "hello", "stream": False},
        client=pool,
        api_path="responses",
    )

    assert result["status_code"] == 200
    assert pool.calls == [{
        "provider": "deepseek",
        "key_alias": "gs",
        "model": "deepseek-v4-pro",
        "api_path": "responses",
    }]
    assert send_calls[0]["stream"] is False


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
async def test_forward_accepts_any_2xx_response(monkeypatch):
    """上游任意 2xx 响应都应按成功透传，不触发 Key 错误策略。"""
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "ok", "provider": "openai", "api_key": "sk-xxx",
        "base_url": "https://api.openai.com",
    }))
    mock_client = AsyncMock()
    _make_send_mock(mock_client, [FakeStreamResponse(201, '{"id":"created"}')])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
        client=mock_client,
    )

    assert result["status_code"] == 201
    assert result["body"] == '{"id":"created"}'


@pytest.mark.asyncio
async def test_forward_excludes_plugin_replaced_key_after_failure(monkeypatch):
    """插件替换 Key 后，失败重试必须排除实际发送的替代 Key。"""
    selected_exclusions = []

    async def pick_key_mock(model, exclude_aliases=None):
        selected_exclusions.append(set(exclude_aliases or []))
        if len(selected_exclusions) == 1:
            return {"alias": "candidate", "provider": "openai", "api_key": "sk-a", "base_url": "https://api.openai.com"}
        assert selected_exclusions[-1] == {"replacement"}
        return {"alias": "fallback", "provider": "openai", "api_key": "sk-c", "base_url": "https://api.openai.com"}

    class ReplacingPM:
        def __init__(self):
            self.key_selection_count = 0

        def get_converter(self, from_fmt, to_fmt):
            return None

        async def run_hook(self, hook, ctx=None, **kwargs):
            if hook == "on_key_selected":
                self.key_selection_count += 1
                if self.key_selection_count == 1:
                    assert ctx is not None
                    ctx.key = {"alias": "replacement", "provider": "openai", "api_key": "sk-b", "base_url": "https://api.openai.com"}
                return ctx
            if hook == "on_upstream_error":
                return "switch"
            return ctx

    monkeypatch.setattr("akm.proxy.pick_key_async", pick_key_mock)
    mock_client = AsyncMock()
    _make_send_mock(mock_client, [FakeStreamResponse(500), FakeStreamResponse(200, '{"ok":true}')])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
        client=mock_client,
        plugin_manager=ReplacingPM(),
    )

    assert result["status_code"] == 200
    assert result["key_alias"] == "fallback"


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
        async def run_hook(self, hook, ctx=None, **kwargs):
            if hook == "on_request" and ctx is not None:
                # 阻断走 ctx.set_block，不再返回 on_request_block/__akm_action__
                ctx.set_block(
                    status_code=400,
                    error="blocked by guard",
                    security_action="block",
                    security_reason="request_code_secret:messages[0].content",
                    body='{"error":"blocked by guard"}',
                )
                return ctx
            return ctx if ctx is not None else kwargs

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
        client=AsyncMock(),
        plugin_manager=DummyPM(),
    )
    assert result["status_code"] == 400
    assert result["error"] == "blocked by guard"
    assert result["security_action"] == "block"


@pytest.mark.asyncio
async def test_forward_request_uses_redacted_payload_returned_by_on_request_plugin(monkeypatch):
    """on_request 若返回改写后的请求体，转发到上游的 payload 不应再包含原始敏感明文。"""
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "ok", "provider": "openai", "api_key": "sk-xxx",
        "base_url": "https://api.openai.com",
    }))

    class DummyPM:
        def get_converter(self, from_fmt, to_fmt):
            return None

        async def run_hook(self, hook, ctx=None, **kwargs):
            if hook == "on_request" and ctx is not None:
                req = dict(ctx.request)
                req["messages"] = [{"role": "user", "content": "token=[GITHUB-TOKEN]"}]
                ctx.set_request(req)
                return ctx
            return ctx if ctx is not None else kwargs

    mock_client = AsyncMock()
    send_calls = _make_send_mock(
        mock_client,
        [FakeStreamResponse(200, '{"choices":[{"message":{"content":"ok"}}]}')],
    )

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "token=ghp_abcdefghijklmnopqrstuvwxyz123456"}]},
        client=mock_client,
        plugin_manager=DummyPM(),
    )

    assert result["status_code"] == 200
    upstream_payload = send_calls[0]["req"].content.decode("utf-8")
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in upstream_payload
    assert "[GITHUB-TOKEN]" in upstream_payload


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

        async def run_hook(self, hook, ctx=None, **kwargs):
            return ctx if ctx is not None else kwargs

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
async def test_forward_records_upstream_raw_response_before_conversion(monkeypatch):
    """非流式下 upstream_response_body_for_log 应记录插件转换前的上游原始响应。"""

    class DummyRespToChat:
        _source_format = "responses"

        def convert_request(self, body):
            return dict(body)

        def convert_response(self, body):
            return body + "|chat"

    class DummyChatToMsg:
        _source_format = "chat"

        def convert_request(self, body):
            return dict(body)

        def convert_response(self, body):
            return body + "|msg"

    class DummyPM:
        def get_converter(self, from_fmt, to_fmt):
            if (from_fmt, to_fmt) == ("responses", "chat"):
                return DummyRespToChat()
            if (from_fmt, to_fmt) == ("chat", "messages"):
                return DummyChatToMsg()
            return None

        async def run_hook(self, hook, ctx=None, **kwargs):
            return ctx if ctx is not None else kwargs

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "k1",
        "provider": "anthropic",
        "api_key": "sk-ant",
        "base_url": "https://api.anthropic.com",
    }))

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"raw":"upstream"}')])

    result = await forward_request(
        body={"model": "claude-3", "input": "hi", "stream": False},
        client=mock_client,
        api_path="responses",
        plugin_manager=DummyPM(),
    )

    assert result["status_code"] == 200
    # 转换前原始响应 = 上游返回内容
    assert result["upstream_response_body_for_log"] == '{"raw":"upstream"}'
    # 转换后响应 = 发给客户端的 body（两段转换：先 chat→msg，再 responses→chat）
    assert result["body"] == '{"raw":"upstream"}|msg|chat'


@pytest.mark.asyncio
async def test_forward_chained_adapter_receives_provider_context(monkeypatch):
    """两段式转换链路也应把选中 key 的 provider 透传给每一段适配器。"""

    class DummyRespToChat:
        _source_format = "responses"

        def __init__(self):
            self.provider = ""

        def set_request_context(self, **kwargs):
            self.provider = str(kwargs.get("provider") or "")

        def convert_request(self, body):
            out = dict(body)
            out["first_provider_seen"] = self.provider
            return out

        def convert_response(self, body):
            return body

    class DummyChatToMsg:
        _source_format = "chat"

        def __init__(self):
            self.provider = ""

        def set_request_context(self, **kwargs):
            self.provider = str(kwargs.get("provider") or "")

        def convert_request(self, body):
            out = dict(body)
            out["second_provider_seen"] = self.provider
            return out

        def convert_response(self, body):
            return body

    class DummyPM:
        def __init__(self):
            self.first = DummyRespToChat()
            self.second = DummyChatToMsg()

        def get_converter(self, from_fmt, to_fmt):
            if (from_fmt, to_fmt) == ("responses", "chat"):
                return self.first
            if (from_fmt, to_fmt) == ("chat", "messages"):
                return self.second
            return None

        async def run_hook(self, hook, ctx=None, **kwargs):
            return ctx if ctx is not None else kwargs

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "k1",
        "provider": "anthropic",
        "api_key": "sk-ant",
        "base_url": "https://api.anthropic.com",
    }))

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, "hello")])

    result = await forward_request(
        body={"model": "claude-3", "input": "hi", "stream": False},
        client=mock_client,
        api_path="responses",
        plugin_manager=DummyPM(),
    )

    assert result["status_code"] == 200
    payload = send_calls[0]["req"].content.decode("utf-8")
    assert '"first_provider_seen":"anthropic"' in payload
    assert '"second_provider_seen":"anthropic"' in payload


@pytest.mark.asyncio
async def test_forward_messages_converter_receives_provider_context(monkeypatch):
    """messages 转 chat 转换时，应把选中 key 的 provider 传入协议转换器上下文。"""

    class DummyMessagesToChat:
        _source_format = "messages"

        def __init__(self):
            self.provider = ""

        def set_request_context(self, **kwargs):
            self.provider = str(kwargs.get("provider") or "")

        def convert_request(self, body):
            out = dict(body)
            out["provider_seen"] = self.provider
            return out

        def convert_response(self, body):
            return body

    class DummyPM:
        def __init__(self):
            self.adapter = DummyMessagesToChat()

        def get_converter(self, from_fmt, to_fmt):
            if (from_fmt, to_fmt) == ("messages", "chat"):
                return self.adapter
            return None

        async def run_hook(self, hook, ctx=None, **kwargs):
            return ctx if ctx is not None else kwargs

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "k1",
        "provider": "openai",
        "api_key": "sk-openai",
        "base_url": "https://api.openai.com",
    }))

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"choices":[{"message":{"content":"ok"}}]}')])

    result = await forward_request(
        body={"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 1024},
        client=mock_client,
        api_path="messages",
        plugin_manager=DummyPM(),
    )

    assert result["status_code"] == 200
    payload = send_calls[0]["req"].content.decode("utf-8")
    assert '"provider_seen":"openai"' in payload


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
async def test_forward_rerank_request_does_not_inject_stream_or_conversion(monkeypatch):
    """rerank 转发应参考 embeddings 走普通 JSON 透传，不注入 stream，也不走协议转换。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "rerank", "provider": "openai", "api_key": "sk-rerank",
        "base_url": "https://api.openai.com",
    }))

    class DummyPM:
        def get_converter(self, from_fmt, to_fmt):
            raise AssertionError("rerank 不应尝试获取协议转换器")

        async def run_hook(self, hook, ctx=None, **kwargs):
            return ctx if ctx is not None else kwargs

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"results":[{"index":0,"relevance_score":0.8}],"model":"rerank-v1"}')])

    result = await forward_request(
        body={"model": "rerank-v1", "query": "hello", "documents": ["a"]},
        client=mock_client,
        api_path="rerank",
        plugin_manager=DummyPM(),
    )

    assert result["status_code"] == 200
    assert send_calls[0]["stream"] is False
    payload = send_calls[0]["req"].content.decode("utf-8")
    assert '"stream":' not in payload


@pytest.mark.asyncio
async def test_forward_image_generations_request_does_not_inject_stream_or_conversion(monkeypatch):
    """图片生成转发应按普通 JSON 透传，不注入 stream，也不走协议转换。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "image", "provider": "openai", "api_key": "__AKM_CREDENTIAL_VALUE_63353636d4c9__",
        "base_url": "https://api.openai.com",
    }))

    class DummyPM:
        def get_converter(self, from_fmt, to_fmt):
            raise AssertionError("images/generations 不应尝试获取协议转换器")

        async def run_hook(self, hook, ctx=None, **kwargs):
            return ctx if ctx is not None else kwargs

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"created":123,"data":[{"b64_json":"abc"}]}')])

    result = await forward_request(
        body={"model": "gpt-image-1", "prompt": "draw a cat"},
        client=mock_client,
        api_path="images/generations",
        plugin_manager=DummyPM(),
    )

    assert result["status_code"] == 200
    assert send_calls[0]["stream"] is False
    payload = send_calls[0]["req"].content.decode("utf-8")
    assert send_calls[0]["req"].headers["User-Agent"] == f"akm/{__version__}"
    assert '"stream":' not in payload


@pytest.mark.asyncio
async def test_forward_image_generations_request_can_use_native_user_agent(monkeypatch):
    """开启 use_native_user_agent 后，图片请求也应透传原始 User-Agent。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "image", "provider": "openai", "api_key": "__AKM_CREDENTIAL_VALUE_63353636d4c9__",
        "base_url": "https://api.openai.com",
    }))
    monkeypatch.setattr("akm.agent.config_get", lambda key, default=None: True if key == "use_native_user_agent" else default)

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"created":123,"data":[{"b64_json":"abc"}]}')])

    result = await forward_request(
        body={"model": "gpt-image-1", "prompt": "draw a cat"},
        client=mock_client,
        api_path="images/generations",
        original_user_agent="OpenCode/9.9.9",
    )

    assert result["status_code"] == 200
    assert send_calls[0]["req"].headers["User-Agent"] == "OpenCode/9.9.9"


@pytest.mark.asyncio
async def test_forward_passthrough_headers_in_native_mode(monkeypatch):
    """开启 use_native_user_agent 后，客户端业务头应透传，认证/传输基础设施头应被排除。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "gs-codex", "provider": "openai", "api_key": "__AKM_CREDENTIAL_VALUE_63353636d4c9__",
        "base_url": "https://api.openai.com",
    }))
    monkeypatch.setattr("akm.agent.config_get", lambda key, default=None: True if key == "use_native_user_agent" else default)
    monkeypatch.setattr("akm.proxy.load_config", lambda: {"use_native_user_agent": True})

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"created":123,"object":"chat.completion","model":"m","choices":[]}')])

    client_headers = {
        "authorization": "Bearer client-token",
        "host": "127.0.0.1:8800",
        "content-length": "63348",
        "connection": "keep-alive",
        "accept-encoding": "gzip, deflate, br",
        "user-agent": "Codex Desktop/9.9.9",
        "originator": "Codex Desktop",
        "session-id": "019fb7bd-6079-7d03",
        "x-codex-turn-metadata": '{"installation_id":"i1","sandbox":"seatbelt"}',
        "x-openai-internal-codex-responses-lite": "true",
        "accept": "text/event-stream",
    }

    result = await forward_request(
        body={"model": "gpt-5", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
        client=mock_client,
        api_path="responses",
        original_user_agent="Codex Desktop/9.9.9",
        passthrough_headers=client_headers,
    )

    assert result["status_code"] == 200
    sent = send_calls[0]["req"].headers
    # 业务头应透传到上游
    assert sent["originator"] == "Codex Desktop"
    assert sent["session-id"] == "019fb7bd-6079-7d03"
    assert sent["x-codex-turn-metadata"] == '{"installation_id":"i1","sandbox":"seatbelt"}'
    assert sent["x-openai-internal-codex-responses-lite"] == "true"
    assert sent["accept"] == "text/event-stream"
    # 认证头被替换为所选 key 密钥，客户端原始认证头不生效
    assert sent["Authorization"] == "Bearer __AKM_CREDENTIAL_VALUE_63353636d4c9__"
    # 传输基础设施头被排除，交由 httpx 重建（host 按上游 URL 生成）
    assert sent["host"] == "api.openai.com"
    assert "connection" not in sent
    assert "accept-encoding" not in sent
    assert sent.get("content-length") != "63348"
    # User-Agent 走 _resolve_user_agent：原生模式 + override 为空 → 原生 UA
    assert sent["User-Agent"] == "Codex Desktop/9.9.9"


@pytest.mark.asyncio
async def test_forward_passthrough_disabled_when_native_off(monkeypatch):
    """未开启 use_native_user_agent 时，透传头不生效，上游仍只发基础头。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "x", "provider": "openai", "api_key": "__AKM_CREDENTIAL_VALUE_63353636d4c9__",
        "base_url": "https://api.openai.com",
    }))
    monkeypatch.setattr("akm.proxy.load_config", lambda: {"use_native_user_agent": False})
    monkeypatch.setattr("akm.agent.config_get", lambda key, default=None: default)

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"created":123,"object":"chat.completion","model":"m","choices":[]}')])

    result = await forward_request(
        body={"model": "gpt-5", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
        client=mock_client,
        api_path="responses",
        original_user_agent="Codex Desktop/9.9.9",
        passthrough_headers={"originator": "Codex Desktop", "x-codex-turn-metadata": "{}"},
    )

    assert result["status_code"] == 200
    sent = send_calls[0]["req"].headers
    # 原生模式未开启，客户端业务头不随转发携带
    assert "originator" not in sent
    assert "x-codex-turn-metadata" not in sent
    # 未开启原生 + 无 override → 回退 akm/<version>
    assert sent["User-Agent"] == f"akm/{__version__}"


@pytest.mark.asyncio
async def test_forward_image_generations_request_honors_custom_request_timeout(monkeypatch):
    """图片生成接口应允许调用方覆盖单次上游请求超时。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "image", "provider": "openai", "api_key": "__AKM_CREDENTIAL_VALUE_63353636d4c9__",
        "base_url": "https://api.openai.com",
    }))

    class DummyPM:
        def get_converter(self, from_fmt, to_fmt):
            raise AssertionError("images/generations 不应尝试获取协议转换器")

        async def run_hook(self, hook, ctx=None, **kwargs):
            return ctx if ctx is not None else kwargs

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"created":123,"data":[{"b64_json":"abc"}]}')])

    result = await forward_request(
        body={"model": "gpt-image-1", "prompt": "draw a cat"},
        client=mock_client,
        api_path="images/generations",
        plugin_manager=DummyPM(),
        request_timeout=300,
    )

    assert result["status_code"] == 200
    assert send_calls[0]["req"].extensions.get("_akm_test_timeout") == 300


@pytest.mark.asyncio
async def test_forward_image_edits_request_uses_multipart_passthrough(monkeypatch):
    """图片编辑应使用 multipart 透传，不注入 JSON Content-Type，也不走协议转换。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "image", "provider": "openai", "api_key": "__AKM_CREDENTIAL_VALUE_63353636d4c9__",
        "base_url": "https://api.openai.com",
    }))

    class DummyPM:
        def get_converter(self, from_fmt, to_fmt):
            raise AssertionError("images/edits 不应尝试获取协议转换器")

        async def run_hook(self, hook, ctx=None, **kwargs):
            return ctx if ctx is not None else kwargs

    mock_client = AsyncMock()
    send_calls = _make_send_mock(mock_client, [FakeStreamResponse(200, '{"created":123,"data":[{"b64_json":"abc"}]}')])

    result = await forward_request(
        body={
            "model": "gpt-image-2",
            "__akm_multipart__": True,
            "__akm_form_fields__": {"model": "gpt-image-2", "prompt": "edit it"},
            "__akm_form_files__": {"image": ("cat.png", b"fake-bytes", "image/png")},
        },
        client=mock_client,
        api_path="images/edits",
        plugin_manager=DummyPM(),
    )

    assert result["status_code"] == 200
    assert send_calls[0]["stream"] is False
    req = send_calls[0]["req"]
    assert req.headers["Content-Type"].startswith("multipart/form-data;")
    assert req.headers["User-Agent"] == f"akm/{__version__}"
    body = b"".join(req.stream)
    assert b"filename=\"cat.png\"" in body


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

        async def run_hook(self, hook, ctx=None, **kwargs):
            # 对齐 PluginManager：先把 kwargs["response"] 写入 ctx
            if ctx is not None and "response" in kwargs and isinstance(kwargs.get("response"), dict):
                ctx.response = kwargs["response"]
            if hook == "on_response" and ctx is not None:
                self.events.append({"response": ctx.response, "request": ctx.request})
                return ctx
            return ctx if ctx is not None else kwargs

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

        async def run_hook(self, hook, ctx=None, **kwargs):
            # 对齐 PluginManager：先把 kwargs["response"] 写入 ctx
            if ctx is not None and "response" in kwargs and isinstance(kwargs.get("response"), dict):
                ctx.response = kwargs["response"]
            if hook == "on_response" and ctx is not None and isinstance(ctx.response, dict) and ctx.response.get("ok"):
                resp = dict(ctx.response)
                resp["response_body"] = '{"choices":[{"message":{"content":"blocked"}}]}'
                ctx.response = resp
                return ctx
            return ctx if ctx is not None else kwargs

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
async def test_forward_streaming_does_not_emit_on_response_before_stream_end(monkeypatch):
    """流式请求不应在 proxy 返回时就触发 on_response，避免提前回收并发计数。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "k1", "provider": "openai", "api_key": "sk-a",
        "base_url": "https://api.openai.com",
    }))

    class DummyPM:
        def __init__(self):
            self.events = []

        def get_converter(self, from_fmt, to_fmt):
            return None

        async def run_hook(self, hook, ctx=None, **kwargs):
            if hook == "on_response":
                self.events.append({"response": getattr(ctx, "response", None)})
            return ctx if ctx is not None else kwargs

    pm = DummyPM()
    mock_client = AsyncMock()
    _make_send_mock(mock_client, [FakeChunkedStreamResponse(200, [b"data: one\n\n", b"data: [DONE]\n\n"])])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}], "stream": True},
        client=mock_client,
        plugin_manager=pm,
    )

    assert result["status_code"] == 200
    assert result["stream"] is True
    assert pm.events == []


@pytest.mark.asyncio
async def test_forward_streaming_returns_local_request_with_reverse_map(monkeypatch):
    """流式成功返回必须携带 request_context（bag reverse_map）与兼容 local_request。"""

    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "k1", "provider": "openai", "api_key": "sk-a",
        "base_url": "https://api.openai.com",
    }))

    class DummyPM:
        def get_converter(self, from_fmt, to_fmt):
            return None

        async def run_hook(self, hook, ctx=None, **kwargs):
            if hook == "on_request" and ctx is not None:
                # 模拟 data_filter_guard：改写 request，reverse_map 进 bag
                changed = dict(ctx.request)
                changed["messages"] = [{"role": "user", "content": "<AKM-SEC:x@1:abc123/>"}]
                ctx.set_request(changed)
                ctx.bag_set("data_filter_guard.reverse_map", {"<AKM-SEC:x@1:abc123/>": "secret"})
                return ctx
            return ctx if ctx is not None else kwargs

    mock_client = AsyncMock()
    _make_send_mock(mock_client, [FakeChunkedStreamResponse(200, [b"data: one\n\n", b"data: [DONE]\n\n"])])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "secret"}], "stream": True},
        client=mock_client,
        plugin_manager=DummyPM(),
    )

    assert result["stream"] is True
    stream_ctx = result["request_context"]
    assert stream_ctx.bag_get("data_filter_guard.reverse_map") == {"<AKM-SEC:x@1:abc123/>": "secret"}
    local = result["local_request"]
    assert isinstance(local, dict)
    assert local["messages"][0]["content"] == "<AKM-SEC:x@1:abc123/>"
    # reverse_map 不在 request 上，上游/日志也不得带私有字段
    assert "__akm_reverse_map__" not in local
    assert "__akm_reverse_map__" not in result["request_body_for_log"]


@pytest.mark.asyncio
async def test_chained_adapter_streams_incrementally_without_buffering_full_midstream():
    """链式协议转换应保持增量转发，不能先把整段中间流攒满再输出。"""
    from akm.proxy import _ChainedAdapter

    seen_mid_chunks = []

    class FirstAdapter:
        async def convert_sse_stream(self, upstream_stream):
            async for part in upstream_stream:
                seen_mid_chunks.append(part)
                yield f"OUT:{part}"

    class SecondAdapter:
        async def convert_sse_stream(self, upstream_stream):
            async for chunk in upstream_stream:
                text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
                yield f"MID:{text}"

    async def upstream():
        yield b"a"
        yield b"b"

    adapter = _ChainedAdapter(FirstAdapter(), SecondAdapter())
    outputs = []
    async for line in adapter.convert_sse_stream(upstream()):
        outputs.append(line)

    assert seen_mid_chunks == ["MID:a", "MID:b"]
    assert outputs == ["OUT:MID:a", "OUT:MID:b"]


@pytest.mark.asyncio
async def test_test_key_connectivity_openai_uses_chat_only(monkeypatch):
    """默认情况下 openai 类 key 测试时只请求 chat/completions，不自动回退。"""

    called_urls = []

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            called_urls.append(url)
            return FakeTestResponse(403, '{"error":{"message":"restricted","code":"codex_access_restricted"}}')

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    result = await check_key_connectivity({
        "alias": "share",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "https://example.com",
        "models": "gpt-5.4",
    })

    assert result["ok"] is False
    assert result["api_path"] == "chat/completions"
    assert result["attempted_paths"] == ["chat/completions"]
    assert called_urls == ["https://example.com/v1/chat/completions"]


@pytest.mark.asyncio
async def test_test_key_connectivity_openai_falls_back_when_enabled(monkeypatch):
    """显式开启 fallback 后，openai 类 key 可从 chat/completions 回退到 responses。"""

    responses = [
        FakeTestResponse(404, '{"error":{"message":"not found"}}'),
        FakeTestResponse(200, '{"id":"ok"}'),
    ]

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            return responses.pop(0)

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    result = await check_key_connectivity({
        "alias": "share",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "https://example.com",
        "models": "gpt-5.4",
    }, allow_fallback=True)

    assert result["ok"] is True
    assert result["api_path"] == "responses"
    assert result["attempted_paths"] == ["chat/completions", "responses"]
    assert result["fallback_used"] is True


@pytest.mark.asyncio
async def test_test_key_connectivity_deepseek_prefers_chat(monkeypatch):
    """deepseek 同时支持 chat 与 responses，测试时应首选 chat/completions。"""

    called_urls = []

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            called_urls.append(url)
            return FakeTestResponse(200, '{"id":"ok"}')

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    result = await check_key_connectivity({
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
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            called.append((url, headers, json))
            return FakeTestResponse(200, '{"id":"ok"}')

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    result = await check_key_connectivity({
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
async def test_test_key_connectivity_custom_agent_uses_first_supported_format(monkeypatch):
    """自定义供应商测试时，应优先请求其第一个启用的协议格式。"""

    called = []

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            called.append((url, json))
            return FakeTestResponse(200, '{"id":"ok"}')

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    AGENT_REGISTRY["vendor-chat-first"] = AGENT_REGISTRY["openai"].__class__(
        name="vendor-chat-first",
        default_base_url="https://vendor.example.com",
        supports_chat=True,
        supports_responses=True,
        supports_messages=True,
    )

    try:
        result = await check_key_connectivity({
            "alias": "vendor-chat-first-key",
            "provider": "vendor-chat-first",
            "api_key": "__AKM_CREDENTIAL_VALUE_ff9f2df28bc7__",
            "base_url": "https://vendor.example.com",
            "models": "vendor-model",
        })
    finally:
        del AGENT_REGISTRY["vendor-chat-first"]

    assert result["ok"] is True
    assert result["api_path"] == "chat/completions"
    assert result["attempted_paths"] == ["chat/completions"]
    assert called[0][0] == "https://vendor.example.com/v1/chat/completions"
    assert called[0][1]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_test_key_connectivity_messages_provider_without_anthropic_switch(monkeypatch):
    """供应商即使原生支持 messages，未开启开关时也不应自动改写到 /anthropic。"""

    called = []

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
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
        result = await check_key_connectivity({
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
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            called.append(json)
            return FakeTestResponse(200, '{"id":"ok"}')

    monkeypatch.setattr("akm.proxy.httpx.AsyncClient", DummyAsyncClient)

    result = await check_key_connectivity({
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

    result = await check_key_connectivity({
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


def test_redact_headers_masks_sensitive_values_keeps_keys():
    """敏感请求头值应被掩码，但键名与非敏感头应原样保留。"""
    out = redact_headers({
        "Authorization": "Bearer sk-secret-123",
        "x-api-key": "abc123",
        "Cookie": "session=xyz",
        "X-Custom": "keep-me",
        "user-agent": "test-client/1.0",
    })
    assert out["Authorization"] == "Bearer ***"
    assert out["x-api-key"] == "***"
    assert out["Cookie"] == "***"
    assert out["X-Custom"] == "keep-me"
    assert out["user-agent"] == "test-client/1.0"


def test_redact_headers_case_insensitive_and_empty():
    """脱敏匹配应大小写不敏感，空输入应返回空对象。"""
    assert redact_headers({"AUTHORIZATION": "abc"}) == {"AUTHORIZATION": "***"}
    assert redact_headers(None) == {}
    assert redact_headers({}) == {}


class FakeSlowStreamResponse:
    """模拟“接受了请求但迟迟不产首字节”的上游响应。"""

    def __init__(self, status_code, delay, chunks):
        self.status_code = status_code
        self._delay = delay
        self._chunks = chunks
        self.closed = False

    async def aiter_bytes(self):
        await asyncio.sleep(self._delay)
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_forward_stream_prefetches_first_chunk(monkeypatch):
    """流式请求成功时，应预读首块并通过 first_chunk 回传（避免丢块）。"""
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value={
        "alias": "ok", "provider": "openai", "api_key": "sk-xxx",
        "base_url": "https://api.openai.com",
    }))
    mock_client = AsyncMock()
    _make_send_mock(mock_client, [
        FakeChunkedStreamResponse(200, ["data: {\"x\":1}\n\n", "data: {\"x\":2}\n\n"])
    ])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        client=mock_client,
        api_path="chat/completions",
    )

    assert result["stream"] is True
    assert result["status_code"] == 200
    assert result["key_alias"] == "ok"
    # 首块已被 proxy 预读并缓存回传，server 端据此先输出再续读
    assert result["first_chunk"] == b"data: {\"x\":1}\n\n"
    # 必须回传同一流式生成器：httpx aiter_raw 只能消费一次，server 侧需复用
    # 该生成器继续迭代，否则二次调用 resp.aiter_bytes() 会抛 StreamConsumed
    assert result["aiter"] is not None
    # 复用生成器继续读取应拿到剩余块（而非从头重复首块或抛异常）
    rest = [c async for c in result["aiter"]]
    assert rest == [b"data: {\"x\":2}\n\n"]


@pytest.mark.asyncio
async def test_forward_stream_first_byte_timeout_switches_key(monkeypatch):
    """首字节超时（上游 2xx 后迟迟不产 body）时应关闭该上游并切换到下一个 Key。"""
    monkeypatch.setattr("akm.proxy._proxy_first_byte_timeout", lambda: 0.1)
    key1 = {"alias": "slow", "provider": "openai", "api_key": "sk-xxx",
            "base_url": "https://api.openai.com"}
    key2 = {"alias": "fast", "provider": "openai", "api_key": "sk-yyy",
            "base_url": "https://api.openai.com"}
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(side_effect=[key1, key2]))
    mock_client = AsyncMock()
    slow_resp = FakeSlowStreamResponse(200, delay=5.0, chunks=[b"data: {\"x\":1}\n\n"])
    fast_resp = FakeChunkedStreamResponse(200, ["data: {\"x\":2}\n\n", "data: [DONE]\n\n"])
    _make_send_mock(mock_client, [slow_resp, fast_resp])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        client=mock_client,
        api_path="chat/completions",
    )

    # 第一个 Key 首字节超时被跳过，切到第二个 Key 成功
    assert result["stream"] is True
    assert result["status_code"] == 200
    assert result["key_alias"] == "fast"
    assert result["first_chunk"] == b"data: {\"x\":2}\n\n"
    assert slow_resp.closed is True


@pytest.mark.asyncio
async def test_forward_attempts_records_failed_and_success(monkeypatch):
    """逐次尝试记录 attempts：失败尝试携带上游返回内容片段，成功尝试不携带。"""
    keys_called = []

    async def pick_key_mock(model, exclude_aliases=None):
        keys_called.append(model)
        if len(keys_called) == 1:
            return {"alias": "k1", "provider": "openai", "api_key": "sk-a",
                    "base_url": "https://api.openai.com"}
        return {"alias": "k2", "provider": "openai", "api_key": "sk-b",
                "base_url": "https://api.openai.com"}

    monkeypatch.setattr("akm.proxy.pick_key_async", pick_key_mock)
    monkeypatch.setattr("akm.proxy.mark_rate_limited", lambda alias: None)
    monkeypatch.setattr("akm.proxy.set_status", lambda alias, status: None)

    mock_client = AsyncMock()
    _make_send_mock(mock_client, [
        FakeStreamResponse(429, '{"error":{"message":"rate limited"}}'),
        FakeStreamResponse(200, '{"choices":[{"message":{"content":"ok"}}]}'),
    ])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
        client=mock_client,
    )
    assert result["status_code"] == 200
    assert result["key_alias"] == "k2"
    attempts = result.get("attempts") or []
    assert len(attempts) >= 2
    # 第一条：k1 429 失败，附带上游返回内容片段
    a0 = attempts[0]
    assert a0["status_code"] == 429
    assert a0["key_alias"] == "k1"
    assert a0["response_body"] == '{"error":{"message":"rate limited"}}'
    assert a0.get("error")
    # 最后一条：k2 200 成功，不携带返回内容
    a_last = attempts[-1]
    assert a_last["status_code"] == 200
    assert a_last["key_alias"] == "k2"
    assert not a_last.get("response_body")


@pytest.mark.asyncio
async def test_forward_local_only_502_excludes_select_key_record(monkeypatch):
    """无可用 key 的 select_key 本地 502 不应进入 attempts。

    该状态码没有真实 Key，与「逐次尝试」语义不符（前端原始报错 tab 顶部
    已有错误诊断红块展示）。注意：每条真实 Key 的失败仍会被逐条记录。
    """
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value=None))
    mock_client = AsyncMock()
    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
        client=mock_client,
    )
    assert result["status_code"] in (502, 503)
    # attempts 应为空：本地合成的 select_key 记录不再写入，也没有任何真实 Key 被尝试
    attempts = result.get("attempts") or []
    assert not any(a.get("phase") == "select_key" for a in attempts)
    assert not any(a.get("phase") == "exhausted" for a in attempts)


@pytest.mark.asyncio
async def test_forward_attempts_excludes_exhausted_local_record(monkeypatch):
    """有匹配 key 但全部尝试失败时，attempts 不应包含 exhausted 本地合成的 502。

    该记录没有真实 Key，与「逐次尝试」语义不符，各 Key 的失败已逐条记录。
    """
    picked = []

    async def pick_key_multi(model, exclude_aliases=None):
        picked.append({"model": model, "exclude": list(exclude_aliases or [])})
        # 第一次给 k1，之后给 k2：两者都失败，让 while 循环跑满 2 次后走 exhausted
        return {"alias": "k1", "provider": "openai", "api_key": "sk-a",
                "base_url": "https://api.openai.com"} if not exclude_aliases else {"alias": "k2", "provider": "openai", "api_key": "sk-b", "base_url": "https://api.openai.com"}

    monkeypatch.setattr("akm.proxy.pick_key_async", pick_key_multi)
    monkeypatch.setattr("akm.proxy.pick_wildcard_key_async", AsyncMock(return_value=None))
    monkeypatch.setattr("akm.proxy.mark_rate_limited", lambda alias: None)
    monkeypatch.setattr("akm.proxy.set_status", lambda alias, status: None)
    # 限制尝试次数与单 key 重试，保证请求量可控且走完 exhausted 兜底
    monkeypatch.setattr("akm.proxy.load_config", lambda: {
        "proxy_max_key_tries": 2,
        "proxy_max_retries_per_key": 0,
        "proxy_retry_backoff_base_sec": 0.1,
    })

    mock_client = AsyncMock()
    _make_send_mock(mock_client, [
        FakeStreamResponse(429, '{"error":{"message":"rate limited"}}'),
        FakeStreamResponse(429, '{"error":{"message":"rate limited"}}'),
    ])

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
        client=mock_client,
    )
    assert result["status_code"] == 502
    assert "所有 key 均已尝试但均失败" in result["error"]
    attempts = result.get("attempts") or []
    assert len(attempts) >= 2
    # exhausted 本地合成 502 不应进入 attempts；各 Key 失败记录保留
    assert not any(a.get("phase") == "exhausted" for a in attempts)
    assert attempts[-1]["key_alias"] == "k2"
    assert attempts[-1]["status_code"] == 429
