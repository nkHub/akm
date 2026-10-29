"""网页抓取工具（akm_fetch_url）的边界测试。

覆盖四层：
1. 开关：默认不注册，`agent_fetch_url_enabled=true` 才出现；
2. HTML → 纯文本：标题、去脚本/样式、段落换行；
3. 截断与内容类型：max_chars 截断、纯文本直出、二进制类型拒绝；
4. SSRF 防护：内网/回环/云元数据地址、非 http(s)、非标准端口、重定向到内网都要拦住。
"""

from __future__ import annotations

import ipaddress
import json

import pytest
from fastapi import FastAPI

from akm.agent_runtime import tools as tools_module
from akm.agent_runtime.tools import build_builtin_tools


def _config(**overrides) -> dict:
    """构造工具能用的最小配置（默认开启抓取、10s、2MB）。"""
    base = {
        "agent_fetch_url_enabled": True,
        "agent_fetch_url_timeout_sec": 10.0,
        "agent_fetch_url_max_bytes": 2 * 1024 * 1024,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """不联网：IP 字面量返回自身，localhost 走回环，其它域名统一解析成公网地址。"""

    async def fake_resolve(host: str, port: int) -> list[str]:
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        # localhost 类主机名按真实语义解析到回环地址（要被拦）
        if host.lower().startswith("localhost"):
            return ["127.0.0.1"]
        return ["93.184.216.34"]

    monkeypatch.setattr(tools_module, "_resolve_host_ips", fake_resolve)


def _stub_fetch(monkeypatch, responses: list[tuple[int, dict, bytes]]):
    """按顺序返回预设响应，记录每次请求的 URL。"""
    calls: list[str] = []

    async def fake_fetch(url: str, *, timeout: float, max_bytes: int):
        calls.append(url)
        if not responses:
            raise AssertionError(f"多余的一次抓取: {url}")
        status, headers, body = responses.pop(0)
        return status, headers, body, url

    monkeypatch.setattr(tools_module, "_fetch_once", fake_fetch)
    return calls


def _fetch_handler(app):
    """从 build_builtin_tools 里取出 akm_fetch_url 的 handler。"""
    for tool in build_builtin_tools(app):
        if tool.name == "akm_fetch_url":
            return tool.handler
    raise AssertionError("akm_fetch_url 未注册，检查 agent_fetch_url_enabled")


# ── 1. 开关 ──────────────────────────────────────────────────────────────────


def test_tool_registered_only_when_enabled(monkeypatch):
    monkeypatch.setattr(tools_module, "load_config", lambda: {"agent_fetch_url_enabled": False})
    assert "akm_fetch_url" not in [t.name for t in build_builtin_tools(FastAPI())]

    monkeypatch.setattr(tools_module, "load_config", lambda: {"agent_fetch_url_enabled": True})
    assert "akm_fetch_url" in [t.name for t in build_builtin_tools(FastAPI())]


@pytest.mark.asyncio
async def test_fetch_reports_disabled(monkeypatch):
    """即使 handler 被直接调用，开关关闭时也返回可读错误而不是发请求。"""
    monkeypatch.setattr(tools_module, "load_config", lambda: {"agent_fetch_url_enabled": False})
    out = json.loads(await tools_module.fetch_url_text("http://example.com/"))
    assert "未启用" in out["error"]


# ── 2. HTML → 纯文本 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_converts_html_to_text(monkeypatch):
    monkeypatch.setattr(tools_module, "load_config", _config)
    html = (
        "<html><head><title>示例标题</title>"
        "<style>.a{color:red}</style><script>var secret = 1;</script></head>"
        "<body><h1>大标题</h1><p>第一段</p><p>第二段</p></body></html>"
    )
    _stub_fetch(monkeypatch, [(200, {"content-type": "text/html; charset=utf-8"}, html.encode())])

    out = json.loads(await tools_module.fetch_url_text("http://example.com/page"))
    assert out["status"] == 200
    assert out["title"] == "示例标题"
    assert "大标题" in out["text"]
    assert "第一段" in out["text"] and "第二段" in out["text"]
    # 脚本与样式内容必须被剥掉
    assert "secret" not in out["text"]
    assert "color:red" not in out["text"]
    # 外部内容提示必须在
    assert out["notice"]


@pytest.mark.asyncio
async def test_fetch_plain_text_passthrough(monkeypatch):
    monkeypatch.setattr(tools_module, "load_config", _config)
    _stub_fetch(monkeypatch, [(200, {"content-type": "text/plain; charset=utf-8"}, b"line1\nline2")])
    out = json.loads(await tools_module.fetch_url_text("http://example.com/a.txt"))
    assert out["text"] == "line1\nline2"
    assert out["title"] == ""


@pytest.mark.asyncio
async def test_fetch_rejects_binary_content_type(monkeypatch):
    monkeypatch.setattr(tools_module, "load_config", _config)
    _stub_fetch(monkeypatch, [(200, {"content-type": "image/png"}, b"\x89PNG\r\n")])
    out = json.loads(await tools_module.fetch_url_text("http://example.com/a.png"))
    assert "不支持的内容类型" in out["error"]


# ── 3. 截断 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_truncates_text_by_max_chars(monkeypatch):
    monkeypatch.setattr(tools_module, "load_config", _config)
    long_text = "<p>" + "字" * 5000 + "</p>"
    _stub_fetch(monkeypatch, [(200, {"content-type": "text/html"}, long_text.encode())])

    out = json.loads(await tools_module.fetch_url_text("http://example.com/big", max_chars=800))
    assert len(out["text"]) == 800
    assert out["truncated"] is True


@pytest.mark.asyncio
async def test_fetch_truncates_body_by_max_bytes(monkeypatch):
    """下载上限生效：正文超过 max_bytes 时截断并标记 truncated。"""
    monkeypatch.setattr(
        tools_module, "load_config", lambda: _config(agent_fetch_url_max_bytes=4096)
    )
    body = b"<html><body><p>" + b"a" * 10000 + b"</p></body></html>"
    _stub_fetch(monkeypatch, [(200, {"content-type": "text/html"}, body[:4096])])

    out = json.loads(await tools_module.fetch_url_text("http://example.com/huge"))
    assert out["truncated"] is True
    assert out["bytes"] <= 4096


# ── 4. SSRF 防护 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://192.168.1.10/admin",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",  # 云元数据
        "http://[::1]/",
    ],
)
async def test_fetch_blocks_internal_addresses(monkeypatch, url):
    """内网 / 回环 / 链路本地一律拒绝，且在发请求之前就拒绝。"""
    monkeypatch.setattr(tools_module, "load_config", _config)
    calls = _stub_fetch(monkeypatch, [])

    out = json.loads(await tools_module.fetch_url_text(url))
    assert "error" in out
    assert calls == []  # 关键：没有发生任何真实抓取


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/a", "javascript:alert(1)"])
async def test_fetch_rejects_non_http_scheme(monkeypatch, url):
    monkeypatch.setattr(tools_module, "load_config", _config)
    out = json.loads(await tools_module.fetch_url_text(url))
    assert "http" in out["error"].lower()


