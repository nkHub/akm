# Agent Loop

`POST /v1/agent` 提供多轮 LLM 工具调用编排能力。请求传入对话历史和工具定义，Agent Loop 内部循环调用 LLM → 解析 `tool_calls` → 执行工具 → 回传结果，直到 LLM 返回最终文本回复或达到最大轮次。

每次 LLM 调用通过 `proxy.forward_request` 透传，自动复用 Key 选择、协议转换、重试等所有现有能力。

Agent 实现集中在 `akm/agent_runtime/`：`router.py` 提供端点、`loop.py` 负责多轮编排、`tools.py` 提供内置只读调试工具与工作区文件工具、`service.py` 负责服务启动时的初始化、`sessions.py` 负责会话持久化（`/v1/agent` 请求结束自动落盘到 `~/.akm/agent_sessions/*.json`，供 `akm_load_session` / `akm_list_sessions` 工具及客户端回顾使用；设置 `agent_session_auto_save=false` 可关闭，保持无状态不写磁盘）。

服务启动后会自动为每次 Agent 请求注入以下只读 AKM 调试工具与工作区文件工具。它们仅作用于 `/v1/agent` 和 `/agent`，不会进入常规转发端点（如 `/v1/chat/completions`、`/v1/messages`、`/v1/responses`）。工作区文件工具（`akm_read_file` 等）需在 config.json 配置 `agent_workspace_root` 才会注册，写工具与 shell 工具默认不注册（见「工作区文件工具」章节）。客户端显式声明同名已注册工具时，服务端会保留客户端的授权意图，但始终使用服务端工具定义，避免参数契约不一致：

