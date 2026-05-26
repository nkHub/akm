# OpenAI Chat Completions → Responses API 转换器设计

## 目标

让 akm 能将 Chat Completions 格式双向转换为 Responses API 格式，
使 DeepSeek 等仅支持 Chat Completions 的供应商可以在 OpenAI Codex CLI 中使用。
Codex 通过 akm 的 `/v1/responses` 调用，akm 内部转为 `/v1/chat/completions` 发给 DeepSeek。

## 零、两种 API 的核心差异

| 维度 | Chat Completions | Responses API |
|------|-----------------|---------------|
| 请求体字段 | `messages` | `input` + `instructions` |
| 流式事件类型 | `choices[].delta.content` | `response.output_text.delta` |
| 完成事件 | `choices[].finish_reason` | `response.completed` |
| usage 位置 | 在最后 chunk 中 | `response.completed` 的 `response.usage` |
| tool calls | `choices[].delta.tool_calls[]` | `response.output_item.added` + `function_call_arguments.delta` |
| 消息 ID | `id: chatcmpl-xxx` | `id: resp_xxx` |
| system 角色 | `messages[0]{role:"system"}` | `instructions` (string) |

---

## 一、请求转换：Responses → Chat Completions

### 1.1 字段映射

```
Responses API 请求                        Chat Completions 请求
═════════════════                        ════════════════════

{                                        {
  "model": "deepseek-v4-pro",            "model": "deepseek-v4-pro",
  "instructions": "你是...",      ───→     "messages": [
  "input": "你好",                 ───→       {"role": "system", "content": "你是..."},
  "stream": true,                 ───→       {"role": "user", "content": "你好"}
  "temperature": 0.7,             ───→     ],
  "max_output_tokens": 4096,      ───→     "stream": true,
  "top_p": 0.9,                   ───→     "temperature": 0.7,
  "previous_response_id": "...",   ️ 丢弃   "max_tokens": 4096,
  "tools": [...],                 ──→     "top_p": 0.9,
  "tool_choice": "auto",          ──→     "tools": [...格式转换...],
  "reasoning": {...},              ️ 丢弃   "tool_choice": "auto"
}                                        }
```

### 1.2 input 字段解析

`input` 可以是多种格式：

```python
def responses_input_to_messages(input_value, instructions, tools=None):
    """
    Responses API 的 input → Chat Completions 的 messages
    """
    messages = []

    # 1. system prompt 来自 instructions
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # 2. 解析 input
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
        return messages

    if not isinstance(input_value, list):
        return messages

    # input 是数组时逐项解析
    for item in input_value:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})

        elif item.get("role") in ("user", "assistant", "system", "developer"):
            # 直接是 message 对象
            content = item.get("content", "")
            if isinstance(content, list):
                content = _flatten_content_items(content)
            messages.append({"role": item["role"], "content": content})

        elif item.get("type") == "message":
            # 嵌套 message
            content = item.get("content", "")
            if isinstance(content, list):
                content = _flatten_content_items(content)
            role = item.get("role", "user")
            messages.append({"role": role, "content": content})

        elif item.get("type") == "function_call":
            # assistant 的工具调用结果
            messages.append({
                "role": "assistant",
                "tool_calls": [{
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "")
                    }
                }]
            })

        elif item.get("type") == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": item.get("output", "")
            })

        elif item.get("type") == "item_reference":
            # 引用之前消息，用占位文本
            messages.append({
                "role": "user",
                "content": f"[reference: {item.get('id', '')}]"
            })

        elif item.get("type") == "input_text":
            messages.append({"role": "user", "content": item.get("text", "")})

        elif item.get("type") == "input_image":
            messages.append({
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": item.get("image_url", "")}}]
            })
    return messages


def _flatten_content_items(content_list):
    """content block 数组 → 纯文本"""
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
```

### 1.3 tools 转换

