"""
Anthropic Messages API ↔ OpenAI Chat Completions 双向转换器

支持将 Anthropic Messages API 请求转为 Chat Completions 格式（用于 OpenAI 兼容供应商），
并将 Chat Completions SSE 流转为 Messages SSE 流。
"""

import json
import time
from uuid import uuid4
from typing import AsyncIterator
from akm.adapter import BaseAdapter


class MessagesToChatAdapter(BaseAdapter):
    """Anthropic Messages ↔ OpenAI Chat Completions 双向转换"""

    # ── 请求转换：Anthropic Messages → Chat Completions ──

    def convert_request(self, body: dict) -> dict:
        """Anthropic Messages 请求 → Chat Completions 请求"""
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

        return chat_body

    def _messages_to_openai(self, body: dict) -> list:
        """构造 OpenAI messages 数组"""
        messages = []

        # system prompt 插入到 messages 最前面
        system = body.get("system")
        if system:
            content = self._flatten_content(system)
            if content:
                messages.append({"role": "system", "content": content})

        # Anthropic messages → OpenAI messages
        for m in body.get("messages", []):
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                content = self._flatten_anthropic_content(content)
            messages.append({"role": role, "content": content})

        return messages

    def _flatten_content(self, value) -> str:
        """展开 system 字段（string 或 content block 数组）"""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            texts = []
            for block in value:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif block.get("type") == "image":
                        texts.append("[image]")
                    elif block.get("type") == "tool_use":
                        texts.append(f"[tool_use: {block.get('name', '')}]")
                    elif block.get("type") == "tool_result":
                        c = block.get("content", "")
                        texts.append(str(c))
            return "\n".join(texts) if texts else ""
        return str(value)

    def _flatten_anthropic_content(self, content_list: list) -> str:
        """Anthropic content block 数组展平"""
        return self._flatten_content(content_list)

    # ── 非流式响应转换 ──

    def convert_response(self, body: str) -> str:
        """Chat Completions JSON → Anthropic Messages JSON"""
        try:
            chat = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return body

        choice = (chat.get("choices", [{}]) or [{}])[0]
        message = choice.get("message", {})
        usage = chat.get("usage", {})
        content = message.get("content", "")

        # Chat stop_reason → Anthropic stop_reason
        finish = choice.get("finish_reason", "stop")
        stop_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
        stop_reason = stop_map.get(finish, "end_turn")

        resp = {
            "id": chat.get("id", f"msg_{uuid4().hex[:12]}"),
            "type": "message",
            "role": "assistant",
            "model": chat.get("model", ""),
            "content": [{"type": "text", "text": content}] if content else [],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }
        return json.dumps(resp, ensure_ascii=False)

    # ── 流式 SSE 转换：Chat Completions SSE → Anthropic Messages SSE ──

    async def convert_sse_stream(
        self, upstream_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[str]:
        """Chat Completions SSE 字节流 → Anthropic Messages SSE 文本流"""
        msg_id = f"msg_{uuid4().hex[:12]}"
        model = ""
        content = ""
        input_tokens = 0
        output_tokens = 0
        finish_reason = "end_turn"
        role_sent = False
        block_sent = False

        async for raw_line in upstream_stream:
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data: "):
                continue
            if line.startswith("data: [DONE]"):
                break

            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            if not model:
                model = chunk.get("model", "")

            choices = chunk.get("choices", [])
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta", {})
            usage = chunk.get("usage", {})

            # 首次 role delta → message_start
            if delta.get("role") and not role_sent:
                role_sent = True
                yield _anthropic_event("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                    },
                })

            # content delta → content_block_start + content_block_delta
            if delta.get("content"):
                text = delta["content"]
                content += text

                if not block_sent:
                    block_sent = True
                    yield _anthropic_event("content_block_start", {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    })

                yield _anthropic_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                })

            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
                stop_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
                finish_reason = stop_map.get(finish, "end_turn")

            if usage:
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)

        # 流结束 → content_block_stop + message_delta + message_stop
        if block_sent:
            yield _anthropic_event("content_block_stop", {
                "type": "content_block_stop",
                "index": 0,
            })

        yield _anthropic_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": finish_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        })

        yield _anthropic_event("message_stop", {
            "type": "message_stop",
        })
        yield "data: [DONE]\n\n"


def _anthropic_event(event_name: str, data: dict) -> str:
    """构建一条 Anthropic SSE 事件"""
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