| 工具 | 用途 |
|------|------|
| `akm_get_status` | 查询健康监护、审计队列和插件状态 |
| `akm_list_keys` | 查询 Key 的别名、供应商、状态和模型列表，不返回 API Key 或连接地址 |
| `akm_get_keys_summary` | 返回当前 Key 总数，以及每个 Key 的供应商与模型清单，不返回 API Key 或连接地址 |
| `akm_list_logs` | 查询近期审计摘要，可按状态、天数和 Key 别名筛选；不返回请求体、响应体或请求头 |
| `akm_get_usage_stats` | 查询 Token 用量统计（默认同时返回最近 1/7/30 天）；开启 `cost_stats_enabled` 时额外返回费用估算与模型单价表 |
| `akm_get_time` | 获取服务器当前时间，返回本地 ISO 时间、UTC 时间、UNIX 时间戳与时区 |
| `akm_get_config` | 读取 AKM 运行配置；密钥类字段（`agent_api_token`、`tavily_api_key`）不做明文透出，仅标记是否已配置 |
| `akm_list_plugins` | 列出已加载插件的非敏感摘要：名称、版本、分类、描述、是否内置、是否启用与来源 |
| `akm_list_sessions` | 列出历史 Agent 会话的元信息（会话名、创建/更新时间、消息数、模型），不含消息正文，按更新时间倒序 |
| `akm_load_session` | 读取历史 Agent 会话的最近若干条消息（`limit` 1-100，默认 20），用于回顾之前会话的上下文 |
| `akm_list_tasks` | 列出已配置的定时任务（akm 后台任务系统，见「定时任务」章节）：任务 id、名称、类型、间隔、启用状态与执行时间；可用 `task_type` / `enabled=1` 过滤 |
| `akm_create_task` | 创建一条定时任务（见「定时任务」章节）：`agent_call` 类型需 `payload.messages`（周期调用 Agent Loop 跑一轮对话），`usage_query` 类型需 `payload.alias`；`interval_sec` 为循环间隔秒数，0（默认）表示单次执行后自动禁用 |
| `akm_delete_task` | 按 `akm_list_tasks` 返回的 `task_id` 删除一条定时任务，返回是否删除成功 |
| `tavily_search` | 通过 Tavily 实时联网搜索，返回含标题、链接和摘要的搜索结果；需先在 config.json 中配置 `tavily_api_key` |
| `akm_search_kb` | 检索 `markdown_kb` 插件索引的 Markdown 知识库，返回命中文档片段（标题/文件名/分数/内容）；需本机已启用并索引 markdown_kb 插件 |
| `akm_generate_image` | 调用 AKM 配置的图片生成模型生成图片，返回图片资源（url + 本地路径 + `/agent-uploads/...` HTTP 地址）；需配置 `image_supported_models` 对应的可用 API Key |
| `akm_edit_image` | 编辑图片（如重绘局部、扩展内容），返回编辑后的图片资源（url + 本地路径 + `/agent-uploads/...` HTTP 地址）；本地路径仅允许工作区或上传目录，亦可传入 base64 数据 |
| `akm_read_file` | 读取工作区内的文本文件（可指定 `offset` / `limit`），返回内容与起始行号；单文件超 50MB 拒绝，单次返回超 60KB 截断并标记 |
| `akm_list_dir` | 列出工作区内目录的条目（名称、类型、大小），供模型感知工作区结构 |
| `akm_glob` | 按 glob 模式匹配工作区内文件（如 `**/*.py`），返回相对路径列表；遍历跳过隐藏目录与 `node_modules`/`.git`/`build` 等依赖构建目录，并设条目预算防止全盘递归（超限时标记 `truncated`） |
| `akm_grep` | 在工作区内按正则搜索文件内容，返回命中文件、行号与行内容（限制最多 100 条；单文件超 10MB 跳过） |
| `akm_file_info` | 查询工作区内文件/目录的类型、大小与修改时间 |
| `akm_write_file` | 写入/覆盖工作区内文件（需开启 `agent_write_tools_enabled`；单次内容超 10MB 拒绝） |
| `akm_edit_file` | 结构化编辑工作区内文件：行号模式（传 `start_line`，可配 `end_line` 与锚点 `old_string` 校验）把行区间整体替换为 `new_content`，内容模式把 `old_string` → `new_string`（支持 `replace_all`）；需开启 `agent_write_tools_enabled` |
| `akm_make_dir` | 在工作区内递归创建目录（需开启 `agent_write_tools_enabled`） |
| `akm_delete_file` | 删除工作区内的文件或目录。默认只删除**单个文件**；`recursive=true` 可删除目录并递归清除其中所有内容（批量删除）；始终禁止删除工作区根目录；需开启 `agent_write_tools_enabled`） |
| `akm_xlsx` | 创建工作区内 `.xlsx` 文件（`action=create`，`data` 传二维数组或 `{工作表名: 二维数组}`，已存在需 `overwrite=true`）或修改已有文件的单元格（`action=edit`，`updates` 传 `[{"sheet","cell","value"}]`）；可选 `styles` / `column_widths` / `row_heights` / `merge_cells` / `freeze_panes` / `charts` 自定义样式、布局与图表；需开启 `agent_write_tools_enabled` |
| `akm_run_shell` | 在工作区内用系统 shell 执行命令字符串并返回输出与退出码（需开启 `agent_run_shell_enabled`） |
| `akm_run_git` | 在工作区内执行固定的结构化 Git 操作并返回输出与退出码；`dir` 参数可切换工作区内任意仓库目录（含根目录），默认工作区根（需开启 `agent_git_enabled`） |
| `akm_send_email` | 通过 SMTP 发送纯文本邮件，返回 Message-ID（需管理员在 config.json 配置 `agent_email_smtp_host`/`agent_email_smtp_user`/`agent_email_smtp_password` 并开启 `agent_email_enabled`）；支持自定义发件人 `from_`，正文单次上限 10MB |
| `akm_send_notification` | 发送 macOS 原生系统通知，在当前 Mac 桌面弹出提醒（需通过菜单栏启动 AKM 才能正常展示，受 `agent_notify_enabled` 控制，默认开启）；适合推送任务完成、定时提醒等短消息，不产生网络流量 |
| `akm_clipboard_get` | 读取当前 macOS 剪贴板文本内容，返回内容与长度（超过 10 万字符截断并标记 `truncated`） |
| `akm_clipboard_set` | 将文本写入当前 macOS 剪贴板（超 10 万字符截断），返回写入长度 |
| `akm_system_info` | 返回本机系统信息：macOS 版本、架构、主机名、硬件型号、CPU 型号与核数、内存字节数、Python 版本与服务器本地时间 |
| `akm_open` | 打开指定目标：`kind=url` 仅允许 `http/https` 链接、`kind=path` 打开工作区内文件或目录（沙箱校验）、`kind=app` 按应用名启动（不接受路径分隔符） |
| `akm_frontmost_app` | 返回当前前台应用的信息（名称、Bundle ID、进程 ID）；无活跃 GUI 会话时返回空 |
| `akm_context_status` | 查询当前对话上下文的 token 占用（估算已用 token、上限与剩余空间），用于判断是否需要压缩早期历史 |
| `akm_compact_context` | 主动压缩当前对话的早期历史为一段摘要，保留最近约 `agent_keep_recent_messages` 条消息（工具调用与配对消息自动完整保留） |
| `akm_flow_list` | 列出已配置的工作流（akm flow 工作流引擎）：返回工作流的 id、名称、描述、节点数与更新时间，不含完整定义 |
| `akm_flow_get` | 按 id 读取一条工作流的完整定义（nodes / edges / variables），供检查流程结构或复制修改 |
| `akm_flow_save` | 创建或更新一条工作流定义：`workflow_id` 留空则新建，传已有 id 则更新；`nodes` 为节点数组（`type` 取值 intake/plan/code/review/test/fix/human/router/merge/output，`data` 含 label/modelId/systemPrompt 等）、`edges` 为连线数组（含 source / target / condition / loop）、`variables` 为运行变量（如 projectPath / maxNodeVisits） |
| `akm_flow_delete` | 删除一条工作流及其全部运行记录，返回是否删除成功 |
| `akm_flow_run` | 启动一次工作流运行：传入工作流 id 与用户提示词 `prompt`，后台执行 DAG（LLM 节点 / 条件分支 / 循环重入 / 并行），返回运行 id 与初始状态 |
| `akm_flow_runs` | 列出工作流运行记录（按创建时间倒序）：返回运行 id、状态、输入摘要、token 用量与起止时间；可按 `workflow_id` 过滤 |
| `akm_subagent_spawn` | 开启一个独立的子 Agent 子进程，子进程调用本机 `/v1/agent` 运行次级对话（进程级隔离，默认独立临时工作区，不污染当前工作区）；返回 `task_id` 与初始状态。嵌套层数由 `agent_subagent_max_depth` 控制（默认 1，即主会话可开子进程、子进程内不能再开下一级） |
| `akm_subagent_wait` | 等待指定子 Agent 完成并返回其结果（`final_message` 文本）；`timeout_ms` 默认 600000，超时返回「仍在运行」而非失败，可稍后再次查询或用 `akm_subagent_kill` 终止 |
| `akm_subagent_kill` | 终止指定子 Agent 子进程（含其进程组），返回是否成功 |
| `akm_subagent_list` | 列出全部子 Agent 任务（按创建时间倒序）：返回每个任务的 `task_id`、状态、嵌套层数、模型、工作区与创建时间 |
| `akm_subagent_status` | 查询单个子 Agent 任务详情：返回状态、嵌套层数、模型、工作区、日志路径、退出码与日志尾部（最多 2000 字符，超长会截断并标记 `log_truncated`） |
| `akm_ask_user` | 向用户提出澄清问题并等待回答：当用户请求信息不完整、存在歧义或缺少关键参数时调用，本轮立即中断并把问题返回给客户端，用户回答后携带上下文续跑。支持三种交互模式：不传 `options` 时用户自由文本回答；传 `options` 时用户从候选中单选（`multiple` 缺省 `false`）；传 `options` + `multiple: true` 时多选（详见「交互式澄清提问」） |

## 配置

