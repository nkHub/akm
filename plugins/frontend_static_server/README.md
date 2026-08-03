# `frontend_static_server` 插件

托管 Vue、React 等前端构建产物，支持自定义访问路径和 SPA History 路由回退

## 基本信息

| 项 | 值 |
|----|----|
| 类别 | 应用类（挂载路由/后台任务） |
| 默认状态 | 默认关闭 |
| 优先级 | `200` |
| Hook | — |

## 配置项

> 配置存于 `~/.akm/config.json` 的 `plugin_configs.frontend_static_server`，管理台「插件」页可编辑；修改后多数插件热读生效。默认值以插件 `plugin.json` 声明为准。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `build_dir` | string | `""` | Vue/React 打包后的 dist 或 build 目录。支持绝对路径和以启动目录为基准的相对路径。（构建产物目录） |
| `route_prefix` | string | `/web` | 站点挂载路径，例如 /web 或 /console。不能使用 /、/api、/v1、/admin、/health、/debug。修改目录或路径后需要重启服务。（访问路径） |
| `static_dir` | string | `""` | 可选。目录中的文件会挂载到“访问路径/static”，例如 /web/static/logo.png。留空时仅使用构建产物目录内的资源。（独立静态资源目录） |
| `spa_fallback` | boolean | `True` | Vue Router 或 React Router 的 History 模式下，将不存在的无扩展名路径返回 index.html。（启用 SPA 路由回退） |
