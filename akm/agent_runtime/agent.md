# Agent Loop

`POST /v1/agent` 提供多轮 LLM 工具调用编排能力。请求传入对话历史和工具定义，Agent Loop 内部循环调用 LLM → 解析 `tool_calls` → 执行工具 → 回传结果，直到 LLM 返回最终文本回复或达到最大轮次。

每次 LLM 调用通过 `proxy.forward_request` 透传，自动复用 Key 选择、协议转换、重试等所有现有能力。

Agent 实现集中在 `akm/agent_runtime/`：`router.py` 提供端点、`loop.py` 负责多轮编排、`tools.py` 提供内置只读调试工具，`service.py` 负责服务启动时的初始化。

服务启动后会自动为每次 Agent 请求注入以下只读 AKM 调试工具。它们仅作用于 `/v1/agent` 和 `/agent`，不会进入常规转发端点（如 `/v1/chat/completions`、`/v1/messages`、`/v1/responses`）。调用方不应使用相同名称，以免工具定义与内置处理器不匹配：

| 工具 | 用途 |
|------|------|
| `akm_get_status` | 查询健康监护、审计队列和插件状态 |
| `akm_list_keys` | 查询 Key 的别名、供应商、状态和模型列表，不返回 API Key 或连接地址 |
| `akm_list_logs` | 查询近期审计摘要，可按状态、天数和 Key 别名筛选；不返回请求体、响应体或请求头 |
| `akm_get_time` | 获取服务器当前时间，返回本地 ISO 时间、UTC 时间、UNIX 时间戳与时区 |
| `tavily_search` | 通过 Tavily 实时联网搜索，返回含标题、链接和摘要的搜索结果；需先在 config.json 中配置 `tavily_api_key` |
| `akm_search_kb` | 检索 `markdown_kb` 插件索引的 Markdown 知识库，返回命中文档片段（标题/文件名/分数/内容）；需本机已启用并索引 markdown_kb 插件 |
| `akm_generate_image` | 调用 AKM 配置的图片生成模型生成图片，返回图片资源（url + 本地路径 + `/agent-uploads/...` HTTP 地址）；需配置 `image_supported_models` 对应的可用 API Key |
| `akm_edit_image` | 编辑图片（如重绘局部、扩展内容），返回编辑后的图片资源（url + 本地路径 + `/agent-uploads/...` HTTP 地址）；图片可通过本地路径或 base64 数据传入 |
| `akm_context_status` | 查询当前对话上下文的 token 占用（估算已用 token、上限与剩余空间），用于判断是否需要压缩早期历史 |
| `akm_compact_context` | 主动压缩当前对话的早期历史为一段摘要，保留最近约 `agent_keep_recent_messages` 条消息（工具调用与配对消息自动完整保留） |

## 请求格式

```json
{
  "model": "gpt-4o",
  "messages": [{"role": "user", "content": "帮我查一下今天的天气"}],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "查询指定城市的天气",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "城市名称"}
          },
          "required": ["city"]
        }
      }
    }
  ],
  "instructions": "你是 AKM 内置助手，请用中文回复",
  "api_path": "chat/completions",
  "max_turns": 20
}
```

## 参数说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `messages` | list | 是 | 对话历史（Chat 格式的 messages 数组） |
| `model` | string | 否 | 指定模型，为空时自动选择第一个可用 Key 的模型 |
| `tools` | list | 否 | 工具定义列表（OpenAI function calling 格式）。传入时**只注入本列表声明的工具**（内置工具如 `tavily_search`、`akm_search_kb` 不再自动注入，避免模型未经声明自主调用）；显式传空数组 `[]` 表示**不注入任何工具**；不传时注入除 `tavily_search` / `akm_generate_image` / `akm_edit_image` 外的全部内置工具（联网搜索与图片生成涉及外部服务调用，需在 tools 中显式声明才能启用） |
| `instructions` | string | 否 | 系统级指令，注入到 messages 首条 system 消息；未传时使用 config.json 的 `agent_default_instructions`（默认要求数学公式以 KaTeX 语法返回） |
| `api_path` | string | 否 | LLM 调用协议格式（默认 `chat/completions`，也支持 `responses` / `messages`） |
| `max_turns` | int | 否 | 最大迭代轮次（默认 20），防止工具调用无限循环 |
| `stream` | bool | 否 | 是否 SSE 流式返回（默认 `false`）；思考与正文均实时以 `reasoning_delta` / `model_delta` 推送，工具调用事件按上游输出顺序穿插，`final` 收尾（详见「SSE 流式事件」） |

