"""Messages 流式转换辅助工具。"""

import json
from akm.plugins.protocol_converter._messages_codec import (
    sse_chat_to_json,
    messages_sse_event,
)


def build_rescue_messages_sse(
    adapter,
    raw_stream_text: str,
    msg_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
):
    """当主流式解析未产出 role 时，基于完整原始 SSE 做一次救援还原。

    返回: (events, usage_dict) 或 None
    """
    if (not raw_stream_text) or ("data:" not in raw_stream_text):
        return None

    try:
        chat_json_text = sse_chat_to_json(raw_stream_text)
        msg_json_text = adapter.convert_response(chat_json_text)
        msg_obj = json.loads(msg_json_text)
        if not isinstance(msg_obj, dict):
            return None
        content_blocks = msg_obj.get("content", [])
        usage_obj = msg_obj.get("usage", {}) if isinstance(msg_obj.get("usage"), dict) else {}

        lines: list[str] = []
        lines.append(messages_sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_obj.get("id", msg_id),
                "type": "message",
                "role": "assistant",
                "model": msg_obj.get("model", model),
                "content": [],
                "usage": {"input_tokens": usage_obj.get("input_tokens", input_tokens)},
            },
        }))

        for i, cb in enumerate(content_blocks):
            if not isinstance(cb, dict):
                continue
            cb_type = cb.get("type")
            if cb_type == "thinking":
                lines.append(messages_sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {"type": "thinking", "thinking": ""},
                }))
                lines.append(messages_sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "thinking_delta", "thinking": cb.get("thinking", "")},
                }))
            elif cb_type == "tool_use":
                lines.append(messages_sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {
                        "type": "tool_use",
                        "id": cb.get("id", ""),
                        "name": cb.get("name", ""),
                        "input": {},
                    },
                }))
                lines.append(messages_sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(cb.get("input", {}), ensure_ascii=False),
                    },
                }))
            else:
                lines.append(messages_sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {"type": "text", "text": ""},
                }))
                lines.append(messages_sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "text_delta", "text": cb.get("text", "")},
                }))

            lines.append(messages_sse_event("content_block_stop", {
                "type": "content_block_stop",
                "index": i,
            }))

        usage_ret = {
            "prompt_tokens": usage_obj.get("input_tokens", input_tokens) or 0,
            "completion_tokens": usage_obj.get("output_tokens", output_tokens) or 0,
            "cached_tokens": usage_obj.get("cached_tokens", cached_tokens) or 0,
        }
        usage_ret["total_tokens"] = usage_ret["prompt_tokens"] + usage_ret["completion_tokens"]

        lines.append(messages_sse_event("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": msg_obj.get("stop_reason", "end_turn"),
                "stop_sequence": msg_obj.get("stop_sequence"),
            },
            "usage": {
                "input_tokens": usage_ret["prompt_tokens"],
                "output_tokens": usage_ret["completion_tokens"],
                "cached_tokens": usage_ret["cached_tokens"],
            },
        }))
        lines.append(messages_sse_event("message_stop", {"type": "message_stop"}))
        lines.append("data: [DONE]\n\n")
        return lines, usage_ret
    except Exception:
        return None


