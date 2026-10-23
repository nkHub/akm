"""布局渲染（AG-UI）链路的边界测试。

覆盖三层，都是「出错必须安静降级」的地方：
1. ``layout_schema.validate_layout`` —— 协议校验与夹紧（纯函数）；
2. ``layout_agent.render_layout`` —— 超时/非 2xx/非法 JSON/空 blocks 都要返回 None，
   且校验失败会带错误反馈重试一次；
3. ``AgentLoop._maybe_render_layout`` —— 开关与模型配置决定是否真的发起调用；
4. 旁路请求审计 —— 布局 / 摘要各自是一次真实上游请求，都要能落审计（source=layout/summary）。
"""

from __future__ import annotations

import asyncio
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
        {"type": "alert", "message": "   ", "unknown": "drop-me"},   # 空壳告警 → 丢弃
        {"type": "alert", "message": "磁盘使用率 92%", "severity": "warning", "extra": 1},
    ])
    out = layout_schema.validate_layout(raw)
    assert out is not None
    # 空 message 的 alert 被当作空壳丢弃，只剩后面那条
    assert [block["type"] for block in out["blocks"]] == ["alert"]
    assert out["blocks"][0]["severity"] == "warning"
    assert "extra" not in out["blocks"][0]
    assert out["version"] == "1.0"


def test_version_mismatch_rejects_whole_document():
    assert layout_schema.validate_layout(_layout([{"type": "alert", "message": "x"}], version="2.0")) is None


def test_unknown_block_type_rejects_whole_document():
    """不认识的块整份放弃（不做「跳过未知块继续渲染」）。"""
    raw = _layout([{"type": "alert", "message": "x"}, {"type": "carousel"}])
    assert layout_schema.validate_layout(raw) is None


def test_empty_blocks_rejects():
    assert layout_schema.validate_layout(_layout([])) is None
    assert layout_schema.validate_layout({"version": "1.0"}) is None
    assert layout_schema.validate_layout("not-a-dict") is None


def test_blocks_are_capped_by_max_blocks():
    blocks = [{"type": "alert", "message": f"第{i}条"} for i in range(20)]
    out = layout_schema.validate_layout(_layout(blocks), max_blocks=3)
    assert out is not None
    assert len(out["blocks"]) == 3




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


def test_chart_xkey_inferred_before_point_cleanup():
    """未传 xKey 时：分类键必须保留，xKey 取数据点的第一个键。

    回归「先按 series ∪ xKey 裁剪数据、再推断 xKey」的顺序错误 ——
    模型漏传 xKey 时分类键已被当成噪声删掉，推断只能落回数值键（"y"），
    前端随即拿不到横轴标签。
    """
    out = layout_schema.validate_layout(_layout([
        {"type": "chart", "chartType": "bar",
         "data": [{"月份": "1月", "销量": 12}, {"月份": "2月", "销量": 18}],
         "series": [{"key": "销量", "label": "销量"}]},
    ]))
    assert out is not None
    chart = out["blocks"][0]
    assert chart["xKey"] == "月份"
    assert [point["月份"] for point in chart["data"]] == ["1月", "2月"]
    assert [point["销量"] for point in chart["data"]] == [12, 18]

    # 显式声明时以声明为准，不被「第一个键」覆盖
    out2 = layout_schema.validate_layout(_layout([
        {"type": "chart", "chartType": "line", "xKey": "月",
         "data": [{"序号": 1, "月": "1月", "值": 3}], "series": [{"key": "值"}]},
    ]))
    assert out2 is not None
    assert out2["blocks"][0]["xKey"] == "月"
    assert out2["blocks"][0]["data"][0] == {"月": "1月", "值": 3}


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
    payload = _layout([{"type": "alert", "message": "正文"}])
    calls: list[dict] = []

    async def fake_forward(body, *_args, **_kwargs):
        calls.append(body)
        content = "坏输出" if len(calls) == 1 else json.dumps(payload)
        return {"status_code": 200, "body": json.dumps({"choices": [{"message": {"content": content}}]})}

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)
    out = await layout_agent.render_layout(content="x" * 400, model="m", http_client=None, max_retries=1)
    assert out is not None
    assert out["blocks"][0]["message"] == "正文"
    assert len(calls) == 2
    # 第二次请求里带上一次失败原因
    retry_messages = calls[1]["messages"]
    assert any("未通过校验" in str(message.get("content")) for message in retry_messages)