## 上下文压缩

长对话可能导致上下文超出模型窗口。Agent Loop 提供两层保障，均由 config.json 控制：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `agent_max_context_tokens` | `30000` | 上下文 token 估算上限，超过后自动压缩早期历史；`0` 关闭自动压缩 |
| `agent_keep_recent_messages` | `10` | 压缩时保留的最近消息条数，工具调用与其配对的 `tool_calls` 消息整组完整保留 |
| `agent_context_warning_ratio` | `0.8` | 上下文占用超过上限该比例时，SSE 流式下发 `context_warning` 事件；`0` 关闭警告 |

1. **自动压缩兜底**：每轮开始前估算上下文 token（CJK 字符按 1 token/字符，其余按 4 字符≈1 token，图片块固定估算），超过 `agent_max_context_tokens` 时把早期历史交给 LLM 总结为摘要并替换（保留最近 `agent_keep_recent_messages` 条消息与工具调用配对组），保证上下文不爆掉；摘要生成失败时降级为直接丢弃早期历史。
2. **AI 主动压缩**：模型可调用 `akm_context_status` 查询当前 token 占用、`akm_compact_context` 主动压缩早期历史。`akm_compact_context` 优先采用摘要替换，不丢失关键信息。`agent_context_warning_ratio` 触发的 `context_warning` SSE 事件即用于提示客户端 / 模型接近上限。

压缩只作用于早期历史，最近消息与所有工具调用配对始终完整保留；`final` / `error` / `context_warning` 事件的 `compacted` 字段表示本次运行累计压缩次数。

## 文件上传

`/v1/agent` 支持 `multipart/form-data` 上传文件。`messages` 改为 JSON 字符串表单字段，`tools` 等其余字段同纯 JSON 方式；`files` 字段可携带多个文件。上传的文件会被读取并作为独立的 user 消息追加到对话末尾：图片（`image/*`）转成 base64 的 `image_url` 内容块，其他文件按 UTF-8 读取为文本内容，无法解码的二进制文件会返回 400。纯 JSON 请求方式保持不变。

上传的图片还会同时落盘到 `agent_upload_dir` 配置的目录（默认 `~/.akm/cache`，可通过 `~/.akm/config.json` 修改，支持 `~` 展开；文件名为随机 UUID），并在追加的 user 消息文本中给出绝对路径提示。模型可据此调用 `akm_edit_image` 传入 `image_path` 编辑该图片。该目录不会自动清理，请根据运行环境定期清理。

```bash
curl -X POST http://127.0.0.1:8788/v1/agent \
  -F 'messages=[{"role":"user","content":"请分析这个文件"}]' \
  -F 'model=gpt-4o' \
  -F 'files=@./report.txt'
```

流式 `final` 事件中的 `final_message` 会保留上游 Chat 响应的 `reasoning_content`，以便客户端展示完成后的推理内容。

## 联网搜索

内置的 `tavily_search` 工具通过 Tavily 官方远程 MCP 端点提供实时联网搜索。使用前需要在 `~/.akm/config.json` 中配置 API Key：

```json
{
  "tavily_api_key": "tvly-xxxxxxxx"
}
```

配置后 Agent 即可调用 `tavily_search`，参数如下：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 搜索关键词 |
| `max_results` | int | 否 | 返回结果数量（1-20，默认 5） |
| `search_depth` | string | 否 | 搜索深度 `basic` 或 `advanced`（默认 `basic`） |

