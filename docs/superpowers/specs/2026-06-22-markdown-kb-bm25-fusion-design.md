# markdown_kb BM25 融合设计

## 背景

当前 `markdown_kb` 的检索主链路采用“向量分 + 轻量关键词覆盖率分”的两路混合排序：

- 向量分负责语义召回；
- 关键词分只做标题 / chunk 文本中的 token 覆盖率计算；
- 启用 `reranker_model` 后，第一阶段会退回纯向量召回，再把候选交给 rerank。

这套设计的主要问题有两点：

1. 关键词分不是 BM25，对精确术语、低频词和局部短语的区分能力有限；
2. 启用 rerank 后，关键词信号完全退出第一阶段候选召回，导致“相关 chunk 没进候选集，rerank 也无法补救”。

本次改动目标是把当前“向量分 + 轻量关键词覆盖率分”升级为“向量分 + BM25 分”的加权融合，并保持现有 API、配置和页面使用方式尽量不变。

## 目标

1. 在不引入新第三方检索库的前提下，为 `markdown_kb` 增加内置 BM25 打分。
2. 保持当前 `semantic_weight / keyword_weight` 配置语义不变，仅把 `keyword_weight` 对应的分数来源从覆盖率改成 BM25。
3. 调整启用 rerank 时的第一阶段候选召回逻辑，使 BM25 仍参与粗召回，而不是退回纯向量。
4. 保持 `query / ask / 自动注入` 共用同一条检索主链路，避免不同入口表现不一致。

## 非目标

1. 不引入独立倒排索引存储层。
2. 不新增 `bm25_k1 / bm25_b` 等对外配置项。
3. 不修改当前工作域过滤、文档范围过滤和项目优先收敛规则。
4. 不重做现有 chunk 切分策略。

## 方案

### 1. 检索打分

保留现有 `_score_documents()` 主结构，但将 `_score_keywords()` 的实现替换为 BM25 打分：

- query token 继续复用现有 `_tokenize_keywords()`；
- 文档 token 使用同一套分词规则，从 `title + chunk_text` 生成 token 序列；
- 基于当前候选文档集合计算：
  - 文档长度 `dl`
  - 平均文档长度 `avgdl`
  - 文档频率 `df`
  - 逆文档频率 `idf`
- 对每个候选 chunk 计算 BM25 原始分。

由于 BM25 原始分与向量余弦分不在同一量纲，融合前需要做归一化。当前设计采用“在当前候选集内做 min-max 归一化”：

- 若所有候选 BM25 原始分相同，则统一记为 `0.0`；
- 否则归一化到 `0~1` 后作为 `keyword_score`。

最终保留现有融合公式：

`hybrid_score = vector_score * semantic_weight + keyword_score * keyword_weight`

这样可以最大程度复用现有配置、返回结构和测试断言。

### 2. rerank 前候选召回

当前逻辑在启用 `reranker_model` 后会把第一阶段退回纯向量召回。本次调整为：

- 无论是否启用 rerank，第一阶段都继续使用“向量分 + BM25 分”的混合排序；
- rerank 只负责第二阶段重排；
- `score_threshold` 在 rerank 后仍按最终 `score` 过滤，维持现有行为。

这样做的原因是：rerank 只能重排已召回候选，不能弥补第一阶段漏召回。因此第一阶段仍需保留字面相关性信号。

### 3. 缓存

当前插件已经有 embedding 相关缓存。本次新增一份与文档快照绑定的 BM25 统计缓存，缓存内容包括：

- tokenized documents
- term frequency
- document frequency
- document lengths
- average document length

缓存失效时机与当前 embedding 文档缓存保持一致：

- `rebuild_index`
- `rebuild_file`
- `sync_index` 应用变更后
- `clear_index`

只要索引文档集合变化，就一并清空 BM25 缓存，避免使用过期统计值。

### 4. 兼容性

保持以下内容不变：

- 外部 API 字段名：`vector_score / keyword_score / hybrid_score / score`
- 插件配置项：`semantic_weight / keyword_weight / top_k / score_threshold`
- 前端测试页的交互方式

变化仅在于：

- `keyword_score` 的语义从“覆盖率分”变为“归一化 BM25 分”；
- 启用 rerank 时，第一阶段候选召回不再忽略关键词信号。

## 代码影响范围

### 核心代码

- `akm/plugins/markdown_kb/index.py`

预计新增或调整的职责：

- 替换 `_score_keywords()` 的内部实现；
- 增加 BM25 文档统计缓存；
- 调整 `_retrieve()` 中启用 rerank 时的第一阶段权重处理逻辑；
- 在缓存失效点清理 BM25 缓存。

### 测试

- `tests/test_server.py`

需要补充或调整的验证点：

1. 未启用 rerank 时，精确术语查询中，BM25 能稳定把命中字面术语的文档排前；
2. 中文短语查询下，局部短语命中仍可工作；
3. 启用 rerank 时，第一阶段仍会保留 BM25 对候选召回的影响，而不是退回纯向量；
4. 现有 `query / ask / rebuild / mixed embedding dimensions` 等测试继续通过。

### 文档

- `README.md`
- `docs/design/plugin-system.md`

需要同步更新的内容：

- `markdown_kb` 当前检索策略说明；
- `keyword_weight` 的语义说明；
- 启用 rerank 时第一阶段候选召回行为说明。

预计无需更新：

- `docs/release-guide.md`
- `docs/design/web-components.md`

若实现过程中发现这些文档已有与新行为直接冲突的内容，再一并修正。

## 风险与权衡

1. 中文检索效果仍受当前分词方式影响。
说明：本次只把关键词分升级为 BM25，不引入更复杂的中文分词依赖，因此中文效果会优于简单覆盖率，但不会等同于成熟中文搜索引擎。

2. BM25 归一化方式会影响融合稳定性。
说明：min-max 归一化实现简单、改动小，但不同 query 之间的绝对分数不可直接比较。当前链路本来就是单 query 内排序，因此该权衡可接受。

3. 启用 rerank 后保留 BM25 粗召回，可能改变现有部分排序结果。
说明：这是有意行为变化，目标是提升候选召回质量。最终顺序仍由 rerank 决定，但候选集会更偏向“语义 + 字面都相关”的片段。

## 验收标准

1. `markdown_kb` 的 query / ask / 自动注入统一走“向量分 + BM25 分”第一阶段召回。
2. 启用 rerank 后，第一阶段不再退回纯向量召回。
3. 现有配置和 API 结构不需要用户额外迁移。
4. 新增测试能够覆盖 BM25 融合的核心行为。
5. `README.md` 与 `docs/design/plugin-system.md` 已同步到与实现一致。