```
Responses API tools:                     Chat Completions tools:
[                                         [
  {                                         {
    "type": "function",                       "type": "function",
    "name": "get_weather",                    "function": {
    "description": "...",                       "name": "get_weather",
    "parameters": {...},                        "description": "...",
    "strict": true                              "parameters": {...}
  }                                             }
]                                             }
                                            ]

基本一致，外层 key 名不同。直接映射即可。
```

### 1.4 需要丢弃的字段

```python
RESPONSES_ONLY_FIELDS = {
    "previous_response_id",  # 多轮对话引用，Chat 用 messages 数组代替
    "truncation",            # Chat 无此参数
    "conversation",          # Chat 无此参数
    "text",                  # {text: {format: ...}} 文本格式约束
    "reasoning",             # Chat 无此参数
    "include",               # 响应中包含的内容选项
    "parallel_tool_calls",   # API 版本差异可忽略
    "store",                 # Chat 无持久化
    "metadata",              # Chat 无此参数
}
```

---

## 二、响应转换：Chat Completions SSE → Responses SSE

### 2.1 SSE 事件映射

这是整个转换最复杂的部分。Chat Completions 流返回原始 SSE，需要逐行解析并重新包装为 Responses 事件。

```
Chat Completions SSE                      Responses API SSE
════════════════════                      ══════════════════

data: {id, object, created,
       model, choices:[{                   event: response.created
         delta:{role:"assistant"}          data: {type:"response.created",
       }]}                                   response:{id, status, ...}}

data: {choices:[{                         event: response.output_item.added
         delta:{content:"Hello"},        data: {type:"response.output_item.added",
         index:0                            output_index:0,
       }]}                                  item:{type:"message",...}}

                                          event: response.content_part.added
                                          data: {type:"response.content_part.added",
                                            output_index:0, content_index:0,
                                            part:{type:"output_text", text:""}}

                                          event: response.output_text.delta
data: {choices:[{                         data: {type:"response.output_text.delta",
         delta:{content:" world"},          output_index:0, content_index:0,
         index:0                            delta:"Hello"
       }]}                                }

                                          event: response.output_text.delta
                                          data: {delta:" world"}

                                          event: response.content_part.done
                                          data: {...}

                                          event: response.output_item.done
                                          data: {...}

data: {choices:[{                         event: response.completed
         finish_reason:"stop"}],         data: {type:"response.completed",
       usage:{prompt:10,compl:5,           response:{id, output:[], usage:{...}}}
       total:15
       }}

data: [DONE]                              data: [DONE]
```

### 2.2 核心转换算法（生成器）