def finalize_messages_sse(adapter, state: dict) -> list[str]:
    """构建 Messages SSE 收尾事件（block stop / message_delta / message_stop）。"""
    lines: list[str] = []

    role_sent = state["role_sent"]
    msg_id = state["msg_id"]
    model = state["model"]
    input_tokens = state["input_tokens"]
    output_tokens = state["output_tokens"]
    cached_tokens = state["cached_tokens"]
    thinking_block_sent = state["thinking_block_sent"]
    thinking_block_index = state["thinking_block_index"]
    thinking_text = state["thinking_text"]
    text_block_sent = state["text_block_sent"]
    text_block_emitted = state["text_block_emitted"]
    text_block_index = state["text_block_index"]
    current_tool_calls = state["current_tool_calls"]
    loop_guard_triggered = state["loop_guard_triggered"]
    force_early_end_turn = state["force_early_end_turn"]
    finish_reason = state["finish_reason"]

    if not role_sent:
        lines.append(messages_sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "usage": {"input_tokens": input_tokens},
            },
        }))

    if thinking_block_sent:
        lines.append(messages_sse_event("content_block_stop", {
            "type": "content_block_stop",
            "index": thinking_block_index,
        }))

    has_any_tool_block = any(tc.get("started") and tc.get("valid") for tc in current_tool_calls.values())
    if (not text_block_sent) and (not thinking_block_sent) and (not has_any_tool_block):
        if not text_block_emitted:
            text_block_emitted = True
            lines.append(messages_sse_event("content_block_start", {
                "type": "content_block_start",
                "index": text_block_index,
                "content_block": {"type": "text", "text": ""},
            }))
        lines.append(messages_sse_event("content_block_delta", {
            "type": "content_block_delta",
            "index": text_block_index,
            "delta": {"type": "text_delta", "text": "当前请求未产生可用响应，请重试一次。"},
        }))
        text_block_sent = True

    if loop_guard_triggered:
        if not text_block_emitted:
            text_block_emitted = True
            lines.append(messages_sse_event("content_block_start", {
                "type": "content_block_start",
                "index": text_block_index,
                "content_block": {"type": "text", "text": ""},
            }))
        lines.append(messages_sse_event("content_block_delta", {
            "type": "content_block_delta",
            "index": text_block_index,
            "delta": {
                "type": "text_delta",
                "text": "\n[循环保护] 检测到同一工具调用重复下发，已终止该工具链并结束本轮。",
            },
        }))
        text_block_sent = True

    if text_block_sent:
        lines.append(messages_sse_event("content_block_stop", {
            "type": "content_block_stop",
            "index": text_block_index,
        }))

    for tc in sorted(current_tool_calls.values(), key=lambda t: t.get("claude_index", 999)):
        if tc.get("started") and tc.get("claude_index") is not None:
            lines.append(messages_sse_event("content_block_stop", {
                "type": "content_block_stop",
                "index": tc["claude_index"],
            }))
            adapter._tool_trace_events.append(
                f"tool_stop cb={tc['claude_index']} done={1 if tc.get('done') else 0}"
            )

    has_any_tool_block = any(tc.get("started") and tc.get("valid") for tc in current_tool_calls.values())
    if finish_reason == "tool_use" and not has_any_tool_block:
        finish_reason = "end_turn"
    if loop_guard_triggered or force_early_end_turn:
        finish_reason = "end_turn"

    adapter._last_usage_tokens = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cached_tokens": cached_tokens,
    }
    lines.append(messages_sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": finish_reason, "stop_sequence": None},
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
        },
    }))
    lines.append(messages_sse_event("message_stop", {"type": "message_stop"}))
    lines.append("data: [DONE]\n\n")
    return lines


def update_loop_guard(
    signature_counts: dict[str, int],
    tool_name: str,
    args_json: str,
    call_id: str,
    tc_index: int,
    threshold: int,
) -> tuple[bool, str, int]:
    """更新循环保护计数并返回是否触发。

    返回: (triggered, signature, count)
    """
    signature = f"{tool_name}|{args_json or '{}'}|{call_id or tc_index}"
    count = signature_counts.get(signature, 0) + 1
    signature_counts[signature] = count
    return count > threshold, signature, count


def upsert_tool_call_state(current_tool_calls: dict[int, dict], tc_index: int, tc_delta: dict) -> tuple[dict, dict]:
    """初始化并更新单个 tool_call 的跟踪状态。"""
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
    if tc_delta.get("id"):
        tc["id"] = tc_delta["id"]

    func_data = tc_delta.get("function", {}) or {}
    if func_data.get("name"):
        tc["name"] = func_data["name"]

    return tc, func_data


def should_try_empty_args_tool_start(tc: dict) -> bool:
    """判断是否应尝试用空参数启动工具调用。"""
    return bool(tc.get("id") and tc.get("name") and (not tc.get("started")) and tc.get("args_buffer") in ("", None))


