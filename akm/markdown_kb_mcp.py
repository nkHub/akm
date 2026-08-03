"""markdown-kb 的 MCP HTTP 端点 — 把本地知识库查询/问答暴露为 MCP 工具。

实现 MCP streamable HTTP 协议（protocolVersion 2025-06-18）的最小可用于集，
供支持 "type": "http" 配置的 MCP 客户端（如 Cursor、Claude Desktop）连接：

    {
      "type": "http",
      "url": "http://127.0.0.1:8800/api/markdown-kb/mcp"
    }

端点提供两个工具：
- search_kb：POST /api/markdown-kb/query 的语义检索；
- ask_kb：POST /api/markdown-kb/ask 的基于知识库问答。

实现采用无状态会话（每次请求独立处理，不依赖 Mcp-Session-Id），MCP 客户端
在 protocolVersion 2025-06-18 下可正常使用。工具内部通过 AKM 自身的 HTTP
接口调用插件，因此不依赖插件实例是否被加载；插件未启用时接口返回 404，
工具会转成明确的错误信息。

刻意不引入 mcp 依赖库（避免打包后依赖解析不稳定），协议层全部手写。
"""

import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from akm.config import load_config

logger = logging.getLogger(__name__)

# MCP 协议版本：streamable HTTP + 无状态会话
MCP_PROTOCOL_VERSION = "2025-06-18"

# 语义检索工具参数 Schema（OpenAI function calling 风格）
_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "查询问题"},
        "top_k": {
            "type": "integer",
            "description": "返回的命中条数（1-20，默认 5）",
            "minimum": 1,
            "maximum": 20,
        },
        "embedding_model": {"type": "string", "description": "向量化模型（可选，默认用插件配置）"},
        "reranker_model": {"type": "string", "description": "重排模型（可选，默认用插件配置）"},
    },
    "required": ["question"],
}

# 基于知识库问答工具参数 Schema
_ASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "问题"},
        "chat_model": {"type": "string", "description": "问答模型（可选，默认用插件配置）"},
    },
    "required": ["question"],
}

router = APIRouter()


