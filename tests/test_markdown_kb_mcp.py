"""akm.markdown_kb_mcp 的 MCP streamable HTTP 端点测试。"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from akm import markdown_kb_mcp
from akm.markdown_kb_mcp import router as mcp_router
from akm.server import app


@pytest.fixture(autouse=True)
def _fake_call_kb(monkeypatch):
    """默认拦截 _call_kb，避免测试真的请求本地插件端口。"""

    class _Recorder:
        calls = []

    rec = _Recorder()
    monkeypatch.setattr(markdown_kb_mcp, "_call_kb", None)  # 占位防意外
    return rec


async def _rpc(client, method, params=None, msg_id=1, accept="application/json"):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    resp = await client.post(
        "/api/markdown-kb/mcp",
        json=body,
        headers={"Accept": accept},
    )
    return resp


@pytest.mark.asyncio
async def test_mcp_initialize_handshake(monkeypatch):
    """initialize 握手应声明协议版本、能力与工具 serverInfo。"""
    captured = {}

    async def fake_call(endpoint, payload, timeout=120.0):
        captured["endpoint"] = endpoint
        return {"ok": True}

    monkeypatch.setattr(markdown_kb_mcp, "_call_kb", fake_call)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _rpc(client, "initialize")
    assert resp.status_code == 200
    data = resp.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 1
    result = data["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"]["name"] == "akm-markdown-kb"


@pytest.mark.asyncio
async def test_mcp_tools_list(monkeypatch):
    """tools/list 应返回 search_kb 与 ask_kb 两个工具及 Schema。"""

    async def fake_call(endpoint, payload, timeout=120.0):
        return {"ok": True}

    monkeypatch.setattr(markdown_kb_mcp, "_call_kb", fake_call)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _rpc(client, "tools/list")
    assert resp.status_code == 200
    tools = resp.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"search_kb", "ask_kb"}
    search = next(t for t in tools if t["name"] == "search_kb")
    assert search["inputSchema"]["required"] == ["question"]
    assert "top_k" in search["inputSchema"]["properties"]


@pytest.mark.asyncio
async def test_mcp_tools_call_search_kb(monkeypatch):
    """search_kb 应组装 payload 调 query 并返回精简命中文本。"""
    captured = {}

    async def fake_call(endpoint, payload, timeout=120.0):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {
            "ok": True,
            "hits": [
                {
                    "title": "安装指南",
                    "file_name": "install.md",
                    "score": 0.82,
                    "chunk_text": "首先执行安装步骤…" * 30,
                }
            ],
        }

    monkeypatch.setattr(markdown_kb_mcp, "_call_kb", fake_call)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _rpc(
            client,
            "tools/call",
            {"name": "search_kb", "arguments": {"question": "如何安装", "top_k": 3}},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["content"][0]["type"] == "text"
    text = data["result"]["content"][0]["text"]
    assert "安装指南" in text and "install.md" in text and "相关度" in text
    assert captured["endpoint"] == "query"
    assert captured["payload"]["question"] == "如何安装"
    assert captured["payload"]["top_k"] == 3
    # chunk_text 已截断到 800 字
    assert len(text) < 2000


@pytest.mark.asyncio
async def test_mcp_tools_call_ask_kb(monkeypatch):
    """ask_kb 应调 ask 端点并附引用来源。"""
    captured = {}

    async def fake_call(endpoint, payload, timeout=120.0):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {
            "ok": True,
            "answer": "可以直接用 `akm_search_kb`。",
            "citations": [{"title": "FAQ", "file_name": "faq.md"}],
        }

    monkeypatch.setattr(markdown_kb_mcp, "_call_kb", fake_call)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _rpc(
            client,
            "tools/call",
            {"name": "ask_kb", "arguments": {"question": "怎么检索"}},
        )
    assert resp.status_code == 200
    text = resp.json()["result"]["content"][0]["text"]
    assert "akm_search_kb" in text
    assert "引用来源" in text and "FAQ" in text
    assert captured["endpoint"] == "ask"


@pytest.mark.asyncio
async def test_mcp_tools_call_error_paths(monkeypatch):
    """question 为空、未知工具、上游失败均应返回 MCP 错误而非崩溃。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # question 为空
        resp = await _rpc(
            client, "tools/call", {"name": "search_kb", "arguments": {"question": "  "}}
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32603

        # 未知工具
        resp = await _rpc(client, "tools/call", {"name": "nope", "arguments": {}})
        assert resp.json()["error"]["code"] == -32601

        # 未知方法
        resp = await _rpc(client, "bogus_method")
        assert resp.json()["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_mcp_tools_call_plugin_unavailable(monkeypatch):
    """插件未启用（query 返回非 ok 或 404）时应转为明确错误信息。"""
    import httpx

    async def fake_call(endpoint, payload, timeout=120.0):
        req = httpx.Request("POST", "http://test/api/markdown-kb/query")
        resp = httpx.Response(404, request=req)
        raise httpx.HTTPStatusError("404 Not Found", request=req, response=resp)

    monkeypatch.setattr(markdown_kb_mcp, "_call_kb", fake_call)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _rpc(
            client, "tools/call", {"name": "search_kb", "arguments": {"question": "hi"}}
        )
    assert resp.status_code == 200
    err = resp.json()["error"]
    assert err["code"] == -32603
    assert "调用失败" in err["message"]


@pytest.mark.asyncio
async def test_mcp_notification_returns_202(monkeypatch):
    """notifications/initialized 通知应返回 202 空响应。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/markdown-kb/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
    assert resp.status_code == 202
    assert resp.text == ""


@pytest.mark.asyncio
async def test_mcp_sse_response_format(monkeypatch):
    """Accept: text/event-stream 时应返回 SSE 帧（event: message + data）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _rpc(client, "ping", accept="text/event-stream")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert body.startswith("event: message\ndata: ")
    assert '"result": {}' in body


@pytest.mark.asyncio
async def test_mcp_invalid_json_returns_400():
    """非法 JSON 请求应返回 400 与 -32700 错误。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/markdown-kb/mcp",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_mcp_options_preflight():
    """OPTIONS 预检应返回 204 与 CORS 头。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.options("/api/markdown-kb/mcp")
    assert resp.status_code == 204
    assert resp.headers.get("access-control-allow-methods") == "POST, OPTIONS"
