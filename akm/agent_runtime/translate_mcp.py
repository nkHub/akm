"""Agent 内置翻译工具 — 通过 uv 拉起全局翻译 MCP 脚本（MCP stdio 协议）。

翻译能力不在 AKM 内部实现，而是复用用户在 ~/.agents/plugins 配置的
translate-mcp.py（基于 mcp + translators 库的多引擎翻译 MCP server）。
AKM 作为 MCP client 用 ``uv run <script>`` 以子进程方式启动脚本，通过
stdio 换行分隔 JSON（NDJSON）的 JSON-RPC 协议调用其 ``translate`` /
``detect_language`` 工具。

脚本顶部自带 ``# /// script`` 依赖声明，``uv run`` 会自动为其建立隔离
环境，因此 AKM 自身（及打包后的 .app）无需携带 translators 等翻译依赖，
只需运行环境具备 ``uv`` 命令与可访问的脚本路径。

MCP stdio 传输采用「每行一个 JSON-RPC 消息」（NDJSON），而非 LSP 风格的
Content-Length framing；server 还可能在响应前推送无 id 的通知行，读取
响应时需要跳过这些行只匹配目标 id。
"""

import asyncio
import json
import shutil
from pathlib import Path

from akm.config import load_config

# MCP JSON-RPC 协议版本：与全局脚本所用 mcp 库协商的版本保持一致
_MCP_PROTOCOL_VERSION = "2025-06-18"
# 单次翻译调用（含 uv 首次解析依赖、多引擎 fallback 的网络耗时）的总体超时
_MCP_CALL_TIMEOUT_SEC = 90.0
# 默认翻译 MCP 脚本路径，可通过 config.json 的 agent_translate_mcp 覆盖
_DEFAULT_TRANSLATE_MCP = "~/.agents/plugins/translate-mcp.py"
# 打包后的 .app 从 GUI 启动时 PATH 可能不含用户级安装目录，因此除 PATH
# 外还显式探测这些常见位置（相对路径以 HOME 为基准）
_UV_CANDIDATE_DIRS = (
    ".local/bin",
    ".cargo/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/opt/uv/bin",
)


class TranslateMCPError(RuntimeError):
    """翻译 MCP 调用失败时抛出的异常。"""


def resolve_translate_script() -> str:
    """读取配置返回翻译 MCP 脚本的绝对路径（支持 ~ 展开）。"""
    raw = str(
        load_config().get("agent_translate_mcp") or _DEFAULT_TRANSLATE_MCP
    ).strip()
    return str(Path(raw).expanduser())


def resolve_uv_path() -> str | None:
    """解析 uv 可执行文件路径。

    优先走 ``shutil.which``（依赖 PATH）；GUI 启动的 .app 环境 PATH 往往
    不含 ``~/.local/bin`` 等目录，找不到时再逐个探测常见安装位置。返回
    完整可执行路径；全部找不到时返回 None。
    """
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home()
    for rel in _UV_CANDIDATE_DIRS:
        cand = Path(rel) if rel.startswith("/") else home / rel
        if (cand / "uv").is_file():
            return str(cand / "uv")
    return None


def uv_available() -> bool:
    """返回运行环境是否具备 uv 命令（PATH 或常见安装目录）。"""
    return resolve_uv_path() is not None


async def _write_json(stream, payload: dict) -> None:
    """以 NDJSON 行形式向子进程 stdin 写入一条 JSON-RPC 消息。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stream.write(data + b"\n")
    await stream.drain()


async def _read_response(stream, expected_id) -> dict:
    """逐行读取子进程 stdout，直到拿到目标 id 的 JSON-RPC 响应。

    MCP server 可能穿插推送无 id 的通知（如 notifications/message 日志），
    以及本客户端发出的 initialize 等其它请求的响应，都需要跳过，只返回
    匹配 expected_id 的那条响应。
    """
    while True:
        line = await stream.readline()
        if not line:
            raise TranslateMCPError("翻译脚本提前退出，未收到响应")
        try:
            msg = json.loads(line.decode("utf-8", errors="replace").strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or msg.get("id") != expected_id:
            continue
        if "error" in msg:
            err = msg["error"] or {}
            code = err.get("code", "")
            message = err.get("message", "")
            detail = f"{code}: {message}".strip(": ") or "翻译脚本返回错误"
            raise TranslateMCPError(detail)
        return msg


async def _mcp_call(script: str, tool_name: str, arguments: dict) -> dict:
    """启动 uv 子进程，完成一次 translate/detect_language 的 MCP 调用。

    流程：initialize → notifications/initialized → tools/call。每次调用
    起一个新子进程，结束时无论成败都终止进程，避免残留 uv/python 进程。
    """
    uv_path = resolve_uv_path()
    if not uv_path:
        raise TranslateMCPError("运行环境缺少 uv 命令，无法使用翻译工具")
    try:
        proc = await asyncio.create_subprocess_exec(
            uv_path, "run", script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise TranslateMCPError(f"无法启动翻译脚本: {exc}") from exc

    try:
        await _write_json(proc.stdin, {
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "akm-agent", "version": "1.0"},
            },
        })
        await _read_response(proc.stdout, 0)

        await _write_json(proc.stdin, {
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        })

        await _write_json(proc.stdin, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        return await _read_response(proc.stdout, 1)
    except TranslateMCPError as exc:
        # 子进程提前退出等场景，把 uv/脚本的 stderr 拼进错误信息，
        # 便于定位「uv run 启动失败」的真实原因
        stderr_text = ""
        try:
            stderr_text = (
                await asyncio.wait_for(proc.stderr.read(), timeout=3.0)
            ).decode("utf-8", errors="replace").strip()
        except Exception:
            stderr_text = ""
        if stderr_text:
            raise TranslateMCPError(f"{exc}（uv 输出: {stderr_text[-500:]}）") from exc
        raise
    finally:
        # 无论成败都终止子进程，避免残留 uv/python 进程
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass


async def _extract_text(result: dict) -> str:
    """从 tools/call 的 result.content 中拼接全部 text 内容。"""
    content = (result.get("result") or {}).get("content") or []
    parts = [
        str(item.get("text", ""))
        for item in content
        if isinstance(item, dict)
    ]
    return "\n".join(parts).strip()


async def translate_text(text: str, dest: str = "zh-cn", src: str = "auto") -> str:
    """把文本翻译为目标语言，返回翻译脚本给出的文本结果。

    脚本本身已做多引擎 fallback（bing/google/baidu/alibaba）与源语言检测，
    翻译失败时脚本会返回以「翻译失败」开头的文本，这里原样透传给模型。
    """
    result = await asyncio.wait_for(
        _mcp_call(
            resolve_translate_script(),
            "translate",
            {"text": text, "dest": dest, "src": src},
        ),
        timeout=_MCP_CALL_TIMEOUT_SEC,
    )
    return await _extract_text(result)


async def detect_language(text: str) -> str:
    """检测文本语言，返回脚本给出的语言代码与置信度文本。"""
    result = await asyncio.wait_for(
        _mcp_call(
            resolve_translate_script(),
            "detect_language",
            {"text": text},
        ),
        timeout=_MCP_CALL_TIMEOUT_SEC,
    )
    return await _extract_text(result)
