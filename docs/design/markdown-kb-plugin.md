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

增加 `on_request` hook，并在三类文本请求里按“命中才注入”的方式自动运行：

- 处理 `/v1/chat/completions`
- 处理 `/v1/messages`
- 处理 `/v1/responses`
- 仅当知识库确实命中片段时才注入参考资料

注入方式建议尽量保守：

1. 读取原始 `messages`
2. 抽取最后一条用户问题
3. 执行知识库检索
4. 在请求最前面插入一条 system 消息，说明以下内容是参考资料
5. 再继续沿用原链路转发

这种方式对 AKM 现有代理逻辑侵入最小。

## 自动注入现状

当前实现已经不再依赖模型名前缀或自定义请求头来决定是否启用知识库注入，而是统一收口到插件自己的 `on_request` 链路。

### 当前处理范围

插件启用后，当前默认处理三类文本请求：

- `/v1/chat/completions`
- `/v1/messages`
- `/v1/responses`

图片、embedding、rerank 等非文本问答入口不参与这条自动注入链路。

### 当前判定流程

当前实现的判断顺序更接近下面这条最小闭环：

1. 先识别当前请求是否属于三类文本入口
2. 从请求体中抽取最后一条用户问题
3. 尝试从请求里提取 `workspace_root / working_directory` 等工作域上下文
4. 先按工作域过滤候选文档，只保留公共文档和当前工作域文档
5. 执行检索；只有命中非空时才构造参考资料注入文本
6. 按 `chat / messages / responses` 各自原生字段把参考资料注回请求
7. 如果没有命中任何知识库片段，则直接透传原请求

这样做的核心约束是：

- 不要求调用方额外改模型名
- 不要求调用方额外带专用请求头
- 不命中就不注入，尽量避免污染普通请求

### 当前配置如何影响自动注入

自动注入链路当前直接复用插件默认检索配置：

- `embedding_model`
- `reranker_model`
- `top_k`
- `score_threshold`
- `semantic_weight / keyword_weight`

其中：

- `top_k` 控制最终保留的命中条数
- `score_threshold` 控制低分片段过滤
- `semantic_weight / keyword_weight` 只在未启用 rerank 时参与第一阶段排序
- `document_workspace_root` 会影响文档归属的工作域，从而影响自动注入时的候选文档过滤

### 与旧方案的差异

下面这些都属于早期设计阶段讨论过、但**不再代表当前实现**的方案：

- 通过模型名前缀启用知识库注入
- 通过 `X-AKM-KB` 之类的专用请求头启用或关闭注入
- 通过模型名前缀来表达“启用知识库”

当前如果仍在文档或页面文案里看到这类说法，应以代码现状为准，把它们视为待清理的旧描述。

### 推荐的第一版落地策略

如果要概括当前已经落地的第一版策略，更准确的说法是：

- 插件启用后，统一接管三类文本请求的自动注入判断
- 只有知识库实际命中时才注入
- 未命中的请求继续按原样透传

这样既保留了低侵入，也避免了额外要求调用方修改模型名或请求头。

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

## 后期动态维护方案

如果这个知识库后续要长期维护，核心问题就不再是“能不能查”，而是“文档更新后能不能稳定、低成本地同步到索引”。从实现复杂度、成本和收益的平衡来看，最推荐的演进路线不是一上来就做目录监听或任务队列，而是分阶段推进。

### 方案一：手动全量重建

最简单的方式仍然是保留全量 `rebuild`：

1. 扫描全部 Markdown 文件
2. 全量切片
3. 全量生成 embedding
4. 全量重建索引

优点：

- 逻辑最简单
- 最容易排障
- 不容易出现局部索引残留

缺点：

- 文档变多后会明显变慢
- embedding 成本高
- 只改一个文件也需要全量重建

这适合 PoC 阶段和小规模文档场景，但不适合作为长期维护的唯一策略。

### 方案二：文件级增量更新

这是最推荐的下一阶段方案。

核心思路：

1. 为每个文件记录 `file_path / file_name / content_hash / updated_at / chunk_ids`
2. 每次同步时识别三类变化：新增、修改、删除
3. 只对发生变化的文件重新切片、重新生成 embedding、重新写入索引

优点：

- 更新速度快很多
- embedding 成本显著下降
- 实现复杂度仍然可控
- 非常适合单机 Markdown 知识库

推荐作为长期默认维护方式。

### 方案三：目录同步

如果用户的知识库本来就是一个本地目录，而不是只通过页面上传文件，那么应补充“目录同步”能力。

可以支持：

- 手动点击“同步目录”
- 定时扫描指定目录

