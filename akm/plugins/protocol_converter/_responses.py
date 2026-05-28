"""
Responses 格式适配器

源格式为 Responses API，提供到 Chat Completions 的双向转换。
- Responses → Chat（请求转换）
- Chat SSE → Responses SSE（响应逆转换）
- Chat JSON → Responses JSON（非流式响应转换）

未实现：Responses → Messages（暂无需求，留空）
"""

import json
import time
import logging
from uuid import uuid4
from typing import AsyncIterator
from akm.adapter import BaseAdapter


logger = logging.getLogger("uvicorn.error")


class ResponsesAdapter(BaseAdapter):
    """Responses 格式适配器：源格式为 Responses API

    发送方向：convert_request()     — Responses → Chat（请求转换）
    接收方向：convert_sse_stream()  — Chat SSE → Responses SSE（响应逆转换）
    非流式：  convert_response()   — Chat JSON → Responses JSON
    """

    def __init__(self):
        self._namespace_map: dict[str, str] = {}  # full_name → namespace_prefix（用于 SSE 反向映射）
        self._id_counter: int = 0  # 每个适配器实例独立计数，避免 request 间碰撞

    def _reset_state(self):
        """每次请求前重置状态（namespace_map 和 id_counter）"""
        self._namespace_map = {}
        self._id_counter = 0

    # ── 请求转换：Responses → Chat Completions ──

    def convert_request(self, body: dict) -> dict:
        """Responses API 请求 → Chat Completions 请求"""
        self._reset_state()  # 每次新请求重置 namespace_map 等状态

        chat_body = {}

        if "model" in body:
            chat_body["model"] = body["model"]

        messages = self._input_to_messages(
            body.get("input"),
            body.get("instructions"),
            body.get("tools"),
        )
        chat_body["messages"] = messages

        # DeepSeek v4-pro 在消息列表以 tool 结尾时不继续生成，
        # 追加空 user 消息作为模型继续触发的 workaround
        if messages and messages[-1].get("role") == "tool":
            chat_body["messages"] = messages + [{"role": "user", "content": ""}]

        if "stream" in body:
            chat_body["stream"] = body["stream"]

        # Thinking / Effort 适配（DeepSeek Chat API）
        thinking = body.get("thinking")
        if isinstance(thinking, dict):
            t = thinking.get("type")
            if t in ("enabled", "disabled"):
                chat_body["thinking"] = {"type": t}

        effort_raw = None
        reasoning = body.get("reasoning")
        if isinstance(reasoning, dict):
            effort_raw = reasoning.get("effort")
        if effort_raw is None:
            effort_raw = body.get("reasoning_effort")
        output_config = body.get("output_config")
        if effort_raw is None and isinstance(output_config, dict):
            effort_raw = output_config.get("effort")

        effort = self._normalize_effort(effort_raw)
        if effort:
            chat_body["reasoning_effort"] = effort

        if "reasoning_effort" not in chat_body:
            chat_body["reasoning_effort"] = "high"
        if "thinking" not in chat_body:
            chat_body["thinking"] = {"type": "enabled"}

        logger.info(
            "[adapter:responses] thinking=%s reasoning_effort=%s src(reasoning.effort=%s reasoning_effort=%s output_config.effort=%s)",
            chat_body.get("thinking", {}).get("type") if isinstance(chat_body.get("thinking"), dict) else None,
            chat_body.get("reasoning_effort"),
            reasoning.get("effort") if isinstance(reasoning, dict) else None,
            body.get("reasoning_effort"),
            output_config.get("effort") if isinstance(output_config, dict) else None,
        )

        for key in ("temperature", "top_p", "max_tokens", "max_output_tokens", "stop"):
            if key in body:
                target = "max_tokens" if key == "max_output_tokens" else key
                chat_body[target] = body[key]

        if "tools" in body and body["tools"]:
            chat_body["tools"] = self._convert_tools(body["tools"])
        if "tool_choice" in body:
            chat_body["tool_choice"] = body["tool_choice"]

        return chat_body

    def _normalize_effort(self, effort) -> str | None:
        """将不同来源的 effort 规范为 DeepSeek 可接受值"""
        if effort is None:
            return None
        v = str(effort).strip().lower()
        if not v:
            return None
        if v in ("high", "max"):
            return v
        if v in ("low", "medium"):
            return "high"
        if v in ("xhigh", "very_high", "very-high"):
            return "max"
        return None

    def _input_to_messages(
        self,
        input_value,
        instructions: str | None = None,
        tools: list | None = None,
    ) -> list:
        """Responses API 的 input + instructions → Chat Completions 的 messages"""
        ROLE_MAP = {"developer": "system"}
        messages = []
        if instructions:
            messages.append({"role": "system", "content": instructions})

        if input_value is None:
            return messages
        if isinstance(input_value, str):
            messages.append({"role": "user", "content": input_value})
            return messages
        if not isinstance(input_value, list):
            return messages

        pending_tool_calls: list[dict] = []
        pending_reasoning = ""

        def _flush_tool_calls():
            nonlocal pending_tool_calls, pending_reasoning
            if pending_tool_calls:
                msg = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": pending_tool_calls,
                }
                if pending_reasoning:
                    msg["reasoning_content"] = pending_reasoning
                messages.append(msg)
                pending_tool_calls = []
                pending_reasoning = ""

        for item in input_value:
            if isinstance(item, str):
                _flush_tool_calls()
                messages.append({"role": "user", "content": item})
                continue

            item_type = item.get("type")

            if item_type == "function_call":
                pending_tool_calls.append({
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    }
                })
                if item.get("reasoning_content") and not pending_reasoning:
                    pending_reasoning = item["reasoning_content"]

            elif item_type == "function_call_output":
                _flush_tool_calls()
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "content": item.get("output", ""),
                })

            elif item_type == "input_text":
                _flush_tool_calls()
                messages.append({"role": "user", "content": item.get("text", "")})

            elif item_type == "input_image":
                _flush_tool_calls()
                messages.append({
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": item.get("image_url", "")}}]
                })

            elif item_type == "item_reference":
                _flush_tool_calls()
                messages.append({"role": "user", "content": f"[reference: {item.get('id', '')}]"})

            elif item_type == "message":
                _flush_tool_calls()
                role = item.get("role", "user")
                role = ROLE_MAP.get(role, role)
                content = item.get("content", "")

                if isinstance(content, list):
                    texts = []
                    tool_calls = []
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        c_type = c.get("type")
                        if c_type in ("text", "input_text", "output_text"):
                            t = c.get("text", "")
                            if t.strip():
                                texts.append(t)
                        elif c_type == "tool_call":
                            tool_calls.append({
                                "id": c.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": c.get("name", ""),
                                    "arguments": c.get("arguments", ""),
                                }
                            })
                    text_content = "\n".join(texts)
                    if tool_calls:
                        msg = {"role": role, "content": text_content or "", "tool_calls": tool_calls}
                        if item.get("reasoning_content"):
                            msg["reasoning_content"] = item["reasoning_content"]
                        messages.append(msg)
                    elif text_content:
                        msg = {"role": role, "content": text_content}
                        if item.get("reasoning_content"):
                            msg["reasoning_content"] = item["reasoning_content"]
                        messages.append(msg)
                elif isinstance(content, str) and content.strip():
                    msg = {"role": role, "content": content.strip()}
                    if item.get("reasoning_content"):
                        msg["reasoning_content"] = item["reasoning_content"]
                    messages.append(msg)

            elif "role" in item and not item_type:
                _flush_tool_calls()
                role = item["role"]
                role = ROLE_MAP.get(role, role)
                if role in ("user", "assistant", "system"):
                    content = item.get("content", "")
                    if isinstance(content, list):
                        content = self._flatten_content(content)
                    messages.append({"role": role, "content": content})

        _flush_tool_calls()

        # 消息重排序：确保 tool 结果紧跟 assistant(tool_calls)
        reordered = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                expected_ids = {tc["id"] for tc in msg["tool_calls"]}
                tool_msgs = []
                non_tool_msgs = []
                j = i + 1
                while j < len(messages) and expected_ids:
                    nxt = messages[j]
                    if nxt.get("role") == "tool" and nxt.get("tool_call_id") in expected_ids:
                        expected_ids.remove(nxt["tool_call_id"])
                        tool_msgs.append(nxt)
                    elif nxt.get("role") in ("system", "developer"):
                        non_tool_msgs.append(nxt)
                    else:
                        break
                    j += 1
                reordered.extend(non_tool_msgs)
                reordered.append(msg)
                reordered.extend(tool_msgs)
                i = j
            else:
                reordered.append(msg)
                i += 1

        return reordered

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

    def _clean_schema(self, obj):
        """递归清除 JSON Schema 中 DeepSeek 不支持的字段（additionalProperties、strict）"""
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

    def _convert_tools(self, tools: list) -> list:
        """Responses API tools → Chat Completions tools（自动清理不兼容字段）

        支持三种工具格式：
        1. {"type": "function", "function": {...}}  → 直接透传
        2. {"type": "namespace", "name": "...", "tools": [...]}  → 递归展开子工具，
           子工具名加命名空间前缀（如 mcp__translate__ + translate → mcp__translate__translate）
        3. {"name": "...", "parameters": {...}}  → 包装为 function 格式
        """
        result = []
        for t in tools:
            # ── 命名空间工具：递归展开子工具 ──
            if t.get("type") == "namespace" and isinstance(t.get("tools"), list):
                ns_prefix = t.get("name", "")
                # 去掉末尾的 __（如 "mcp__translate__" → "mcp__translate"），后面再加回来
                # 保留原始前缀确保子工具名与 Codex 内部注册名一致
                for sub_tool in t["tools"]:
                    sub_name = sub_tool.get("name", "")
                    if not sub_name:
                        sub_name = sub_tool.get("function", {}).get("name", "")
                    if not sub_name:
                        continue
                    full_name = ns_prefix + sub_name  # "mcp__translate__" + "translate" = "mcp__translate__translate"
                    # 存储反向映射，用于 SSE 转换时还原短名 + namespace
                    self._namespace_map[full_name] = ns_prefix
                    if sub_tool.get("function"):
                        # 子工具已是 function 格式，仅替换 name
                        expanded = dict(sub_tool)
                        expanded["function"] = dict(expanded["function"])
                        expanded["function"]["name"] = full_name
                        if "parameters" in expanded["function"]:
                            expanded["function"]["parameters"] = self._clean_schema(
                                expanded["function"]["parameters"]
                            )
                        result.append(expanded)
                    else:
                        # 子工具是简洁格式，包装为 function
                        sub_params = sub_tool.get("parameters") or {}
                        if not isinstance(sub_params, dict) or "type" not in sub_params:
                            sub_params = {"type": "object", "properties": {}}
                        result.append({
                            "type": "function",
                            "function": {
                                "name": full_name,
                                "description": sub_tool.get("description", ""),
                                "parameters": self._clean_schema(sub_params),
                            }
                        })
                continue

            name = t.get("name", "")
            if not name and t.get("function"):
                name = t["function"].get("name", "")
            if not name:
                continue
            if t.get("function"):
                cleaned = dict(t)
                if "parameters" in t.get("function", {}):
                    cleaned["function"] = dict(cleaned.get("function", {}))
                    cleaned["function"]["parameters"] = self._clean_schema(
                        cleaned["function"]["parameters"]
                    )
                result.append(cleaned)
            else:
                params = t.get("parameters") or {}
                if not params or not isinstance(params, dict) or "type" not in params:
                    params = {"type": "object", "properties": {}}
                result.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": t.get("description", ""),
                        "parameters": self._clean_schema(params),
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
        """Chat Completions SSE 字节流 → Responses API SSE 文本流

        将 DeepSeek Chat SSE 的 reasoning_content 映射为 Responses SSE 的独立推理项
        （type=reasoning），通过 response.reasoning_summary_text.delta 事件流式推送，
        使 Codex 能在「思考」面板中折叠展示推理过程，与正文输出分离。
        """
        resp_id = f"resp_{uuid4().hex[:24]}"
        created = int(time.time())
        model = ""
        content = ""
        reasoning = ""
        prompt_tokens = 0
        completion_tokens = 0

        tool_calls_state: dict[int, dict] = {}
        seen_tool_indices: set[int] = set()

        state = "init"
        in_reasoning = False
        reasoning_item_id = ""
        reasoning_seq = 0
        buffer = ""
        done = False
        seq = 0
        msg_id = ""

        async for raw_chunk in upstream_stream:
            if done:
                break
            buffer += raw_chunk.decode("utf-8", errors="replace") if isinstance(raw_chunk, bytes) else raw_chunk
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

                # ── 流开始时创建顶层响应和推理输出项 ──
                if state == "init" and (delta.get("role") or delta.get("reasoning_content") or delta.get("tool_calls")):
                    state = "started"
                    yield _sse_event("response.created", _make_response_created(resp_id, model, created))
                    yield _sse_event("response.in_progress", _make_response_in_progress(resp_id, model, created))

                    if delta.get("reasoning_content"):
                        in_reasoning = True
                        reasoning_item_id = f"rsn_{uuid4().hex[:12]}"
                        reasoning_seq = 0
                        yield _sse_event("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                "id": reasoning_item_id,
                                "type": "reasoning",
                                "status": "in_progress",
                            },
                        })

                # ── 推理内容：映射为 reasoning_summary_text.delta ──
                if delta.get("reasoning_content"):
                    if not in_reasoning:
                        in_reasoning = True
                        reasoning_item_id = f"rsn_{uuid4().hex[:12]}"
                        reasoning_seq = 0
                        yield _sse_event("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                "id": reasoning_item_id,
                                "type": "reasoning",
                                "status": "in_progress",
                            },
                        })
                    text = delta["reasoning_content"]
                    reasoning += text
                    reasoning_seq += 1
                    yield _sse_event("response.reasoning_summary_text.delta", {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": reasoning_item_id,
                        "output_index": 0,
                        "summary_index": 0,
                        "delta": text,
                        "sequence_number": reasoning_seq,
                    })

                # ── 正文或工具调用开始前，先关闭推理输出项 ──
                _has_non_reasoning = delta.get("content") is not None or delta.get("tool_calls")
                if _has_non_reasoning and in_reasoning:
                    yield _sse_event("response.reasoning_summary_text.done", {
                        "type": "response.reasoning_summary_text.done",
                        "item_id": reasoning_item_id,
                        "output_index": 0,
                        "summary_index": 0,
                        "text": reasoning,
                    })
                    yield _sse_event("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "id": reasoning_item_id,
                            "type": "reasoning",
                            "status": "completed",
                        },
                    })
                    in_reasoning = False

                # ── 正文输出：先创建 message 输出项 ──
                if (delta.get("content") or delta.get("tool_calls")) and not msg_id:
                    # 推理项的 output_index 已占用 0，message 从 1 开始
                    text_output_index = 1 if reasoning else 0
                    msg_id = f"msg_{uuid4().hex[:12]}"
                    yield _sse_event("response.output_item.added", _make_output_item_added(resp_id, text_output_index, msg_id, "in_progress"))
                    yield _sse_event("response.content_part.added", _make_content_part_added(resp_id, text_output_index, 0, ""))

                if delta.get("content"):
                    text = delta["content"]
                    content += text
                    seq += 1
                    yield _sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": msg_id,
                        "output_index": 1 if reasoning else 0,
                        "content_index": 0,
                        "delta": text,
                        "sequence_number": seq,
                    })

                if delta.get("tool_calls"):
                    for tc in delta["tool_calls"]:
                        idx = tc.get("index", 0)
                        fn = tc.get("function", {})

                        if idx not in seen_tool_indices:
                            seen_tool_indices.add(idx)
                            tc_id = tc.get("id", f"call_{uuid4().hex[:12]}")
                            tc_name = fn.get("name", "")
                            tc_args = fn.get("arguments", "")

                            # 根据 namespace_map 提取 namespace（MCP 工具还原短名）
                            ns_prefix = self._namespace_map.get(tc_name, "")
                            display_name = tc_name[len(ns_prefix):] if ns_prefix else tc_name

                            self._id_counter += 1
                            fc_item_id = f"fc_{uuid4().hex[:12]}"
                            tc_output_index = idx + (2 if reasoning else 1)

                            tool_calls_state[idx] = {
                                "id": tc_id,
                                "name": tc_name,
                                "display_name": display_name,
                                "namespace": ns_prefix,
                                "item_id": fc_item_id,
                                "arguments": tc_args,
                                "output_index": tc_output_index,
                                "start_seq": seq,  # 记录本 tool_call 出现时的 seq
                            }

                            item_data = {
                                "id": fc_item_id,
                                "type": "function_call",
                                "status": "in_progress",
                                "call_id": tc_id,
                                "name": display_name,
                                "arguments": tc_args,
                            }
                            if ns_prefix:
                                item_data["namespace"] = ns_prefix

                            yield _sse_event("response.output_item.added", {
                                "type": "response.output_item.added",
                                "output_index": tc_output_index,
                                "sequence_number": seq + 1,
                                "item": item_data,
                            })
                        else:
                            arg_delta = fn.get("arguments", "")
                            if arg_delta:
                                tool_calls_state[idx]["arguments"] += arg_delta
                                tc_info = tool_calls_state[idx]
                                yield _sse_event("response.function_call_arguments.delta", {
                                    "type": "response.function_call_arguments.delta",
                                    "item_id": tc_info["item_id"],
                                    "output_index": tc_info["output_index"],
                                    "delta": arg_delta,
                                    "sequence_number": seq + 2,
                                })

                if usage:
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)

        # ── 流结束 ──

        if state == "init":
            yield _sse_event("response.completed", {
                "type": "response.completed",
                "response": {
                    "id": resp_id, "object": "response",
                    "status": "completed", "model": model,
                    "output": [],
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                },
            })
            yield "data: [DONE]\n\n"
            return

        # ── 纯推理（无正文/工具调用）：关闭推理项，创建 message 展示推理内容 ──
        if in_reasoning:
            yield _sse_event("response.reasoning_summary_text.done", {
                "type": "response.reasoning_summary_text.done",
                "item_id": reasoning_item_id,
                "output_index": 0,
                "summary_index": 0,
                "text": reasoning,
            })
            yield _sse_event("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": reasoning_item_id,
                    "type": "reasoning",
                    "status": "completed",
                },
            })
            in_reasoning = False

        text_output_index = 1 if reasoning else 0

        # ── 无正文输出项时创建一个（纯 reasoning 或有 tool_calls）──
        if not msg_id:
            msg_id = f"msg_{uuid4().hex[:12]}"
            yield _sse_event("response.output_item.added", _make_output_item_added(resp_id, text_output_index, msg_id, "in_progress"))
            yield _sse_event("response.content_part.added", _make_content_part_added(resp_id, text_output_index, 0, ""))
            # 纯 reasoning 场景需要发送 text delta
            if reasoning and not content and not tool_calls_state:
                yield _sse_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": msg_id,
                    "output_index": text_output_index,
                    "content_index": 0,
                    "delta": reasoning,
                    "sequence_number": 1,
                })

        output_content = []
        if content:
            output_content.append({"type": "output_text", "text": content})
        elif reasoning and not tool_calls_state:
            output_content.append({"type": "output_text", "text": reasoning})
        elif tool_calls_state:
            output_content = [{"type": "output_text", "text": ""}]
        if not output_content:
            output_content = [{"type": "output_text", "text": ""}]
        final_text = content or (reasoning if not tool_calls_state else "")

        yield _sse_event("response.output_text.done", {
            "type": "response.output_text.done",
            "text": final_text,
            "item_id": msg_id,
            "output_index": text_output_index,
            "content_index": 0,
        })

        yield _sse_event("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": msg_id,
            "output_index": text_output_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": final_text},
        })
        text_output_item = {
            "id": msg_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": output_content,
        }
        if reasoning:
            text_output_item["reasoning_content"] = reasoning
        yield _sse_event("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": text_output_index,
            "item": text_output_item,
        })

        for idx in sorted(tool_calls_state.keys()):
            tc = tool_calls_state[idx]
            tc_output_index = tc.get("output_index", idx + (2 if reasoning else 1))
            tc_display_name = tc.get("display_name", tc["name"])
            tc_namespace = tc.get("namespace", "")

            yield _sse_event("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": tc["item_id"],
                "output_index": tc_output_index,
                "name": tc_display_name,
                "arguments": tc["arguments"],
            })

            done_item = {
                "id": tc["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": tc["id"],
                "name": tc_display_name,
                "arguments": tc["arguments"],
            }
            if tc_namespace:
                done_item["namespace"] = tc_namespace

            yield _sse_event("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": tc_output_index,
                "item": done_item,
            })

        total = prompt_tokens + completion_tokens
        resp_output = []
        if reasoning:
            resp_output.append({
                "type": "reasoning",
                "id": reasoning_item_id,
                "status": "completed",
                "content": [{
                    "type": "reasoning_summary_text",
                    "text": reasoning,
                }],
            })
        resp_output.append({
            "type": "message",
            "role": "assistant",
            "content": output_content,
        })
        for idx in sorted(tool_calls_state.keys()):
            tc = tool_calls_state[idx]
            tc_display_name = tc.get("display_name", tc["name"])
            tc_namespace = tc.get("namespace", "")
            fc_entry = {
                "type": "function_call",
                "id": tc["item_id"],
                "status": "completed",
                "call_id": tc["id"],
                "name": tc_display_name,
                "arguments": tc["arguments"],
            }
            if tc_namespace:
                fc_entry["namespace"] = tc_namespace
            resp_output.append(fc_entry)
        yield _sse_event("response.completed", {
            "type": "response.completed",
            "response": {
                "id": resp_id,
                "object": "response",
                "status": "completed",
                "model": model,
                "output": resp_output,
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


def _make_response_in_progress(resp_id: str, model: str, created: int) -> dict:
    return {
        "type": "response.in_progress",
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
