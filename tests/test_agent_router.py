"""/v1/agent 端点的文件上传能力测试。"""

import base64
import json

import pytest
from httpx import ASGITransport, AsyncClient

from akm.agent_runtime.loop import AgentResult
from akm.server import app


class _FakeAgentLoop:
    """记录传入 messages 与 options 的假 Agent Loop，不做真实调用。"""

    def __init__(self):
        self.calls = []
        self.stream_options = []

    async def run(self, messages, **options):
        self.calls.append({"messages": messages, "options": options})
        return AgentResult(
            ok=True,
            final_message={"role": "assistant", "content": "done"},
            messages=messages,
        )

    async def run_stream(self, messages, **options):
        self.stream_options.append(options)
        # 模拟生成器：生成 10 个 model_delta 事件，逐条等待取消回调
        for i in range(10):
            yield f"data: {json.dumps({'event': 'model_delta', 'data': {'content': str(i)}})}\n\n"


@pytest.fixture(autouse=True)
def _mount_agent_loop():
    app.state.agent_loop = _FakeAgentLoop()
    yield
    app.state.agent_loop = None


@pytest.mark.asyncio
async def test_multipart_text_file_appended_as_user_message():
    """multipart 上传文本文件应作为独立 user 消息追加，内容可被读取。"""
    loop = app.state.agent_loop
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            data={"messages": json.dumps([{"role": "user", "content": "请分析"}]), "model": "gpt-4o"},
            files=[("files", ("note.txt", "hello world\n", "text/plain"))],
        )
    assert resp.status_code == 200
    assert loop.calls[0]["options"]["model"] == "gpt-4o"
    messages = loop.calls[0]["messages"]
    assert len(messages) == 2
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("用户上传了文件：note.txt")
    assert "hello world" in messages[1]["content"]


@pytest.mark.asyncio
async def test_multipart_image_appended_as_image_url(monkeypatch, tmp_path):
    """multipart 上传图片应转为 base64 data URL 的 image_url 内容块，并提示保存路径。"""
    # 隔离上传目录到临时目录，避免测试写入真实 ~/.akm/cache
    from akm.agent_runtime import router as router_module

    monkeypatch.setattr(router_module, "load_config", lambda: {"agent_upload_dir": str(tmp_path / "cache")})
    loop = app.state.agent_loop
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"fakedata"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            data={"messages": json.dumps([{"role": "user", "content": "看图"}]), "model": "gpt-4o"},
            files=[("files", ("pic.png", png_bytes, "image/png"))],
        )
    assert resp.status_code == 200
    messages = loop.calls[0]["messages"]
    assert len(messages) == 2
    content = messages[1]["content"]
    assert isinstance(content, list)
    expected_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    assert content[1] == {"type": "image_url", "image_url": {"url": expected_url}}
    # 文本提示应包含临时落盘路径，供 akm_edit_image 使用
    assert "图片已保存至：" in content[0]["text"]
    assert "akm_edit_image" in content[0]["text"]


def test_supports_vision_defaults_true(monkeypatch):
    """默认情况下模型应视为支持直接视觉（原生上传最优），不降级。"""
    from akm.agent_runtime.router import _supports_vision

    monkeypatch.setattr("akm.agent_runtime.router.load_config", lambda: {})
    assert _supports_vision("") is True
    assert _supports_vision("deepseek-v4-flash") is True
    assert _supports_vision("gpt-5.6-luna") is True


def test_supports_vision_no_vision_list_hit(monkeypatch):
    """命中 agent_no_vision_models 名单的模型应判定为不支持直接视觉。"""
    from akm.agent_runtime.router import _supports_vision

    monkeypatch.setattr(
        "akm.agent_runtime.router.load_config",
        lambda: {"agent_no_vision_models": "deepseek-v4-flash, deepseek-v4-pro"},
    )
    assert _supports_vision("deepseek-v4-flash") is False
    assert _supports_vision("deepseek-v4-pro") is False
    assert _supports_vision("gpt-5.6-luna") is True
    assert _supports_vision("") is True


