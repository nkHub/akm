# AI Key Manager

本地 AI API Key 管理代理服务。集中管理多个 AI 供应商的 API Key，自动根据优先级选择可用 Key，支持故障切换、请求代理转发及完整审计日志。

## 安装

```bash
pip install -e .
```

## 打包为 macOS 应用

> 需要 Python 3.12+（推荐 3.12.13），打包过程中 `pyproject.toml` 与 py2app 冲突需临时移走。

```bash
# 开发模式运行（无需打包，直接测试）
python -m akm.menubar

# 安装打包工具
pip install py2app pillow setuptools

# 打包（生成 dist/AI Key Manager.app）
mv pyproject.toml pyproject.toml.bak
python setup.py py2app
mv pyproject.toml.bak pyproject.toml

# 推荐：使用脚本打包（自动处理 pyproject 备份恢复）
./scripts/build_app.sh

# 可选：清理构建缓存后重新打包
rm -rf build dist
python setup.py py2app
```

应用图标由 `logo.icns` 提供，通过 `setup.py` 中的 `iconfile` 选项配置。详细打包规范、版本号管理及更新方案见 [docs/release-guide.md](docs/release-guide.md)。

## 快速开始

```bash
# 1. 添加 Key
akm key add my-key deepseek

# 2. 启动代理服务（自动打开管理台）
akm serve

# 3. 将客户端 base_url 指向代理
# http://127.0.0.1:8800/v1
```

## CLI 命令

```bash
akm --help                    # 查看帮助

# Key 管理
akm key add <别名> <供应商>     # 添加 Key
akm key list                   # 列出所有 Key
akm key remove <别名>           # 删除 Key
akm key disable <别名>          # 禁用 Key
akm key enable <别名>           # 启用 Key
akm key set-key <别名>          # 修改 API Key
akm key set-priority <别名> <N> # 设置优先级
akm key set-base-url <别名> <URL> # 修改 API 地址
akm key test <别名>             # 测试连通性

# 服务
akm serve                      # 启动代理（默认 :8800）
akm serve --port 8080          # 指定端口
akm serve --no-open            # 不自动打开浏览器

# 日志
akm log list                   # 查看最近日志
akm log clean --before YYYY-MM-DD # 清理旧日志
```

## 菜单栏应用

```bash
# 开发模式
python -m akm.menubar

# 安装后
akm-menubar
```

状态栏显示 logo 图标，下拉菜单：

| 菜单项 | 说明 |
|--------|------|
| 🟢/🟡/🔴 状态 | 运行中 / 启动中 / 失败 |
| 打开管理 | 浏览器打开 Web 管理台 |
| 重启服务 | 停止并重新启动代理（端口变更后生效） |
| 退出 | 退出应用 |

启动时从 `~/.akm/config.json` 读取配置（端口、是否自动打开管理台等）。

## Web 管理台

`akm serve` 启动后访问 `http://127.0.0.1:8800/admin`

| 页面 | 功能 |
|------|------|
| 统计 | Token 用量仪表盘（骨架屏加载、缓存命中与缓存创建独立展示、输入 Token 不含缓存、按 Key/模型/日期分组、K/M 格式、1d/7d/30d 自然日切换、时间筛选本地缓存） |
| 审计 | 请求日志（输入/缓存/输出 Token 列、Key/状态筛选、成功/失败切换、筛选持久化、正倒序、每页 10 条、JSON/会话 WebComponent 渲染、超长内容阈值控制；日志头可查看 token 回填来源 flags） |
| 管理 | Key 增删改查、启用/禁用、优先级排序、最近 10 次成功请求平均延迟展示、连通性测试、自定义测试结果弹窗、一键导出备份、一键刷新提供商模型列表、模型标签点击复制、展示提供商模型列表、每页 12 条分页 |
| 插件 | 插件列表、启用/禁用开关、上传 .zip 安装、插件配置读写（无界面插件不显示转换节点；有界面插件仅在启用后可点击进入页面） |
| 设置 | 分区布局（服务/日志/供应商代理）、端口配置、日志保留天数、日志体积控制（请求/响应体开关）、并排双按钮（清空日志 / 清空请求响应体）、JSON 渲染阈值（显示数据库大小）、供应商代理管理 |
| 关于 | 版本与功能简介（展示内置插件能力） |

## 配置

配置文件位于 `~/.akm/config.json`，可通过 Web 设置页面修改：

```json
{
  "auto_open_admin": true,
  "log_retention_days": 30,
  "server_port": 8800,
  "log_request_body": false,
  "log_response_body": false,
  "stats_include_estimated_usage": false,
  "json_viewer_max_text_length": 600000
}
```

