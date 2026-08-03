# `rate_limit_guard` 插件

本地限流：按全局/模型/用户限制 RPM、RPH 与并发；超限在转发前返回 429

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 请求/响应过滤 |
| 默认状态 | 默认关闭 |
| 优先级 | `15` |
| Hook | `on_request`, `on_response` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.rate_limit_guard`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 关闭后插件保持加载，但不限制任何请求。（启用限流） |
| `scope` | select | `global` | global=进程全局；model=按请求 model；user=按请求体 user 字段（空则归为 anonymous）。（限流维度） |
| `max_requests_per_minute` | number | `60` | 固定 60 秒窗口内允许的最大请求数，0 表示不限制。（每分钟请求上限） |
| `max_requests_per_hour` | number | `0` | 固定 3600 秒窗口内允许的最大请求数，0 表示不限制。（每小时请求上限） |
| `max_concurrent` | number | `0` | 同一限流键上允许的在途请求数，0 表示不限制；在响应结束后释放。（最大并发） |
| `block_message` | text | `请求过于频繁，已被本地限流插件拦截。` | 返回给客户端的错误文案。（限流提示） |
