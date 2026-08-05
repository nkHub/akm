import json
from pathlib import Path

import pytest

from akm.agent_runtime.loop import AgentLoop, ToolDef, ToolRegistry, _SSEStreamAccumulator

class FakeStreamResponse:
    """提供可分块读取的最小流响应，便于验证 Agent 不透传上游帧。"""

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


def _sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _events(chunks):
    return [json.loads(chunk.removeprefix("data: ")) for chunk in chunks]


@pytest.mark.asyncio
async def test_run_stream_emits_agent_deltas_and_preserves_upstream_total_usage(monkeypatch):
    """思考（reasoning）与正文应各自实时流式下发，思考优先于同段正文。"""
    response = FakeStreamResponse(200, [
        _sse({"model": "test", "choices": [{"delta": {"content": "你", "reasoning_content": "先"}}]}),
        _sse({"choices": [{"delta": {"content": "好", "reasoning_content": "思考"}}]}) + _sse({"usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 10}}),
        "data: [DONE]\n\n",
    ])

    async def forward(*_args, **_kwargs):
        return {"stream": True, "response": response, "status_code": 200, "provider": "test", "key_alias": "key"}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    loop = AgentLoop(http_client=None, tool_registry=ToolRegistry())
    events = _events([item async for item in loop.run_stream([{"role": "user", "content": "hi"}])])

    assert [event["event"] for event in events] == ["reasoning_delta", "model_delta", "reasoning_delta", "model_delta", "final"]
    assert [event["data"]["content"] for event in events[:4]] == ["先", "你", "思考", "好"]
    assert events[-1]["data"]["final_message"]["content"] == "你好"
    assert events[-1]["data"]["final_message"]["reasoning_content"] == "先思考"
    assert events[-1]["data"]["usage"] == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 10}
    assert response.closed is True


@pytest.mark.asyncio
async def test_run_stream_reassembles_tool_call_before_next_turn(monkeypatch):
    """分片 tool_calls 要在首轮结束后执行，并作为下一轮的上下文回传。"""
    ToolRegistry.reset()
    registry = ToolRegistry.instance()
    registry.register(ToolDef("get_weather", "weather", {"type": "object"}, lambda city: {"city": city, "temp": 25}))
    first = FakeStreamResponse(200, [
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_", "arguments": "{\"city\":\""}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "weather", "arguments": "beijing\"}"}}]}}]}),
    ])
    second = FakeStreamResponse(200, [_sse({"choices": [{"delta": {"content": "晴天"}}]})])
    calls = []

    async def forward(body, *_args, **_kwargs):
        # run_stream 会在下一轮继续追加消息，保存快照以核对本轮实际提交的上下文。
        calls.append(json.loads(json.dumps(body)))
        return {"stream": True, "response": [first, second][len(calls) - 1], "status_code": 200}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    loop = AgentLoop(http_client=None, tool_registry=registry)
    events = _events([item async for item in loop.run_stream([{"role": "user", "content": "天气"}])])

    assert [event["event"] for event in events] == ["turn_start", "tool_call", "tool_result", "model_delta", "final"]
    assert events[1]["data"] == {"name": "get_weather", "arguments": {"city": "beijing"}}
    assert events[2]["data"]["result"] == '{"city": "beijing", "temp": 25}'
    assert calls[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert calls[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": '{"city": "beijing", "temp": 25}'}
    ToolRegistry.reset()


@pytest.mark.asyncio
async def test_run_stream_streams_content_realtime_then_tools(monkeypatch):
    """工具轮正文应实时以 model_delta 流出（自然顺序），工具事件随后，最终主体实时流式。"""
    ToolRegistry.reset()
    registry = ToolRegistry.instance()
    registry.register(ToolDef("get_weather", "weather", {"type": "object"}, lambda city: {"city": city, "temp": 25}))
    first = FakeStreamResponse(200, [
        _sse({"choices": [{"delta": {"content": "我来查一下天气", "reasoning_content": "用户想查天气"}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\":\"beijing\"}"}}]}}]}),
    ])
    second = FakeStreamResponse(200, [_sse({"choices": [{"delta": {"content": "北京晴天"}}]})])
    calls = []

    async def forward(body, *_args, **_kwargs):
        calls.append(body)
        return {"stream": True, "response": [first, second][len(calls) - 1], "status_code": 200}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    loop = AgentLoop(http_client=None, tool_registry=registry)
    events = _events([item async for item in loop.run_stream([{"role": "user", "content": "北京天气"}])])

    # 顺序：思考 → 工具轮正文(model_delta) → 工具 → 最终主体(model_delta) → final
    assert [event["event"] for event in events] == [
        "reasoning_delta", "model_delta", "turn_start", "tool_call", "tool_result", "model_delta", "final",
    ]
    assert events[0]["data"]["content"] == "用户想查天气"
    assert events[1]["data"]["content"] == "我来查一下天气"
    assert events[-2]["data"]["content"] == "北京晴天"
    assert events[-1]["data"]["final_message"]["content"] == "北京晴天"
    ToolRegistry.reset()