Key 和日志数据存储在 `~/.akm/akm.db`（SQLite）。另外，Key 的增删改、启停和模型刷新会额外追加写入 `~/.akm/keys.log`，它的定位是“Key 配置/状态审计日志”，主要用于复盘谁在什么时间改了哪些 Key 元数据，不包含 `api_key` 明文；事件名统一采用 `key.config.*`、`key.status.*`、`key.models.*` 这种层级化审计风格。运行时卡顿、请求堆积、连接池重建等问题请优先查看 `/health/detail` 与 `/debug/runtime`。

`stats_include_estimated_usage` 仅作为 `config.json` 隐藏配置项存在，默认 `false`，不会在设置页单独展示；如需让首页统计把 `usage_estimated_light` 这类估算 token 计入总量与请求数，可手动改为 `true`。

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI Chat Completions 接口 |
| POST | `/v1/messages` | Anthropic Messages 接口（兼容 Claude Code，非 OpenAI 格式） |
| POST | `/v1/responses` | OpenAI Responses API 接口（Codex 兼容） |
| POST | `/v1/embeddings` | Embeddings 转发接口（仅透传，不做协议转换） |
| GET | `/v1/models` | 模型列表 |
| GET | `/health` | 健康检查 |
| GET | `/health/live` | 存活探针：仅表示服务进程仍在响应 HTTP |
| GET | `/health/ready` | 就绪探针：根据事件循环、审计积压、DB 探针等判断是否适合继续接流量 |
| GET | `/health/detail` | 详细健康状态：返回聚合状态、原因和关键运行时指标 |
| GET | `/debug/runtime` | 运行时诊断快照：返回进程 RSS、线程数、fd 数、审计队列和健康监护状态 |
| GET | `/debug/runtime/history` | 最近运行时事件环形缓冲：返回连接池重建、审计队列丢弃、DB 探针失败、健康状态变化等事件 |
| GET | `/api/keys` | Key 列表（脱敏，附带最近 10 次成功请求平均延迟与已同步的提供商模型列表） |
| POST | `/api/keys` | 添加 Key |
| PUT | `/api/keys/{alias}` | 编辑 Key |
| PATCH | `/api/keys/{alias}/status` | 启用/禁用 Key |
| DELETE | `/api/keys/{alias}` | 删除 Key |
| POST | `/api/keys/{alias}/test` | 测试连通性 |
| POST | `/api/keys/refresh-models` | 批量刷新所有 Key 的提供商模型列表 |
| GET | `/api/keys/export` | 导出 Key 配置（含完整密钥） |
| GET | `/api/logs` | 审计日志（支持 status/days/key_alias 筛选；days 按自然日区间；可选 `hide_est=true` 隐藏 `usage_estimated_light` 且低延迟/低 completion 的元数据请求） |
| GET | `/api/logs/size` | 本地缓存占用（数据库 + WAL/SHM + `.log` 文件） |
| POST | `/api/logs/clean` | 清空日志 |
| POST | `/api/logs/clean-bodies` | 清空请求体/响应体（保留元数据与统计列） |
| GET | `/api/stats` | Token 统计（支持 days 自然日范围：1=今天，7=近7天，30=近30天） |
| GET/POST | `/api/config` | 配置读写 |
| GET | `/api/agents` | 供应商代理列表（内置 + 自定义） |
| POST | `/api/agents` | 添加自定义供应商代理 |
| DELETE | `/api/agents/{name}` | 删除自定义供应商代理 |
| GET | `/api/plugins` | 插件列表 |
| POST | `/api/plugins/{name}/toggle` | 启用/禁用插件 |
| DELETE | `/api/plugins/{name}` | 删除第三方插件 |
| POST | `/api/plugins/upload` | 上传 .zip 安装第三方插件 |
| GET/POST | `/api/plugin-config/{name}` | 插件配置读写 |

## 故障切换策略

> `error_handler` 插件接管错误处理策略，可在管理台「插件」页面配置最大重试次数和 Key 切换上限。

| 状态码 | 行为 |
|--------|------|
| 429 | 标记限流，60 秒冷却后恢复 |
| 401/403 | 禁用 Key |
| 402 | 禁用 Key（余额不足） |
| 5xx | 指数退避重试（同 Key 最多 3 次），失败后切换 Key |
| 连接/超时 | 指数退避重试后切换 Key |

Key 选择分两阶段：优先精确匹配当前 model 的 Key（按优先级依次尝试，已失败 Key 自动排除），精确匹配全部不可用时回退到 `models='*'` 的 Key。`*` 不再表示“无条件匹配全部模型”，而是表示“保存时自动同步 `{base_url}/models`，并在这些提供商模型列表中参与匹配”；如果该 key 没有可用的 provider 模型列表，则不会参与 wildcard 匹配。

