# `usage_quota_guard` 插件

本地窗口配额：按 Key 和模型限制请求次数及实际响应 Token，用尽后自动跳过当前 Key

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | Key/模型匹配 |
| 默认状态 | 默认关闭 |
| 优先级 | `20` |
| Hook | `on_key_selected`, `on_response` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.usage_quota_guard`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 关闭后插件保持加载，但不限制任何请求。（启用配额控制） |
| `window_seconds` | number | `3600` | 请求数和 Token 用量按固定时间窗口累计；服务重启后当前窗口重新开始。（统计窗口秒数） |
| `max_requests_per_key` | number | `0` | 窗口内单个 Key 允许的最大已选中请求数，0 表示不限制。（单 Key 请求上限） |
| `max_requests_per_model` | number | `0` | 窗口内同一模型允许的最大已选中请求数，0 表示不限制。（单模型请求上限） |
| `max_tokens_per_key` | number | `0` | 窗口内单个 Key 的已观测响应 Token 上限，0 表示不限制；当前请求完成后才会计入。（单 Key Token 上限） |
| `max_tokens_per_model` | number | `0` | 窗口内同一模型的已观测响应 Token 上限，0 表示不限制；当前请求完成后才会计入。（单模型 Token 上限） |
