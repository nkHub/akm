"""ResponsesAdapter 和 MessagesAdapter 单元测试"""
import json
from pathlib import Path
import pytest
from akm.plugins.protocol_converter._responses import ResponsesAdapter
from akm.plugins.protocol_converter._messages import MessagesAdapter
from akm.plugins.protocol_converter._chat import ChatAdapter
from akm.plugins.protocol_converter._provider_profile import get_provider_profile
from akm.plugins.protocol_converter.index import Plugin
from akm.plugins.protocol_converter._warnings import (
    RESPONSES_INCLUDE_NOT_FULLY_MAPPED,
    RESPONSES_STORE_NOT_MAPPED,
    RESPONSES_REASONING_SUMMARY_NOT_MAPPED,
    RESPONSES_PARALLEL_TOOL_CALLS_NOT_MAPPED,
)


# ═══════════════════════════════════════
# ResponsesAdapter — 请求转换
# ═══════════════════════════════════════

class TestResponsesAdapterRequest:

    def test_convert_simple_string_input(self):
        adapter = ResponsesAdapter()
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
        adapter = ResponsesAdapter()
        body = {"model": "test", "input": "hi"}
        result = adapter.convert_request(body)
        assert len(result["messages"]) == 1
        assert result["messages"][0] == {"role": "user", "content": "hi"}

    def test_convert_array_input(self):
        adapter = ResponsesAdapter()
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
        adapter = ResponsesAdapter()
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
        adapter = ResponsesAdapter()
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

    def test_convert_multiple_function_calls_merged(self):
        """验证多个连续 function_call 被合并为一条 assistant(tool_calls) 消息"""
        adapter = ResponsesAdapter()
        body = {
            "model": "test",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "bash",
                    "arguments": '{"cmd":"ls"}',
                },
                {
                    "type": "function_call",
                    "call_id": "call_2",
                    "name": "read_file",
                    "arguments": '{"path":"/tmp/x"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "result1",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_2",
                    "output": "result2",
                },
            ],
        }
        result = adapter.convert_request(body)
        # 应只有 1 条 assistant 消息，包含 2 个 tool_calls
        assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert len(assistant_msgs[0]["tool_calls"]) == 2
        assert assistant_msgs[0]["tool_calls"][0]["function"]["name"] == "bash"
        assert assistant_msgs[0]["tool_calls"][1]["function"]["name"] == "read_file"
        # tool 结果消息应紧跟 assistant
        tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
        assert len(tool_msgs) == 2

    def test_convert_function_call_with_reasoning(self):
        """验证 function_call 上的 reasoning_content 被回写到 assistant 消息"""
        adapter = ResponsesAdapter()
        body = {
            "model": "test",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "bash",
                    "arguments": "{}",
                    "reasoning_content": "This is the thinking process",
                },
            ],
        }
        result = adapter.convert_request(body)
        assistant = result["messages"][0]
        assert assistant["reasoning_content"] == "This is the thinking process"

    def test_convert_function_call_output(self):
        adapter = ResponsesAdapter()
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

    def test_convert_function_call_output_object_is_serialized(self):
        adapter = ResponsesAdapter()
        body = {
            "model": "test",
            "input": [{
                "type": "function_call_output",
                "call_id": "call_1",
                "output": {"weather": "Sunny", "temp": 26},
            }],
        }
        result = adapter.convert_request(body)
        assert result["messages"][0]["role"] == "tool"
        assert result["messages"][0]["content"] == '{"weather": "Sunny", "temp": 26}'

    def test_convert_input_text_type(self):
        adapter = ResponsesAdapter()
        body = {"model": "test", "input": [{"type": "input_text", "text": "hello"}]}
        result = adapter.convert_request(body)
        assert result["messages"][0] == {"role": "user", "content": "hello"}

    def test_convert_max_output_tokens_mapping(self):
        adapter = ResponsesAdapter()
        body = {"model": "test", "input": "hi", "max_output_tokens": 4096}
        result = adapter.convert_request(body)
        assert result["max_tokens"] == 4096

    def test_convert_request_deepseek_profile_injects_thinking_defaults(self):
        """验证 deepseek profile 会为 Responses -> Chat 补 thinking/reasoning 默认值。"""
        adapter = ResponsesAdapter()
        adapter._request_provider = "deepseek"
        body = {"model": "deepseek-v4", "input": "hi"}
        result = adapter.convert_request(body)
        assert result["thinking"] == {"type": "enabled"}
        assert result["reasoning_effort"] == "high"

    def test_convert_request_openai_profile_does_not_inject_deepseek_defaults(self):
        """验证 openai profile 不会在 Responses -> Chat 链路强塞 DeepSeek 默认 thinking。"""
        adapter = ResponsesAdapter()
        adapter._request_provider = "openai"
        body = {"model": "gpt-5", "input": "hi"}
        result = adapter.convert_request(body)
        assert "thinking" not in result
        assert "reasoning_effort" not in result

    def test_convert_request_explicit_thinking_and_effort_override_profile_defaults(self):
        """验证显式传入的 thinking / reasoning_effort 优先于 provider profile 默认值。"""
        adapter = ResponsesAdapter()
        adapter._request_provider = "deepseek"
        body = {
            "model": "deepseek-v4",
            "input": "hi",
            "thinking": {"type": "disabled"},
            "reasoning_effort": "max",
        }
        result = adapter.convert_request(body)
        assert result["thinking"] == {"type": "disabled"}
        assert result["reasoning_effort"] == "max"

    def test_convert_tools_passthrough(self):
        adapter = ResponsesAdapter()
        tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
        body = {"model": "test", "input": "hi", "tools": tools}
        result = adapter.convert_request(body)
        assert result["tools"] == tools

    def test_convert_request_response_format_cleans_schema(self):
        adapter = ResponsesAdapter()
        body = {
            "model": "test",
            "input": "hi",
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "person",
                    "schema": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                        "strict": True,
                    },
                },
            },
        }
        result = adapter.convert_request(body)
        assert result["response_format"]["type"] == "json_schema"
        assert result["response_format"]["json_schema"]["name"] == "person"
        schema = result["response_format"]["json_schema"]["schema"]
        assert "additionalProperties" not in schema
        assert "strict" not in schema

    def test_convert_request_text_format_to_response_format_cleans_schema(self):
        adapter = ResponsesAdapter()
        body = {
            "model": "test",
            "input": "hi",
            "text": {
                "format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "obj",
                        "schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                            "strict": True,
                        },
                    },
                }
            },
        }
        result = adapter.convert_request(body)
        assert result["response_format"]["type"] == "json_schema"
        assert result["response_format"]["json_schema"]["name"] == "obj"
        schema = result["response_format"]["json_schema"]["schema"]
        assert "additionalProperties" not in schema
        assert "strict" not in schema

    def test_convert_request_metadata_and_previous_response_id_passthrough(self):
        adapter = ResponsesAdapter()
        body = {
            "model": "test",
            "input": "hi",
            "metadata": {"trace_id": "t-1", "scene": "unit-test"},
            "previous_response_id": "resp_prev_123",
        }
        result = adapter.convert_request(body)
        assert result["metadata"]["trace_id"] == "t-1"
        assert result["previous_response_id"] == "resp_prev_123"

    def test_convert_modern_tool_message_continuation(self):
        adapter = ResponsesAdapter()
        body = {
            "model": "test",
            "input": [{
                "type": "message",
                "role": "tool",
                "tool_call_id": "call_1",
                "content": [{"type": "output", "body": {"ok": True}}],
            }],
        }
        result = adapter.convert_request(body)
        assert result["messages"][0]["role"] == "tool"
        assert result["messages"][0]["tool_call_id"] == "call_1"
        assert result["messages"][0]["content"] == '{"ok": true}'

    def test_convert_request_sets_conversion_warnings_for_unmapped_fields(self):
        adapter = ResponsesAdapter()
        body = {
            "model": "test",
            "input": "hi",
            "include": ["reasoning.encrypted_content"],
            "store": False,
            "reasoning": {"summary": "concise"},
            "parallel_tool_calls": True,
        }
        adapter.convert_request(body)
        warns = getattr(adapter, "_conversion_warnings", [])
        assert RESPONSES_INCLUDE_NOT_FULLY_MAPPED in warns
        assert RESPONSES_STORE_NOT_MAPPED in warns
        assert RESPONSES_REASONING_SUMMARY_NOT_MAPPED in warns
        assert RESPONSES_PARALLEL_TOOL_CALLS_NOT_MAPPED in warns


