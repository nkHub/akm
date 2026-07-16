# markdown_kb 客户端 Hook 学习入库设计

日期：2026-06-23
状态：设计中

## 0. 当前实现进度（2026-06-24）

这份文档当前同时承担“设计稿 + 实现接力记录”的作用，便于后续会话直接续做。

### 0.1 已完成

1. 本设计稿已单独提交：
   - commit：`e89067e`
   - message：`docs: add markdown kb hook learn design`
2. 已确认第一版范围仍按本文执行：
   - 正式支持 `Codex` 与 `Claude Code`
   - `OpenCode` 暂不纳入第一版正式支持
   - 关键词只在最后一行精确匹配
   - 命中后必须在客户端转发前删除关键词行
3. 当前本轮实现已覆盖服务端、客户端公共适配层以及源码态接线示例，当前已修改但**尚未提交**的文件：
   - `plugins/markdown_kb/index.py`
   - `akm/markdown_kb_hook.py`
   - `akm/cli.py`
   - `tests/test_server.py`
   - `tests/test_markdown_kb_hook.py`
   - `tests/test_markdown_kb_hook_wrappers.py`
   - `.codex/hooks.json`
   - `.codex/hooks/*.py`
   - `.claude/settings.local.json`
   - `.claude/hooks/*.py`

### 0.2 已落地到代码的内容

当前代码里已经加入了 `/api/markdown-kb/learn` 的第一版最小闭环，核心包括：

1. 新增路由：
   - `POST /api/markdown-kb/learn`
2. `markdown_kb` 插件内部新增学习入库主流程：
   - 校验 `source / trigger_phase / session_id / dedupe_key`
   - 只允许 `source in {codex, claude_code}`
   - 只允许 `trigger_phase in {stop, pre_compact}`
3. 新增幂等记录文件：
   - `~/.akm/markdown_kb/learn_records.json`
   - 用 `dedupe_key` 防止 `Stop` 与 `PreCompact` 重复入库
4. 服务端会调用本地 AKM chat 接口，把候选材料归纳成结构化 JSON：
   - `should_learn`
   - `title`
   - `summary_markdown`
   - `quotes`
5. 若 `should_learn=false` 或没有有效摘要，则返回：
   - `ok=true`
   - `ignored=true`
   - 不落盘
6. 若可沉淀，则会：
   - 生成 `.learn.md` 文档
   - 统一包裹元信息壳子
   - 复用现有 manifest + workspace 绑定落盘链路
   - 继续调用 `rebuild_file` 刷新索引
7. 已特别保持以下约束：
   - 客户端触发关键词不会被写入最终知识文档
   - 第一版只追加新文档，不修改既有知识文档

此外，客户端公共适配逻辑也已经落到代码里，当前包括：

1. 新增公共适配模块：
   - `akm/markdown_kb_hook.py`
2. 新增 CLI 入口：
   - `akm markdown-kb-hook prompt-submit`
   - `akm markdown-kb-hook stop`
   - `akm markdown-kb-hook pre-compact`
3. 当前已覆盖的客户端公共行为：
   - 最后一行关键词精确匹配
   - 命中后剥离关键词行并返回净化后的 prompt
   - 本地 pending 状态落盘
   - `Stop` 失败后由 `PreCompact` 复用同一 `dedupe_key` 补偿调用
4. 当前仓库已附带源码态联调接线示例：
   - `.codex/hooks.json`
   - `.codex/hooks/*.py`
   - `.claude/settings.local.json`
   - `.claude/hooks/*.py`
   - 对应 wrapper 也已经补了最小行为测试：
     - `tests/test_markdown_kb_hook_wrappers.py`

### 0.3 已确认通过的测试

当前已新增并确认通过两组测试：

1. `test_markdown_kb_learn_api_creates_workspace_bound_doc_and_dedupes`
   - 验证 `/learn` 能落盘
   - 验证生成文档进入索引
   - 验证 workspace 绑定生效
   - 验证同一 `dedupe_key` 重复触发时不会重复入库
2. `test_markdown_kb_learn_ignored_when_model_says_no_stable_knowledge`
   - 验证模型返回“无需沉淀”时不会写文档
   - 验证会写入 ignored 幂等记录

