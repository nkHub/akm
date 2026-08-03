# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp[cli]",
#     "httpx",
# ]
# ///
"""本地 AKM markdown-kb 查询 MCP Server

通过 HTTP 调用本机 AKM 服务的 /api/markdown-kb/query 与 /api/markdown-kb/ask
接口，把 markdown-kb 知识库检索能力暴露成 MCP 工具，供 Claude Desktop、
Cursor、opencode 等支持 MCP 的客户端直接配置使用。
"""
import asyncio
import json
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


server = Server("akm-markdown-kb-mcp")


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


@server.list_tools()
async def list_tools() -> list[Tool]:
    """列出所有可用的 markdown-kb 查询工具"""
    return [
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