以下 `agent_*` 配置项在 `~/.akm/config.json` 中归组于嵌套的 `agent_config` 对象下（内存加载与前端展示仍以扁平键名呈现，键名不变，可直接按本表检索）。`tavily_api_key` 位于配置顶层。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `agent_max_turns` | `100` | Agent Loop 最大迭代轮次，防止工具调用无限循环 |
| `agent_max_context_tokens` | `272000` | Agent Loop 上下文 token 估算上限，超过后自动压缩早期历史；`0` 表示关闭自动压缩 |
| `agent_keep_recent_messages` | `10` | 压缩上下文时保留的最近消息条数，工具调用及其配对的 `tool_calls` 消息会整组保留 |
| `agent_context_warning_ratio` | `0.8` | 上下文占用量超过上限该比例时，SSE 流式响应下发 `context_warning` 事件；`0` 表示关闭警告 |
| `agent_upload_dir` | `~/.akm/cache` | Agent 上传文件（图片）的保存目录，路径支持 `~` 展开 |
| `agent_workspace_root` | `""` | Agent 工作区沙箱根目录，文件工具（`akm_read_file` 等）仅能在此目录内读写；请求级 `workspace_root` 只能选择其子目录；留空则文件工具不可用 |
| `agent_write_tools_enabled` | `false` | 是否启用 Agent 写工具（`akm_write_file` / `akm_edit_file` / `akm_make_dir` / `akm_delete_file` / `akm_xlsx`），默认关闭需显式开启 |
| `agent_run_shell_enabled` | `false` | 是否启用 Agent shell 执行工具（`akm_run_shell`），默认关闭需显式开启；命令由模型直接传入、系统 shell 执行。**注意：这不是文件系统沙箱**，命令可访问工作区之外（如 `/etc`、家目录），仅以工作区为 cwd，启用前应确认调用方可信 |
| `agent_run_shell_sandbox` | `true` | `akm_run_shell` 默认用 macOS seatbelt 沙箱（`sandbox_init_with_parameters` + `preexec_fn`）隔离 shell 子进程：只读工作区与临时目录，全局禁写（仅放行工作区/TMP/`/dev`），并拒绝 `~/.ssh` / `~/.aws` / `~/.akm` / `~/Downloads` / `~/Documents` / `~/Desktop` / `~/Library` / 家目录根 dotfile（`.zshrc` / `.zprofile` / `.bash_profile` / `.bashrc` / `.zsh_history` / `.bash_history` / `.gitconfig` / `.git-credentials` / `.npmrc` / `.netrc`）/ `/etc` / `/private/etc` / `/var/log` / `/private/var/log` / `/var/db` / `/private/var/db` / `/tmp` / `/private/tmp` 等敏感路径，同时拒绝 `~` 的目录列举（`file-read-metadata`）；系统不支持该 API 时自动退回普通执行并记录警告。设为 `false` 可关闭隔离。注意：这是「限制敏感读写」级隔离，非真正的 chroot（网络、`/usr` 等仍可访问） |
| `agent_git_enabled` | `false` | 是否启用 Agent git 工具（`akm_run_git`，仅允许固定的结构化 operation），默认关闭需显式开启 |
| `agent_subagent_enabled` | `true` | 是否启用子 Agent 递归委托工具（`akm_subagent_spawn` / `akm_subagent_wait` / `akm_subagent_kill`），默认开启；子 Agent 通过子进程调用本机 `/v1/agent`，进程级隔离 |
| `agent_subagent_max_depth` | `1` | 子 Agent 最大嵌套层数：默认 1 表示主会话（depth 0）能开启子进程会话、子进程内（depth ≥ 1）不能再开下一级；调大后允许更深的多级递归委托（`akm_subagent_spawn` 在达到上限时返回错误而非报错） |
| `agent_email_enabled` | `false` | 是否启用 Agent 发邮件工具（`akm_send_email`），默认关闭需显式开启并配置 SMTP |
| `agent_notify_enabled` | `true` | 是否启用 Agent 原生通知工具（`akm_send_notification`），默认开启；通过菜单栏启动 AKM 时可弹出 macOS 系统通知 |
| `agent_native_tools_enabled` | `true` | 是否启用 Agent 原生系统工具（`akm_clipboard_get` / `akm_clipboard_set` / `akm_system_info` / `akm_open` / `akm_frontmost_app`），默认开启；直接调用本机剪贴板、系统信息与前台应用（pyobjc，不受 akm_run_shell 沙箱约束） |
| `agent_email_smtp_host` | `""` | SMTP 服务器地址（如 smtp.qq.com）；留空则 `akm_send_email` 不可用 |
| `agent_email_smtp_port` | `465` | SMTP 端口：465 走 SSL，587 走 STARTTLS（由 `agent_email_smtp_ssl` 决定是否用 SSL） |
| `agent_email_smtp_user` | `""` | SMTP 登录账号；`agent_email_from` 留空时默认作为发件人 |
| `agent_email_smtp_password` | `""` | SMTP 密码 / 授权码（敏感字段，`akm_get_config` 会脱敏，不返回明文） |
| `agent_email_from` | `""` | 默认发件人地址；留空则使用 SMTP 账号 |
| `agent_email_smtp_ssl` | `true` | 是否使用 SSL 加密连接（`true`=SMTP_SSL/465；`false`=STARTTLS/587） |
| `agent_max_tool_calls` | `30` | 单次 Agent 请求最多执行的工具调用数，超过上限的调用只返回错误，不会执行 |
| `agent_tool_retry_max_retries` | `1` | Agent 工具失败后的最大自愈修正轮次（服务端注入修正提示强制模型重试；`0` 关闭） |
| `agent_api_token` | `""` | `/v1/agent` 可选鉴权 token；留空不校验，配置后请求需带 `Authorization: Bearer <token>` 或 `X-Agent-Token` |
| `agent_default_instructions` | KaTeX 返回公式指令 | Agent 默认系统指令，客户端未传 `instructions` 时注入；默认要求数学公式以 KaTeX 语法返回 |
| `agent_session_auto_save` | `true` | 是否把 `/v1/agent` 会话自动落盘到 `~/.akm/agent_sessions/`，默认开启（请求结束时自动保存完整对话历史，供 `akm_load_session` / `akm_list_sessions` 串联回顾使用）；设为 `false` 则保持无状态、不写磁盘 |
| `tavily_api_key` | `""` | Tavily 联网搜索 API Key（Agent 内置 `tavily_search` 工具使用） |

## 请求格式

