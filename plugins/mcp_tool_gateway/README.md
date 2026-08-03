# `mcp_tool_gateway` 插件

MCP/工具网关：注册本地 HTTP 工具、可选注入到模型 tools 声明，并提供受控调用 API（与 tool_policy_guard 互补，不替代本机沙箱）

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 应用类（挂载路由/后台任务） |
| 默认状态 | 默认关闭 |
| 优先级 | `35` |
| 路由前缀 | `/api/mcp-tools` |
| Hook | `on_request` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.mcp_tool_gateway`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 关闭后不注入工具、不处理调用 API 的执行逻辑（路由仍可能 503）。（启用工具网关） |
| `inject_tools` | boolean | `False` | 开启后将已注册工具以 OpenAI function tools 形式合并进 request.tools（Chat/Responses 兼容结构）。（注入工具声明到请求） |
| `strip_unlisted_tools` | boolean | `False` | 开启后移除不在本网关注册表中的 tools 声明；不影响普通无 tools 请求。（剥离未注册工具声明） |
| `tools_json` | text | `[]` | JSON 数组。每项: name, description?, parameters?(object schema), url, method?(POST), timeout_seconds?, headers?(object)。仅允许 http/https URL。（工具注册表 JSON） |
| `max_argument_bytes` | number | `32768` | 调用工具时 arguments 序列化后的大小上限，防止过大 payload 打爆下游。（参数最大字节数） |
| `default_timeout_seconds` | number | `15` | 单工具未单独配置 timeout 时的 HTTP 超时。（默认超时秒数） |
| `allow_call_api` | boolean | `True` | 关闭后 POST /call 返回 403，仅保留 list/status 与可选注入。（允许 HTTP 调用 API） |
| `allowed_url_hosts` | text | `127.0.0.1,localhost` | 逗号/换行分隔；非空时工具 url 的 host 必须命中列表（降低 SSRF 风险）。留空表示不额外限制 host（仍仅 http/https）。（允许的 URL Host） |
