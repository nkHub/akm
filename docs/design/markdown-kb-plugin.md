# Markdown 知识库插件设计草图

## 目标

在不修改 AKM 核心代码的前提下，为项目增加一个基于 Markdown 文件的本地知识库插件。第一版仅支持 `.md` 文件，重点解决以下问题：

- Markdown 文档导入与管理
- 文档切片与向量索引构建
- 基于向量检索的问答测试
- 通过 AKM 统一转发 embedding 与 chat 请求

该插件的目标是以最小侵入方式验证“AKM + 本地 Markdown 知识库”的组合是否实用，而不是在第一版就把 AKM 扩展成完整的 RAG 平台。

## 设计前提

本设计基于以下前提，如果后续目标变化，需要同步调整插件范围：

1. 仅支持本地单机使用，不考虑多用户隔离。
2. 仅支持 `.md` 文件，不处理 PDF、Word、网页抓取等异构来源。
3. 向量库存储使用本地 `Chroma` 持久化目录。
4. embedding 与问答模型统一走 AKM 本地代理，而不是插件内自行管理外部 API Key。
5. 第一版只提供显式的“检索 / 问答”页面与 API，不默认拦截所有聊天请求。

## 为什么适合做成插件

AKM 当前定位是本地 AI API Key 管理代理服务，核心职责是：

- 统一模型请求入口
- Key 管理与切换
- 协议转换
- 审计日志与健康监护

Markdown 知识库则更适合作为附加能力挂在插件层，原因如下：

1. 知识库需要独立的数据目录、索引生命周期与页面交互，这些都更符合 `app` 类插件职责。
2. AKM 已有 `/v1/embeddings` 与 `/v1/chat/completions` 接口，插件可以直接复用它们做向量化与问答生成。
3. 这样可以避免把文档管理、向量库、检索策略强耦合进代理核心。
4. 如果后续效果不理想，也能单独卸载插件，而不影响主链路。

## 插件定位

建议第一版把插件定义为一个第三方 `app` 插件：

- 插件名：`markdown_kb`
- 分类：`app`
- 菜单：启用
- 自动 RAG 注入：默认关闭

第一版只负责：

- 导入 Markdown 文件
- 构建 / 清空索引
- 查询相关片段
- 根据片段做问答测试

第二版如果验证有效，再考虑补充 `on_request` hook，实现对部分聊天请求自动注入知识库上下文。

## 目录结构建议

第三方插件建议放在 `~/.akm/plugins/markdown_kb/`：

```text
~/.akm/plugins/markdown_kb/
├── plugin.json
├── index.py
├── views/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── data/
│   ├── docs/
│   └── chroma/
└── README.md
```

目录说明：

- `plugin.json`：插件元信息与配置项定义
- `index.py`：插件主逻辑、路由与可选 hook
- `views/`：管理台页面资源
- `data/docs/`：保存导入的 Markdown 原文件
- `data/chroma/`：Chroma 持久化目录

如果后续希望数据目录与插件代码分离，也可以迁移到 `~/.akm/markdown-kb/`，但第一版放在插件目录最简单。

## plugin.json 草图

下面是建议的字段结构，作为实现参考：

```json
{
  "name": "markdown_kb",
  "category": "app",
  "has_menu": true,
  "version": "0.1.0",
  "description": "Markdown 本地知识库插件，支持导入、切片、向量检索和问答测试",
  "menu": {
    "title": "知识库",
    "icon": "book",
    "order": 60
  },
  "routes_prefix": "/api/markdown-kb",
  "hooks": {
    "on_request": false
  },
  "settings": [
    {
      "key": "embedding_model",
      "label": "嵌入模型",
      "type": "text",
      "default": "text-embedding-3-small"
    },
    {
      "key": "chat_model",
      "label": "问答模型",
      "type": "text",
      "default": "gpt-4o-mini"
    },
    {
      "key": "chunk_size",
      "label": "切片大小",
      "type": "number",
      "default": 800
    },
    {
      "key": "chunk_overlap",
      "label": "切片重叠",
      "type": "number",
      "default": 120
    },
    {
      "key": "top_k",
      "label": "默认召回数",
      "type": "number",
      "default": 4
    },
    {
      "key": "auto_rag_enabled",
      "label": "启用自动检索注入",
      "type": "boolean",
      "default": false
    }
  ]
}
```

