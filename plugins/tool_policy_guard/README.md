# `tool_policy_guard` 插件

工具策略：限制声明或续接中的工具名称与危险参数，支持告警或阻断请求

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 请求/响应过滤 |
| 默认状态 | 默认关闭 |
| 优先级 | `30` |
| Hook | `on_request` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.tool_policy_guard`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 关闭后插件保持加载，但不检查工具声明或工具调用续接。（启用工具策略） |
| `mode` | select | `block` | block 直接拒绝本次模型请求；warn 只记录告警。（命中处理方式） |
| `allow_tool_names` | text | `""` | 逗号或换行分隔；非空时只允许这些工具名，支持 * 通配符。（工具白名单） |
| `deny_tool_names` | text | `bash,shell,terminal,exec` | 逗号或换行分隔；命中后拒绝或告警，支持 * 通配符。（工具黑名单） |
| `deny_argument_patterns` | text | `(?i)rm\s+-rf\s+/ (?i)curl\s+[^\n|]+\|\s*(bash|sh) (?i)wget\s+[^\n|]+\|\s*(bash|s…` | 按行填写；仅扫描客户端续接中已存在的工具调用参数，不扫描普通用户文本。（危险参数正则） |
| `block_message` | text | `工具调用不符合当前安全策略，已被拒绝。` | block 模式返回给客户端的错误文本。（阻断提示） |