@pytest.mark.asyncio
async def test_run_stream_reports_streaming_http_error_and_closes_response(monkeypatch):
    """流式 HTTP 错误不能被当成正常 SSE 解析，也必须关闭响应连接。"""
    response = FakeStreamResponse(429, ['{"error":"rate limited"}'])
    audits = []

    async def forward(*_args, **_kwargs):
        return {"stream": True, "response": response, "status_code": 429, "provider": "test", "key_alias": "key"}

    async def audit(record):
        audits.append(record)

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    loop = AgentLoop(http_client=None, tool_registry=ToolRegistry(), audit_submitter=audit)
    events = _events([item async for item in loop.run_stream([{"role": "user", "content": "hi"}])])

    assert events == [{"event": "error", "data": {"error": "rate limited", "turns": 1, "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "compacted": 0}}]
    assert response.closed is True
    assert audits[-1]["status_code"] == 429
    assert audits[-1]["error"] == "rate limited"


@pytest.mark.asyncio
async def test_run_includes_registered_tools_in_upstream_request(monkeypatch):
    """未显式传 tools 时，内置注册表工具也必须提供给上游模型调用。"""
    registry = ToolRegistry()
    registry.register(ToolDef("akm_get_status", "读取 AKM 状态", {"type": "object", "properties": {}}, lambda: {}))
    requests = []

    async def forward(body, *_args, **_kwargs):
        requests.append(body)
        return {"status_code": 200, "body": '{"choices":[{"message":{"content":"ok"}}]}'}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    result = await AgentLoop(http_client=None, tool_registry=registry).run([{"role": "user", "content": "状态"}])

    assert result.ok is True
    names = [t["function"]["name"] for t in requests[0]["tools"]]
    assert names == ["akm_get_status", "akm_context_status", "akm_compact_context"]


@pytest.mark.asyncio
async def test_run_whitelist_injects_only_client_declared_tools(monkeypatch):
    """客户端显式声明 tools 时，只注入客户端声明的工具 + 上下文管理框架工具。"""
    registry = ToolRegistry()
    registry.register(ToolDef("akm_get_status", "读取 AKM 状态", {"type": "object", "properties": {}}, lambda: {}))
    client_tool = {"type": "function", "function": {"name": "my_search", "description": "自定义搜索", "parameters": {"type": "object", "properties": {}}}}
    requests = []

    async def forward(body, *_args, **_kwargs):
        requests.append(body)
        return {"status_code": 200, "body": '{"choices":[{"message":{"content":"ok"}}]}'}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    result = await AgentLoop(http_client=None, tool_registry=registry).run(
        [{"role": "user", "content": "搜一下"}], tools=[client_tool]
    )

    assert result.ok is True
    names = [t["function"]["name"] for t in requests[0]["tools"]]
    assert names == ["my_search", "akm_context_status", "akm_compact_context"]
    assert "akm_get_status" not in names


@pytest.mark.asyncio
async def test_run_stream_whitelist_injects_only_client_declared_tools(monkeypatch):
    """流式路径同样遵循白名单：客户端声明 tools 时注入声明工具 + 框架工具。"""
    registry = ToolRegistry()
    registry.register(ToolDef("akm_get_time", "获取时间", {"type": "object", "properties": {}}, lambda: {}))
    client_tool = {"type": "function", "function": {"name": "my_tool", "description": "自定义", "parameters": {"type": "object", "properties": {}}}}
    requests = []

    async def forward(body, *_args, **_kwargs):
        requests.append(body)
        return {"status_code": 200, "body": '{"choices":[{"message":{"content":"ok"}}]}'}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    result = AgentLoop(http_client=None, tool_registry=registry).run_stream(
        [{"role": "user", "content": "hi"}], tools=[client_tool]
    )
    async for _ in result:
        break

    names = [t["function"]["name"] for t in requests[0]["tools"]]
    assert names == ["my_tool", "akm_context_status", "akm_compact_context"]
    assert "akm_get_time" not in names


@pytest.mark.asyncio
async def test_run_empty_tools_injects_no_tools(monkeypatch):
    """显式传空数组 [] 时不注入任何工具（既不注入内置工具，也不带上游 tools 字段）。"""
    registry = ToolRegistry()
    registry.register(ToolDef("akm_get_status", "读取 AKM 状态", {"type": "object", "properties": {}}, lambda: {}))
    requests = []

    async def forward(body, *_args, **_kwargs):
        requests.append(body)
        return {"status_code": 200, "body": '{"choices":[{"message":{"content":"ok"}}]}'}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    result = await AgentLoop(http_client=None, tool_registry=registry).run(
        [{"role": "user", "content": "你好"}], tools=[]
    )

    assert result.ok is True
    assert "tools" not in requests[0]


@pytest.mark.asyncio
async def test_run_stream_empty_tools_injects_no_tools(monkeypatch):
    """流式路径同样遵循空数组语义：显式传 [] 时既不注入内置工具，也不带上游 tools 字段。"""
    registry = ToolRegistry()
    registry.register(ToolDef("akm_get_time", "获取时间", {"type": "object", "properties": {}}, lambda: {}))
    requests = []

    async def forward(body, *_args, **_kwargs):
        requests.append(body)
        return {"status_code": 200, "body": '{"choices":[{"message":{"content":"ok"}}]}'}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    result = AgentLoop(http_client=None, tool_registry=registry).run_stream(
        [{"role": "user", "content": "hi"}], tools=[]
    )
    async for _ in result:
        break

    assert "tools" not in requests[0]


@pytest.mark.asyncio
async def test_run_default_injects_tools_except_excluded(monkeypatch):
    """未传 tools 时默认注入除联网搜索/图片（_DEFAULT_EXCLUDED_TOOLS）外的全部内置工具。"""
    registry = ToolRegistry()
    for name in ("tavily_search", "akm_generate_image", "akm_edit_image", "akm_get_status", "akm_get_time"):
        registry.register(ToolDef(name, f"{name} 描述", {"type": "object", "properties": {}}, lambda: {}))
    requests = []

    async def forward(body, *_args, **_kwargs):
        requests.append(body)
        return {"status_code": 200, "body": '{"choices":[{"message":{"content":"ok"}}]}'}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    result = await AgentLoop(http_client=None, tool_registry=registry).run(
        [{"role": "user", "content": "你好"}]
    )

    assert result.ok is True
    names = [t["function"]["name"] for t in requests[0]["tools"]]
    assert "akm_get_status" in names
    assert "akm_get_time" in names
    assert "tavily_search" not in names
    assert "akm_generate_image" not in names
    assert "akm_edit_image" not in names


@pytest.mark.asyncio
async def test_run_explicit_tools_can_override_excluded(monkeypatch):
    """客户端显式声明被默认排除的工具时，仍按白名单正常注入（可主动启用联网搜索/图片）。"""
    registry = ToolRegistry()
    registry.register(ToolDef("tavily_search", "联网搜索", {"type": "object", "properties": {}}, lambda: {}))
    client_tool = {"type": "function", "function": {"name": "tavily_search", "description": "联网搜索", "parameters": {"type": "object", "properties": {}}}}
    requests = []

    async def forward(body, *_args, **_kwargs):
        requests.append(body)
        return {"status_code": 200, "body": '{"choices":[{"message":{"content":"ok"}}]}'}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    result = await AgentLoop(http_client=None, tool_registry=registry).run(
        [{"role": "user", "content": "帮我搜一下"}], tools=[client_tool]
    )

    assert result.ok is True
    names = [t["function"]["name"] for t in requests[0]["tools"]]
    assert names == ["tavily_search", "akm_context_status", "akm_compact_context"]


@pytest.mark.asyncio
async def test_run_stream_default_excludes_tavily_and_image(monkeypatch):
    """流式路径未传 tools 时同样默认排除联网搜索/图片工具。"""
    registry = ToolRegistry()
    for name in ("tavily_search", "akm_generate_image", "akm_get_time"):
        registry.register(ToolDef(name, f"{name} 描述", {"type": "object", "properties": {}}, lambda: {}))
    requests = []

    async def forward(body, *_args, **_kwargs):
        requests.append(body)
        return {"status_code": 200, "body": '{"choices":[{"message":{"content":"ok"}}]}'}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    result = AgentLoop(http_client=None, tool_registry=registry).run_stream(
        [{"role": "user", "content": "hi"}]
    )
    async for _ in result:
        break

    names = [t["function"]["name"] for t in requests[0]["tools"]]
    assert "akm_get_time" in names
    assert "tavily_search" not in names
    assert "akm_generate_image" not in names


def test_sse_accumulator_supports_responses_and_messages_events():
    """原生 Responses/Messages 流也应能还原可见文本和工具调用。"""
    responses = _SSEStreamAccumulator()
    assert responses.feed(_sse({"type": "response.output_text.delta", "delta": "你好"})) == ["你好"]
    responses.feed(_sse({"type": "response.output_item.added", "output_index": 3, "item": {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": ""}}))
    responses.feed(_sse({"type": "response.function_call_arguments.delta", "output_index": 3, "delta": "{\"id\":1}"}))
    assert _extract_calls(responses.response_body) == [{"id": "call_1", "name": "lookup", "arguments": '{"id":1}'}]

    messages = _SSEStreamAccumulator()
    assert messages.feed(_sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "世界"}})) == ["世界"]
    messages.feed(_sse({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {}}}))
    messages.feed(_sse({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"id\":2}"}}))
    assert _extract_calls(messages.response_body) == [{"id": "tool_1", "name": "lookup", "arguments": '{"id":2}'}]


def _extract_calls(response_body):
    from akm.agent_runtime.loop import _extract_tool_calls_from_response

    return _extract_tool_calls_from_response(response_body)


def _small_config():
    """返回一个把上下文阈值调小、便于触发压缩/告警的配置。"""
    return {
        "agent_max_turns": 20,
        "agent_max_context_tokens": 60,
        "agent_keep_recent_messages": 2,
        "agent_context_warning_ratio": 0.8,
    }


def _big_config():
    """返回一个关闭自动压缩兜底（阈值极大）的配置，用于单独验证 AI 主动压缩。"""
    return {
        "agent_max_turns": 20,
        "agent_max_context_tokens": 999999,
        "agent_keep_recent_messages": 2,
        "agent_context_warning_ratio": 0.8,
    }


@staticmethod
def _long_cjk_message(n: int) -> list[dict]:
    """构造一个足够长的多轮对话，使其 token 估算超过阈值。"""
    return [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "字" * n},
        {"role": "assistant", "content": "字" * n},
        {"role": "user", "content": "字" * n},
        {"role": "assistant", "content": "字" * n},
        {"role": "user", "content": "当前问题"},
    ]


@pytest.mark.asyncio
async def test_run_ai_compact_context_tool_compresses_history(monkeypatch):
    """AI 主动调用 akm_compact_context 时应强制压缩早期历史并回传压缩结果。

    使用极大阈值关闭自动压缩兜底，确保只有 AI 主动调用触发压缩。
    """
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", _big_config)
    calls = []

    async def forward(body, *_args, **_kwargs):
        if body["messages"][0].get("content") == "你是一个对话摘要助手。":
            return {"status_code": 200, "body": '{"choices":[{"message":{"content":"早前对话摘要"}}]}'}
        calls.append(body)
        if len(calls) == 1:
            return {"status_code": 200, "body": '{"choices":[{"message":{"content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"akm_compact_context","arguments":"{}"}}]}}]}'}
        return {"status_code": 200, "body": '{"choices":[{"message":{"content":"完成"}}]}'}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    loop = AgentLoop(http_client=None, tool_registry=ToolRegistry())
    result = await loop.run(_long_cjk_message(100))

    assert result.ok is True
    # 第二轮请求里，早期历史应被一条 system 摘要替换，且 AI 压缩结果工具消息完整保留
    second_messages = calls[1]["messages"]
    assert second_messages[0]["role"] == "system"
    assert "摘要" in second_messages[0]["content"]
    tool_msg = [m for m in second_messages if m.get("role") == "tool"]
    assert tool_msg, "第二轮应包含 AI 压缩工具的结果消息"
    assert json.loads(tool_msg[-1]["content"])["compacted"] is True
    assert result.compacted >= 1


@pytest.mark.asyncio
async def test_run_akm_context_status_reports_estimate(monkeypatch):
    """akm_context_status 工具应返回当前上下文的 token 估算与剩余空间。"""
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", _small_config)
    calls = []

    async def forward(body, *_args, **_kwargs):
        if body["messages"][0].get("content") == "你是一个对话摘要助手。":
            return {"status_code": 200, "body": '{"choices":[{"message":{"content":"摘要"}}]}'}
        calls.append(body)
        if len(calls) == 1:
            return {"status_code": 200, "body": '{"choices":[{"message":{"content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"akm_context_status","arguments":"{}"}}]}}]}'}
        return {"status_code": 200, "body": '{"choices":[{"message":{"content":"完成"}}]}'}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    loop = AgentLoop(http_client=None, tool_registry=ToolRegistry())
    result = await loop.run(_long_cjk_message(10))

    assert result.ok is True
    tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    payload = json.loads(tool_msgs[0]["content"])
    assert "estimated_tokens" in payload
    assert payload["max_tokens"] == 60
    assert payload["remaining_tokens"] >= 0


@pytest.mark.asyncio
async def test_run_auto_compact_fallback_still_active(monkeypatch):
    """即使 AI 未主动压缩，超限时服务端自动压缩兜底仍应触发（compacted 计数）。"""
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", _small_config)
    registry = ToolRegistry()
    registry.register(ToolDef("get_weather", "天气", {"type": "object"}, lambda city: {"city": city, "temp": 25}))
    calls = []

    async def forward(body, *_args, **_kwargs):
        if body["messages"][0].get("content") == "你是一个对话摘要助手。":
            return {"status_code": 200, "body": '{"choices":[{"message":{"content":"摘要"}}]}'}
        calls.append(body)
        if len(calls) == 1:
            return {"status_code": 200, "body": '{"choices":[{"message":{"content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"get_weather","arguments":"{\\"city\\":\\"beijing\\"}"}}]}}]}'}
        return {"status_code": 200, "body": '{"choices":[{"message":{"content":"完成"}}]}'}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    loop = AgentLoop(http_client=None, tool_registry=registry)
    result = await loop.run(_long_cjk_message(100))

    assert result.ok is True
    assert result.compacted >= 1


@pytest.mark.asyncio
async def test_run_stream_emits_context_warning_when_over_ratio(monkeypatch):
    """上下文占用超过上限的 agent_context_warning_ratio 时，应下发 context_warning 事件。"""
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", _small_config)
    response = FakeStreamResponse(200, [_sse({"choices": [{"delta": {"content": "好了"}}]}), "data: [DONE]\n\n"])

    async def forward(*_args, **_kwargs):
        return {"stream": True, "response": response, "status_code": 200}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    loop = AgentLoop(http_client=None, tool_registry=ToolRegistry())
    events = _events([item async for item in loop.run_stream(_long_cjk_message(100))])

    warning_events = [e for e in events if e["event"] == "context_warning"]
    assert warning_events, "应至少下发一次 context_warning 事件"
    data = warning_events[0]["data"]
    assert data["estimated_tokens"] > 0
    assert data["max_tokens"] == 60
    assert data["remaining_tokens"] >= 0
    assert "ratio" in data


@pytest.mark.asyncio
async def test_run_passes_request_workspace_root_to_tools(monkeypatch):
    """请求级 workspace_root 应在工具执行期间生效，让工具看到请求指定的工作区。"""
    from akm.agent_runtime.tools import _workspace_root

    ToolRegistry.reset()
    registry = ToolRegistry.instance()
    captured: dict = {}

    def probe_ws():
        captured["root"] = str(_workspace_root())
        return "ok"

    registry.register(ToolDef("probe_ws", "探针", {"type": "object"}, probe_ws))

    first = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "probe_ws", "arguments": "{}"}}]}}], "usage": {}}
    second = {"choices": [{"message": {"content": "完成", "tool_calls": None}}], "usage": {}}
    responses = iter([
        {"status_code": 200, "body": json.dumps(first), "provider": "t", "key_alias": "k"},
        {"status_code": 200, "body": json.dumps(second), "provider": "t", "key_alias": "k"},
    ])

    async def forward(*_args, **_kwargs):
        return next(responses)

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", lambda: {})
    monkeypatch.setattr("akm.agent_runtime.tools.load_config", lambda: {"agent_workspace_root": "/global/ws"})
    loop = AgentLoop(http_client=None, tool_registry=registry)

    result = await loop.run([{"role": "user", "content": "hi"}], workspace_root="/req/ws")

    assert result.ok is True
    assert captured["root"] == str(Path("/req/ws").resolve())


