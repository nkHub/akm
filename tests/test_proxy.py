import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
from akm.proxy import forward_request, _build_upstream_url


class FakeResponse:
    """模拟 httpx.Response"""
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=MagicMock(), response=self)


def test_build_upstream_url():
    assert _build_upstream_url("https://api.openai.com") == \
        "https://api.openai.com/v1/chat/completions"
    assert _build_upstream_url("https://api.deepseek.com/v1") == \
        "https://api.deepseek.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_forward_success(monkeypatch):
    """正常转发成功返回"""
    monkeypatch.setattr("akm.proxy.pick_key", lambda model: {
        "alias": "ok", "provider": "openai", "api_key": "sk-xxx",
        "base_url": "https://api.openai.com",
    })
    mock_client = AsyncMock()
    mock_client.post.return_value = FakeResponse(200, {"choices": [{"message": {"content": "hi"}}]})

    with patch("akm.proxy.httpx.AsyncClient", return_value=mock_client):
        result = await forward_request(
            body={"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
            client=mock_client,
            log_callback=None,
        )
    assert result["status_code"] == 200
    assert result["key_alias"] == "ok"


@pytest.mark.asyncio
async def test_forward_429_switches_key(monkeypatch):
    """429 限流后切换下一个 key"""
    keys_called = []

    def pick_key_mock(model):
        keys_called.append(model)
        if len(keys_called) == 1:
            return {"alias": "k1", "provider": "openai", "api_key": "sk-a",
                    "base_url": "https://api.openai.com"}
        else:
            return {"alias": "k2", "provider": "openai", "api_key": "sk-b",
                    "base_url": "https://api.openai.com"}

    monkeypatch.setattr("akm.proxy.pick_key", pick_key_mock)
    monkeypatch.setattr("akm.proxy.mark_rate_limited", lambda alias: None)

    mock_client = AsyncMock()
    mock_client.post.side_effect = [
        FakeResponse(429),
        FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
    ]

    with patch("akm.proxy.httpx.AsyncClient", return_value=mock_client):
        result = await forward_request(
            body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
            client=mock_client,
            log_callback=None,
        )
    assert result["status_code"] == 200
    assert result["key_alias"] == "k2"
    assert len(keys_called) >= 2


@pytest.mark.asyncio
async def test_forward_402_disables_key(monkeypatch):
    """402 余额不足后禁用 key 并切换"""
    keys_called = []

    def pick_key_mock(model):
        keys_called.append(model)
        if len(keys_called) == 1:
            return {"alias": "k1", "provider": "openai", "api_key": "sk-a",
                    "base_url": "https://api.openai.com"}
        else:
            return {"alias": "k2", "provider": "openai", "api_key": "sk-b",
                    "base_url": "https://api.openai.com"}

    monkeypatch.setattr("akm.proxy.pick_key", pick_key_mock)
    monkeypatch.setattr("akm.proxy.set_status", lambda alias, status: None)

    mock_client = AsyncMock()
    mock_client.post.side_effect = [
        FakeResponse(402),
        FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
    ]

    with patch("akm.proxy.httpx.AsyncClient", return_value=mock_client):
        result = await forward_request(
            body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
            client=mock_client,
            log_callback=None,
        )
    assert result["status_code"] == 200
    assert result["key_alias"] == "k2"


@pytest.mark.asyncio
async def test_forward_401_disables_key(monkeypatch):
    """401 认证失败后禁用 key 并切换"""
    keys_called = []

    def pick_key_mock(model):
        keys_called.append(model)
        if len(keys_called) == 1:
            return {"alias": "k1", "provider": "openai", "api_key": "sk-a",
                    "base_url": "https://api.openai.com"}
        else:
            return {"alias": "k2", "provider": "openai", "api_key": "sk-b",
                    "base_url": "https://api.openai.com"}

    monkeypatch.setattr("akm.proxy.pick_key", pick_key_mock)
    monkeypatch.setattr("akm.proxy.set_status", lambda alias, status: None)

    mock_client = AsyncMock()
    mock_client.post.side_effect = [
        FakeResponse(401),
        FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
    ]

    with patch("akm.proxy.httpx.AsyncClient", return_value=mock_client):
        result = await forward_request(
            body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
            client=mock_client,
            log_callback=None,
        )
    assert result["status_code"] == 200
    assert result["key_alias"] == "k2"


@pytest.mark.asyncio
async def test_forward_all_keys_exhausted(monkeypatch):
    """所有 key 都不可用时返回 503"""
    monkeypatch.setattr("akm.proxy.pick_key", lambda model: None)

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
        client=AsyncMock(),
        log_callback=None,
    )
    assert result["status_code"] == 503
    assert "没有可用" in result["error"]