```python
import json
import time
from uuid import uuid4

async def chat_sse_to_responses_sse(chat_stream):
    """
    Chat Completions SSE → Responses SSE
    边收边转，零缓冲（除必要状态）
    """
    resp_id = f"resp_{uuid4().hex[:24]}"
    created = int(time.time())
    model = ""
    content = ""
    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    finish_reason = "stop"

    state = "init"  # init → delta → done

    async for raw_line in chat_stream:
        line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data: "):
            continue
        if line.startswith("data: [DONE]"):
            break

        try:
            chunk = json.loads(line[6:])
        except json.JSONDecodeError:
            continue

        # 提取模型名（从第一个 chunk）
        if not model:
            model = chunk.get("model", "")

        choices = chunk.get("choices", [])
        if not choices:
            continue

        choice = choices[0]
        delta = choice.get("delta", {})
        usage = chunk.get("usage", {})

        if delta.get("role") and state == "init":
            # 首次收到 role delta → 发送 response.created
            state = "started"
            yield _make_response_created(resp_id, model, created)

            # 发送 output_item.added
            msg_id = f"msg_{uuid4().hex[:12]}"
            yield _make_output_item_added(resp_id, 0, msg_id)

            # 发送 content_part.added
            yield _make_content_part_added(resp_id, 0, 0, "")

        if delta.get("content"):
            content += delta["content"]
            yield _make_output_text_delta(resp_id, 0, 0, delta["content"])

        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

        if usage:
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens") or (prompt_tokens + completion_tokens)

    # 流结束 → 发送关闭事件
    yield _make_content_part_done(resp_id, 0, 0, content)
    yield _make_output_item_done(resp_id, 0)
    yield _make_response_completed(
        resp_id, model, content,
        prompt_tokens, completion_tokens, total_tokens
    )
    yield "data: [DONE]\n\n"


def _make_response_created(resp_id, model, created):
    return (
        f"event: response.created\n"
        f"data: {json.dumps({'type': 'response.created', 'response': {"
        f"'id': resp_id, 'object': 'response', 'model': model, "
        f"'status': 'in_progress', 'created_at': created, "
        f"'output': []}})}\n\n"
    )

def _make_output_item_added(resp_id, output_index, msg_id):
    return (
        f"event: response.output_item.added\n"
        f"data: {json.dumps({'type': 'response.output_item.added', "
        f"'output_index': output_index, "
        f"'item': {'id': msg_id, 'type': 'message', 'status': 'in_progress', "
        f"'role': 'assistant', 'content': []}})}\n\n"
    )

def _make_content_part_added(resp_id, output_index, content_index, text):
    return (
        f"event: response.content_part.added\n"
        f"data: {json.dumps({'type': 'response.content_part.added', "
        f"'output_index': output_index, 'content_index': content_index, "
        f"'part': {'type': 'output_text', 'text': text}})}\n\n"
    )

def _make_output_text_delta(resp_id, output_index, content_index, delta_text):
    return (
        f"event: response.output_text.delta\n"
        f"data: {json.dumps({'type': 'response.output_text.delta', "
        f"'output_index': output_index, 'content_index': content_index, "
        f"'delta': delta_text})}\n\n"
    )

def _make_content_part_done(resp_id, output_index, content_index, text):
    return (
        f"event: response.content_part.done\n"
        f"data: {json.dumps({'type': 'response.content_part.done', "
        f"'output_index': output_index, 'content_index': content_index, "
        f"'part': {'type': 'output_text', 'text': text}})}\n\n"
    )

def _make_output_item_done(resp_id, output_index):
    return (
        f"event: response.output_item.done\n"
        f"data: {json.dumps({'type': 'response.output_item.done', "
        f"'output_index': output_index, "
        f"'item': {'type': 'message', 'status': 'completed', "
        f"'role': 'assistant'}})}\n\n"
    )

def _make_response_completed(resp_id, model, output_text, prompt_tokens, completion_tokens, total_tokens):
    return (
        f"event: response.completed\n"
        f"data: {json.dumps({'type': 'response.completed', 'response': {"
        f"'id': resp_id, 'object': 'response', 'model': model, "
        f"'status': 'completed', "
        f"'output': [{{'type': 'message', 'role': 'assistant', "
        f"'content': [{{'type': 'output_text', 'text': output_text}}]}}], "
        f"'usage': {{'input_tokens': prompt_tokens, "
        f"'output_tokens': completion_tokens, "
        f"'total_tokens': total_tokens}}}})}\n\n"
    )

def _to_sse(data_str):
    """将字符串包装为 SSE data 行"""
    return f"data: {data_str}\n\n"
```

### 2.3 非流式转换

```python
def chat_completion_to_response(chat_body):
    """
    非流式 Chat Completions JSON → Responses API JSON
    """
    choice = chat_body.get("choices", [{}])[0]
    message = choice.get("message", {})
    usage = chat_body.get("usage", {})

    output_text = message.get("content", "")

    return {
        "id": chat_body.get("id", "").replace("chatcmpl-", "resp_"),
        "object": "response",
        "model": chat_body.get("model", ""),
        "status": "completed",
        "created_at": chat_body.get("created", 0),
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": output_text,
                "annotations": []
            }]
        }],
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "input_tokens_details": usage.get("prompt_tokens_details", {}),
            "output_tokens": usage.get("completion_tokens", 0),
            "output_tokens_details": usage.get("completion_tokens_details", {}),
            "total_tokens": usage.get("total_tokens", 0),
        }
    }
```

