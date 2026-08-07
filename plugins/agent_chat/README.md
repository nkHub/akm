# `agent_chat` 插件

AKM `/v1/agent` 的 Web 聊天界面（AetherAI 对话窗口）。构建产物已打进本插件
`dist/` 目录，**启用即用，无需配置绝对路径**。

## 使用

1. 在管理台「插件」页启用 `agent_chat`（或在 `~/.akm/config.json` 的
   `plugin_configs.agent_chat` 中配置）。
2. 访问 `<访问路径>`（默认 `http://127.0.0.1:8800/chat`）开始对话。

界面直接请求同源 `/v1/agent` 与 `/v1/models`，上传的图片等资源走
`/agent-uploads/...` 相对路径，不依赖前端代理。

## 配置

| 键 | 类型 | 默认值 | 说明 |
|----|------|--------|------|
| `route_prefix` | string | `/chat` | 聊天界面挂载路径。不能使用 `/`、`/api`、`/v1`、`/admin`、`/health`、`/debug`。修改后需重启服务 |
| `spa_fallback` | boolean | `true` | 将不存在的无扩展名路径回退到 `index.html`。聊天界面为单页应用，建议保持开启 |

## 更新界面

聊天界面源码在独立的 [`chat`](https://github.com/nkHub/chat) 项目。更新步骤：

```bash
cd chat
VITE_BASE='./' VITE_AKM_API_URL='' npm run build
cp -R dist/* ../ccs/plugins/agent_chat/dist/
```

`VITE_BASE='./'` 使产物资源引用为相对路径（可挂载到子路径）；
`VITE_AKM_API_URL=''` 使界面请求同源 `/v1/agent`。
