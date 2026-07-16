"""Messages 适配器公共编解码工具。"""

import json


def sse_chat_to_json(sse_text: str) -> str:
    """将 Chat Completions SSE 聚合为 Chat JSON。"""
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

        if delta.get("content"):
            content += delta["content"]
        if delta.get("reasoning_content"):
            reasoning += delta["reasoning_content"]

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

    if result["choices"][0].get("finish_reason") == "tool_calls":
        if not result["choices"][0]["message"].get("tool_calls"):
            result["choices"][0]["finish_reason"] = "stop"

    if reasoning:
        result["choices"][0]["message"]["reasoning_content"] = reasoning
    if usage:
        result["usage"] = usage

    return json.dumps(result, ensure_ascii=False)


def messages_sse_event(event_name: str, data: dict) -> str:
    """构建一条 Messages SSE 事件。"""
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
