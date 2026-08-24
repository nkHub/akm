# /// script
# requires-python = ">=3.10"
# dependencies = [
#     # mcp 2.0 把 Server 改为低层 API，移除了 list_tools / call_tool 装饰器；
#     # 本脚本按 1.x 的装饰器风格编写，pin <2 保证可运行。
#     "mcp[cli]<2",
#     "httpx",
# ]
# ///
"""本地 AKM markdown-kb MCP Server（stdio）

通过 HTTP 调用本机 AKM 服务的 /api/markdown-kb 接口，把 markdown-kb 知识库的
检索与维护能力暴露成 MCP 工具，供 Claude Desktop、Cursor、opencode 等支持 MCP
的客户端直接配置使用。

工具分两类：
- 查询：search_kb（检索相关文档片段）、ask_kb（基于知识库问答）；
- 维护：list_kb_files（列目录）/ read_kb_file（读全文）/ write_kb_file（按文本
  写入，写入后需 rebuild_kb_file 或 rebuild_kb 重建索引）/ delete_kb_file /
  bind_kb_workspace / rebuild_kb / rebuild_kb_file / clear_kb / sync_kb /
  kb_status / learn_kb / scan_kb_sessions，形成「读 → 改 → 重建」维护闭环。
"""
import asyncio
import json
import os
from urllib.parse import quote as _urlencode

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


server = Server("akm-markdown-kb-mcp")


