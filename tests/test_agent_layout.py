"""布局渲染（AG-UI）链路的边界测试。

覆盖三层，都是「出错必须安静降级」的地方：
1. ``layout_schema.validate_layout`` —— 协议校验与夹紧（纯函数）；
2. ``layout_agent.render_layout`` —— 超时/非 2xx/非法 JSON/空 blocks 都要返回 None，
   且校验失败会带错误反馈重试一次；
3. ``AgentLoop._maybe_render_layout`` —— 开关与模型配置决定是否真的发起调用。
"""

from __future__ import annotations

import json

import pytest

from akm.agent_runtime import layout_agent, layout_schema
from akm.agent_runtime.loop import AgentLoop, AgentResult, ToolRegistry


# ── 1. schema 校验 ────────────────────────────────────────────────────────────


def _layout(blocks: list[dict], **extra) -> dict:
    return {"version": "1.0", "intent": "explanation", "blocks": blocks, **extra}


def test_valid_layout_passes_and_is_cleaned():
    """合法布局通过，且脏字段被丢弃、缺省字段被补齐。"""
    raw = _layout([
        {"type": "text", "content": "  ", "unknown": "drop-me"},
        {"type": "heading", "content": "标题", "rank": 2, "extra": 1},
    ])
    out = layout_schema.validate_layout(raw)
    assert out is not None
    # 空内容的 text 块被当作空壳丢弃，只剩 heading
    assert [block["type"] for block in out["blocks"]] == ["heading"]
    assert out["blocks"][0]["rank"] == 2
    assert "extra" not in out["blocks"][0]
    assert out["version"] == "1.0"


def test_version_mismatch_rejects_whole_document():
    assert layout_schema.validate_layout(_layout([{"type": "text", "content": "x"}], version="2.0")) is None


def test_unknown_block_type_rejects_whole_document():
    """不认识的块整份放弃（不做「跳过未知块继续渲染」）。"""
    raw = _layout([{"type": "text", "content": "x"}, {"type": "carousel"}])
    assert layout_schema.validate_layout(raw) is None


def test_empty_blocks_rejects():
    assert layout_schema.validate_layout(_layout([])) is None
    assert layout_schema.validate_layout({"version": "1.0"}) is None
    assert layout_schema.validate_layout("not-a-dict") is None


def test_blocks_are_capped_by_max_blocks():
    blocks = [{"type": "text", "content": f"第{i}段"} for i in range(20)]
    out = layout_schema.validate_layout(_layout(blocks), max_blocks=3)
    assert out is not None
    assert len(out["blocks"]) == 3


def test_table_rows_and_columns_are_clamped():
    columns = [{"key": f"c{i}", "label": f"列{i}"} for i in range(20)]
    rows = [{f"c{i}": i for i in range(20)} for _ in range(200)]
    out = layout_schema.validate_layout(_layout([{"type": "table", "columns": columns, "rows": rows}]))
    assert out is not None
    table = out["blocks"][0]
    assert len(table["columns"]) <= layout_schema.MAX_COLUMN_COUNT
    assert len(table["rows"]) <= layout_schema.MAX_ROW_COUNT
    # 只保留有 column 定义的键
    assert set(table["rows"][0]) == {column["key"] for column in table["columns"]}


def test_chart_points_and_series_are_clamped():
    series = [{"key": f"s{i}", "label": f"S{i}"} for i in range(20)]
    data = [{"x": i, **{f"s{i}": i for i in range(20)}} for i in range(300)]
    out = layout_schema.validate_layout(_layout([
        {"type": "chart", "chartType": "bar", "data": data, "xKey": "x", "series": series},
    ]))
    assert out is not None
    chart = out["blocks"][0]
    assert len(chart["series"]) <= layout_schema.MAX_SERIES
    assert len(chart["data"]) <= layout_schema.MAX_CHART_POINTS
    # 未在 series/xKey 中声明的键被清理掉
    assert set(chart["data"][0]) <= {entry["key"] for entry in chart["series"]} | {"x"}


def test_chart_color_must_be_hex():
    """颜色只接受 #hex，函数式 CSS 颜色被丢弃（避免注入）。"""
    out = layout_schema.validate_layout(_layout([
        {"type": "chart", "chartType": "bar", "xKey": "x",
         "series": [{"key": "y", "label": "Y", "color": "url(javascript:alert(1))"}],
         "data": [{"x": "a", "y": 1}]},
    ]))
    assert out is not None
    assert out["blocks"][0]["series"][0]["color"] is None


def test_enum_values_fall_back_to_defaults():
    out = layout_schema.validate_layout(_layout([
        {"type": "alert", "severity": "nope", "message": "注意"},
        {"type": "progress", "items": [{"label": "a", "value": 900, "color": "rainbow"}]},
    ]))
    assert out is not None
    assert out["blocks"][0]["severity"] == "info"
    assert out["blocks"][1]["items"][0]["value"] == 100.0
    assert out["blocks"][1]["items"][0]["color"] == "default"


# ── 2. render_layout：任何失败都返回 None ─────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, body: str):
        self.payload = {"status_code": status_code, "body": body}

    async def __call__(self, *_args, **_kwargs):
        return self.payload


