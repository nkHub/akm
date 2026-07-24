"""budget_gate / mcp_tool_gateway 聚焦回归测试。"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akm.plugins.context import RequestContext
from plugins.budget_gate.index import Plugin as BudgetGate
from plugins.mcp_tool_gateway.index import Plugin as McpToolGateway


def _ctx(request: dict | None = None, **kwargs) -> RequestContext:
    """构造单次请求级上下文。"""
    return RequestContext(request if isinstance(request, dict) else {}, **kwargs)


@pytest.mark.asyncio
async def test_budget_gate_blocks_after_spent_reaches_budget():
    """累计估算费用达到预算后，后续请求应被阻断。"""
    plugin = BudgetGate()
    plugin.logger = logging.getLogger("test.budget_gate")
    plugin.config = {
        "enabled": True,
        "scope": "global",
        "period": "calendar_day",
        "budget_usd": 0.01,
        "use_core_pricing": False,
        # 1M prompt = $1，1M completion = $0 → 10000 prompt ≈ $0.01
        "custom_pricing_table": "gpt-4=1/0/0\n*=1/0/0",
        "soft_warn_ratio": 0,
        "block_status_code": 429,
    }
    await plugin.on_load()

    req = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    ctx1 = _ctx(req)
    await plugin.on_request(ctx1)
    assert not ctx1.is_block

    ctx1.response = {
        "ok": True,
        "model": "gpt-4",
        "response_body": json.dumps(
            {"usage": {"prompt_tokens": 10000, "completion_tokens": 0, "total_tokens": 10000}}
        ),
    }
    await plugin.on_response(ctx1)

    status = plugin.status()
    assert status["buckets"]
    assert status["buckets"][0]["exhausted"] is True

    ctx2 = _ctx(req)
    await plugin.on_request(ctx2)
    assert ctx2.is_block
    action = ctx2.action or {}
    assert action.get("security_action") == "budget_exceeded"
    assert action.get("status_code") == 429
    assert "budget_gate" in str(action.get("body") or "")


@pytest.mark.asyncio
async def test_budget_gate_scope_model_independent_buckets():
    """模型维度下，不同 model 应有独立预算桶。"""
    plugin = BudgetGate()
    plugin.logger = logging.getLogger("test.budget_gate")
    plugin.config = {
        "enabled": True,
        "scope": "model",
        "period": "rolling_window",
        "window_seconds": 3600,
        "budget_usd": 0.001,
        "use_core_pricing": False,
        "custom_pricing_table": "*=1/0/0",
        "soft_warn_ratio": 0,
    }
    await plugin.on_load()

    async def spend(model: str, prompt_tokens: int):
        ctx = _ctx({"model": model})
        await plugin.on_request(ctx)
        ctx.response = {
            "ok": True,
            "model": model,
            "response_body": json.dumps(
                {
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": 0,
                        "total_tokens": prompt_tokens,
                    }
                }
            ),
        }
        await plugin.on_response(ctx)
        return ctx

    await spend("gpt-a", 1000)  # $0.001
    blocked = _ctx({"model": "gpt-a"})
    await plugin.on_request(blocked)
    assert blocked.is_block

    other = _ctx({"model": "gpt-b"})
    await plugin.on_request(other)
    assert not other.is_block


@pytest.mark.asyncio
async def test_budget_gate_reset_clears_buckets():
    plugin = BudgetGate()
    plugin.logger = logging.getLogger("test.budget_gate")
    plugin.config = {
        "enabled": True,
        "budget_usd": 0.001,
        "use_core_pricing": False,
        "custom_pricing_table": "*=1/0/0",
        "soft_warn_ratio": 0,
    }
    await plugin.on_load()
    ctx = _ctx({"model": "m"})
    await plugin.on_request(ctx)
    ctx.response = {
        "ok": True,
        "model": "m",
        "response_body": '{"usage":{"prompt_tokens":1000,"completion_tokens":0,"total_tokens":1000}}',
    }
    await plugin.on_response(ctx)
    assert plugin.reset()["cleared_buckets"] >= 1
    again = _ctx({"model": "m"})
    await plugin.on_request(again)
    assert not again.is_block


@pytest.mark.asyncio
async def test_mcp_tool_gateway_inject_and_strip():
    plugin = McpToolGateway()
    plugin.logger = logging.getLogger("test.mcp_tool_gateway")
    plugin.config = {
        "enabled": True,
        "inject_tools": True,
        "strip_unlisted_tools": True,
        "tools_json": json.dumps(
            [
                {
                    "name": "local_echo",
                    "description": "echo",
                    "url": "http://127.0.0.1:9999/echo",
                    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
                }
            ]
        ),
        "allowed_url_hosts": "127.0.0.1,localhost",
    }
    await plugin.on_load()

    req = {
        "model": "gpt-4",
        "tools": [
            {"type": "function", "function": {"name": "bash", "parameters": {}}},
            {"type": "function", "function": {"name": "local_echo", "parameters": {}}},
        ],
    }
    ctx = _ctx(req)
    await plugin.on_request(ctx)
    names = []
    for tool in req.get("tools") or []:
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        names.append(fn.get("name"))
    assert "bash" not in names
    assert "local_echo" in names
    assert ctx.bag_get("mcp_tool_gateway.stripped")
    # 已有 local_echo 时不应重复注入
    assert "local_echo" in names


@pytest.mark.asyncio
async def test_mcp_tool_gateway_rejects_bad_host_and_calls_http():
    plugin = McpToolGateway()
    plugin.logger = logging.getLogger("test.mcp_tool_gateway")
    plugin.config = {
        "enabled": True,
        "inject_tools": False,
        "tools_json": json.dumps(
            [
                {
                    "name": "evil",
                    "url": "http://evil.example/x",
                },
                {
                    "name": "ok_tool",
                    "url": "http://127.0.0.1:9/tool",
                    "method": "POST",
                },
            ]
        ),
        "allowed_url_hosts": "127.0.0.1",
        "allow_call_api": True,
        "max_argument_bytes": 1024,
        "default_timeout_seconds": 5,
    }
    await plugin.on_load()

    public = plugin.list_tools_public()
    assert public["count"] == 1
    assert public["tools"][0]["name"] == "ok_tool"

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as missing:
        await plugin.call_tool({"name": "evil", "arguments": {}})
    assert missing.value.status_code == 404

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.text = '{"echo":1}'
    mock_resp.json = MagicMock(return_value={"echo": 1})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("plugins.mcp_tool_gateway.index.httpx.AsyncClient", return_value=mock_client):
        result = await plugin.call_tool({"name": "ok_tool", "arguments": {"a": 1}})
    assert result["ok"] is True
    assert result["result"] == {"echo": 1}
    mock_client.request.assert_awaited()
