"""MCP 客户端与 akm_load_mcp 工具测试。

覆盖的行为契约：
- 配置读取容错：文件缺失 / JSON 非法 / 缺 mcpServers 时返回空，不抛异常；
- 服务器名只认配置文件里的名字，模型传路径或命令一律拒绝；
- 白名单 `agent_mcp_allowed_servers` 生效；
- stdio 全链路：initialize 握手 → tools/list → tools/call，含 isError 透传；
- 内层工具名必须是 tools/list 实际返回过的名字（防模型凭空构造）；
- 会话复用（同一服务器不重复拉起子进程）与进程死亡后重建；
- 启动失败 / 超时给出可诊断错误而不是挂死；
- 工具注册受 `agent_mcp_enabled` 控制（默认关闭 = 模型看不见）；
- `akm_load_mcp` 在网关常驻名单里（避免三层跳）。
"""

import json
import sys

import pytest
from fastapi import FastAPI

from akm.agent_runtime import mcp_client

# 用例结束时会 kill 掉 stdio 子进程，而 asyncio 的 subprocess transport 在事件循环
# 关闭后才被 GC 回收，于是抛一条与本产品无关的 "Event loop is closed" 噪声。
# 产品侧由 mcp_client 的 atexit 钩子负责终止子进程，这里只需静音测试基建的回收顺序。
pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")

#: 最小 stdio MCP 服务器：只实现 initialize / tools/list / tools/call。
_FAKE_SERVER = r'''
import json, sys

TOOLS = [
    {"name": "echo", "description": "回显", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "boom", "description": "总是报错", "inputSchema": {"type": "object", "properties": {}}},
]

def reply(i, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": i, "result": result}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        reply(mid, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        reply(mid, {"tools": TOOLS})
    elif method == "tools/call":
        p = msg.get("params") or {}
        if p.get("name") == "echo":
            reply(mid, {"content": [{"type": "text", "text": "echo: " + str((p.get("arguments") or {}).get("text", ""))}]})
        elif p.get("name") == "boom":
            reply(mid, {"content": [{"type": "text", "text": "出错了"}], "isError": True})
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown"}}) + "\n")
            sys.stdout.flush()
'''


def _session(server: str):
    """按 server 名取当前 loop 的会话（_SESSIONS 的键带 loop_id，不能直接按名查）。"""
    import asyncio

    return mcp_client._SESSIONS[f"{id(asyncio.get_running_loop())}:{server}"]


@pytest.fixture()
async def fake_config(tmp_path, monkeypatch):
    """把 MCP 配置指向临时文件，并让 load_config 返回受控值。"""
    script = tmp_path / "fake_server.py"
    script.write_text(_FAKE_SERVER, encoding="utf-8")
    cfg_file = tmp_path / "mcp.json"
    state = {
        "path": str(cfg_file),
        "allowed": "",
        "timeout": 20,
    }

    def write_servers(servers: dict) -> None:
        cfg_file.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    write_servers({
        "fake": {"command": sys.executable, "args": [str(script)]},
    })
    monkeypatch.setattr(
        mcp_client,
        "load_config",
        lambda: {
            "agent_mcp_config_path": state["path"],
            "agent_mcp_allowed_servers": state["allowed"],
            "agent_mcp_timeout_sec": state["timeout"],
        },
    )
    mcp_client._SESSIONS.clear()
    mcp_client._SESSION_LOCKS.clear()
    yield {"file": cfg_file, "script": script, "write": write_servers, "state": state}
    # 每个用例结束都要 await 进程真正退出：只 kill 不 wait 会把
    # BaseSubprocessTransport 留到 loop 关闭后才 GC，pytest 报
    # PytestUnraisableExceptionWarning（Event loop is closed）。
    for session in list(mcp_client._SESSIONS.values()):
        try:
            await session.close()
        except Exception:  # noqa: BLE001 - 清理阶段不应让用例失败
            pass
    mcp_client._SESSIONS.clear()
    mcp_client._SESSION_LOCKS.clear()


def test_missing_config_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mcp_client, "load_config", lambda: {"agent_mcp_config_path": str(tmp_path / "nope.json")}
    )
    assert mcp_client.config_path() is None
    assert mcp_client.load_servers() == {}


def test_invalid_json_returns_empty(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{ 不是 json", encoding="utf-8")
    monkeypatch.setattr(mcp_client, "load_config", lambda: {"agent_mcp_config_path": str(bad)})
    assert mcp_client.load_servers() == {}


def test_config_without_mcpservers_returns_empty(tmp_path, monkeypatch):
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"other": 1}), encoding="utf-8")
    monkeypatch.setattr(mcp_client, "load_config", lambda: {"agent_mcp_config_path": str(f)})
    assert mcp_client.load_servers() == {}


def test_empty_config_path_disables(tmp_path, monkeypatch):
    """留空表示关闭：不能回落到默认路径。"""
    monkeypatch.setattr(mcp_client, "load_config", lambda: {"agent_mcp_config_path": ""})
    assert mcp_client.config_path() is None


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "..", "rm -rf /", "fake/../fake"])
def test_unknown_server_rejected(fake_config, bad):
    """服务器名只做配置里的键匹配，路径或命令构造都匹配不到。"""
    with pytest.raises(mcp_client.MCPError):
        mcp_client.resolve_server(bad)


def test_whitelist_blocks_server(fake_config):
    fake_config["state"]["allowed"] = "other-server"
    with pytest.raises(mcp_client.MCPError) as exc:
        mcp_client.resolve_server("fake")
    assert "白名单" in str(exc.value)


