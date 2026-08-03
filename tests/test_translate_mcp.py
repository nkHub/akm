"""翻译 MCP 客户端与 akm_translate / akm_detect_language 内置工具的测试。"""

import json

import pytest
from fastapi import FastAPI

from akm.agent_runtime import translate_mcp
from akm.agent_runtime.loop import ToolDef
from akm.agent_runtime.tools import build_builtin_tools


class FakeStream:
    """模拟 asyncio 子进程的 stdin/stdout 流。

    readline 依次返回预设字节行；write/drain 记录写入内容供断言。
    """

    def __init__(self, lines=None):
        self._buf = list(lines or [])
        self.written = []

    def write(self, data):
        # asyncio StreamWriter.write 是同步方法，这里同样用同步实现
        self.written.append(data)

    async def drain(self):
        pass

    async def readline(self):
        if not self._buf:
            return b""
        return self._buf.pop(0)


class FakeProc:
    """模拟 asyncio.create_subprocess_exec 返回的进程对象。"""

    def __init__(self, stdin, stdout):
        self.stdin = stdin
        self.stdout = stdout
        self.terminated = False
        self.waited = False

    def terminate(self):
        self.terminated = True

    async def wait(self):
        self.waited = True
        return 0


def _line(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _install_fake_exec(monkeypatch, stdout_lines, written=None):
    """把 translate_mcp 的 create_subprocess_exec 替换为返回 FakeProc 的假实现。"""
    created = {}

    async def fake_exec(*args, **kwargs):
        stdin = FakeStream()
        proc = FakeProc(stdin, FakeStream(list(stdout_lines)))
        created["cmd"] = args
        created["proc"] = proc
        return proc

    monkeypatch.setattr(translate_mcp.asyncio, "create_subprocess_exec", fake_exec)
    return created


# ── 协议层：_mcp_call / translate_text / detect_language ──


@pytest.mark.asyncio
async def test_translate_text_roundtrip(monkeypatch):
    """完整 MCP 序列：initialize → 通知 → tools/call，并返回拼接文本。"""
    monkeypatch.setattr(
        translate_mcp, "load_config",
        lambda: {"agent_translate_mcp": "~/.agents/plugins/translate-mcp.py"},
    )
    created = _install_fake_exec(monkeypatch, [
        _line({"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-06-18"}}),
        _line({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": "原文: 你好\n译文: Hello"}]}}),
    ])

    result = await translate_mcp.translate_text("你好", dest="en")

    assert result == "原文: 你好\n译文: Hello"
    proc = created["proc"]
    assert proc.terminated
    # 断言命令为 uv run 脚本，且请求序列包含 initialize 与 tools/call
    assert created["cmd"][0] == "uv"
    assert created["cmd"][1] == "run"
    written = b"".join(proc.stdin.written).decode("utf-8").splitlines()
    assert json.loads(written[0])["method"] == "initialize"
    assert json.loads(written[1])["method"] == "notifications/initialized"
    call = json.loads(written[2])
    assert call["method"] == "tools/call"
    assert call["params"] == {
        "name": "translate",
        "arguments": {"text": "你好", "dest": "en", "src": "auto"},
    }


@pytest.mark.asyncio
async def test_read_response_skips_untargeted_lines(monkeypatch):
    """读取响应时跳过无 id 的通知行与不匹配的 id。"""
    monkeypatch.setattr(translate_mcp, "load_config", lambda: {})
    created = _install_fake_exec(monkeypatch, [
        _line({"jsonrpc": "2.0", "method": "notifications/message",
               "params": {"level": "warning", "data": "log line"}}),
        _line({"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-06-18"}}),
        _line({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}),
        _line({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "ok"}]}}),
    ])

    result = await translate_mcp.detect_language("Bonjour")
    assert result == "ok"
    assert created["proc"].terminated


@pytest.mark.asyncio
async def test_read_response_raises_on_jsonrpc_error(monkeypatch):
    """tools/call 返回 JSON-RPC error 时抛出 TranslateMCPError。"""
    monkeypatch.setattr(translate_mcp, "load_config", lambda: {})
    _install_fake_exec(monkeypatch, [
        _line({"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-06-18"}}),
        _line({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad args"}}),
    ])

    with pytest.raises(translate_mcp.TranslateMCPError, match="bad args"):
        await translate_mcp.translate_text("x")


@pytest.mark.asyncio
async def test_mcp_call_terminates_process_after_error(monkeypatch):
    """发生错误后子进程仍会被 terminate，避免残留进程。"""
    monkeypatch.setattr(translate_mcp, "load_config", lambda: {})
    created = _install_fake_exec(monkeypatch, [
        _line({"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-06-18"}}),
        _line({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}),
    ])

    with pytest.raises(translate_mcp.TranslateMCPError):
        await translate_mcp.detect_language("x")
    assert created["proc"].terminated


