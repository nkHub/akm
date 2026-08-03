# `response_schema_guard` 插件

结构化响应校验：校验调用方声明的 JSON 模式，失败时告警或返回协议兼容错误

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 后置处理 |
| 默认状态 | 默认关闭 |
| 优先级 | `60` |
| Hook | `on_response` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.response_schema_guard`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 仅处理调用方已经声明 JSON 输出格式的非流式成功响应。（启用结构化校验） |
| `mode` | select | `block` | block 返回协议兼容的校验错误响应；warn 保留模型原始响应并记录安全事件。（校验失败处理） |
| `block_message` | text | `模型响应不符合调用方声明的 JSON 格式。` | block 模式返回给客户端的提示文本。（校验失败提示） |
