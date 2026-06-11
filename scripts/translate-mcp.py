# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp[cli]",
#     "translators>=5.9.0",
#     "langdetect>=1.0.9",
# ]
# ///
"""
基于 translators 库的本地翻译 MCP Server
使用 translators 库实现免费翻译，无需 API Key，支持多引擎自动切换
"""
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
import translators as ts
from langdetect import detect_langs, DetectorFactory


# 确保语言检测结果可复现
DetectorFactory.seed = 0

server = Server("translators-mcp")


# translators 库语言代码兼容映射（将 googletrans 风格映射为 translators 风格）
_LANG_MAP = {
    "zh-cn": "zh",
    "zh-tw": "zh-TW",
    "zh-hk": "zh-TW",
}


def _normalize_lang(code: str) -> str:
    """标准化语言代码，兼容 googletrans 风格"""
    return _LANG_MAP.get(code.lower(), code)


@server.list_tools()
async def list_tools() -> list[Tool]:
    """列出所有可用的翻译工具"""
    return [
        Tool(
            name="translate",
            description="将文本翻译为目标语言。使用 translators 库（多引擎支持），支持 100+ 种语言。",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "需要翻译的文本内容",
                    },
                    "dest": {
                        "type": "string",
                        "description": "目标语言代码，例如：zh-cn(简体中文)、zh-tw(繁体中文)、en(英语)、ja(日语)、ko(韩语)、fr(法语)、de(德语)、es(西班牙语) 等",
                        "default": "zh-cn",
                    },
                    "src": {
                        "type": "string",
                        "description": "源语言代码，留空则自动检测源语言",
                        "default": "auto",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="detect_language",
            description="检测文本的语言",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "需要检测语言的文本",
                    },
                },
                "required": ["text"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """处理工具调用"""
    if name == "translate":
        text = arguments.get("text", "")
        dest = arguments.get("dest", "zh-cn")
        src = arguments.get("src", "auto")

        try:
            # translators 库是同步的，用线程池避免阻塞事件循环
            from_lang = _normalize_lang(src) if src and src != "auto" else "auto"
            to_lang = _normalize_lang(dest)

            # 多引擎 fallback：优先用国内可直连的引擎，避免 VPN 依赖
            engines = ["bing", "google", "baidu", "alibaba"]
            result_text = None
            last_error = None
            for engine in engines:
                try:
                    result_text = await asyncio.to_thread(
                        ts.translate_text,
                        query_text=text,
                        from_language=from_lang,
                        to_language=to_lang,
                        translator=engine,
                    )
                    break  # 成功后退出
                except Exception as eng_err:
                    last_error = eng_err
            if result_text is None:
                raise last_error or RuntimeError("所有翻译引擎均失败")
            output = f"原文: {text}\n译文: {result_text}\n"

            # 如果源语言是自动检测，尝试用 langdetect 检测实际语言
            if src == "auto" or not src:
                try:
                    det_results = await asyncio.to_thread(detect_langs, text)
                    if det_results:
                        detected = det_results[0]
                        output += f"检测源语言: {detected.lang} (置信度: {detected.prob:.2%})"
                except Exception:
                    pass  # 语言检测失败不影响翻译结果

            return [TextContent(type="text", text=output)]
        except Exception as e:
            return [
                TextContent(
                    type="text",
                    text=f"翻译失败: {type(e).__name__}: {str(e)}",
                )
            ]

    elif name == "detect_language":
        text = arguments.get("text", "")

        try:
            det_results = await asyncio.to_thread(detect_langs, text)
            if det_results:
                best = det_results[0]
                output = f"检测语言: {best.lang}\n置信度: {best.prob:.2%}"
                # 如果有多个候选也显示
                if len(det_results) > 1:
                    others = ", ".join(
                        f"{r.lang}({r.prob:.2%})" for r in det_results[1:4]
                    )
                    output += f"\n其他候选: {others}"
            else:
                output = "语言检测: 无法确定"
            return [TextContent(type="text", text=output)]
        except Exception as e:
            return [
                TextContent(
                    type="text",
                    text=f"检测失败: {type(e).__name__}: {str(e)}",
                )
            ]

    else:
        return [TextContent(type="text", text=f"未知工具: {name}")]


async def main():
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
