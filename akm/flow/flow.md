# 工作流引擎（/v1/flow）

`POST /v1/flow` 提供 DAG 工作流引擎，把「需求 → 方案 → 编码 → 审查 → 测试 → 交付」拆成有向无环图：每个节点是一个步骤（`intake` / `plan` / `code` / `review` / `test` / `fix` / `human` / `router` / `merge` / `output`），边上的 `condition` 决定分支走向（`pass` / `fail` / 子串匹配）、`loop` 边支持按预算重入（如审查不通过回到修复）。多路并行节点（如双模型竞赛）同时执行，全部前驱完成后才汇聚（fan-in）。节点输出以 `artifacts` 累积，供下游 `{{artifacts.xxx}}` 模板引用；LLM 调用复用 AKM 代理网关（自动选 Key / 协议转换 / 重试），鉴权与 `/v1/agent` 一致。

实现位于 `akm/flow/`：`engine.py`（执行引擎）、`router.py`（路由）、`db.py`（持久化）、`models.py`（模型目录解析）、`templates.py`（内置模板）、`worktree.py`（git worktree 沙箱）、`workspace_diff.py`（工作区快照 diff）、`path_lock.py`（路径锁）、`pi_runner.py`（编码节点子进程 runner）。

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/flow/health` | 引擎健康检查 |
| GET | `/v1/flow/models` | 可用模型目录（从 AKM Key 模型列表构建，附 mock 兜底） |
| GET | `/v1/flow/workflows` | 工作流列表 |
| POST | `/v1/flow/workflows` | 创建工作流（`name` 必填） |
| GET | `/v1/flow/workflows/{id}` | 查询工作流完整定义 |
| PUT | `/v1/flow/workflows/{id}` | 更新工作流定义 |
| DELETE | `/v1/flow/workflows/{id}` | 删除工作流（连带删除全部运行记录） |
| GET | `/v1/flow/templates` | 内置模板列表（standard_dev / hotfix / dual_model） |
| POST | `/v1/flow/templates/{id}/instantiate` | 实例化模板为工作流（重新生成全部 id） |
| POST | `/v1/flow/workflows/{id}/runs` | 启动运行（`prompt` 必填），返回 `201` 与运行对象 |
| GET | `/v1/flow/runs` | 运行分页列表（`workflow_id` / `limit` ≤500 / `offset`） |
| GET | `/v1/flow/runs/{id}` | 查询运行完整快照 |
| POST | `/v1/flow/runs/{id}/cancel` | 取消运行（运行中/等待审批节点标 cancelled，待激活标 skipped） |
| POST | `/v1/flow/runs/{id}/resume` | 人工审批：body `{action: approve\|reject, note?, nodeId?}`；运行须处于 `waiting_human`，否则 `409` |
| GET | `/v1/flow/runs/{id}/events` | SSE 事件流（先发完整 `snapshot`，再转发 `run_start` / `node_start` / `log` / `token` / `human_wait` / `node_end` / `run_end`，每 1 秒 `ping`），断线重连先收到完整 `snapshot` |

## 数据表

- `flow_workflows`：工作流定义，节点/边以 JSON 存储。
- `flow_runs`：每次运行快照 `data_json`。
- 均位于 `~/.akm/akm.db`。

## 内置模板

- `standard_dev`：标准开发，含 review→fix 循环。
- `hotfix`：快速热修。
- `dual_model`：双模型并行竞赛。
- 可一键实例化为工作流（`POST /v1/flow/templates/{id}/instantiate`）。

## 运行变量（`variables`）

`POST /v1/flow/workflows/{id}/runs` 的 body 支持 `variables`（对象），覆盖合并进本次运行的变量，仅本次运行生效，不修改工作流定义。示例：

```json
{"prompt": "...", "variables": {"projectPath": "/path/to/proj", "language": "HTML"}}
```

| 变量 | 说明 |
|------|------|
| `projectPath` | 编码节点的工作项目路径，支持绝对路径或相对路径；相对路径基于 `agent_workspace_root` 配置的工作区根目录解析（未配置时回退到进程当前目录） |
| `maxNodeVisits` | 单节点最大访问次数（loop 预算），默认 `3`，取值钳制到 `[1, 20]` |
| `useWorktree` | `true` 时编码节点用 git worktree 沙箱隔离；`false`（默认）直接在工作项目目录执行 |
| `worktreeMode` | `run`（整个运行共享一个沙箱）或 `per-coding`（每个编码节点独立沙箱，可真并行写） |
| `keepWorktree` | `true` 时运行结束后保留 worktree，否则自动清理 |

## 节点能力

- **LLM 节点**：执行提示词，输出以 `artifacts` 累积；对上游瞬时 5xx（网关时段性故障）自动重试（指数退避，默认最多重试 2 次，可用 `agent_flow.llm_retry_max` 调整），避免单次瞬时故障拖垮整个运行；4xx 视为请求本身问题不重试。
- **流程控制**：拓扑排序 / 并行执行 / 条件分支 / loop 重入 / `merge` 汇聚 / `router` 路由。
- **编码节点**（`code` / `fix` / `test`）：调用本机 `pi` CLI（subprocess），失败回退 mock 摘要；执行前后对项目目录做工作区快照 diff，产出写入 `run.fileDiffs`；同一项目路径通过路径锁串行化，避免并发写冲突。
- **人工审批**（`human`）：默认自动放行（`flow_human_auto_approve` / `agent_flow.human_auto_approve` 为 `true`），设为 `false` 后运行在审批节点挂起（`waiting_human`），经 `resume` 批准/驳回后继续或终止。
- **git worktree 沙箱**（`variables.useWorktree`）：`run` / `per-coding` 两种模式，隔离编码过程避免污染工作目录。
- **节点级重试**：`data.retry` 字段支持 `{max: N, on: "error"}`（LLM/代理调用失败重试，指数退避 `400ms×attempt`）或 `{on: "review_fail"}`（审查结论为 `fail` 时同节点重执行）。

## pi-agent 定位

优先读取 `~/.akm/config.json` 的 `agent_flow.pi_path` 显式指定的 pi 路径（各机器安装位置不同时可自定义，支持 `~` 展开）；未配置时自动在 PATH 与常见安装目录定位。pi 是 Node 脚本，要求 Node.js ≥22.19.0，运行时自动从 PATH / nvm 各版本中选择满足版本的 node 绝对路径直接执行（打包 app 经 GUI/launchctl 启动时环境 PATH 常为空，绕过 shebang 的 `env node`）。超时默认 1 小时，可用 `agent_flow.pi_timeout_ms`（毫秒）或环境变量 `FLOW_PI_TIMEOUT_MS` / `FLOW_AGENT_TIMEOUT_MS` 覆盖。

## 配置项（`agent_flow` 配置组）

`~/.akm/config.json` 中集中管理工作流引擎行为，未配置时全部使用默认值：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `pi_path` | - | pi 可执行文件路径 |
| `pi_model` | - | 强制 pi 使用的模型（环境变量 `FLOW_PI_MODEL` 优先） |
| `pi_timeout_ms` | `3600000` | 编码节点超时毫秒（环境变量 `FLOW_PI_TIMEOUT_MS` / `FLOW_AGENT_TIMEOUT_MS` 仍优先） |
| `llm_retry_max` | `2` | LLM 节点 5xx 重试次数 |
| `llm_retry_base_delay` | `1.0` | 重试退避基数秒 |
| `llm_temperature` | `0.3` | LLM 节点请求温度 |
| `llm_max_tokens` | `4096` | LLM 节点请求最大 token |
| `human_auto_approve` | `true` | 布尔，覆盖顶层 `flow_human_auto_approve` |
| `worktrees_root` | `~/.akm/flow_worktrees` | git worktree 沙箱根目录 |
| `wsdiff_max_file_bytes` | `120000` | 工作区快照单文件上限 |
| `wsdiff_max_files_scan` | `800` | 快照扫描文件上限 |
| `wsdiff_max_diffs` | `40` | 差异条目上限 |
| `wsdiff_max_content_chars` | `24000` | 快照单文件内容字符上限 |

## 内置工具

`/v1/agent` 注入 `akm_flow_list` / `akm_flow_get` / `akm_flow_save` / `akm_flow_delete` / `akm_flow_run` / `akm_flow_runs` / `akm_flow_run_get`，可在对话里直接管理、驱动工作流：

- `akm_flow_list`：列出已配置的工作流（id / 名称 / 描述 / 节点数 / 更新时间，不含完整定义）。
- `akm_flow_get`：按 id 读取工作流完整定义（nodes / edges / variables）。
- `akm_flow_save`：创建或更新工作流定义（`workflow_id` 留空新建；`nodes` 为节点数组，`data` 含 label/modelId/systemPrompt 等；`edges` 含 source / target / condition / loop；`variables` 为运行变量）。
- `akm_flow_delete`：删除工作流及其全部运行记录。
- `akm_flow_run`：传入工作流 id 与 `prompt` 启动后台运行，返回运行 id 与初始状态。
- `akm_flow_runs`：列出运行记录（可按 `workflow_id` 过滤）。
- `akm_flow_run_get`：查询单次运行的节点级详情（状态/错误/输出正文与结构化产物/最近日志），定位卡住或失败的节点。
