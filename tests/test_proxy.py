import pytest
from unittest.mock import AsyncMock, MagicMock
import httpx
from akm.proxy import forward_request, _build_upstream_url


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


def test_build_upstream_url():
    assert _build_upstream_url("https://api.openai.com") == \
        "https://api.openai.com/v1/chat/completions"
    assert _build_upstream_url("https://api.deepseek.com/v1") == \
        "https://api.deepseek.com/v1/chat/completions"


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

    async def pick_key_mock(model):
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

    async def pick_key_mock(model):
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

    async def pick_key_mock(model):
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
