"""
Chat 格式适配器

源格式为 Chat Completions API，提供到 Responses 和 Messages 的双向转换。
当前实现聚焦 P0：补齐 chat <-> messages 最小可用转换链路。
"""

import json
from uuid import uuid4
from typing import AsyncIterator
from akm.adapter import BaseAdapter
from akm.plugins.protocol_converter._ir import messages_content_to_ir, ir_to_chat_message


class ChatAdapter(BaseAdapter):
    """Chat 格式适配器：源格式为 Chat Completions API

    发送方向：convert_request()     — Chat → Messages（最小实现）
    接收方向：convert_sse_stream()  — Messages SSE → Chat SSE
    非流式：  convert_response()   — Messages JSON/SSE → Chat JSON
    """

    def convert_request(self, body: dict) -> dict:
        """Chat 请求转换为 Messages 请求（最小可用版本）"""
        msg_body: dict = {
            "model": body.get("model", ""),
            "messages": [],
            "stream": body.get("stream", False),
        }

        if "max_tokens" in body:
            msg_body["max_tokens"] = body["max_tokens"]
        if "temperature" in body:
            msg_body["temperature"] = body["temperature"]
        if "top_p" in body:
            msg_body["top_p"] = body["top_p"]
        if "stop" in body:
            stop = body.get("stop")
            msg_body["stop_sequences"] = stop if isinstance(stop, list) else [str(stop)]

        # tools: OpenAI function tools -> Anthropic tools
        tools = body.get("tools") or []
        converted_tools = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                fn = t["function"]
                converted_tools.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
        if converted_tools:
            msg_body["tools"] = converted_tools

        # tool_choice: OpenAI -> Anthropic
        if "tool_choice" in body:
            tc = body.get("tool_choice")
            if isinstance(tc, str):
                if tc == "required":
                    msg_body["tool_choice"] = {"type": "any"}
                else:
                    msg_body["tool_choice"] = {"type": tc}
            elif isinstance(tc, dict):
                name = (tc.get("function") or {}).get("name")
                if name:
                    msg_body["tool_choice"] = {"type": "tool", "name": name}

        system_parts = []
        for m in body.get("messages", []):
            if not isinstance(m, dict):
                continue
            role = m.get("role", "user")
            content = m.get("content")

            if role == "system":
                if isinstance(content, str):
                    if content.strip():
                        system_parts.append(content)
                elif isinstance(content, list):
                    text = self._flatten_content_to_text(content)
                    if text:
                        system_parts.append(text)
                continue

            if role == "tool":
                msg_body["messages"].append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", ""),
                        "content": content if isinstance(content, str) else self._flatten_content_to_text(content),
                    }],
                })
                continue

            if role == "assistant":
                blocks = []
                if isinstance(content, str) and content:
                    blocks.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    blocks.extend(self._chat_content_to_messages_blocks(content))

                for tc in m.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function", {}) or {}
                    raw_args = fn.get("arguments", "{}")
                    try:
                        parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        parsed = {}
                    if not isinstance(parsed, dict):
                        parsed = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", f"toolu_{uuid4().hex[:12]}"),
                        "name": fn.get("name", ""),
                        "input": parsed,
                    })

                if not blocks:
                    blocks = [{"type": "text", "text": ""}]
                msg_body["messages"].append({"role": "assistant", "content": blocks})
                continue

            # user/其他角色兜底为 user
            user_blocks = []
            if isinstance(content, str):
                user_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                user_blocks.extend(self._chat_content_to_messages_blocks(content))
            else:
                user_blocks.append({"type": "text", "text": str(content or "")})
            msg_body["messages"].append({"role": "user", "content": user_blocks})

        if system_parts:
            msg_body["system"] = "\n\n".join(system_parts)

        return msg_body

    async def convert_sse_stream(
        self, upstream_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[str]:
        """Messages SSE 转 Chat SSE（最小可用版本）"""
        msg_id = f"chatcmpl-{uuid4().hex[:24]}"
        model = ""
        text_parts = []
        reasoning_parts = []
        tool_calls = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        finish_reason = "stop"

        buffer = ""
        async for raw in upstream_stream:
            chunk = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            buffer += chunk
            while "\n\n" in buffer:
                event_str, buffer = buffer.split("\n\n", 1)
                event_str = event_str.strip()
                if not event_str:
                    continue
                if event_str == "data: [DONE]":
                    continue

                event_name = ""
                data_json = None
                for line in event_str.split("\n"):
                    if line.startswith("event: "):
                        event_name = line[7:].strip()
                    elif line.startswith("data: "):
                        try:
                            data_json = json.loads(line[6:])
                        except json.JSONDecodeError:
                            data_json = None

                if not isinstance(data_json, dict):
                    continue

                if event_name == "message_start":
                    message = data_json.get("message", {})
                    model = message.get("model", model)
                    input_tokens = (message.get("usage", {}) or {}).get("input_tokens", 0)
                    usage["prompt_tokens"] = input_tokens
                    yield self._chat_sse_chunk(msg_id, model, {"role": "assistant"}, None)

                elif event_name == "content_block_delta":
                    delta = data_json.get("delta", {}) or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        text_parts.append(text)
                        yield self._chat_sse_chunk(msg_id, model, {"content": text}, None)
                    elif delta.get("type") == "thinking_delta":
                        t = delta.get("thinking", "")
                        reasoning_parts.append(t)
                        yield self._chat_sse_chunk(msg_id, model, {"reasoning_content": t}, None)
                    elif delta.get("type") == "input_json_delta":
                        partial = delta.get("partial_json", "")
                        idx = data_json.get("index", 0)
                        while len(tool_calls) <= idx:
                            tool_calls.append({
                                "id": f"call_{uuid4().hex[:12]}",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            })
                        tool_calls[idx]["function"]["arguments"] += partial
                        yield self._chat_sse_chunk(msg_id, model, {
                            "tool_calls": [{
                                "index": idx,
                                "id": tool_calls[idx]["id"],
                                "type": "function",
                                "function": {"arguments": partial},
                            }]
                        }, None)

                elif event_name == "content_block_start":
                    cb = data_json.get("content_block", {}) or {}
                    idx = data_json.get("index", 0)
                    if cb.get("type") == "tool_use":
                        while len(tool_calls) <= idx:
                            tool_calls.append({
                                "id": cb.get("id", f"call_{uuid4().hex[:12]}"),
                                "type": "function",
                                "function": {"name": cb.get("name", ""), "arguments": ""},
                            })
                        tool_calls[idx]["id"] = cb.get("id", tool_calls[idx]["id"])
                        tool_calls[idx]["function"]["name"] = cb.get("name", "")
                        yield self._chat_sse_chunk(msg_id, model, {
                            "tool_calls": [{
                                "index": idx,
                                "id": tool_calls[idx]["id"],
                                "type": "function",
                                "function": {"name": cb.get("name", ""), "arguments": ""},
                            }]
                        }, None)

                elif event_name == "message_delta":
                    delta = data_json.get("delta", {}) or {}
                    usage_obj = data_json.get("usage", {}) or {}
                    usage["completion_tokens"] = usage_obj.get("output_tokens", usage["completion_tokens"])
                    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                    stop_reason = delta.get("stop_reason", "end_turn")
                    finish_reason = "tool_calls" if stop_reason == "tool_use" else "stop"

        # 结束帧 + usage 帧 + DONE
        yield self._chat_sse_chunk(msg_id, model, {}, finish_reason)
        yield "data: " + json.dumps({
            "id": msg_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [],
            "usage": usage,
        }, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    def convert_response(self, body: str) -> str:
        """Messages JSON/SSE 转 Chat JSON（最小可用版本）"""
        if isinstance(body, str) and body.lstrip().startswith("event: "):
            body = self._messages_sse_to_chat_json(body)

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return body

        # 若已经是 chat，直接透传
        if data.get("object") == "chat.completion":
            return body

        # messages JSON -> chat JSON
        if data.get("type") == "message":
            message = self._messages_message_to_chat_message(data)
            usage = data.get("usage", {}) or {}
            prompt_tokens = usage.get("input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)
            chat = {
                "id": data.get("id", f"chatcmpl-{uuid4().hex[:24]}"),
                "object": "chat.completion",
                "model": data.get("model", ""),
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": self._stop_reason_to_finish_reason(data.get("stop_reason", "end_turn")),
                }],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
            return json.dumps(chat, ensure_ascii=False)

        return body

    def _messages_sse_to_chat_json(self, sse_text: str) -> str:
        """Messages SSE 文本聚合为 Chat JSON（用于非流式路径）"""
        model = ""
        msg_id = f"chatcmpl-{uuid4().hex[:24]}"
        text_parts = []
        reasoning_parts = []
        tool_calls_by_index: dict[int, dict] = {}
        prompt_tokens = 0
        completion_tokens = 0
        finish_reason = "stop"

        event_name = ""
        for line in sse_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("event: "):
                event_name = line[7:].strip()
                continue
            if line == "data: [DONE]":
                break
            if not line.startswith("data: "):
                continue

            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            if event_name == "message_start":
                message = data.get("message", {})
                model = message.get("model", model)
                usage = message.get("usage", {}) or {}
                prompt_tokens = usage.get("input_tokens", prompt_tokens)

            elif event_name == "content_block_start":
                cb = data.get("content_block", {}) or {}
                idx = data.get("index", 0)
                if cb.get("type") == "tool_use":
                    tool_calls_by_index[idx] = {
                        "id": cb.get("id", f"call_{uuid4().hex[:12]}"),
                        "type": "function",
                        "function": {"name": cb.get("name", ""), "arguments": ""},
                    }

            elif event_name == "content_block_delta":
                idx = data.get("index", 0)
                delta = data.get("delta", {}) or {}
                d_type = delta.get("type")
                if d_type == "text_delta":
                    text_parts.append(delta.get("text", ""))
                elif d_type == "thinking_delta":
                    reasoning_parts.append(delta.get("thinking", ""))
                elif d_type == "input_json_delta":
                    tc = tool_calls_by_index.get(idx)
                    if tc is None:
                        tc = {
                            "id": f"call_{uuid4().hex[:12]}",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                        tool_calls_by_index[idx] = tc
                    tc["function"]["arguments"] += delta.get("partial_json", "")

            elif event_name == "message_delta":
                usage = data.get("usage", {}) or {}
                completion_tokens = usage.get("output_tokens", completion_tokens)
                stop_reason = (data.get("delta", {}) or {}).get("stop_reason", "end_turn")
                finish_reason = "tool_calls" if stop_reason == "tool_use" else "stop"

        message = {
            "role": "assistant",
            "content": "".join(text_parts),
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls_by_index:
            message["tool_calls"] = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]

        result = {
            "id": msg_id,
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        return json.dumps(result, ensure_ascii=False)

    def _messages_message_to_chat_message(self, msg: dict) -> dict:
        ir = messages_content_to_ir(msg.get("content", []) or [])
        return ir_to_chat_message(ir)

    def _chat_content_to_messages_blocks(self, content: list) -> list:
        blocks = []
        for p in content:
            if not isinstance(p, dict):
                continue
            p_type = p.get("type")
            if p_type == "text":
                blocks.append({"type": "text", "text": p.get("text", "")})
            elif p_type == "image_url":
                image = p.get("image_url", {}) if isinstance(p.get("image_url"), dict) else {}
                url = image.get("url", "")
                if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                    header, b64 = url.split(";base64,", 1)
                    media_type = header[5:] if header.startswith("data:") else "image/png"
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    })
                elif url:
                    blocks.append({
                        "type": "image",
                        "source": {"type": "url", "url": url},
                    })
        return blocks

    def _flatten_content_to_text(self, content) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content or "")
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
        return "\n".join(parts)

    def _stop_reason_to_finish_reason(self, stop_reason: str) -> str:
        if stop_reason == "tool_use":
            return "tool_calls"
        if stop_reason == "max_tokens":
            return "length"
        return "stop"

    def _chat_sse_chunk(self, msg_id: str, model: str, delta: dict, finish_reason):
        return "data: " + json.dumps({
            "id": msg_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }],
        }, ensure_ascii=False) + "\n\n"