> 应用重启后，数据库中残留的 `rate_limited` 状态会自动恢复为 `active`。
> Key 的 models 字段存储时自动规范化（去除逗号前后空格），防止匹配失败。
> Key 管理页保存时会自动同步 `{base_url}/models`；`*` 不能和自定义模型同时使用。测试 wildcard Key 时也会直接使用这份已同步模型列表；如果列表为空，需要先保存或刷新模型。

## 流式转发

请求转发会跟随客户端的流式意图：

- 客户端 `stream=true` → 逐块透传 SSE
- 客户端 `stream=false` → 直接请求上游普通 JSON 并原样/按协议转换后返回
- 流式结束后异步写入审计日志（完整响应体用于统计和对话回放）
- 流式请求的插件 `on_response` 生命周期会等到 SSE 真正结束后才触发，避免并发计数过早回收导致慢 key 持续拥塞
- 流式响应的内存捕获已改为有界模式：默认最多保留 `256KB`（配置项 `stream_capture_max_bytes`），超出后仅保留头尾两段并追加截断标记，日志 flags 会记录 `stream_capture_truncated`

## 健康监护

当前版本已内置轻量监护：

- 后台心跳会周期性检测事件循环卡顿和 SQLite 探针状态
- AI 请求会登记 in-flight 请求数，流式请求会登记 active streams
- 审计日志写入已改为“有界队列 + 单 worker”模式：高峰期会优先保护主请求链路，队列满时丢弃新增审计日志，并把 backlog / dropped 信号暴露给健康探针
- 上游连续失败次数会被聚合进健康状态，便于区分“本地卡住”和“上游雪崩”
- 当上游连续失败次数达到阈值时，服务会自动软重建共享 `http_client`，尝试恢复异常连接池或脏连接状态

`/health/detail` 当前会暴露以下关键指标：

- `event_loop_lag_ms` / `max_event_loop_lag_ms`
- `inflight_requests`
- `active_streams`
- `pending_audit_tasks`
- `audit_queue_dropped`
- `db_consecutive_failures` / `db_last_latency_ms`
- `consecutive_upstream_failures`
- `http_client_recreate_count`
- `http_client_last_recreated_at`
- `http_client_last_recreate_reason`

`/debug/runtime` 面向排障使用，会额外补充：

- 进程 `pid`
- `rss_bytes`（当前进程 RSS）
- `thread_count`
- `gc_counts`
- `open_fds`（当前平台支持时）
- 审计队列状态与最近错误

`/debug/runtime/history` 会保留最近一段运行时事件（环形缓冲），当前覆盖：

- 连接池软重建：`http_client.recreated`
- 审计队列丢弃：`audit.queue.dropped`
- DB 探针失败：`db.probe.failed`
- 健康状态变化：`health.status.changed`

## 供应商代理与插件系统

### 内置供应商

| 供应商 | Chat | Responses | Messages | 说明 |
|--------|------|-----------|----------|------|
| openai | ✓ | ✓ | - | 原生支持 Chat + Responses |
| deepseek | ✓ | - | ✓ | 支持 Messages（DeepSeek 官方 Claude 兼容入口） |
| anthropic | - | - | ✓ | 不支持 Chat，自动转 Messages |

当 Key 的供应商不支持请求的 API 协议时，akm 自动进行格式转换，对客户端透明。例如 Codex CLI 通过 `/v1/responses` 调用 DeepSeek Key 时内部自动转为 `/v1/chat/completions`。

供应商代理管理新增了 `Messages -> /anthropic` 开关：

- `deepseek` 内置默认开启，请求 `/v1/messages` 时会自动转到 `/anthropic/v1/messages`
- 自定义供应商默认关闭；只有供应商的 Claude 兼容入口也挂在 `/anthropic/v1/messages` 时才需要开启

### 插件系统

akm 核心仅保留请求转发与审计日志，协议转换、模型匹配、错误处理等由插件接管：

| 插件 | 类型 | 必需 | 职责 |
|------|------|:---:|------|
| `protocol_converter` | converter | | Responses/Messages/Chat 三格式双向转换 |
| `model_matcher` | matcher | ✓ | 模型别名映射与 Key 模型匹配 |
| `error_handler` | handler | | 429 限流换 Key、5xx 指数退避重试 |

插件位于 `akm/plugins/`（内置）、项目根目录 `plugins/`（项目本地）或 `~/.akm/plugins/`（第三方），通过管理台「插件」页面启用/禁用/上传。`model_matcher` 标记为必需（`required: true`），不可禁用。

当前版本不做“上游可逆加密”；如果上游没有对应解密协议，直接发送密文会让模型只能看到密文内容。

`proxy` 已补充 `on_response` 生命周期事件：每次上游尝试结束（成功/失败）都会向插件发送结构化元信息（如 `phase`、`status_code`、`key_alias`、`latency_ms`、`action`），用于审计增强与并发状态回收。

`model_matcher` 新增可配置的并发/慢 key 旁路策略（默认关闭，保守模式）：

