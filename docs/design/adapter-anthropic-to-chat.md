# Anthropic Messages → OpenAI Chat Completions 转换器设计

## 目标

让 akm 能将 Anthropic Messages API 格式转为 OpenAI Chat Completions 格式，
支持流式和非流式，使 Anthropic 的 API Key 可以通过 akm 的 `/v1/chat/completions` 访问。
同理，DeepSeek、OpenAI 兼容中转站的 Key 也可以被 Anthropic 客户端调用。

---

## 一、请求转换

### 1.1 Messages → Chat Completions

```
POST /v1/messages                       POST /v1/chat/completions
┌──────────────────────┐                ┌──────────────────────────┐
│ model                 │            ┌──│ model                    │
│ messages              │ ─────────→ │  │ messages                 │
│   [                   │   转换     │  │   [                      │
│     {                 │            │  │     {role, content}, ... │
│       role,           │            │  │   ]                      │
│       content,        │            │  │ stream                   │
│     }                 │            │  │ max_tokens               │
│   ]                   │            │  │ system                   │
│ system (prompt)       │──────────→ │  └──────────────────────────┘
│ stream                │──────────→ │
│ max_tokens            │──────────→ │
│ temperature           │──────────→ │
│ top_p                 │──────────→ │
│ top_k                 │   丢弃     │
│ stop_sequences        │──────────→ │  stop
│ metadata              │   丢弃     │
│ tools                 │─────→      │  tools（需转换）
└──────────────────────┘            ┌────╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┐
                                    │  Anthropic 特有字段单独存  │
                                    │  供响应转换阶段回填        │
                                    └──────────────────────────┘
```

#### 字段映射表

| Anthropic | OpenAI Chat | 处理方式 |
|-----------|-------------|----------|
| `model` | `model` | 直接透传（丢弃 `claude-` 前缀可选） |
| `messages[].role` | `messages[].role` | `user`/`assistant` 直接透传 |
| `messages[].content` | `messages[].content` | string→string, array→需展平 |
| `system` (string) | `messages[0]{role:system}` | 插入到 messages 数组最前面 |
| `system` (array) | 同上 | 拼接为单个 system message |
| `stream` | `stream` | 直接透传 |
| `max_tokens` | `max_tokens` | 直接透传 |
| `temperature` | `temperature` | 直接透传（范围不同，需注意） |
| `top_p` | `top_p` | 直接透传 |
| `top_k` | ❌ | 丢弃，OpenAI 无对应参数 |
| `stop_sequences` | `stop` | 数组 → 数组/string |
| `tools` | `tools` | 需格式转换（见1.4） |
| `tool_choice` | `tool_choice` | 需格式转换（见1.4） |
| `metadata.user_id` | `user` | `metadata?.user_id` → `user` |
| `thinking` | ❌ | Anthropic 专有，丢弃（或存到 extra_headers） |

#### 1.2 content 数组展平

```python
def _flatten_content(content):
    """Anthropic content block 数组 → 纯文本"""
    if isinstance(content, str):
        return content
    texts = []
    for block in content:
        if block.get("type") == "text":
            texts.append(block["text"])
        elif block.get("type") == "image":
            # 图片暂不支持，标注占位
            texts.append("[image]")
        elif block.get("type") == "tool_use":
            texts.append(f"[tool_use: {block.get('name')}]")
        elif block.get("type") == "tool_result":
            texts.append(block.get("content", "[tool_result]"))
    return "\n".join(texts) if texts else "[non-text content]"
```

#### 1.3 system 字段处理

```python
def _convert_system(anthropic_body):
    """Anthropic system → OpenAI system message"""
    system = anthropic_body.get("system")
    if not system:
        return None
    if isinstance(system, str):
        return {"role": "system", "content": system}
    # Anthropic system 也可以是 content block 数组
    if isinstance(system, list):
        text = _flatten_content(system)
        return {"role": "system", "content": text}
    return None
```

#### 1.4 tools 字段转换

```
Anthropic tools:                          OpenAI tools:
[                                         [
  {                                         {
    "name": "get_weather",                    "type": "function",
    "description": "...",                     "function": {
    "input_schema": {...}                       "name": "get_weather",
  }                                             "description": "...",
]                                               "parameters": {...}
                                              }
                                            }
                                          ]

Anthropic tool_choice:                    OpenAI tool_choice:
{ "type": "tool", "name": "x" }  ───→    {"type": "function", "function": {"name": "x"}}
{ "type": "any" }                ───→    "required"
{ "type": "auto" }               ───→    "auto"
```

---

## 二、响应转换

### 2.1 非流式响应

```
Anthropic Messages Response              OpenAI Chat Completions Response
┌────────────────────────────┐           ┌──────────────────────────────────┐
│ id                         │──────→    │ id                               │
│ model                      │──────→    │ model                            │
│ type: "message"            │   丢弃    │ object: "chat.completion"        │
│ role: "assistant"          │   丢弃    │ created                          │
│ content[{type,text}]       │──→        │ choices[{index, message,         │
│   → text                   │提取文本   │   finish_reason}]                │
│ stop_reason                │──→        │   message: {role, content}       │
│ stop_sequence              │──→        │ usage: {prompt,completion,total} │
│ usage: {                   │──→        └──────────────────────────────────┘
│   input_tokens,            │ 重命名
│   output_tokens,           │
│   cache_creation/read      │
│ }                          │
└────────────────────────────┘
```

### 2.2 流式 SSE 响应转换（核心难点）