---

## 三、接入架构

### 3.1 触发机制

在 `forward_request` 中新增转换判断：

```python
# proxy.py
NEED_RESPONSES_CONVERSION = {
    "deepseek",   # DeepSeek 不支持 responses API
    # 未来可扩展其他供应商
}

async def forward_request(body, client, log_callback=None, api_path="chat/completions"):
    model = body.get("model", "")
    ...
    # 如果请求是 responses 格式但 key 的供应商不支持
    need_conv = api_path == "responses" and key["provider"] in NEED_RESPONSES_CONVERSION

    if need_conv:
        # 请求体转换
        upstream_body = responses_to_chat_body(body)
        upstream_path = "chat/completions"
    else:
        upstream_body = body
        upstream_path = api_path
```

### 3.2 流式转换接入

当前 `forward_request` 中流式透传的逻辑需要改造：
- 客户端流式 + 需要转换 → 使用 `chat_sse_to_responses_sse` 包装
- 客户端流式 + 不需要转换 → 直接透传（现有逻辑）
- 客户端非流式 + 需要转换 → 调用 `chat_completion_to_response`

### 3.3 完整数据流

```
Codex CLI
  │ POST /v1/responses (stream: true)
  │ body: {model: "deepseek-v4-pro", input: "...", instructions: "..."}
  ▼
akm server.py /v1/responses
  │ _handle_ai_request → forward_request
  ▼
akm proxy.py forward_request
  │ pick_key → 选中 gs (deepseek)
  │ 检测: provider=deepseek ∈ NEED_RESPONSES_CONVERSION
  │ 请求转换: responses_body → chat_body
  │ 目标 URL: https://api.deepseek.com/v1/chat/completions
  │ upstream_body["stream"] = True (内部始终 stream)
  ▼
DeepSeek API
  │ 返回 Chat Completions SSE
  ▼
akm proxy.py
  │ 检测需要转换 → chat_sse_to_responses_sse
  │ 逐行转换 SSE 事件类型
  │ return {stream: True, response: 转换后的生成器}
  ▼
akm server.py
  │ StreamingResponse(generator, media_type="text/event-stream")
  ▼
Codex CLI
  │ 收到 Responses API 格式 SSE
```

---

## 四、错误处理

### 4.1 上游错误映射

```python
# Chat 错误 → Responses 错误
CHAT_ERROR_TO_RESPONSES_STATUS = {
    429: 429,   # rate limit 保持一致
    401: 401,
    403: 403,
    500: 500,
    502: 502,
    503: 503,
}

def chat_error_to_responses(response, raw_error=""):
    return {
        "type": "error",
        "error": {
            "type": "api_error",
            "message": raw_error or f"HTTP {response.status_code}",
            "code": str(response.status_code)
        }
    }
```

### 4.2 转换异常处理

```python
try:
    chat_body = responses_to_chat_body(body)
except Exception as e:
    # 无法转换的请求体，返回 400
    return {
        "status_code": 400,
        "body": json.dumps({
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": f"无法转换 Response API 请求: {e}"
            }
        }),
        ...
    }
```

---

## 五、实现边界

### 必须实现（第一批）
- 纯文本对话（流式 + 非流式）
- instructions → system message
- input 字符串/数组解析
- SSE 事件生成器（response.created, output_text.delta, response.completed）
- usage token 映射
- 配置驱动开启（Key 的 provider 在转换白名单中）

### 可选实现（第二批）
- tools/function calling 互转
- content_part.done / output_item.done 事件
- previous_response_id → messages 历史拼接
- 图片 input 转发
- reasoning/thinking 内容传递

### 不实现
- Response 专有功能（store, conversation 等）
- item_reference 多轮引用（先简化为一维 messages）