@pytest.mark.asyncio
async def test_run_stream_passes_request_workspace_root_to_tools(monkeypatch):
    """流式模式同样应在工具执行期间注入请求级 workspace_root。"""
    from akm.agent_runtime.tools import _workspace_root

    ToolRegistry.reset()
    registry = ToolRegistry.instance()
    captured: dict = {}

    def probe_ws():
        captured["root"] = str(_workspace_root())
        return "ok"

    registry.register(ToolDef("probe_ws", "探针", {"type": "object"}, probe_ws))

    first = FakeStreamResponse(200, [
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "probe_ws", "arguments": "{}"}}]}}]}),
    ])
    second = FakeStreamResponse(200, [_sse({"choices": [{"delta": {"content": "完成"}}]}), "data: [DONE]\n\n"])
    responses = iter([first, second])

    async def forward(*_args, **_kwargs):
        return {"stream": True, "response": next(responses), "status_code": 200}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", lambda: {})
    monkeypatch.setattr("akm.agent_runtime.tools.load_config", lambda: {"agent_workspace_root": "/global/ws"})
    loop = AgentLoop(http_client=None, tool_registry=registry)

    events = _events([item async for item in loop.run_stream([{"role": "user", "content": "hi"}], workspace_root="/req/ws")])

    assert events[-1]["event"] == "final"
    assert captured["root"] == str(Path("/req/ws").resolve())