class TestResponsesAdapterResponse:

    def test_convert_response_from_chat_sse_text(self):
        """验证 responses 非流式路径可直接消费 Chat SSE 原文并完成聚合转换"""
        adapter = ResponsesAdapter()
        sse_text = (
            'data: {"id":"chatcmpl-1","model":"gpt-5","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"},"index":0}]}\n\n'
            'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
            'data: [DONE]\n\n'
        )
        result = json.loads(adapter.convert_response(sse_text))
        assert result["object"] == "response"
        assert result["model"] == "gpt-5"
        assert result["output"][0]["content"][0]["text"] == "Hello"
        assert result["usage"]["input_tokens"] == 3
        assert result["usage"]["output_tokens"] == 2
        assert result["usage"]["total_tokens"] == 5

    def test_clean_schema_removes_additional_properties(self):
        """验证 _clean_schema 递归移除 additionalProperties 字段"""
        adapter = ResponsesAdapter()
        params = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "nested": {
                    "type": "object",
                    "properties": {"deep": {"type": "string"}},
                    "additionalProperties": True,
                },
            },
            "additionalProperties": False,
        }
        result = adapter._clean_schema(params)
        assert "additionalProperties" not in result
        assert "additionalProperties" not in result["properties"]["nested"]
        assert result["type"] == "object"
        assert result["properties"]["name"]["type"] == "string"

    def test_clean_schema_removes_strict(self):
        """验证 _clean_schema 移除 strict 字段"""
        adapter = ResponsesAdapter()
        params = {"type": "object", "strict": True, "properties": {}}
        result = adapter._clean_schema(params)
        assert "strict" not in result
        assert "type" in result

    def test_convert_tools_with_strict_removed(self):
        """验证 convert_request 中 tools 的 strict 和 additionalProperties 被清理"""
        adapter = ResponsesAdapter()
        tools = [{
            "type": "function",
            "name": "search",
            "description": "search something",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "additionalProperties": False,
                "strict": True,
            }
        }]
        body = {"model": "test", "input": "hi", "tools": tools}
        result = adapter.convert_request(body)
        # 扁平格式 tools → function wrapper 格式
        func = result["tools"][0]["function"]
        params = func["parameters"]
        assert "additionalProperties" not in params
        assert "strict" not in func  # strict 在 function 层级也不应有

    def test_convert_namespace_tools_expansion(self):
        """验证 namespace 类型工具递归展开子工具，名称加命名空间前缀"""
        adapter = ResponsesAdapter()
        tools = [
            {
                "type": "function",
                "name": "exec_command",
                "description": "执行命令",
                "parameters": {"type": "object", "properties": {}}
            },
            {
                "type": "namespace",
                "name": "mcp__translate__",
                "description": "翻译 MCP 工具",
                "tools": [
                    {
                        "type": "function",
                        "name": "translate",
                        "description": "翻译文本",
                        "strict": False,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "需要翻译的文本"}
                            },
                            "required": ["text"]
                        }
                    },
                    {
                        "name": "detect_language",
                        "description": "检测语言",
                        "strict": False,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "需要检测的文本"}
                            },
                            "required": ["text"]
                        }
                    }
                ]
            }
        ]
        result = adapter._convert_tools(tools)
        # 应该有 3 个工具：1 个普通 + 2 个展开的 MCP 子工具
        assert len(result) == 3

        # 普通工具不变
        assert result[0]["function"]["name"] == "exec_command"

        # MCP 子工具 1（function 格式）：名称加前缀
        assert result[1]["function"]["name"] == "mcp__translate__translate"
        assert result[1]["function"]["description"] == "翻译文本"
        params1 = result[1]["function"]["parameters"]
        assert "text" in params1.get("properties", {})
        assert "additionalProperties" not in params1

        # MCP 子工具 2（简洁格式）：名称加前缀，包装为 function 格式
        assert result[2]["function"]["name"] == "mcp__translate__detect_language"
        assert result[2]["function"]["description"] == "检测语言"
        params2 = result[2]["function"]["parameters"]
        assert "text" in params2.get("properties", {})

    def test_convert_empty_namespace_skipped(self):
        """验证空的 namespace（无 tools）被跳过"""
        adapter = ResponsesAdapter()
        tools = [
            {"type": "namespace", "name": "empty", "tools": []},
            {"type": "function", "name": "real_tool", "parameters": {}}
        ]
        result = adapter._convert_tools(tools)
        assert len(result) == 1
        assert result[0]["function"]["name"] == "real_tool"

    def test_convert_namespace_with_nested_function_cleaning(self):
        """验证 namespace 子工具的 strict 和 additionalProperties 被清理"""
        adapter = ResponsesAdapter()
        tools = [{
            "type": "namespace",
            "name": "mcp__test__",
            "tools": [{
                "name": "foo",
                "description": "test tool",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {"bar": {"type": "string"}}
                }
            }]
        }]
        result = adapter._convert_tools(tools)
        assert len(result) == 1
        params = result[0]["function"]["parameters"]
        assert "additionalProperties" not in params

    def test_message_reordering_system_between_tool_calls(self):
        """验证 system 消息被移到 assistant(tool_calls) 之前，tool 结果紧跟 assistant"""
        adapter = ResponsesAdapter()
        # 模拟 Codex 在 tool_calls 和 tool 结果之间注入的 system 消息
        body = {
            "model": "test",
            "input": [
                {"type": "input_text", "text": "run this"},
                {
                    "type": "function_call",
                    "call_id": "call_a",
                    "name": "bash",
                    "arguments": "{}",
                },
                # Codex 可能在这里注入 system 消息（如审批通知）
                {"role": "system", "content": "[approval required]"},
                {
                    "type": "function_call_output",
                    "call_id": "call_a",
                    "output": "result",
                },
            ],
        }
        result = adapter.convert_request(body)
        # 获取消息顺序
        roles = [m["role"] for m in result["messages"]]
        # system 应该在 assistant 之前（不是 tool 和 assistant 之间）
        sys_idx = roles.index("system")
        assistant_idx = roles.index("assistant")
        tool_idx = roles.index("tool")
        assert sys_idx < assistant_idx  # system 移到 assistant 之前
        assert assistant_idx + 1 == tool_idx  # tool 紧跟 assistant

    def test_message_type_with_tool_calls(self):
        """验证 message 类型中嵌套 tool_call content block 的正确处理"""
        adapter = ResponsesAdapter()
        body = {
            "model": "test",
            "input": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Let me run this"},
                        {"type": "tool_call", "id": "call_x", "name": "bash", "arguments": "{}"},
                    ],
                    "reasoning_content": "thinking...",
                },
            ],
        }
        result = adapter.convert_request(body)
        msg = result["messages"][0]
        assert msg["role"] == "assistant"
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "bash"
        assert msg["reasoning_content"] == "thinking..."
        assert "Let me run this" in msg["content"]