## 第一版 API 设计

第一版 API 只保留最小闭环，避免过早扩张：

### `GET /api/markdown-kb/status`

返回插件运行状态：

- 文档数量
- chunk 数量
- 索引路径
- 最后一次重建时间

### `GET /api/markdown-kb/files`

列出当前已导入的 Markdown 文件。

### `POST /api/markdown-kb/files/upload`

上传 `.md` 文件并保存到 `data/docs/`。

第一版可支持两种行为：

1. 上传后立即入库建索引
2. 上传后仅保存，等待手动重建

建议优先使用第一种，降低用户理解成本。

### `POST /api/markdown-kb/files/import`

从本地目录批量导入 `.md` 文件。第一版可只支持一个目录路径，不必做复杂过滤规则。

### `DELETE /api/markdown-kb/files/{name}`

删除指定文档，并同步删除对应索引记录。

### `POST /api/markdown-kb/rebuild`

全量重建索引。第一版只做全量，不做复杂的后台任务系统。

### `POST /api/markdown-kb/query`

输入问题，只做向量检索，返回：

- 命中的 chunks
- 来源文件名
- chunk 序号
- 可选相似度信息

### `POST /api/markdown-kb/ask`

输入问题，执行完整链路：

1. 问题向量化
2. 检索 top-k chunks
3. 拼接 context
4. 调用 AKM chat 接口生成答案
5. 返回答案与引用片段

### `POST /api/markdown-kb/clear`

清空知识库索引。是否同时删除原文档可作为请求参数控制。

## 页面设计建议

页面建议只提供一个“知识库”入口，并保持偏工具化，而不是产品化。第一版分为四个区域即可：

### 1. 文档管理

- 上传 `.md` 文件
- 指定目录批量导入
- 文件列表
- 删除文件

### 2. 索引管理

- 查看索引状态
- 查看 chunk 总数
- 全量重建
- 清空索引

### 3. 检索测试

- 输入问题
- 查看 top-k 命中片段
- 显示来源文件与片段序号

### 4. 问答测试

- 输入问题
- 查看拼接上下文
- 查看最终回答
- 查看引用来源

第一版页面重点是可观测性，便于调试检索质量，而不是复杂视觉设计。

## Markdown 切片策略

用户提供的原始脚本采用“按空行分段 + 字符长度累计”方式，这个方向是成立的，但插件化后建议稍微提升为“标题优先”的结构化切片。

建议第一版切片规则：

1. 先按 Markdown 标题层级拆分，优先识别 `#`、`##`、`###`
2. 每个标题块内部按段落累计
3. 达到 `chunk_size` 后再切分
4. 相邻 chunks 保留少量 `overlap`

这么做的原因是：

- Markdown 最有价值的信息就是标题层级
- 纯字符切片容易破坏语义边界
- 标题信息对后续结果展示和可解释性更友好

## 存储与 metadata 设计

即使第一版使用 Chroma，也建议每个 chunk 都带完整 metadata，至少包括：

- `doc_id`
- `file_name`
- `file_path`
- `title`
- `chunk_index`
- `chunk_text`
- `content_hash`
- `created_at`
- `updated_at`

原因如下：

1. 删除单个文档时可以准确删除其全部 chunks。
2. 前端可以展示片段来源，而不是只展示裸文本。
3. 后续可以基于文件 hash 做增量更新。
4. 可以避免重复导入和索引污染。

`id` 也不建议继续使用 `base_name_{i}` 这种形式，因为不同目录下的同名文件会冲突。更稳妥的方案是使用“文件路径 hash + chunk_index”的稳定组合。

## 模型调用方式

插件不应自行管理 OpenAI / Ollama / DeepSeek 等外部接入，而应统一走 AKM 本地代理：

- embedding：`http://127.0.0.1:8800/v1/embeddings`
- chat：`http://127.0.0.1:8800/v1/chat/completions`

这样做的优点：

1. 插件不需要保存独立 API Key。
2. 自动复用 AKM 已有的 Key 轮换与故障切换。
3. 请求统一进入 AKM 审计日志，便于排障。
4. 后续更换供应商或模型时，插件主体逻辑无需重写。

因此，插件应只关心“要调用哪个模型名”，而不关心该模型最终由哪个供应商 key 提供。

## 问答链路设计

