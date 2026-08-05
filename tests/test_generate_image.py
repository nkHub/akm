"""akm_generate_image / akm_edit_image 内置工具的测试。"""

import json

import pytest
from fastapi import FastAPI

from akm.agent_runtime.loop import ToolDef
from akm.agent_runtime import tools as tools_module
from akm.agent_runtime.tools import build_builtin_tools


class FakeResp:
    """模拟 httpx 图片下载响应。"""

    def __init__(self, content, content_type):
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        pass


class FakeImageClient:
    """模拟可下载图片的连接池 client。"""

    def __init__(self, content=b"\x89PNG\r\n\x1a\ngenerated", content_type="image/png"):
        self.content = content
        self.content_type = content_type

    async def get(self, url):
        return FakeResp(self.content, self.content_type)


class FakePool:
    """记录 get_client 参数并返回假 client 的连接池。"""

    is_route_pool = True

    def __init__(self, client: object = "fake-client"):
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
async def test_generate_image_success(monkeypatch, tmp_path):
    """成功时返回 URL 列表并落盘到上传目录，附 local_path 与 http_url。"""
    from pathlib import Path

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
    app.state.http_client = FakePool(client=FakeImageClient())
    upload_dir = tmp_path / "uploads"

    def fake_config():
        return {
            "image_supported_models": "dall-e-3",
            "agent_upload_dir": str(upload_dir),
            "server_port": 8800,
        }

    monkeypatch.setattr(tools_module, "load_config", fake_config)
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _image_tool(app)
    text = await tool.handler(prompt="a red apple", size="1024x1024", quality="hd", n=2)

    result = json.loads(text)
    assert result["images"][0]["url"] == "https://img/1.png"
    assert result["images"][1]["url"] == "https://img/2.png"
    for entry in result["images"]:
        assert Path(entry["local_path"]).exists()
        assert entry["local_path"].startswith(str(upload_dir))
        assert entry["http_url"].startswith("http://127.0.0.1:8800/agent-uploads/")
        assert "save_error" not in entry
    assert isinstance(captured["client"], FakeImageClient)
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
async def test_generate_image_b64_fallback(monkeypatch, tmp_path):
    """上游只返回 b64_json 时解码落盘并给出长度提示而不是回传大字符串。"""
    from pathlib import Path

    async def fake_forward(body, client, **kwargs):
        return {
            "status_code": 200,
            "body": json.dumps({"data": [{"b64_json": "QUJD"}]}),
            "error": "",
        }

    app = FastAPI()
    app.state.http_client = FakePool()
    upload_dir = tmp_path / "uploads"

    def fake_config():
        return {"agent_upload_dir": str(upload_dir), "server_port": 8800}

    monkeypatch.setattr(tools_module, "load_config", fake_config)
    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _image_tool(app)
    text = await tool.handler(prompt="x")
    result = json.loads(text)
    assert result["images"][0]["b64_json_hint"] == "base64 数据，长度 4"
    assert "b64_json" not in result["images"][0]
    local_path = Path(result["images"][0]["local_path"])
    assert local_path.exists()
    assert local_path.read_bytes() == b"ABC"
    assert result["images"][0]["http_url"].startswith("http://127.0.0.1:8800/agent-uploads/")


def test_image_input_size_limit_rejects_before_reading(monkeypatch, tmp_path):
    """图片输入超过上限时应在读取或解码前拒绝，避免工具调用耗尽内存。"""
    monkeypatch.setattr(tools_module, "_AGENT_IMAGE_MAX_INPUT_BYTES", 3)
    image_path = tmp_path / "too-large.png"
    image_path.write_bytes(b"ABCD")

    with pytest.raises(ValueError, match="超过"):
        tools_module._read_image_file(str(image_path))
    with pytest.raises(ValueError, match="超过"):
        tools_module._decode_image_base64("QUJDRA==")


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


@pytest.mark.asyncio
async def test_generate_image_clamps_requested_count(monkeypatch, tmp_path):
    """图片数量必须限制在内置上限内，避免模型异常参数放大上游费用。"""
    captured = {}
    app = FastAPI()
    app.state.http_client = FakePool()
    monkeypatch.setattr(
        tools_module,
        "load_config",
        lambda: {"image_supported_models": "dall-e-3", "agent_upload_dir": str(tmp_path / "uploads")},
    )

    async def fake_forward(body, *_args, **_kwargs):
        captured.update(body)
        return {"status_code": 200, "body": json.dumps({"data": []}), "error": ""}

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    await _image_tool(app).handler(prompt="x", n=999)

    assert captured["n"] == 4


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
    assert tool.parameters["required"] == ["prompt"]
    assert tool.parameters["properties"]["mask_path"]["type"] == "string"
    assert tool.parameters["properties"]["image_base64"]["type"] == "string"
    assert tool.parameters["properties"]["mask_base64"]["type"] == "string"


