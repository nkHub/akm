"""akm_generate_image / akm_edit_image 内置工具的测试。"""

import json

import pytest
from fastapi import FastAPI

from akm.agent_runtime.loop import ToolDef
from akm.agent_runtime import tools as tools_module
from akm.agent_runtime.tools import build_builtin_tools


class FakePool:
    """记录 get_client 参数并返回假 client 的连接池。"""

    is_route_pool = True

    def __init__(self, client="fake-client"):
        self.calls = []
        self.client = client

    async def get_client(self, **kwargs):
        self.calls.append(kwargs)
        return self.client


@pytest.fixture
def image_app(monkeypatch):
    """构造绑定 FakePool 的 app，并固定图片模型配置。"""
    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(
        tools_module, "load_config", lambda: {"image_supported_models": "dall-e-3, gpt-image-2"}
    )
    return app


def _image_tool(app):
    tools = build_builtin_tools(app)
    return next(tool for tool in tools if tool.name == "akm_generate_image")


# ── 注册 ──


def test_builtin_tools_register_generate_image(image_app):
    tool = _image_tool(image_app)
    assert tool.parameters["required"] == ["prompt"]
    assert tool.parameters["properties"]["model"]["type"] == "string"
    assert tool.parameters["properties"]["n"]["type"] == "integer"


# ── 成功路径 ──


@pytest.mark.asyncio
async def test_generate_image_success(monkeypatch):
    """成功时返回 URL 列表，model 未指定时回填配置首项。"""
    captured = {}

    async def fake_forward(body, client, **kwargs):
        captured["body"] = body
        captured["client"] = client
        captured["kwargs"] = kwargs
        return {
            "status_code": 200,
            "body": json.dumps({"data": [{"url": "https://img/1.png"}, {"url": "https://img/2.png"}]}),
            "error": "",
        }

    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(
        tools_module, "load_config", lambda: {"image_supported_models": "dall-e-3"}
    )
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _image_tool(app)
    text = await tool.handler(prompt="a red apple", size="1024x1024", quality="hd", n=2)

    result = json.loads(text)
    assert result == {
        "images": [
            {"index": 0, "url": "https://img/1.png"},
            {"index": 1, "url": "https://img/2.png"},
        ]
    }
    assert captured["client"] == "fake-client"
    assert captured["body"]["model"] == "dall-e-3"
    assert captured["body"]["prompt"] == "a red apple"
    assert captured["body"]["n"] == 2
    assert captured["kwargs"]["api_path"] == "images/generations"
    assert isinstance(captured["kwargs"]["request_timeout"], float)


@pytest.mark.asyncio
async def test_generate_image_defaults_model_and_omits_empty(monkeypatch):
    """空的可选参数不进入 body，n 默认 1 且不附加。"""
    captured = {}

    async def fake_forward(body, client, **kwargs):
        captured["body"] = body
        return {"status_code": 200, "body": json.dumps({"data": [{"url": "https://img/1.png"}]}), "error": ""}

    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(tools_module, "load_config", lambda: {"image_supported_models": "gpt-image-2"})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _image_tool(app)
    text = await tool.handler(prompt="cat")
    assert captured["body"] == {"model": "gpt-image-2", "prompt": "cat"}


@pytest.mark.asyncio
async def test_generate_image_b64_fallback(monkeypatch):
    """上游只返回 b64_json 时给出长度提示而不是回传大字符串。"""
    async def fake_forward(body, client, **kwargs):
        return {
            "status_code": 200,
            "body": json.dumps({"data": [{"b64_json": "QUJD"}]}),
            "error": "",
        }

    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(tools_module, "load_config", lambda: {})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _image_tool(app)
    text = await tool.handler(prompt="x")
    assert json.loads(text) == {
        "images": [{"index": 0, "b64_json_hint": "base64 数据，长度 4"}]
    }


# ── 失败路径 ──


@pytest.mark.asyncio
async def test_generate_image_upstream_error(monkeypatch):
    """上游返回错误时回传 error 文本。"""
    async def fake_forward(body, client, **kwargs):
        return {
            "status_code": 400,
            "body": json.dumps({"error": {"message": "bad prompt"}}),
            "error": "bad prompt",
        }

    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(tools_module, "load_config", lambda: {})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _image_tool(app)
    text = await tool.handler(prompt="x")
    assert json.loads(text) == {"error": "bad prompt"}


@pytest.mark.asyncio
async def test_generate_image_exception(monkeypatch):
    """forward_request 抛异常时返回结构化错误。"""
    async def fake_forward(body, client, **kwargs):
        raise RuntimeError("boom")

    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(tools_module, "load_config", lambda: {})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _image_tool(app)
    text = await tool.handler(prompt="x")
    assert "boom" in json.loads(text)["error"]