```json
{
  "model": "gpt-4o",
  "messages": [{"role": "user", "content": "帮我查一下今天的天气"}],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "查询指定城市的天气",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "城市名称"}
          },
          "required": ["city"]
        }
      }
    }
  ],
  "instructions": "你是 AKM 内置助手，请用中文回复",
  "api_path": "chat/completions",
  "max_turns": 20
}
```

## 参数说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `messages` | list | 是 | 对话历史（Chat 格式的 messages 数组） |
| `model` | string | 否 | 指定模型，为空时自动选择第一个可用 Key 的模型 |
| `tools` | list | 否 | 工具定义列表（OpenAI function calling 格式）。传入时**只注入本列表声明的工具**（内置工具如 `tavily_search`、`akm_search_kb` 不再自动注入，避免模型未经声明自主调用）；若名称与服务端已注册工具相同，服务端会覆盖客户端给出的 description 和 parameters；显式传空数组 `[]` 表示**不注入任何工具**；不传时注入除 `tavily_search` / `akm_generate_image` / `akm_edit_image` / `akm_write_file` / `akm_edit_file` / `akm_make_dir` / `akm_delete_file` / `akm_run_shell` / `akm_run_git` 外的全部内置工具（联网搜索、图片生成涉及外部服务调用，文件写操作、shell 执行与 git 操作涉及本机写入，需在 tools 中显式声明才能启用） |
| `instructions` | string | 否 | 系统级指令，注入到 messages 首条 system 消息；未传时使用 config.json 的 `agent_default_instructions`（默认要求数学公式以 KaTeX 语法返回）。其中 `{AKM_SOURCE_DIR}`、`{CURRENT_WORKING_DIRECTORY}`、`{USER_AGENTS_MD_PATH}`、`{USER_AGENTS_SKILLS_DIR}`、`{USER_PI_NPM_DIR}` 占位符在注入前自动替换为运行时实际路径（源码根目录、请求工作区、用户环境路径；用户路径不存在时替换为空字符串） |
| `api_path` | string | 否 | LLM 调用协议格式（默认 `chat/completions`，也支持 `responses` / `messages`） |
| `max_turns` | int | 否 | 最大迭代轮次（默认 20），防止工具调用无限循环 |
| `stream` | bool | 否 | 是否 SSE 流式返回（默认 `false`）；思考与正文均实时以 `reasoning_delta` / `model_delta` 推送，工具调用事件按上游输出顺序穿插，`final` 收尾（详见「SSE 流式事件」） |
| `workspace_root` | string | 否 | 本次请求的工作区根目录（绝对路径），只能指定为全局 `agent_workspace_root` 的子目录；不传或传空字符串时使用全局配置 |
| `session_id` | string | 否 | 会话 ID（可选，需配合 `stream=true` 使用）。传入时自动落盘会复用同名会话文件做**增量合并**：以磁盘上已有历史为基线，按消息内容去重追加本次新增消息，同一逻辑对话只保留一份完整历史，不会重复保存多个内容重叠的独立文件；不传则每次请求新建一个独立会话文件（默认行为） |

## 上下文压缩

长对话可能导致上下文超出模型窗口。Agent Loop 提供两层保障，均由 config.json 中的 `agent_config` 配置项控制（`agent_max_context_tokens` / `agent_keep_recent_messages` / `agent_context_warning_ratio` / `agent_max_tool_calls`，见「配置」章节）：

1. **自动压缩兜底**：每轮开始前估算上下文 token（CJK 字符按 1 token/字符，其余按 4 字符≈1 token，图片块固定估算），超过 `agent_max_context_tokens` 时把早期历史交给 LLM 总结为摘要并替换（保留最近 `agent_keep_recent_messages` 条消息与工具调用配对组），保证上下文不爆掉；摘要生成失败时降级为直接丢弃早期历史。
2. **AI 主动压缩**：模型可调用 `akm_context_status` 查询当前 token 占用、`akm_compact_context` 主动压缩早期历史。`akm_compact_context` 优先采用摘要替换，不丢失关键信息。`agent_context_warning_ratio` 触发的 `context_warning` SSE 事件即用于提示客户端 / 模型接近上限。

压缩只作用于早期历史，最近消息与所有工具调用配对始终完整保留；`final` / `error` / `context_warning` 事件的 `compacted` 字段表示本次运行累计压缩次数。

## 交互式澄清提问

默认编排中，Agent Loop 会在一轮请求内持续调用工具直到给出最终答案。当模型认为用户请求信息不完整、存在歧义、或缺少继续执行所需的关键参数时，可调用内置的 **`akm_ask_user`** 工具向用户提出澄清问题，本轮编排会**立即中断**，不再执行后续工具调用。该工具支持三种交互模式：

| 调用参数 | 交互模式 | 说明 |
|----------|----------|------|
| 仅 `question` | 输入框 | 用户以自由文本回答 |
| `question` + `options` | 单选 | 用户从候选列表中单选，`multiple` 缺省为 `false` |
| `question` + `options` + `multiple: true` | 多选 | 用户可从候选中多选 |

- **非流式**：响应体额外返回 `ask_user` 字段（`{"question": "...", "options": [...], "multiple": false}`），`final_message.content` 即模型想确认的问题，`messages` 中已把本轮 `akm_ask_user` 调用与其 `awaiting_user` 结果写入历史。
- **流式**（`stream: true`）：在 `turn_start` / `tool_call` / `tool_result` 事件之后下发 `ask_user` 事件（`data` 含 `question` / `options` / `multiple` / `messages` / `turns` / `usage`），随后本轮结束，不再下发 `final`。

客户端收到 `ask_user` 后应按 `multiple` / `options` 渲染输入框或选择控件，把问题展示给用户，等待用户回答；用户回答后，把上轮返回的 `messages` 追加一条新的 `user` 回答消息后重新请求 `/v1/agent`，模型即可结合上下文继续完成原本的任务。

