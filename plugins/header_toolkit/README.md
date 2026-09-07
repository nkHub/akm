# `header_toolkit` 插件

客户端请求头变换工具：读取客户端原始请求头快照，按规则做「重命名 / 复制 / 固定值 / 补缺失 / 加前后缀」等变换后写入上游请求头。

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 请求过滤（filter） |
| 默认状态 | 默认关闭 |
| 优先级 | `100` |
| Hook | `on_request` |

## 它能做什么

本地代理最典型的用法是给官方客户端补上游身份 / 会话头。例如 opencode 等官方 SDK 会发送 `x-opencode-session`（同会话请求可路由到同一供应商，利于 token 缓存）；当本地代理需要带一组上游网关要求的自定义头时，可用规则把客户端某个头改名 / 复制为上游头，或直接补固定值 / 前缀。

## 前提：内核已打通客户端头透传

插件读取的是 `RequestContext.client_headers` —— 由内核 `server.py` 在真实客户端入口透传的**完整请求头快照**（键统一转小写、值统一字符串化）。只有经过 `server.py` 公开端点（`/v1/chat/completions`、`/v1/messages`、`/v1/responses`、`/v1/embeddings`、`/v1/rerank`、`/v1/images/*`）的请求才有该快照；agent 内部子请求、flow 引擎等**内部调用方不携带客户端头**，插件在这些路径上自动 no-op，不影响内部链路。

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.header_toolkit`，管理台「插件」页可编辑；修改后热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 关闭后插件保持加载，但不改写任何上游请求头。（启用头变换） |
| `rules_json` | text | `[]` | 变换规则 JSON 数组，按声明顺序执行；非法 JSON 只跳过本次变换。（变换规则 JSON） |

## 规则 schema

每条规则是一个 JSON 对象，公共字段：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `action` | string | ✓ | `rename` / `copy` / `set` / `add_if_missing` / `prefix` / `suffix` |
| `from_header` | string | 视 action | 源**客户端**头名（任意大小写，内核已归一为小写比对）；可逗号分隔多个候选源，按顺序取第一个存在且非空的值（见「候选源」） |
| `to_header` | string | 视 action | 写入的**上游**目标头名；大小写随规则原样写入 |
| `value` | string | set/add_if_missing | 固定值 |
| `prefix` / `suffix` | string | prefix/suffix | 追加的前缀 / 后缀 |
| `match_client` | string | 否 | 客户端 User-Agent 子串过滤（不区分大小写）；为空时不限制 |

### 各 action 语义

| action | 行为 | 源缺失时 | 注意 |
|--------|------|:---:|------|
| `rename` | 把 `from_header` 的值改名写为 `to_header`（覆盖原目标） | no-op | 源键存在但值为空串也 no-op |
| `copy` | 把 `from_header` 的值写到 `to_header` | no-op | 不删除源 |
| `set` | 无条件把 `to_header` 设为 `value` | — | value 为空则跳过 |
| `add_if_missing` | 仅当 `to_header` 尚不在本插件已写入的上游头集合时写 `value` | — | 幂等补缺 |
| `prefix` | 在值前加前缀后写入 `to_header` | no-op | 见下方「叠加语义」 |
| `suffix` | 在值后加后缀后写入 `to_header` | no-op | 同上 |

> 所有 action 的值都不能写出空头：源缺失 / 源值为空串 / `value` 为空时都跳过。头名大小写不敏感比对仅用于**源查找**；`to_header` 写出时保留规则声明的大小写，转发层在 `build_headers` 之后按「小写同名先删后写」合并，因此即使与内置头（`User-Agent` / `Content-Type` / `accept`）仅大小写不同，也会原子替换成一条，不会出现重复头上送。

### 候选源（逗号分隔，顺序优先）

`rename` / `copy` / `prefix` / `suffix` 的 `from_header` 可写多个候选源，用英文逗号分隔（每项自动 trim）：

```json
{ "action": "rename", "from_header": "x-opencode-session, session-id, x-session-id", "to_header": "x-gw-session" }
```

按声明顺序取**第一个在客户端头快照中存在且值非空**的源；前面的候选缺失或为空时自动落到下一个。全部候选都缺失时整体 no-op（不写目标头、不回退固定值）。典型用法：不同客户端（opencode / Codex / 其它）各自的会话头名不同，用一条规则统一映射，谁发就用谁的。`prefix` / `suffix` 缺省 `to_header` 时，原位加工目标为**第一个候选源名**。

### rename / copy 源缺失语义

`rename` 与 `copy` 在所有候选源都不存在（或值为空）时**整体 no-op**：不写目标头，也不会回退成固定值。需要「客户端没发这个头就给默认值」时请改用 `add_if_missing`（`to_header` 固定值）。

### prefix / suffix 叠加语义

`prefix`/`suffix` 的 `to_header` 缺省时等于第一个候选源名（对同一头原位加工）；指定独立 `to_header` 时先取候选源客户端头值，若该目标头已被本插件**前面的规则**写入过值，则基于该累计值继续加工（同一头多规则可串行叠加）。规则间按数组顺序执行。

## 配置示例

把客户端不同会话头统一映射为上游会话头（候选源顺序优先），补齐网关要求的固定标识，并给上游附加一个带前缀的代理 UA 标记：

```json
[
  {
    "action": "rename",
    "from_header": "x-opencode-session, session-id, x-session-id",
    "to_header": "x-session-id"
  },
  {
    "action": "set",
    "to_header": "x-akm-edge",
    "value": "edge-cn-1"
  },
  {
    "action": "add_if_missing",
    "to_header": "x-gateway-token",
    "value": "gt-9f2a"
  },
  {
    "action": "prefix",
    "from_header": "user-agent",
    "to_header": "x-proxy-ua",
    "prefix": "akm/"
  },
  {
    "action": "suffix",
    "from_header": "x-opencode-session, session-id",
    "to_header": "x-session-tag",
    "suffix": ":v2"
  }
]
```

效果：客户端 `x-opencode-session: s-1` + `user-agent: opencode/1.18.26 ...` 的请求，上游将额外收到 `x-session-id: s-1`、`x-akm-edge: edge-cn-1`、`x-gateway-token: gt-9f2a`、`x-proxy-ua: akm/opencode/1.18.26 ...`、`x-session-tag: s-1:v2`。若客户端发的是 `session-id` 而非 `x-opencode-session`，第一条规则自动落到第二个候选，仍写出 `x-session-id`。

## 与原生透传（`use_native_user_agent`）的关系

转发层合并顺序为 **插件覆写 > 原生透传 > 默认头**，全部按序叠加：开启 `use_native_user_agent` 时，客户端业务头先整体透传保留（如 codex 的 `x-oai-attestation` / `chatgpt-account-id` / `x-codex-turn-metadata` 等身份头不丢），本插件再在其上增量补写 / 覆写规则声明的头；未开原生透传时插件单独生效。插件声明值优先于透传值（同头插件覆盖透传）。适用场景：`use_native_user_agent=true` 保留 codex 原生身份头，同时用本插件把客户端会话 id 合成上游强制要求的 `x-opencode-session`——两者可共存，不再互斥丢包。`User-Agent` / `Content-Type` / `accept` 允许被本插件改写；`authorization` / `host` / `content-length` / `connection` / `accept-encoding` / `transfer-encoding` / `upgrade` 等认证与传输基础设施头始终受保护、不可覆写（`authorization` 等敏感头的值在审计日志中会被掩码）。

## 使用建议

1. 先在管理台**单独启用**，用小流量验证日志与行为。
2. 需要「客户端没带头就给默认」用 `add_if_missing`；需要「客户端带了就改名带给上游」用 `rename`；不确定客户端是否携带时优先 `add_if_missing` + 固定值。
3. 不确定客户端是否携带 / 不同客户端头名不一，用 `rename` + 逗号分隔候选源（如 `x-opencode-session, session-id, x-session-id`）顺序优先映射；需要「客户端没带头就给默认」用 `add_if_missing`。
4. 规则里引用的客户端头名来自内核归一化快照（小写）；配置 JSON 里写任意大小写均可。
