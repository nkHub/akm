import json

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
    """Chat SSE 必须转换为 model_delta，不能把 choices 等上游字段直接交给客户端。"""
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

    assert [event["event"] for event in events] == ["model_delta", "model_delta", "final"]
    assert [event["data"]["content"] for event in events[:2]] == ["你", "好"]
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

    assert events == [{"event": "error", "data": {"error": "rate limited", "turns": 1, "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}}]
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
    assert requests[0]["tools"] == [registry.list_tools()[0]]


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