请求会经 AKM 的按路由隔离 HTTP 连接池发往 `https://mcp.tavily.com/mcp/`（Key 通过查询参数注入），遵循出站代理设置。未配置 `tavily_api_key` 时工具调用会返回明确错误提示。

## 知识库查询（markdown-kb）

内置的 `akm_search_kb` 工具通过本机 `POST /api/markdown-kb/query` 接口检索 `markdown_kb` 插件索引的 Markdown 知识库，让 Agent 可以主动查询项目文档。工具经本机 HTTP 调用，不依赖插件内部实现。参数如下：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `question` | string | 是 | 检索关键词 / 问题描述 |
| `top_k` | int | 否 | 返回的检索结果数量（1-20，默认 5） |
| `embedding_model` | string | 否 | 指定向量模型，默认取插件配置 |
| `reranker_model` | string | 否 | 指定重排模型，默认取插件配置 |

返回结果为命中文档片段列表（每项含标题、文件名、相关度分数与截断后的内容片段），避免全文撑爆上下文。知识库未初始化或未命中时返回明确提示。该工具的 HTTP 端点同时提供 MCP 访问方式（见 `plugins/markdown_kb/README.md`）。

## 图片生成

内置的 `akm_generate_image` 工具复用 `/v1/images/generations` 的转发链路，让 Agent 可以直接生成图片。返回结果只含图片 URL（或 `b64_json_hint` 提示），避免体积巨大的 base64 数据占用模型上下文。参数如下：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `prompt` | string | 是 | 图片描述提示词 |
| `model` | string | 否 | 图片生成模型，默认取 `image_supported_models` 配置首项 |
| `size` | string | 否 | 图片尺寸，如 `1024x1024` |
| `quality` | string | 否 | 生成质量，如 `standard` 或 `hd` |
| `n` | int | 否 | 生成张数（默认 1） |

请求经连接池调用 `forward_request`（`api_path=images/generations`），自动复用 Key 选择、故障切换与图片专用超时（`image_request_timeout_sec`，默认 300 秒）。调用失败时会返回明确错误文本；上游只返回 `b64_json` 时会给出数据长度提示。

生成成功后，每张图片还会下载保存到 `agent_upload_dir`（默认 `~/.akm/cache`），并在结果项中附带两个资源字段：

- `local_path`：图片在本机的绝对路径，可供 `akm_edit_image` 等按路径读取的工具使用；
- `http_url`：`http://127.0.0.1:{port}/agent-uploads/{filename}` 形式的访问地址，可直接用于前端展示或下载。

若下载或保存失败，结果项会附带 `save_error` 说明原因，且不影响 `url` 等主结果字段。

### 图片编辑

内置的 `akm_edit_image` 工具复用 `/v1/images/edits` 的 multipart 纯透传链路。图片有两种来源：`image_path`（服务器可访问的本地绝对路径），或 `image_base64`（图片 base64 数据，可直接使用对话中图片的 `data:image/...;base64,` 前缀 data URL，适用于本地无文件的云端场景）。工具按与 `/v1/images/edits` 一致的 multipart 结构组装请求，`image` 与可选的 `mask` 作为文件字段上传。参数如下：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `image_path` | string | 否* | 服务器本地图片文件的绝对路径，与 `image_base64` 二选一 |
| `image_base64` | string | 否* | 图片 base64 数据（可带 `data:image/...;base64,` 前缀，或纯 base64），与 `image_path` 二选一，同时提供时优先 |
| `prompt` | string | 是 | 编辑指令，描述期望的修改效果 |
| `model` | string | 否 | 图片编辑模型，默认取 `image_supported_models` 配置首项 |
| `mask_path` | string | 否 | 本地蒙版图片路径，用于限定重绘区域，与 `mask_base64` 二选一 |
| `mask_base64` | string | 否 | 蒙版图片 base64 数据，与 `mask_path` 二选一，同时提供时优先 |
| `size` | string | 否 | 输出图片尺寸，如 `1024x1024` |
| `quality` | string | 否 | 生成质量，如 `standard` 或 `hd` |
| `output_format` | string | 否 | 输出格式，如 `png` 或 `jpeg` |
| `n` | int | 否 | 生成张数（默认 1） |