另外客户端公共适配层也已经新增并确认通过以下测试：

1. `test_split_prompt_by_learn_keyword_only_matches_last_line_exactly`
   - 验证只在最后一行精确匹配关键词时触发
2. `test_register_prompt_submit_writes_pending_and_reuses_codex_turn_id`
   - 验证 `UserPromptSubmit` 会写入 pending，并按 `Codex` 的 `session + turn` 生成 dedupe_key
3. `test_trigger_pending_learn_stop_success_marks_record_completed`
   - 验证 `Stop` 成功调用 `/learn` 后会把 pending 标记为 completed
4. `test_trigger_pending_learn_precompact_retries_after_stop_failure`
   - 验证 `Stop` 失败后 `PreCompact` 会复用同一 dedupe_key 补偿提交
5. `test_markdown_kb_hook_cli_prompt_submit_returns_sanitized_prompt`
   - 验证 CLI 入口会输出机器可消费 JSON，方便直接接到客户端 Hook

### 0.4 本轮已确认的问题与修正

首次跑新增测试时，失败原因已经定位，不是 `/learn` 路由缺失，而是测试里：

1. 把 `httpx.AsyncClient.post` 全局 monkeypatch 成了假上游；
2. 导致测试自己发给 FastAPI 的 `client.post("/api/markdown-kb/learn")` 也被同一个 patch 拦截；
3. 因此返回了伪造的 `404`，不是服务端真实行为。

这个测试问题已经在本地修正为：

1. 测试请求改走 `client.request("POST", ...)`
2. 这样测试侧 HTTP 调用不会误吃到对上游模型请求的 monkeypatch

### 0.5 当前仍未完成

当前这一轮已完成的收尾包括：

1. 新增 `/learn` 测试已通过
2. 客户端公共适配层测试已通过
3. 已回归一组现有 `markdown_kb` 主流程测试，确认没有行为回归
4. 已同步更新：
   - `README.md`
   - `docs/design/plugin-system.md`
   - `docs/design/markdown-kb-plugin.md`
   - `docs/superpowers/specs/2026-06-23-markdown-kb-client-hook-learn-design.md`

当前仍待继续的范围主要在客户端侧：

1. 在真实 `Codex` / `Claude Code` 会话里做端到端联调
2. 验证不同客户端事件字段是否需要再补兼容映射
3. 若坚持“关键词绝不进入模型”，则仍需额外引入 launcher / wrapper 层实现真正 prompt 改写

## 1. 背景

当前项目已经具备两类基础能力：

1. `markdown_kb` 可以在请求进入 AKM 后自动检索知识库，并把命中的参考资料注入到模型请求中。
2. AKM 本身具备插件路由、知识文档落盘、workspace 绑定、索引重建与增量同步能力。

现在需要补的是反向链路：当用户在客户端明确表达“这一轮值得沉淀”时，把当前轮次的有效内容提炼成知识条目，再自动写回 `markdown_kb`。

第一版以 `Claude Code` 和 `Codex` 为正式支持对象。`OpenCode` 不纳入第一版正式范围，只在接口层保留后续兼容空间。

## 2. 目标

第一版目标如下：

1. 支持在 `Claude Code` 和 `Codex` 中通过客户端 Hook 主动触发知识沉淀。
2. 触发标记仅在用户输入最后一行出现时生效，避免误触发。
3. 客户端 Hook 只做“轻整理”，不承担最终知识总结职责。
4. AKM 统一提供 `/api/markdown-kb/learn` 接口，负责最终知识归纳、落盘、绑定 workspace 和刷新索引。
5. 允许知识条目中保留少量原话片段，便于后续回溯。
6. 即使 `Stop` 与 `PreCompact` 同时触发，也只能入库一次。
7. 学习入库失败不能影响主会话继续结束，也不能反向阻塞客户端正常使用。

## 3. 非目标

第一版明确不做以下事项：

1. 不正式支持 `OpenCode` 的自动触发接入。
2. 不在 AKM 服务端直接解析 `Claude Code` 或 `Codex` 的 transcript 文件格式。
3. 不自动修改既有知识文档，只追加新的学习文档。
4. 不为不同需求类型建立多套结构化模板，默认采用“AI 自由归纳”。
5. 不把学习入库做成主会话的强依赖，不因为入库失败而中断客户端流程。
6. 不在第一版引入复杂的知识合并、聚类、冲突解决或知识版本管理。

