# `data_filter_guard` 插件

数据安全插件：请求脱敏（敏感字段/关键词/正则可逆占位符）、非流式与流式字段级滑动窗口响应高风险内容拦截

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 请求/响应过滤 |
| 默认状态 | 默认关闭 |
| 优先级 | `20` |
| Hook | `on_request`, `on_response` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.data_filter_guard`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 关闭后插件保留已加载状态，但不改写请求体和响应体（启用过滤） |
| `sensitive_fields` | text | `api_key,apikey,api-key,x-api-key,authorization,proxy-authorization,password,pass…` | 递归扫描这些字段名；命中后将整个字段值替换为可逆占位符 <AKM-SEC:tag@seq:hash/>（与关键词/正则相同），响应侧可还原；支持逗号或换行分隔（敏感字段名） |
| `keyword_rules` | text | `""` | 按行或逗号填写关键词；命中后自动替换为可逆占位符 <AKM-SEC:tag@seq:hash/>，可选 `关键词#标签` 或 `关键词#@标签` 格式（关键词替换规则） |
| `regex_rules` | text | `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}#@email (?<!\d)(1[3-9]\d{9})(?!\d)…` | 按行填写正则；命中后自动替换为可逆占位符 <AKM-SEC:tag@seq:hash/>。可选 `正则#@标签`。默认已并入邮箱/手机号，以及原代码敏感分组规则（LLM Key、VCS Token、云厂商密钥、ChatOps、JWT/Bearer、私钥头、数据库连接串、凭据赋值），响应侧可按指纹宽松还原（正则替换规则） |
| `request_text_paths` | text | `messages[].content,input,instructions,system,messages[].tool_calls[].function.ar…` | 仅对这些路径下的字符串做关键词/正则替换；messages[].content 同时覆盖字符串 content 与 content[].text 多模态/Anthropic blocks；默认另含 Chat 续接 messages[].tool_calls[].function.arguments；留空表示处理所有字符（文本处理路径） |
| `process_keys_case_insensitive` | boolean | `True` | 开启后 Authorization 和 authorization 会按同一字段处理（字段名忽略大小写） |
| `enable_response_guard` | boolean | `True` | 对非流式响应正文执行基础风险扫描，命中高风险模式时拦截返回（启用响应安全拦截） |
| `enable_stream_response_guard` | boolean | `False` | 对流式 SSE 输出做字段级滑动窗口安全扫描（对齐换回截流：只扫 content/text/delta 等可见字段，边 yield 边扫）。窗口长度见 stream_guard_cache_chars；block/mask 命中均中断并返回安全载荷（mask 退化为 block）（启用流式响应拦截） |
| `stream_guard_cache_chars` | number | `2048` | 流式安全扫描时每个 content 字段（及纯文本 tail）保留的最近字符数，用于跨 chunk 字段级命中；与占位符换回截流同思路，不做整段完整缓冲（流式扫描缓存长度） |
| `response_guard_mode` | string | `block` | 可选值：block（整条拦截）/ mask（替换命中片段）/ warn（仅告警不改写）。非流式 mask 替换正文；流式 mask 因已边下发边扫，统一退化为 block（响应防护模式） |
| `response_block_patterns` | text | `(?i)rm\s+-rf\s+/ (?i)curl\s+[^\n|]+\|\s*(bash|sh) (?i)wget\s+[^\n|]+\|\s*(bash|s…` | 按行填写高风险正则（勿在行尾加逗号）；默认已合并「命令执行风险」与「提示词注入」模板：常见命令执行/脚本投递 + 注入话术，命中后按响应防护模式或单条动作处理（响应拦截正则） |
| `response_rule_actions` | text | `(?i)ignore\s+(all\s+)?previous\s+instructions=>warn (?i)reveal\s+(the\s+)?system…` | 按行填写 regex=>warn|mask|block；可对单条规则覆盖全局响应防护模式。默认已合并「提示词注入」模板动作（注入话术多为 warn，dump secrets 为 block）（响应规则动作） |
| `response_mask_replacement` | string | `[BLOCKED-RISKY-CONTENT]` | 当响应防护模式为 mask 时，用该文本替换命中的危险片段（响应命中替换文本） |
| `response_block_message` | text | `检测到疑似高风险指令或恶意载荷，已由数据安全插件拦截。` | 命中响应安全规则后，返回给客户端的提示文案（响应拦截提示） |