@pytest.mark.asyncio
async def test_multipart_image_degraded_for_no_vision_model(monkeypatch, tmp_path):
    """命中 agent_no_vision_models 的模型：上传图片不塞入上下文，改为文本提示走 akm_read_image。"""
    from akm.agent_runtime import router as router_module

    monkeypatch.setattr(
        router_module, "load_config",
        lambda: {"agent_upload_dir": str(tmp_path / "cache"), "agent_no_vision_models": "deepseek-v4-flash"},
    )
    loop = app.state.agent_loop
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"fakedata"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            data={"messages": json.dumps([{"role": "user", "content": "看图"}]),
                  "model": "deepseek-v4-flash"},
            files=[("files", ("pic.png", png_bytes, "image/png"))],
        )
    assert resp.status_code == 200
    messages = loop.calls[0]["messages"]
    assert len(messages) == 2
    content = messages[1]["content"]
    # 降级：纯文本消息，不含 image_url 内容块
    assert isinstance(content, str)
    assert "不支持直接视觉输入" in content
    assert "akm_read_image" in content
    assert "image_path=" in content
    # 图片仍落盘，供 akm_read_image 读取
    import pathlib
    saved = [p for p in (tmp_path / "cache").iterdir() if p.suffix == ".png"]
    assert len(saved) == 1
    assert saved[0].read_bytes() == png_bytes


@pytest.mark.asyncio
async def test_uploaded_image_saved_to_akm_cache(monkeypatch, tmp_path):
    """上传的图片应真实落盘到默认的 ~/.akm/cache，且内容与扩展名正确。"""
    from pathlib import Path

    from akm.agent_runtime import router as router_module
    from akm.agent_runtime.router import _save_uploaded_image

    monkeypatch.setattr(
        router_module, "load_config", lambda: {"agent_upload_dir": str(tmp_path / "cache")}
    )

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"fakedata"
    path = Path(_save_uploaded_image(png_bytes, "image/png"))

    assert str(path).startswith(str(tmp_path / "cache"))
    assert path.suffix == ".png"
    assert path.read_bytes() == png_bytes


@pytest.mark.asyncio
async def test_uploaded_image_saved_to_default_dir_when_unconfigured(monkeypatch, tmp_path):
    """未配置 agent_upload_dir 时应回落到 ~/.akm/cache 默认目录。"""
    from pathlib import Path

    from akm.agent_runtime import router as router_module
    from akm.agent_runtime.router import _save_uploaded_image

    monkeypatch.setattr(router_module, "load_config", lambda: {})
    # 默认目录位于用户目录；测试必须使用临时 HOME，避免向开发机真实缓存目录落盘。
    monkeypatch.setenv("HOME", str(tmp_path))

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"fakedata"
    path = Path(_save_uploaded_image(png_bytes, "image/png"))

    try:
        assert str(path).startswith(str(Path.home() / ".akm" / "cache"))
    finally:
        # 回落默认目录会写到真实家目录，测试后清理，避免污染用户环境
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_multipart_multiple_files_all_appended():
    """多个文件应全部读取，并各追加一条 user 消息。"""
    loop = app.state.agent_loop
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            data={"messages": json.dumps([{"role": "user", "content": "hi"}]), "model": "gpt-4o"},
            files=[
                ("files", ("a.txt", "alpha", "text/plain")),
                ("files", ("b.txt", "beta", "text/plain")),
            ],
        )
    assert resp.status_code == 200
    messages = loop.calls[0]["messages"]
    assert len(messages) == 3
    assert "alpha" in messages[1]["content"]
    assert "beta" in messages[2]["content"]


