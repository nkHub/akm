"""ChatToResponsesAdapter 和 MessagesToChatAdapter 单元测试"""

import json
import pytest
from akm.adapters.chat_to_responses import ChatToResponsesAdapter
from akm.adapters.anthropic_messages import MessagesToChatAdapter


# ═══════════════════════════════════════
# ChatToResponsesAdapter
# ═══════════════════════════════════════

class TestChatToResponsesRequest:

    def test_convert_simple_string_input(self):
        adapter = ChatToResponsesAdapter()
        body = {
            "model": "deepseek-v4-pro",
            "input": "Hello",
            "instructions": "You are helpful",
            "stream": True,
        }
        result = adapter.convert_request(body)
        assert result["model"] == "deepseek-v4-pro"
        assert result["stream"] is True
        assert len(result["messages"]) == 2
        assert result["messages"][0] == {"role": "system", "content": "You are helpful"}
        assert result["messages"][1] == {"role": "user", "content": "Hello"}

    def test_convert_no_instructions(self):
        adapter = ChatToResponsesAdapter()
        body = {"model": "test", "input": "hi"}
        result = adapter.convert_request(body)
        assert len(result["messages"]) == 1
        assert result["messages"][0] == {"role": "user", "content": "hi"}

    def test_convert_array_input(self):
        adapter = ChatToResponsesAdapter()
        body = {
            "model": "test",
            "input": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        }
        result = adapter.convert_request(body)
        assert len(result["messages"]) == 2
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][1]["role"] == "assistant"

    def test_convert_input_with_nested_content(self):
        adapter = ChatToResponsesAdapter()
        body = {
            "model": "test",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "part1"},
                        {"type": "input_text", "text": "part2"},
                    ]
                }
            ],
        }
        result = adapter.convert_request(body)
        assert result["messages"][0]["content"] == "part1\npart2"

    def test_convert_function_call(self):
        adapter = ChatToResponsesAdapter()
        body = {
            "model": "test",
            "input": [{
                "type": "function_call",
                "call_id": "call_1",
                "name": "get_weather",
                "arguments": '{"city": "NYC"}',
            }],
        }
        result = adapter.convert_request(body)
        assert result["messages"][0]["role"] == "assistant"
        assert result["messages"][0]["tool_calls"][0]["function"]["name"] == "get_weather"

    def test_convert_function_call_output(self):
        adapter = ChatToResponsesAdapter()
        body = {
            "model": "test",
            "input": [{
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Sunny",
            }],
        }
        result = adapter.convert_request(body)
        assert result["messages"][0]["role"] == "tool"
        assert result["messages"][0]["content"] == "Sunny"

    def test_convert_input_text_type(self):
        adapter = ChatToResponsesAdapter()
        body = {"model": "test", "input": [{"type": "input_text", "text": "hello"}]}
        result = adapter.convert_request(body)
        assert result["messages"][0] == {"role": "user", "content": "hello"}

    def test_convert_max_output_tokens_mapping(self):
        adapter = ChatToResponsesAdapter()
        body = {"model": "test", "input": "hi", "max_output_tokens": 4096}
        result = adapter.convert_request(body)
        assert result["max_tokens"] == 4096

    def test_convert_tools_passthrough(self):
        adapter = ChatToResponsesAdapter()
        tools = [{"type": "function", "function": {"name": "search"}}]
        body = {"model": "test", "input": "hi", "tools": tools}
        result = adapter.convert_request(body)
        assert result["tools"] == tools


