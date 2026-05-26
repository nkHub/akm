"""
Responses API ↔ Chat Completions 双向转换器

使仅支持 Chat Completions 的供应商（如 DeepSeek）可以在 Codex CLI 中使用。
Codex 通过 akm 的 /v1/responses 调用，akm 内部转为 /v1/chat/completions 发给 DeepSeek，
再将 Chat Completions SSE 流转为 Responses SSE 流返回给 Codex。
"""

import json
import time
from uuid import uuid4
from typing import AsyncIterator
from akm.adapter import BaseAdapter


class ChatToResponsesAdapter(BaseAdapter):
    """Chat Completions ↔ OpenAI Responses API 双向转换"""

    # ── 请求转换：Responses → Chat Completions ──

    def convert_request(self, body: dict) -> dict:
        """Responses API 请求 → Chat Completions 请求"""
        chat_body = {}

        # 模型名透传
        if "model" in body:
            chat_body["model"] = body["model"]

        # 构造 messages
        messages = self._input_to_messages(
            body.get("input"),
            body.get("instructions"),
            body.get("tools"),
        )
        chat_body["messages"] = messages

        # 流式标记
        if "stream" in body:
            chat_body["stream"] = body["stream"]

        # 其他参数透传
        for key in ("temperature", "top_p", "max_tokens", "max_output_tokens", "stop"):
            if key in body:
                target = "max_tokens" if key == "max_output_tokens" else key
                chat_body[target] = body[key]

        # tools 透传（Responses 格式 → Chat 格式转换）
        if "tools" in body and body["tools"]:
            chat_body["tools"] = self._convert_tools(body["tools"])
        if "tool_choice" in body:
            chat_body["tool_choice"] = body["tool_choice"]

        return chat_body

    def _input_to_messages(
        self,
        input_value,
        instructions: str | None = None,
        tools: list | None = None,
    ) -> list:
        """Responses API 的 input + instructions → Chat Completions 的 messages"""
        messages = []

        # system prompt 来自 instructions
        if instructions:
            messages.append({"role": "system", "content": instructions})

        # 解析 input
        if input_value is None:
            return messages

        if isinstance(input_value, str):
            messages.append({"role": "user", "content": input_value})
            return messages

        if not isinstance(input_value, list):
            return messages

        for item in input_value:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue

            item_type = item.get("type")

            # 先检查 type 字段（function_call, input_text 等没有 role）
            if item_type == "function_call":
                messages.append({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": item.get("call_id", ""),
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", ""),
                        }
                    }]
                })

            elif item_type == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "content": item.get("output", ""),
                })

            elif item_type == "input_text":
                messages.append({"role": "user", "content": item.get("text", "")})

            elif item_type == "input_image":
                messages.append({
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": item.get("image_url", "")}}]
                })

            elif item_type == "item_reference":
                messages.append({"role": "user", "content": f"[reference: {item.get('id', '')}]"})

            elif item_type == "message":
                content = item.get("content", "")
                if isinstance(content, list):
                    content = self._flatten_content(content)
                role = item.get("role", "user")
                if role == "developer":
                    role = "system"
                messages.append({"role": role, "content": content})

            # 有 role 字段的标准 message
            elif "role" in item and not item_type:
                role = item["role"]
                # DeepSeek 等供应商不支持 developer 角色，映射为 system
                if role == "developer":
                    role = "system"
                if role in ("user", "assistant", "system"):
                    content = item.get("content", "")
                    if isinstance(content, list):
                        content = self._flatten_content(content)
                    messages.append({"role": role, "content": content})

        return messages

    def _flatten_content(self, content_list: list) -> str:
        """content block 数组展平为纯文本"""
        texts = []
        for c in content_list:
            if isinstance(c, str):
                texts.append(c)
            elif c.get("type") == "input_text":
                texts.append(c.get("text", ""))
            elif c.get("type") == "output_text":
                texts.append(c.get("text", ""))
            elif c.get("type") == "refusal":
                texts.append("[refusal]")
        return "\n".join(texts) if texts else ""

    def _convert_tools(self, tools: list) -> list:
        """Responses API tools → Chat Completions tools"""
        result = []
        for t in tools:
            name = t.get("name", "")
            if not name and t.get("function"):
                name = t["function"].get("name", "")
            if not name:
                continue  # 跳过空名称的 tool（DeepSeek 不接受）
            if t.get("function"):
                result.append(t)
            else:
                params = t.get("parameters") or {}
                if not params or not isinstance(params, dict) or "type" not in params:
                    params = {"type": "object", "properties": {}}
                result.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": t.get("description", ""),
                        "parameters": params,
                        "strict": t.get("strict"),
                    }
                })
        return result

    # ── 非流式响应转换 ──

    def convert_response(self, body: str) -> str:
        """Chat Completions JSON → Responses API JSON"""
        try:
            chat = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return body

        choice = (chat.get("choices", [{}]) or [{}])[0]
        message = choice.get("message", {})
        usage = chat.get("usage", {})
        output_text = message.get("content", "")

        resp_id = chat.get("id", "").replace("chatcmpl-", "resp_")
        resp = {
            "id": resp_id or f"resp_{uuid4().hex[:24]}",
            "object": "response",
            "model": chat.get("model", ""),
            "status": "completed",
            "created_at": chat.get("created", 0),
            "output": [],
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        if output_text:
            resp["output"].append({
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": output_text, "annotations": []}],
            })

        return json.dumps(resp, ensure_ascii=False)

    # ── 流式 SSE 转换：Chat SSE → Responses SSE ──

    async def convert_sse_stream(
        self, upstream_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[str]:
        """Chat Completions SSE 字节流 → Responses API SSE 文本流"""
        resp_id = f"resp_{uuid4().hex[:24]}"
        created = int(time.time())
        model = ""
        content = ""
        prompt_tokens = 0
        completion_tokens = 0

        state = "init"
        buffer = ""  # 跨 chunk 缓冲
        done = False

        async for raw_chunk in upstream_stream:
            if done:
                break
            buffer += raw_chunk.decode("utf-8", errors="replace") if isinstance(raw_chunk, bytes) else raw_chunk
            # 按 \n\n 拆分成完整事件，未闭合的部分留在 buffer
            while "\n\n" in buffer:
                event_str, buffer = buffer.split("\n\n", 1)
                event_str = event_str.strip()
                if not event_str:
                    continue

                if not event_str.startswith("data: "):
                    continue
                if event_str.startswith("data: [DONE]"):
                    done = True
                    break

                try:
                    chunk = json.loads(event_str[6:])
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

                if delta.get("role") and state == "init":
                    state = "started"
                    msg_id = f"msg_{uuid4().hex[:12]}"
                    yield _sse_event("response.created", _make_response_created(resp_id, model, created))
                    yield _sse_event("response.output_item.added", _make_output_item_added(resp_id, 0, msg_id, "in_progress"))
                    yield _sse_event("response.content_part.added", _make_content_part_added(resp_id, 0, 0, ""))

                if delta.get("content"):
                    text = delta["content"]
                    content += text
                    yield _sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": text,
                    })

                if usage:
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)

        # 流结束，发送关闭事件
        yield _sse_event("response.content_part.done", {
            "type": "response.content_part.done",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": content},
        })
        yield _sse_event("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"type": "message", "status": "completed", "role": "assistant"},
        })
        total = prompt_tokens + completion_tokens
        yield _sse_event("response.completed", {
            "type": "response.completed",
            "response": {
                "id": resp_id,
                "object": "response",
                "model": model,
                "status": "completed",
                "output": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                }],
                "usage": {
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                    "total_tokens": total,
                },
            },
        })
        yield "data: [DONE]\n\n"


# ── SSE 事件构建工具函数 ──

def _sse_event(event_name: str, data: dict) -> str:
    """构建一条 SSE 事件"""
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _make_response_created(resp_id: str, model: str, created: int) -> dict:
    return {
        "type": "response.created",
        "response": {
            "id": resp_id,
            "object": "response",
            "model": model,
            "status": "in_progress",
            "created_at": created,
            "output": [],
        },
    }


def _make_output_item_added(resp_id, output_index, msg_id, status):
    return {
        "type": "response.output_item.added",
        "output_index": output_index,
        "item": {
            "id": msg_id,
            "type": "message",
            "status": status,
            "role": "assistant",
            "content": [],
        },
    }


def _make_content_part_added(resp_id, output_index, content_index, text):
    return {
        "type": "response.content_part.added",
        "output_index": output_index,
        "content_index": content_index,
        "part": {"type": "output_text", "text": text},
    }