第一版建议使用最小可用的标准 RAG 流程：

1. 用户输入问题
2. 使用 embedding 模型对问题向量化
3. Chroma 检索 top-k chunks
4. 将命中的 chunks 拼成 context
5. 调用 AKM chat 接口生成答案
6. 使用强约束 system prompt，要求“只根据资料回答，不知道就说不知道”
7. 返回最终答案与引用片段

这种方式实现简单，便于先验证切片和召回质量。

## 是否要自动注入到聊天链路

可以做，但不建议第一版就默认启用。原因如下：

1. 需要先定义哪些请求应该走知识库。
2. 默认拦截所有聊天请求，容易污染普通对话。
3. 需要处理上下文长度限制。
4. 需要提供可观测的启用开关与命中反馈。

因此建议分阶段推进：

### 第一版

- 仅通过插件页面显式使用“检索 / 问答”能力
- 不修改正常代理请求行为

### 第二版

增加 `on_request` hook，但只在明确命中时启用，例如：

- 请求头带 `X-AKM-KB: markdown_kb`
- 模型名使用约定前缀，如 `kb:gpt-4o-mini`
- 插件配置显式开启自动 RAG 注入

注入方式建议尽量保守：

1. 读取原始 `messages`
2. 抽取最后一条用户问题
3. 执行知识库检索
4. 在请求最前面插入一条 system 消息，说明以下内容是参考资料
5. 再继续沿用原链路转发

这种方式对 AKM 现有代理逻辑侵入最小。

## 自动注入触发条件设计

自动注入的核心原则不是“能注就注”，而是“只有用户或调用方明确表达了要用知识库时才注入”。如果默认对所有请求无条件拼接知识库上下文，极容易污染普通对话、增加 token 成本，并且让排障变得困难。

因此建议插件至少支持以下三类触发方式，并定义清晰的优先级。

### 触发方式一：请求头显式启用

这是最推荐的方式，因为最直接、最显式，也最方便排查。

建议约定：

- `X-AKM-KB: markdown_kb`
- `X-AKM-KB-Mode: auto`
- `X-AKM-KB-TopK: 4`

最小可用版本里，实际只需要 `X-AKM-KB` 就够了，其他头都可以作为后续扩展。

推荐语义：

- `X-AKM-KB: markdown_kb`
  - 表示本次请求启用 `markdown_kb` 插件检索注入
- `X-AKM-KB: off`
  - 即使全局开启自动注入，也强制本次关闭
- `X-AKM-KB-TopK: N`
  - 覆盖本次请求的默认召回数

优点：

- 显式且不歧义
- 不污染模型名
- 对 `Chat / Responses / Messages` 三种入口完全通用
- 最适合 SDK、脚本和内部调用方接入

建议将它定义为最高优先级触发条件。

### 触发方式二：模型名前缀触发

对于无法方便控制请求头的客户端，可以提供模型名前缀触发作为补充方案。

建议约定：

- `kb:gpt-4o-mini`
- `kb:gpt-4.1`
- `kb:claude-sonnet-4`

其语义是：

- `kb:` 前缀只代表“启用知识库注入”
- 去掉前缀后的真实模型名，仍然交由 AKM 正常做模型匹配与转发

例如：

- `kb:gpt-4o-mini` → 知识库插件命中 → 实际转发模型仍是 `gpt-4o-mini`
- `kb:deepseek-chat` → 知识库插件命中 → 实际转发模型仍是 `deepseek-chat`

优点：

- 对不方便加 header 的客户端更友好
- 便于用户手动测试

缺点：

- 会让模型名承担“功能开关”语义
- 需要插件在 `on_request` 里把前缀剥掉，再把真实模型名继续传下去
- 如果实现不谨慎，容易与模型别名映射插件产生耦合

因此它应作为次优先方案，而不是默认首选。

### 触发方式三：插件全局配置开关

插件配置里可以提供一个全局开关，例如：

- `auto_rag_enabled=true`

但这个开关不建议单独直接生效到所有请求，而应作为“默认候选行为”，仍然配合路径白名单、模型白名单或显式关闭机制使用。

更稳妥的设计是：

1. 全局开关只表示“允许自动注入逻辑运行”
2. 是否真的触发，还要继续匹配路径、模型或 header
3. 任意请求都可以通过 `X-AKM-KB: off` 显式关闭

