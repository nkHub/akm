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
from akm.plugins.protocol_converter._messages_codec import (
    sse_chat_to_json,
    messages_sse_event as _messages_sse_event,
)
from akm.plugins.protocol_converter._messages_stream import (
    build_rescue_messages_sse,
    finalize_messages_sse,
    update_loop_guard,
    upsert_tool_call_state,
    should_try_empty_args_tool_start,
    build_tool_use_start_events,
    handle_thinking_delta,
    ensure_message_start,
    handle_text_delta,
    map_finish_reason_to_stop_reason,
    append_tool_trace,
    normalize_tool_input,
    make_finalize_state,
    append_tool_arguments_delta,
    parse_tool_args_buffer,
    start_tool_call_state,
    mark_empty_args_tool_state,
)


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
            body = sse_chat_to_json(body)

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
            valid, tool_input = normalize_tool_input(self, tool_name, tool_input)
            if not valid:
                self._tool_trace_events = getattr(self, "_tool_trace_events", [])
                append_tool_trace(self, f"drop_invalid_tool name={tool_name}")
                continue
            fixed = dict(tc)
            fixed["arguments"] = tool_input
            valid_tool_calls.append(fixed)
        ir["tool_calls"] = valid_tool_calls

        # ── 构建 content 数组 ──
        content_blocks = ir_to_messages_content(ir)

        # finish_reason → stop_reason
        finish = choice.get("finish_reason", "stop")
        stop_reason = map_finish_reason_to_stop_reason(finish)

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
                has_any_tool_started = any(tc.get("started") for tc in current_tool_calls.values())
                thinking_lines, thinking_state = handle_thinking_delta(
                    thinking_text=thinking_text,
                    reasoning_delta=reasoning,
                    role_sent=role_sent,
                    text_block_sent=text_block_sent,
                    text_block_emitted=text_block_emitted,
                    text_block_index=text_block_index,
                    thinking_block_index=thinking_block_index,
                    thinking_block_sent=thinking_block_sent,
                    has_any_tool_started=has_any_tool_started,
                    msg_id=msg_id,
                    model=model,
                    input_tokens=input_tokens,
                    first_thinking_at=first_thinking_at,
                    stream_started_at=stream_started_at,
                )
                for ln in thinking_lines:
                    yield ln

                thinking_text = thinking_state["thinking_text"]
                role_sent = thinking_state["role_sent"]
                text_block_sent = thinking_state["text_block_sent"]
                text_block_emitted = thinking_state["text_block_emitted"]
                text_block_index = thinking_state["text_block_index"]
                thinking_block_index = thinking_state["thinking_block_index"]
                thinking_block_sent = thinking_state["thinking_block_sent"]
                thinking_mirrored_to_text = thinking_state["thinking_mirrored_to_text"]
                first_thinking_at = thinking_state["first_thinking_at"]
                if thinking_mirrored_to_text:
                    self._fallback_thinking_to_text = True
                if thinking_state["force_early_end_turn"]:
                    force_early_end_turn = True
                    append_tool_trace(self, "early_end_turn_only_thinking")
                    break

            # ── role delta（消息开始，去重）──
            if delta.get("role") and not role_sent:
                role_lines, role_sent = ensure_message_start(
                    role_sent=role_sent,
                    msg_id=msg_id,
                    model=model,
                    input_tokens=input_tokens,
                )
                for ln in role_lines:
                    yield ln

            # ── 文本 delta（字段存在即处理，避免空串被误吞）──
            if "content" in delta and delta.get("content") is not None:
                text = delta["content"]
                text_lines, text_state = handle_text_delta(
                    text=text,
                    role_sent=role_sent,
                    text_block_emitted=text_block_emitted,
                    text_block_sent=text_block_sent,
                    text_block_index=text_block_index,
                    msg_id=msg_id,
                    model=model,
                    input_tokens=input_tokens,
                )
                for ln in text_lines:
                    yield ln
                role_sent = text_state["role_sent"]
                text_block_emitted = text_state["text_block_emitted"]
                text_block_sent = text_state["text_block_sent"]

            # ── tool_calls delta（增量 tool_use）──
            if "tool_calls" in delta and delta["tool_calls"]:
                for tc_delta in delta["tool_calls"]:
                    tc_index = tc_delta.get("index", 0)
                    tc, func_data = upsert_tool_call_state(current_tool_calls, tc_index, tc_delta)

                    # 工具参数 delta（增量 JSON）
                    if append_tool_arguments_delta(tc, func_data):

                        # 尝试标记 JSON 是否完整，并在“完整+通过schema校验”后再发 tool_use
                        try:
                            ok, parsed_dict = parse_tool_args_buffer(tc["args_buffer"])
                            if not ok:
                                raise json.JSONDecodeError("incomplete", tc["args_buffer"] or "", 0)
                            tc["done"] = True
                            tool_name = tc.get("name") or ""
                            tc["valid"], parsed_dict = normalize_tool_input(self, tool_name, parsed_dict)
                            # 把修复后的参数写回缓冲，确保下游拿到的是可用参数
                            tc["args_buffer"] = json.dumps(parsed_dict, ensure_ascii=False)

                            if tc["id"] and tc["name"] and tc["valid"] and not tc["started"]:
                                # 循环判定签名：name + args + tool_call_id。
                                # 关键点：同一轮里不同 call_id 的多个 Read/Bash 属于合法并行工具调用，
                                # 不应被误判为循环；仅当同一 call_id 反复下发相同参数才计数。
                                triggered, _sig, _count = update_loop_guard(
                                    signature_counts=tool_signature_counts,
                                    tool_name=tc["name"],
                                    args_json=tc["args_buffer"] or "{}",
                                    call_id=tc.get("id") or "",
                                    tc_index=tc_index,
                                    threshold=loop_guard_threshold,
                                )
                                if triggered:
                                    loop_guard_triggered = True
                                    append_tool_trace(self, f"loop_guard_drop sig={tc['name']} id={tc.get('id') or ''} cnt={_count}")
                                    # 不再继续下发重复工具块，避免客户端陷入循环等待
                                    tc["started"] = False
                                    tc["valid"] = False
                                    continue
                                tool_block_counter, claude_index = start_tool_call_state(
                                    tc=tc,
                                    tool_block_counter=tool_block_counter,
                                    text_block_index=text_block_index,
                                )
                                append_tool_trace(self, f"tool_start tc={tc_index} cb={claude_index} name={tc['name']}")
                                tool_lines, role_sent = build_tool_use_start_events(
                                    role_sent=role_sent,
                                    msg_id=msg_id,
                                    model=model,
                                    input_tokens=input_tokens,
                                    claude_index=claude_index,
                                    tool_id=tc["id"],
                                    tool_name=tc["name"],
                                    partial_json=tc["args_buffer"] or "{}",
                                )
                                for ln in tool_lines:
                                    yield ln
                                tc["args_flushed"] = True
                                append_tool_trace(self, f"tool_delta tc={tc_index} cb={tc['claude_index']} len={len(tc['args_buffer'] or '{}')} full")
                        except json.JSONDecodeError:
                            pass

                    # 参数为空对象场景：name/id 已到但 arguments 可能为空字符串，尝试兜底发 {}
                    if should_try_empty_args_tool_start(tc):
                        if self._validate_tool_input(tc["name"], {}):
                            tool_block_counter, claude_index = start_tool_call_state(
                                tc=tc,
                                tool_block_counter=tool_block_counter,
                                text_block_index=text_block_index,
                            )
                            mark_empty_args_tool_state(tc)
                            append_tool_trace(self, f"tool_start tc={tc_index} cb={claude_index} name={tc['name']} empty_args")
                            tool_lines, role_sent = build_tool_use_start_events(
                                role_sent=role_sent,
                                msg_id=msg_id,
                                model=model,
                                input_tokens=input_tokens,
                                claude_index=claude_index,
                                tool_id=tc["id"],
                                tool_name=tc["name"],
                                partial_json="{}",
                            )
                            for ln in tool_lines:
                                yield ln
                            tc["args_flushed"] = True

            # ── finish_reason ──
            if choice.get("finish_reason"):
                finish_reason = map_finish_reason_to_stop_reason(choice["finish_reason"])

        # ═══════════════════════════════════════
        #  SSE 收尾：content_block_stop × N
        # ═══════════════════════════════════════

        # 兜底救援：若流式阶段完全未产出 role（常见于上游 SSE 被分片导致逐行解析失败），
        # 尝试基于完整原始 SSE 文本聚合为 Chat JSON，再一次性还原为 Messages 事件。
        if (not role_sent) and raw_stream_text and ("data:" in raw_stream_text):
            rescued = build_rescue_messages_sse(
                adapter=self,
                raw_stream_text=raw_stream_text,
                msg_id=msg_id,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
            )
            if rescued:
                rescue_lines, rescue_usage = rescued
                self._last_usage_tokens = {
                    "prompt_tokens": rescue_usage.get("prompt_tokens", 0),
                    "completion_tokens": rescue_usage.get("completion_tokens", 0),
                    "total_tokens": rescue_usage.get("total_tokens", 0),
                    "cached_tokens": rescue_usage.get("cached_tokens", 0),
                }
                for line in rescue_lines:
                    yield line
                return

        state = make_finalize_state(
            role_sent=role_sent,
            msg_id=msg_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            thinking_block_sent=thinking_block_sent,
            thinking_block_index=thinking_block_index,
            thinking_text=thinking_text,
            text_block_sent=text_block_sent,
            text_block_emitted=text_block_emitted,
            text_block_index=text_block_index,
            current_tool_calls=current_tool_calls,
            loop_guard_triggered=loop_guard_triggered,
            force_early_end_turn=force_early_end_turn,
            finish_reason=finish_reason,
        )
        for line in finalize_messages_sse(self, state):
            yield line