# 文档维护类工具规格（与 akm/markdown_kb_mcp.py 内置版同构，数据驱动：
# tools/list 与 tools/call 共用）。method 为 GET 时无请求体；POST 时按
# inputSchema.properties 把非空参数构造为请求体；url_params 为需要内插进
# URL 路径的参数名（其余非 url_params 参数拼到 GET query string）。
_MAINT_SPECS = [
    {
        "name": "list_kb_files",
        "description": "列出当前知识库中的 Markdown 文件（仅元数据，不含正文）；传入 workspace_root 时仅返回绑定到该工作目录的文件",
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


def _akm_api_base_url() -> str:
    """读取 ~/.akm/config.json 的 server_port，返回本机 AKM 服务地址。

    独立脚本不依赖 AKM 环境变量，直接读取用户配置文件；读取失败时
    回退到默认端口 8800。
    """
    port = 8800
    try:
        cfg_path = os.path.expanduser("~/.akm/config.json")
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        port = int(cfg.get("server_port", 8800) or 8800)
    except (OSError, ValueError, TypeError):
        pass
    return f"http://127.0.0.1:{port}"


def _format_hits(data: dict) -> str:
    """把 query 接口返回的 hits 格式化成适合展示的文本。"""
    hits = data.get("hits") or []
    if not hits:
        return "未检索到相关内容。"
    lines = [f"共 {len(hits)} 条命中：", ""]
    for index, hit in enumerate(hits, start=1):
        title = hit.get("title") or hit.get("file_name") or "未命名"
        file_name = hit.get("file_name") or ""
        score = hit.get("score") or hit.get("vector_score") or 0
        chunk = str(hit.get("chunk_text") or "").strip()
        lines.append(f"[{index}] {title}（{file_name}，相关度 {score}）")
        if chunk:
            lines.append(chunk[:800])
        lines.append("")
    return "\n".join(lines)


def _build_payload(spec: dict, args: dict) -> dict:
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
    payload = {}
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


async def _call_maintenance(name: str, args: dict) -> str:
    """执行一个文档维护类工具，返回展示文本（JSON 序列化）。"""
    spec = next((s for s in _MAINT_SPECS if s["name"] == name), None)
    if spec is None:
        return f"未知维护工具: {name}"
    base = _akm_api_base_url()
    method = str(spec.get("method") or "POST")
    payload = None
    if method != "GET":
        payload = _build_payload(spec, args)
    endpoint = str(spec["endpoint"])
    # 需要把参数内插进 URL 路径的工具（如 read_kb_file 的文件 {file_name}）：
    # 必要参数从 args 取值，可选参数（doc_id / workspace_root 等）拼进 GET query string。
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
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.request(method, f"{base}/api/markdown-kb/{endpoint}", json=payload if method != "GET" else None)
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict) and data.get("ok") is False:
        return f"{name} 执行失败: {data.get('error') or data.get('message') or '未知错误'}"
    return json.dumps(data, ensure_ascii=False)


@server.list_tools()
async def list_tools() -> list[Tool]:
    """列出所有可用的 markdown-kb 工具（查询 + 维护）"""
    tools = [
        Tool(
            name="search_kb",
            description="在 AKM markdown-kb 知识库中检索与问题最相关的文档片段，返回命中内容的标题、文件名、相关度分数与正文。需要 markdown-kb 插件已启用且已学习文档。",
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "检索问题",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回命中条数，默认 5",
                        "default": 5,
                    },
                    "embedding_model": {
                        "type": "string",
                        "description": "向量模型，留空使用插件配置",
                        "default": "",
                    },
                    "reranker_model": {
                        "type": "string",
                        "description": "重排模型，留空使用插件配置",
                        "default": "",
                    },
                },
                "required": ["question"],
            },
        ),
        Tool(
            name="ask_kb",
            description="基于 markdown-kb 知识库回答问题，返回答案与引用来源。需要 markdown-kb 插件已启用、已学习文档且配置了可用的 chat_model。",
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "问题",
                    },
                    "chat_model": {
                        "type": "string",
                        "description": "生成回答使用的模型，留空使用插件配置",
                        "default": "",
                    },
                },
                "required": ["question"],
            },
        ),
    ]
    tools.extend(
        Tool(
            name=spec["name"],
            description=spec["description"],
            inputSchema=spec["inputSchema"],
        )
        for spec in _MAINT_SPECS
    )
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """处理工具调用，转发到本机 AKM 的 markdown-kb 接口"""
    base = _akm_api_base_url()
    try:
        if name == "search_kb":
            payload = {"question": arguments.get("question", "")}
            top_k = arguments.get("top_k", 5)
            payload["top_k"] = max(1, min(int(top_k or 5), 20))
            if arguments.get("embedding_model"):
                payload["embedding_model"] = arguments["embedding_model"]
            if arguments.get("reranker_model"):
                payload["reranker_model"] = arguments["reranker_model"]
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(f"{base}/api/markdown-kb/query", json=payload)
                resp.raise_for_status()
                data = resp.json()
            if not data.get("ok"):
                return [TextContent(type="text", text=f"查询失败: {data.get('error') or data.get('message') or '未知错误'}")]
            return [TextContent(type="text", text=_format_hits(data))]

        elif name == "ask_kb":
            payload = {"question": arguments.get("question", "")}
            if arguments.get("chat_model"):
                payload["chat_model"] = arguments["chat_model"]
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await client.post(f"{base}/api/markdown-kb/ask", json=payload)
                resp.raise_for_status()
                data = resp.json()
            if not data.get("ok"):
                return [TextContent(type="text", text=f"提问失败: {data.get('error') or data.get('message') or '未知错误'}")]
            answer = str(data.get("answer") or "未返回答案。")
            citations = data.get("citations") or []
            if citations:
                refs = "\n".join(
                    f"- {c.get('title') or c.get('file_name') or ''}"
                    for c in citations
                )
                answer += f"\n\n引用来源：\n{refs}"
            return [TextContent(type="text", text=answer)]

        elif any(spec["name"] == name for spec in _MAINT_SPECS):
            return [TextContent(type="text", text=await _call_maintenance(name, arguments))]

        else:
            return [TextContent(type="text", text=f"未知工具: {name}")]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=f"调用失败: {type(e).__name__}: {str(e)}",
            )
        ]


async def main():
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())