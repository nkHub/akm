"""工具网关（按需加载）测试：折叠规划、网关执行、越权拒绝与配置归一化。

覆盖的行为契约：
- 可下发工具数超过阈值时，长尾工具折叠为 akm_list_tools / akm_call_tool，常用工具保持直连；
- 未超阈值、开关关闭、显式 tools 白名单三种情况都不折叠（回到原行为）；
- akm_call_tool 只能执行本次被折叠的工具，不能借网关绕过「默认排除」或直接调常驻工具；
- 模型误调被折叠的工具时，错误提示指向网关（而不是笼统的「未获授权」）；
- SSE 的 tool_call / tool_result 事件带 inner_name，前端可显示真实工具名；
- 两个新配置项参与归一化（布尔/阈值夹紧）并写入 agent_config 归组。
"""

import json

import pytest
from fastapi import FastAPI

from akm.agent_runtime import loop as loop_module
from akm.agent_runtime.loop import AgentLoop, ToolDef, ToolRegistry
from akm.agent_runtime.tools import build_builtin_tools, set_request_gateway_context, reset_request_gateway_context

# 测试用的长尾工具名：故意不用内置工具名，避免与真实注册表语义混淆。
_LONG_TAIL = [f"long_tail_{index}" for index in range(10)]


def _gateway_defs() -> tuple[ToolDef, ToolDef]:
    """从真实注册代码里取两个网关工具定义（handler 是闭包，只能这样拿到）。"""
    defs = {tool.name: tool for tool in build_builtin_tools(FastAPI())}
    return defs["akm_list_tools"], defs["akm_call_tool"]


def _gateway_config(**overrides) -> dict:
    """返回 AgentLoop 读取的最小配置：网关开启、阈值 10、关闭自愈重试与自动压缩。"""
    config = {
        "agent_tool_gateway_enabled": True,
        "agent_tool_gateway_threshold": 10,
        "agent_max_tool_calls": 50,
        "agent_tool_retry_max_retries": 0,
        "agent_max_turns": 6,
        "agent_max_context_tokens": 0,
        "agent_keep_recent_messages": 10,
        "agent_context_warning_ratio": 0.0,
    }
    config.update(overrides)
    return config


def _build_registry(calls: list[tuple[str, dict]]) -> ToolRegistry:
    """构造：两个网关工具 + 1 个常驻工具 + 10 个长尾工具（共 13 个，必定超过阈值）。"""
    list_def, call_def = _gateway_defs()
    registry = ToolRegistry()
    registry.register(list_def)
    registry.register(call_def)

    def make_handler(name: str):
        def handler(**kwargs):
            calls.append((name, kwargs))
            return {"ok": True, "tool": name, "args": kwargs}

        return handler

    registry.register(ToolDef("akm_read_file", "读取文件", {"type": "object", "properties": {}}, make_handler("akm_read_file")))
    for name in _LONG_TAIL:
        registry.register(ToolDef(name, f"{name} 的描述", {"type": "object", "properties": {}}, make_handler(name)))
    return registry


@pytest.fixture
def gateway_config(monkeypatch):
    """把 AgentLoop 读到的配置钉成网关开启 + 阈值 10（隔离本机 config.json）。"""
    fake = _gateway_config()
    monkeypatch.setattr(loop_module, "load_config", lambda: dict(fake))
    return fake


def _tool_names(plan) -> list[str]:
    return [tool["function"]["name"] for tool in plan[0]]


def test_plan_tools_folds_long_tail_and_pins_common_tools(gateway_config):
    """超过阈值：常驻工具直连，长尾折叠，两个网关工具追加在末尾。"""
    registry = _build_registry([])
    plan = AgentLoop(http_client=None, tool_registry=registry)._plan_tools(None, None, None)
    names = _tool_names(plan)

    assert names[0] == "akm_read_file"
    gateway_index = names.index("akm_list_tools")
    assert names[gateway_index:gateway_index + 2] == ["akm_list_tools", "akm_call_tool"]
    assert not set(_LONG_TAIL) & set(names)
    assert set(plan[4]) == set(_LONG_TAIL)
    # 框架工具照旧追加，且网关工具在授权名单里（否则模型调用会被拒）
    assert {"akm_ask_user", "akm_context_status", "akm_compact_context"} <= set(names)
    assert {"akm_list_tools", "akm_call_tool"} <= plan[1]