@pytest.mark.asyncio
async def test_multipart_files_reject_total_size_over_limit(monkeypatch):
    """附件在读取和 Base64 编码前必须拒绝超过总量上限的输入。"""
    from akm.agent_runtime import router as router_module

    monkeypatch.setattr(router_module, "_AGENT_UPLOAD_MAX_BYTES", 3)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            data={"messages": json.dumps([{"role": "user", "content": "hi"}])},
            files=[("files", ("large.txt", b"1234", "text/plain"))],
        )

    assert resp.status_code == 400
    assert "总大小" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_multipart_binary_file_rejected():
    """无法按 UTF-8 解码的二进制文件应返回 400。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            data={"messages": json.dumps([{"role": "user", "content": "hi"}]), "model": "gpt-4o"},
            files=[("files", ("data.bin", b"\x00\x01\xff\xfe", "application/octet-stream"))],
        )
    assert resp.status_code == 400
    assert "不支持的文件类型" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_multipart_missing_messages_rejected():
    """multipart 缺少 messages 字段应返回 400。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            data={"model": "gpt-4o"},
            files=[("files", ("a.txt", "alpha", "text/plain"))],
        )
    assert resp.status_code == 400
    assert "缺少 messages 参数" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_multipart_invalid_messages_json_rejected():
    """multipart 中 messages 不是合法 JSON 时应返回 400。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            data={"messages": "not-json", "model": "gpt-4o"},
            files=[("files", ("a.txt", "alpha", "text/plain"))],
        )
    assert resp.status_code == 400
    assert "messages 必须是合法的 JSON 字符串" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_plain_json_path_still_works():
    """纯 JSON 请求路径保持原有行为，不受文件上传改造影响。"""
    loop = app.state.agent_loop
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}],
            },
        )
    assert resp.status_code == 200
    assert len(loop.calls[0]["messages"]) == 1
    assert loop.calls[0]["options"]["tools"][0]["function"]["name"] == "f"


@pytest.mark.asyncio
async def test_default_instructions_used_when_not_provided(monkeypatch):
    """客户端未传 instructions 时应回填 config.json 的 agent_default_instructions。"""
    from akm.agent_runtime import router as router_module

    default_instructions = "数学公式请使用 KaTeX 语法返回"
    monkeypatch.setattr(
        router_module, "load_config", lambda: {"agent_default_instructions": default_instructions}
    )
    loop = app.state.agent_loop
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert loop.calls[0]["options"]["instructions"] == default_instructions


@pytest.mark.asyncio
async def test_custom_instructions_preserved(monkeypatch):
    """客户端传入 instructions 时优先使用客户端的，不回填默认值。"""
    from akm.agent_runtime import router as router_module

    monkeypatch.setattr(
        router_module, "load_config", lambda: {"agent_default_instructions": "默认指令"}
    )
    loop = app.state.agent_loop
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
                "instructions": "自定义指令",
            },
        )
    assert resp.status_code == 200
    assert loop.calls[0]["options"]["instructions"] == "自定义指令"


# ── Agent 可选鉴权 ──


class _FakeRequest:
    """模拟带请求头的 Request 对象（仅暴露 _check_agent_auth 需要的字段）。

    headers 使用普通 dict；测试中键统一用小写，与 starlette Headers
    大小写不敏感行为对齐。
    """

    def __init__(self, headers=None):
        self.headers = headers or {}


@pytest.mark.asyncio
async def test_auth_skipped_when_token_unconfigured(monkeypatch):
    """未配置 agent_api_token 时鉴权应直接放行。"""
    from akm.agent_runtime.router import _check_agent_auth

    monkeypatch.setattr("akm.agent_runtime.router.load_config", lambda: {})
    request = _FakeRequest(headers={})
    assert await _check_agent_auth(request) is None


@pytest.mark.asyncio
async def test_auth_allows_dangerous_tools_without_token(monkeypatch):
    """写/shell/git 已开启但未配置 agent_api_token 时仍放行（token 可选）。"""
    from akm.agent_runtime.router import _check_agent_auth

    monkeypatch.setattr(
        "akm.agent_runtime.router.load_config",
        lambda: {
            "agent_write_tools_enabled": True,
            "agent_run_shell_enabled": True,
            "agent_git_enabled": True,
            "agent_api_token": "",
        },
    )

    assert await _check_agent_auth(_FakeRequest(headers={})) is None


@pytest.mark.asyncio
async def test_auth_rejects_missing_token(monkeypatch):
    """配置了 token 但请求未携带时应返回 401。"""
    from akm.agent_runtime.router import _check_agent_auth

    monkeypatch.setattr(
        "akm.agent_runtime.router.load_config", lambda: {"agent_api_token": "secret123"}
    )
    request = _FakeRequest(headers={})
    resp = await _check_agent_auth(request)
    assert resp is not None
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_rejects_wrong_token(monkeypatch):
    """配置了 token 但请求携带错误 token 时应返回 401。"""
    from akm.agent_runtime.router import _check_agent_auth

    monkeypatch.setattr(
        "akm.agent_runtime.router.load_config", lambda: {"agent_api_token": "secret123"}
    )
    request = _FakeRequest(headers={"authorization": "Bearer wrong"})
    resp = await _check_agent_auth(request)
    assert resp is not None
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_accepts_bearer_token(monkeypatch):
    """配置了 token 且请求携带正确 Bearer token 时应放行。"""
    from akm.agent_runtime.router import _check_agent_auth

    monkeypatch.setattr(
        "akm.agent_runtime.router.load_config", lambda: {"agent_api_token": "secret123"}
    )
    request = _FakeRequest(headers={"authorization": "Bearer secret123"})
    assert await _check_agent_auth(request) is None


@pytest.mark.asyncio
async def test_auth_accepts_x_agent_token_header(monkeypatch):
    """配置了 token 且请求通过 X-Agent-Token 头携带时应放行。"""
    from akm.agent_runtime.router import _check_agent_auth

    monkeypatch.setattr(
        "akm.agent_runtime.router.load_config", lambda: {"agent_api_token": "secret123"}
    )
    request = _FakeRequest(headers={"x-agent-token": "secret123"})
    assert await _check_agent_auth(request) is None


@pytest.mark.asyncio
async def test_auth_rejects_endpoint_request_without_token(monkeypatch):
    """端点级集成：配置 token 后无 token 请求应返回 401。"""
    monkeypatch.setattr(
        "akm.agent_runtime.router.load_config", lambda: {"agent_api_token": "secret123"}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 401
    assert "未授权" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_auth_accepts_endpoint_request_with_token(monkeypatch):
    """端点级集成：配置 token 后携带正确 Bearer token 应正常执行。"""
    monkeypatch.setattr(
        "akm.agent_runtime.router.load_config", lambda: {"agent_api_token": "secret123"}
    )
    loop = app.state.agent_loop
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer secret123"},
        )
    assert resp.status_code == 200
    assert len(loop.calls[0]["messages"]) == 1


def test_render_default_instructions_replaces_placeholders():
    """默认指令中的占位符应按运行时路径替换。"""
    from akm.agent_runtime.router import _render_default_instructions

    sample = (
        "{AKM_SOURCE_DIR} | {CURRENT_WORKING_DIRECTORY} | "
        "{USER_AGENTS_MD_PATH} | {USER_AGENTS_SKILLS_DIR} | {USER_PI_NPM_DIR}"
    )
    out = _render_default_instructions(sample, "/tmp/myws")

    # AKM 源码根：akm/__init__.py 的父目录的父目录
    import akm
    from pathlib import Path as P

    assert str(P(akm.__file__).resolve().parent.parent) in out
    # 请求级工作区优先
    assert "/tmp/myws" in out
    # 本机用户路径（存在才替换，不存在替换为空）
    home = P.home()
    if (home / ".config/opencode/AGENTS.md").exists():
        assert str(home / ".config/opencode/AGENTS.md") in out
    else:
        assert out.startswith("{USER_AGENTS_MD_PATH}")
    # 不存在的用户目录替换为空字符串
    assert "  | {USER_PI_NPM_DIR}" not in out


def test_render_default_instructions_empty_returns_empty():
    """空指令原样返回。"""
    from akm.agent_runtime.router import _render_default_instructions

    assert _render_default_instructions("", "/tmp") == ""


def test_render_default_instructions_os_getcwd_failure_falls_back_home(monkeypatch):
    """工作目录被删除（os.getcwd 抛 OSError）时回退到用户主目录，不抛异常。"""
    from pathlib import Path

    import os as _os

    from akm.agent_runtime.router import _render_default_instructions

    def _boom():
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(_os, "getcwd", _boom)
    out = _render_default_instructions("{CURRENT_WORKING_DIRECTORY}", "")
    assert out == str(Path.home())


@pytest.mark.asyncio
async def test_default_instructions_placeholder_replaced_in_request(monkeypatch):
    """未传 instructions 时回填默认指令，且占位符在注入前被替换。"""
    monkeypatch.setattr(
        "akm.agent_runtime.router.load_config",
        lambda: {"agent_default_instructions": "源码 {AKM_SOURCE_DIR}，工作区 {CURRENT_WORKING_DIRECTORY}"},
    )
    loop = app.state.agent_loop
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
                "workspace_root": "/tmp/ws",
            },
        )
    assert resp.status_code == 200
    options = loop.calls[0]["options"]
    import akm
    from pathlib import Path as P

    assert str(P(akm.__file__).resolve().parent.parent) in options["instructions"]
    assert "/tmp/ws" in options["instructions"]
    assert "{AKM_SOURCE_DIR}" not in options["instructions"]
    assert "{CURRENT_WORKING_DIRECTORY}" not in options["instructions"]


@pytest.mark.asyncio
async def test_stream_passes_cancel_check_to_run_stream():
    """流式请求应将断连检测回调作为 cancel_check 传给 run_stream。"""
    loop = app.state.agent_loop
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/agent",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            # 读完整个流，验证 cancel_check 已注入且可调用（正常结束时为 False）
            chunks = [chunk async for chunk in resp.aiter_bytes()]

    assert len(loop.stream_options) == 1
    cancel_check = loop.stream_options[0].get("cancel_check")
    assert cancel_check is not None
    assert callable(cancel_check)
    # 连接未断开时回调返回 False
    assert cancel_check() is False
    # 收到了流式内容
    assert chunks


# ── /v1/agent/compact 手动压缩 ──


class _FakeCompactingLoop(_FakeAgentLoop):
    """支持 compact_messages 的假 Agent Loop，记录调用参数并返回固定结果。"""

    def __init__(self):
        super().__init__()
        self.compact_calls = []

    async def compact_messages(self, messages, *, model="", api_path="chat/completions"):
        self.compact_calls.append({"messages": messages, "model": model, "api_path": api_path})
        return {
            "ok": True,
            "messages": [{"role": "system", "content": "以下是对较早对话历史的摘要：\n摘要内容"}, {"role": "user", "content": "最近一条"}],
            "summary": "摘要内容",
            "removed_count": len(messages) - 1,
            "before_count": len(messages),
            "after_count": 2,
            "estimated_tokens": 100,
        }


@pytest.mark.asyncio
async def test_compact_endpoint_proxies_to_loop():
    """POST /v1/agent/compact 应把 messages/model 交给 AgentLoop.compact_messages，并原样返回结果。"""
    # 覆盖 autouse fixture 安装的普通 FakeLoop，换成支持压缩的版本
    app.state.agent_loop = _FakeCompactingLoop()
    loop = app.state.agent_loop
    messages = [
        {"role": "user", "content": "较早的问题"},
        {"role": "assistant", "content": "较早的回答"},
        {"role": "user", "content": "最近的问题"},
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent/compact",
            json={"model": "gpt-4o", "messages": messages},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["summary"] == "摘要内容"
    assert data["removed_count"] == len(messages) - 1
    assert len(loop.compact_calls) == 1
    assert loop.compact_calls[0]["model"] == "gpt-4o"
    assert loop.compact_calls[0]["api_path"] == "chat/completions"
    assert loop.compact_calls[0]["messages"] == messages


@pytest.mark.asyncio
async def test_compact_endpoint_requires_messages():
    """缺少 messages 字段应返回 400。"""
    app.state.agent_loop = _FakeCompactingLoop()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/agent/compact", json={"model": "gpt-4o"})
    assert resp.status_code == 400
    assert "messages" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_compact_endpoint_rejects_invalid_json():
    """非 JSON 请求体应返回 400。"""
    app.state.agent_loop = _FakeCompactingLoop()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent/compact",
            content="not-json",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    assert "JSON" in resp.json()["detail"] or "JSON" in json.dumps(resp.json())


@pytest.mark.asyncio
async def test_compact_endpoint_respects_auth(monkeypatch):
    """配置 token 后无 token 请求应返回 401。"""
    monkeypatch.setattr(
        "akm.agent_runtime.router.load_config", lambda: {"agent_api_token": "secret123"}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent/compact",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 401
    assert "未授权" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_compact_endpoint_503_when_loop_missing():
    """Agent Loop 未初始化时应返回 503。"""
    app.state.agent_loop = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent/compact",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 503
    assert "未初始化" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_compact_endpoint_501_when_loop_unsupported():
    """Agent Loop 不支持 compact_messages 时应返回 501。"""
    # autouse fixture 的 _FakeAgentLoop 没有 compact_messages
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent/compact",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 501
    assert "不支持手动压缩" in resp.json()["detail"]