class TestChatToResponsesResponse:

    def test_convert_response(self):
        adapter = ChatToResponsesAdapter()
        chat_resp = json.dumps({
            "id": "chatcmpl-abc123",
            "model": "gpt-4o",
            "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })
        result = json.loads(adapter.convert_response(chat_resp))
        assert result["id"] == "resp_abc123"
        assert result["object"] == "response"
        assert result["model"] == "gpt-4o"
        assert result["status"] == "completed"
        assert result["output"][0]["content"][0]["text"] == "Hello!"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5


class TestChatToResponsesSSE:

    @pytest.mark.asyncio
    async def test_convert_sse_basic(self):
        adapter = ChatToResponsesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"gpt-4","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","model":"gpt-4","choices":[{"delta":{"content":"Hello"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","model":"gpt-4","choices":[{"delta":{"content":" world"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = self._parse_sse(lines)
        event_names = [e[0] for e in events]
        assert "response.created" in event_names
        assert "response.output_item.added" in event_names
        assert "response.content_part.added" in event_names
        assert event_names.count("response.output_text.delta") == 2

        # 最后的 completed 事件包含 usage
        completed = events[-1]
        assert completed[0] == "response.completed"
        assert completed[1]["response"]["usage"]["input_tokens"] == 10

    def _parse_sse(self, lines):
        return _parse_sse_helper(lines)


class TestMessagesToChatRequest:

    def test_convert_simple(self):
        adapter = MessagesToChatAdapter()
        body = {
            "model": "claude-sonnet",
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }
        result = adapter.convert_request(body)
        assert result["model"] == "claude-sonnet"
        assert result["stream"] is True
        assert result["messages"][0] == {"role": "system", "content": "You are helpful"}
        assert result["messages"][1] == {"role": "user", "content": "Hi"}

    def test_convert_system_as_content_blocks(self):
        adapter = MessagesToChatAdapter()
        body = {
            "model": "claude",
            "system": [
                {"type": "text", "text": "You are helpful"},
                {"type": "text", "text": "Be concise"},
            ],
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = adapter.convert_request(body)
        assert result["messages"][0]["content"] == "You are helpful\nBe concise"

    def test_convert_content_list_with_image(self):
        adapter = MessagesToChatAdapter()
        body = {
            "model": "claude",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "image", "source": None},
                ]
            }],
        }
        result = adapter.convert_request(body)
        assert result["messages"][0]["content"] == "What is this?\n[image]"

    def test_convert_stop_sequences(self):
        adapter = MessagesToChatAdapter()
        body = {"model": "claude", "messages": [], "stop_sequences": ["END"]}
        result = adapter.convert_request(body)
        assert result["stop"] == ["END"]

    def test_convert_max_tokens_temperature(self):
        adapter = MessagesToChatAdapter()
        body = {"model": "claude", "messages": [], "max_tokens": 1000, "temperature": 0.7, "top_p": 0.9}
        result = adapter.convert_request(body)
        assert result["max_tokens"] == 1000
        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.9


class TestMessagesToChatResponse:

    def test_convert_response(self):
        adapter = MessagesToChatAdapter()
        chat_resp = json.dumps({
            "id": "chatcmpl-xyz",
            "model": "claude-sonnet",
            "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })
        result = json.loads(adapter.convert_response(chat_resp))
        assert result["id"] == "chatcmpl-xyz"
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert result["content"][0]["text"] == "Hello!"
        assert result["stop_reason"] == "end_turn"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5


class TestMessagesToChatSSE:

    @pytest.mark.asyncio
    async def test_convert_sse_basic(self):
        adapter = MessagesToChatAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"claude","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"end_turn"}],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = self._parse_sse(lines)
        event_names = [e[0] for e in events]
        assert event_names[0] == "message_start"
        assert event_names[1] == "content_block_start"
        assert event_names[2] == "content_block_delta"
        assert event_names[3] == "content_block_stop"
        assert event_names[4] == "message_delta"
        assert event_names[5] == "message_stop"

    def _parse_sse(self, lines):
        return _parse_sse_helper(lines)


# ── 工具函数 ──

async def _async_bytes_iter(items):
    for item in items:
        yield item


def _parse_sse_helper(lines):
    """将 SSE 文本行列表（每项可能是多行的完整事件）解析为 [(event_name, data_dict), ...]"""
    # 先按 \n 拆成单行
    flat = []
    for item in lines:
        flat.extend(item.split("\n"))
    events = []
    i = 0
    while i < len(flat):
        line = flat[i].strip()
        if line.startswith("data: [DONE]"):
            break
        if line.startswith("event: "):
            event_name = line[7:]
            i += 1
            if i < len(flat) and flat[i].startswith("data: "):
                try:
                    data = json.loads(flat[i][6:])
                except json.JSONDecodeError:
                    data = flat[i][6:]
                events.append((event_name, data))
        i += 1
    return events