@pytest.mark.asyncio
async def test_run_self_healing_retry_injects_correction(monkeypatch):
    """工具返回 error 时应注入 system 修正提示并强制模型再试。"""
    ToolRegistry.reset()
    registry = ToolRegistry.instance()

    def failing_tool():
        return json.dumps({"error": "old_string 未在文件中找到"})

    registry.register(ToolDef("akm_failing", "故障探针", {"type": "object"}, failing_tool))

    first = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "akm_failing", "arguments": "{}"}}]}}], "usage": {}}
    second = {"choices": [{"message": {"content": "已修正完成", "tool_calls": None}}], "usage": {}}
    responses = iter([
        {"status_code": 200, "body": json.dumps(first), "provider": "t", "key_alias": "k"},
        {"status_code": 200, "body": json.dumps(second), "provider": "t", "key_alias": "k"},
    ])
    calls = []

    async def forward(body, *_args, **_kwargs):
        # body["messages"] 引用的是正在被不断追加的 working_messages，
        # 需深拷贝快照，否则后续轮次的追加会污染已保存的请求记录
        calls.append(json.loads(json.dumps(body)))
        return next(responses)

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", lambda: {"agent_tool_retry_max_retries": 1})
    loop = AgentLoop(http_client=None, tool_registry=registry)

    result = await loop.run([{"role": "user", "content": "改一下"}])

    assert result.ok is True
    assert len(calls) == 2
    # 第二轮请求末尾应包含注入的 system 修正提示
    assert calls[1]["messages"][-1]["role"] == "system"
    assert "上一步工具调用失败" in calls[1]["messages"][-1]["content"]
    assert "old_string 未在文件中找到" in calls[1]["messages"][-1]["content"]
    ToolRegistry.reset()


