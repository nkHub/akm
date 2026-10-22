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
    """与 markdown 重复的 5 种块（text/heading/code/table/list）不再暴露给模型。"""
    schema = rs.load_render_schema()
    types = [block["type"] for block in schema["blocks"]]
    assert len(types) == 10
    assert set(types) == {
        "stats", "cards", "alert", "comparison", "timeline",
        "chart", "progress", "kanban", "map", "separator",
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
async def test_render_tool_still_accepts_legacy_block_types():
    """table 等旧块已不再暴露给模型，但历史消息仍要能渲染（校验白名单保留全集）。"""
    payload = json.loads(
        await _render_tool(
            blocks=[{"type": "table", "columns": [{"key": "k", "label": "K"}], "rows": [{"k": "v"}]}]
        )
    )
    assert payload["ok"] is True
    assert payload["layout"]["blocks"][0]["type"] == "table"

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
