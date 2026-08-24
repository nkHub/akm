"""akm_read_image 内置工具（给 /v1/agent 的 Agent Loop 工具集）的测试。

该工具调用配置的视觉模型做一次多模态 chat/completions 请求来描述一张图片，
图片来源支持 image_path 本地文件与 image_base64（data URL / 裸 base64）两种。
视觉模型取 agent_config.agent_vision_model（默认 gpt-5.6-luna，与图片生成的
image_supported_models 相互独立）；开关 agent_read_image_enabled 控制是否注册进工具集。
"""

import base64
import json

import pytest
from fastapi import FastAPI

from akm.agent_runtime.loop import ToolDef
from akm.agent_runtime import tools as tools_module
from akm.agent_runtime.tools import build_builtin_tools


class FakePool:
    """记录 get_client 参数并返回假 client 的连接池。"""

    is_route_pool = True

    def __init__(self, client: object = "fake-client"):
        self.calls = []
        self.client = client

    async def get_client(self, **kwargs):
        self.calls.append(kwargs)
        return self.client


def _config(use_image_path=True):
    """返回常见视觉模型配置，确保 akm_read_image 被注册。"""
    return {"image_supported_models": "gpt-4o-vision, gpt-4o"}


def _read_tool(app):
    tools = build_builtin_tools(app)
    return next(tool for tool in tools if tool.name == "akm_read_image")


# ── 注册 ──


def test_builtin_tools_register_read_image(monkeypatch):
    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(tools_module, "load_config", lambda: {"image_supported_models": "foo"})
    tool = _read_tool(app)
    props = tool.parameters["properties"]
    assert set(props) == {"image_path", "image_base64", "prompt", "model"}
    assert tool.parameters["required"] == []
    assert isinstance(tool, ToolDef)


def test_builtin_tools_register_read_image_disabled(monkeypatch):
    """agent_read_image_enabled=false 时不注册 akm_read_image。"""
    app = FastAPI()
    app.state.http_client = FakePool()

    def fake_config():
        return {"agent_read_image_enabled": False}

    monkeypatch.setattr(tools_module, "load_config", fake_config)
    tools = build_builtin_tools(app)
    assert not any(tool.name == "akm_read_image" for tool in tools)


# ── 成功路径 ──