@pytest.mark.asyncio
async def test_edit_image_success(edit_app, monkeypatch, tmp_path):
    """成功时返回 URL 列表并落盘，multipart 结构与 /v1/images/edits 一致。"""
    from pathlib import Path

    app, image_path = edit_app
    app.state.http_client = FakePool(client=FakeImageClient())
    upload_dir = tmp_path / "uploads"
    captured = {}

    def fake_config():
        return {
            "image_supported_models": "dall-e-3",
            "agent_upload_dir": str(upload_dir),
            "server_port": 8800,
        }

    monkeypatch.setattr(tools_module, "load_config", fake_config)

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

    result = json.loads(text)
    entry = result["images"][0]
    assert entry["url"] == "https://img/edited.png"
    assert Path(entry["local_path"]).exists()
    assert entry["http_url"].startswith("http://127.0.0.1:8800/agent-uploads/")
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
    from pathlib import Path

    app, image_path = edit_app
    app.state.http_client = FakePool(client=FakeImageClient())
    mask = tmp_path / "mask.png"
    mask.write_bytes(b"\x89PNGmask")
    captured = {}

    def fake_config():
        return {
            "image_supported_models": "dall-e-3",
            "agent_upload_dir": str(tmp_path / "uploads"),
            "server_port": 8800,
        }

    monkeypatch.setattr(tools_module, "load_config", fake_config)

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
    result = json.loads(text)
    assert result["images"][0]["url"] == "https://img/edited.png"
    assert Path(result["images"][0]["local_path"]).exists()
    files = captured["body"]["__akm_form_files__"]
    assert set(files.keys()) == {"image", "mask"}
    assert files["mask"][1] == b"\x89PNGmask"


@pytest.mark.asyncio
async def test_generate_image_save_failure_reports_error(monkeypatch, tmp_path):
    """下载/保存图片失败时不阻断主结果，附 save_error 说明。"""
    app = FastAPI()
    # 默认 FakePool client 是字符串，没有 get 方法，触发保存失败
    app.state.http_client = FakePool()
    monkeypatch.setattr(
        tools_module,
        "load_config",
        lambda: {"image_supported_models": "dall-e-3", "agent_upload_dir": str(tmp_path / "uploads")},
    )

    async def fake_forward(body, client, **kwargs):
        return {
            "status_code": 200,
            "body": json.dumps({"data": [{"url": "https://img/1.png"}]}),
            "error": "",
        }

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _image_tool(app)
    text = await tool.handler(prompt="x")
    entry = json.loads(text)["images"][0]
    assert entry["url"] == "https://img/1.png"
    assert "save_error" in entry
    assert "local_path" not in entry


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
async def test_edit_image_rejects_path_outside_workspace_and_upload_dir(monkeypatch, tmp_path):
    """通过 Agent 请求执行时，图片文件不得来自工作区与上传目录之外。"""
    from akm.agent_runtime.tools import reset_request_workspace_root, set_request_workspace_root

    app = FastAPI()
    app.state.http_client = FakePool()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    upload_dir = tmp_path / "uploads"
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNGoutside")
    called = {"count": 0}
    monkeypatch.setattr(
        tools_module,
        "load_config",
        lambda: {
            "image_supported_models": "dall-e-3",
            "agent_workspace_root": str(workspace),
            "agent_upload_dir": str(upload_dir),
        },
    )

    async def fake_forward(*_args, **_kwargs):
        called["count"] += 1
        return {"status_code": 200, "body": "{}", "error": ""}

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)
    token = set_request_workspace_root("")
    try:
        text = await _edit_tool(app).handler(image_path=str(outside), prompt="x")
    finally:
        reset_request_workspace_root(token)

    assert "图片路径必须位于工作区或 agent_upload_dir 内" in json.loads(text)["error"]
    assert called["count"] == 0


@pytest.mark.asyncio
async def test_edit_image_path_fallback_to_upload_dir(edit_app, monkeypatch, tmp_path):
    """image_path 不存在但文件名在 agent_upload_dir 时按文件名回退查找。

    模型常把 http_url 里的 /agent-uploads/ 前缀误当成本地路径（如
    /data/agent-uploads/xxx.png），此时只要文件名一致，回退逻辑应命中
    真实落盘文件。
    """
    from pathlib import Path

    app, _ = edit_app
    app.state.http_client = FakePool(client=FakeImageClient())
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nfallback")
    captured = {}

    def fake_config():
        return {
            "image_supported_models": "dall-e-3",
            "agent_upload_dir": str(upload_dir),
            "server_port": 8800,
        }

    monkeypatch.setattr(tools_module, "load_config", fake_config)

    async def fake_forward(body, client, **kwargs):
        captured["body"] = body
        return {
            "status_code": 200,
            "body": json.dumps({"data": [{"url": "https://img/edited.png"}]}),
            "error": "",
        }

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _edit_tool(app)
    text = await tool.handler(
        image_path="/data/agent-uploads/photo.png", prompt="make it blue"
    )
    result = json.loads(text)
    assert result["images"][0]["url"] == "https://img/edited.png"
    name, content, content_type = captured["body"]["__akm_form_files__"]["image"]
    assert name == "photo.png"
    assert content == b"\x89PNG\r\n\x1a\nfallback"
    assert content_type == "image/png"


