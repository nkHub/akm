"""
Messages 格式适配器

源格式为 Anthropic Messages API，提供到 Chat Completions 的双向转换。
- Messages → Chat（请求转换） — 含工具调用、图片、推理
- Chat SSE → Messages SSE（响应逆转换） — 含增量 tool、reasoning 流
- Chat JSON → Messages JSON（非流式响应转换） — 含 tool_use 还原、推理

参考：rosetta-llm (IR codec 架构), claude-code-proxy (增量 tool SSE)
"""

import json
import time
from uuid import uuid4
from typing import AsyncIterator
from akm.adapter import BaseAdapter
from akm.plugins.protocol_converter._ir import chat_message_to_ir, ir_to_messages_content


class MessagesAdapter(BaseAdapter):
    """Messages 格式适配器：源格式为 Anthropic Messages API

    发送方向：convert_request()     — Messages → Chat（请求转换）
    接收方向：convert_sse_stream()  — Chat SSE → Messages SSE（响应逆转换）
    非流式：  convert_response()   — Chat JSON → Messages JSON
    """

    # ═══════════════════════════════════════════════════════════
    #  请求转换：Messages → Chat Completions
    # ═══════════════════════════════════════════════════════════

    def convert_request(self, body: dict) -> dict:
        """Messages API 请求 → Chat Completions 请求

        处理内容：
        - system 提示（字符串或 content block 数组）
        - user 消息中的 text / image / tool_result 内容块
        - assistant 消息中的 text / tool_use 内容块 → tool_calls
        - 工具定义 tools schema 转换
        - 通用参数：max_tokens, temperature, top_p, stop_sequences
        """
        chat_body = {
            "model": body.get("model", ""),
            "messages": self._messages_to_openai(body),
            "stream": body.get("stream", False),
        }

        if "max_tokens" in body:
            chat_body["max_tokens"] = body["max_tokens"]
        if "temperature" in body:
            chat_body["temperature"] = body["temperature"]
        if "top_p" in body:
            chat_body["top_p"] = body["top_p"]
        if "stop_sequences" in body:
            chat_body["stop"] = body["stop_sequences"]

        # ── 工具定义转换 ──
        tools = body.get("tools")
        if tools:
            # 保存 schema，供响应侧 tool 参数校验（仅本插件内部使用）
            self._tool_schemas_by_name = {
                t.get("name", ""): self._clean_schema(t.get("input_schema", {}))
                for t in tools if isinstance(t, dict) and t.get("name")
            }
            chat_body["tools"] = self._convert_tools(tools)
            # 保留 tool_choice（大部分供应商兼容 auto/any/required）
            if "tool_choice" in body:
                chat_body["tool_choice"] = self._convert_tool_choice(body["tool_choice"])
        else:
            self._tool_schemas_by_name = {}

        return chat_body

    def _validate_tool_input(self, tool_name: str, tool_input: dict) -> bool:
        """按请求侧工具 schema 做最小校验（required + 基础类型）。

        目标：拦住会触发 Claude CLI `Invalid tool parameters` 的明显坏参数。
        只在 Messages 转换插件内部生效，不影响其他转发链路。
        """
        schemas = getattr(self, "_tool_schemas_by_name", {}) or {}
        schema = schemas.get(tool_name)
        if not schema or not isinstance(schema, dict):
            return True
        if not isinstance(tool_input, dict):
            return False

        required = schema.get("required") or []
        if isinstance(required, list):
            for k in required:
                if k not in tool_input:
                    return False

        props = schema.get("properties")
        if not isinstance(props, dict):
            return True

        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        for k, v in tool_input.items():
            sch = props.get(k)
            if not isinstance(sch, dict):
                continue
            t = sch.get("type")
            py_t = type_map.get(t)
            if py_t and not isinstance(v, py_t):
                return False

        # Bash 语义约束：command 必须是非空字符串；timeout 若存在需为正数
        if isinstance(tool_name, str) and tool_name.lower() == "bash":
            cmd = tool_input.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                return False
            if "timeout" in tool_input:
                t = tool_input.get("timeout")
                if not isinstance(t, (int, float)) or t <= 0:
                    return False
        return True

    def _repair_tool_input(self, tool_name: str, tool_input: dict) -> dict:
        """按 schema 对工具参数做最小修复，降低无意义重试。

        只做“保守补全”：
        - required 且缺失的 string/object/array 字段补默认值
        - Bash 缺 description 时基于 command 补一句简短描述
        """
        if not isinstance(tool_input, dict):
            return {}

        schemas = getattr(self, "_tool_schemas_by_name", {}) or {}
        schema = schemas.get(tool_name)
        if not isinstance(schema, dict):
            return tool_input

        fixed = dict(tool_input)
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []

        # 通用 required 字段补全
        for k in required:
            if k in fixed:
                continue
            p = props.get(k) if isinstance(props, dict) else {}
            t = p.get("type") if isinstance(p, dict) else None
            if t == "string":
                fixed[k] = ""
            elif t == "object":
                fixed[k] = {}
            elif t == "array":
                fixed[k] = []

        # Bash 专项：description 缺失时自动生成
        if tool_name.lower() == "bash":
            cmd = fixed.get("command")
            # command 缺失/为空时给安全默认值，避免触发 CLI Invalid tool parameters
            if not isinstance(cmd, str) or not cmd.strip():
                fixed["command"] = "find . -maxdepth 3 -mindepth 1"
                cmd = fixed["command"]
            if isinstance(cmd, str) and cmd.strip() and not fixed.get("description"):
                fixed["description"] = f"Run command: {cmd[:80]}"
            # timeout 缺失或非法时使用安全默认值
            t = fixed.get("timeout")
            if t is None or not isinstance(t, (int, float)) or t <= 0:
                fixed["timeout"] = 120000

        return fixed

    # ── 工具定义 ──

    def _convert_tools(self, tools: list) -> list:
        """Anthropic tools → OpenAI tools 格式，递归清理不兼容字段"""
        result = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name", "")
            description = tool.get("description", "")
            input_schema = tool.get("input_schema", {"type": "object", "properties": {}})
            result.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": self._clean_schema(input_schema),
                },
            })
        return result

    def _clean_schema(self, obj):
        """递归清除 JSON Schema 中部分供应商不支持的字段（additionalProperties、strict）"""
        if not isinstance(obj, dict):
            return obj
        cleaned = {}
        for k, v in obj.items():
            if k in ("additionalProperties", "strict"):
                continue
            if isinstance(v, dict):
                cleaned[k] = self._clean_schema(v)
            elif isinstance(v, list):
                cleaned[k] = [self._clean_schema(i) if isinstance(i, dict) else i for i in v]
            else:
                cleaned[k] = v
        return cleaned

    def _convert_tool_choice(self, tool_choice: dict | str) -> str | dict:
        """Anthropic tool_choice → OpenAI tool_choice"""
        if isinstance(tool_choice, str):
            return tool_choice
        if isinstance(tool_choice, dict):
            choice_type = tool_choice.get("type", "auto")
            if choice_type == "any":
                return "required"
            if choice_type == "tool" and "name" in tool_choice:
                return {
                    "type": "function",
                    "function": {"name": tool_choice["name"]},
                }
            return "auto"
        return "auto"

    # ── 消息转换 ──

    def _messages_to_openai(self, body: dict) -> list:
        """将 Anthropic Messages 数组转为 OpenAI messages 数组

        每个 Anthropic 消息按 role 分发处理：
        - user 消息可能包含 text / image / tool_result 内容块
        - assistant 消息可能包含 text / tool_use 内容块
        - tool_result 转换为 role="tool" 消息
        """
        messages = []

        # ── system 提示 ──
        system = body.get("system")
        if system:
            system_text = self._extract_system_text(system)
            if system_text:
                messages.append({"role": "system", "content": system_text})

        for m in body.get("messages", []):
            role = m.get("role", "user")
            content = m.get("content", "")

            if role == "user":
                converted = self._convert_user_message(content)
                if isinstance(converted, list):
                    # 可能拆分为多条消息（如 tool_result → role=tool）
                    messages.extend(converted)
                else:
                    messages.append(converted)

            elif role == "assistant":
                messages.append(self._convert_assistant_message(content))

            else:
                # 其他角色直接透传
                if isinstance(content, list):
                    content = self._flatten_text_only(content)
                messages.append({"role": role, "content": content})

        return messages

    def _extract_system_text(self, system) -> str:
        """提取 system 提示文本（兼容字符串和 content block 数组）"""
        if isinstance(system, str):
            return system
        if isinstance(system, list):
            parts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "\n\n".join(parts)
        return ""

    def _flatten_text_only(self, content_list: list) -> str:
        """从 content block 数组中仅提取纯文本（丢弃图片/工具块）"""
        texts = []
        for block in content_list:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "".join(texts) if texts else ""

    # ── user 消息转换 ──

    def _convert_user_message(self, content) -> dict | list:
        """转换 user 消息

        字符串 → {role: "user", content: "..."}
        content block 数组：
          - 纯文本 → {role: "user", content: "..."}
          - 文本 + 图片 → {role: "user", content: [{...}, {...}]}
          - 含 tool_result → 返回多条 [{role: "tool", ...}, {role: "user", ...}]
        """
        if isinstance(content, str):
            return {"role": "user", "content": content}

        if not isinstance(content, list):
            return {"role": "user", "content": str(content)}

        texts = []
        images = []
        tool_results = []

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type == "text":
                texts.append(block.get("text", ""))
            elif block_type == "image":
                images.append(self._convert_image_block(block))
            elif block_type == "tool_result":
                tool_results.append(self._convert_tool_result_block(block))

        # 构建结果列表：tool 结果在前，用户消息在后
        result = []
        for tr in tool_results:
            result.append(tr)

        if images:
            # 有图片 → OpenAI 多模态 content 数组
            content_parts = []
            if texts:
                content_parts.append({"type": "text", "text": "\n".join(texts)})
            content_parts.extend(images)
            result.append({"role": "user", "content": content_parts})
        elif texts:
            result.append({"role": "user", "content": "\n".join(texts) if len(texts) > 1 else texts[0]})

        return result if result else [{"role": "user", "content": ""}]

    def _convert_image_block(self, block: dict) -> dict:
        """Anthropic image content block → OpenAI image_url content block"""
        source = block.get("source") or {}
        source_type = source.get("type", "base64")
        media_type = source.get("media_type", "image/png")
        data = source.get("data", "")

        if source_type == "url":
            url = source.get("url", data)
        else:
            url = f"data:{media_type};base64,{data}"

        return {
            "type": "image_url",
            "image_url": {
                "url": url,
                "detail": "auto",
            },
        }

    def _convert_tool_result_block(self, block: dict) -> dict:
        """Anthropic tool_result content block → OpenAI tool 消息"""
        tool_use_id = block.get("tool_use_id", "")
        result_content = block.get("content", "")

        # 展平 tool_result 的 content（可能是字符串或 content block 数组）
        if isinstance(result_content, list):
            parts = []
            for item in result_content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    parts.append(item)
            result_content = "\n".join(parts)

        return {
            "role": "tool",
            "tool_call_id": tool_use_id,
            "content": str(result_content),
        }

    # ── assistant 消息转换 ──

    def _convert_assistant_message(self, content) -> dict:
        """转换 assistant 消息

        content block 数组 → {role: "assistant", content: "...", tool_calls: [...]}
        字符串 → {role: "assistant", content: "..."}
        """
        if isinstance(content, str):
            return {"role": "assistant", "content": content}

        if not isinstance(content, list):
            return {"role": "assistant", "content": str(content)}

        texts = []
        tool_calls = []

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type == "text":
                texts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
            elif block_type == "thinking":
                # thinking 块通常不传给上游，跳过
                pass

        msg = {"role": "assistant"}
        msg["content"] = "".join(texts) if texts else None

        if tool_calls:
            msg["tool_calls"] = tool_calls

        return msg

    # ═══════════════════════════════════════════════════════════
    #  非流式响应转换：Chat JSON → Messages JSON
    # ═══════════════════════════════════════════════════════════

    def convert_response(self, body: str) -> str:
        """Chat Completions JSON → Anthropic Messages JSON

        还原内容：
        - 文本 → text content block
        - tool_calls → tool_use content block
        - reasoning_content → thinking content block
        """
        # 若输入是 Chat SSE 原文，先在插件内完成 SSE->Chat 聚合（协议语义在插件内处理）
        if isinstance(body, str) and body.lstrip().startswith("data: "):
            body = self._sse_chat_to_json(body)

        try:
            chat = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return body

        choice = (chat.get("choices", [{}]) or [{}])[0]
        message = choice.get("message", {})
        usage = chat.get("usage", {})

        ir = chat_message_to_ir(message)

        # tool 参数校验/修复（Messages 链路特有约束）
        valid_tool_calls = []
        for tc in ir.get("tool_calls") or []:
            tool_name = tc.get("name", "")
            tool_input = tc.get("arguments", {}) if isinstance(tc.get("arguments"), dict) else {}
            if not self._validate_tool_input(tool_name, tool_input):
                tool_input = self._repair_tool_input(tool_name, tool_input)
            if not self._validate_tool_input(tool_name, tool_input):
                self._tool_trace_events = getattr(self, "_tool_trace_events", [])
                self._tool_trace_events.append(f"drop_invalid_tool name={tool_name}")
                continue
            fixed = dict(tc)
            fixed["arguments"] = tool_input
            valid_tool_calls.append(fixed)
        ir["tool_calls"] = valid_tool_calls

        # ── 构建 content 数组 ──
        content_blocks = ir_to_messages_content(ir)

        # finish_reason → stop_reason
        finish = choice.get("finish_reason", "stop")
        stop_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "max_tokens": "max_tokens",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "content_filter": "end_turn",
        }
        stop_reason = stop_map.get(finish, "end_turn")

        # finish=tool_calls 但无有效 tool_use 时，降级为 end_turn，避免客户端状态机异常
        if stop_reason == "tool_use":
            has_tool_use = any(cb.get("type") == "tool_use" for cb in content_blocks if isinstance(cb, dict))
            if not has_tool_use:
                stop_reason = "end_turn"

        # 兜底：上游返回空完成帧（无文本、无工具、无推理）时补可见文本，避免 Claude 端“无返回”
        if not content_blocks:
            content_blocks.append({
                "type": "text",
                "text": "当前请求未产生可用响应，请重试一次。",
            })

        # ── 提取 token 信息 ──
        cached_tokens = 0
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached_tokens = prompt_details.get("cached_tokens", 0)

        resp = {
            "id": chat.get("id", f"msg_{uuid4().hex[:12]}"),
            "type": "message",
            "role": "assistant",
            "model": chat.get("model", ""),
            "content": content_blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "cached_tokens": cached_tokens,
            },
        }
        return json.dumps(resp, ensure_ascii=False)

    def _sse_chat_to_json(self, sse_text: str) -> str:
        """将 Chat Completions SSE 聚合为 Chat JSON（插件内版本）

        仅用于 messages 非流式路径，把协议语义（tool_calls 重组/finish 降级）下沉到插件。
        """
        content = ""
        reasoning = ""
        model = ""
        msg_id = ""
        usage = None
        finish_reason = "stop"
        tool_calls_map: dict[int, dict] = {}

        for line in sse_text.split("\n"):
            line = line.strip()
            if not line.startswith("data: ") or line.startswith("data: [DONE]"):
                continue
            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            if not model:
                model = chunk.get("model", "")
            if not msg_id:
                msg_id = chunk.get("id", "")
            if "usage" in chunk and chunk["usage"]:
                usage = chunk["usage"]

            choices = chunk.get("choices", []) or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta", {}) or {}

            # 文本与推理
            if delta.get("content"):
                content += delta["content"]
            if delta.get("reasoning_content"):
                reasoning += delta["reasoning_content"]

            # tool_calls 增量重组
            if delta.get("tool_calls"):
                for tc in delta["tool_calls"]:
                    idx = tc.get("index", 0)
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "id": tc.get("id", ""),
                            "type": tc.get("type", "function") or "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    cur = tool_calls_map[idx]
                    if tc.get("id"):
                        cur["id"] = tc["id"]
                    if tc.get("type"):
                        cur["type"] = tc["type"]
                    fn = tc.get("function", {}) or {}
                    if fn.get("name"):
                        cur["function"]["name"] = fn["name"]
                    if "arguments" in fn and fn.get("arguments") is not None:
                        cur["function"]["arguments"] += fn.get("arguments", "")

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

        result = {
            "id": msg_id,
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }],
        }

        if tool_calls_map:
            tool_calls = []
            for i in sorted(tool_calls_map.keys()):
                tc = tool_calls_map[i]
                if tc.get("function", {}).get("name"):
                    tool_calls.append(tc)
            if tool_calls:
                result["choices"][0]["message"]["tool_calls"] = tool_calls

        # finish=tool_calls 但无有效 tool_call 时降级为 stop
        if result["choices"][0].get("finish_reason") == "tool_calls":
            if not result["choices"][0]["message"].get("tool_calls"):
                result["choices"][0]["finish_reason"] = "stop"

        if reasoning:
            result["choices"][0]["message"]["reasoning_content"] = reasoning
        if usage:
            result["usage"] = usage

        return json.dumps(result, ensure_ascii=False)

    # ═══════════════════════════════════════════════════════════
    #  流式 SSE 转换：Chat SSE → Messages SSE
    # ═══════════════════════════════════════════════════════════

    async def convert_sse_stream(
        self, upstream_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[str]:
        """Chat Completions SSE 字节流 → Messages SSE 文本流

        增量还原：
        - 文本 delta → content_block_delta (text_delta)
        - tool_calls delta → content_block_start/delta/stop (tool_use)
        - reasoning_content delta → thinking content block
        - usage → message_delta
        """
        msg_id = f"msg_{uuid4().hex[:12]}"
        # 供 server.py 写审计日志时读取的内部标记
        self._fallback_thinking_to_text = False
        self._tool_trace_events: list[str] = []
        self._last_usage_tokens = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
        }
        model = ""
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        finish_reason = "end_turn"

        # ── 流状态跟踪 ──
        role_sent = False
        text_block_index = 0
        text_block_sent = False  # 曾发出过 text delta（有可见正文）
        text_block_emitted = False  # content_block_start 已发出
        tool_block_counter = 0
        thinking_block_index = None  # 推理内容块索引（如果有的话动态分配）
        thinking_block_sent = False
        thinking_text = ""
        thinking_mirrored_to_text = False
        stream_started_at = time.monotonic()
        first_thinking_at = None
        force_early_end_turn = False

        # tool_call 增量跟踪：{index: {id, name, args_buffer, started, claude_index}}
        current_tool_calls: dict[int, dict] = {}
        # 防循环保险丝：同名 + 同参数工具调用重复计数
        tool_signature_counts: dict[str, int] = {}
        loop_guard_triggered = False
        loop_guard_threshold = 2
        raw_stream_text = ""

        async for raw_line in upstream_stream:
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
            raw_stream_text += line
            if not line.startswith("data: "):
                continue
            if line.startswith("data: [DONE]"):
                break

            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            # ── 先提取 usage（上游可能在无 choices 的纯 usage chunk 中返回）──
            chunk_usage = chunk.get("usage", {})
            if chunk_usage:
                input_tokens = chunk_usage.get("prompt_tokens", 0) or input_tokens
                output_tokens = chunk_usage.get("completion_tokens", 0) or output_tokens
                prompt_details = chunk_usage.get("prompt_tokens_details")
                if isinstance(prompt_details, dict):
                    cached = prompt_details.get("cached_tokens", 0)
                    if cached:
                        cached_tokens = cached

            if not model:
                model = chunk.get("model", "")

            choices = chunk.get("choices", [])
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta", {})

            # ── 推理/思考内容 (reasoning_content) ──
            reasoning = delta.get("reasoning_content", "")
            if reasoning:
                if first_thinking_at is None:
                    first_thinking_at = time.monotonic()
                thinking_text += reasoning
                # 首次推理内容出现时初始化索引
                if thinking_block_index is None:
                    # 如果 text 块已在 role 中预发出（index=0），推理块用下一个索引
                    if text_block_emitted:
                        thinking_block_index = 1
                    else:
                        thinking_block_index = 0
                        text_block_index = 1  # 文本块索引顺延
                if not role_sent:
                    role_sent = True
                    yield _messages_sse_event("message_start", {
                        "type": "message_start",
                        "message": {
                            "id": msg_id,
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [],
                            "usage": {"input_tokens": input_tokens},
                        },
                    })
                if not thinking_block_sent:
                    thinking_block_sent = True
                    yield _messages_sse_event("content_block_start", {
                        "type": "content_block_start",
                        "index": thinking_block_index,
                        "content_block": {"type": "thinking", "thinking": ""},
                    })
                yield _messages_sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": thinking_block_index,
                    "delta": {"type": "thinking_delta", "thinking": reasoning},
                })

                # 可见性增强：当长时间只有 thinking 而无正文/工具时，提前镜像一份可见文本。
                # 这样 Claude Code 不会一直显示 Razzmatazzing/Channeling 而无任何可见输出。
                has_any_tool_started = any(tc.get("started") for tc in current_tool_calls.values())
                if (
                    (not thinking_mirrored_to_text)
                    and (not text_block_sent)
                    and (not has_any_tool_started)
                    and len(thinking_text) >= 120
                ):
                    self._fallback_thinking_to_text = True
                    if not text_block_emitted:
                        text_block_emitted = True
                        yield _messages_sse_event("content_block_start", {
                            "type": "content_block_start",
                            "index": text_block_index,
                            "content_block": {"type": "text", "text": ""},
                        })
                    preview = thinking_text[:200]
                    yield _messages_sse_event("content_block_delta", {
                        "type": "content_block_delta",
                        "index": text_block_index,
                        "delta": {"type": "text_delta", "text": preview},
                    })
                    text_block_sent = True
                    thinking_mirrored_to_text = True

                # 超时收敛：若持续仅有 thinking 且无正文/工具，主动结束本轮，避免客户端长时间“Flowing”。
                has_any_tool_started = any(tc.get("started") for tc in current_tool_calls.values())
                elapsed = time.monotonic() - (first_thinking_at or stream_started_at)
                if (not text_block_sent) and (not has_any_tool_started) and elapsed >= 8.0:
                    force_early_end_turn = True
                    self._tool_trace_events.append("early_end_turn_only_thinking")
                    break

            # ── role delta（消息开始，去重）──
            if delta.get("role") and not role_sent:
                role_sent = True
                yield _messages_sse_event("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "usage": {"input_tokens": input_tokens},
                    },
                })

            # ── 文本 delta（字段存在即处理，避免空串被误吞）──
            if "content" in delta and delta.get("content") is not None:
                text = delta["content"]
                if not role_sent:
                    role_sent = True
                    yield _messages_sse_event("message_start", {
                        "type": "message_start",
                        "message": {
                            "id": msg_id,
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [],
                            "usage": {"input_tokens": input_tokens},
                        },
                    })
                if not text_block_emitted:
                    text_block_emitted = True
                    yield _messages_sse_event("content_block_start", {
                        "type": "content_block_start",
                        "index": text_block_index,
                        "content_block": {"type": "text", "text": ""},
                    })
                # 仅当有可见文本时，才标记 text_block_sent
                if text:
                    text_block_sent = True
                yield _messages_sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": text_block_index,
                    "delta": {"type": "text_delta", "text": text},
                })

            # ── tool_calls delta（增量 tool_use）──
            if "tool_calls" in delta and delta["tool_calls"]:
                for tc_delta in delta["tool_calls"]:
                    tc_index = tc_delta.get("index", 0)

                    # 初始化 tool call 跟踪
                    if tc_index not in current_tool_calls:
                        current_tool_calls[tc_index] = {
                            "id": None,
                            "name": None,
                            "args_buffer": "",
                            "started": False,
                            "done": False,
                            "args_flushed": False,
                            "valid": False,
                        }

                    tc = current_tool_calls[tc_index]

                    # 更新 id 和 name
                    if tc_delta.get("id"):
                        tc["id"] = tc_delta["id"]

                    func_data = tc_delta.get("function", {})
                    if func_data.get("name"):
                        tc["name"] = func_data["name"]

                    # 工具参数 delta（增量 JSON）
                    if "arguments" in func_data:
                        part = func_data.get("arguments") or ""
                        # 无论是否 started，先缓冲，避免“参数先于 name/id 到达”时丢片段
                        tc["args_buffer"] += part

                        # 尝试标记 JSON 是否完整，并在“完整+通过schema校验”后再发 tool_use
                        try:
                            parsed = json.loads(tc["args_buffer"] or "{}")
                            tc["done"] = True
                            tool_name = tc.get("name") or ""
                            parsed_dict = parsed if isinstance(parsed, dict) else {}
                            if not self._validate_tool_input(tool_name, parsed_dict):
                                parsed_dict = self._repair_tool_input(tool_name, parsed_dict)
                                # 把修复后的参数写回缓冲，确保下游拿到的是可用参数
                                tc["args_buffer"] = json.dumps(parsed_dict, ensure_ascii=False)
                            tc["valid"] = self._validate_tool_input(tool_name, parsed_dict)

                            if tc["id"] and tc["name"] and tc["valid"] and not tc["started"]:
                                # 循环判定签名：name + args + tool_call_id。
                                # 关键点：同一轮里不同 call_id 的多个 Read/Bash 属于合法并行工具调用，
                                # 不应被误判为循环；仅当同一 call_id 反复下发相同参数才计数。
                                sig = f"{tc['name']}|{tc['args_buffer'] or '{}'}|{tc.get('id') or tc_index}"
                                tool_signature_counts[sig] = tool_signature_counts.get(sig, 0) + 1
                                if tool_signature_counts[sig] > loop_guard_threshold:
                                    loop_guard_triggered = True
                                    self._tool_trace_events.append(
                                        f"loop_guard_drop sig={tc['name']} id={tc.get('id') or ''} cnt={tool_signature_counts[sig]}"
                                    )
                                    # 不再继续下发重复工具块，避免客户端陷入循环等待
                                    tc["started"] = False
                                    tc["valid"] = False
                                    continue
                                if not role_sent:
                                    role_sent = True
                                    yield _messages_sse_event("message_start", {
                                        "type": "message_start",
                                        "message": {
                                            "id": msg_id,
                                            "type": "message",
                                            "role": "assistant",
                                            "model": model,
                                            "content": [],
                                            "usage": {"input_tokens": input_tokens},
                                        },
                                    })
                                tool_block_counter += 1
                                tc["started"] = True
                                claude_index = text_block_index + tool_block_counter
                                tc["claude_index"] = claude_index
                                self._tool_trace_events.append(
                                    f"tool_start tc={tc_index} cb={claude_index} name={tc['name']}"
                                )
                                yield _messages_sse_event("content_block_start", {
                                    "type": "content_block_start",
                                    "index": claude_index,
                                    "content_block": {
                                        "type": "tool_use",
                                        "id": tc["id"],
                                        "name": tc["name"],
                                        "input": {},
                                    },
                                })
                                # 完整 JSON 一次性发出，避免碎片导致解析失败
                                yield _messages_sse_event("content_block_delta", {
                                    "type": "content_block_delta",
                                    "index": tc["claude_index"],
                                    "delta": {
                                        "type": "input_json_delta",
                                        "partial_json": tc["args_buffer"] or "{}",
                                    },
                                })
                                tc["args_flushed"] = True
                                self._tool_trace_events.append(
                                    f"tool_delta tc={tc_index} cb={tc['claude_index']} len={len(tc['args_buffer'] or '{}')} full"
                                )
                        except json.JSONDecodeError:
                            pass

                    # 参数为空对象场景：name/id 已到但 arguments 可能为空字符串，尝试兜底发 {}
                    if tc["id"] and tc["name"] and not tc["started"] and tc["args_buffer"] in ("", None):
                        if self._validate_tool_input(tc["name"], {}):
                            if not role_sent:
                                role_sent = True
                                yield _messages_sse_event("message_start", {
                                    "type": "message_start",
                                    "message": {
                                        "id": msg_id,
                                        "type": "message",
                                        "role": "assistant",
                                        "model": model,
                                        "content": [],
                                        "usage": {"input_tokens": input_tokens},
                                    },
                                })
                            tool_block_counter += 1
                            tc["started"] = True
                            tc["done"] = True
                            tc["valid"] = True
                            claude_index = text_block_index + tool_block_counter
                            tc["claude_index"] = claude_index
                            self._tool_trace_events.append(
                                f"tool_start tc={tc_index} cb={claude_index} name={tc['name']} empty_args"
                            )
                            yield _messages_sse_event("content_block_start", {
                                "type": "content_block_start",
                                "index": claude_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tc["id"],
                                    "name": tc["name"],
                                    "input": {},
                                },
                            })
                            yield _messages_sse_event("content_block_delta", {
                                "type": "content_block_delta",
                                "index": tc["claude_index"],
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": "{}",
                                },
                            })
                            tc["args_flushed"] = True

            # ── finish_reason ──
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
                stop_map = {
                    "stop": "end_turn",
                    "length": "max_tokens",
                    "max_tokens": "max_tokens",
                    "tool_calls": "tool_use",
                    "function_call": "tool_use",
                    "content_filter": "end_turn",
                }
                finish_reason = stop_map.get(finish, "end_turn")

        # ═══════════════════════════════════════
        #  SSE 收尾：content_block_stop × N
        # ═══════════════════════════════════════

        # 兜底救援：若流式阶段完全未产出 role（常见于上游 SSE 被分片导致逐行解析失败），
        # 尝试基于完整原始 SSE 文本聚合为 Chat JSON，再一次性还原为 Messages 事件。
        if (not role_sent) and raw_stream_text and ("data:" in raw_stream_text):
            try:
                chat_json_text = self._sse_chat_to_json(raw_stream_text)
                msg_json_text = self.convert_response(chat_json_text)
                msg_obj = json.loads(msg_json_text)
                content_blocks = msg_obj.get("content", []) if isinstance(msg_obj, dict) else []
                usage_obj = msg_obj.get("usage", {}) if isinstance(msg_obj, dict) else {}

                yield _messages_sse_event("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": msg_obj.get("id", msg_id),
                        "type": "message",
                        "role": "assistant",
                        "model": msg_obj.get("model", model),
                        "content": [],
                        "usage": {"input_tokens": usage_obj.get("input_tokens", input_tokens)},
                    },
                })

                for i, cb in enumerate(content_blocks):
                    if not isinstance(cb, dict):
                        continue
                    cb_type = cb.get("type")
                    if cb_type == "thinking":
                        yield _messages_sse_event("content_block_start", {
                            "type": "content_block_start",
                            "index": i,
                            "content_block": {"type": "thinking", "thinking": ""},
                        })
                        yield _messages_sse_event("content_block_delta", {
                            "type": "content_block_delta",
                            "index": i,
                            "delta": {"type": "thinking_delta", "thinking": cb.get("thinking", "")},
                        })
                    elif cb_type == "tool_use":
                        yield _messages_sse_event("content_block_start", {
                            "type": "content_block_start",
                            "index": i,
                            "content_block": {
                                "type": "tool_use",
                                "id": cb.get("id", ""),
                                "name": cb.get("name", ""),
                                "input": {},
                            },
                        })
                        yield _messages_sse_event("content_block_delta", {
                            "type": "content_block_delta",
                            "index": i,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(cb.get("input", {}), ensure_ascii=False),
                            },
                        })
                    else:
                        yield _messages_sse_event("content_block_start", {
                            "type": "content_block_start",
                            "index": i,
                            "content_block": {"type": "text", "text": ""},
                        })
                        yield _messages_sse_event("content_block_delta", {
                            "type": "content_block_delta",
                            "index": i,
                            "delta": {"type": "text_delta", "text": cb.get("text", "")},
                        })

                    yield _messages_sse_event("content_block_stop", {
                        "type": "content_block_stop",
                        "index": i,
                    })

                yield _messages_sse_event("message_delta", {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": msg_obj.get("stop_reason", "end_turn"),
                        "stop_sequence": msg_obj.get("stop_sequence"),
                    },
                    "usage": {
                        "input_tokens": usage_obj.get("input_tokens", input_tokens),
                        "output_tokens": usage_obj.get("output_tokens", output_tokens),
                        "cached_tokens": usage_obj.get("cached_tokens", cached_tokens),
                    },
                })
                self._last_usage_tokens = {
                    "prompt_tokens": usage_obj.get("input_tokens", input_tokens) or 0,
                    "completion_tokens": usage_obj.get("output_tokens", output_tokens) or 0,
                    "total_tokens": (usage_obj.get("input_tokens", input_tokens) or 0)
                    + (usage_obj.get("output_tokens", output_tokens) or 0),
                    "cached_tokens": usage_obj.get("cached_tokens", cached_tokens) or 0,
                }
                yield _messages_sse_event("message_stop", {"type": "message_stop"})
                yield "data: [DONE]\n\n"
                return
            except Exception:
                pass

        # 协议兜底：若上游全程未给 role/content/tool/reasoning，
        # 也必须先发 message_start，避免直接 message_delta 导致客户端解析失败。
        if not role_sent:
            role_sent = True
            yield _messages_sse_event("message_start", {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "usage": {"input_tokens": input_tokens},
                },
            })

        # 推理 content_block_stop
        if thinking_block_sent:
            yield _messages_sse_event("content_block_stop", {
                "type": "content_block_stop",
                "index": thinking_block_index,
            })

        # 兜底：若整段只有 thinking 没有正文，补一条可见文本，避免 Claude CLI 看起来“无返回”
        if thinking_text and not text_block_sent:
            self._fallback_thinking_to_text = True
            if not text_block_emitted:
                text_block_emitted = True
                yield _messages_sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": text_block_index,
                    "content_block": {"type": "text", "text": ""},
                })
            yield _messages_sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": text_block_index,
                "delta": {"type": "text_delta", "text": thinking_text},
            })
            text_block_sent = True

        # 兜底：若既无正文、也无工具、也无推理，补一条可见文本
        has_any_tool_block = any(tc.get("started") and tc.get("valid") for tc in current_tool_calls.values())
        if (not text_block_sent) and (not thinking_block_sent) and (not has_any_tool_block):
            if not text_block_emitted:
                text_block_emitted = True
                yield _messages_sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": text_block_index,
                    "content_block": {"type": "text", "text": ""},
                })
            yield _messages_sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": text_block_index,
                "delta": {"type": "text_delta", "text": "当前请求未产生可用响应，请重试一次。"},
            })
            text_block_sent = True

        # 防循环保险丝触发后，强制给出可见提示，避免 Claude Code 长时间 Roosting。
        if loop_guard_triggered:
            if not text_block_emitted:
                text_block_emitted = True
                yield _messages_sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": text_block_index,
                    "content_block": {"type": "text", "text": ""},
                })
            yield _messages_sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": text_block_index,
                "delta": {
                    "type": "text_delta",
                    "text": "\n[循环保护] 检测到同一工具调用重复下发，已终止该工具链并结束本轮。",
                },
            })
            text_block_sent = True

        # 文本 content_block_stop
        if text_block_sent:
            yield _messages_sse_event("content_block_stop", {
                "type": "content_block_stop",
                "index": text_block_index,
            })

        # 工具 content_block_stop（按 index 排序）
        for tc in sorted(current_tool_calls.values(), key=lambda t: t.get("claude_index", 999)):
            if tc.get("started") and tc.get("claude_index") is not None:
                yield _messages_sse_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": tc["claude_index"],
                })
                self._tool_trace_events.append(
                    f"tool_stop cb={tc['claude_index']} done={1 if tc.get('done') else 0}"
                )

        # finish_reason=tool_use 但无任何有效 tool 块时降级为 end_turn
        has_any_tool_block = any(tc.get("started") and tc.get("valid") for tc in current_tool_calls.values())
        if finish_reason == "tool_use" and not has_any_tool_block:
            finish_reason = "end_turn"
        if loop_guard_triggered:
            finish_reason = "end_turn"
        if force_early_end_turn:
            finish_reason = "end_turn"

        # ── message_delta ──
        self._last_usage_tokens = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cached_tokens": cached_tokens,
        }
        yield _messages_sse_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": finish_reason, "stop_sequence": None},
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
            },
        })

        # ── message_stop + [DONE] ──
        yield _messages_sse_event("message_stop", {
            "type": "message_stop",
        })
        yield "data: [DONE]\n\n"


def _messages_sse_event(event_name: str, data: dict) -> str:
    """构建一条 Messages SSE 事件"""
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