# ═══════════════════════════════════════
# ResponsesAdapter — 响应转换
# ═══════════════════════════════════════

class TestResponsesAdapterResponse:

    def test_convert_response(self):
        adapter = ResponsesAdapter()
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

    def test_convert_response_preserves_cached_tokens(self):
        adapter = ResponsesAdapter()
        chat_resp = json.dumps({
            "id": "chatcmpl-cache1",
            "model": "deepseek-v4",
            "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 8,
                "total_tokens": 128,
                "prompt_tokens_details": {"cached_tokens": 96},
            },
        })
        result = json.loads(adapter.convert_response(chat_resp))
        assert result["usage"]["input_tokens"] == 120
        assert result["usage"]["output_tokens"] == 8
        assert result["usage"]["input_tokens_details"]["cached_tokens"] == 96

    def test_convert_response_preserves_tool_calls_as_function_call_items(self):
        adapter = ResponsesAdapter()
        chat_resp = json.dumps({
            "id": "chatcmpl-tool1",
            "model": "gpt-5",
            "choices": [{
                "message": {
                    "content": "I will call a tool",
                    "tool_calls": [{
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        })
        result = json.loads(adapter.convert_response(chat_resp))
        fc_items = [x for x in result["output"] if x.get("type") == "function_call"]
        assert len(fc_items) == 1
        assert fc_items[0]["call_id"] == "call_abc"
        assert fc_items[0]["name"] == "bash"
        assert fc_items[0]["arguments"] == '{"command":"ls"}'

    def test_convert_response_preserves_reasoning_item(self):
        adapter = ResponsesAdapter()
        chat_resp = json.dumps({
            "id": "chatcmpl-rsn1",
            "model": "gpt-5",
            "choices": [{
                "message": {
                    "content": "final answer",
                    "reasoning_content": "step by step analysis",
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
        })
        result = json.loads(adapter.convert_response(chat_resp))
        reasoning_items = [x for x in result["output"] if x.get("type") == "reasoning"]
        assert len(reasoning_items) == 1
        assert reasoning_items[0]["summary"][0]["text"] == "step by step analysis"


# ═══════════════════════════════════════
# ResponsesAdapter — SSE 流式转换
# ═══════════════════════════════════════

class TestResponsesAdapterSSE:

    @pytest.mark.asyncio
    async def test_convert_sse_basic(self):
        adapter = ResponsesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"gpt-4","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","model":"gpt-4","choices":[{"delta":{"content":"Hello"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","model":"gpt-4","choices":[{"delta":{"content":" world"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)
        event_names = [e[0] for e in events]
        assert "response.created" in event_names
        assert "response.in_progress" in event_names
        assert "response.output_item.added" in event_names
        assert "response.content_part.added" in event_names
        assert event_names.count("response.output_text.delta") == 2

        # 验证序列号和 item_id
        deltas = [e for e in events if e[0] == "response.output_text.delta"]
        assert deltas[0][1]["sequence_number"] == 1
        assert deltas[1][1]["sequence_number"] == 2
        assert "item_id" in deltas[0][1]

        # response.output_text.done 应在 content_part.done 之前
        done_names = [e[0] for e in events if "done" in e[0]]
        assert "response.output_text.done" in done_names

        # 最后的 completed 事件包含 usage
        completed = [e for e in events if e[0] == "response.completed"][0]
        assert completed[0] == "response.completed"
        assert completed[1]["response"]["usage"]["input_tokens"] == 10

    @pytest.mark.asyncio
    async def test_convert_sse_with_reasoning(self):
        """验证 DeepSeek reasoning_content 映射为独立的 reasoning_summary_text.delta 事件"""
        adapter = ResponsesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"role":"assistant","reasoning_content":""},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"reasoning_content":"Now"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"reasoning_content":" processing..."},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"content":"Hello world"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)

        # reasoning 内容走 reasoning_summary_text.delta，不走 output_text.delta
        reasoning_deltas = [e[1]["delta"] for e in events if e[0] == "response.reasoning_summary_text.delta"]
        assert len(reasoning_deltas) == 2
        assert reasoning_deltas == ["Now", " processing..."]

        # 正文走 output_text.delta
        output_deltas = [e[1]["delta"] for e in events if e[0] == "response.output_text.delta"]
        assert len(output_deltas) == 1
        assert output_deltas == ["Hello world"]

        # content_part.done 只包含正文
        done_event = [e for e in events if e[0] == "response.content_part.done"][0]
        assert done_event[1]["part"]["text"] == "Hello world"

        # output_item.done 仍包含 reasoning_content 元数据
        item_done = [e for e in events if e[0] == "response.output_item.done"]
        # 有两个 output_item.done：推理项 + message 项
        assert len(item_done) == 2
        message_done = [e for e in item_done if e[1]["item"]["type"] == "message"][0]
        assert message_done[1]["item"]["reasoning_content"] == "Now processing..."

        # response.completed 的 output：推理项 + message
        completed_event = [e for e in events if e[0] == "response.completed"][0]
        output = completed_event[1]["response"]["output"]
        assert len(output) == 2
        assert output[0]["type"] == "reasoning"
        assert output[0]["content"][0]["text"] == "Now processing..."
        assert output[0]["content"][0]["type"] == "reasoning_summary_text"
        assert output[1]["type"] == "message"
        assert output[1]["content"][0]["text"] == "Hello world"

    @pytest.mark.asyncio
    async def test_convert_sse_preserves_cached_tokens(self):
        adapter = ResponsesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"content":"ok"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":200,"completion_tokens":5,"total_tokens":205,"prompt_tokens_details":{"cached_tokens":150}}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)
        completed = [e for e in events if e[0] == "response.completed"][0]
        usage = completed[1]["response"]["usage"]
        assert usage["input_tokens"] == 200
        assert usage["output_tokens"] == 5
        assert usage["input_tokens_details"]["cached_tokens"] == 150

    @pytest.mark.asyncio
    async def test_convert_sse_reasoning_only(self):
        """验证纯 reasoning 无正文时，reasoning 作为独立推理项 + message 兜底输出"""
        adapter = ResponsesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"role":"assistant","reasoning_content":""},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"reasoning_content":"thinking..."},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)

        # 流式推理走 reasoning_summary_text.delta
        reasoning_deltas = [e[1]["delta"] for e in events if e[0] == "response.reasoning_summary_text.delta"]
        assert len(reasoning_deltas) == 1
        assert "thinking..." in reasoning_deltas

        # 必须有 reasoning_summary_text.done 关闭推理
        rsn_done = [e for e in events if e[0] == "response.reasoning_summary_text.done"]
        assert len(rsn_done) == 1
        assert rsn_done[0][1]["text"] == "thinking..."

        # 纯推理场景仍将 reasoning 作为正文兜底输出到 message output_item
        done_event = [e for e in events if e[0] == "response.content_part.done"][0]
        assert done_event[1]["part"]["text"] == "thinking..."

        # response.completed 应包含 reasoning 项 + message 项
        completed = [e for e in events if e[0] == "response.completed"][0]
        output = completed[1]["response"]["output"]
        assert len(output) == 2
        assert output[0]["type"] == "reasoning"
        assert output[1]["type"] == "message"

    @pytest.mark.asyncio
    async def test_convert_sse_empty_stream(self):
        """验证空流（0 tokens）时 adapter 返回空 completed 而不报错"""
        adapter = ResponsesAdapter()
        chat_sse = [
            b'data: [DONE]\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)
        assert len(events) == 1
        assert events[0][0] == "response.completed"
        assert events[0][1]["response"]["output"] == []

    @pytest.mark.asyncio
    async def test_convert_sse_with_tool_calls(self):
        """验证 Chat SSE 中逐 chunk 的 tool_calls delta 被转为 Responses function_call 事件"""
        adapter = ResponsesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_01","type":"function","function":{"name":"bash","arguments":""}}]},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{"}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"cmd\\":\\"ls\\""}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)

        added_items = [e for e in events if e[0] == "response.output_item.added"]
        assert len(added_items) == 2
        fc_item = added_items[1][1]
        assert fc_item["output_index"] == 1
        assert fc_item["item"]["type"] == "function_call"
        assert fc_item["item"]["name"] == "bash"

        arg_deltas = [e[1]["delta"] for e in events if e[0] == "response.function_call_arguments.delta"]
        assert len(arg_deltas) == 3

        modern_begin = [e for e in events if e[0] == "response.output_tool_call.begin"]
        assert len(modern_begin) == 1
        assert modern_begin[0][1]["name"] == "bash"

        modern_deltas = [e[1]["delta"] for e in events if e[0] == "response.output_tool_call.delta"]
        assert len(modern_deltas) == 3

        done_items = [e for e in events if e[0] == "response.output_item.done"]
        assert len(done_items) == 2  # message + function_call
        fc_done = done_items[1][1]
        assert fc_done["item"]["arguments"] == '{"cmd":"ls"}'

        modern_end = [e for e in events if e[0] == "response.output_tool_call.end"]
        assert len(modern_end) == 1
        assert modern_end[0][1]["arguments"] == '{"cmd":"ls"}'

        completed = [e for e in events if e[0] == "response.completed"][0]
        resp_output = completed[1]["response"]["output"]
        assert len(resp_output) == 2  # message + function_call
        assert resp_output[1]["name"] == "bash"
        assert any(e[0] == "response.done" for e in events)

    @pytest.mark.asyncio
    async def test_convert_sse_tool_calls_only_no_text(self):
        """验证仅 tool_calls 无文本内容时的转换（纯函数调用场景）"""
        adapter = ResponsesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_x","type":"function","function":{"name":"memory_search_nodes","arguments":"{}"}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)
        text_deltas = [e for e in events if e[0] == "response.output_text.delta"]
        assert len(text_deltas) == 0

        fc_dones = [e for e in events if e[0] == "response.output_item.done"
                    and e[1]["item"]["type"] == "function_call"]
        assert len(fc_dones) == 1

        completed = [e for e in events if e[0] == "response.completed"][0]
        resp_output = completed[1]["response"]["output"]
        assert len(resp_output) == 2

    @pytest.mark.asyncio
    async def test_convert_sse_multiple_tool_calls(self):
        """验证多个并行 tool_calls 的转换"""
        adapter = ResponsesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"deepseek-v4","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"bash","arguments":"{}"}},{"index":1,"id":"call_b","type":"function","function":{"name":"read_file","arguments":"{}"}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":5,"completion_tokens":5,"total_tokens":10}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)

        added = [e for e in events if e[0] == "response.output_item.added"]
        assert len(added) == 3  # message + 2 tool_calls

        fc_items = [a for a in added if a[1]["item"]["type"] == "function_call"]
        names = {fc[1]["item"]["name"] for fc in fc_items}
        assert names == {"bash", "read_file"}

        completed = [e for e in events if e[0] == "response.completed"][0]
        resp_output = completed[1]["response"]["output"]
        assert len(resp_output) == 3  # message + 2 function_calls


# ═══════════════════════════════════════
# MessagesAdapter

class TestMessagesAdapterRequest:

    def test_convert_simple(self):
        adapter = MessagesAdapter()
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
        adapter = MessagesAdapter()
        body = {
            "model": "claude",
            "system": [
                {"type": "text", "text": "You are helpful"},
                {"type": "text", "text": "Be concise"},
            ],
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = adapter.convert_request(body)
        assert result["messages"][0]["content"] == "You are helpful\n\nBe concise"

    def test_convert_content_list_with_image(self):
        adapter = MessagesAdapter()
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
        # 图片应转换为 OpenAI 多模态 content 数组格式
        content = result["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "What is this?"}
        assert content[1]["type"] == "image_url"
        assert "image_url" in content[1]

    def test_convert_stop_sequences(self):
        adapter = MessagesAdapter()
        body = {"model": "claude", "messages": [], "stop_sequences": ["END"]}
        result = adapter.convert_request(body)
        assert result["stop"] == ["END"]

    def test_convert_max_tokens_temperature(self):
        adapter = MessagesAdapter()
        body = {"model": "claude", "messages": [], "max_tokens": 1000, "temperature": 0.7, "top_p": 0.9}
        result = adapter.convert_request(body)
        assert result["max_tokens"] == 1000
        assert "max_completion_tokens" not in result
        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.9

    def test_convert_gpt_reasoning_params(self):
        """验证 openai 供应商请求会补 max_completion_tokens 与 reasoning_effort。"""
        adapter = MessagesAdapter()
        adapter._request_provider = "openai"
        body = {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 2048,
            "reasoning": {"effort": "max"},
        }
        result = adapter.convert_request(body)
        assert result["max_tokens"] == 2048
        assert result["max_completion_tokens"] == 2048
        assert result["reasoning_effort"] == "high"

    def test_convert_max_completion_tokens_depends_on_provider_not_model_name(self):
        """验证 max_completion_tokens 是否补齐取决于供应商，而不是模型名。"""
        adapter = MessagesAdapter()
        adapter._request_provider = "deepseek"
        body = {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 2048,
        }
        result = adapter.convert_request(body)
        assert result["max_tokens"] == 2048
        assert "max_completion_tokens" not in result

    def test_convert_reasoning_effort_depends_on_provider_not_model_name(self):
        """验证 reasoning_effort 是否注入取决于供应商，而不是模型名。"""
        adapter = MessagesAdapter()
        adapter._request_provider = "deepseek"
        body = {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "Hi"}],
            "reasoning": {"effort": "high"},
        }
        result = adapter.convert_request(body)
        assert "reasoning_effort" not in result

    def test_convert_reasoning_effort_skipped_when_thinking_disabled(self):
        """验证调用方显式关闭 thinking 时不再额外注入 reasoning_effort。"""
        adapter = MessagesAdapter()
        body = {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "Hi"}],
            "reasoning_effort": "high",
            "thinking": {"type": "disabled"},
        }
        result = adapter.convert_request(body)
        assert "reasoning_effort" not in result

    def test_convert_metadata_user_id_to_user(self):
        """验证 metadata.user_id 会在未显式提供 user 时映射到 Chat user 字段。"""
        adapter = MessagesAdapter()
        body = {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "Hi"}],
            "metadata": {"user_id": "trace-user", "scene": "cli"},
        }
        result = adapter.convert_request(body)
        assert result["metadata"] == {"user_id": "trace-user", "scene": "cli"}
        assert result["user"] == "trace-user"

    def test_convert_explicit_user_overrides_metadata_user_id(self):
        """验证显式 user 优先于 metadata.user_id，避免覆盖调用方已有标识。"""
        adapter = MessagesAdapter()
        body = {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "Hi"}],
            "user": "explicit-user",
            "metadata": {"user_id": "trace-user"},
        }
        result = adapter.convert_request(body)
        assert result["user"] == "explicit-user"