@pytest.mark.asyncio
async def test_run_self_healing_respects_max_retries(monkeypatch):
    """超过修正上限后不再注入提示，错误结果照常回传由模型自主决定。"""
    ToolRegistry.reset()
    registry = ToolRegistry.instance()

    def failing_tool():
        return json.dumps({"error": "总是失败"})

    registry.register(ToolDef("akm_failing", "故障探针", {"type": "object"}, failing_tool))

    first = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "akm_failing", "arguments": "{}"}}]}}], "usage": {}}
    done = {"choices": [{"message": {"content": "放弃", "tool_calls": None}}], "usage": {}}
    responses = iter([
        {"status_code": 200, "body": json.dumps(first), "provider": "t", "key_alias": "k"},
        {"status_code": 200, "body": json.dumps(first), "provider": "t", "key_alias": "k"},
        {"status_code": 200, "body": json.dumps(done), "provider": "t", "key_alias": "k"},
    ])
    calls = []

    async def forward(body, *_args, **_kwargs):
        calls.append(json.loads(json.dumps(body)))
        return next(responses)

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", lambda: {"agent_tool_retry_max_retries": 1})
    loop = AgentLoop(http_client=None, tool_registry=registry)

    result = await loop.run([{"role": "user", "content": "hi"}])

    assert result.ok is True
    assert len(calls) == 3
    # 全程只注入了一次修正提示（第一轮失败时）
    system_msgs = [
        m for m in calls[2]["messages"]
        if m.get("role") == "system" and "上一步工具调用失败" in str(m.get("content", ""))
    ]
    assert len(system_msgs) == 1
    ToolRegistry.reset()