def test_plan_tools_keeps_full_list_under_threshold(gateway_config):
    """可下发工具数不超过阈值时不折叠：不应该白白多出两个声明。"""
    list_def, call_def = _gateway_defs()
    registry = ToolRegistry()
    registry.register(list_def)
    registry.register(call_def)
    for name in _LONG_TAIL[:4]:
        registry.register(ToolDef(name, "描述", {"type": "object", "properties": {}}, lambda: {}))
    plan = AgentLoop(http_client=None, tool_registry=registry)._plan_tools(None, None, None)

    assert plan[4] is None
    assert "akm_list_tools" not in _tool_names(plan)
    assert set(_LONG_TAIL[:4]) <= set(_tool_names(plan))


def test_plan_tools_keeps_full_list_when_gateway_disabled(monkeypatch):
    """开关关闭时回到全量下发（行为与改造前一致）。"""
    monkeypatch.setattr(
        loop_module, "load_config", lambda: _gateway_config(agent_tool_gateway_enabled=False)
    )
    registry = _build_registry([])
    plan = AgentLoop(http_client=None, tool_registry=registry)._plan_tools(None, None, None)

    assert plan[4] is None
    assert "akm_list_tools" not in _tool_names(plan)
    assert set(_LONG_TAIL) <= set(_tool_names(plan))


def test_plan_tools_whitelist_is_not_folded(gateway_config):
    """调用方显式传 tools 时不折叠：白名单就是调用方的明确意图。"""
    registry = _build_registry([])
    declared = [
        {"type": "function", "function": {"name": name, "description": "d", "parameters": {"type": "object", "properties": {}}}}
        for name in _LONG_TAIL
    ]
    plan = AgentLoop(http_client=None, tool_registry=registry)._plan_tools(declared, None, None)

    assert plan[4] is None
    assert set(_LONG_TAIL) <= set(_tool_names(plan))
    assert "akm_call_tool" not in _tool_names(plan)


@pytest.mark.asyncio
async def test_gateway_call_executes_folded_tool(monkeypatch, gateway_config):
    """模型通过 akm_call_tool 调用被折叠的工具时，内层 handler 收到正确参数。"""
    calls: list[tuple[str, dict]] = []
    registry = _build_registry(calls)
    requests: list[dict] = []
    responses = iter([
        {
            "status_code": 200,
            "body": json.dumps({
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "akm_call_tool",
                                "arguments": json.dumps({"name": "long_tail_3", "arguments": {"x": 1}}),
                            },
                        }],
                    }
                }]
            }),
        },
        {"status_code": 200, "body": '{"choices":[{"message":{"content":"完成"}}]}'},
    ])

    async def forward(body, *_args, **_kwargs):
        requests.append(json.loads(json.dumps(body)))
        return next(responses)

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    result = await AgentLoop(http_client=None, tool_registry=registry).run(
        [{"role": "user", "content": "hi"}]
    )

    assert result.ok is True
    assert calls == [("long_tail_3", {"x": 1})]
    # 结果回传给模型的是内层工具的真实输出
    tool_messages = [m for m in requests[1]["messages"] if m.get("role") == "tool"]
    assert json.loads(tool_messages[0]["content"])["tool"] == "long_tail_3"


