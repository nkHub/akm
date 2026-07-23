# 项目本地插件说明

本目录存放 AKM / ccs 的**项目本地插件**（非内置核心）。它们默认大多关闭，在管理台「插件」页启用并配置后生效。

数据与状态默认只在**当前进程内存**中，服务重启会清空（除非插件另行说明）。跨 hook 状态写在请求级 `RequestContext.bag`（约定键 `{plugin}.{field}`），业务 request 与 bag 分离；multipart 等传输字段若仍用 `__akm_*` 前缀，转发层会在发往上游前剥离。
## 一览

| 插件 | 类别 | 默认 | 一句话 |
|------|------|:----:|--------|
| `protocol_converter` | converter | — | Responses / Messages / Chat 三格式互转 |
| `prompt_booster` | filter | — | 给请求追加固定 system prompt |
| `prompt_profiles` | filter | 关 | 按模型/接口/客户端叠加多套提示词 |
| `data_filter_guard` | filter | 关 | 请求脱敏 + 响应敏感内容拦截 |
| `tool_policy_guard` | filter | 关 | 限制工具名与危险工具参数 |
| `rate_limit_guard` | filter | 关 | 本地 RPM / RPH / 并发限流 |
| `cache_proxy` | filter | 关 | 相同非流式请求的进程内响应缓存 |
| `usage_quota_guard` | matcher | 关 | 按 Key/模型限制窗口内请求次数与 Token |
| `fallback_router` | handler | 关 | 失败后切到备用模型并重选 Key |
| `response_schema_guard` | post | 关 | 校验调用方声明的 JSON Schema |
| `webhook_notifier` | post | 关 | 失败/安全/慢请求异步 Webhook |
| `provider_health_probe` | app | 关 | Key 连通性探测与状态快照 API |
| `markdown_kb` | app | 关 | 本地 Markdown 知识库（上传/状态） |

内置核心（通常在 `akm/plugins/`，不在本目录）：`model_matcher`、`error_handler` 等，负责选 Key、重试，不在此表展开。

---

## 按职责说明

### 协议与提示词

- **`protocol_converter`**  
  在客户端协议与上游协议不一致时做格式转换（Chat / Responses / Anthropic Messages）。多数转发链路依赖它。

- **`prompt_booster`**  
  在请求出站前注入一段固定附加提示词。规则简单时可用；与 `prompt_profiles` 同时开会叠加重注，建议只留一套。

- **`prompt_profiles`**  
  用 JSON 配置多条 profile：按模型 glob、接口路径、客户端 UA 等过滤，再按顺序叠加 `prompt`。适合多客户端、多模型不同系统提示。

### 安全与策略

- **`data_filter_guard`**  
  请求侧敏感字段名/关键词/正则（均用可逆 `<AKM-SEC:.../>`，已移除固定 `[REDACTED]`）；默认 `regex_rules` 已并入原代码敏感分组（LLM Key、VCS、云厂商、ChatOps、JWT/Bearer、私钥、连接串、凭据赋值）及邮箱/手机号。默认 `request_text_paths` 覆盖对话正文、system/input/instructions 与 Chat `tool_calls` 参数。响应侧非流式与有界流式安全扫描，可 mask 或 block。可逆映射进 bag `data_filter_guard.reverse_map`，流式由 `request_context` 回传，在 yield 前增量还原：SSE 字段级 content 截流（短片段以 `<` 开头/结尾才缓冲）+ 纯文本半截前缀缓冲。插件总开关仍默认关闭。
- **`tool_policy_guard`**  
  约束 tools 声明与续接里的工具调用参数（白名单/黑名单/危险正则）。保护的是进入代理的工具协议，**不能**替代客户端本机工具沙箱。

