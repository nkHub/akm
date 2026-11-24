"""MCP 客户端：按需连接全局 MCP 配置里的服务器并代理其工具。

为什么走「代理」而不是把 MCP 工具注册进 ToolRegistry：
MCP 工具是运行时才发现的（取决于服务器是否在跑、拉起了几个工具），而 ToolRegistry
是进程级静态注册表，`_plan_tools` 每轮按注册内容规划。把 MCP 工具动态塞进注册表会让
tools 声明随服务器状态漂移，既破坏 prompt 缓存，也让权限审计失去稳定的工具名清单。
因此这里只暴露**一个**稳定工具 `akm_load_mcp`：先列清单、再按名字执行内层工具。

传输：stdio（子进程 + JSON-RPC over stdin/stdout）与 streamable HTTP 两种。
现有 `tavily_mcp.py` 是 Tavily 专用、只走 HTTP 且每次重新握手，这里新建一套通用的，
但复用它对「响应可能是 JSON 也可能是 SSE」的判定思路。

安全边界（**必须**）：
- 服务器名只能取自配置文件，模型无法构造路径或命令；
- 启用需显式开关 `agent_mcp_enabled`，且默认不注册工具（模型看不见就等于不可调）；
- 可选 `agent_mcp_allowed_servers` 白名单进一步收窄；
- 内层工具名必须是该服务器 `tools/list` 实际返回过的名字，落成白名单。
理由：MCP 服务器（如 ssh-mcp）能直接操作生产机，接上后会绕过 `agent_write_tools_enabled`
与 seatbelt 沙箱等全部既有开关——这是与文件工具完全不同的权限面。
"""

import asyncio
import atexit
import json
import logging
import os
from pathlib import Path
from typing import Any

from akm.config import load_config

logger = logging.getLogger(__name__)

#: 与 markdown_kb_mcp / tavily_mcp 保持同一协议版本。
MCP_PROTOCOL_VERSION = "2025-06-18"

#: 默认读取的全局 MCP 配置（PI 与多数客户端共用的格式）。
DEFAULT_CONFIG_PATH = "~/.pi/agent/mcp.json"

#: 单次请求默认超时（秒）。stdio 服务器首次启动可能拉包，留宽一些。
DEFAULT_TIMEOUT_SEC = 60

#: `tools/list` 结果在会话内的缓存时长（秒）。服务器热更新工具时靠它自然失效。
_TOOLS_CACHE_TTL_SEC = 300

#: 子进程 stderr 保留的最大字节数，仅用于出错时诊断（不参与 JSON 流）。
_STDERR_TAIL_BYTES = 8192

#: readline 的行长上限。tools/list 的响应可能很大，默认 64KB 会直接抛 LimitOverrunError。
_STREAM_LIMIT = 16 * 1024 * 1024


class MCPError(RuntimeError):
    """MCP 调用失败。"""


def config_path() -> Path | None:
    """返回 MCP 配置文件路径（已展开 ~）；文件不存在时返回 None。"""
    # 区分「未配置」（None，用默认路径）与「显式留空」（""，关闭该能力）：
    # 用 `or` 会把空串也当未配置，配置页里就关不掉这个功能。
    configured = load_config().get("agent_mcp_config_path")
    raw = str(DEFAULT_CONFIG_PATH if configured is None else configured).strip()
    if not raw:
        return None
    try:
        path = Path(raw).expanduser()
    except (OSError, RuntimeError):
        return None
    return path if path.is_file() else None


def load_servers() -> dict[str, dict]:
    """读取配置里的 mcpServers；文件缺失或格式非法时返回空 dict（不抛异常）。"""
    path = config_path()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[MCP] 读取配置失败 %s: %s", path, exc)
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)}


def allowed_servers() -> set[str]:
    """返回白名单服务器名集合；空白名单表示「不额外限制」（由总开关把关）。"""
    raw = str(load_config().get("agent_mcp_allowed_servers") or "").strip()
    return {item.strip() for item in raw.split(",") if item.strip()}


def resolve_server(name: str) -> tuple[str, dict]:
    """按名字取服务器配置，只接受配置里存在的名字。

    Raises:
        MCPError: 名字不存在、不在白名单内，或配置无法识别传输方式。
    """
    key = str(name or "").strip()
    if not key:
        raise MCPError("缺少 server 参数：请先不传 server 调用一次本工具，查看可用服务器")
    servers = load_servers()
    if key not in servers:
        available = ", ".join(sorted(servers)) or "（无，检查 agent_mcp_config_path）"
        raise MCPError(f"未找到 MCP 服务器: {key}；可用: {available}")
    whitelist = allowed_servers()
    if whitelist and key not in whitelist:
        raise MCPError(
            f"MCP 服务器 {key} 不在白名单内；当前 agent_mcp_allowed_servers = "
            f"{', '.join(sorted(whitelist))}"
        )
    cfg = servers[key]
    if not str(cfg.get("command") or "").strip() and not str(cfg.get("url") or "").strip():
        raise MCPError(f"MCP 服务器 {key} 的配置缺少 command 或 url")
    return key, cfg