class TestProviderProfile:

    def test_openai_profile_enables_openai_specific_mappings(self):
        """验证 openai profile 会集中开启 OpenAI 相关兼容能力。"""
        profile = get_provider_profile("openai")
        assert profile.inject_max_completion_tokens is True
        assert profile.inject_reasoning_effort is True
        assert profile.map_metadata_user_id_to_user is True

    def test_unknown_provider_profile_is_conservative(self):
        """验证未知供应商默认使用保守 profile，不主动注入 OpenAI 特有字段。"""
        profile = get_provider_profile("vendor-x")
        assert profile.inject_max_completion_tokens is False
        assert profile.inject_reasoning_effort is False

    def test_deepseek_profile_enables_responses_defaults(self):
        """验证 deepseek profile 会集中声明 Responses -> Chat 兼容默认值。"""
        profile = get_provider_profile("deepseek")
        assert profile.responses_force_thinking_enabled is True
        assert profile.responses_default_reasoning_effort == "high"

    def test_convert_tools_without_explicit_tool_choice_keeps_protocol_neutral(self):
        """验证协议层不注入模型策略：未显式传 tool_choice 时保持中立"""
        adapter = MessagesAdapter()
        body = {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "read_file",
                    "description": "Read file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                }
            ],
        }
        result = adapter.convert_request(body)
        assert "tool_choice" not in result

    def test_convert_tools_keeps_explicit_tool_choice(self):
        """验证已显式传入 tool_choice 时不被自动策略覆盖"""
        adapter = MessagesAdapter()
        body = {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "read_file",
                    "description": "Read file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                }
            ],
            "tool_choice": {"type": "tool", "name": "read_file"},
        }
        result = adapter.convert_request(body)
        assert result["tool_choice"] == {"type": "function", "function": {"name": "read_file"}}