```bash
# 第一轮：AI 提问澄清（单选模式）
curl -s http://127.0.0.1:8788/v1/agent \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"帮我查一下天气"}],"tools":[{"type":"function","function":{"name":"akm_ask_user","description":"向用户澄清","parameters":{"type":"object","properties":{"question":{"type":"string"},"options":{"type":"array","items":{"type":"string"}},"multiple":{"type":"boolean"}},"required":["question"]}}}]}'
# → {"ok":true,"ask_user":{"question":"你想查哪个城市的天气？","options":["北京","上海","广州"],"multiple":false},...}

# 第二轮：携带上下文继续（messages 为上轮返回，末尾追加用户回答）
curl -s http://127.0.0.1:8788/v1/agent \
  -H 'Content-Type: application/json' \
  -d '{"messages":[<上轮返回的 messages>...,{"role":"user","content":"北京"}]}'
```

## 文件上传

`/v1/agent` 支持 `multipart/form-data` 上传文件。`messages` 改为 JSON 字符串表单字段，`tools` 等其余字段同纯 JSON 方式；`files` 字段可携带多个文件，但单次最多 8 个、总大小最多 20MB。上传的文件会被读取并作为独立的 user 消息追加到对话末尾：图片（`image/*`）转成 base64 的 `image_url` 内容块，其他文件按 UTF-8 读取为文本内容，无法解码的二进制文件会返回 400。纯 JSON 请求方式保持不变。

上传的图片还会同时落盘到 `agent_upload_dir` 配置的目录（默认 `~/.akm/cache`，可通过 `~/.akm/config.json` 修改，支持 `~` 展开；文件名为随机 UUID），并在追加的 user 消息文本中给出绝对路径提示。模型可据此调用 `akm_edit_image` 传入 `image_path` 编辑该图片。该目录不会自动清理，请根据运行环境定期清理。

```bash
curl -X POST http://127.0.0.1:8788/v1/agent \
  -F 'messages=[{"role":"user","content":"请分析这个文件"}]' \
  -F 'model=gpt-4o' \
  -F 'files=@./report.txt'
```

流式 `final` 事件中的 `final_message` 会保留上游 Chat 响应的 `reasoning_content`，以便客户端展示完成后的推理内容。

## 工作区文件工具与安全边界

工作区文件工具受 `agent_workspace_root` 沙箱与配置开关约束（`agent_workspace_root` / `agent_write_tools_enabled` / `agent_run_shell_enabled` / `agent_git_enabled` / `agent_max_tool_calls` / `agent_api_token` 见「配置」章节）。shell 是单独开启的主机级执行能力，`cwd` 不能提供文件系统隔离。

### 读工具（默认可用）

配置 `agent_workspace_root` 后，以下只读工具始终注册：`akm_read_file`、`akm_list_dir`、`akm_glob`、`akm_grep`、`akm_file_info`。它们只读取工作区内的文件，路径越界访问直接返回错误。

### 写工具、shell 与 git（默认禁用）

写工具、shell 与 git 工具默认**不注册**，即使配置了 `agent_workspace_root`，模型也看不到这些工具。需要显式开启对应配置开关并在请求 `tools` 中显式声明才会启用。这是默认只读的安全设计：文件写操作、shell 执行与 git 操作会改动本机状态，应经人工确认后再开放。

`akm_run_shell` 接受 `command` 字符串参数。模型可直接传入任意 shell 命令，服务端用系统 shell 解释执行（支持管道、通配符、重定向），以当前工作区作为 cwd；执行受超时（1–300 秒，默认 60）与输出大小（60KB）限制。这是显式开启的主机级进程执行能力，`cwd` 不能提供文件系统隔离，管理员应结合 `tool_policy_guard` 等插件策略约束调用内容。默认开启的 `agent_run_shell_sandbox` 用 macOS seatbelt 沙箱隔离 shell 子进程（只读工作区与临时目录 + 全局禁写 + 拒绝 `~/.ssh` / `~/.aws` / `~/.akm` / `~/Library` / 家目录根 dotfile（`.zshrc` / `.zsh_history` / `.gitconfig` / `.npmrc` 等）/ `/etc` / `/private/etc` / `/var/log` / `/var/db` / `/tmp` 等敏感路径与家目录列举），将越界风险从「无限制」降到「限制敏感读写」级别。此外，父进程若携带 `PYTHONHOME`/`PYTHONPATH`（py2app 打包的 app 内嵌 Python 运行时设置），执行前会被剥离，避免污染 shell 里外部 `python3` 使其启动即崩溃。

`akm_run_git` 不接受 `command` 参数，只支持 `status`、`diff`、`log`、`show`、`add`、`restore`、`reset`、`commit`、`branch`。模型以 `operation` 调用；涉及文件的操作传相对 `paths`，`commit` 必须传 `message`。可选 `dir` 参数指定 git 仓库目录（等价于 `git -C <dir>`，默认工作区根）：接受工作区内的相对或绝对路径（含根目录），用于切换不同仓库；越界（工作区外）会返回错误。`paths` 为相对 `dir` 的路径。

`akm_xlsx` 通过 `action` 区分创建与修改，基于 `openpyxl`：

- `create`：`data` 传纯二维数组（写入默认 `Sheet1`）或 `{"工作表名": [[...]]}` 映射（每个工作表写入对应数组）；目标文件已存在时需 `overwrite=true`，否则返回错误。
- `edit`：`updates` 传 `[{"sheet": "Sheet1", "cell": "B2", "value": 42}]` 列表，按坐标写入已有文件的单元格；`sheet` 缺省为 `Sheet1`，目标工作表不存在时自动创建。

两种模式共用以下可选自定义参数（均为单工作表时可直接传，多工作表时以 `{"工作表名": 配置}` 映射）：

- `styles`：单元格样式数组 `[{"sheet"?, "cell", "bold"?, "italic"?, "size"?, "color"?, "fill"?, "align"?, "number_format"?}]`。`color`/`fill` 为十六进制色值（如 `FF0000`），`align` 可传 `left`/`center`/`right`/`fill`/`justify`/`center_continuous`/`distributed` 之一。
- `column_widths` / `row_heights`：列宽/行高映射，如 `{"A": 25}` / `{1: 30}`，键分别为列名（`A`）与行号（`1`）。
- `merge_cells`：合并单元格区间列表，如 `["A1:C1"]`。
- `freeze_panes`：冻结窗格坐标，如 `"A2"`（冻结首行）。
- `charts`：图表数组 `[{"sheet"?, "type", "title"?, "data_range", "categories_range"?, "x_title"?, "y_title"?, "anchor"?, "legend"?}]`。`type` 支持 `bar`/`line`/`pie`/`scatter`/`area`/`doughnut`，`data_range`/`categories_range` 为单元格区间（如 `"B2:B3"`），`anchor` 缺省 `F2`。