目录同步本质上仍然应复用“文件级增量更新”的判断逻辑，而不是重新走全量重建。

### 方案四：文件系统监听

这是更动态的方案：监听某个目录下 `.md` 文件的新增、修改、删除事件，并自动触发单文件更新。

优点：

- 最接近实时维护
- 用户改完文件后可以很快反映到知识库

缺点：

- 实现复杂度高
- 要处理重复事件、抖动事件、半写入状态
- 跨平台兼容性和后台常驻任务管理更麻烦

因此不建议太早做。更合理的顺序是：先做增量更新，再做目录同步，最后视需要再做目录监听。

### 方案五：上传即索引

对于“通过页面上传文件”的交互，建议长期演进到“上传即索引”：

- 上传单个 Markdown 文件后，立即只处理该文件
- 删除文件时，立即删除其索引条目

这样用户不需要每次都重新全量 `rebuild`，体验会更自然。

### 推荐的维护判断方式

建议后期采用双层判断：

1. 先用 `mtime + size` 做快速筛选
2. 命中疑似变更后，再计算 `sha256 / content_hash`
3. 最终以 `content_hash` 作为是否真的需要重建的依据

这样既能减少无意义 hash 计算，又能避免只看时间戳带来的误判。

### 推荐的索引元数据

为了支持动态维护，索引层最好至少记录：

- `doc_id`
- `file_name`
- `file_path`
- `content_hash`
- `updated_at`
- `embedding_model`
- `chunk_strategy_version`
- `chunk_size`
- `chunk_overlap`
- `indexed_at`

这样后续才能判断：

- 文件内容是否变化
- 切片策略是否变化
- embedding 模型是否变化
- 是否需要重建单文件或整库

### 推荐演进路线

综合来看，推荐按以下顺序演进：

1. 保留当前手动 `rebuild`
2. 增加文件级增量更新
3. 增加目录同步
4. 增加单文件重建 / 清空索引 / 健康检查等运维能力
5. 最后再视需要增加目录监听

如果后续确认要引入真正的向量库存储，也建议在“增量维护机制”已经稳定之后再替换底层，而不是一开始就把“向量库接入”和“动态同步机制”两个复杂问题绑在一起做。

## kb.db 方案与方案一迁移路径

基于 SQLite Vector 路线，`markdown_kb` 更适合让插件自己单独维护一个 `kb.db`，而不是继续长期依赖 `index.json`，也不是一开始就把知识库数据塞进 AKM 主库 `akm.db`。

推荐目录结构：

```text
~/.akm/markdown_kb/
├── docs/
└── index_store/
    └── kb.db
```

设计约定：

1. `docs/` 是原始 Markdown 文件的 source of truth。
2. `kb.db` 是知识库索引、副本 metadata 和向量数据的持久化载体。
3. 任意时候都可以保留 `docs/`、删除 `kb.db` 并重新 `rebuild`。

### 推荐表结构

至少拆 4 类数据：

1. `kb_documents`
   - 文件级元数据：`doc_id / file_name / file_path / content_hash / file_size_bytes / title / chunk_count / updated_at / indexed_at / created_at`
2. `kb_chunks`
   - chunk 元数据与文本：`chunk_id / doc_id / file_name / file_path / title / heading_level / chunk_index / chunk_text / content_hash / created_at / updated_at / indexed_at`
3. `kb_vectors`
   - 向量数据：`chunk_id -> embedding`
4. `kb_index_meta`
   - 索引级元信息：`embedding_model / reranker_model / chunk_size / chunk_overlap / schema_version / last_rebuilt_at`

### 方案一：最稳妥迁移

当前项目采用的就是这条路线：

1. 保留 `docs/` 原文目录
2. 忽略旧的 `index.json`
3. 初始化新的 `kb.db`
4. 扫描全部 Markdown 文件
5. 重新切片、重新 embedding
6. 全量写入 SQLite 表

这样做的优点：

- 逻辑最简单
- 不需要写 `index.json -> kb.db` 的一次性迁移脚本
- 最不容易带入旧脏数据
- 后续如果接 SQLite Vector 扩展或别的后端，也可以继续复用这套 schema 和插件 API

当前阶段即使尚未真实接入 SQLite Vector 扩展，也可以先把：

- metadata
- chunk_text
- embedding 向量

先统一落到 `kb.db` 中，检索阶段仍由 Python 层计算相似度。这样可以先把持久化从 JSON 文件迁移到 SQLite，而不把本地扩展安装风险和数据迁移风险绑在一起。

## 推荐实施顺序

建议按以下顺序开发：

