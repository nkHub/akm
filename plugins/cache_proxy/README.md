# `cache_proxy` 插件

本地响应缓存：对非流式、无工具调用的相同请求在 TTL 内短路返回，进程内内存缓存

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 请求/响应过滤 |
| 默认状态 | 默认关闭 |
| 优先级 | `210` |
| Hook | `on_request`, `on_response` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.cache_proxy`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 关闭后插件保持加载，但不读写缓存。（启用缓存） |
| `ttl_seconds` | number | `300` | 缓存条目存活时间；过期后下次请求重新走上游。（缓存 TTL 秒） |
| `max_entries` | number | `256` | 超出时按最旧条目淘汰；服务重启后缓存清空。（最大缓存条数） |
| `max_body_bytes` | number | `262144` | 超过该大小的响应不写入缓存，避免内存膨胀。（单条最大响应字节） |
| `skip_stream` | boolean | `True` | 开启后 stream=true 的请求不参与缓存（推荐保持开启）。（跳过流式请求） |
| `skip_tools` | boolean | `True` | 请求包含 tools / tool_choice / functions 时不缓存，避免工具会话错乱。（跳过含工具请求） |
| `include_models` | text | `""` | 逗号/换行分隔模型名；留空表示不限制。支持尾部 * 通配。（仅缓存模型（可选）） |
