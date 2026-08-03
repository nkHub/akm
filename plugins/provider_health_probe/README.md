# `provider_health_probe` 插件

供应商健康探测：手动或定时复用 Key 连通性测试，提供脱敏状态快照 API

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 应用类（挂载路由/后台任务） |
| 默认状态 | 默认关闭 |
| 优先级 | `200` |
| 路由前缀 | `/api/provider-health` |
| Hook | — |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.provider_health_probe`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `probe_interval_seconds` | number | `0` | 0 表示仅手动探测；大于 0 时按该间隔探测所有 active Key。（定时探测间隔秒数） |
| `max_concurrency` | number | `3` | 限制同时连接上游的探测任务数量，避免健康检查抢占正常流量。（最大并发探测数） |
| `allow_protocol_fallback` | boolean | `False` | 开启后测试主协议失败时允许尝试供应商的其他兼容协议。（允许协议兼容探测） |