@pytest.mark.asyncio
async def test_run_stream_self_healing_emits_tool_retry(monkeypatch):
    """流式路径工具失败应下发 tool_retry 事件并注入修正提示。"""
    ToolRegistry.reset()
    registry = ToolRegistry.instance()

    def failing_tool():
        return json.dumps({"error": "命令包含非法字符"})

    registry.register(ToolDef("akm_failing", "故障探针", {"type": "object"}, failing_tool))

    first = FakeStreamResponse(200, [
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "akm_failing", "arguments": "{}"}}]}}]}),
    ])
    second = FakeStreamResponse(200, [_sse({"choices": [{"delta": {"content": "好了"}}]}), "data: [DONE]\n\n"])
    responses = iter([first, second])
    calls = []

    async def forward(body, *_args, **_kwargs):
        calls.append(json.loads(json.dumps(body)))
        return {"stream": True, "response": next(responses), "status_code": 200}

    monkeypatch.setattr("akm.proxy.forward_request", forward)
    monkeypatch.setattr("akm.agent_runtime.loop.load_config", lambda: {"agent_tool_retry_max_retries": 1})
    loop = AgentLoop(http_client=None, tool_registry=registry)

    events = _events([item async for item in loop.run_stream([{"role": "user", "content": "hi"}])])

    retry_events = [e for e in events if e["event"] == "tool_retry"]
    assert retry_events, "应下发 tool_retry 事件"
    assert retry_events[0]["data"]["error"] == "命令包含非法字符"
    assert retry_events[0]["data"]["retry_count"] == 1
    assert retry_events[0]["data"]["max_retries"] == 1
    # 第二轮请求末尾是注入的 system 修正提示
    assert calls[1]["messages"][-1]["role"] == "system"
    assert "上一步工具调用失败" in calls[1]["messages"][-1]["content"]
    assert events[-1]["event"] == "final"
    ToolRegistry.reset()