`data`/`updates` 中值以 `=` 开头的字符串会按公式写入（如 `"=SUM(A2:A3)"`）。

两种模式都以工作区为沙箱，路径越界返回「超出工作区范围」错误。

### 路径沙箱

所有文件工具的文件路径（相对/绝对路径）都会先解析，并校验解析结果必须位于 `agent_workspace_root` 目录内：

- 绝对路径越界（如 `/etc/passwd`）被拒绝；
- 相对路径中的 `..` 穿越工作区被拒绝；
- 软链接指向工作区外时（resolve 后越界）被拒绝。

越界访问统一返回「超出工作区范围」错误，不会读写工作区之外的任何文件；请求级 `workspace_root` 只能选择全局工作区的子目录。`akm_edit_image` 的本地图片和蒙版仅允许从工作区或 `agent_upload_dir` 读取，单个文件或 Base64 解码后的大小最多 20MB。`akm_run_shell` 以工作区目录为 cwd 用系统 shell 执行命令并受 `_WORKSPACE_SHELL_TIMEOUT_SEC`（默认 60 秒）超时保护，但它不是文件系统沙箱，启用前应确认调用方可信；默认开启的 `agent_run_shell_sandbox` 用 macOS seatbelt 对 shell 子进程做「限制敏感读写」级隔离。`akm_run_git` 只构造固定 operation 对应的 argv，`dir` 参数限定的仓库目录必须在工作区内。`akm_read_file` 单次最多读取 60000 字节，`akm_grep` 最多返回 100 条命中，目录列表最多返回 500 条，避免工具结果撑爆上下文。

## 自愈重试

工具调用失败时（工具返回含 `error` 字段的结果，如路径越界、文件未找到、git 命令执行失败等），Agent Loop 会**注入一条 `system` 修正提示**，强制模型基于错误信息修正工具参数后重新调用，避免模型收到失败结果后敷衍了事或死循环。重试同样占用轮次，超过上限后错误结果照常回传，由模型自主决定。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `agent_tool_retry_max_retries` | `1` | 工具失败后的最大自愈修正轮次；`0` 关闭（失败结果直接回传，模型自行决定是否修正） |

SSE 流式模式下，每次注入修正提示前会先下发 `tool_retry` 事件（`data` 含 `turn` / `retry_count` / `max_retries` / `error`），便于客户端感知服务端已介入重试。

### 鉴权

`agent_api_token` 为可选鉴权：留空时 `/v1/agent` 不校验 token（含写/shell/git 已开启的场景，危险工具仍由各自配置开关控制是否注册）。配置了 token 后，所有请求（含纯 JSON 与 multipart）必须携带 `Authorization: Bearer <token>` 或 `X-Agent-Token` 头，否则返回 `401`。

### 管理接口：`GET /api/agent-tools`

  只读返回当前服务已注册的 Agent 工具名称列表（`{"data": ["akm_read_file", ...]}`）。接口位于管理端点，不经过 `/v1/agent` 的鉴权与工具注入逻辑。

### 定时任务：`/v1/tasks`