也就是说，全局开关更像一个“主闸门”，不是一个“对所有请求直接注入”的暴力开关。

### 推荐优先级

三种触发方式建议按以下优先级处理：

1. 请求头显式关闭：`X-AKM-KB: off`
2. 请求头显式启用：`X-AKM-KB: markdown_kb`
3. 模型名前缀命中：如 `kb:gpt-4o-mini`
4. 插件全局配置命中：如 `auto_rag_enabled=true`
5. 以上都未命中：不注入，直接透传

这个优先级的目标是：

- 用户的单次显式意图永远高于全局默认值
- 可以安全回退
- 可以避免“明明不想用知识库，却被默认注入”的问题

### 推荐的判定流程

插件在 `on_request` 中可以按以下顺序判断：

1. 读取请求路径，只处理：
   - `/v1/chat/completions`
   - `/v1/responses`
   - `/v1/messages`
2. 检查是否带 `X-AKM-KB: off`
   - 如果是，直接跳过知识库注入
3. 检查是否带 `X-AKM-KB: markdown_kb`
   - 如果是，启用知识库注入
4. 检查模型名是否以 `kb:` 开头
   - 如果是，剥掉前缀并启用知识库注入
5. 检查插件全局配置是否允许自动注入
   - 如果允许，再继续判断是否命中模型白名单 / 路径白名单
6. 如果最终未命中，直接透传原请求

建议始终把“是否启用知识库”的判断放在执行检索之前，避免无意义向量检索带来的额外开销。

### 建议补充的配置项

如果要支持自动注入，建议插件配置里除了 `auto_rag_enabled` 再补几项：

- `auto_rag_paths`
  - 默认：`chat/completions,responses,messages`
- `auto_rag_model_allowlist`
  - 默认空，表示不按模型限制
- `auto_rag_header_name`
  - 默认：`X-AKM-KB`
- `auto_rag_model_prefix`
  - 默认：`kb:`
- `auto_rag_default_top_k`
  - 默认：`4`

这样既能保留默认约定，也能避免把触发规则写死在代码里。

### 冲突处理建议

多种触发条件可能同时出现，需要提前定义冲突规则，避免实现时行为漂移。

推荐规则：

1. `X-AKM-KB: off` 永远优先，哪怕模型名带 `kb:` 也不注入。
2. `X-AKM-KB: markdown_kb` 优先于模型前缀。
3. 如果 header 指定了 `top_k`，优先于插件默认值。
4. 如果模型名前缀启用后去掉前缀得到空模型名，应直接报错，而不是继续猜测。
5. 如果插件全局开启，但请求明确是图片、embedding 等非聊天接口，应直接跳过。

### 不建议的默认行为

以下默认行为不建议启用：

- 对所有 `/v1/chat/completions` 无条件自动注入
- 只要模型名包含某个子串就自动命中知识库
- 不提供显式关闭手段
- 根据用户问题内容猜测是否该用知识库

这些规则虽然看起来“智能”，但可预测性很差，而且非常容易在生产使用时造成误注入。

### 推荐的第一版落地策略

如果要做自动注入的第一版，建议只启用这一条最简单规则：

- 仅当请求头带 `X-AKM-KB: markdown_kb` 时启用知识库注入

等这个模式验证稳定后，再逐步增加：

1. 模型名前缀触发
2. 全局配置主闸门
3. 模型白名单或路径白名单

这样能显著降低第一版复杂度，也更符合 AKM 当前“尽量低侵入”的扩展原则。

## 三种入口的注入格式

知识库插件不应只支持 `Chat`，而应同时兼容 AKM 当前暴露的三种主入口：

- `/v1/chat/completions`
- `/v1/responses`
- `/v1/messages`

设计原则如下：

1. 优先按入口协议的原生字段注入知识库上下文。
2. 不在知识库插件里手工把 `Responses` 或 `Messages` 先改写成 `Chat`。
3. 协议转换仍交给现有 `protocol_converter` 处理，避免职责重叠。
4. 注入内容始终保持“强约束、低侵入、可回退”。

这样做的好处是：

- 不会和 AKM 现有的协议转换链路互相打架
- 能保留 `Responses` 的 continuation / tool call 语义
- 能保留 `Messages` 的 system / content block 语义
- 出问题时更容易从入口协议定位问题

### 通用注入模板