class TestMessagesAdapterResponse:

    def test_convert_response(self):
        adapter = MessagesAdapter()
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

    def test_convert_response_with_tool_calls(self):
        """验证非流式响应中 tool_calls 被转换为 tool_use content block"""
        adapter = MessagesAdapter()
        chat_resp = json.dumps({
            "id": "chatcmpl-tc1",
            "model": "gpt-5.4",
            "choices": [{
                "message": {
                    "content": "Let me check",
                    "tool_calls": [
                        {"id": "call_1", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path":"/tmp/x"}'}},
                    ],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        })
        result = json.loads(adapter.convert_response(chat_resp))
        # 应有 text + tool_use 两个 content block
        assert len(result["content"]) == 2
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "Let me check"
        assert result["content"][1]["type"] == "tool_use"
        assert result["content"][1]["name"] == "read_file"
        assert result["content"][1]["input"] == {"path": "/tmp/x"}
        assert result["stop_reason"] == "tool_use"

    def test_convert_response_with_reasoning(self):
        """验证非流式响应中 reasoning_content 被转换为 thinking 块"""
        adapter = MessagesAdapter()
        chat_resp = json.dumps({
            "id": "chatcmpl-rs1",
            "model": "deepseek-v4",
            "choices": [{
                "message": {
                    "content": "Answer",
                    "reasoning_content": "Deep thinking process...",
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 30, "completion_tokens": 10},
        })
        result = json.loads(adapter.convert_response(chat_resp))
        # 应有 thinking + text 两个 content block
        assert len(result["content"]) == 2
        assert result["content"][0]["type"] == "thinking"
        assert result["content"][0]["thinking"] == "Deep thinking process..."
        assert result["content"][1]["type"] == "text"
        assert result["content"][1]["text"] == "Answer"

    def test_convert_response_tool_calls_only(self):
        """验证纯 tool_calls（无文本）时 content 不包含空 text 块"""
        adapter = MessagesAdapter()
        chat_resp = json.dumps({
            "id": "chatcmpl-tc2",
            "model": "gpt-5.4",
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_x", "type": "function",
                         "function": {"name": "bash", "arguments": '{"cmd":"ls"}'}},
                    ],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        })
        result = json.loads(adapter.convert_response(chat_resp))
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "tool_use"
        assert result["content"][0]["name"] == "bash"


class TestMessagesAdapterRequestTools:

    def test_convert_tools(self):
        """验证 tools 定义的转换"""
        adapter = MessagesAdapter()
        body = {
            "model": "claude",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {"name": "read_file", "description": "Read a file",
                 "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
            ],
        }
        result = adapter.convert_request(body)
        assert "tools" in result
        assert result["tools"][0]["type"] == "function"
        assert result["tools"][0]["function"]["name"] == "read_file"
        assert result["tools"][0]["function"]["parameters"]["properties"]["path"]["type"] == "string"

    def test_convert_tool_use_in_assistant(self):
        """验证 assistant 消息中 tool_use → tool_calls"""
        adapter = MessagesAdapter()
        body = {
            "model": "claude",
            "messages": [
                {"role": "user", "content": "Read /tmp/x"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "Let me read that."},
                    {"type": "tool_use", "id": "tool_001", "name": "read_file",
                     "input": {"path": "/tmp/x"}},
                ]},
            ],
        }
        result = adapter.convert_request(body)
        assistant_msg = result["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["content"] == "Let me read that."
        assert len(assistant_msg["tool_calls"]) == 1
        assert assistant_msg["tool_calls"][0]["function"]["name"] == "read_file"
        assert json.loads(assistant_msg["tool_calls"][0]["function"]["arguments"]) == {"path": "/tmp/x"}

    def test_convert_response_invalid_bash_tool_call_is_dropped_instead_of_inventing_command(self):
        """验证缺少 command 的 bash tool_call 不会被协议层伪造成真实命令。"""
        adapter = MessagesAdapter()
        adapter._tool_schemas_by_name = {
            "bash": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            }
        }
        chat_resp = json.dumps({
            "id": "chatcmpl-invalid-bash",
            "model": "gpt-5.4",
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {"name": "bash", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        })

        result = json.loads(adapter.convert_response(chat_resp))
        assert result["stop_reason"] == "end_turn"
        assert result["content"] == [{"type": "text", "text": "当前请求未产生可用响应，请重试一次。"}]

    def test_convert_tool_result(self):
        """验证 user 消息中 tool_result → role=tool"""
        adapter = MessagesAdapter()
        body = {
            "model": "claude",
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tool_001", "name": "read_file",
                     "input": {"path": "/tmp/x"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tool_001",
                     "content": "file contents here"},
                ]},
            ],
        }
        result = adapter.convert_request(body)
        # 应有 assistant + tool 两条消息
        assert len(result["messages"]) == 2
        tool_msg = result["messages"][1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "tool_001"
        assert tool_msg["content"] == "file contents here"

    def test_convert_tool_result_preserves_structured_content(self):
        """验证 tool_result 含非 text 结构时会保留完整 JSON，而不是静默丢块。"""
        adapter = MessagesAdapter()
        body = {
            "model": "claude",
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tool_002", "name": "read_file", "input": {"path": "/tmp/x"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tool_002", "content": [
                        {"type": "text", "text": "summary"},
                        {"type": "output", "body": {"lines": [1, 2, 3], "ok": True}},
                    ]},
                ]},
            ],
        }
        result = adapter.convert_request(body)
        tool_msg = result["messages"][1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "tool_002"
        assert json.loads(tool_msg["content"]) == [
            {"type": "text", "text": "summary"},
            {"type": "output", "body": {"lines": [1, 2, 3], "ok": True}},
        ]

    def test_convert_tool_result_object_is_serialized(self):
        """验证 tool_result 为对象时会序列化为 JSON 字符串。"""
        adapter = MessagesAdapter()
        body = {
            "model": "claude",
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tool_003", "name": "run", "input": {}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tool_003", "content": {"ok": True, "count": 2}},
                ]},
            ],
        }
        result = adapter.convert_request(body)
        tool_msg = result["messages"][1]
        assert tool_msg["content"] == '{"ok": true, "count": 2}'


