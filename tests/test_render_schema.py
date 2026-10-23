"""AG-UI 渲染工具（akm_render）的 schema 加载与工具执行测试。

覆盖三件事：
1. ``render_schema.json`` 是唯一真源 —— 工具名、块清单、最大块数都从它读取；
2. ``akm_render`` 执行时复用 ``layout_schema`` 校验：合法块返回归一化 layout，
   非法块返回 ``ok=false`` 而不是抛异常（模型可据此重试一次）；
3. 工具只在 ``agent_render_enabled`` 为真时注册，默认不注册（不污染其它会话）。
"""

import json

import pytest
from fastapi import FastAPI

from akm.agent_runtime import render_schema as rs
from akm.agent_runtime.tools import _render_tool, build_builtin_tools


def test_schema_only_exposes_blocks_markdown_cannot_do():
    """暴露给模型的块：10 种结构化块 + flow；markdown 能做的 5 种一律不下发。"""
    schema = rs.load_render_schema()
    types = [block["type"] for block in schema["blocks"]]
    assert len(types) == 11
    assert set(types) == {
        "stats", "cards", "alert", "comparison", "timeline",
        "chart", "progress", "kanban", "map", "flow", "separator",
    }
    for duplicated in ("text", "heading", "code", "table", "list"):
        assert duplicated not in types


def test_tool_name_and_max_blocks_come_from_schema():
    assert rs.tool_name() == "akm_render"
    assert rs.max_blocks() == 3


def test_tool_description_lists_every_block_type():
    """工具描述里必须逐个列出块类型与字段，否则模型无从遵循。"""
    description = rs.build_tool_description()
    for block in rs.load_render_schema()["blocks"]:
        assert block["type"] in description
    assert "硬性规则" in description


def test_tool_parameters_shape():
    params = rs.build_tool_parameters()
    assert params["type"] == "object"
    assert params["required"] == ["blocks"]
    assert params["properties"]["blocks"]["type"] == "array"
    assert params["properties"]["blocks"]["maxItems"] == 3


@pytest.mark.asyncio
async def test_render_tool_returns_normalized_layout():
    raw = await _render_tool(
        title="用量",
        summary="近 7 天 6337 次请求",
        blocks=[
            {"type": "stats", "items": [{"label": "请求数", "value": "6337"}]},
            {"type": "comparison", "headers": ["A", "B"], "rows": [{"label": "维度", "values": ["x", "y"]}]},
        ],
    )
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["blocks"] == 2
    assert payload["summary"] == "近 7 天 6337 次请求"
    assert payload["layout"]["title"] == "用量"
    assert [block["type"] for block in payload["layout"]["blocks"]] == ["stats", "comparison"]


@pytest.mark.asyncio
async def test_render_tool_rejects_legacy_markdown_duplicate_blocks():
    """与 markdown 重复的旧块（text / heading / code / table / list）已从协议移除。

    模型再传这些 type 时整份校验失败——这些形态的归宿是正文 markdown，
    协议与前端渲染都不再为它们保留分支。
    """
    payload = json.loads(
        await _render_tool(
            blocks=[{"type": "table", "columns": [{"key": "k", "label": "K"}], "rows": [{"k": "v"}]}]
        )
    )
    assert payload["ok"] is False

@pytest.mark.asyncio
async def test_render_tool_rejects_unknown_block_without_raising():
    payload = json.loads(await _render_tool(blocks=[{"type": "not_a_block", "x": 1}]))
    assert payload["ok"] is False
    assert "校验" in payload["error"]


@pytest.mark.asyncio
async def test_render_tool_handles_empty_blocks():
    payload = json.loads(await _render_tool(blocks=[]))
    assert payload["ok"] is False


def _tool_names(monkeypatch, enabled: bool) -> list[str]:
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_render_enabled": enabled},
    )
    return [tool.name for tool in build_builtin_tools(FastAPI())]


def test_render_tool_registered_only_when_enabled(monkeypatch):
    assert "akm_render" not in _tool_names(monkeypatch, False)
    assert "akm_render" in _tool_names(monkeypatch, True)


# ── 4. 工具调用级审计 ────────────────────────────────────────────────────────