无论入口是哪一种协议，知识库插件都建议先生成一段统一的“参考资料说明”，再按不同协议落到对应字段中。建议模板如下：

```text
以下是与当前问题相关的参考资料。你必须优先依据这些资料回答。

如果资料不足以支持结论，必须明确回答“不知道”或“资料中未提及”，不要补充未在资料中出现的事实。

参考资料：
<chunk 1>

---

<chunk 2>

---

<chunk 3>
```

这段模板的目标不是替代用户问题，而是为模型追加一层明确的上下文约束。

### Chat 注入格式

对于 `/v1/chat/completions`，建议直接操作 `messages`，这是三种格式里最简单、最稳定的方式。

建议策略：

1. 保留原始 `messages` 顺序。
2. 将知识库上下文包装为一条新的 `system` 消息。
3. 插入到最前面，或插入到现有 `system` 消息之后。
4. 不直接改写最后一条用户消息，避免污染用户原始输入。

推荐注入示意：

```json
{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "system",
      "content": "以下是与当前问题相关的参考资料。你必须优先依据这些资料回答。如果资料不足以支持结论，必须明确回答'不知道'或'资料中未提及'。\n\n参考资料：\n..."
    },
    {
      "role": "system",
      "content": "你是一个有帮助的助手。"
    },
    {
      "role": "user",
      "content": "这里是用户问题"
    }
  ]
}
```

保守规则：

- 如果原请求已经有多个 `system`，优先把知识库 `system` 插到最前面。
- 不修改 `tools`、`tool_choice`、`response_format` 等其他字段。
- 如果召回为空，则不注入任何知识库消息，直接透传原请求。

### Responses 注入格式

对于 `/v1/responses`，不建议先手工改成 Chat，再去插 `messages`。更稳妥的方式是优先利用 `instructions` 字段承载知识库上下文。

原因是 AKM 当前已有明确的 `Responses -> Chat` 转换规则，其中 `instructions` 会映射到 `system` 角色消息，因此把知识库内容放到 `instructions`，可以最自然地进入后续链路。

建议策略：

1. 如果原请求没有 `instructions`，则直接设置一段新的知识库约束文本。
2. 如果原请求已有 `instructions`，则将知识库约束文本追加到原 `instructions` 前面或后面。
3. 尽量不改写 `input` 主体，除非后续遇到特定客户端必须依赖 `input` 才能生效。
4. 不碰 `previous_response_id`、`tools`、`text.format`、`response_format` 等结构化字段。

推荐注入示意：

```json
{
  "model": "gpt-4.1",
  "instructions": "以下是与当前问题相关的参考资料。你必须优先依据这些资料回答。如果资料不足以支持结论，必须明确回答'不知道'或'资料中未提及'。\n\n参考资料：\n...\n\n原始系统要求：你是一个代码助手。",
  "input": "这里是用户问题"
}
```

如果原始 `input` 是数组结构，也仍然建议优先只改 `instructions`，例如：

```json
{
  "model": "gpt-4.1",
  "instructions": "知识库约束与参考资料...",
  "input": [
    {
      "role": "user",
      "content": [
        {"type": "input_text", "text": "这里是用户问题"}
      ]
    }
  ]
}
```

保守规则：

- 不要覆盖已有 `instructions`，只做拼接。
- 不要改写 `previous_response_id`，避免破坏续接语义。
- 不要直接篡改 `function_call` / `function_call_output` 内容。
- 如果召回内容过长，应先裁剪知识库上下文，而不是去压缩原始 `input`。

### Messages 注入格式

对于 `/v1/messages`，建议优先使用其原生 `system` 字段注入知识库上下文，而不是把参考资料拼进普通 `messages` 文本里。

AKM 当前的 `Messages -> Chat` 转换规则会把 `system` 映射为首条 system 消息，因此这一做法最自然，也最接近 Anthropic 风格接口的设计习惯。

建议策略：

1. 如果原请求没有 `system`，则直接新增知识库 `system`。
2. 如果原请求已有 `system` 字符串，则将知识库上下文和原 `system` 做拼接。
3. 如果原请求已有结构化 `system` 内容，则优先按该结构追加新的文本块。
4. 不直接改写已有 `messages` 的 content block，避免误伤 tool result 或多模态块结构。

推荐注入示意：