def build_tool_use_start_events(
    role_sent: bool,
    msg_id: str,
    model: str,
    input_tokens: int,
    claude_index: int,
    tool_id: str,
    tool_name: str,
    partial_json: str,
) -> tuple[list[str], bool]:
    """构建一次 tool_use 启动的 SSE 事件序列。"""
    lines: list[str] = []
    if not role_sent:
        role_sent = True
        lines.append(messages_sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "usage": {"input_tokens": input_tokens},
            },
        }))

    lines.append(messages_sse_event("content_block_start", {
        "type": "content_block_start",
        "index": claude_index,
        "content_block": {
            "type": "tool_use",
            "id": tool_id,
            "name": tool_name,
            "input": {},
        },
    }))
    lines.append(messages_sse_event("content_block_delta", {
        "type": "content_block_delta",
        "index": claude_index,
        "delta": {
            "type": "input_json_delta",
            "partial_json": partial_json,
        },
    }))
    return lines, role_sent


def handle_thinking_delta(
    thinking_text: str,
    reasoning_delta: str,
    role_sent: bool,
    text_block_sent: bool,
    text_block_emitted: bool,
    text_block_index: int,
    thinking_block_index: int | None,
    thinking_block_sent: bool,
    has_any_tool_started: bool,
    msg_id: str,
    model: str,
    input_tokens: int,
    first_thinking_at,
    stream_started_at,
) -> tuple[list[str], dict]:
    """处理 thinking 增量并返回事件与更新后的状态字段。"""
    lines: list[str] = []
    thinking_text = (thinking_text or "") + (reasoning_delta or "")

    if first_thinking_at is None:
        import time as _t
        first_thinking_at = _t.monotonic()

    if thinking_block_index is None:
        if text_block_emitted:
            thinking_block_index = 1
        else:
            thinking_block_index = 0
            text_block_index = 1

    if not role_sent:
        role_sent = True
        lines.append(messages_sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "usage": {"input_tokens": input_tokens},
            },
        }))

    if not thinking_block_sent:
        thinking_block_sent = True
        lines.append(messages_sse_event("content_block_start", {
            "type": "content_block_start",
            "index": thinking_block_index,
            "content_block": {"type": "thinking", "thinking": ""},
        }))

    lines.append(messages_sse_event("content_block_delta", {
        "type": "content_block_delta",
        "index": thinking_block_index,
        "delta": {"type": "thinking_delta", "thinking": reasoning_delta},
    }))

    # Claude Code 对 thinking block 有原生展示能力。
    # 这里保持协议语义纯净：仅输出 thinking_delta，不再镜像成正文预览，
    # 也不因为长时间只有 thinking 就提前伪造 end_turn。
    thinking_mirrored_to_text = False
    force_early_end_turn = False

    state = {
        "thinking_text": thinking_text,
        "role_sent": role_sent,
        "text_block_sent": text_block_sent,
        "text_block_emitted": text_block_emitted,
        "text_block_index": text_block_index,
        "thinking_block_index": thinking_block_index,
        "thinking_block_sent": thinking_block_sent,
        "thinking_mirrored_to_text": thinking_mirrored_to_text,
        "first_thinking_at": first_thinking_at,
        "force_early_end_turn": force_early_end_turn,
    }
    return lines, state


def ensure_message_start(
    role_sent: bool,
    msg_id: str,
    model: str,
    input_tokens: int,
) -> tuple[list[str], bool]:
    """确保 message_start 已发送。"""
    if role_sent:
        return [], role_sent
    return [messages_sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "usage": {"input_tokens": input_tokens},
        },
    })], True


