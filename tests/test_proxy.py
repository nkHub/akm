import pytest
from unittest.mock import AsyncMock, MagicMock
import httpx
from akm.proxy import forward_request
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
