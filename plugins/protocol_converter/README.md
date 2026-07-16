# protocol_converter

协议格式转换插件，负责 Responses / Messages / Chat 三种格式的双向转换，按 `api_path` 自动选择适配方向。

## 转换方向

| 请求格式 | 供应商不支持时 | 请求转换 | 响应 SSE 转换 |
|----------|---------------|----------|---------------|
| `/v1/responses` | → `chat/completions` | Responses → Chat | Chat SSE → Responses SSE |
| `/v1/messages` | → `chat/completions` | Messages → Chat | Chat SSE → Messages SSE |
| `/v1/chat/completions` | → `messages` | Chat → Messages | Messages SSE → Chat SSE |

## Messages → Chat 发送方向

- `system` → 首条 `system` 消息
- `stop_sequences` → `stop`
- `metadata` 默认透传；`metadata.user_id` 会在未显式传 `user` 时映射到 Chat `user`
- `max_tokens` 默认映射到 `max_tokens`；当当前选中的供应商为 `openai` 时，会额外补一份 `max_completion_tokens`
- `reasoning.effort` / `reasoning_effort` 仅在当前选中的供应商为 `openai`、且调用方未显式关闭 `thinking` 时，保守映射为 `reasoning_effort`
- `tool_result.content` 为纯文本时会展平为 Chat `tool` 文本；若包含非文本结构，则保留整份原始内容并序列化为 JSON 字符串
- `thinking` 不直接透传到 Chat-only 上游，避免向不兼容供应商注入 Anthropic 专有字段

以上和供应商相关的兼容开关已下沉并收口到 `Agent` 能力层：当前内置 `openai` 会开启 `max_completion_tokens` / `reasoning_effort` 补齐，其他供应商默认走保守策略，避免在协议层再维护独立 provider profile。

## Responses → Chat 发送方向

- `instructions` → `system` 角色消息
- `input` 中的 `function_call` / `function_call_output` → `tool` 角色消息
- 连续 `function_call` 合并为单条 `assistant` 消息（含多个 `tool_calls`）
- `function_call` 上的 `reasoning_content` 回写到合并后的 `assistant` 消息
- `function_call_output` 的结构化 `output` 会序列化为字符串，保证 Chat `role=tool` 消息合法
- `previous_response_id` 会通过本地内存会话缓存恢复上一轮 Chat 历史（含 `reasoning_content` / `tool_calls`），不再把该字段直接转发给 Chat-only 上游
- 兼容现代 continuation：`role=tool` / `tool_call_id` / typed output content 会统一转成 Chat `tool` 消息
- 消息重排序：`system` 移到 `assistant(tool_calls)` 之前
- `_clean_schema()` 递归移除工具 schema 与 structured output schema 中的 `additionalProperties` / `strict` 字段
- `namespace` 类型工具递归展开子工具（支持 MCP）
- `tool` 结尾时追加空 `user` 消息，触发 DeepSeek 继续生成
- `thinking` / `reasoning_effort` 的默认补齐也已收口到 `Agent` 能力层：当前仅 `deepseek` 会在未显式传入时补 `thinking={type:enabled}` 与 `reasoning_effort="high"`

## Chat SSE → Responses SSE 接收方向

| Chat SSE delta | Responses SSE 事件 |
|----------------|-------------------|
| `delta.reasoning_content` | `response.output_item.added`（type=reasoning）+ `response.reasoning_summary_text.delta` |
| `delta.content` | `response.output_text.delta` |
| `delta.tool_calls`（首 chunk） | `response.output_item.added`（type=function_call）+ `response.output_tool_call.begin` |
| `delta.tool_calls`（后续） | `response.function_call_arguments.delta` + `response.output_tool_call.delta` |
| `finish_reason: tool_calls` | `response.function_call_arguments.done` + `response.output_tool_call.end` + `response.output_item.done` |
| 流结束 | `response.completed` + `response.done` |

推理内容通过独立的 `reasoning` 类型输出项流式推送，Codex 在「思考」面板中折叠展示，与正文分离。`protocol_converter` 会在内存中保留最近一批 Responses 会话快照（TTL 24 小时、最多 256 条），用于 `previous_response_id` 续接时恢复 DeepSeek thinking/tool-call 所需的 `reasoning_content` 和工具调用历史；该缓存不持久化到磁盘，服务重启后自动清空。