*`image_path` 与 `image_base64` 至少提供其一，否则返回错误。

图片文件不存在、base64 解码失败或两种来源都未提供时，会返回明确错误提示；其余失败（上游错误、无法解析响应等）与 `akm_generate_image` 行为一致。编辑结果与生成结果一样，会附带 `local_path`、`http_url`（`/agent-uploads/...`）资源字段，保存失败时附 `save_error`。

### 上传目录的 HTTP 访问

服务端提供 `GET /agent-uploads/{filename}` 端点，按文件名提供 `agent_upload_dir` 目录下文件的访问（文件名仅接受单段安全名称，防止路径穿越）。生成、编辑工具返回的 `http_url` 即指向该端点，前端或其他客户端可通过 `http://127.0.0.1:{port}/agent-uploads/{filename}` 直接获取图片。

## 响应格式

```json
{
  "ok": true,
  "final_message": {
    "role": "assistant",
    "content": "今天北京的天气是晴天，气温 25°C。"
  },
  "messages": [...],
  "turns": 2,
  "compacted": 1,
  "error": "",
  "usage": {
    "prompt_tokens": 1200,
    "completion_tokens": 350,
    "total_tokens": 1550
  }
}
```

## SSE 流式事件（`stream: true`）

事件顺序约定：**思考（`reasoning_delta`）与正文（`model_delta`）按模型输出顺序实时流式下发**，工具轮正文同样实时流出，工具事件（`turn_start`/`tool_call`/`tool_result`）随后出现，最终以 `final` 收尾。

| 事件 | 说明 |
|------|------|
| `reasoning_delta` | LLM 思考（推理）过程片段，`data.turn` 为当前轮次，`data.content` 为增量内容；实时下发，先于同段正文 |
| `model_delta` | 可见正文片段，`data.turn` 为当前轮次，`data.content` 为增量内容；实时下发（含工具轮过程性正文与最终主体内容） |
| `context_warning` | 上下文占用接近上限（估算 token 超过 `agent_max_context_tokens` × `agent_context_warning_ratio`）时下发，`data` 含 `estimated_tokens` / `max_tokens` / `remaining_tokens` / `ratio` / `compacted`，供客户端提前感知并提示 AI 调用 `akm_compact_context` 主动压缩 |
| `turn_start` | 新一轮开始，`data.turn` 为当前轮次 |
| `tool_call` | LLM 请求调用工具，`data.name` / `data.arguments` |
| `tool_result` | 工具执行结果，`data.name` / `data.result` |
| `final` | Agent 完成，含 `data.final_message` / `data.turns` / `data.usage` / `data.compacted` |
| `error` | 错误终止，含 `data.error` / `data.turns` / `data.usage` / `data.compacted` |

```json
// SSE 示例（工具轮：思考与正文实时下发，工具事件随后，最终主体实时流式）
data: {"event":"reasoning_delta","data":{"turn":1,"content":"用户想查天气"}}

data: {"event":"model_delta","data":{"turn":1,"content":"我来查询"}}

data: {"event":"turn_start","data":{"turn":1}}

data: {"event":"tool_call","data":{"name":"get_weather","arguments":{"city":"beijing"}}}

data: {"event":"tool_result","data":{"name":"get_weather","result":"{\"city\":\"beijing\",\"temp\":25}"}}

data: {"event":"model_delta","data":{"turn":2,"content":"北京今天..."}}

data: {"event":"final","data":{"final_message":{"role":"assistant","content":"北京今天..."},"turns":2,"usage":{...}}}
```
