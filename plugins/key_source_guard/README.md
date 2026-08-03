# `key_source_guard` 插件

按客户端 User-Agent 来源绑定指定上游 Key；来源不匹配时跳过该 Key

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | Key/模型匹配 |
| 默认状态 | 默认关闭 |
| 优先级 | `10` |
| Hook | `on_key_selected` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.key_source_guard`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 关闭后插件保持加载，但不限制 Key 的客户端来源。（启用 Key 来源绑定） |
| `bindings_json` | text | `[]` | JSON 数组。每项为 {"key_alias": "my-key", "client_patterns": ["ClaudeCode/*"]}；仅列出的 Key 受限，client_patterns 使用 User-Agent glob。（绑定规则 JSON） |
| `block_message` | text | `当前客户端来源无权使用此 API Key。` | 当全部候选 Key 均因来源不匹配被跳过时，返回给客户端的提示。（拒绝提示） |
