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

另有文档维护类工具（list_kb_files / read_kb_file / write_kb_file / delete_kb_file /
rebuild_kb / rebuild_kb_file / bind_kb_workspace / sync_kb / clear_kb / learn_kb /
scan_kb_sessions / kb_status），其中 read_kb_file 读取单个知识条目的全文，
write_kb_file 按文本写入/覆盖文档（写入后需重建索引）。

实现采用无状态会话（每次请求独立处理，不依赖 Mcp-Session-Id），MCP 客户端
在 protocolVersion 2025-06-18 下可正常使用。工具内部通过 AKM 自身的 HTTP
接口调用插件，因此不依赖插件实例是否被加载；插件未启用时接口返回 404，
工具会转成明确的错误信息。

刻意不引入 mcp 依赖库（避免打包后依赖解析不稳定），协议层全部手写。
"""

import json
import logging
from typing import Any, AsyncIterator
from urllib.parse import quote as _urlencode

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
        "workspace_root": {"type": "string", "description": "工作目录绝对路径（可选；不传时检索全部文档）"},
    },
    "required": ["question"],
}

# 基于知识库问答工具参数 Schema
_ASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "问题"},
        "chat_model": {"type": "string", "description": "问答模型（可选，默认用插件配置）"},
        "workspace_root": {"type": "string", "description": "工作目录绝对路径（可选；不传时检索全部文档）"},
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


async def _call_kb(
    endpoint: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 120.0,
    method: str = "POST",
) -> Any:
    """通过 AKM 自身的 HTTP 接口调用 markdown-kb 插件的各种端点。

    method 支持 POST（默认，json body）与 GET（查询类端点，无 body）。
    httpx 在此处延迟导入，避免与 FastAPI 的启动顺序产生依赖负担。
    """
    import httpx

    url = f"{_base_url()}/api/markdown-kb/{endpoint}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        if method == "GET":
            resp = await client.get(url)
        else:
            resp = await client.post(url, json=payload or {})
        resp.raise_for_status()
        return resp.json()


# 文档维护类工具规格（数据驱动：tools/list 与 tools/call 共用）
# 每个规格：name/description/inputSchema/endpoint/method/required。
# method 为 GET 时无请求体；POST 时按 inputSchema.properties 把非空参数构造为请求体。
_MAINT_SPECS: list[dict[str, Any]] = [
    {
        "name": "list_kb_files",
        "description": "列出当前知识库中的 Markdown 文件；传入 workspace_root 时仅返回绑定到该工作目录的文件",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_root": {"type": "string", "description": "工作目录绝对路径（可选；传入时只返回绑定到该目录的文件）"},
            },
            "required": [],
        },
        "endpoint": "files",
        "method": "GET",
    },
    {
        "name": "kb_status",
        "description": "返回知识库插件状态（数据目录、文档目录、Markdown 文件数量、最近上传时间）",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "endpoint": "status",
        "method": "GET",
    },
    {
        "name": "bind_kb_workspace",
        "description": "为单个 Markdown 文件绑定工作目录",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "要绑定的文件名"},
                "workspace_root": {"type": "string", "description": "绑定的工作目录绝对路径"},
                "doc_id": {"type": "string", "description": "文档 ID（可选，按文档 ID 精确定位）"},
            },
            "required": ["file_name", "workspace_root"],
        },
        "endpoint": "files/bind-workspace",
    },
    {
        "name": "delete_kb_file",
        "description": "按 file_name（可选 doc_id / workspace_root 组合定位）删除 Markdown 文件并同步移除索引",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "要删除的文件名"},
                "workspace_root": {"type": "string", "description": "工作目录（与 file_name 组合定位文档）"},
                "doc_id": {"type": "string", "description": "文档 ID（优先按此删除）"},
            },
            "required": ["file_name"],
        },
        "endpoint": "files/delete",
    },
    {
        "name": "rebuild_kb",
        "description": "全量重建知识库索引（重新切片并向量化所有文档）",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "endpoint": "rebuild",
    },
    {
        "name": "rebuild_kb_file",
        "description": "只重建单个 Markdown 文件的索引",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "要重建的文件名"},
                "workspace_root": {"type": "string", "description": "工作目录（可选）"},
                "doc_id": {"type": "string", "description": "文档 ID（可选）"},
            },
            "required": ["file_name"],
        },
        "endpoint": "rebuild-file",
    },
    {
        "name": "clear_kb",
        "description": "清空索引，可选同时删除原始 Markdown 文档",
        "inputSchema": {
            "type": "object",
            "properties": {
                "delete_docs": {"type": "boolean", "description": "是否同时删除原始 Markdown 文档（默认 false）"},
            },
            "required": [],
        },
        "endpoint": "clear",
    },
    {
        "name": "sync_kb",
        "description": "按 docs 目录和索引状态做增量同步；apply 为 true 时真正写入，false 时仅预览差异",
        "inputSchema": {
            "type": "object",
            "properties": {
                "apply": {"type": "boolean", "description": "是否执行同步写入（默认 false，仅预览）"},
            },
            "required": [],
        },
        "endpoint": "sync",
    },
    {
        "name": "learn_kb",
        "description": "把一段对话/材料提炼为 Markdown 知识条目并写回知识库（幂等，重复 dedupe_key 不重复生成）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["codex", "claude_code"], "description": "知识来源"},
                "trigger_phase": {"type": "string", "enum": ["stop", "pre_compact"], "description": "触发阶段"},
                "session_id": {"type": "string", "description": "会话 ID（用于幂等去重）"},
                "dedupe_key": {"type": "string", "description": "去重键（相同内容重复调用会返回 deduped 结果）"},
                "workspace_root": {"type": "string", "description": "知识归属的工作目录（可选）"},
                "title_hint": {"type": "string", "description": "标题提示（可选）"},
                "user_prompt": {"type": "string", "description": "用户问题原文（可选）"},
                "assistant_excerpt": {"type": "string", "description": "回答摘录（可选）"},
                "conversation_excerpt": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "description": "消息角色，如 user / assistant"},
                            "text": {"type": "string", "description": "消息文本"},
                        },
                    },
                    "description": "对话片段数组 [{role, text}]（可选）",
                },
                "learn_keyword": {"type": "string", "description": "触发学习的关键词（可选）"},
                "turn_id": {"type": "string", "description": "轮次 ID（可选）"},
            },
            "required": ["source", "trigger_phase", "session_id", "dedupe_key"],
        },
        "endpoint": "learn",
    },
    {
        "name": "scan_kb_sessions",
        "description": "扫描客户端本地会话文件，生成知识并更新记忆",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_hours": {"type": "number", "description": "扫描多久以内的会话（默认 24）"},
                "max_sessions": {"type": "integer", "description": "最多处理多少会话（默认 5）"},
                "learn_enabled": {"type": "boolean", "description": "是否生成 learn 知识（默认 true）"},
                "memory_enabled": {"type": "boolean", "description": "是否做交叉验证 boost（默认 true）"},
            },
            "required": [],
        },
        "endpoint": "scan-sessions",
    },
    {
        "name": "read_kb_file",
        "description": "读取单个 Markdown 知识条目的全文内容（区别于 list_kb_files 只返回元数据）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "文件名（如 notes.md）"},
                "workspace_root": {"type": "string", "description": "工作目录（可选，同名文档需提供）"},
                "doc_id": {"type": "string", "description": "文档 ID（可选，比 file_name 更精确）"},
            },
            "required": ["file_name"],
        },
        "endpoint": "files/{file_name}",
        "method": "GET",
        "url_params": ["file_name"],
    },
    {
        "name": "write_kb_file",
        "description": "按文本内容写入（新建或覆盖）单个 Markdown 知识条目；写入后需调用 rebuild_kb_file 或 rebuild_kb 重建索引才会被检索到",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "目标文件名（如 notes.md，仅支持 .md）"},
                "content": {"type": "string", "description": "Markdown 全文内容"},
                "workspace_root": {"type": "string", "description": "工作目录（可选）"},
            },
            "required": ["file_name", "content"],
        },
        "endpoint": "files/write",
    },
]


def _build_payload(spec: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """按工具规格把调用参数构造成插件端点请求体。

    规则：必填字段缺省或为空字符串 → 抛 ValueError；其余按 properties 类型
    转换（string 去空、boolean/integer/number 数值化、array/object 原样透传），
    空值字段直接省略（交给插件端点的默认值）。
    """
    input_schema = spec.get("inputSchema") or {}
    for key in input_schema.get("required") or []:
        if not str(args.get(key) or "").strip():
            raise ValueError(f"{key} 不能为空")
    properties = input_schema.get("properties") or {}
    payload: dict[str, Any] = {}
    for key, prop in properties.items():
        value = args.get(key)
        if value is None:
            continue
        prop_type = prop.get("type")
        if prop_type == "string":
            text = str(value).strip()
            if text:
                payload[key] = text
        elif prop_type == "boolean":
            payload[key] = bool(value)
        elif prop_type == "integer":
            try:
                payload[key] = int(value)
            except (TypeError, ValueError):
                continue
        elif prop_type == "number":
            try:
                payload[key] = float(value)
            except (TypeError, ValueError):
                continue
        else:
            payload[key] = value
    return payload


async def _call_maintenance(name: str, args: dict[str, Any]) -> str:
    """执行一个文档维护类工具，返回展示文本（JSON 序列化）。"""
    spec = next((s for s in _MAINT_SPECS if s["name"] == name), None)
    if spec is None:
        raise ValueError(f"未知维护工具: {name}")
    method = str(spec.get("method") or "POST")
    payload: dict[str, Any] | None = None
    if method != "GET":
        payload = _build_payload(spec, args)
    endpoint = str(spec["endpoint"])
    # 需要把参数内插进 URL 路径的工具（如 read_kb_file 的文件 {file_name}）：必要参数
    # 从 args 取值，可选参数（doc_id / workspace_root 等）拼进 GET query string。
    url_params = [str(p) for p in (spec.get("url_params") or [])]
    if url_params:
        for key in url_params:
            value = str(args.get(key) or "").strip()
            if not value:
                raise ValueError(f"{key} 不能为空")
            endpoint = endpoint.replace("{" + key + "}", _urlencode(value))
    # GET 请求的过滤参数（如 list_kb_files 的 workspace_root）拼成 query string；
    # url_params 是路径内插参数，不重复进 query。
    query_parts = {
        str(k): str(v).strip()
        for k, v in args.items()
        if str(k) not in url_params and str(v or "").strip()
    }
    if method == "GET" and query_parts:
        endpoint += "?" + "&".join(f"{_urlencode(k)}={_urlencode(v)}" for k, v in query_parts.items())
    data = await _call_kb(endpoint, payload=payload, method=method)
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(str(data.get("error") or f"{name} 执行失败"))
    return json.dumps(data, ensure_ascii=False)


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
                + [
                    {
                        "name": spec["name"],
                        "description": spec["description"],
                        "inputSchema": spec["inputSchema"],
                    }
                    for spec in _MAINT_SPECS
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
                workspace_root = str(args.get("workspace_root") or "").strip()
                if workspace_root:
                    payload["workspace_root"] = workspace_root
                else:
                    # 未指定工作目录时默认检索全部文档，避免受工作域过滤限制
                    payload["ignore_workspace"] = True
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
                workspace_root = str(args.get("workspace_root") or "").strip()
                if workspace_root:
                    payload["workspace_root"] = workspace_root
                else:
                    # 未指定工作目录时默认检索全部文档，避免受工作域过滤限制
                    payload["ignore_workspace"] = True
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
            elif name in {spec["name"] for spec in _MAINT_SPECS}:
                text = await _call_maintenance(str(name), args)
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