@pytest.mark.asyncio
async def test_render_layout_strips_markdown_fence(monkeypatch):
    payload = _layout([{"type": "alert", "message": "标题"}])
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    body = json.dumps({"choices": [{"message": {"content": fenced}}]})
    monkeypatch.setattr("akm.proxy.forward_request", _FakeResponse(200, body))
    out = await layout_agent.render_layout(content="x" * 400, model="m", http_client=None)
    assert out is not None
    assert out["blocks"][0]["message"] == "标题"


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
        return {"version": "1.0", "intent": "explanation", "title": None, "blocks": [{"type": "alert", "message": "x"}]}

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
    layout = _layout([{"type": "alert", "message": "x"}])
    payload = AgentResult(ok=True, layout=layout).to_dict()
    assert payload["layout"] == layout
    # 未生成时为 None（纯增字段，旧客户端忽略）
    assert AgentResult(ok=True).to_dict()["layout"] is None


# ── 5. 旁路请求的审计（layout / summary）──────────────────────────────────────


@pytest.mark.asyncio
async def test_render_layout_notifies_every_attempt(monkeypatch):
    """每次真实上游请求都回调一次（含 400 降级重试），供上层写审计日志。"""
    seen: list[tuple] = []

    async def on_attempt(result, body, response_body, status_code, error):
        seen.append((status_code, error, body.get("model"), bool(response_body)))

    payload = _layout([{"type": "alert", "message": "正文"}])
    statuses = [400, 200]

    async def fake_forward(body, *_args, **_kwargs):
        code = statuses.pop(0)
        return {
            "status_code": code,
            "body": json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}),
            "error": "",
        }

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)
    out = await layout_agent.render_layout(
        content="x" * 400, model="m", http_client=None, on_attempt=on_attempt
    )

    assert out is not None
    # 400 那次也记：它是一次真实请求，只是被降级重试掉了
    assert [item[0] for item in seen] == [400, 200]
    assert seen[0][1]  # 失败那次带错误说明
    assert seen[1][1] == ""
    assert seen[1][2] == "m"


@pytest.mark.asyncio
async def test_render_layout_notifies_timeout(monkeypatch):
    """超时也回调一次（审计侧记一次失败的旁路请求），且仍然安静返回 None。"""
    seen: list[tuple] = []

    async def on_attempt(result, body, response_body, status_code, error):
        seen.append((result, status_code, error))

    async def slow_forward(*_args, **_kwargs):
        await asyncio.sleep(5)
        return {"status_code": 200, "body": ""}

    monkeypatch.setattr("akm.proxy.forward_request", slow_forward)
    out = await layout_agent.render_layout(
        content="x" * 400,
        model="m",
        http_client=None,
        timeout_sec=0.05,
        on_attempt=on_attempt,
    )

    assert out is None
    assert len(seen) == 1
    assert seen[0][0] is None
    assert seen[0][1] == 0
    assert "超时" in seen[0][2]