@pytest.mark.asyncio
async def test_render_layout_skips_short_content(monkeypatch):
    """正文过短时连上游都不调用（省一次 token）。"""
    called = False

    async def fake_forward(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status_code": 200, "body": ""}

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)
    out = await layout_agent.render_layout(content="好的。", model="m", http_client=None)
    assert out is None
    assert called is False


@pytest.mark.asyncio
async def test_render_layout_returns_none_on_http_error(monkeypatch):
    monkeypatch.setattr("akm.proxy.forward_request", _FakeResponse(500, "boom"))
    out = await layout_agent.render_layout(content="x" * 400, model="m", http_client=None)
    assert out is None


@pytest.mark.asyncio
async def test_render_layout_returns_none_on_invalid_json(monkeypatch):
    body = json.dumps({"choices": [{"message": {"content": "这不是 JSON"}}]})
    monkeypatch.setattr("akm.proxy.forward_request", _FakeResponse(200, body))
    out = await layout_agent.render_layout(content="x" * 400, model="m", http_client=None, max_retries=0)
    assert out is None


@pytest.mark.asyncio
async def test_render_layout_retries_once_then_succeeds(monkeypatch):
    """第一次输出非法、第二次合法 → 带错误反馈重试后拿到布局。"""
    payload = _layout([{"type": "text", "content": "正文"}])
    calls: list[dict] = []

    async def fake_forward(body, *_args, **_kwargs):
        calls.append(body)
        content = "坏输出" if len(calls) == 1 else json.dumps(payload)
        return {"status_code": 200, "body": json.dumps({"choices": [{"message": {"content": content}}]})}

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)
    out = await layout_agent.render_layout(content="x" * 400, model="m", http_client=None, max_retries=1)
    assert out is not None
    assert out["blocks"][0]["content"] == "正文"
    assert len(calls) == 2
    # 第二次请求里带上一次失败原因
    retry_messages = calls[1]["messages"]
    assert any("未通过校验" in str(message.get("content")) for message in retry_messages)


@pytest.mark.asyncio
async def test_render_layout_strips_markdown_fence(monkeypatch):
    payload = _layout([{"type": "heading", "content": "标题", "rank": 1}])
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    body = json.dumps({"choices": [{"message": {"content": fenced}}]})
    monkeypatch.setattr("akm.proxy.forward_request", _FakeResponse(200, body))
    out = await layout_agent.render_layout(content="x" * 400, model="m", http_client=None)
    assert out is not None
    assert out["blocks"][0]["content"] == "标题"


# ── 3. _maybe_render_layout：开关决定是否发起调用 ──────────────────────────────


def _make_loop() -> AgentLoop:
    return AgentLoop(http_client=None, tool_registry=ToolRegistry())



@pytest.mark.asyncio
async def test_maybe_render_layout_without_model_returns_none(monkeypatch):
    """没配布局模型 → 直接跳过（对齐「配了模型才生效」的口径）。"""
    loop = _make_loop()
    called = False

    async def fake_render(**_kwargs):
        nonlocal called
        called = True
        return {"version": "1.0", "intent": "explanation", "title": None, "blocks": []}

    monkeypatch.setattr("akm.agent_runtime.layout_agent.render_layout", fake_render)
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", lambda: {
        "agent_layout_model": "   ",
    })
    assert await loop._maybe_render_layout("x" * 400, "chat/completions") is None
    assert called is False


@pytest.mark.asyncio
async def test_maybe_render_layout_passes_config_through(monkeypatch):
    """启用且配了模型 → 按配置的 timeout/retries/max_blocks 调用。"""
    loop = _make_loop()
    seen: dict = {}

    async def fake_render(**kwargs):
        seen.update(kwargs)
        return {"version": "1.0", "intent": "explanation", "title": None, "blocks": [{"type": "text", "content": "x"}]}

    monkeypatch.setattr("akm.agent_runtime.layout_agent.render_layout", fake_render)
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", lambda: {
        "agent_layout_model": " deepseek/deepseek-v4.1-flash ",
        "agent_layout_timeout_sec": 3.0,
        "agent_layout_max_retries": 0,
        "agent_layout_max_blocks": 5,
    })
    out = await loop._maybe_render_layout("x" * 400, "chat/completions")
    assert out is not None
    assert seen["model"] == "deepseek/deepseek-v4.1-flash"
    assert seen["timeout_sec"] == 3.0
    assert seen["max_retries"] == 0
    assert seen["max_blocks"] == 5


@pytest.mark.asyncio
async def test_maybe_render_layout_swallows_exceptions(monkeypatch):
    """布局调用抛异常时不能影响回答收尾。"""
    loop = _make_loop()

    async def boom(**_kwargs):
        raise RuntimeError("layout exploded")

    monkeypatch.setattr("akm.agent_runtime.layout_agent.render_layout", boom)
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", lambda: {
        "agent_layout_model": "m",
    })
    assert await loop._maybe_render_layout("x" * 400, "chat/completions") is None


# ── 4. AgentResult 序列化 ────────────────────────────────────────────────────


def test_agent_result_carries_layout():
    layout = _layout([{"type": "text", "content": "x"}])
    payload = AgentResult(ok=True, layout=layout).to_dict()
    assert payload["layout"] == layout
    # 未生成时为 None（纯增字段，旧客户端忽略）
    assert AgentResult(ok=True).to_dict()["layout"] is None
