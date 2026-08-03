"""Tavily MCP 客户端与 tavily_search 内置工具的测试。"""

import json as _json

import httpx
import pytest
from fastapi import FastAPI

from akm.agent_runtime.loop import ToolDef
from akm.agent_runtime import tavily_mcp
from akm.agent_runtime.tavily_mcp import (
    TavilyMCPClient,
    TavilyMCPError,
    _extract_text,
    tavily_search,
)
from akm.agent_runtime.tools import build_builtin_tools


class FakeHTTP:
    """按 JSON-RPC method 返回预设响应的假 http 客户端。"""

    def __init__(self, responses=None, sse_for=()):
        self.calls = []
        self.responses = responses or {}
        self.sse_for = set(sse_for)

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        method = (json or {}).get("method")
        if method in self.sse_for:
            payload, resp_headers = self.responses[method]
            return httpx.Response(
                200,
                text=f"data: {_json.dumps(payload)}\n\n",
                headers={"content-type": "text/event-stream", **(resp_headers or {})},
            )
        payload, resp_headers = self.responses.get(method, ({}, {}))
        return httpx.Response(200, json=payload, headers=resp_headers or {})


@pytest.fixture(autouse=True)
def _tavily_key(monkeypatch):
    """测试期间固定 tavily_api_key。"""
    monkeypatch.setattr(
        tavily_mcp, "load_config", lambda: {"tavily_api_key": "test-key"}
    )


# ── TavilyMCPClient ──


def test_endpoint_url_with_key():
    url = TavilyMCPClient._endpoint_url()
    assert url == "https://mcp.tavily.com/mcp/?tavilyApiKey=test-key"


def test_endpoint_url_missing_key(monkeypatch):
    monkeypatch.setattr(tavily_mcp, "load_config", lambda: {"tavily_api_key": ""})
    with pytest.raises(TavilyMCPError, match="tavily_api_key"):
        TavilyMCPClient._endpoint_url()


@pytest.mark.asyncio
async def test_call_tool_full_flow():
    """验证 initialize → initialized 通知 → tools/call 的请求序列与 session 透传。"""
    http = FakeHTTP(
        responses={
            "initialize": (
                {"jsonrpc": "2.0", "result": {"protocolVersion": "2025-06-18"}},
                {"mcp-session-id": "sess-1"},
            ),
            "tools/call": (
                {
                    "jsonrpc": "2.0",
                    "result": {
                        "content": [
                            {"type": "text", "text": '["result one", "result two"]'},
                        ]
                    },
                },
                {},
            ),
        }
    )
    client = TavilyMCPClient(http)
    result = await client.call_tool("tavily-search", {"query": "hello"})

    assert result["content"][0]["text"] == '["result one", "result two"]'
    methods = [call["json"]["method"] for call in http.calls]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    # 第二个请求（通知）开始应携带服务器下发的会话标识
    assert http.calls[1]["headers"].get("Mcp-Session-Id") == "sess-1"
    assert http.calls[2]["headers"].get("Mcp-Session-Id") == "sess-1"
    # tools/call 参数透传
    assert http.calls[2]["json"]["params"] == {
        "name": "tavily-search",
        "arguments": {"query": "hello"},
    }


@pytest.mark.asyncio
async def test_call_tool_accepts_sse_response():
    """服务器以 text/event-stream 返回时也能解析 JSON-RPC 响应。"""
    http = FakeHTTP(
        responses={
            "initialize": ({"jsonrpc": "2.0", "result": {"protocolVersion": "2025-06-18"}}, {}),
            "tools/call": (
                {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "ok"}]}},
                {},
            ),
        },
        sse_for=("tools/call",),
    )
    client = TavilyMCPClient(http)
    result = await client.call_tool("tavily-search", {"query": "x"})
    assert result["content"][0]["text"] == "ok"


@pytest.mark.asyncio
async def test_call_tool_raises_on_jsonrpc_error():
    http = FakeHTTP(
        responses={
            "initialize": (
                {"jsonrpc": "2.0", "error": {"code": -32000, "message": "bad key"}},
                {},
            ),
        }
    )
    client = TavilyMCPClient(http)
    with pytest.raises(TavilyMCPError, match="initialize"):
        await client.call_tool("tavily-search", {"query": "x"})


def test_extract_text_skips_non_text_content():
    result = {
        "content": [
            {"type": "text", "text": "a"},
            {"type": "image", "data": "x"},
            {"type": "text", "text": ""},
            {"type": "text", "text": "b"},
        ]
    }
    assert _extract_text(result) == "a\nb"


# ── tavily_search 参数规范化 ──


@pytest.mark.asyncio
async def test_tavily_search_normalizes_arguments(monkeypatch):
    captured = {}

    class FakePool:
        async def get_client(self, **kwargs):
            return None

    async def fake_call(self, name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(tavily_mcp.TavilyMCPClient, "call_tool", fake_call)
    await tavily_search(FakePool(), query="q", max_results=99, search_depth="ADVANCED")
    # max_results 钳制到上限，search_depth 小写化
    assert captured["arguments"]["max_results"] == 20
    assert captured["arguments"]["search_depth"] == "advanced"

    await tavily_search(FakePool(), query="q", max_results=-3, search_depth="weird")
    assert captured["arguments"]["max_results"] == 1
    assert captured["arguments"]["search_depth"] == "basic"


# ── build_builtin_tools 注册 ──


def test_builtin_tools_register_tavily_search():
    app = FastAPI()
    tools = build_builtin_tools(app)
    names = [tool.name for tool in tools]
    assert "tavily_search" in names
    tool = next(tool for tool in tools if tool.name == "tavily_search")
    assert tool.parameters["required"] == ["query"]
    assert tool.parameters["properties"]["max_results"]["type"] == "integer"


@pytest.mark.asyncio
async def test_tavily_search_tool_handler(monkeypatch):
    """工具 handler 通过连接池取 client 并委托 tavily_search。"""
    app = FastAPI()
    captured = {}

    class FakePool:
        is_route_pool = True

        async def get_client(self, **kwargs):
            captured["pool_kwargs"] = kwargs
            return "fake-client"

    async def fake_tavily_search(client, **kwargs):
        captured["client"] = client
        captured["kwargs"] = kwargs
        return "search-result"

    app.state.http_client = FakePool()
    monkeypatch.setattr("akm.agent_runtime.tools.tavily_search", fake_tavily_search)

    tools = build_builtin_tools(app)
    tool: ToolDef = next(tool for tool in tools if tool.name == "tavily_search")
    text = await tool.handler(query="python async", max_results=3)

    assert text == "search-result"
    assert captured["client"] == "fake-client"
    assert captured["pool_kwargs"] == {
        "provider": "tavily",
        "key_alias": "mcp",
        "model": "",
        "api_path": "mcp",
    }
    assert captured["kwargs"]["query"] == "python async"


@pytest.mark.asyncio
async def test_tavily_search_tool_handler_without_pool(monkeypatch):
    """连接池缺失时返回结构化错误而不是抛出异常。"""
    app = FastAPI()
    app.state.http_client = None
    tools = build_builtin_tools(app)
    tool = next(tool for tool in tools if tool.name == "tavily_search")
    text = await tool.handler(query="q")
    assert "连接池未就绪" in text