## 4. 触发方式

### 4.1 触发原则

第一版采用“客户端 Hook 检测关键词，AKM 统一入库”的双层方案。

触发条件：

1. 用户输入最后一行是预设关键词。
2. 只有最后一行做精确匹配时才触发。
3. 命中后，这一行关键词不应进入最终知识内容，也不应继续污染后续模型上下文。
4. 命中后，客户端在请求继续发给模型之前，就应删除最后一行关键词，只把净化后的正文继续转发。

### 4.2 关键词策略

关键词本身不使用 slash command，也不使用 `skill`、`mcp`、`plugin`、`tool` 这类容易和客户端保留语义冲突的词。

第一版建议允许通过客户端本地配置指定一组候选关键词，默认只启用一条，例如：

`AKM入库`

匹配规则：

1. 只检查最后一行。
2. 仅允许去掉首尾空白后的精确匹配。
3. 不支持任意位置模糊包含。
4. 不支持一行中同时夹带业务正文和触发词。

## 5. 整体架构

整体链路如下：

1. 用户在 `Claude Code` 或 `Codex` 中发起一轮对话。
2. `UserPromptSubmit` Hook 检查最后一行是否为学习关键词。
3. 如果命中，客户端在本地写入一条 pending 学习状态，并保存最基础的本轮信息。
4. 当该轮次结束时，由 `Stop` Hook 优先触发学习请求。
5. 如果 `Stop` 没有成功提交学习请求，而会话又即将压缩，则由 `PreCompact` Hook 再兜底触发一次。
6. 客户端 Hook 只把本轮候选材料发送到 AKM 的 `/api/markdown-kb/learn`。
7. AKM 使用统一总结链路，把候选材料归纳为 Markdown 知识条目。
8. AKM 把知识条目写入 `markdown_kb` 原始文档目录，绑定 `workspace_root`，并执行索引同步。

职责边界：

1. 客户端负责“什么时候触发”和“拿哪些候选材料”。
2. AKM 负责“如何总结”和“如何入库”。

## 6. 客户端 Hook 设计

### 6.1 统一原则

虽然 `Claude Code` 和 `Codex` 的 Hook 字段略有差异，但第一版统一要求客户端适配脚本完成以下动作：

1. 检测是否命中学习关键词。
2. 去掉最后一行关键词，得到净化后的用户输入。
3. 记录本地 pending 状态。
4. 在 `Stop` 或 `PreCompact` 触发时组装统一学习请求。
5. 调用 AKM 的 `/api/markdown-kb/learn`。

### 6.2 `UserPromptSubmit`

在 `UserPromptSubmit` 阶段，客户端适配脚本执行：

1. 读取当前 prompt。
2. 判断最后一行是否为学习关键词。
3. 如果未命中，则直接退出。
4. 如果命中，则先删除最后一行关键词，得到净化后的用户输入。
5. 如果命中，则生成或更新 pending 学习状态，至少保存：
   - `source`
   - `session_id`
   - `workspace_root` 或 `cwd`
   - 去掉关键词后的 `user_prompt`
   - 初始 `dedupe_key`
6. 将去掉关键词后的净化正文继续发给模型，而不是把原始带关键词的文本继续转发。

此阶段不直接调用 AKM，但必须完成“关键词剥离后再继续转发”这一动作。

### 6.3 `Stop`

`Stop` 是第一优先触发点。

在 `Stop` 阶段，客户端适配脚本执行：

1. 检查当前 session 是否存在未处理的 pending 学习状态。
2. 如果不存在，则直接退出。
3. 如果存在，则读取本轮可用的 assistant 输出与少量上下文。
4. 组装学习请求并调用 `/learn`。
5. 调用成功后，将本地 pending 状态标记为已处理。

### 6.4 `PreCompact`

`PreCompact` 是兜底触发点，不是主路径。

在 `PreCompact` 阶段，客户端适配脚本执行：

1. 检查当前 session 是否仍有未处理的 pending 学习状态。
2. 只有在 `Stop` 未成功处理时，才允许 `PreCompact` 再尝试调用 `/learn`。
3. `PreCompact` 与 `Stop` 必须使用同一个 `dedupe_key`，确保 AKM 侧只能真正入库一次。