@pytest.mark.asyncio
async def test_translate_text_raises_when_script_exits_early(monkeypatch):
    """脚本提前退出（readline 返回空）时抛出明确错误。"""
    monkeypatch.setattr(translate_mcp, "load_config", lambda: {})
    _install_fake_exec(monkeypatch, [])

    with pytest.raises(translate_mcp.TranslateMCPError, match="提前退出"):
        await translate_mcp.translate_text("x")


# ── build_builtin_tools 注册与 handler ──


def test_builtin_tools_register_translate_and_detect():
    from fastapi import FastAPI

    app = FastAPI()
    tools = build_builtin_tools(app)
    names = [tool.name for tool in tools]
    assert "akm_translate" in names
    assert "akm_detect_language" in names

    tool: ToolDef = next(t for t in tools if t.name == "akm_translate")
    assert tool.parameters["required"] == ["text"]
    assert tool.parameters["properties"]["dest"]["default"] == "zh-cn"

    detect: ToolDef = next(t for t in tools if t.name == "akm_detect_language")
    assert detect.parameters["required"] == ["text"]


@pytest.mark.asyncio
async def test_translate_tool_missing_uv(monkeypatch):
    """uv 不可用时不启动子进程，返回结构化错误。"""
    monkeypatch.setattr("akm.agent_runtime.tools.uv_available", lambda: False)
    tools = build_builtin_tools(FastAPI())
    tool = next(t for t in tools if t.name == "akm_translate")
    text = await tool.handler(text="你好")
    assert "uv 命令" in text


@pytest.mark.asyncio
async def test_translate_tool_script_missing(monkeypatch, tmp_path):
    """脚本路径不存在时返回明确错误。"""
    monkeypatch.setattr("akm.agent_runtime.tools.uv_available", lambda: True)
    monkeypatch.setattr(
        "akm.agent_runtime.tools.resolve_translate_script",
        lambda: str(tmp_path / "nope.py"),
    )
    tools = build_builtin_tools(FastAPI())
    tool = next(t for t in tools if t.name == "akm_translate")
    text = await tool.handler(text="你好")
    assert "翻译脚本不存在" in text


@pytest.mark.asyncio
async def test_translate_tool_delegates_and_returns_text(monkeypatch, tmp_path):
    """uv 与脚本就绪时委托 translate_text，成功返回纯文本结果。"""
    script = tmp_path / "translate-mcp.py"
    script.write_text("#!/usr/bin/env python\n", encoding="utf-8")

    monkeypatch.setattr("akm.agent_runtime.tools.uv_available", lambda: True)
    monkeypatch.setattr(
        "akm.agent_runtime.tools.resolve_translate_script", lambda: str(script)
    )

    async def fake_translate(text, dest="zh-cn", src="auto"):
        return f"原文: {text}\n译文: Hello"

    monkeypatch.setattr("akm.agent_runtime.tools.translate_text", fake_translate)

    tools = build_builtin_tools(FastAPI())
    tool = next(t for t in tools if t.name == "akm_translate")
    text = await tool.handler(text="你好", dest="en")
    assert text == "原文: 你好\n译文: Hello"


@pytest.mark.asyncio
async def test_translate_tool_returns_error_on_exception(monkeypatch, tmp_path):
    """翻译异常时返回结构化错误而非抛出。"""
    script = tmp_path / "translate-mcp.py"
    script.write_text("x", encoding="utf-8")

    monkeypatch.setattr("akm.agent_runtime.tools.uv_available", lambda: True)
    monkeypatch.setattr(
        "akm.agent_runtime.tools.resolve_translate_script", lambda: str(script)
    )

    async def fake_translate(text, dest="zh-cn", src="auto"):
        raise translate_mcp.TranslateMCPError("网络超时")

    monkeypatch.setattr("akm.agent_runtime.tools.translate_text", fake_translate)

    tools = build_builtin_tools(FastAPI())
    tool = next(t for t in tools if t.name == "akm_translate")
    text = await tool.handler(text="你好")
    assert "网络超时" in text


@pytest.mark.asyncio
async def test_detect_language_tool_success(monkeypatch, tmp_path):
    """akm_detect_language 成功时返回脚本给出的文本结果。"""
    script = tmp_path / "translate-mcp.py"
    script.write_text("x", encoding="utf-8")

    monkeypatch.setattr("akm.agent_runtime.tools.uv_available", lambda: True)
    monkeypatch.setattr(
        "akm.agent_runtime.tools.resolve_translate_script", lambda: str(script)
    )

    async def fake_detect(text):
        return "检测语言: en\n置信度: 99.00%"

    monkeypatch.setattr("akm.agent_runtime.tools.detect_language", fake_detect)

    tools = build_builtin_tools(FastAPI())
    tool = next(t for t in tools if t.name == "akm_detect_language")
    text = await tool.handler(text="Bonjour")
    assert "en" in text