- **`rate_limit_guard`**  
  进程内固定窗口限流：每分钟请求数、每小时请求数、最大并发。维度可选全局 / 模型 / 用户（请求体 `user`）。超限时 `ctx.set_block` 返回 **HTTP 429**，不消耗上游；并发槽记在 bag `rate_limit_guard.slot`，`on_response` 释放。  
  与 `usage_quota_guard` 不同：后者偏「配额用尽后跳过 Key」，本插件偏「入口 QPS/并发闸门」。
- **`response_schema_guard`**  
  仅当请求声明了 `json_object` / `json_schema` 时，校验非流式响应是否符合常见 Schema 子集；可告警或返回错误。

### 稳定性与容量

- **`usage_quota_guard`**  
  按 Key、模型在固定时间窗口内限制请求次数和**已观测** Token。超额时 `ctx.set_skip_key` 换其他 Key。Token 仅来自响应里可解析的 usage，**不是**供应商账单。

- **`fallback_router`**  
  配置 `source_model => fallback_model` 与触发状态码/网络错误；命中后改模型并重新选 Key；尝试历史在 bag `fallback_router.history` 防循环。

- **`cache_proxy`**  
  对**非流式、默认不含 tools** 的请求，用规范化请求体做 SHA256 键，进程内缓存成功响应。TTL / 条数 / 单条大小可配。  
  命中时 `ctx.set_block` 短路返回缓存正文（元信息为本地 cache_hit，不再打上游）；未命中时 bag 记 `cache_proxy.cache_key` / `eligible` 供 `on_response` 写入。流式与工具会话默认跳过，避免语义错乱。  
  **注意**：仅适合幂等、确定性较高的补全场景；不要对带副作用或强随机输出的请求开缓存。
### 观测与运维

- **`webhook_notifier`**  
  请求结束后异步发 Webhook（generic / 飞书 / 企微 / Slack 等）：上游失败、安全拦截、慢请求等，带冷却去重，避免拖慢转发。

- **`provider_health_probe`**  
  手动或定时探测 Key 连通性，结果脱敏（无 API Key、无上游 URL/正文）。  
  默认前缀 `/api/provider-health`：`GET /status`、`POST /probe`。

- **`markdown_kb`**  
  本地 Markdown 知识库骨架：上传 `.md`、查看状态等，供检索/注入类能力扩展。

---

## 近期新增插件（速查）

| 插件 | 何时用 | 何时不要用 |
|------|--------|------------|
| `rate_limit_guard` | 防止单进程被打爆、按用户/模型卡 RPM | 需要跨实例共享限流（本插件是进程本地） |
| `cache_proxy` | 重复相同 prompt、省延迟和费用 | 流式、工具调用、强随机/强时效内容 |

> 费用估算已并入核心：设置页「费用统计」开关 + 模型单价表；首页 `/api/stats` 在开启后展示总费用与每日费用。

---

## 启用建议

1. 先在管理台**单独启用**，用小流量验证日志与行为。  
2. 安全类（`data_filter_guard`、`tool_policy_guard`）建议先「告警/观察」再收紧为阻断。  
3. `rate_limit_guard` 与 `usage_quota_guard` 可同时开：一个管入口速率，一个管 Key/模型配额。  
4. 修改配置后以管理台保存为准；多数插件热读配置，无需改代码。启用/禁用/安装/删除默认热生效（`on_load`/`on_unload`），改插件源码仍需重启服务。

### 请求级 bag 约定（速查）

| bag 键 | 插件 | 用途 |
|--------|------|------|
| `data_filter_guard.reverse_map` | data_filter_guard | 可逆占位符 → 原文 |
| `cache_proxy.cache_key` / `cache_proxy.eligible` | cache_proxy | 缓存键 / 是否可写 |
| `rate_limit_guard.slot` | rate_limit_guard | 并发槽位 |
| `fallback_router.history` | fallback_router | 本请求已尝试模型 |

Hook 签名均为 `on_*(ctx: RequestContext)`，控制流用 `ctx.set_block` / `ctx.set_skip_key`。更完整说明见 `docs/design/plugin-system.md` §8。