```
Anthropic SSE 事件                       OpenAI Chat SSE 事件
═══════════════                          ═══════════════════

event: message_start                    data: {"id":"...","object":"chat.completion.chunk",
data: {"message":{                      "choices":[{"delta":{"role":"assistant"},"index":0}]}
  "id":"msg_xxx",                       ── role delta 一次性发送
  "model":"claude-xxx",
  "usage":{"input_tokens":...}
}}

event: content_block_start              (无直接对应，记录 block 信息)
data: {"index":0,"content_block":{
  "type":"text","text":""
}}

event: content_block_delta              data: {"choices":[{"delta":{"content":"Hello"},
data: {"delta":{                        "index":0}]}
  "type":"text_delta",
  "text":"Hello"
}}

event: content_block_delta              data: {"choices":[{"delta":{"content":" world"},
data: {"delta":{                        "index":0}]}
  "type":"text_delta",
  "text":" world"
}}

event: content_block_stop               (无直接对应)
data: {"index":0}

event: message_delta                    合并输出 usage
data: {"delta":{
  "stop_reason":"end_turn",
  "stop_sequence":null
}}

event: message_stop                     data: {"choices":[{"finish_reason":"stop"}],
data: {}                                 "usage":{...}}
                                        data: [DONE]
```

**转换算法（生成器模式）：**

```python
async def anthropic_sse_to_chat_sse(anthropic_stream):
    """将 Anthropic SSE 流转换为 OpenAI Chat Completions SSE 流"""
    chat_id = f"chatcmpl-{uuid4().hex[:24]}"
    created = int(time.time())
    model = ""
    content = ""
    finish_reason = "stop"
    input_tokens = 0
    output_tokens = 0
    role_sent = False

    async for raw_line in anthropic_stream:
        line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data: "):
            continue

        try:
            event = json.loads(line[6:])
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")

        if event_type == "message_start":
            msg = event.get("message", {})
            model = msg.get("model", "")
            input_tokens = msg.get("usage", {}).get("input_tokens", 0)

            # 发送 role delta
            chunk = _make_chunk(chat_id, model, created, role="assistant")
            yield _to_sse(chunk)
            role_sent = True

        elif event_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                content += text
                chunk = _make_chunk(chat_id, model, created, content=text)
                yield _to_sse(chunk)

        elif event_type == "message_delta":
            delta = event.get("delta", {})
            stop_reason = delta.get("stop_reason", "")
            output_tokens = event.get("usage", {}).get("output_tokens", 0)
            finish_reason = _map_stop_reason(stop_reason)

        elif event_type == "message_stop":
            # 发送最终 chunk（含 finish_reason 和 usage）
            total = input_tokens + output_tokens
            chunk = _make_chunk(
                chat_id, model, created,
                finish_reason=finish_reason,
                usage={"prompt_tokens": input_tokens,
                       "completion_tokens": output_tokens,
                       "total_tokens": total}
            )
            yield _to_sse(chunk)
            yield "data: [DONE]\n\n"
```

### 2.3 stop_reason 映射

```python
def _map_stop_reason(anthropic_reason: str) -> str:
    mapping = {
        "end_turn":     "stop",
        "max_tokens":   "length",
        "stop_sequence":"stop",
        "tool_use":     "tool_calls",
    }
    return mapping.get(anthropic_reason, "stop")
```

---

## 三、接入架构

### 3.1 触发条件

在 `proxy.py` 的 `forward_request` 中：当 `api_path == "chat/completions"` 且选定 Key 的 provider 需要 Anthropic 格式转换时启用。

两种方式判断是否需要转换：
1. **配置驱动**：Key 新增 `adapter` 字段，值为 `"anthropic"` 时启用
2. **自动检测**：检测 `api_path == "messages"`（Anthropic 客户端发出）且 key 是 OpenAI 兼容的

推荐 **方式 1 + 2 结合**：
- 客户端调用 `/v1/messages` → 自动启用 Anthropic→Chat 转换
- Key 的 `adapter=anthropic` → 即使客户端调 `/v1/chat/completions`，也按 Anthropic 格式发给上游

### 3.2 数据流

```
客户端
  │ POST /v1/messages (Anthropic 格式)
  ▼
akm server.py
  │ 新增路由: @app.post("/v1/messages")
  │ 调用 _handle_ai_request(body, "messages")
  ▼
akm proxy.py
  │ forward_request(body, client, api_path="messages")
  │ pick_key 选择 Key
  │ 检测到 api_path="messages" → 启用 Anthropic→Chat 转换
  │ 将 body 转为 Chat Completions 格式
  │ 调用上游 /v1/chat/completions
  ▼
上游 AI API
  │ 返回 Chat Completions SSE
  ▼
akm proxy.py
  │ 检测到需要转换 → 启动 Anthropic SSE 生成器
  │ 逐行转换 Chat SSE → Messages SSE
  ▼
akm server.py
  │ 流式返回 Anthropic 格式 SSE
  ▼
客户端
```

### 3.3 反向转换（Chat → Anthropic Messages）

如果需要 Anthropic 客户端调用 OpenAI 供应商，流程相反：

```
Anthropic 客户端 → /v1/messages → akm → Chat 格式 → OpenAI → Chat SSE → Messages SSE → 客户端
```

这是上面设计的「正向转换」。反向则用于：
```
OpenAI 客户端 → /v1/chat/completions → akm → Messages 格式 → Anthropic → Messages SSE → Chat SSE → 客户端
```

双向都需要实现，但核心是一套请求/响应的互转映射。

---

## 四、实现边界

### 必须实现
- basic 文本对话（非流式 + 流式）
- system prompt 转换
- content 数组展平
- token usage 映射
- stop_reason 映射

### 可选实现
- tools/function calling 互转
- 图片 content block 转发
- thinking/reasoning 内容映射
- 错误响应格式转换（Anthropic error → OpenAI error）

### 不实现
- Anthropic 专有参数（top_k, thinking 等）的语义翻译
- multimodal content 完整转换（先标注占位）