1. 搭建 `app` 插件骨架
2. 完成 Markdown 上传与保存
3. 完成标题优先切片与最小检索闭环
4. 完成 `query / ask / delete / clear` 等基础 API
5. 完成插件页面与配置弹窗
6. 把默认索引从 `index.json` 迁到 `kb.db`
7. 最后再评估是否需要接入 SQLite Vector 扩展或自动注入聊天链路

## 结论

Markdown 知识库非常适合作为 AKM 的第三方插件实现，而不是改进 AKM 核心代码。最稳妥的第一版方案是：

- 做一个 `markdown_kb` 第三方 `app` 插件
- 通过 AKM 的 embedding/chat 接口调用模型
- 插件内部维护 Markdown 文档、切片逻辑与插件私有 `kb.db` 索引
- 先提供显式的检索 / 问答页面与 API
- 暂不默认拦截全部聊天请求

这样既能快速验证功能价值，也能最大程度保持 AKM 主体的稳定性与职责边界。

## 当前落地状态

截至当前仓库版本，项目里已经落了一个内置但默认关闭的 `markdown_kb` 插件，目录位于 `akm/plugins/markdown_kb/`。它的目标不是把 AKM 变成完整 RAG 平台，而是在不污染主代理链路的前提下，提供一套可显式启用、可本地维护的知识库能力。

当前已实现：

- `plugin.json`、`index.py`、`views/index.html` 三件套
- 管理台菜单入口 `/plugins/markdown_kb`
- 宿主页保留 AKM 左侧菜单，原插件页面通过 `/plugins/markdown_kb/raw` 加载
- `GET /api/markdown-kb/status`
- `GET /api/markdown-kb/files`
- `POST /api/markdown-kb/files/upload`
- `DELETE /api/markdown-kb/files/{name}`
- `POST /api/markdown-kb/rebuild`
- `POST /api/markdown-kb/rebuild-file`
- `POST /api/markdown-kb/sync`
- `POST /api/markdown-kb/query`
- `POST /api/markdown-kb/ask`
- `POST /api/markdown-kb/clear`
- `GET /api/markdown-kb/health`
- `on_request` 最小自动注入：默认覆盖 `chat / messages / responses` 三类文本请求，且只有命中非空时才注入
- 标题优先切片
- 本地数据目录默认落到 `~/.akm/markdown_kb/`
- embedding / 可选 rerank / chat 请求统一走本地 AKM 代理
- `embedding_model` 为必填配置；`reranker_model` 为可选配置
- 知识库页面不再内置独立配置弹窗，配置统一收口到插件列表页弹窗
- 插件列表页中的统一插件配置弹窗支持修改 `markdown_kb` 配置；当前规则是所有带配置项插件统一通过弹窗配置
- `markdown_kb` 的模型配置在插件列表页弹窗里改成当前 `/v1/models` 驱动的模型列表下拉
- 模型列表下拉通过通用 setting schema 能力实现，不再在公共模板里写死 `markdown_kb` 特判
- `data_dir / index_backend` 这类当前阶段无实际价值的伪配置已经从配置 schema 中收掉，状态页改为展示真实的健康/漂移信息

当前明确未实现：

- 真正的 SQLite Vector 扩展接入
- 目录同步 / 目录监听
- 请求级显式开关、路径白名单、模型白名单等更细粒度的自动注入控制面

当前实现说明：

- 对外仍然沿用知识库设计的 API 形态；
- 底层索引默认落在 `~/.akm/markdown_kb/index_store/kb.db`；
- 当前持久化已经收口到更完整的 `IndexStore` 接口 + `SqliteKbIndexStore` 默认实现，当前方法边界已覆盖 `replace_all / list_documents / delete_by_file / stats / clear` 这类真实 backend 常见能力；
- 当前检索阶段会优先使用内存预加载 + NumPy 矩阵化相似度计算，并补上 query embedding 缓存与 query 结果缓存；若运行环境暂时还没安装 `numpy`，则自动回退到 Python 循环计算；
- 这样做不是为了长期替代向量库，而是因为当前仓库还没有确认可用的 SQLite Vector 扩展接入方式，先把“上传 -> 切片 -> embedding -> 检索 -> 问答”闭环做通；
- 如果后续接入 SQLite Vector 扩展，优先在现有 `SqliteKbIndexStore` 路线上继续演进，而不是重新引入独立向量后端。

因此，下一阶段最合理的工作重心不再是“插件能不能挂进去”，而是沿着这个骨架继续补：

1. 更完整的目录同步与目录监听
2. 更细粒度的单文件维护与后台任务化
3. 底层索引从 `SqliteKbIndexStore` 继续推进到 SQLite Vector 扩展
4. 自动注入聊天链路