class TestMessagesAdapterSSETools:

    @pytest.mark.asyncio
    async def test_convert_sse_with_reasoning(self):
        """验证 reasoning_content 转换为 thinking delta 事件"""
        adapter = MessagesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"deepseek","choices":[{"delta":{"role":"assistant","reasoning_content":""},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"reasoning_content":"thinking..."},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Answer"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)
        event_names = [e[0] for e in events]

        # 推理内容块：content_block_start(index=0, thinking) → delta → stop
        assert "message_start" in event_names
        # 新语义：不再 role 预发 text 占位块，出现 reasoning 时先发 thinking
        # 通常 thinking 为 index=0，随后 text 为 index=1
        cb_starts = [e for e in events if e[0] == "content_block_start"]
        assert len(cb_starts) >= 2
        # thinking 块
        thinking_start = [c for c in cb_starts if c[1]["content_block"]["type"] == "thinking"]
        assert len(thinking_start) == 1
        assert thinking_start[0][1]["index"] == 0
        # text 块
        text_start = [c for c in cb_starts if c[1]["content_block"]["type"] == "text"]
        assert len(text_start) == 1
        assert text_start[0][1]["index"] == 1

        thinking_deltas = [e for e in events if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "thinking_delta"]
        assert len(thinking_deltas) == 1

        text_deltas = [e for e in events if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "text_delta"]
        assert len(text_deltas) == 1
        assert text_deltas[0][1]["delta"]["text"] == "Answer"

        # content_block_stop 应有 text + thinking（可能还有 tool）
        stops = [e for e in events if e[0] == "content_block_stop"]
        assert len(stops) >= 2

    @pytest.mark.asyncio
    async def test_convert_sse_pure_reasoning_keeps_thinking_only(self):
        """验证只有 reasoning 时仅输出 thinking block，不再镜像为正文。"""
        adapter = MessagesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-think-only","model":"deepseek","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-think-only","choices":[{"delta":{"reasoning_content":"step-1 "},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-think-only","choices":[{"delta":{"reasoning_content":"step-2"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-think-only","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)
        thinking_deltas = [e for e in events if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "thinking_delta"]
        text_deltas = [e for e in events if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "text_delta"]
        starts = [e for e in events if e[0] == "content_block_start"]

        assert len(thinking_deltas) == 2
        assert text_deltas == []
        assert len(starts) == 1
        assert starts[0][1]["content_block"]["type"] == "thinking"

    @pytest.mark.asyncio
    async def test_convert_sse_with_single_tool_call(self):
        """验证单个 tool_call 增量转换为 tool_use 事件序列"""
        adapter = MessagesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"gpt","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"bash","arguments":""}}]},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"}"}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"cmd\\":\\"ls\\""}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":5,"completion_tokens":3}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)

        # 应有 tool_use content_block_start
        cb_starts = [e for e in events if e[0] == "content_block_start"]
        # 第一个是 text (index=0), 第二个是 tool_use (index=1)
        tool_start = [c for c in cb_starts if c[1]["content_block"]["type"] == "tool_use"]
        assert len(tool_start) == 1
        assert tool_start[0][1]["content_block"]["name"] == "bash"

        # tool content_block_stop 也应有（text 占位块 + tool 块）
        stops = [e for e in events if e[0] == "content_block_stop"]
        assert len(stops) >= 1  # 至少 tool 块，text 占位块也可能存在

        # message_delta stop_reason 应为 tool_use
        msg_delta = [e for e in events if e[0] == "message_delta"][0]
        assert msg_delta[1]["delta"]["stop_reason"] == "tool_use"

    @pytest.mark.asyncio
    async def test_convert_sse_loop_guard_drops_repeated_tool_calls(self):
        """验证重复同签名 tool_use 超阈值时触发防循环保险丝并降级 end_turn"""
        adapter = MessagesAdapter()
        # 同签名（name=bash + arguments={"command":"ls"} + 同一 call_id）重复 3 次，超过阈值 2
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"gpt","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_same","type":"function","function":{"name":"bash","arguments":"{\\"command\\":\\"ls\\"}"}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_same","type":"function","function":{"name":"bash","arguments":"{\\"command\\":\\"ls\\"}"}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"tool_calls":[{"index":2,"id":"call_same","type":"function","function":{"name":"bash","arguments":"{\\"command\\":\\"ls\\"}"}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":5,"completion_tokens":3}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)
        msg_delta = [e for e in events if e[0] == "message_delta"][0]
        assert msg_delta[1]["delta"]["stop_reason"] == "end_turn"

        text_deltas = [
            e for e in events
            if e[0] == "content_block_delta"
            and e[1].get("delta", {}).get("type") == "text_delta"
        ]
        assert any("循环保护" in (d[1]["delta"].get("text") or "") for d in text_deltas)