服务内置一个通用的后台定时任务系统，任务持久化在 `scheduled_tasks` 表中，由独立于用量查询调度器的 `TaskScheduler` 周期扫描执行（扫描间隔取配置 `task_check_interval_sec`，默认 5 秒，最小 5 秒）。鉴权与 `/v1/agent` 一致（`agent_api_token` 留空时免鉴权，配置后需 `Authorization: Bearer` 或 `X-Agent-Token`）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/tasks` | 任务列表，可选 `task_type` / `enabled=1` 过滤 |
| POST | `/v1/tasks` | 创建任务，返回 `201` 与完整记录 |
| GET | `/v1/tasks/{id}` | 查询单条任务 |
| PUT | `/v1/tasks/{id}` | 更新任务字段 |
| DELETE | `/v1/tasks/{id}` | 删除任务 |
| POST | `/v1/tasks/{id}/run` | 绕过调度器立即执行一次（不改变 `last_run_at` / `next_run_at`） |

创建请求体：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | str | 是 | 任务名称 |
| `task_type` | str | 是 | `agent_call`（调 Agent Loop 跑一轮对话）或 `usage_query`（对指定 alias 执行用量查询） |
| `payload` | dict | 否 | 任务参数，见下 |
| `interval_sec` | int | 否 | 循环间隔（秒）；`0` 表示单次任务，执行后自动禁用 |
| `cron` | str | 否 | cron 时间表达式（标准 5 段：分 时 日 月 周，支持 `*` `/` `,` `-`）；提供时优先于 `interval_sec`，按表达式计算下一次执行时间；非法表达式返回 `400` |
| `enabled` | bool | 否 | 默认 `true`；创建时 `interval_sec=0` 且无 `cron` 的任务 `next_run_at` 即为当前时间，下一轮扫描即可执行 |

按类型的 `payload`：

- **`agent_call`**：`messages`（必填，对话历史，与 `/v1/agent` 一致）；可选 `model` / `tools` / `instructions` / `max_turns` / `api_path` / `workspace_root`。执行结果摘要会写回 `payload.last_result`（含 `ok` / `final_message`）供事后追溯。
- **`usage_query`**：`alias`（必填，对应 Key 的 alias）；执行该 Key 配置的用量查询脚本并写入 `usage_data`。

执行后调度器更新 `last_run_at`；提供 `cron` 的任务按下一次 cron 时刻滚动 `next_run_at`，循环任务按 `interval_sec` 滚动排下一次 `next_run_at`，单次任务清空 `next_run_at` 并自动禁用。

### 工作流引擎：`/v1/flow`

服务内置一个 DAG 工作流引擎（实现位于 `akm/flow/`），把「需求 → 方案 → 编码 → 审查 → 测试 → 交付」拆成有向无环图：节点为步骤（`intake` / `plan` / `code` / `review` / `test` / `fix` / `human` / `router` / `merge` / `output`），边上的 `condition` 决定分支、`loop` 边支持按预算重入，多路并行节点同时执行、全部前驱完成后汇聚（fan-in）。节点输出以 `artifacts` 累积，下游模板用 `{{artifacts.xxx}}` 引用；LLM 调用复用 AKM 代理网关。鉴权与 `/v1/agent` 一致。

工作流的 HTTP 接口、内置模板、运行变量、节点能力（LLM / 编码 / 人工审批 / worktree / retry）、pi-agent 定位、`agent_flow` 配置组与内置工具，详见 [`akm/flow/flow.md`](../flow/flow.md)。

## 用量统计

内置的 `akm_get_usage_stats` 工具复用服务端 `_get_stats`（与 `/api/stats` 同源），查询审计日志中的 Token 用量汇总。默认同时返回最近 1 / 7 / 30 三个自然日窗口；也可指定单个窗口。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `days` | int | 否 | `1` / `7` / `30` 只返回该窗口；`0` 或省略时返回三个窗口 |

返回结构要点：

- `windows`：按窗口天数键（`"1"` / `"7"` / `"30"`）的用量摘要，含请求数、prompt / completion / total / cached tokens，以及 `by_model` / `by_provider` / `by_key` 分桶
- `cost_stats_enabled`：当前是否开启费用估算
- 当 `cost_stats_enabled=true` 时额外返回：
  - 各窗口的 `total_cost` / `cost_currency`，以及分桶上的 `cost`
  - `pricing`：当前 `cost_pricing_table` 解析后的模型单价表（`input_per_1m` / `cache_per_1m` / `output_per_1m`，单位 USD per 1M tokens）
  - `pricing_unit` 与 `cost_note`（费用为本地估算，不能替代供应商账单）
- 费用未开启时不返回 `pricing` / `total_cost`，仅在 `cost_note` 中提示如何开启

## 联网搜索

内置的 `tavily_search` 工具通过 Tavily 官方远程 MCP 端点提供实时联网搜索。使用前需要在 `~/.akm/config.json` 中配置 API Key：

```json
{
  "tavily_api_key": "tvly-xxxxxxxx"
}
```

配置后 Agent 即可调用 `tavily_search`，参数如下：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 搜索关键词 |
| `max_results` | int | 否 | 返回结果数量（1-20，默认 5） |
| `search_depth` | string | 否 | 搜索深度 `basic` 或 `advanced`（默认 `basic`） |

请求会经 AKM 的按路由隔离 HTTP 连接池发往 `https://mcp.tavily.com/mcp/`（Key 通过查询参数注入），遵循出站代理设置。未配置 `tavily_api_key` 时工具调用会返回明确错误提示。

## 知识库查询（markdown-kb）

内置的 `akm_search_kb` 工具通过本机 `POST /api/markdown-kb/query` 接口检索 `markdown_kb` 插件索引的 Markdown 知识库，让 Agent 可以主动查询项目文档。工具经本机 HTTP 调用，不依赖插件内部实现。参数如下：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `question` | string | 是 | 检索关键词 / 问题描述 |
| `top_k` | int | 否 | 返回的检索结果数量（1-20，默认 5） |
| `embedding_model` | string | 否 | 指定向量模型，默认取插件配置 |
| `reranker_model` | string | 否 | 指定重排模型，默认取插件配置 |

返回结果为命中文档片段列表（每项含标题、文件名、相关度分数与截断后的内容片段），避免全文撑爆上下文。Agent 请求存在有效工作区时，检索范围固定为当前请求工作区，模型不能通过工具参数指定其他工作区；未配置工作区时仍检索公共索引。知识库未初始化或未命中时返回明确提示。该工具的 HTTP 端点同时提供 MCP 访问方式（见 `plugins/markdown_kb/README.md`）。

## 图片生成

内置的 `akm_generate_image` 工具复用 `/v1/images/generations` 的转发链路，让 Agent 可以直接生成图片。返回结果只含图片 URL（或 `b64_json_hint` 提示），避免体积巨大的 base64 数据占用模型上下文。参数如下：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `prompt` | string | 是 | 图片描述提示词 |
| `model` | string | 否 | 图片生成模型，默认取 `image_supported_models` 配置首项 |
| `size` | string | 否 | 图片尺寸，如 `1024x1024` |
| `quality` | string | 否 | 生成质量，如 `standard` 或 `hd` |
| `n` | int | 否 | 生成张数（默认 1） |

请求经连接池调用 `forward_request`（`api_path=images/generations`），自动复用 Key 选择、故障切换与图片专用超时（`image_request_timeout_sec`，默认 300 秒）。调用失败时会返回明确错误文本；上游只返回 `b64_json` 时，服务端会落盘并只回传数据长度提示和资源路径，不会将完整 Base64 数据回灌模型上下文。

生成成功后，每张图片还会下载保存到 `agent_upload_dir`（默认 `~/.akm/cache`），并在结果项中附带两个资源字段：

- `local_path`：图片在本机的绝对路径，可供 `akm_edit_image` 等按路径读取的工具使用；
- `http_url`：`http://127.0.0.1:{port}/agent-uploads/{filename}` 形式的访问地址，可直接用于前端展示或下载。

若下载或保存失败，结果项会附带 `save_error` 说明原因，且不影响 `url` 等主结果字段。

### 图片编辑

