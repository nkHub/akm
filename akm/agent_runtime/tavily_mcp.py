"""Tavily MCP 远程端点的轻量客户端，为 Agent 提供联网搜索能力。

Tavily 官方远程端点基于 MCP Streamable HTTP 传输：客户端以
JSON-RPC 2.0 发送请求，服务器可能以 `application/json` 或
`text/event-stream` 返回响应。本模块只封装 Tavily 所需的
initialize 握手与 tools/call 两步，不引入完整的 MCP 客户端库。
"""

import json
import logging
from typing import Any

from akm.config import load_config

logger = logging.getLogger(__name__)

# MCP 规范当前协议版本（Streamable HTTP）
MCP_PROTOCOL_VERSION = "2025-06-18"
# Tavily 远程 MCP 端点：API Key 通过查询参数注入
TAVILY_MCP_URL_TEMPLATE = "https://mcp.tavily.com/mcp/?tavilyApiKey={key}"
# Tavily 搜索工具的最大结果数上限
TAVILY_MAX_RESULTS = 20


class TavilyMCPError(RuntimeError):
    """Tavily MCP 调用错误。"""


class TavilyMCPClient:
    """轻量 MCP Streamable HTTP 客户端，仅面向 Tavily 远程端点。

    每次调用都会重新执行 initialize 握手并携带服务端下发的
    Mcp-Session-Id（如有），避免长期缓存会话导致 API Key 变更后失效。
    """

    def __init__(self, http_client):
        self._http = http_client
        self._url = ""
        self._session_id = ""

    @classmethod
    def _endpoint_url(cls) -> str:
        """根据当前配置构造 Tavily MCP 端点 URL。"""
        key = str(load_config().get("tavily_api_key", "") or "").strip()
        if not key:
            raise TavilyMCPError("未配置 tavily_api_key，无法使用联网搜索")
        return TAVILY_MCP_URL_TEMPLATE.format(key=key)

    async def _post(self, payload: dict, expect_response: bool = True) -> dict | None:
        """发送一次 JSON-RPC 请求，兼容 JSON 与 SSE 两种响应格式。

        Args:
            payload: JSON-RPC 请求体
            expect_response: 是否等待并解析响应（通知类请求传 False）

        Returns:
            响应 JSON；通知类请求返回 None
        """
        headers = {"Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        resp = await self._http.post(self._url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise TavilyMCPError(f"MCP 请求失败 HTTP {resp.status_code}: {resp.text[:200]}")
        # 服务器可能通过 Mcp-Session-Id 响应头下发会话标识
        self._session_id = resp.headers.get("mcp-session-id") or self._session_id
        if not expect_response:
            return None

        content_type = resp.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            # SSE 帧中的 data 行即 JSON-RPC 响应
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if raw and raw != "[DONE]":
                        return json.loads(raw)
            raise TavilyMCPError("MCP 流式响应为空")
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise TavilyMCPError(f"MCP 响应不是合法 JSON: {resp.text[:200]}") from exc
        return data

    async def _initialize(self) -> None:
        """执行 MCP 初始化握手，并记录服务器协议版本。"""
        payload = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "akm", "version": "1.0"},
            },
        }
        data = await self._post(payload)
        if data is None:
            raise TavilyMCPError("initialize 无响应")
        if "error" in data:
            raise TavilyMCPError(f"initialize 失败: {data['error']}")
        result = data.get("result") or {}
        server_version = result.get("protocolVersion") or ""
        if server_version and server_version != MCP_PROTOCOL_VERSION:
            logger.warning(
                "[TavilyMCP] 服务器协议版本 %s 与客户端 %s 不同",
                server_version,
                MCP_PROTOCOL_VERSION,
            )

    async def _notify_initialized(self) -> None:
        """发送 initialized 通知，告知服务器客户端已完成握手。"""
        await self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_response=False,
        )

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """执行 MCP 工具调用，返回工具结果 dict。

        Args:
            name: MCP 工具名（如 tavily_search）
            arguments: 工具参数

        Returns:
            工具结果中的 result 字段
        """
        self._url = self._endpoint_url()
        await self._initialize()
        await self._notify_initialized()
        data = await self._post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if data is None:
            raise TavilyMCPError("tools/call 无响应")
        if "error" in data:
            raise TavilyMCPError(f"tools/call 失败: {data['error']}")
        return data.get("result") or {}


def _extract_text(result: dict) -> str:
    """从 MCP 工具结果中提取全部 TextContent 文本。"""
    parts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = str(item.get("text") or "")
            if text:
                parts.append(text)
    return "\n".join(parts)


async def tavily_search(
    http_client: Any,
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
) -> str:
    """执行一次 Tavily 联网搜索，返回文本结果。

    Args:
        http_client: 可发起 POST 的 httpx.AsyncClient
        query: 搜索关键词
        max_results: 返回结果数量（1-20，默认 5）
        search_depth: 搜索深度，basic 或 advanced

    Returns:
        序列化后的搜索结果文本
    """
    max_results = max(1, min(int(max_results or 5), TAVILY_MAX_RESULTS))
    search_depth = str(search_depth or "basic").lower()
    if search_depth not in ("basic", "advanced"):
        search_depth = "basic"

    client = TavilyMCPClient(http_client)
    result = await client.call_tool("tavily_search", {
        "query": str(query or ""),
        "max_results": max_results,
        "search_depth": search_depth,
    })
    return _extract_text(result) or json.dumps(result, ensure_ascii=False)
