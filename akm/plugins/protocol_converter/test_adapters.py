"""ResponsesAdapter 和 MessagesAdapter 单元测试"""
import json
import pytest
from akm.plugins.protocol_converter._responses import ResponsesAdapter
from akm.plugins.protocol_converter._messages import MessagesAdapter


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

    def test_convert_tools_passthrough(self):
        adapter = ResponsesAdapter()
        tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
        body = {"model": "test", "input": "hi", "tools": tools}
        result = adapter.convert_request(body)
        assert result["tools"] == tools

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
        completed = events[-1]
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

        done_items = [e for e in events if e[0] == "response.output_item.done"]
        assert len(done_items) == 2  # message + function_call
        fc_done = done_items[1][1]
        assert fc_done["item"]["arguments"] == '{"cmd":"ls"}'

        completed = [e for e in events if e[0] == "response.completed"][0]
        resp_output = completed[1]["response"]["output"]
        assert len(resp_output) == 2  # message + function_call
        assert resp_output[1]["name"] == "bash"

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
        assert result["messages"][0]["content"] == "You are helpful\nBe concise"

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
        assert result["messages"][0]["content"] == "What is this?\n[image]"

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
        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.9


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


class TestMessagesAdapterSSE:

    @pytest.mark.asyncio
    async def test_convert_sse_basic(self):
        adapter = MessagesAdapter()
        chat_sse = [
            b'data: {"id":"chatcmpl-1","model":"claude","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hello"},"index":0}]}\n\n',
            b'data: {"id":"chatcmpl-1","choices":[{"finish_reason":"end_turn"}],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\n',
        ]

        lines = []
        async for line in adapter.convert_sse_stream(_async_bytes_iter(chat_sse)):
            lines.append(line)

        events = _parse_sse_helper(lines)
        event_names = [e[0] for e in events]
        assert event_names[0] == "message_start"
        assert event_names[1] == "content_block_start"
        assert event_names[2] == "content_block_delta"
        assert event_names[3] == "content_block_stop"
        assert event_names[4] == "message_delta"
        assert event_names[5] == "message_stop"


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