@pytest.mark.asyncio
async def test_maybe_render_layout_injects_audit_hook(monkeypatch):
    """启用布局时会把审计回调传下去，否则布局这次上游请求在审计里查不到。"""
    loop = _make_loop()
    seen: dict = {}

    async def fake_render(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr("akm.agent_runtime.layout_agent.render_layout", fake_render)
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", lambda: {"agent_layout_model": "m"})

    assert await loop._maybe_render_layout("x" * 400, "chat/completions") is None
    assert callable(seen.get("on_attempt"))


@pytest.mark.asyncio
async def test_summarize_history_writes_audit(monkeypatch):
    """上下文摘要是一次真实上游请求，必须写 source=summary 的审计（含 token 与缓存）。"""
    audits: list[dict] = []

    async def submitter(data):
        audits.append(data)

    loop = AgentLoop(http_client=None, tool_registry=ToolRegistry(), audit_submitter=submitter)

    async def fake_forward(*_args, **_kwargs):
        return {
            "status_code": 200,
            "body": json.dumps({
                "choices": [{"message": {"content": "摘要"}}],
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 10,
                    "total_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 8},
                },
            }),
            "provider": "opencode",
            "key_alias": "opencode",
        }

    monkeypatch.setattr("akm.proxy.forward_request", fake_forward)
    summary = await loop._summarize_history(
        [{"role": "user", "content": "hi"}], "m", "chat/completions"
    )

    assert summary == "摘要"
    assert len(audits) == 1
    record = audits[0]
    assert record["provider"] == "opencode"
    assert record["model"] == "m"
    assert record["status_code"] == 200
    assert record["prompt_tokens"] == 30
    assert record["completion_tokens"] == 10
    assert record["total_tokens"] == 40
    assert record["cached_tokens"] == 8
    assert '"x-akm-source": "summary"' in record["request_headers"]


# ── 6. flow（流程图）块 ───────────────────────────────────────────────────────


def test_flow_block_normalizes_nodes_and_edges():
    """显式 nodes + edges：id 缺省自动生成，direction 缺省 LR。"""
    out = layout_schema.validate_layout(_layout([{
        "type": "flow",
        "nodes": [{"label": "入口"}, {"label": "编排", "kind": "decision"}, {"label": "上游", "kind": "end"}],
        "edges": [{"from": "n0", "to": "n1", "label": "tool_calls"}, {"from": "n1", "to": "n2", "kind": "branch"}],
        "caption": "一次请求的去程",
    }]))
    assert out is not None
    flow = out["blocks"][0]
    assert flow["direction"] == "LR"
    assert [node["id"] for node in flow["nodes"]] == ["n0", "n1", "n2"]
    assert flow["nodes"][1]["kind"] == "decision"
    assert [edge["kind"] for edge in flow["edges"]] == ["main", "branch"]
    assert flow["edges"][0]["label"] == "tool_calls"
    assert flow["caption"] == "一次请求的去程"


def test_flow_block_auto_chains_when_edges_omitted():
    """只给节点时按顺序自动串主链：模型不必为线性流程重复写边。"""
    out = layout_schema.validate_layout(_layout([{
        "type": "flow",
        "nodes": [{"label": "A"}, {"label": "B"}, {"label": "C"}],
    }]))
    assert out is not None
    edges = out["blocks"][0]["edges"]
    assert [(e["from"], e["to"]) for e in edges] == [("n0", "n1"), ("n1", "n2")]
    assert all(e["kind"] == "main" for e in edges)


def test_flow_block_drops_dangling_and_self_loop_edges():
    """指向不存在节点、自环的边一律丢弃（不猜、不模糊匹配）。"""
    out = layout_schema.validate_layout(_layout([{
        "type": "flow",
        "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        "edges": [
            {"from": "a", "to": "b"},
            {"from": "a", "to": "不存在"},
            {"from": "a", "to": "a"},
        ],
    }]))
    assert out is not None
    assert [(e["from"], e["to"]) for e in out["blocks"][0]["edges"]] == [("a", "b")]


def test_flow_block_with_single_node_is_dropped():
    """单节点没有「流程」可言：当作空壳丢弃，整份没有可用块时返回 None。"""
    assert layout_schema.validate_layout(_layout([{
        "type": "flow",
        "nodes": [{"label": "只有一步"}],
    }])) is None


