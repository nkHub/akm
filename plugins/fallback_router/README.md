# `fallback_router` 插件

模型失败降级：命中指定状态码或网络错误后改用备用模型并重新选 Key

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 流程处理器 |
| 默认状态 | 默认关闭 |
| 优先级 | `10` |
| Hook | `on_upstream_error` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.fallback_router`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 关闭后插件保持加载，但不会改写请求模型。（启用模型降级） |
| `rules` | text | `""` | 每行 source_model=>fallback_model，例如 gpt-5=>gpt-4.1。按声明顺序匹配。（降级规则） |
| `status_codes` | text | `429,500,502,503,504` | 逗号或换行分隔；命中其中任一上游 HTTP 状态码才允许降级。（触发状态码） |
| `error_types` | text | `connect,timeout,read,chunk` | 逗号或换行分隔；仅 status_code 为 0 时生效。（触发网络错误） |
| `max_fallbacks` | number | `1` | 用请求内历史记录阻止循环映射和无限降级。（单请求最大降级次数） |