内置的 `akm_edit_image` 工具复用 `/v1/images/edits` 的 multipart 纯透传链路。图片有两种来源：`image_path`（仅限工作区或 `agent_upload_dir` 内的本地绝对路径），或 `image_base64`（图片 base64 数据，可直接使用对话中图片的 `data:image/...;base64,` 前缀 data URL，适用于本地无文件的云端场景）。工具按与 `/v1/images/edits` 一致的 multipart 结构组装请求，`image` 与可选的 `mask` 作为文件字段上传。参数如下：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `image_path` | string | 否* | 工作区或 `agent_upload_dir` 内图片文件的绝对路径，与 `image_base64` 二选一。若该路径不存在，会提取文件名回退到 `agent_upload_dir`（默认 `~/.akm/cache`）下查找，兼容把 `/agent-uploads/` 访问地址误当成本地路径的传法 |
| `image_base64` | string | 否* | 图片 base64 数据（可带 `data:image/...;base64,` 前缀，或纯 base64），与 `image_path` 二选一，同时提供时优先 |
| `prompt` | string | 是 | 编辑指令，描述期望的修改效果 |
| `model` | string | 否 | 图片编辑模型，默认取 `image_supported_models` 配置首项 |
| `mask_path` | string | 否 | 本地蒙版图片路径，用于限定重绘区域，与 `mask_base64` 二选一 |
| `mask_base64` | string | 否 | 蒙版图片 base64 数据，与 `mask_path` 二选一，同时提供时优先 |
| `size` | string | 否 | 输出图片尺寸，如 `1024x1024` |
| `quality` | string | 否 | 生成质量，如 `standard` 或 `hd` |
| `output_format` | string | 否 | 输出格式，如 `png` 或 `jpeg` |
| `n` | int | 否 | 生成张数（默认 1） |

*`image_path` 与 `image_base64` 至少提供其一，否则返回错误。

图片文件不存在、base64 解码失败或两种来源都未提供时，会返回明确错误提示；其余失败（上游错误、无法解析响应等）与 `akm_generate_image` 行为一致。编辑结果与生成结果一样，会附带 `local_path`、`http_url`（`/agent-uploads/...`）资源字段，保存失败时附 `save_error`。

### 上传目录的 HTTP 访问

服务端提供 `GET /agent-uploads/{filename}` 端点，按文件名提供 `agent_upload_dir` 目录下文件的访问（文件名仅接受单段安全名称，防止路径穿越）。生成、编辑工具返回的 `http_url` 即指向该端点，前端或其他客户端可通过 `http://127.0.0.1:{port}/agent-uploads/{filename}` 直接获取图片。

## 响应格式

```json
{
  "ok": true,
  "final_message": {
    "role": "assistant",
    "content": "今天北京的天气是晴天，气温 25°C。"
  },
  "messages": [...],
  "turns": 2,
  "compacted": 1,
  "error": "",
  "usage": {
    "prompt_tokens": 1200,
    "completion_tokens": 350,
    "total_tokens": 1550
  }
}
```

## SSE 流式事件（`stream: true`）

事件顺序约定：**思考（`reasoning_delta`）与正文（`model_delta`）按模型输出顺序实时流式下发**，工具轮正文同样实时流出，工具事件（`turn_start`/`tool_call`/`tool_result`）随后出现，最终以 `final` 收尾。

| 事件 | 说明 |
|------|------|
| `reasoning_delta` | LLM 思考（推理）过程片段，`data.turn` 为当前轮次，`data.content` 为增量内容；实时下发，先于同段正文 |
| `model_delta` | 可见正文片段，`data.turn` 为当前轮次，`data.content` 为增量内容；实时下发（含工具轮过程性正文与最终主体内容） |
| `context_warning` | 上下文占用接近上限（估算 token 超过 `agent_max_context_tokens` × `agent_context_warning_ratio`）时下发，`data` 含 `estimated_tokens` / `max_tokens` / `remaining_tokens` / `ratio` / `compacted`，供客户端提前感知并提示 AI 调用 `akm_compact_context` 主动压缩 |
| `turn_start` | 新一轮开始，`data.turn` 为当前轮次 |
| `tool_call` | LLM 请求调用工具，`data.name` / `data.arguments` |
| `tool_result` | 工具执行结果，`data.name` / `data.result` |
| `tool_retry` | 工具调用失败触发自愈重试（`agent_tool_retry_max_retries` > 0 时），`data` 含 `turn` / `retry_count` / `max_retries` / `error`；随后服务端注入 `system` 修正提示并强制模型修正参数后重新调用 |
| `ask_user` | AI 调用 `akm_ask_user` 向用户澄清提问，本轮中断；`data` 含 `question` / `options` / `multiple`（`options` 为空数组表示自由文本回答，非空则单选或多选）/ `messages`（含本轮调用与 `awaiting_user` 结果，供续跑）/ `turns` / `usage`；随后本轮结束，不再下发 `final`，客户端展示问题与选择控件、用户回答后携带 messages 续跑 |
| `cancelled` | 手动中断（客户端通过 AbortController 取消流式请求断开连接，服务端主动检测到断连），`data` 含 `turns` / `usage` / `compacted`；随后流结束，不再下发 `final` |
| `final` | Agent 完成，含 `data.final_message` / `data.turns` / `data.usage` / `data.compacted` |
| `error` | 错误终止，含 `data.error` / `data.turns` / `data.usage` / `data.compacted` |

> **断连取消**：客户端通过 AbortController 取消流式请求时底层 TCP 连接断开，服务端后台轮询检测到断连后在关键 await 点（每轮开始、读取上游 LLM 流前、工具执行前）检查并提前退出，下发 `cancelled` 事件，停止继续消费上游 LLM 流，避免「客户端已停止但仍烧 token」。工具轮中已流出的 `tool_call` 事件会保留，但对应工具不再执行。

```json
// SSE 示例（工具轮：思考与正文实时下发，工具事件随后，最终主体实时流式）
data: {"event":"reasoning_delta","data":{"turn":1,"content":"用户想查天气"}}

data: {"event":"model_delta","data":{"turn":1,"content":"我来查询"}}

data: {"event":"turn_start","data":{"turn":1}}

data: {"event":"tool_call","data":{"name":"get_weather","arguments":{"city":"beijing"}}}

data: {"event":"tool_result","data":{"name":"get_weather","result":"{\"city\":\"beijing\",\"temp\":25}"}}

data: {"event":"model_delta","data":{"turn":2,"content":"北京今天..."}}

data: {"event":"final","data":{"final_message":{"role":"assistant","content":"北京今天..."},"turns":2,"usage":{...}}}
```