def handle_text_delta(
    text: str,
    role_sent: bool,
    text_block_emitted: bool,
    text_block_sent: bool,
    text_block_index: int,
    msg_id: str,
    model: str,
    input_tokens: int,
) -> tuple[list[str], dict]:
    """处理 text delta 并返回事件与更新状态。"""
    lines: list[str] = []

    start_lines, role_sent = ensure_message_start(role_sent, msg_id, model, input_tokens)
    lines.extend(start_lines)

    if not text_block_emitted:
        text_block_emitted = True
        lines.append(messages_sse_event("content_block_start", {
            "type": "content_block_start",
            "index": text_block_index,
            "content_block": {"type": "text", "text": ""},
        }))

    if text:
        text_block_sent = True
    lines.append(messages_sse_event("content_block_delta", {
        "type": "content_block_delta",
        "index": text_block_index,
        "delta": {"type": "text_delta", "text": text},
    }))

    return lines, {
        "role_sent": role_sent,
        "text_block_emitted": text_block_emitted,
        "text_block_sent": text_block_sent,
    }


def map_finish_reason_to_stop_reason(finish_reason: str) -> str:
    """上游 finish_reason -> Messages stop_reason 映射。"""
    stop_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "max_tokens": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "end_turn",
    }
    return stop_map.get(finish_reason, "end_turn")


def append_tool_trace(adapter, message: str) -> None:
    """统一写入 tool trace，避免调用侧散落 append 细节。"""
    events = getattr(adapter, "_tool_trace_events", None)
    if not isinstance(events, list):
        adapter._tool_trace_events = []
        events = adapter._tool_trace_events
    events.append(message)


def normalize_tool_input(adapter, tool_name: str, tool_input: dict) -> tuple[bool, dict]:
    """统一执行 tool 参数校验与修复。

    返回: (是否有效, 修复后的输入)
    """
    current = tool_input if isinstance(tool_input, dict) else {}
    if not adapter._validate_tool_input(tool_name, current):
        current = adapter._repair_tool_input(tool_name, current)
    return adapter._validate_tool_input(tool_name, current), current


def make_finalize_state(
    role_sent: bool,
    msg_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    thinking_block_sent: bool,
    thinking_block_index,
    thinking_text: str,
    text_block_sent: bool,
    text_block_emitted: bool,
    text_block_index: int,
    current_tool_calls: dict,
    loop_guard_triggered: bool,
    force_early_end_turn: bool,
    finish_reason: str,
) -> dict:
    """构建收尾阶段所需状态字典，集中字段定义。"""
    return {
        "role_sent": role_sent,
        "msg_id": msg_id,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "thinking_block_sent": thinking_block_sent,
        "thinking_block_index": thinking_block_index,
        "thinking_text": thinking_text,
        "text_block_sent": text_block_sent,
        "text_block_emitted": text_block_emitted,
        "text_block_index": text_block_index,
        "current_tool_calls": current_tool_calls,
        "loop_guard_triggered": loop_guard_triggered,
        "force_early_end_turn": force_early_end_turn,
        "finish_reason": finish_reason,
    }


def append_tool_arguments_delta(tc: dict, func_data: dict) -> bool:
    """将 tool arguments 增量拼接到缓冲区。返回是否有 arguments 字段。"""
    if "arguments" not in func_data:
        return False
    part = func_data.get("arguments") or ""
    tc["args_buffer"] += part
    return True


def parse_tool_args_buffer(args_buffer: str) -> tuple[bool, dict]:
    """尝试解析工具参数缓冲区为 JSON 对象。"""
    try:
        parsed = json.loads(args_buffer or "{}")
    except json.JSONDecodeError:
        return False, {}
    if isinstance(parsed, dict):
        return True, parsed
    return True, {}


def start_tool_call_state(tc: dict, tool_block_counter: int, text_block_index: int) -> tuple[int, int]:
    """标记工具调用进入 started 状态并计算 Claude content block 索引。"""
    tool_block_counter += 1
    tc["started"] = True
    claude_index = text_block_index + tool_block_counter
    tc["claude_index"] = claude_index
    return tool_block_counter, claude_index


def mark_empty_args_tool_state(tc: dict) -> None:
    """标记空参数工具调用状态。"""
    tc["done"] = True
    tc["valid"] = True