## 7. `/api/markdown-kb/learn` 协议

### 7.1 请求体

第一版建议采用如下最小协议：

```json
{
  "source": "codex",
  "trigger_phase": "stop",
  "session_id": "sess_123",
  "turn_id": "turn_456",
  "workspace_root": "/Users/nk/Desktop/ccs",
  "title_hint": "订单退款逻辑排查",
  "user_prompt": "请帮我排查退款页为什么会重复提交",
  "assistant_excerpt": "已经定位到重复绑定 submit 事件...",
  "conversation_excerpt": [
    {
      "role": "user",
      "text": "请帮我排查退款页为什么会重复提交"
    },
    {
      "role": "assistant",
      "text": "已经定位到重复绑定 submit 事件..."
    }
  ],
  "learn_keyword": "AKM入库",
  "dedupe_key": "codex:sess_123:turn_456"
}
```

### 7.2 字段要求

第一版字段语义如下：

1. `source`
   只允许 `claude_code` 或 `codex`。
2. `trigger_phase`
   只允许 `stop` 或 `pre_compact`。
3. `session_id`
   用于关联客户端会话。
4. `turn_id`
   `Codex` 有则传，`Claude Code` 没有可以省略。
5. `workspace_root`
   用于知识文档的 workspace 绑定。
6. `title_hint`
   只是标题提示，不要求准确。
7. `user_prompt`
   去掉关键词后的本轮用户问题。
8. `assistant_excerpt`
   本轮助手输出的精简文本，没有则允许为空。
9. `conversation_excerpt`
   少量用户/助手原话片段，作为自由归纳的材料输入。
10. `learn_keyword`
    记录本次由哪个关键词触发，便于后续排查和审计。
11. `dedupe_key`
    幂等键，用于防止重复入库。

## 8. AKM 服务端处理链路

`/learn` 在 AKM 侧建议统一走一条固定流水线：

1. 校验请求体。
2. 基于 `dedupe_key` 做幂等判断。
3. 组装归纳输入。
4. 调模型生成 Markdown 知识条目。
5. 写入 `markdown_kb` 文档目录。
6. 绑定 `workspace_root`。
7. 执行索引同步。
8. 记录学习审计结果。

### 8.1 归纳原则

虽然本方案采用“AI 自由归纳”，但仍需给模型明确边界，避免输出变成聊天存档。

归纳规则建议如下：

1. 输出必须是 Markdown。
2. 优先提炼稳定知识，而非临时聊天语气。
3. 优先保留：
   - 结论
   - 原因
   - 判断依据
   - 修复方式
   - 注意事项
4. 允许保留少量关键原话片段。
5. 不保留触发关键词本身。
6. 如果本轮没有可沉淀知识，则返回空结果。

### 8.2 最终文档外层壳子

虽然正文允许自由归纳，但建议最终落盘时仍统一包一层稳定元信息壳子，方便检索与人工排查。例如：

```md
# 订单退款逻辑排查

- 来源：Codex
- 触发阶段：stop
- 工作区：/Users/nk/Desktop/ccs
- 记录时间：2026-06-23

## 知识摘要

...由 AI 自由生成...

## 关键原话摘录

> ...
```

这里稳定的是元信息结构，正文仍由 AI 自由生成。

### 8.3 文件命名

第一版采用简单稳妥的命名方式：

`YYYY-MM-DD-HHMMSS-短标题.learn.md`

例如：

`2026-06-23-153012-订单退款逻辑排查.learn.md`

这样做的目的：

1. 避免复杂 slug 规则。
2. 降低重名概率。
3. 便于按时间排查学习入库结果。

### 8.4 落盘后索引刷新

知识文档写入后，必须继续完成以下动作：

1. 为文件绑定 `workspace_root`。
2. 调用 `sync` 或等价单文件重建流程。

原因是：文件进入 `docs_dir` 不等于已经进入检索索引；只有后续同步或重建完成后，它才会真正参与知识检索。

## 9. 去重、幂等与失败策略

### 9.1 去重键

第一版建议由客户端生成 `dedupe_key`，AKM 只负责判重。

建议规则：

