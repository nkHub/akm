"""协议转换内部 IR（中间表示）工具。

目标：在不改外部接口的前提下，统一 chat/messages/responses 三种格式
在“文本 + 推理 + 工具调用”维度的核心语义，减少多处重复映射与行为漂移。
"""

import json
from uuid import uuid4


def chat_message_to_ir(message: dict) -> dict:
    """Chat message -> IR。"""
    text = message.get("content") or ""
    reasoning = message.get("reasoning_content") or ""
    tool_calls_ir = []
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {}) or {}
        raw_args = fn.get("arguments", "{}")
        parsed_args = {}
        if isinstance(raw_args, str):
            try:
                v = json.loads(raw_args or "{}")
                if isinstance(v, dict):
                    parsed_args = v
            except json.JSONDecodeError:
                parsed_args = {}
        elif isinstance(raw_args, dict):
            parsed_args = raw_args
            raw_args = json.dumps(raw_args, ensure_ascii=False)
        tool_calls_ir.append({
            "id": tc.get("id", f"call_{uuid4().hex[:12]}"),
            "name": fn.get("name", ""),
            "arguments_raw": raw_args if isinstance(raw_args, str) else "{}",
            "arguments": parsed_args,
        })
    return {"text": text, "reasoning": reasoning, "tool_calls": tool_calls_ir}


def messages_content_to_ir(content_blocks: list) -> dict:
    """Messages content blocks -> IR。"""
    text_parts = []
    reasoning_parts = []
    tool_calls_ir = []
    for b in content_blocks or []:
        if not isinstance(b, dict):
            continue
        b_type = b.get("type")
        if b_type == "text":
            text_parts.append(b.get("text", ""))
        elif b_type == "thinking":
            reasoning_parts.append(b.get("thinking", ""))
        elif b_type == "tool_use":
            args = b.get("input", {})
            if not isinstance(args, dict):
                args = {}
            tool_calls_ir.append({
                "id": b.get("id", f"call_{uuid4().hex[:12]}"),
                "name": b.get("name", ""),
                "arguments_raw": json.dumps(args, ensure_ascii=False),
                "arguments": args,
            })
    return {
        "text": "".join(text_parts),
        "reasoning": "".join(reasoning_parts),
        "tool_calls": tool_calls_ir,
    }


def ir_to_messages_content(ir: dict) -> list:
    """IR -> Messages content blocks。"""
    blocks = []
    if ir.get("reasoning"):
        blocks.append({"type": "thinking", "thinking": ir["reasoning"], "signature": ""})
    if ir.get("text"):
        blocks.append({"type": "text", "text": ir["text"]})
    for tc in ir.get("tool_calls") or []:
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": tc.get("name", ""),
            "input": tc.get("arguments", {}) if isinstance(tc.get("arguments"), dict) else {},
        })
    return blocks


def ir_to_chat_message(ir: dict) -> dict:
    """IR -> Chat message。"""
    msg = {"role": "assistant", "content": ir.get("text", "")}
    if ir.get("reasoning"):
        msg["reasoning_content"] = ir["reasoning"]
    tool_calls = []
    for tc in ir.get("tool_calls") or []:
        tool_calls.append({
            "id": tc.get("id", f"call_{uuid4().hex[:12]}"),
            "type": "function",
            "function": {
                "name": tc.get("name", ""),
                "arguments": tc.get("arguments_raw") or json.dumps(tc.get("arguments", {}), ensure_ascii=False),
            },
        })
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def ir_to_responses_output(ir: dict) -> list:
    """IR -> Responses output items。"""
    output = []
    if ir.get("reasoning"):
        output.append({
            "id": f"rs_{uuid4().hex[:24]}",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": ir["reasoning"]}],
        })
    if ir.get("text"):
        output.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": ir["text"], "annotations": []}],
        })
    for tc in ir.get("tool_calls") or []:
        output.append({
            "type": "function_call",
            "call_id": tc.get("id", f"call_{uuid4().hex[:12]}"),
            "name": tc.get("name", ""),
            "arguments": tc.get("arguments_raw") or json.dumps(tc.get("arguments", {}), ensure_ascii=False),
        })
    return output
