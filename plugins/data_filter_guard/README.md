# `data_filter_guard` 插件

数据安全插件：请求脱敏（敏感字段/关键词/正则可逆占位符）、非流式与流式字段级滑动窗口响应高风险内容拦截；内置脱敏/拦截运行记录，自 v0.1.4 起通过宿主「首页插件卡片」通用插槽（`dashboard_card()` 协议）在管理台首页展示，v0.1.5 起卡片仅保留累计数字、不再展示最近事件明细；v0.1.6 起新增**逐条命中明细日志**（`match_log`，默认 `redacted` 档只写掩码预览，不落明文）。

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
| `match_log` | select | `redacted` | 逐条命中明细日志档位：`off` 不写；`redacted` 记录时间/规则/字段路径/占位符/原文长度与指纹/是否换回，原文只留首尾各 2 字符的掩码预览（不落明文）；`full` 额外写入明文原文（等于在本机新增一处明文副本，请自行评估）。落盘 `~/.akm/data_filter_guard/matches.jsonl`（命中明细日志） |
| `match_log_max_bytes` | number | `2097152` | 明细日志单文件上限；超过后滚动为 `matches.jsonl.1`，只保留一份历史，更早内容被覆盖（明细日志单文件上限） |

## 运行记录

- 插件启用后，每次实际发生的**请求脱敏**、**响应占位符还原**与**响应拦截**（warn/mask/block）都会累计进运行统计，并在管理台首页「插件卡片」区展示累计数字。v0.1.4 起该展示走宿主通用 `dashboard_card()` 插槽协议（见 `docs/design/plugin-system.md`），插件实现只声明数据、由宿主统一渲染；无任何记录时卡片仍展示（指标为 0），方便首页首见可见。卡片不展示最近事件明细（无跳转入口、看不到被脱敏的具体内容，展示单行事件意义有限），但底层仍累积最近 20 条事件，留作日后详情页 / 审计扩展。
- 聚合统计落盘在 `~/.akm/data_filter_guard/stats.json`，跨进程重启累计值保留；只记录聚合数字与占位符 tag（如命中字段名/规则标签），**不保存敏感明文或占位符原文**。
- 统计随请求自动记录，无独立配置开关；插件未启用/未加载时不产生记录，首页插件卡片区由宿主决定是否整体展示。

## 命中明细日志（v0.1.6 起）

聚合统计只回答「命中过多少次、命中哪些 tag」，定位问题时常需要「这一次具体命中了哪个字段、替换成了什么、响应有没有换回来」。为此新增逐条明细日志，落盘 `~/.akm/data_filter_guard/matches.jsonl`（JSONL，一行一条；文件权限 `0600`、目录 `0700`）。

| 档位 | 行为 |
|------|------|
| `off` | 不写明细，仅保留上面的聚合统计 |
| `redacted`（默认） | 记录时间、事件、规则类型、tag、字段路径、占位符、序号、原文长度、原文 SHA-256 前 8 位，以及原文的掩码预览（首尾各 2 字符，如 `sk…yz`）。**不写明文**，但同一取值跨请求可用指纹关联 |
| `full` | 在 `redacted` 基础上额外写入 `original` 明文原文。**这等于在本机新增一处明文密钥副本**（文件虽为 `0600`，但同用户下的任意进程可读），与插件「防止敏感数据外发」的目标相冲突，仅建议临时排障时开启 |

记录字段与事件：

| 字段 | 含义 |
|------|------|
| `ts` | 本地时区 ISO 时间戳（含偏移，如 `2026-09-30T15:12:03+08:00`） |
| `event` | `mask` 请求侧脱敏；`restore` 响应侧成功换回；`restore_miss` 未换回（含原因） |
| `rule` | `field` 敏感字段名 / `keyword` 关键词 / `regex` 正则 |
| `tag` | 命中的字段名或规则标签（与聚合统计的 tag 口径一致） |
| `path` | 命中发生的位置，如 `api_key`、`messages[0].tool_calls[0].function.arguments` |
| `placeholder` | 生成/还原的占位符本体（`<AKM-SEC:tag@seq:hash/>`，不是明文） |
| `seq` | 同一次请求内的占位符序号 |
| `original_len` / `original_sha256_8` | 原文长度与 SHA-256 前 8 位（跨请求关联同一取值） |
| `original_preview` | `redacted` 档的掩码预览 |
| `original` | `full` 档才有的明文原文 |
| `restored` | 仅还原事件：`true` 已换回，`false` 未换回 |
| `reason` | 未换回原因：`placeholder_left_in_response`（响应里仍有占位符）、`map_unavailable:…`（反向映射不可用）、`loose_fingerprint_hits=N`（模型改写占位符后按指纹宽松换回） |

示例（`redacted` 档）：

```json
{"ts":"2026-09-30T15:12:03+08:00","event":"mask","rule":"field","tag":"api_key","path":"api_key","placeholder":"<AKM-SEC:api_key@1:9f3c2a/>","seq":1,"original_len":51,"original_sha256_8":"a1b2c3d4","original_preview":"sk…yz"}
{"ts":"2026-09-30T15:12:04+08:00","event":"restore","path":"chat/completions","placeholder":"<AKM-SEC:api_key@1:9f3c2a/>","tag":"api_key","original_len":51,"original_sha256_8":"a1b2c3d4","original_preview":"sk…yz","restored":true}
```

- 写入是旁路行为：日志失败只提示一次诊断，不影响脱敏与转发。
- 流式响应按 chunk 还原，不逐 chunk 写明细（避免刷爆日志）；流式路径的换回情况仍看聚合统计与诊断日志。
- 单文件超过 `match_log_max_bytes`（默认 2MB）后滚动为 `matches.jsonl.1`，只保留一份历史；按每请求数十条命中的量级，默认约保留最近几百次请求的明细。
- 明细日志由插件自身维护，不参与宿主的日志轮转开关（`text_log_rotation` 只作用于 `~/.akm` 根目录下的 append-only 文本日志）；需要清理时直接删除该文件即可。