1. `Codex`
   使用 `codex:{session_id}:{turn_id}`。
2. `Claude Code`
   使用 `claude_code:{session_id}:{sha1(user_prompt + assistant_excerpt)}`。

### 9.2 AKM 判重规则

AKM 对 `dedupe_key` 执行以下判定：

1. 如果从未处理过，则继续学习入库。
2. 如果已经成功处理过，则直接返回去重成功结果。
3. 如果之前处理失败过，则允许后续再次重试，但要记录失败状态与错误信息。

### 9.3 为什么 `Stop + PreCompact` 安全

`Stop` 与 `PreCompact` 共存不会导致重复入库，只要满足以下约束：

1. `Stop` 始终是第一优先触发点。
2. `PreCompact` 只在本地 pending 仍未处理成功时兜底触发。
3. 两者传入完全相同的 `dedupe_key`。

### 9.4 失败处理原则

学习入库必须是“尽力而为”的后台能力，不能影响主会话。

因此第一版约束如下：

1. `/learn` 失败不能阻塞客户端正常结束。
2. `Stop` 失败后可以由 `PreCompact` 再尝试一次。
3. `PreCompact` 也失败后，本轮自动入库终止，仅记录错误日志。
4. AKM 侧应保存失败审计，便于后续排查。

## 10. OpenCode 策略

第一版不正式支持 `OpenCode` 自动接入。

原因如下：

1. `Claude Code` 与 `Codex` 官方文档已经明确了 Hook 字段与触发语义。
2. `OpenCode` 当前公开文档对插件事件列表有描述，但事件 payload 契约不像前两者那样稳定、清晰、成文。
3. 若为了第一版强行兼容 `OpenCode`，会把 `/learn` 和客户端适配逻辑显著复杂化。

兼容策略：

1. `/learn` 协议本身保留 `source` 与通用文本字段。
2. 未来如果需要支持 `OpenCode`，只新增一层客户端或插件适配逻辑，不改 AKM 主协议。

## 11. 测试设计

第一版至少覆盖以下测试场景：

### 11.1 客户端适配脚本

1. 最后一行不是关键词时，不写 pending 状态。
2. 最后一行是关键词时，能正确去掉关键词并写入 pending。
3. `Stop` 成功调用 `/learn` 后，pending 被清除或标记为已处理。
4. `Stop` 失败后，`PreCompact` 能继续补偿调用。
5. `Stop` 与 `PreCompact` 重复调用时，传递相同 `dedupe_key`。

### 11.2 `/learn` 接口

1. 缺失必填字段时返回校验错误。
2. `dedupe_key` 首次提交时正常处理。
3. 同一个 `dedupe_key` 再次提交时返回去重结果。
4. 当模型判断无可沉淀知识时，返回“忽略”结果而不落盘。
5. 正常落盘后，能完成 workspace 绑定与索引同步。

### 11.3 集成链路

1. 从关键词命中到知识文档落盘的全链路成功。
2. `Stop` 失败、`PreCompact` 成功的补偿链路。
3. AKM 入库失败但客户端主会话不受影响。
4. 新入库文档能在对应 workspace 范围内被检索命中。

## 12. 风险与取舍

第一版的主要取舍如下：

1. 选择客户端 Hook 触发，而不是 AKM 服务端自动猜测，可减少误判，但会增加客户端适配脚本工作量。
2. 选择 AKM 统一做知识归纳，而不是客户端直接总结，可保持总结逻辑一致，但会增加 AKM 侧编排责任。
3. 选择只追加新文档，而不是合并历史文档，可降低复杂度，但会让知识文件数量增加更快。
4. 选择先放弃 `OpenCode` 正式兼容，可缩小第一版范围，但会推迟跨三端统一体验。

## 13. 成功标准

当以下条件同时满足时，视为第一版设计成功：

1. `Claude Code` 与 `Codex` 可以通过最后一行关键词稳定触发学习入库。
2. 同一轮即使同时触发 `Stop` 和 `PreCompact`，也不会重复入库。
3. AKM 能把候选材料稳定归纳成 Markdown 知识条目，并写入 `markdown_kb`。
4. 新知识条目能按 `workspace_root` 正确参与后续检索。
5. 学习链路失败时，不影响客户端主会话正常结束。