@pytest.mark.asyncio
async def test_gateway_call_rejects_tool_not_in_folded_set(monkeypatch, gateway_config):
    """不能借网关绕过默认排除：未折叠/未下发的工具一律拒绝，handler 不执行。"""
    calls: list[tuple[str, dict]] = []
    registry = _build_registry(calls)
    requests: list[dict] = []
    responses = iter([
        {
            "status_code": 200,
            "body": json.dumps({
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "akm_call_tool",
                                "arguments": json.dumps({"name": "akm_read_file", "arguments": {}}),
                            },
                        }],
                    }
                }]
            }),
        },
        {"status_code": 200, "body": '{"choices":[{"message":{"content":"完成"}}]}'},
    ])

    async def forward(body, *_args, **_kwargs):
        requests.append(json.loads(json.dumps(body)))
        return next(responses)

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    await AgentLoop(http_client=None, tool_registry=registry).run(
        [{"role": "user", "content": "hi"}]
    )

    assert calls == []
    tool_messages = [m for m in requests[1]["messages"] if m.get("role") == "tool"]
    assert "未折叠" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_direct_call_of_folded_tool_points_to_gateway(monkeypatch, gateway_config):
    """模型误调被折叠的工具时，提示改用 akm_call_tool，而不是笼统的「未获授权」。"""
    calls: list[tuple[str, dict]] = []
    registry = _build_registry(calls)
    requests: list[dict] = []
    responses = iter([
        {
            "status_code": 200,
            "body": json.dumps({
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "long_tail_7", "arguments": "{}"},
                        }],
                    }
                }]
            }),
        },
        {"status_code": 200, "body": '{"choices":[{"message":{"content":"完成"}}]}'},
    ])

    async def forward(body, *_args, **_kwargs):
        requests.append(json.loads(json.dumps(body)))
        return next(responses)

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    await AgentLoop(http_client=None, tool_registry=registry).run(
        [{"role": "user", "content": "hi"}]
    )

    assert calls == []
    tool_messages = [m for m in requests[1]["messages"] if m.get("role") == "tool"]
    assert "已折叠" in tool_messages[0]["content"]
    assert "akm_call_tool" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_list_tools_returns_schema_by_name_and_filters_by_query(gateway_config):
    """清单视图只给名字+描述；传 name 才返回完整 parameters，避免一次拉满上下文。"""
    list_def, _ = _gateway_defs()
    registry = _build_registry([])
    token = set_request_gateway_context(
        tool_names=frozenset(_LONG_TAIL), registry=registry
    )
    try:
        summary = await list_def.handler(limit=5)
        assert summary["returned"] == 5
        assert all("parameters" not in item for item in summary["tools"])

        detail = await list_def.handler(name="long_tail_3")
        assert detail["returned"] == 1
        assert detail["tools"][0]["name"] == "long_tail_3"
        assert detail["tools"][0]["parameters"] == {"type": "object", "properties": {}}

        filtered = await list_def.handler(query="long_tail_1")
        names = [item["name"] for item in filtered["tools"]]
        assert names == ["long_tail_1"]

        # 网关自身不出现在清单里（否则模型会递归调自己）
        assert "akm_call_tool" not in [item["name"] for item in summary["tools"]]
        assert "akm_read_file" not in [item["name"] for item in summary["tools"]]
    finally:
        reset_request_gateway_context(token)


@pytest.mark.asyncio
async def test_list_tools_rejects_unknown_name(gateway_config):
    """按 name 查不到时返回可操作错误，而不是空列表。"""
    list_def, _ = _gateway_defs()
    registry = _build_registry([])
    token = set_request_gateway_context(tool_names=frozenset(_LONG_TAIL), registry=registry)
    try:
        result = await list_def.handler(name="not_exist")
        assert result["ok"] is False
        assert "未找到工具" in result["error"]
    finally:
        reset_request_gateway_context(token)


def test_gateway_inner_name_only_for_call_tool():
    """inner_name 只对 akm_call_tool 生效，其他工具与异常参数都返回空串。"""
    assert loop_module._gateway_inner_name("akm_call_tool", {"name": "long_tail_1"}) == "long_tail_1"
    assert loop_module._gateway_inner_name("akm_call_tool", {"name": ""}) == ""
    assert loop_module._gateway_inner_name("akm_call_tool", ["not", "dict"]) == ""
    assert loop_module._gateway_inner_name("long_tail_1", {"name": "x"}) == ""