def _render_handler(app):
    """取出注册进 ToolRegistry 的 akm_render handler（未开启开关时视为测试写错）。"""
    for tool in build_builtin_tools(app):
        if tool.name == "akm_render":
            return tool.handler
    raise AssertionError("akm_render 未注册，检查 agent_render_enabled")


@pytest.mark.asyncio
async def test_render_tool_writes_tool_audit_record(monkeypatch):
    """成功渲染后写一条 source=tool 的审计：provider=akm、model=akm_render、200。

    akm_render 本身不发上游请求，若不单独记这一条，审计日志里就完全看不出
    模型调用过渲染工具、渲染了什么（正文里只剩卡片）。
    """
    audits = []

    async def fake_submit_audit_log(app, data):
        audits.append(data)

    monkeypatch.setattr("akm.server._submit_audit_log", fake_submit_audit_log)
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {
            "agent_render_enabled": True,
            "log_request_body": True,
            "log_response_body": True,
        },
    )

    handler = _render_handler(FastAPI())
    text = await handler(
        title="请求量",
        blocks=[{"type": "alert", "severity": "info", "message": "正常"}],
    )

    assert json.loads(text)["ok"] is True
    assert len(audits) == 1
    record = audits[0]
    assert record["provider"] == "akm"
    # 没有 AgentLoop 注入审计上下文时：model / key_alias 留空，绝不把工具名当模型
    assert record["key_alias"] == ""
    assert record["model"] == ""
    assert record["status_code"] == 200
    assert record["error"] == ""
    # 本地工具不消耗 token，不能污染成本统计
    assert record["total_tokens"] == 0
    assert record["prompt_tokens"] == 0
    # 工具名单独放 x-akm-tool；来源默认按对话触发记 chat-tool
    assert '"x-akm-tool": "akm_render"' in record["request_headers"]
    assert '"x-akm-source": "chat-tool"' in record["request_headers"]
    # 开关打开时：参数进 request_body，归一化后的 layout 进 response_body
    assert "blocks" in record["request_body"]
    assert '"layout"' in record["response_body"]


@pytest.mark.asyncio
async def test_render_tool_audit_uses_request_context(monkeypatch):
    """AgentLoop 注入的审计上下文生效：model 是真实模型、key_alias 是实际用的 Key。

    这条钉住「不要把工具名当模型写进 model 列」——model 列要能参与模型分布统计，
    而工具名只出现在 x-akm-tool 里。
    """
    from akm.agent_runtime import tools as tools_module

    audits = []

    async def fake_submit_audit_log(app, data):
        audits.append(data)

    monkeypatch.setattr("akm.server._submit_audit_log", fake_submit_audit_log)
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_render_enabled": True},
    )

    token = tools_module.set_request_audit_context(
        model="deepseek/deepseek-v4.1-flash",
        key_alias="opencode-main",
        source="chat",
    )
    try:
        handler = _render_handler(FastAPI())
        await handler(blocks=[{"type": "alert", "message": "hi"}])
    finally:
        tools_module.reset_request_audit_context(token)

    record = audits[0]
    assert record["model"] == "deepseek/deepseek-v4.1-flash"
    assert record["key_alias"] == "opencode-main"
    assert '"x-akm-source": "chat-tool"' in record["request_headers"]
    assert '"x-akm-tool": "akm_render"' in record["request_headers"]

@pytest.mark.asyncio
async def test_render_tool_audit_marks_validation_failure(monkeypatch):
    """校验不通过时同样留痕：422 + 可读原因，且默认不落请求/响应体内容。"""
    audits = []

    async def fake_submit_audit_log(app, data):
        audits.append(data)

    monkeypatch.setattr("akm.server._submit_audit_log", fake_submit_audit_log)
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {"agent_render_enabled": True},
    )

    handler = _render_handler(FastAPI())
    text = await handler(blocks=[{"type": "not_a_block"}])

    assert json.loads(text)["ok"] is False
    assert len(audits) == 1
    record = audits[0]
    assert record["status_code"] == 422
    assert record["error"]
    # log_request_body / log_response_body 默认关闭时不落内容
    assert record["request_body"] == ""
    assert record["response_body"] == ""