def test_whitelist_allows_listed_server(fake_config):
    fake_config["state"]["allowed"] = "fake,other"
    key, cfg = mcp_client.resolve_server("fake")
    assert key == "fake"
    assert "command" in cfg


def test_missing_command_and_url_rejected(fake_config):
    fake_config["write"]({"weird": {"env": {}}})
    with pytest.raises(mcp_client.MCPError) as exc:
        mcp_client.resolve_server("weird")
    assert "command 或 url" in str(exc.value)


async def test_list_servers_does_not_spawn(fake_config):
    """列服务器不应该拉起任何子进程（否则一次列举会启动 8 个）。"""
    result = await mcp_client.load_mcp()
    assert result["ok"] is True
    assert [s["server"] for s in result["servers"]] == ["fake"]
    assert result["servers"][0]["transport"] == "stdio"
    assert mcp_client._SESSIONS == {}


async def test_stdio_list_tools(fake_config):
    result = await mcp_client.load_mcp(server="fake")
    assert result["ok"] is True
    names = [t["name"] for t in result["tools"]]
    assert names == ["echo", "boom"]
    assert result["tools"][0]["parameters"]["properties"]["text"]["type"] == "string"


async def test_stdio_call_tool(fake_config):
    result = await mcp_client.load_mcp(server="fake", tool="echo", arguments={"text": "你好"})
    assert result["ok"] is True
    assert "echo: 你好" in result["content"]


async def test_stdio_iserror_propagated(fake_config):
    result = await mcp_client.load_mcp(server="fake", tool="boom")
    assert result["ok"] is False
    assert result["error"] == "出错了"


async def test_unknown_tool_rejected_with_available(fake_config):
    result = await mcp_client.load_mcp(server="fake", tool="nope")
    assert result["ok"] is False
    assert "没有工具 nope" in result["error"]
    assert "echo" in result["available"]


async def test_arguments_must_be_object(fake_config):
    result = await mcp_client.load_mcp(server="fake", tool="echo", arguments="not-a-dict")
    assert result["ok"] is False
    assert "arguments" in result["error"]


async def test_session_is_reused(fake_config):
    """同一服务器第二次调用复用同一子进程，不重复 spawn。"""
    await mcp_client.load_mcp(server="fake")
    first = _session("fake")
    pid = first.proc.pid
    await mcp_client.load_mcp(server="fake", tool="echo", arguments={"text": "x"})
    assert _session("fake") is first
    assert _session("fake").proc.pid == pid


async def test_dead_session_is_replaced(fake_config):
    """子进程被杀后下次调用应重建，而不是一直报连接断开。"""
    await mcp_client.load_mcp(server="fake")
    first = _session("fake")
    first.proc.kill()
    await first.proc.wait()
    result = await mcp_client.load_mcp(server="fake")
    assert result["ok"] is True
    assert _session("fake") is not first


async def test_startup_failure_is_reported(fake_config):
    fake_config["write"]({"broken": {"command": "/nonexistent/binary-xyz", "args": []}})
    result = await mcp_client.load_mcp(server="broken")
    assert result["ok"] is False
    assert "启动失败" in result["error"]


async def test_timeout_is_reported(fake_config, tmp_path):
    """服务器不响应时必须超时返回，而不是把 Agent 循环挂死。"""
    script = tmp_path / "silent.py"
    script.write_text("import sys, time\nfor _ in sys.stdin: time.sleep(60)\n", encoding="utf-8")
    fake_config["write"]({"silent": {"command": sys.executable, "args": [str(script)]}})
    fake_config["state"]["timeout"] = 5  # 下限 5 秒，归一化后不会更低
    mcp_client.DEFAULT_TIMEOUT_SEC = 5
    result = await mcp_client.load_mcp(server="silent")
    assert result["ok"] is False
    assert "未响应" in result["error"] or "提前退出" in result["error"]


async def test_close_all_kills_processes(fake_config):
    await mcp_client.load_mcp(server="fake")
    session = _session("fake")
    proc = session.proc
    await mcp_client.close_all()
    assert mcp_client._SESSIONS == {}
    assert proc.returncode is not None


def test_tool_registered_only_when_enabled(monkeypatch):
    """默认关闭时工具不注册——模型看不见就等于不可调。"""
    import akm.agent_runtime.tools as tools_module

    disabled = {"agent_mcp_enabled": False, "flow_enabled": False, "agent_email_enabled": False}
    monkeypatch.setattr(tools_module, "load_config", lambda: dict(disabled))
    names = [t.name for t in tools_module.build_builtin_tools(FastAPI())]
    assert "akm_load_mcp" not in names

    enabled = dict(disabled, agent_mcp_enabled=True)
    monkeypatch.setattr(tools_module, "load_config", lambda: dict(enabled))
    names = [t.name for t in tools_module.build_builtin_tools(FastAPI())]
    assert "akm_load_mcp" in names


def test_load_mcp_is_gateway_pinned():
    """折叠会让使用链路变成三层，必须常驻。"""
    from akm.agent_runtime.loop import _GATEWAY_PINNED_TOOLS

    assert "akm_load_mcp" in _GATEWAY_PINNED_TOOLS


def test_content_to_text_handles_image_and_unknown():
    text, is_error = mcp_client._content_to_text({
        "content": [
            {"type": "text", "text": "第一段"},
            {"type": "image", "mimeType": "image/png"},
            {"type": "resource", "uri": "x"},
        ]
    })
    assert "第一段" in text
    assert "[image: image/png]" in text
    assert is_error is False


def test_content_to_text_empty_result():
    text, is_error = mcp_client._content_to_text({})
    assert text == ""
    assert is_error is False