def _base_url() -> str:
    """根据配置的 server_port 构造本服务基础地址（与 markdown_kb_hook 约定一致）。"""
    port = int(load_config().get("server_port", 8800) or 8800)
    return f"http://127.0.0.1:{port}"


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """构造 JSON-RPC 成功响应。"""
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    """构造 JSON-RPC 错误响应。"""
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def _call_kb(endpoint: str, payload: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    """通过 AKM 自身的 HTTP 接口调用 markdown-kb 插件的 query / ask。

    httpx 在此处延迟导入，避免与 FastAPI 的启动顺序产生依赖负担。
    """
    import httpx

    url = f"{_base_url()}/api/markdown-kb/{endpoint}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


def _format_hits(data: dict[str, Any]) -> str:
    """把 /query 返回的 hits 精简为便于 LLM/用户阅读的文本（截断正文）。"""
    hits = data.get("hits") or []
    if not hits:
        return "未检索到相关内容。"
    lines = []
    for i, hit in enumerate(hits, 1):
        title = str(hit.get("title") or hit.get("file_name") or "")
        file_name = str(hit.get("file_name") or "")
        score = float(hit.get("score") or hit.get("vector_score") or 0)
        text = str(hit.get("chunk_text") or "")[:800]
        lines.append(f"[{i}] {title}（{file_name}，相关度 {score:.3f}）\n{text}")
    return "\n\n".join(lines)


async def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """按 JSON-RPC method 分发；通知类请求返回 None（HTTP 202）。"""
    method = message.get("method")
    msg_id = message.get("id")

    if method == "initialize":
        # 握手：声明协议版本、能力与服务器信息
        return _result(
            msg_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "akm-markdown-kb", "version": "1.0.0"},
                "instructions": "使用 search_kb 检索本地 Markdown 知识库，ask_kb 进行基于知识库的问答。",
            },
        )
    if method == "ping":
        return _result(msg_id, {})
    if method == "notifications/initialized":
        # 通知类消息无 id，不返回响应体
        return None
    if method == "tools/list":
        return _result(
            msg_id,
            {
                "tools": [
                    {
                        "name": "search_kb",
                        "description": "检索本地 Markdown 知识库，返回相关文档片段（含标题、文件名与相关度）",
                        "inputSchema": _SEARCH_SCHEMA,
                    },
                    {
                        "name": "ask_kb",
                        "description": "基于本地 Markdown 知识库进行问答，返回回答与引用来源",
                        "inputSchema": _ASK_SCHEMA,
                    },
                ]
            },
        )
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "search_kb":
                question = str(args.get("question") or "").strip()
                if not question:
                    raise ValueError("question 不能为空")
                payload: dict[str, Any] = {"question": question}
                try:
                    payload["top_k"] = max(1, min(20, int(args.get("top_k") or 5)))
                except (TypeError, ValueError):
                    payload["top_k"] = 5
                if args.get("embedding_model"):
                    payload["embedding_model"] = str(args["embedding_model"])
                if args.get("reranker_model"):
                    payload["reranker_model"] = str(args["reranker_model"])
                data = await _call_kb("query", payload)
                if not data.get("ok"):
                    raise RuntimeError(str(data.get("error") or "知识库查询失败"))
                text = _format_hits(data)
            elif name == "ask_kb":
                question = str(args.get("question") or "").strip()
                if not question:
                    raise ValueError("question 不能为空")
                payload = {"question": question}
                if args.get("chat_model"):
                    payload["chat_model"] = str(args["chat_model"])
                data = await _call_kb("ask", payload, timeout=180.0)
                if not data.get("ok"):
                    raise RuntimeError(str(data.get("error") or "知识库问答失败"))
                text = str(data.get("answer") or "（无回答）")
                citations = data.get("citations") or []
                if citations:
                    refs = "\n".join(
                        f"- {c.get('title') or c.get('file_name') or ''}" for c in citations
                    )
                    text += f"\n\n引用来源：\n{refs}"
            else:
                return _error(msg_id, -32601, f"未知工具: {name}")
            return _result(msg_id, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:  # noqa: BLE001 —— 工具执行失败统一转为 MCP 错误
            logger.warning("markdown-kb MCP tools/call 失败: %s", exc)
            return _error(msg_id, -32603, f"调用失败: {type(exc).__name__}: {exc}")
    return _error(msg_id, -32601, f"未知方法: {method}")


def _sse_response(payload: dict[str, Any]) -> StreamingResponse:
    """把 JSON-RPC 响应包成 MCP streamable HTTP 的 SSE 帧。"""

    async def _gen() -> AsyncIterator[bytes]:
        body = json.dumps(payload, ensure_ascii=False)
        yield f"event: message\ndata: {body}\n\n".encode("utf-8")

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.options("")
async def _mcp_options() -> Response:
    """预检请求：允许跨域（浏览器端 MCP 客户端需要）。"""
    return Response(
        status_code=204,
        headers={
            "Allow": "POST, OPTIONS",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Accept, Mcp-Session-Id, Authorization",
        },
    )


@router.post("")
async def mcp_endpoint(request: Request) -> Response:
    """MCP streamable HTTP 主入口：处理 JSON-RPC 请求。"""
    try:
        raw = await request.body()
        message = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(_error(None, -32700, "解析错误：无效的 JSON"), status_code=400)
    if not isinstance(message, dict):
        return JSONResponse(_error(None, -32600, "无效的请求：应为 JSON 对象"), status_code=400)

    result = await _handle(message)
    if result is None:
        # 通知类请求：返回 202 Accepted 空响应
        return Response(status_code=202)

    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        return _sse_response(result)
    return JSONResponse(result)