@pytest.mark.asyncio
async def test_generate_image_invalid_json_body(monkeypatch):
    """上游 body 不是合法 JSON 时返回结构化错误。"""
    async def fake_forward(body, client, **kwargs):
        return {"status_code": 200, "body": "not json", "error": ""}

    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(tools_module, "load_config", lambda: {})
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _image_tool(app)
    text = await tool.handler(prompt="x")
    assert "JSON" in json.loads(text)["error"]


@pytest.mark.asyncio
async def test_generate_image_without_pool():
    """连接池缺失时返回结构化错误而不是抛出异常。"""
    app = FastAPI()
    app.state.http_client = None
    tool = _image_tool(app)
    text = await tool.handler(prompt="x")
    assert "连接池未就绪" in text


# ── akm_edit_image ──


@pytest.fixture
def edit_app(monkeypatch, tmp_path):
    """构造绑定 FakePool 的 app，并创建测试图片文件。"""
    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(
        tools_module, "load_config", lambda: {"image_supported_models": "dall-e-3"}
    )
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
    return app, str(img)


def _edit_tool(app):
    tools = build_builtin_tools(app)
    return next(tool for tool in tools if tool.name == "akm_edit_image")


def test_builtin_tools_register_edit_image(edit_app):
    app, _ = edit_app
    tool = _edit_tool(app)
    assert tool.parameters["required"] == ["image_path", "prompt"]
    assert tool.parameters["properties"]["mask_path"]["type"] == "string"


@pytest.mark.asyncio
async def test_edit_image_success(edit_app, monkeypatch):
    """成功时返回 URL 列表，multipart 结构与 /v1/images/edits 一致。"""
    app, image_path = edit_app
    captured = {}

    async def fake_forward(body, client, **kwargs):
        captured["body"] = body
        captured["kwargs"] = kwargs
        return {
            "status_code": 200,
            "body": json.dumps({"data": [{"url": "https://img/edited.png"}]}),
            "error": "",
        }

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _edit_tool(app)
    text = await tool.handler(image_path=image_path, prompt="make it blue")

    assert json.loads(text) == {"images": [{"index": 0, "url": "https://img/edited.png"}]}
    body = captured["body"]
    assert body["__akm_multipart__"] is True
    assert body["model"] == "dall-e-3"
    assert body["__akm_form_fields__"] == {"prompt": "make it blue", "model": "dall-e-3"}
    name, content, content_type = body["__akm_form_files__"]["image"]
    assert name == "photo.png"
    assert content == b"\x89PNG\r\n\x1a\nfakepng"
    assert content_type == "image/png"
    assert captured["kwargs"]["api_path"] == "images/edits"


@pytest.mark.asyncio
async def test_edit_image_with_mask(edit_app, monkeypatch, tmp_path):
    """提供 mask_path 时同时上传 image 与 mask 两个文件。"""
    app, image_path = edit_app
    mask = tmp_path / "mask.png"
    mask.write_bytes(b"\x89PNGmask")
    captured = {}

    async def fake_forward(body, client, **kwargs):
        captured["body"] = body
        return {
            "status_code": 200,
            "body": json.dumps({"data": [{"url": "https://img/edited.png"}]}),
            "error": "",
        }

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _edit_tool(app)
    text = await tool.handler(image_path=image_path, prompt="redraw", mask_path=str(mask))
    assert "edited.png" in text
    files = captured["body"]["__akm_form_files__"]
    assert set(files.keys()) == {"image", "mask"}
    assert files["mask"][1] == b"\x89PNGmask"


@pytest.mark.asyncio
async def test_edit_image_missing_file(edit_app, monkeypatch):
    """图片路径不存在时返回结构化错误，不触发上游请求。"""
    app, image_path = edit_app
    called = {"n": 0}

    async def fake_forward(body, client, **kwargs):
        called["n"] += 1
        return {"status_code": 200, "body": "{}", "error": ""}

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _edit_tool(app)
    text = await tool.handler(image_path="/nonexistent/nope.png", prompt="x")
    assert "不存在" in json.loads(text)["error"]
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_edit_image_exception(edit_app, monkeypatch):
    """forward_request 抛异常时返回结构化错误。"""
    app, image_path = edit_app

    async def fake_forward(body, client, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _edit_tool(app)
    text = await tool.handler(image_path=image_path, prompt="x")
    assert "boom" in json.loads(text)["error"]
