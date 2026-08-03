# `prompt_booster` 插件

附加提示词注入：在请求发送到 AI 供应商之前注入自定义 system prompt 或指令

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 请求/响应过滤 |
| 默认状态 | —（常驻核心/依赖启用） |
| 优先级 | `—` |
| Hook | `on_request` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.prompt_booster`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `prompt_text` | text | `""` | 注入到请求中的附加 system prompt / instructions 文本（附加提示词） |
| `position` | select | `before` | before=加在原有内容前面，after=追加到末尾（注入位置） |