```json
{
  "model": "claude-sonnet-4",
  "system": "以下是与当前问题相关的参考资料。你必须优先依据这些资料回答。如果资料不足以支持结论，必须明确回答'不知道'或'资料中未提及'。\n\n参考资料：\n...",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "这里是用户问题"
        }
      ]
    }
  ]
}
```

如果原请求已有 `system`，推荐拼接示意：

```text
以下是与当前问题相关的参考资料。你必须优先依据这些资料回答。如果资料不足以支持结论，必须明确回答“不知道”或“资料中未提及”。

参考资料：
...

---

原始系统要求：
你是一个严谨的研究助手。
```

保守规则：

- 不改写 `messages` 中的 `tool_result`、`tool_use`、图片等结构化块。
- 不主动追加假的 `user` 或 `assistant` 消息。
- 如果 `system` 已经很长，优先裁剪知识库上下文，而不是覆盖原始 system。

### 三种入口的推荐优先级

虽然三种入口都可以支持，但落地时建议按以下顺序推进：

1. `Chat`：实现最简单，调试成本最低。
2. `Responses`：价值很高，尤其适合 Codex / tool-call / continuation 场景。
3. `Messages`：也应支持，但测试时要特别注意 Anthropic 风格 content block 与 tool use 结构。

### 不建议的做法

以下做法不建议采用：

- 在知识库插件里把 `Responses` 先手工改写成 Chat 再注入
- 在知识库插件里把 `Messages` 先展平成纯文本再注入
- 直接覆盖原始 `instructions` / `system`
- 为了塞入知识库上下文而修改工具调用结构
- 默认对所有请求无条件注入知识库内容

这些做法虽然短期看起来省事，但长期会明显增加与协议转换链路的耦合风险。

## 与用户脚本的复用关系

用户提供的原始脚本思路是正确的，以下能力都可以保留：

- embedding 请求封装
- chat 请求封装
- Markdown 切片逻辑
- 检索与问答主流程
- 清空索引能力

但插件化时需要做以下改造：

1. 不再使用硬编码全局配置，应改为读取插件配置。
2. 不再依赖 `print` 输出，应改为 API JSON 返回或日志记录。
3. 必须补充 metadata，而不是只存 `documents` 与简单 `ids`。
4. 需要支持文档删除时同步删除对应索引。
5. 最好增加文件 hash 机制，为增量更新预留空间。

## 第一版建议严格不做的内容

为了避免过度设计，第一版建议明确不做以下能力：

- PDF / Word / 网页抓取
- 多知识库 namespace
- 权限系统
- rerank
- 混合检索
- 实时目录监听
- 自动拦截全部聊天请求
- 复杂任务队列

第一版目标仅是验证：

1. Markdown 切片是否合理
2. 向量召回是否可用
3. 通过 AKM 调用 embedding/chat 是否顺畅
4. 插件化方式是否足够低侵入

## 风险与边界

该方案虽然可行，但需要提前说明几个现实边界：

1. Chroma 更适合本地单机轻量场景。
2. Markdown 文档多时，全量重建会变慢。
3. embedding 成本会随着文档体量增长。
4. 如果没有 metadata，后续维护成本会快速上升。
5. 自动 RAG 注入若默认开启，容易对现有调用行为造成不可见影响。

因此，该插件的最佳落地路径仍然是：

- 先做显式可见的知识库页面
- 先做检索和问答测试
- 验证效果后，再考虑自动注入主链路

## 推荐实施顺序

建议按以下顺序开发：

1. 搭建 `app` 插件骨架
2. 完成 Markdown 上传与保存
3. 完成切片与 Chroma 索引构建
4. 完成检索 API
5. 完成问答 API
6. 完成插件页面
7. 最后再评估是否需要 `on_request` 自动注入

## 结论

Markdown 知识库非常适合作为 AKM 的第三方插件实现，而不是改进 AKM 核心代码。最稳妥的第一版方案是：

- 做一个 `markdown_kb` 第三方 `app` 插件
- 通过 AKM 的 embedding/chat 接口调用模型
- 插件内部维护 Markdown 文档、切片逻辑与 Chroma 索引
- 先提供显式的检索 / 问答页面与 API
- 暂不默认拦截全部聊天请求

这样既能快速验证功能价值，也能最大程度保持 AKM 主体的稳定性与职责边界。
