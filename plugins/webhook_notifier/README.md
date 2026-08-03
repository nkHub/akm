# `webhook_notifier` 插件

Webhook / 原生 App 告警：上游失败、安全拦截或慢请求时异步去重通知

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 后置处理 |
| 默认状态 | 默认关闭 |
| 优先级 | `180` |
| 路由前缀 | `/api/webhook-notifier` |
| Hook | `on_response` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.webhook_notifier`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 关闭后插件保持加载，但不创建任何通知任务。（启用通知） |
| `app_notifications` | boolean | `True` | 开启后通过菜单栏 App（rumps）弹出 macOS 系统通知。需用 python -m akm.menubar 或打包 App 启动；纯 uvicorn 无此通道。无需打开管理台页面。（原生 App 系统通知） |
| `webhook_url` | text | `""` | 可选通知接收地址。仅支持 http:// 或 https://，为空时只走原生 App 通知（若已开启）。（Webhook 地址） |
| `payload_format` | select | `generic` | 仅影响 Webhook：generic={event,title,text,details}；飞书、企业微信和 Slack 使用各自的文本消息格式。（消息格式） |
| `notify_failures` | boolean | `True` | 通知选 Key、协议转换、连接、读取或上游 HTTP 失败。（通知上游失败） |
| `notify_security_events` | boolean | `True` | 通知 data_filter_guard 的 block、mask、warn 等安全事件。（通知安全事件） |
| `notify_audit_queue_drops` | boolean | `True` | 当健康监护中的 audit_queue_dropped 增长时发送通知。（通知审计队列丢弃） |
| `slow_request_threshold_ms` | number | `0` | 成功请求耗时达到阈值时通知，0 表示关闭慢请求通知。（慢请求阈值毫秒） |
| `cooldown_seconds` | number | `300` | 相同事件在冷却期内只发送一次（Webhook 与 App 通知共用），0 表示不去重。（相同告警冷却秒数） |
| `timeout_seconds` | number | `5` | 仅影响 Webhook；通知失败不会影响代理主请求；超时后仅记录日志。（Webhook 超时秒数） |
| `max_pending_notifications` | number | `32` | 仅限制 Webhook 后台任务数；App 原生通知不占用此上限。（最大待发送 Webhook 数） |