def test_flow_direction_is_clamped():
    out = layout_schema.validate_layout(_layout([{
        "type": "flow",
        "direction": "diagonal",
        "nodes": [{"label": "A"}, {"label": "B"}],
    }]))
    assert out is not None
    assert out["blocks"][0]["direction"] == "LR"
    out_tb = layout_schema.validate_layout(_layout([{
        "type": "flow",
        "direction": "TB",
        "nodes": [{"label": "A"}, {"label": "B"}],
    }]))
    assert out_tb["blocks"][0]["direction"] == "TB"


def test_flow_node_and_edge_counts_are_capped():
    nodes = [{"label": f"N{i}"} for i in range(40)]
    out = layout_schema.validate_layout(_layout([{"type": "flow", "nodes": nodes}]))
    assert out is not None
    assert len(out["blocks"][0]["nodes"]) <= layout_schema.MAX_FLOW_NODES
    assert len(out["blocks"][0]["edges"]) <= layout_schema.MAX_FLOW_EDGES


# ── 7. flow 分组（泳道）与图例 ───────────────────────────────────────────────


def _flow_with_groups(groups, legend=None, nodes=None):
    """构造一个只关心 groups / legend 的 flow 块。"""
    block = {
        "type": "flow",
        "nodes": nodes
        or [
            {"id": "a", "label": "A"},
            {"id": "b", "label": "B"},
            {"id": "c", "label": "C"},
        ],
        "groups": groups,
    }
    if legend is not None:
        block["legend"] = legend
    return _layout([block])


def test_flow_groups_keep_only_existing_nodes():
    """分组里引用了不存在的节点 id 时只丢弃该 id，不整组报废。"""
    out = layout_schema.validate_layout(
        _flow_with_groups([{"label": "入口", "nodes": ["a", "不存在", "c"]}])
    )
    assert out is not None
    group = out["blocks"][0]["groups"][0]
    assert group["nodes"] == ["a", "c"]
    assert group["color"] == "default"


def test_flow_groups_assign_each_node_to_first_group():
    """一个节点只归属第一个匹配的分组，避免框套框。"""
    out = layout_schema.validate_layout(
        _flow_with_groups([
            {"label": "第一组", "nodes": ["a", "b"]},
            {"label": "第二组", "nodes": ["b", "c"]},
        ])
    )
    assert out is not None
    groups = out["blocks"][0]["groups"]
    assert groups[0]["nodes"] == ["a", "b"]
    assert groups[1]["nodes"] == ["c"]


def test_flow_groups_drop_empty_and_cap_count():
    """空组（无有效节点）丢弃且不占名额；有效组超过 4 个时只保留前 4 组。"""
    nodes = [{"id": f"n{i}", "label": f"N{i}"} for i in range(6)]
    out = layout_schema.validate_layout(
        _flow_with_groups(
            [
                {"label": "空组", "nodes": ["不存在"]},
                {"label": "g1", "nodes": ["n0"]},
                {"label": "g2", "nodes": ["n1"]},
                {"label": "g3", "nodes": ["n2"]},
                {"label": "g4", "nodes": ["n3"]},
                {"label": "g5", "nodes": ["n4"]},
            ],
            nodes=nodes,
        )
    )
    assert out is not None
    groups = out["blocks"][0]["groups"]
    assert [g["label"] for g in groups] == ["g1", "g2", "g3", "g4"]
    assert len(groups) <= layout_schema.MAX_FLOW_GROUPS


def test_flow_legend_flag_defaults_false_and_passes_true():
    out = layout_schema.validate_layout(_flow_with_groups([], legend=None))
    assert out["blocks"][0]["legend"] is False
    out2 = layout_schema.validate_layout(_flow_with_groups([], legend=True))
    assert out2["blocks"][0]["legend"] is True


def test_flow_group_color_is_clamped():
    out = layout_schema.validate_layout(
        _flow_with_groups([{"label": "g", "color": "neon", "nodes": ["a"]}])
    )
    assert out["blocks"][0]["groups"][0]["color"] == "default"