class _StdioSession:
    """stdio 传输的 MCP 会话：一个子进程 + 行分隔 JSON-RPC。"""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._stderr_tail = bytearray()
        self._stderr_task: asyncio.Task | None = None
        self.tools: list[dict] = []
        self.tools_at = 0.0

    def _env(self) -> dict[str, str]:
        """合并进程环境与配置里的 env；剥离 PYTHON* 避免污染外部 Python。"""
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        extra = self.cfg.get("env")
        if isinstance(extra, dict):
            env.update({str(k): str(v) for k, v in extra.items()})
        return env

    async def start(self) -> None:
        """拉起子进程并完成 initialize 握手。"""
        command = str(self.cfg.get("command") or "").strip()
        args = [str(a) for a in (self.cfg.get("args") or [])]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
                limit=_STREAM_LIMIT,
            )
        except (OSError, ValueError) as exc:
            raise MCPError(f"MCP 服务器 {self.name} 启动失败（{command}）: {exc}") from exc
        # stderr 必须持续读走：管道写满会让子进程阻塞在 write 上，表现为调用永久挂住。
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "akm", "version": "1.0"},
            },
        )
        await self.notify("notifications/initialized")

    async def _drain_stderr(self) -> None:
        """把 stderr 收进有界缓冲区，仅用于失败诊断。"""
        stream = self.proc.stderr if self.proc else None
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > _STDERR_TAIL_BYTES:
                    del self._stderr_tail[: len(self._stderr_tail) - _STDERR_TAIL_BYTES]
        except (asyncio.CancelledError, OSError):
            return

    def stderr_text(self) -> str:
        return bytes(self._stderr_tail).decode("utf-8", errors="replace").strip()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _write(self, payload: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise MCPError(f"MCP 服务器 {self.name} 未启动")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            self.proc.stdin.write(line.encode("utf-8"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise MCPError(
                f"MCP 服务器 {self.name} 连接已断开；stderr: {self.stderr_text()[:300]}"
            ) from exc

    async def notify(self, method: str, params: dict | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write(payload)

    async def request(
        self, method: str, params: dict | None = None, timeout: float | None = None
    ) -> dict:
        """发一次 JSON-RPC 请求并等待同 id 的响应（跳过通知帧）。"""
        if self.proc is None or self.proc.stdout is None:
            raise MCPError(f"MCP 服务器 {self.name} 未启动")
        timeout = timeout or float(load_config().get("agent_mcp_timeout_sec") or DEFAULT_TIMEOUT_SEC)
        async with self._lock:
            self._next_id += 1
            request_id = self._next_id
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            await self._write(payload)
            try:
                return await asyncio.wait_for(
                    self._read_response(request_id, method), timeout=timeout
                )
            except asyncio.TimeoutError as exc:
                raise MCPError(
                    f"MCP 服务器 {self.name} 在 {timeout:.0f}s 内未响应 {method}；"
                    f"stderr: {self.stderr_text()[:300]}"
                ) from exc

    async def _read_response(self, request_id: int, method: str) -> dict:
        """逐行读 stdout，直到拿到匹配 id 的响应。"""
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            try:
                raw = await self.proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                raise MCPError(f"MCP 服务器 {self.name} 返回了超长单行响应") from exc
            if not raw:
                raise MCPError(
                    f"MCP 服务器 {self.name} 提前退出（{method}）；"
                    f"stderr: {self.stderr_text()[:300]}"
                )
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # 有些服务器会往 stdout 打非 JSON 日志，跳过而不是整轮失败
                logger.debug("[MCP] %s 非 JSON 输出: %s", self.name, line[:200])
                continue
            if not isinstance(data, dict):
                continue
            if data.get("id") != request_id:
                continue  # 通知或其他请求的响应
            if "error" in data:
                err = data["error"]
                message = err.get("message") if isinstance(err, dict) else str(err)
                raise MCPError(f"MCP {method} 失败: {message}")
            return data.get("result") or {}

    async def list_tools(self, *, refresh: bool = False) -> list[dict]:
        """取 tools/list，带短 TTL 缓存。"""
        import time

        if not refresh and self.tools and (time.time() - self.tools_at) < _TOOLS_CACHE_TTL_SEC:
            return self.tools
        result = await self.request("tools/list", {})
        tools = result.get("tools")
        self.tools = [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []
        self.tools_at = time.time()
        return self.tools

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def close(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        except (ProcessLookupError, OSError):
            pass
        # 只等进程退出不够：subprocess transport 的管道要到 loop 跑完收尾回调才释放，
        # 进程退出后立即使 loop 关闭（测试/停机路径）会让 __del__ 在已关 loop 上
        # 报「Event loop is closed」。显式关掉三个 pipe 并让出一轮，把回调跑完。
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None and not stream.is_closing():
                    stream.close()
            except (AttributeError, OSError):
                pass
        await asyncio.sleep(0)


class _HttpSession:
    """streamable HTTP 传输的 MCP 会话（无状态会话，每次请求独立）。"""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        self.url = str(cfg.get("url") or "").strip()
        self._session_id = ""
        # 与 stdio 侧同口径：请求 id 自增。固定 1 时同一会话内多请求并发，
        # 响应无法按 id 区分（SSE 帧可能乱序到达）。
        self._next_id = 0
        self.tools: list[dict] = []
        self.tools_at = 0.0

    async def start(self) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "akm", "version": "1.0"},
            },
        )
        await self.notify("notifications/initialized")

    def alive(self) -> bool:
        return bool(self.url)

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, "params": params or {}}, expect=False)

    async def _post(self, payload: dict, *, expect: bool = True) -> dict:
        import httpx

        headers = {"Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        timeout = float(load_config().get("agent_mcp_timeout_sec") or DEFAULT_TIMEOUT_SEC)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise MCPError(f"MCP 服务器 {self.name} 请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise MCPError(
                f"MCP 服务器 {self.name} 返回 HTTP {resp.status_code}: {resp.text[:200]}"
            )
        self._session_id = resp.headers.get("mcp-session-id") or self._session_id
        if not expect:
            return {}
        content_type = resp.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if raw and raw != "[DONE]":
                        return self._unwrap(json.loads(raw))
            raise MCPError(f"MCP 服务器 {self.name} 流式响应为空")
        try:
            return self._unwrap(resp.json())
        except json.JSONDecodeError as exc:
            raise MCPError(f"MCP 服务器 {self.name} 响应不是合法 JSON") from exc

    def _unwrap(self, data: dict) -> dict:
        if not isinstance(data, dict):
            return {}
        if "error" in data:
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise MCPError(f"MCP 请求失败: {message}")
        return data.get("result") or {}

    async def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        return await self._post(payload)

    async def list_tools(self, *, refresh: bool = False) -> list[dict]:
        import time

        if not refresh and self.tools and (time.time() - self.tools_at) < _TOOLS_CACHE_TTL_SEC:
            return self.tools
        result = await self.request("tools/list", {})
        tools = result.get("tools")
        self.tools = [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []
        self.tools_at = time.time()
        return self.tools

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def close(self) -> None:
        return None


#: server 名 → 会话。复用连接避免每次调用都拉起子进程（uvx 拉包要好几秒）。
_SESSIONS: dict[str, _StdioSession | _HttpSession] = {}
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}


def _new_session(name: str, cfg: dict) -> _StdioSession | _HttpSession:
    if str(cfg.get("url") or "").strip():
        return _HttpSession(name, cfg)
    return _StdioSession(name, cfg)


async def get_session(name: str) -> _StdioSession | _HttpSession:
    """取（必要时建立）一个服务器会话；进程已死或连接失败时重建。

    会话键带 loop_id：stdio 会话绑定着创建它的 event loop（子进程 transport 与
    读 task 都挂在那个 loop 上），跨 loop 复用会拿到属于已关 loop 的会话，
    表现为「Event loop is closed」或调用永久挂住。锁键与会话键必须同源，
    否则两个 loop 会同时认为自己是「第一个」。
    """
    key, cfg = resolve_server(name)
    loop_id = id(asyncio.get_running_loop())
    loop_key = f"{loop_id}:{key}"
    lock = _SESSION_LOCKS.setdefault(loop_key, asyncio.Lock())
    async with lock:
        session = _SESSIONS.get(loop_key)
        if session is not None and session.alive():
            return session
        if session is not None:
            await session.close()
            _SESSIONS.pop(loop_key, None)
        fresh = _new_session(key, cfg)
        await fresh.start()
        _SESSIONS[loop_key] = fresh
        return fresh


async def close_all() -> None:
    """关闭全部会话（测试与服务停机用）。"""
    for name, session in list(_SESSIONS.items()):
        try:
            await session.close()
        except Exception as exc:  # noqa: BLE001 - 清理阶段不应抛
            logger.debug("[MCP] 关闭 %s 失败: %s", name, exc)
        _SESSIONS.pop(name, None)
    _SESSION_LOCKS.clear()


def _close_all_sync() -> None:
    """atexit 兜底：进程退出时终止遗留的 stdio 子进程，避免留下孤儿。"""
    for name, session in list(_SESSIONS.items()):
        if isinstance(session, _StdioSession) and session.proc is not None:
            try:
                if session.proc.returncode is None:
                    session.proc.kill()
            except (ProcessLookupError, OSError):
                pass
        _SESSIONS.pop(name, None)


atexit.register(_close_all_sync)


def _summarize_tool(tool: dict) -> dict[str, Any]:
    """把 MCP 工具定义压成可下发的摘要（名字 + 描述 + 参数结构）。"""
    schema = tool.get("inputSchema") or tool.get("parameters") or {"type": "object", "properties": {}}
    description = " ".join(str(tool.get("description") or "").split())
    return {
        "name": str(tool.get("name") or ""),
        "description": description[:400],
        "parameters": schema,
    }


def _content_to_text(result: dict) -> tuple[str, bool]:
    """把 tools/call 的 content 数组拍平成文本，返回 (文本, 是否 isError)。"""
    is_error = bool(result.get("isError"))
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "text":
                parts.append(str(item.get("text") or ""))
            elif kind in ("image", "audio"):
                parts.append(f"[{kind}: {item.get('mimeType') or 'unknown'}]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
    elif content is not None:
        parts.append(str(content))
    if not parts and result:
        parts.append(json.dumps(result, ensure_ascii=False))
    return "\n".join(p for p in parts if p), is_error


async def list_servers_summary() -> list[dict[str, Any]]:
    """列出配置里的服务器（不连接，避免为了一次列举拉起 8 个子进程）。"""
    whitelist = allowed_servers()
    out: list[dict[str, Any]] = []
    for name, cfg in sorted(load_servers().items()):
        if whitelist and name not in whitelist:
            continue
        transport = "http" if str(cfg.get("url") or "").strip() else "stdio"
        entry: dict[str, Any] = {"server": name, "transport": transport}
        if "command" in cfg:
            args = " ".join(str(a) for a in (cfg.get("args") or []))
            entry["command"] = f"{cfg.get('command')} {args}".strip()
        if "url" in cfg:
            entry["url"] = str(cfg.get("url"))
        out.append(entry)
    return out


async def load_mcp(server: str = "", tool: str = "", arguments: dict | None = None) -> dict[str, Any]:
    """列举 MCP 服务器/工具，或执行其中一个工具。

    Args:
        server: 服务器名。留空则列出全部可用服务器（不连接）。
        tool: 工具名。留空则连接该服务器并列出它的工具。
        arguments: 执行工具时的参数对象。

    Returns:
        dict；失败时含 ``ok=False`` 与 ``error``。
    """
    if not server.strip():
        servers = await list_servers_summary()
        return {
            "ok": True,
            "servers": servers,
            "hint": (
                "传 server 查看该服务器的工具清单；传 server + tool + arguments 执行工具"
                if servers
                else "没有可用的 MCP 服务器：检查 agent_mcp_config_path 指向的配置文件"
            ),
        }
    try:
        session = await get_session(server)
        tools = await session.list_tools(refresh=not tool.strip())
    except MCPError as exc:
        return {"ok": False, "error": str(exc)}

    name = str(tool or "").strip()
    if not name:
        summaries = [_summarize_tool(t) for t in tools]
        return {
            "ok": True,
            "server": server,
            "tools": summaries,
            "hint": (
                f"用 server={server} + tool=<上面的 name> + arguments 执行"
                if summaries
                else "该服务器没有暴露任何工具"
            ),
        }

    # 内层工具名必须落在该服务器实际返回的清单里，否则等于允许模型凭空构造调用。
    # 缓存命中时可能是 300s TTL 内的旧清单：服务器在 TTL 内新加了工具，
    # 不刷新就会误报「没有工具 X」，所以未命中时强制刷一次再判断。
    known = {str(t.get("name") or "") for t in tools}
    if name not in known:
        try:
            tools = await session.list_tools(refresh=True)
        except MCPError as exc:
            return {"ok": False, "error": str(exc)}
        known = {str(t.get("name") or "") for t in tools}
    if name not in known:
        return {
            "ok": False,
            "error": f"MCP 服务器 {server} 没有工具 {name}",
            "available": ", ".join(sorted(k for k in known if k)) or "（无）",
        }
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "arguments 必须是 JSON 对象"}
    try:
        result = await session.call_tool(name, arguments)
    except MCPError as exc:
        return {"ok": False, "error": str(exc)}
    text, is_error = _content_to_text(result)
    payload: dict[str, Any] = {"ok": not is_error, "server": server, "tool": name, "content": text}
    if is_error:
        payload["error"] = text or "MCP 工具返回错误"
    return payload
