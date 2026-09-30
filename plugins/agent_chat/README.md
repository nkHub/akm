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

## 客户端工具与本地会话历史

界面把会话历史存在**浏览器 IndexedDB**（`aether-ai-chat` / `chat-state` / `main`），
服务端读不到。为了让模型也能查询这些历史，界面通过 `/v1/agent` 的**客户端工具执行协议**
自己声明并执行两个工具：

| 工具 | 作用 |
|------|------|
| `ui_list_sessions` | 列出浏览器本地会话（会话名、标题、创建/更新时间、消息数、模型），不含正文 |
| `ui_load_session` | 三合一：传 `query` 搜索全部会话；传 `name` 读该会话**一页**消息；都不传则等同于列出会话元数据 |

**按需取用、不全量读取**：单个会话可能有上千条消息（用户侧不做硬上限），所以
`ui_load_session` 的返回一律有界——分页 `offset 0` 是**最新一页**（从最近往前翻），
单次最多 50 条、默认 20 条，`limit` 在实现层被夹紧，模型要多大都不会一次拉全量；
返回 `total` / `has_more` / `range` 供翻页（`offset` 翻过头会收敛到最早一页，不会给出
空页）。搜索只回命中片段与 `offset_from_latest`，命中总数单独计数、只回传 20 条
（`truncated` 提示还有更多），并**优先保留最近的命中**。系统提示词同时要求模型
「不要大量读取聊天记录或试图读完整个会话」：先列会话、再搜索定位、只读相关一两页。
分页/搜索的纯逻辑在 `src/lib/session-history.ts`，边界有 `npm run check:history` 覆盖
（esbuild + node，26 项断言）。

请求时它们通过顶层 `client_tools` 字段声明（源码见 `src/lib/client-tools.ts`）；模型调用时
服务端下发 `client_tool_call` 事件，界面在浏览器本地执行后把结果作为 `role: "tool"` 消息
追加入 `messages` 续跑。

**对话历史只保存在浏览器 IndexedDB**：服务端不再落盘 Agent 历史，也不注册读取磁盘快照的
`akm_list_sessions` / `akm_load_session` 工具。两套工具并存曾导致模型选中服务端旧快照而不是
浏览器里的实时记录，因此直接删除了服务端工具与写盘逻辑。旧版遗留的
`~/.akm/agent_sessions/` 会在更新包缓存清理开启时随维护流程永久删除（不备份）；新版本不再创建该目录。

客户端工具协议的完整说明（声明方式、同名规则、授权、续跑约定）见
[`akm/agent_runtime/agent.md`](../../akm/agent_runtime/agent.md) 的「客户端工具执行」。

## 更新界面

聊天界面源码在独立的 [`chat`](https://github.com/nkHub/chat) 项目。更新步骤：

```bash
cd chat
VITE_BASE='./' VITE_AKM_API_URL='' npm run build
cp -R dist/* ../ccs/plugins/agent_chat/dist/
```

`VITE_BASE='./'` 使产物资源引用为相对路径（可挂载到子路径）；
`VITE_AKM_API_URL=''` 使界面请求同源 `/v1/agent`。