@pytest.mark.asyncio
async def test_read_image_success_from_path(monkeypatch, tmp_path):
    """image_path 输入：把图片编码为 data URL 的 image_url 块发给视觉模型，
    返回 description。model 未传时回退到 agent_vision_model。"""
    captured = {}

    async def fake_forward(body, client, **kwargs):
        captured["body"] = body
        captured["client"] = client
        captured["kwargs"] = kwargs
        return {
            "status_code": 200,
            "body": json.dumps({"choices": [{"message": {"content": "一只坐着的橘猫。"}}]}),
            "error": "",
        }

    app = FastAPI()
    app.state.http_client = FakePool()
    img = tmp_path / "cat.png"
    raw = b"\x89PNG\r\n\x1a\nfakecat"
    img.write_bytes(raw)

    def fake_config():
        return {"image_supported_models": "gpt-4o", "agent_vision_model": "gpt-4o-vision"}

    monkeypatch.setattr(tools_module, "load_config", fake_config)
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _read_tool(app)
    text = await tool.handler(image_path=str(img), prompt="这只猫在做什么？")

    result = json.loads(text)
    assert result["image"] == "cat.png"
    assert result["model"] == "gpt-4o-vision"
    assert result["description"] == "一只坐着的橘猫。"
    body = captured["body"]
    assert body["stream"] is False
    assert body["model"] == "gpt-4o-vision"
    msg = body["messages"][0]
    assert msg["role"] == "user"
    assert msg["content"][0] == {"type": "text", "text": "这只猫在做什么？"}
    ic = msg["content"][1]
    assert ic["type"] == "image_url"
    assert ic["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(ic["image_url"]["url"].split(",", 1)[1]) == raw
    assert captured["kwargs"]["api_path"] == "chat/completions"
    assert isinstance(captured["kwargs"]["request_timeout"], float)
    assert isinstance(captured["client"], str)  # FakePool 返回的假 client


@pytest.mark.asyncio
async def test_read_image_success_from_base64_data_url(monkeypatch):
    """image_base64 带 data: 前缀：直接复用 data URL，无需本地文件。"""
    captured = {}
    raw = b"\x89PNG\r\n\x1a\nfromb64"

    async def fake_forward(body, client, **kwargs):
        captured["body"] = body
        return {"status_code": 200, "body": json.dumps({"choices": [{"message": {"content": "描述"}}]}), "error": ""}

    app = FastAPI()
    app.state.http_client = FakePool()
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")

    def fake_config():
        return {"image_supported_models": "gpt-4o", "agent_vision_model": "gpt-4o"}

    monkeypatch.setattr(tools_module, "load_config", fake_config)
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _read_tool(app)
    text = await tool.handler(image_base64=data_url)

    result = json.loads(text)
    assert result["image"].endswith(".png")
    assert result["model"] == "gpt-4o"
    assert result["description"] == "描述"
    ic = captured["body"]["messages"][0]["content"][1]
    assert ic["image_url"]["url"].startswith("data:image/png;base64,")
    # 透传的 data URL 与原输入一致（重新转码回原始字节）
    assert base64.b64decode(ic["image_url"]["url"].split(",", 1)[1]) == raw


@pytest.mark.asyncio
async def test_read_image_model_default_when_unconfigured(monkeypatch, tmp_path):
    """agent_vision_model 未配置时模型为空，应提示未配置视觉模型（不再回退到图片生成模型）。"""
    captured = {}

    async def fake_forward(body, client, **kwargs):
        captured["body"] = body
        return {"status_code": 200, "body": json.dumps({"choices": [{"message": {"content": "x"}}]}), "error": ""}

    app = FastAPI()
    app.state.http_client = FakePool()
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    # 只配置图片生成模型，未配置 agent_vision_model
    monkeypatch.setattr(tools_module, "load_config", lambda: {"image_supported_models": " vision-1 , vision-2 "})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _read_tool(app)
    text = await tool.handler(image_path=str(img))
    assert "未配置视觉模型" in json.loads(text)["error"]
    assert "called" not in captured  # 未触发上游请求


@pytest.mark.asyncio
async def test_read_image_default_prompt(monkeypatch, tmp_path):
    """prompt 留空时使用默认描述提示词。"""
    captured = {}

    async def fake_forward(body, client, **kwargs):
        captured["body"] = body
        return {"status_code": 200, "body": json.dumps({"choices": [{"message": {"content": "ok"}}]}), "error": ""}

    app = FastAPI()
    app.state.http_client = FakePool()
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    monkeypatch.setattr(tools_module, "load_config", lambda: {"image_supported_models": "gpt-4o", "agent_vision_model": "gpt-4o"})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _read_tool(app)
    await tool.handler(image_path=str(img), prompt="")
    text_block = captured["body"]["messages"][0]["content"][0]
    assert text_block["text"] == "请描述这张图片的内容。"


# ── 失败路径 ──


@pytest.mark.asyncio
async def test_read_image_disabled_in_handler(monkeypatch):
    """即使工具已注册，agent_read_image_enabled=false 时 handler 也返回 error。"""
    app = FastAPI()
    app.state.http_client = FakePool()
    state = {"enabled": True}

    def fake_config():
        return {"agent_read_image_enabled": state["enabled"], "image_supported_models": "gpt-4o"}

    monkeypatch.setattr(tools_module, "load_config", fake_config)
    tool = _read_tool(app)  # 注册时开关为真，工具存在
    state["enabled"] = False  # 运行时开关被关闭
    text = await tool.handler(image_path="/tmp/x.png")
    assert "未启用" in json.loads(text)["error"]


@pytest.mark.asyncio
async def test_read_image_no_source(monkeypatch):
    """image_path 与 image_base64 都为空时返回结构化错误，不触发上游请求。"""
    called = {"n": 0}

    async def fake_forward(body, client, **kwargs):
        called["n"] += 1
        return {"status_code": 200, "body": "{}", "error": ""}

    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(tools_module, "load_config", lambda: {"image_supported_models": "gpt-4o"})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _read_tool(app)
    text = await tool.handler()
    assert "必须至少提供一个" in json.loads(text)["error"]
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_read_image_missing_file(monkeypatch):
    """image_path 不存在时返回结构化错误，不触发上游请求。"""
    called = {"n": 0}

    async def fake_forward(body, client, **kwargs):
        called["n"] += 1
        return {"status_code": 200, "body": "{}", "error": ""}

    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(tools_module, "load_config", lambda: {"image_supported_models": "gpt-4o"})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _read_tool(app)
    text = await tool.handler(image_path="/nonexistent/nope.png")
    assert "不存在" in json.loads(text)["error"]
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_read_image_invalid_base64(monkeypatch):
    """base64 数据非法时返回结构化错误，不触发上游请求。"""
    called = {"n": 0}

    async def fake_forward(body, client, **kwargs):
        called["n"] += 1
        return {"status_code": 200, "body": "{}", "error": ""}

    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(tools_module, "load_config", lambda: {"image_supported_models": "gpt-4o"})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _read_tool(app)
    text = await tool.handler(image_base64="not!!base64!!")
    assert "解码失败" in json.loads(text)["error"]
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_read_image_no_vision_model(monkeypatch, tmp_path):
    """agent_vision_model 未配置时返回「未配置视觉模型」且不触发上游请求。"""
    called = {"n": 0}

    async def fake_forward(body, client, **kwargs):
        called["n"] += 1
        return {"status_code": 200, "body": "{}", "error": ""}

    app = FastAPI()
    app.state.http_client = FakePool()
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    monkeypatch.setattr(tools_module, "load_config", lambda: {})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _read_tool(app)
    text = await tool.handler(image_path=str(img))
    assert "未配置视觉模型" in json.loads(text)["error"]
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_read_image_upstream_error(monkeypatch, tmp_path):
    """上游返回错误时回传 error 文本。"""
    async def fake_forward(body, client, **kwargs):
        return {"status_code": 400, "body": "{}", "error": "上游拒绝"}

    app = FastAPI()
    app.state.http_client = FakePool()
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    monkeypatch.setattr(tools_module, "load_config", lambda: {"image_supported_models": "gpt-4o", "agent_vision_model": "gpt-4o"})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _read_tool(app)
    text = await tool.handler(image_path=str(img))
    assert json.loads(text) == {"error": "上游拒绝"}


@pytest.mark.asyncio
async def test_read_image_exception(monkeypatch, tmp_path):
    """forward_request 抛异常时返回结构化错误。"""
    async def fake_forward(body, client, **kwargs):
        raise RuntimeError("boom")

    app = FastAPI()
    app.state.http_client = FakePool()
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    monkeypatch.setattr(tools_module, "load_config", lambda: {"image_supported_models": "gpt-4o", "agent_vision_model": "gpt-4o"})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _read_tool(app)
    text = await tool.handler(image_path=str(img))
    assert "boom" in json.loads(text)["error"]


@pytest.mark.asyncio
async def test_read_image_empty_response_text(monkeypatch, tmp_path):
    """上游成功但内容为空时返回错误而不是空描述。"""
    async def fake_forward(body, client, **kwargs):
        return {"status_code": 200, "body": json.dumps({"choices": [{"message": {"content": ""}}]}), "error": ""}

    app = FastAPI()
    app.state.http_client = FakePool()
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    monkeypatch.setattr(tools_module, "load_config", lambda: {"image_supported_models": "gpt-4o", "agent_vision_model": "gpt-4o"})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _read_tool(app)
    text = await tool.handler(image_path=str(img))
    assert "未返回文本内容" in json.loads(text)["error"]


@pytest.mark.asyncio
async def test_read_image_without_pool(monkeypatch, tmp_path):
    """连接池缺失时返回结构化错误而不是抛出异常。"""
    app = FastAPI()
    app.state.http_client = None
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    monkeypatch.setattr(tools_module, "load_config", lambda: {"image_supported_models": "gpt-4o", "agent_vision_model": "gpt-4o"})
    tool = _read_tool(app)
    text = await tool.handler(image_path=str(img))
    assert "连接池未就绪" in json.loads(text)["error"]