class TestChatAdapter:

    def test_convert_request_chat_to_messages(self):
        adapter = ChatAdapter()
        body = {
            "model": "claude-sonnet",
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"},
                {
                    "role": "assistant",
                    "content": "Let me call tool",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"/tmp/x"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "read file",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                }
            ],
            "tool_choice": "required",
            "stop": ["END"],
        }
        result = adapter.convert_request(body)
        assert result["model"] == "claude-sonnet"
        assert result["system"] == "You are helpful"
        assert result["stop_sequences"] == ["END"]
        assert result["tool_choice"] == {"type": "any"}
        assert result["tools"][0]["name"] == "read_file"
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][1]["content"][1]["type"] == "tool_use"
        assert result["messages"][2]["content"][0]["type"] == "tool_result"

    def test_convert_response_messages_json_to_chat_json(self):
        adapter = ChatAdapter()
        msg_json = json.dumps({
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet",
            "content": [
                {"type": "thinking", "thinking": "analysis"},
                {"type": "text", "text": "answer"},
                {"type": "tool_use", "id": "call_2", "name": "bash", "input": {"command": "ls"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
        result = json.loads(adapter.convert_response(msg_json))
        assert result["object"] == "chat.completion"
        assert result["choices"][0]["message"]["content"] == "answer"
        assert result["choices"][0]["message"]["reasoning_content"] == "analysis"
        assert result["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "bash"
        assert result["choices"][0]["finish_reason"] == "tool_calls"

    def test_convert_response_messages_sse_to_chat_json(self):
        adapter = ChatAdapter()
        sse_text = (
            'event: message_start\n'
            'data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"claude-sonnet","content":[],"usage":{"input_tokens":7}}}\n\n'
            'event: content_block_delta\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n'
            'event: message_delta\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
            'event: message_stop\n'
            'data: {"type":"message_stop"}\n\n'
            'data: [DONE]\n\n'
        )
        result = json.loads(adapter.convert_response(sse_text))
        assert result["object"] == "chat.completion"
        assert result["model"] == "claude-sonnet"
        assert result["choices"][0]["message"]["content"] == "Hello"
        assert result["usage"]["prompt_tokens"] == 7
        assert result["usage"]["completion_tokens"] == 3


class TestProtocolConverterPluginSessions:

    async def _load_plugin(self):
        plugin = Plugin()
        plugin._static_dir = Path(__file__).resolve().parent / "views"
        await plugin.on_load()
        return plugin

    @pytest.mark.asyncio
    async def test_previous_response_id_restores_non_stream_history(self):
        plugin = await self._load_plugin()

        first_request = {
            "model": "deepseek-v4",
            "input": "Need weather",
            "stream": False,
        }
        first_chat = plugin.convert_request(first_request)
        assert "previous_response_id" not in first_chat

        chat_resp = json.dumps({
            "id": "chatcmpl-prev1",
            "model": "deepseek-v4",
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_content": "I should call weather.",
                    "tool_calls": [{
                        "id": "call_weather",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"杭州"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        })
        plugin.convert_response(chat_resp)

        follow_up = plugin.convert_request({
            "model": "deepseek-v4",
            "previous_response_id": "resp_prev1",
            "input": [{
                "type": "function_call_output",
                "call_id": "call_weather",
                "output": {"forecast": "cloudy"},
            }],
        })
        messages = follow_up["messages"]
        assert "previous_response_id" not in follow_up
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["reasoning_content"] == "I should call weather."
        assert messages[1]["tool_calls"][0]["id"] == "call_weather"
        assert messages[2] == {
            "role": "tool",
            "tool_call_id": "call_weather",
            "content": '{"forecast": "cloudy"}',
        }

    @pytest.mark.asyncio
    async def test_previous_response_id_restores_stream_history(self):
        plugin = await self._load_plugin()
        plugin.convert_request({"model": "deepseek-v4", "input": "Need tool", "stream": True})

        chat_sse = [
            b'data: {"id":"chatcmpl-stream1","model":"deepseek-v4","choices":[{"delta":{"role":"assistant","reasoning_content":""},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-stream1","choices":[{"delta":{"reasoning_content":"Thinking"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-stream1","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_x","type":"function","function":{"name":"bash","arguments":"{}"}}]}}]}\n\n',
            b'data: {"id":"chatcmpl-stream1","choices":[{"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n',
        ]
        lines = []
        async for line in plugin.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)
        assert any(e[0] == "response.done" for e in _parse_sse_helper(lines))
        response_id = next(iter(plugin._response_sessions.keys()))

        follow_up = plugin.convert_request({
            "model": "deepseek-v4",
            "previous_response_id": response_id,
            "input": [{"type": "function_call_output", "call_id": "call_x", "output": "ok"}],
        })
        assert follow_up["messages"][1]["role"] == "assistant"
        assert follow_up["messages"][1]["reasoning_content"] == "Thinking"
        assert follow_up["messages"][1]["tool_calls"][0]["id"] == "call_x"


# ── 工具函数 ──

async def _async_bytes_iter(items):
    for item in items:
        yield item


def _parse_sse_helper(lines):
    """将 SSE 文本行列表解析为 [(event_name, data_dict), ...]"""
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