@pytest.mark.asyncio
async def test_fetch_rejects_unusual_port(monkeypatch):
    """本机服务的 8800 端口这种写法必须被拦下。"""
    monkeypatch.setattr(tools_module, "load_config", _config)
    out = json.loads(await tools_module.fetch_url_text("http://example.com:8800/"))
    assert "端口" in out["error"]


@pytest.mark.asyncio
async def test_fetch_rejects_credentials_in_url(monkeypatch):
    monkeypatch.setattr(tools_module, "load_config", _config)
    out = json.loads(await tools_module.fetch_url_text("http://user:pass@example.com/"))
    assert "用户名" in out["error"] or "密码" in out["error"]


@pytest.mark.asyncio
async def test_fetch_follows_safe_redirect(monkeypatch):
    monkeypatch.setattr(tools_module, "load_config", _config)
    calls = _stub_fetch(monkeypatch, [
        (302, {"location": "https://example.org/final"}, b""),
        (200, {"content-type": "text/html"}, b"<p>ok</p>"),
    ])
    out = json.loads(await tools_module.fetch_url_text("http://example.com/start"))
    assert out["status"] == 200
    assert "ok" in out["text"]
    assert calls == ["http://example.com/start", "https://example.org/final"]


@pytest.mark.asyncio
async def test_fetch_rejects_redirect_to_internal(monkeypatch):
    """302 到 127.0.0.1 必须在跟随前拦住——这是最容易绕过 SSRF 校验的路径。"""
    monkeypatch.setattr(tools_module, "load_config", _config)
    calls = _stub_fetch(monkeypatch, [
        (302, {"location": "http://127.0.0.1:8800/v1/agent"}, b""),
    ])
    out = json.loads(await tools_module.fetch_url_text("http://example.com/start"))
    assert "重定向被拒绝" in out["error"]
    assert len(calls) == 1  # 只发了第一跳


@pytest.mark.asyncio
async def test_fetch_stops_after_too_many_redirects(monkeypatch):
    monkeypatch.setattr(tools_module, "load_config", _config)
    _stub_fetch(monkeypatch, [
        (302, {"location": f"https://example.org/{i}"}, b"") for i in range(10)
    ])
    out = json.loads(await tools_module.fetch_url_text("http://example.com/start"))
    assert "重定向" in out["error"]


# ── 5. 审计 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_writes_tool_audit(monkeypatch):
    """抓取是一次工具调用：审计里 provider 取真实供应商（无上下文时留空）、工具名进 x-akm-tool、正文不进库。"""
    audits = []

    async def fake_submit(app, data):
        audits.append(data)

    monkeypatch.setattr("akm.server._submit_audit_log", fake_submit)
    monkeypatch.setattr(tools_module, "load_config", _config)
    _stub_fetch(monkeypatch, [(200, {"content-type": "text/html"}, b"<title>T</title><p>hi</p>")])

    handler = _fetch_handler(FastAPI())
    await handler(url="http://example.com/")

    assert len(audits) == 1
    record = audits[0]
    # 没有 AgentLoop 注入审计上下文时：provider 留空，绝不写死应用名 akm
    assert record["provider"] == ""
    assert '"x-akm-tool": "akm_fetch_url"' in record["request_headers"]
    # 正文不进审计，只留状态摘要
    assert "hi" not in record["response_body"]
