# `prompt_profiles` 插件

提示词配置集：按模型、接口和客户端规则叠加注入 system prompt 或 instructions

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 请求/响应过滤 |
| 默认状态 | 默认关闭 |
| 优先级 | `120` |
| Hook | `on_request` |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.prompt_profiles`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | `True` | 关闭后插件保持加载，但不会注入任何提示词。（启用配置集） |
| `profiles_json` | text | `[]` | JSON 数组。每项支持 name、enabled、models（通配符数组）、api_paths、client_patterns、position（before/after）、prompt；所有命中的配置按数组顺序叠加。（提示词配置集 JSON） |