- `enable_inflight_bypass`：是否启用拥塞旁路（默认 `false`）
- `max_inflight_per_key`：单 key 并发阈值（默认 `3`）
- `slow_inflight_threshold_sec`：最老 in-flight 慢请求阈值秒数（默认 `8`）

可选开启“智能旁路”（同样默认关闭），在触发拥塞旁路后对多个候选 key 进行健康打分择优：

- `enable_smart_bypass`：启用健康分择优（默认 `false`）
- `smart_bypass_candidate_pool`：候选评估数量（默认 `4`）
- `smart_bypass_min_improve`：最小改善阈值（默认 `0.15`）
- `smart_bypass_error_cooldown_sec`：错误冷却惩罚窗口（默认 `15` 秒）

开启后，仅在当前 key 明显拥塞时尝试旁路到其他可用 key；若无替代 key 则自动回退当前 key，不会额外硬失败。

更完整的 Hook/字段定义见 `docs/design/plugin-system.md`。

#### 协议转换细节

`protocol_converter` 内联三种格式的双向转换逻辑，按 `api_path` 自动选择适配方向：

| 请求格式 | 供应商不支持时 | 请求转换 | 响应 SSE 转换 |
|----------|---------------|----------|---------------|
| `/v1/responses` | → `chat/completions` | Responses → Chat | Chat SSE → Responses SSE |
| `/v1/messages` | → `chat/completions` | Messages → Chat | Chat SSE → Messages SSE |
| `/v1/chat/completions` | → `messages` | Chat → Messages | Messages SSE → Chat SSE |

##### Responses → Chat 发送方向

- `instructions` → `system` 角色消息
- `input` 中的 `function_call` / `function_call_output` → `tool` 角色消息
- 连续 `function_call` 合并为单条 `assistant` 消息（含多个 `tool_calls`）
- `function_call` 上的 `reasoning_content` 回写到合并后的 `assistant` 消息
- 消息重排序：`system` 移到 `assistant(tool_calls)` 之前
- `_clean_schema()` 递归移除 `additionalProperties` / `strict` 字段
- `namespace` 类型工具递归展开子工具（支持 MCP）
- `tool` 结尾时追加空 `user` 消息，触发 DeepSeek 继续生成
- `reasoning_effort` 兜底注入 `"high"`（v4-pro 原生值）

##### Chat SSE → Responses SSE 接收方向

| Chat SSE delta | Responses SSE 事件 |
|----------------|-------------------|
| `delta.reasoning_content` | `response.output_item.added`（type=reasoning）+ `response.reasoning_summary_text.delta` |
| `delta.content` | `response.output_text.delta` |
| `delta.tool_calls`（首 chunk） | `response.output_item.added`（type=function_call） |
| `delta.tool_calls`（后续） | `response.function_call_arguments.delta` |
| `finish_reason: tool_calls` | `response.function_call_arguments.done` + `response.output_item.done` |

推理内容通过独立的 `reasoning` 类型输出项流式推送，Codex 在「思考」面板中折叠展示，与正文分离。

### 自定义供应商

在设置页「供应商代理管理」可添加自定义供应商（如第三方中转站），定义其默认 base_url、认证头模板和协议能力，持久化到 `~/.akm/config.json`。添加后新建 Key 选择该供应商时可省略 base_url 和认证头。

Key 管理页「添加 Key」中的供应商下拉会自动读取 `/api/agents`（即设置页维护的数据），并保留「自定义」选项用于手工填写 `base_url`/`auth_header`。保存 Key 时，管理台会同步请求该提供商的 `{base_url}/models`，并把返回的模型列表缓存到当前 key，用于前端展示以及 `models='*'` 时的模型匹配。

```json
// ~/.akm/config.json 中 custom_agents 示例
{
  "custom_agents": {
    "dmxapi": {
      "default_base_url": "https://www.dmxapi.cn/v1",
      "default_auth_header": "{api_key}",
      "supports_chat": true,
      "supports_responses": false,
      "supports_messages": false,
      "messages_use_anthropic_path": false
    }
  }
}
```

## 认证头配置

Key 编辑表单支持自定义认证头模板，`{api_key}` 占位符会被替换为实际 Key：

| 场景 | auth_header |
|------|------------|
| OpenAI / DeepSeek 官方 | `Bearer {api_key}` |
| 第三方中转（如 dmxapi.cn） | `{api_key}` |
| 其他自定义 | `Api-Key {api_key}` 等 |

## 技术栈

- Python 3.12+ / FastAPI / uvicorn
- httpx 共享连接池（lifespan 管理，TCP keep-alive 复用）
- SQLite（审计日志持久化，WAL 模式，token 列直读 + 内存缓存优化统计性能）
- Fernet 加密（Key 存储）
- rumps（macOS 菜单栏）
- Tailwind CSS / marked.js（Web UI）