def test_config_normalizes_gateway_keys(isolated_config):
    """新配置项参与归一化：布尔兜底默认开启，阈值夹紧到 [1, 100] 并写入 agent_config。"""
    import akm.config as cfg

    cfg.save_config({"agent_tool_gateway_threshold": 999})
    loaded = cfg.load_config()
    assert loaded["agent_tool_gateway_threshold"] == 100
    assert loaded["agent_tool_gateway_enabled"] is True

    cfg.save_config({"agent_tool_gateway_enabled": False, "agent_tool_gateway_threshold": 0})
    loaded = cfg.load_config()
    assert loaded["agent_tool_gateway_enabled"] is False
    assert loaded["agent_tool_gateway_threshold"] == 1

    raw = json.loads(open(isolated_config / "config.json", encoding="utf-8").read())
    assert raw["agent_config"]["agent_tool_gateway_enabled"] is False
    assert raw["agent_config"]["agent_tool_gateway_threshold"] == 1
    assert "agent_tool_gateway_enabled" not in raw


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """把 config 目录指向临时目录，避免测试改写用户真实的 ~/.akm/config.json。"""
    import akm.config as cfg

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr(cfg, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(cfg_dir / "config.json"))
    return cfg_dir


class _FakeStreamResponse:
    """最小流响应：只提供 Agent Loop 用到的 aiter_bytes / aread / aclose。"""

    def __init__(self, status_code, chunks):
        self.status_code = status_code
        self._chunks = [chunk.encode("utf-8") if isinstance(chunk, str) else chunk for chunk in chunks]
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return b"".join(self._chunks)

    async def aclose(self):
        self.closed = True


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _events(chunks) -> list[dict]:
    return [json.loads(chunk.removeprefix("data: ")) for chunk in chunks]


@pytest.mark.asyncio
async def test_run_stream_tool_events_expose_inner_name(monkeypatch, gateway_config):
    """流式路径的 tool_call / tool_result 事件要带 inner_name，前端才能显示真实工具名。"""
    calls: list[tuple[str, dict]] = []
    registry = _build_registry(calls)
    first = _FakeStreamResponse(200, [
        _sse({"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "akm_call_tool",
                "arguments": json.dumps({"name": "long_tail_3", "arguments": {"k": "v"}}),
            },
        }]}}]}),
        "data: [DONE]\n\n",
    ])
    second = _FakeStreamResponse(200, [
        _sse({"choices": [{"delta": {"content": "完成"}}]}),
        "data: [DONE]\n\n",
    ])
    responses = iter([first, second])

    async def forward(*_args, **_kwargs):
        return {
            "stream": True,
            "response": next(responses),
            "status_code": 200,
            "provider": "test",
            "key_alias": "key",
        }

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    events = _events([
        item async for item in AgentLoop(http_client=None, tool_registry=registry).run_stream(
            [{"role": "user", "content": "hi"}]
        )
    ])

    tool_call_event = next(event for event in events if event["event"] == "tool_call")
    assert tool_call_event["data"]["name"] == "akm_call_tool"
    assert tool_call_event["data"]["inner_name"] == "long_tail_3"

    tool_result_event = next(event for event in events if event["event"] == "tool_result")
    assert tool_result_event["data"]["inner_name"] == "long_tail_3"
    assert calls == [("long_tail_3", {"k": "v"})]


def test_pinned_tools_cover_render_tool_name():
    """常驻名单里的渲染工具名必须与 render_schema 的真源一致。

    否则有人改 render_schema.json 的 tool.name 后，akm_render 会从「常驻」掉进折叠集合：
    每一步渲染都要多一次 list+call，而且不会有任何报错提示——只能靠这条测试兜住。
    """
    from akm.agent_runtime.render_schema import load_render_schema

    assert load_render_schema()["tool"]["name"] in loop_module._GATEWAY_PINNED_TOOLS