@pytest.mark.asyncio
async def test_edit_image_path_fallback_ignores_wrong_directory(edit_app, monkeypatch, tmp_path):
    """回退查找丢弃传入路径的目录部分，只取文件名在 agent_upload_dir 内查找。"""
    from pathlib import Path

    app, _ = edit_app
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "photo.png").write_bytes(b"safe")
    called = {"n": 0}

    def fake_config():
        return {
            "image_supported_models": "dall-e-3",
            "agent_upload_dir": str(upload_dir),
            "server_port": 8800,
        }

    monkeypatch.setattr(tools_module, "load_config", fake_config)

    async def fake_forward(body, client, **kwargs):
        called["n"] += 1
        return {"status_code": 200, "body": "{}", "error": ""}

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    # 传入带错误目录的路径，回退后只取 basename=photo.png，命中 upload_dir 下的同名文件
    tool = _edit_tool(app)
    text = await tool.handler(image_path="/tmp/somewhere/else/photo.png", prompt="x")
    assert "error" not in json.loads(text)
    assert called["n"] == 1


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


@pytest.mark.asyncio
async def test_edit_image_base64_data_url(edit_app, monkeypatch, tmp_path):
    """传入带 data: 前缀的 base64 时直接解码为图片文件，无需本地文件。"""
    from pathlib import Path

    app, _ = edit_app
    app.state.http_client = FakePool(client=FakeImageClient())
    upload_dir = tmp_path / "uploads"
    captured = {}
    raw = b"\x89PNG\r\n\x1a\nfromb64"

    def fake_config():
        return {
            "image_supported_models": "dall-e-3",
            "agent_upload_dir": str(upload_dir),
            "server_port": 8800,
        }

    monkeypatch.setattr(tools_module, "load_config", fake_config)

    async def fake_forward(body, client, **kwargs):
        captured["body"] = body
        return {
            "status_code": 200,
            "body": json.dumps({"data": [{"url": "https://img/edited.png"}]}),
            "error": "",
        }

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    import base64 as _b64

    data_url = "data:image/png;base64," + _b64.b64encode(raw).decode("ascii")
    tool = _edit_tool(app)
    text = await tool.handler(image_base64=data_url, prompt="make it blue")

    result = json.loads(text)
    assert result["images"][0]["url"] == "https://img/edited.png"
    name, content, content_type = captured["body"]["__akm_form_files__"]["image"]
    assert content == raw
    assert content_type == "image/png"
    assert name.endswith(".png")
    assert Path(result["images"][0]["local_path"]).exists()


@pytest.mark.asyncio
async def test_edit_image_base64_bare(edit_app, monkeypatch, tmp_path):
    """传入无前缀的裸 base64 时按 image/png 处理。"""
    from pathlib import Path

    app, _ = edit_app
    app.state.http_client = FakePool(client=FakeImageClient())
    upload_dir = tmp_path / "uploads"
    captured = {}
    raw = b"PNG-bare"

    def fake_config():
        return {
            "image_supported_models": "dall-e-3",
            "agent_upload_dir": str(upload_dir),
            "server_port": 8800,
        }

    monkeypatch.setattr(tools_module, "load_config", fake_config)

    async def fake_forward(body, client, **kwargs):
        captured["body"] = body
        return {"status_code": 200, "body": json.dumps({"data": [{"url": "u"}]}), "error": ""}

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    import base64 as _b64

    tool = _edit_tool(app)
    text = await tool.handler(image_base64=_b64.b64encode(raw).decode("ascii"), prompt="x")

    assert "error" not in json.loads(text)
    name, content, content_type = captured["body"]["__akm_form_files__"]["image"]
    assert content == raw
    assert content_type == "image/png"
    assert name.endswith(".png")


@pytest.mark.asyncio
async def test_edit_image_base64_invalid(edit_app, monkeypatch):
    """base64 数据非法时返回结构化错误，不触发上游请求。"""
    app, _ = edit_app
    called = {"n": 0}

    async def fake_forward(body, client, **kwargs):
        called["n"] += 1
        return {"status_code": 200, "body": "{}", "error": ""}

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _edit_tool(app)
    text = await tool.handler(image_base64="not!!base64!!", prompt="x")
    assert "解码失败" in json.loads(text)["error"]
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_edit_image_no_source(edit_app, monkeypatch):
    """image_path 与 image_base64 都为空时返回结构化错误。"""
    app, _ = edit_app

    async def fake_forward(body, client, **kwargs):
        return {"status_code": 200, "body": "{}", "error": ""}

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)

    tool = _edit_tool(app)
    text = await tool.handler(prompt="x")
    assert "必须至少提供一个" in json.loads(text)["error"]
