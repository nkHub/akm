# markdown_kb 记忆系统设计

## 问题

当前 `markdown_kb` 检索链路纯无状态：每次 query → embedding → KNN/BM25 → 可选 rerank → score_threshold 过滤。所有 chunk 完全平等，缺乏"哪些内容近期被频繁用到"的记忆，导致命中率偏低。

## 核心思路

引入**艾宾浩斯遗忘曲线**驱动 chunk 级别记忆值：
- **短期记忆**：近期命中过的 chunk，记忆值高，随时间快速衰减（默认半衰期 24h）
- **长期记忆**：反复被确认有价值的 chunk，衰减极慢，长期保持高权重
- **遗忘**：从未被命中或长期未用的 chunk 自动降权，减少检索噪音

记忆值来源于两层反馈：
1. **批量扫描**（`.codex/sessions/`）→ 冷启动建基线
2. **hooks 触发**（Stop/PreCompact）→ 日常使用中持续微调

## 架构总览

```
┌──────────────────────────────────────────────────────────┐
│  触发源              │  行为                │  产出         │
├──────────────────────┼──────────────────────┼───────────────┤
│  自动整理记忆（推荐）  │  定期/消息阈值触发     │ 记忆 boost   │
│                      │  扫描 sessions 生成知识 │ .learn.md    │
├──────────────────────┼──────────────────────┼───────────────┤
│  hooks Stop/Compact  │  确认对话有稳定知识    │ .learn.md    │
│                      │  关联 chunk boost     │ 记忆 boost   │
├──────────────────────┼──────────────────────┼───────────────┤
│  检索命中             │  chunk 被检索到       │ 基础 boost   │
└──────────────────────┴──────────────────────┴───────────────┘
```

注：**手动扫描 API 默认关闭**，启用后仅在显式调用时执行。日常使用推荐自动整理记忆。

四层 boost 叠加到同一个艾宾浩斯衰减曲线：

| 来源 | boost 值 | 场景 |
|------|----------|------|
| 新 learn 文档初始值 | 0.30 | 从真实对话提炼，高可信度起跑 |
| hooks 确认（Stop/PreCompact） | 0.20 | 模型确认有稳定知识 |
| 扫描交叉验证 | 0.15 | 存量 chunk 在 session 中被引用 |
| 检索命中 | 0.10 | 被动命中，最弱信号 |

---

## 一、数据模型

### `kb_chunk_memory` 表

建在 `~/.akm/markdown_kb/index_store/kb.db`，与索引同库：

```sql
CREATE TABLE kb_chunk_memory (
    chunk_id      TEXT PRIMARY KEY,
    hit_count     INTEGER NOT NULL DEFAULT 0,
    last_hit_at   TEXT,
    memory_value  REAL NOT NULL DEFAULT 0.0,
    created_at    TEXT NOT NULL
);
```

**设计要点：**
- 不需要独立的 STM/LTM 两套存储 —— 同一根衰减曲线天然实现了"用进废退"
- `memory_value` 始终在 `[0, 1]` 区间，越接近 1 表示记忆越强
- 全文回表到 `kb_chunks` 取 chunk 数据

### `kb_chunks` 新增列

```sql
ALTER TABLE kb_chunks ADD COLUMN categories TEXT DEFAULT '';
```

存储逗号分隔的分类标签，如 `"技术实现,业务逻辑,uniapp"`。为空时表示未分类。

### 记忆分类

6 个固定类别，不同类别使用独立衰减半衰期：

| 类别 | 半衰期 | 说明 |
|------|--------|------|
| `技术实现` | 48h | API 用法、框架特性、具体代码方案 |
| `业务逻辑` | 168h (7d) | 业务流程、规则、状态判断 |
| `架构设计` | 168h (7d) | 项目结构、模块划分、设计模式 |
| `调试修复` | 24h | bug 原因、排查方法、修复方案 |
| `配置部署` | 336h (14d) | 环境配置、打包、发布 |
| `代码风格` | 720h (30d) | 命名约定、注释规范、代码习惯 |

**半衰期设计原则：**
- 调试经验快速忘记（同一类 bug 不常反复出现）
- 代码风格永久保持（团队规范写一次记很久）
- 配置部署几乎不过期（环境很少变）

分类来源：
1. chat 模型自动归类（learn 时一并生成）
2. `.learn.md` 头部 `**知识分类**` 行手动修改
3. rebuild 时重新入库生效

### 配置项

新增 `markdown_kb` 插件配置：

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `memory_enabled` | bool | true | 是否启用记忆系统 |
| `memory_weight` | float | 0.10 | 记忆分在混合排序中的原始权重（会归一化） |
| `memory_boost` | float | 0.15 | 单次检索命中的记忆增量 |
| `category_bonus` | float | 0.10 | 分类匹配加权比例 |
| `category_config` | dict | 见下表 | 分类名 → {half_life_hours} |
| `organize_interval_hours` | float | 24 | 自动整理记忆周期（小时） |
| `organize_message_threshold` | int | 50 | 触发自动整理的消息数阈值 |

`category_config` 默认值：

```python
{
    "技术实现": {"half_life_hours": 48},
    "业务逻辑": {"half_life_hours": 168},
    "架构设计": {"half_life_hours": 168},
    "调试修复": {"half_life_hours": 24},
    "配置部署": {"half_life_hours": 336},
    "代码风格": {"half_life_hours": 720},
}
```

如果 chunk 未分类或分类不在配置中，回退到默认半衰期 24h。

---

## 二、记忆值算法（艾宾浩斯）

### 2.1 时间衰减（按分类）

查询时读取当前记忆值，半衰期取决于 chunk 的分类：

```
def half_life = category_config[chunk.category].half_life_hours  or 24.0  # 默认
λ = ln(2) / half_life
decay = e^(-λ × Δt)
current_memory = stored_memory × decay
```

多分类 chunk（如 `"技术实现,业务逻辑"`）取**最短半衰期**（保守策略：记忆越快衰减越不易形成噪音）。

### 2.2 命中 Boost（写入时）

```
stored_memory = old_memory × decay + boost × (1 - old_memory × decay)
```

公式特点：
- 始终保持 `[0, 1]` 区间
- 越接近 1 增速越慢（边际递减）
- 反复命中 → 逐渐饱和 → 即使长期不用也开始衰减

### 2.3 比例式 boost（防噪音记忆积累）

避免对边缘命中 chunk 也给予满分 boost：

```
actual_boost = base_boost × (score / max_score_in_batch)
```

- top-1 命中拿满 boost · 边缘命中（score 仅略超 threshold）只拿到 10-30%
- 时间长了不会把语义相关的边缘 chunk 的 memory 推上来

### 2.4 多源 boost 叠加

实际 boost 不是单一值，而是按来源分层，再做比例缩放：

```python
def compute_boost(chunk_id, hit_source, base_boost, score_ratio, elapsed_hours):
    boost = base_boost * score_ratio
    decay = math.exp(-lambda_d * elapsed_hours)
    return old_memory * decay + boost * (1 - old_memory * decay)

base_boost_map = {
    "learn_new": 0.30,
    "hook_confirm": 0.20,
    "scan_cross_validate": 0.15,
    "retrieval_hit": 0.10,
}
```

### 2.5 记忆表清理

长期未命中的 chunk 即使衰减到近乎 0 也应从表里移除：

```
DELETE FROM kb_chunk_memory
WHERE memory_value < 0.001
  AND (last_hit_at IS NULL OR last_hit_at < datetime('now', '-30 days'))
```

每次更新记忆时顺带执行一次轻量清理（LIMIT 100），避免表无限膨胀。

---

## 三、树形切片与检索

### 3.1 树形切片策略

**标题树优先，chunk_size 仅做安全阀。** 每个标题下的内容尽量保持完整，不因字符数强制切分。采用内置正则解析器，删除 `markdown-chunker` 依赖。

```
解析 md → 构建标题树 (H1 > H2 > H3...)
    → 每个叶子 section 独立成一个 chunk（标题边界 = 硬边界，不再跨 section 合并）
    → chunk_size 只在单 section 内容过长时触发拆分：
        section 内容 > chunk_size × 2 → 按 list 项边界拆分
        否则 → 整个 section 一个 chunk
    → 每个 chunk 记录 heading_path
```

**超长 section 拆分规则（四级级联，块级元素保护优先）：**

```
块级元素不可拆分原则：
    代码块(```)、引用块(>)、表格(|...|)、HTML 原生块 保持完整
    若单个块级元素 > chunk_size → 报错，提示用户优化源文档
    （块级元素截断会导致语义损坏，宁可拒绝处理）

Markdown 格式保留原则：
    拆分后的每个 chunk 保留完整 Markdown 语法（标题、粗体、列表层级、
    行内代码、链接等），不做 plain text 降级。embedding 模型能理解
    Markdown 结构，保留格式有助于语义表达。

section 内容 > chunk_size × 2 时：
    1. 非 list 内容（引言、总结等段落）独立成 chunk，共享 heading_path
    2. 按顶层 list 项拆分，逐个累积到接近 chunk_size，每一组为一个 chunk
       （每个 list 项内部 Markdown 格式保持完整）
    3. 单个 list 项仍超长（> chunk_size）→ 按段落边界（空行）拆分
    4. 单个段落仍超长 → 字符级滑动窗口截断（chunk_size 步长，保留 overlap）
    5. 拆分后的所有子 chunk（含非 list 内容和 list 组）保留同一 heading_path
       检索时通过 (doc_id, heading_path) 捞出全部兄弟 chunk，按 chunk_index 还原
```

**拆分示意：**

```
## Section X                             Chunk N:   heading_path=[..., "Section X"]
引言段落（200字）                                    内容 = 引言 (独立 chunk，不被 list 稀释)
                                          Chunk N+1: heading_path=[..., "Section X"]
- Item 1（600字）                                    内容 = Item 1+2 (list 组 1)
- Item 2（500字）                                   
                                          Chunk N+2: heading_path=[..., "Section X"]
- Item 3（400字）                                    内容 = Item 3+4 (list 组 2)
- Item 4（400字）
                                           Chunk N+3: heading_path=[..., "Section X"]
总结段落（100字）                                    内容 = 总结 (独立 chunk)
```

**示例**（chunk_size=800）：

```
# 积分流水页面                    Chunk 0: heading_path=["积分流水页面"]
  - 元数据(120字)                        内容 = 元数据 (120字)
                                         ├─ 短 section，独立 chunk
  ## 技术实现                       Chunk 1: heading_path=["积分流水页面", "技术实现"]
  - PullDownRef(80字)                       内容 = 技术实现 (330字)
  - LoginCheck(150字)                       ├─ < 800×2，整个章节一个 chunk
  - 目录结构(100字)
                                         Chunk 2-a: heading_path=["积分流水页面", "业务逻辑"]
  ## 业务逻辑                              内容 = list项1..N (约占 700 字)
  - 登录判断(1200字)                        Chunk 2-b: heading_path=["积分流水页面", "业务逻辑"]
  - 积分规则(200字)                         内容 = list项N+1..M (约占 700 字)
  - 数据模型(300字)                         ├─ > 1600，按 list 项边界拆成两个 chunk
  - 接口说明(400字)
  - 错误处理(150字)
```

**`.learn.md` 的表现：**
- 当前模板（H2 分隔）：3 个短 chunk（元数据、知识摘要、原话摘录）
- 新模板（粗体代替 H2）：1 个 chunk（全文），超过 1600 字才会在 list 边界拆
- 无论哪种，检索时分片合并确保返回完整文档

**heading_path 存储**：`kb_chunks` 新增 `heading_path TEXT DEFAULT ''`，存 JSON 数组。

**heading_path 用于分片整合**：超长 section 拆出的多个子 chunk 共享同一 `heading_path`，检索命中任一个时，通过 `(doc_id, heading_path)` 捞出所有兄弟 chunks，按 `chunk_index` 排序即可拼接还原完整 section。

### 3.2 检索时的树形展开

### 3.2 检索时的树形展开

命中 chunk 后补齐层级上下文：

| 命中情况 | 展开行为 |
|----------|----------|
| 命中含 H2 子标题的 chunk | 结果前缀 `积分流水页面 > 业务逻辑` + chunk_text |
| 命中仅 H1 的 chunk | 无需前缀 |
| 用户 query 匹配到父标题词 | 连带提升所有子级 chunk 的 score（parent_bonus） |

**parent_bonus**：query 关键词命中 chunk 的 `heading_path[0]`（H1 标题）时，该文档下所有 chunk 获得 0.05 权重加成。`heading_path[1]`（H2 标题）命中时该 H2 下的 chunk 获得 0.10 加成。

### 3.3 与分片合并的协同

两者互补，分片合并优先：

```
KNN 候选 → 树形加权 (parent_bonus，不改候选池)
    → 分片合并 (同 doc_id chunks 拼接为一个完整结果，score 取最高分)
    → 合并后提取 heading_path[0] 作为展示前缀
    → 三路融合排序 + 分类加权
    → rerank / threshold / 返回
```

对 `.learn.md`（只有 H1）：合并 = 全文。对普通长 md：按章节粒度加权 + 合并返回。

---

## 四、检索配合

### 4.1 多路融合排序

当前 `score = vector_score × semantic_weight + keyword_score × keyword_weight`

改后 `score = vector_score × semantic_weight + keyword_score × keyword_weight + memory_score × memory_weight`

在此基础上叠加**分类加权**：

```
query_categories = detect_categories(query)   # 用 BM25 tokenizer 匹配分类名称
overlap = len(set(chunk.categories) & set(query_categories)) / max(len(query_categories), 1)
category_multiplier = 1.0 + category_bonus × overlap
final_score = score × category_multiplier
```

查询分类检测走轻量路径：query 分词后与所有类别名做关键词匹配，命中即算该分类涉及，不需要额外 API 调用。

权重归一化（总和 = 1），默认：
- 语义分 0.9、关键词分 0.0、记忆分 0.1 → 归一化后 0.9 / 0.0 / 0.1

### 4.2 分片关联（合并返回）

同一个文档被切片后，不同 chunk 之间语义关联断裂。解决方式不是在切片层面优化，而是在检索阶段做**同文档 chunk 合并**：

```
query → KNN 候选池
    → 对每个候选 chunk: 查同 doc_id 的所有兄弟 chunks（一次 SQL IN 查询）
    → 命中 chunk + 兄弟 chunks → 按 chunk_index 排序拼接为一个完整文档
    → 合并后的完整文档作为 retrieval result
    → score 取所有兄弟 chunk 中的最高分（代表该文档与 query 的匹配度）
    → 后续 rerank / threshold / 返回 均基于合并后的结果
```

**合并批次处理：**
- 多个候选 chunk 可能属于同一文档 → 去重合并为一条结果
- KNN `limit = top_k × 6` 的候选池足够覆盖同文档的所有 chunks

**好处：**
- 不改切片器、不改模板（H2 保留无影响）
- learn 文档和普通 md 统一处理
- 用户检索到的永远不会是截断片段
- 记忆 boost 按文档维度：合并后的 score 用于所有兄弟 chunk 的 memory 更新

### 4.3 候选池策略（一次 KNN 查询）

不做两次 SQL 查询，改用**一次扩大 KNN 候选池**：

```
KNN limit = max(12, top_k × 6)
```

取回更多候选后，在 Python 侧做三路融合排序 —— 记忆值高的自然排序靠前。效果一致，少一次 SQL。

### 4.4 score_threshold 豁免（独立判断，不修改 score）

豁免逻辑不依赖 rerank 后的 score（rerank 后 score 已不含记忆值），而是独立判断：

```
对每个候选 chunk：
  if memory_value > 0.5:
      直接放行（被多次验证过有价值，即使 rerank 分略低也保留）
  else:
      走正常 score >= score_threshold 过滤
```

豁免是二值开关（过/不过），不修改 score 本身，避免和 rerank 分产生语义冲突。

### 4.5 完整检索链路

```
query → embedding → sqlite-vec KNN (limit = top_k × 6)
    → [分片关联] 对每个候选 chunk 查同 doc_id 兄弟，合并去重
    → 三路融合排序 (含 memory_score, 取兄弟 chunk 最高分)
    → 分类加权
    → 取 top_candidates → 可选 rerank (替换 score)
    → score_threshold 过滤 (含独立 memory 豁免)
    → 返回 top_k 个合并后的完整文档
    → 更新记忆表 (对通过过滤的 hits 的所有兄弟 chunk 按比例 boost)
```

注意：
- **memory_score 只在第一阶段排序中起作用**，不参与 rerank 后的 score
- **score_threshold 豁免独立于 score 判断**，直接看 `memory_value > 0.5`
- **boost 按比例缩放**：`actual_boost = base_boost × (score/max_score_in_batch)`
- **合并后 score 取兄弟 chunk 的最高分**，rerank 针对合并后的完整文本

### 4.6 缓存策略

- **query embedding 缓存**：照常使用（128 条 LRU）
- **query 结果缓存**：`memory_enabled=true` 时不缓存，因为记忆值随时间衰减会导致 stale score

### 4.7 重建时的记忆迁移

rebuid 时 chunk_id 可能漂移（`path::index::content_hash` 中 `index` 变化），需要做映射迁移：

```python
# 重建前：保存 old_chunk_id → (file_name, chunk_index, content_hash)
# 重建后：用 (file_name, new_chunk_index, content_hash) 匹配
# 匹配成功的：将 memory 记录从 old_chunk_id 迁移到 new_chunk_id
# 匹配失败的：丢弃（chunk 内容已变，旧记忆无意义）
```

### 4.8 可观测性

`/api/markdown-kb/health` 中新增记忆状态指标：

| 指标 | 说明 |
|------|------|
| `memory_chunk_count` | 当前有记忆记录的 chunk 总数 |
| `memory_avg_value` | 所有记录的平均记忆值 |
| `memory_high_value_count` | memory_value > 0.5 的 chunk 数（会被豁免） |
| `memory_total_hits` | 累计命中次数总和 |

---

## 五、.learn.md 文档模板改进

### 5.1 问题

当前模板用 `## 知识摘要`、`## 关键原话摘录` 等 H2 标题，导致 markdown-chunker 按标题切分成 2-3 个独立 chunk。检索时可能只命中其中一部分，信息不完整。

### 5.2 改进方案

**一个知识点一个 chunk**，通过模板结构调整实现，不改切片器：

1. `##` 标题 → `**粗体**`，不再触发 heading 级分片
2. 新增 `**关联关键词**` 元数据行

### 5.3 新模板格式

```markdown
# {title}

**关联关键词**：{keywords}

**知识分类**：{categories}

- 来源：{source_label} | 触发阶段：{trigger_phase} | 工作区：{workspace_root}
- 会话 ID：{session_id} | 记录时间：{timestamp}

---

**知识摘要**

{summary_markdown}

**关键原话摘录**

- "{quote_1}"
- "{quote_2}"
```

示例：

```markdown
# 积分流水页面下拉刷新与登录态判断

**关联关键词**：积分流水, 下拉刷新, onPullDownRefresh, 登录判断, uniapp, pages.json

**知识分类**：技术实现, 业务逻辑

- 来源：Codex | 触发阶段：stop | 工作区：/Users/nk/Desktop/project/bonnie-clyde
- 会话 ID：019f1c8c-... | 记录时间：2026-07-01T07:24:00Z

---

**知识摘要**

pages/member/integralwater/ 是积分流水页面。实现要点：

- 使用 `onPullDownRefresh` 生命周期，pages.json 配置 `enablePullDownRefresh: true`
- 刷新前检查 `checkLoginStatus()`，未登录时 `uni.showModal` 引导跳转 `/pages/login/index`
- 登录后调积分流水接口，`uni.stopPullDownRefresh()` 停止动画

**关键原话摘录**

- "下拉刷新需要判断是否登录，登录才会刷新，没有登录弹窗去登录"
- "在 pages.json 中配置 enablePullDownRefresh: true"
```

### 5.4 关键词的作用

### 5.5 模型 prompt 改动

`_generate_learn_summary` 的 system prompt 新增 `keywords` + `categories` 字段：

```
JSON 字段固定为：
should_learn(boolean),
title(string),
categories(array[string], 从以下类别中选择一个或多个: 技术实现、业务逻辑、架构设计、调试修复、配置部署、代码风格),
keywords(array[string], 3-8个核心中文/英文关键词，用于增强检索匹配度，提取文中核心概念和技术名词),
summary_markdown(string, ...),
quotes(array[string], ...)
```

---

## 六、架构分层

**核心原则：优先只改插件内部。** 所有功能优先实现在 `akm/plugins/markdown_kb/` 内，仅在插件内部确实无法获取所需数据时，才向外扩展 hooks、CLI 入口或 API 路由。

客户端 hooks 只管事件入口和触发，核心逻辑全部在服务端插件内统一处理。

```
┌─────────────────────────────────────────────────────┐
│  Codex hooks                          Claude hooks  │
│  .codex/hooks/                        .claude/hooks/ │
│    stop.py         event 入口           stop.py      │
│    pre_compact.py  不同                  pre_compact.py
│         │                                    │       │
│         └────→ markdown_kb_hook_common.py ←────┘   │
│                  (字段解耦: detect_* 多字段宽松提取)│
│                           │                        │
│              调 akm markdown-kb-hook CLI            │
│              POST /api/markdown-kb/learn            │
└───────────────────────────┬────────────────────────┘
                            │
┌───────────────────────────┴────────────────────────┐
│  akm/plugins/markdown_kb/     ← 两种客户端共用       │
│                                                     │
│  session_scanner.py          ← 新增                  │
│    parse_codex_jsonl()        Codex JSONL 解析       │
│    parse_claude_jsonl()       Claude JSONL 解析      │
│    normalize_session()    →  {session_id,cwd,source, │
│                                turns[{role,text}]}   │
│    scan_sessions()            编排: 遍历→解析→learn  │
│                               → cross_validate→dedupe│
│                                                     │
│  memory_store.py              ← 新增 (或内置         │
│    read_memory_map()            SqliteKbIndexStore)  │
│    update_memory()             记忆表 CRUD           │
│    cleanup_expired()                                 │
│                                                     │
│  learn.py                     ← 现有                 │
│    learn()                     /learn 接口           │
│    _generate_learn_summary()   chat 归纳+关键词       │
│    _render_learn_document()    新模板                │
│                                                     │
│  index.py                     ← 现有                 │
│    _retrieve()                 集成 memory_score     │
│    自动整理触发                 3 路融合排序           │
│    on_request()                记忆豁免              │
│    health()                    新增 memory 指标      │
└─────────────────────────────────────────────────────┘
```

**差异只在两个地方：**
- **hooks 层**：`markdown_kb_hook_common.py` 已有 `detect_*` 多字段宽松提取，Codex/Claude 各自适配 stdin 事件格式
- **Scanner 层**：`session_scanner.py` 的 `parse_codex_jsonl` / `parse_claude_jsonl` 适配 JSONL 格式差异

**其余全部共享**：learn 归纳、memory boost、dedupe、两阶段原子写入、记忆值算法、三路融合排序。

---

## 七、Session Scanner

### 8.1 目标

绕过 hooks 直接读取本地 JSONL 会话文件，完成知识生成和记忆更新。**默认关闭**，通过配置启用后仅手动 API 调用，不自动触发。

### 7.2 Session 文件格式（两种客户端统一适配）

#### Codex

`~/.codex/sessions/YYYY/MM/DD/rollout-{时间}-{uuid}.jsonl`

```json
{type: "session_meta", payload: {session_id, cwd, cli_version, git}}
{type: "response_item", payload: {type: "message", role: "user", content: [{type: "input_text", text: "..."}]}}
{type: "response_item", payload: {type: "message", role: "assistant", content: [{type: "output_text", text: "..."}]}}
```

#### Claude Code

`~/.claude/projects/{project-name}/{uuid}.jsonl`

```json
{type: "user", message: {role: "user", content: "纯字符串"}, sessionId, cwd, uuid, parentUuid}
{type: "assistant", message: {role: "assistant", content: [{type: "text", text: "..."}]}, sessionId, cwd, uuid}
{type: "system", subtype: "turn_duration", ...}
```

#### 统一提取层

Scanner 内部做格式检测后统一映射为通用结构：

```python
{
    "session_id": str,        # Codex: session_meta.session_id / Claude: user.sessionId
    "cwd": str,               # Codex: session_meta.cwd / Claude: user.cwd
    "source": "codex" | "claude_code",
    "turns": [
        {"role": "user", "text": "..."},      # 归一化后的纯文本
        {"role": "assistant", "text": "..."},
        ...
    ]
}
```

content 归一化规则：
- Codex `content` 是 `[{type: "input_text"/"output_text", text: "..."}]` → 拼接所有 text
- Claude `content` 是 `"纯字符串"`或 `[{type: "text", text: "..."}]` → 优先取字符串，否则拼接

### 7.3 触发策略

**仅手动调用**，不做自动触发。日常推荐使用**自动整理记忆**（第八章）：

```
POST /api/markdown-kb/scan-sessions
{
  "since_hours": 168,
  "max_sessions": 20,
  "learn_enabled": true,
  "memory_enabled": true
}
```

同时保留手动 API 用于首次冷启动批量扫描。

### 7.4 API 设计

```
POST /api/markdown-kb/scan-sessions
```

请求体：

```json
{
  "since_hours": 24,
  "max_sessions": 5,
  "learn_enabled": true,
  "memory_enabled": true
}
```

### 7.5 扫描流程

```
1. 列举 Codex sessions:  ~/.codex/sessions/ 下 mtime 在 since_hours 内的 *.jsonl
2. 列举 Claude sessions: ~/.claude/projects/*/ 下 mtime 在 since_hours 内的 *.jsonl
3. 合并并按 mtime 排序，取 max_sessions 个

4. 逐文件解析（自动检测格式）：
   a. 提取 session_id, cwd, source 类型
   b. 提取 user turns (type=user / role=user)
   c. 提取 assistant turns (type=assistant / role=assistant)

5. 查重：已处理过的 session_id 跳过

6. 知识生成（learn_enabled）：
   a. 将 user + assistant turns 拼成对话摘录
   b. 调用 chat 模型归纳 → JSON {should_learn, title, keywords, summary_markdown, quotes}
   c. should_learn=true 时生成 .learn.md → 重建该文件索引
   d. 新 chunk 初始记忆值 = 0.30

7. 记忆更新（memory_enabled）：
   a. 对 session 中所有 user question 做 embedding
   b. 在知识库中检索匹配 chunks（排除本次新生成的 chunk_ids）
   c. 对匹配到的 chunk 执行交叉验证 boost (0.15, 比例缩放)

8. 两阶段原子写入：
   a. learn 成功后立即写入 dedupe（标记 memory_updated=false）
   b. memory 完成后更新为 memory_updated=true
```

### 7.6 已处理记录

`~/.akm/markdown_kb/scanned_sessions.json`：

```json
{
  "codex:019f1c8c-...": {
    "scanned_at": "2026-07-01T12:00:00Z",
    "learned": true,
    "memory_updated": true,
    "doc_count": 1,
    "boosted_chunks": 5
  },
  "claude_code:0245c0e0-...": {
    "scanned_at": "2026-07-01T12:30:00Z",
    "learned": false,
    "memory_updated": true,
    "boosted_chunks": 3
  }
}
```

dedupe_key = `{source}:{session_id}`，Codex 和 Claude 各自的 session_id 天然不会冲突。

---

## 八、自动整理记忆

### 8.1 目标

替代 Scanner 的手动触发，在后台自动维护记忆：
- 定期扫描 sessions 生成知识
- 对存量文档交叉验证，更新记忆值
- 清理过期记忆

### 8.2 两种触发机制

| 触发方式 | 配置键 | 默认值 | 说明 |
|----------|--------|--------|------|
| 消息计数 | `organize_message_threshold` | 50 | 每收到 N 条新用户消息后触发 |
| 定时周期 | `organize_interval_hours` | 24 | 每 N 小时触发一次 |

满足任一条件即触发。计数器持久化在 `~/.akm/markdown_kb/organizer_state.json`。

### 8.3 触发流程

```
每次 _retrieve 被调用时检查：
  ├→ counter.increment()  # 消息计数 +1
  ├→ if counter >= message_threshold → 触发
  └→ if now - last_organize_at > interval_hours → 触发

触发后的执行（异步，不阻塞检索）：
  ├→ scan_sessions(since_hours=24, max_sessions=5, learn_enabled=true, memory_enabled=true)
  ├→ cleanup_expired_memory()
  ├→ reset counter
  └→ 更新 last_organize_at
```

### 8.4 状态持久化

`~/.akm/markdown_kb/organizer_state.json`：

```json
{
  "message_count": 37,
  "last_organize_at": "2026-07-01T08:00:00Z",
  "last_error": null
}
```

---

## 九、边界情况与权衡

| 场景 | 处理方式 |
|------|---------|
| 新 chunk 无记忆记录 | `memory_score = 0`，不影响检索 |
| 清空索引 | 同时清空 `kb_chunk_memory` 表 |
| 全量重建 rebuild | 按 `(file_name, chunk_index, content_hash)` 做 chunk_id 映射迁移记忆值，匹配不到则丢弃 |
| memory_enabled=false | 完全不走记忆链路，行为与当前一致 |
| query 结果缓存 | memory 启用时跳过缓存，避免 stale score |
| 多 embedding 维度不一致 | 仅正常维度 chunk 的 memory 生效 |
| 存量 .learn.md 不包含关键词 | 关键词行为空行，模板兼容 |
| Session 多次扫描 | 按 session_id 幂等去重 |
| 自动整理消息计数器断电 | 从 `organizer_state.json` 恢复 |
| 整理任务执行中再次触发 | 互斥锁（文件锁），跳过重复触发 |
| 分片关联合并后 rerank | 用合并后的完整文本调用 reranker |
| 表无限膨胀 | 每次更新时清理 `memory_value < 0.001` 且超过 30 天未命中的行 |
| 噪音记忆积累 | boost 按 `score/max_score_in_batch` 比例缩放，边缘命中只获少量 boost |
| score_threshold 豁免 vs rerank | 豁免独立于 score 判断，直接看 `memory_value > 0.5`，不与 rerank 分冲突 |

---

## 十、已知缺陷与修复

### 10.1 learn → memory 的 chunk_id 获取问题

`rebuild_file()` 当前返回 `{ok, file_name, chunk_count}` 不含 chunk_id 列表。
learn 生成 .learn.md 后需要知道新 chunk 的 ID 才能写入记忆表。

**修复**：`rebuild_file` 返回值新增 `chunk_ids` 字段（`list[str]`），learn 调用方直接用返回值写记忆。

### 10.2 hooks confirm 时新 chunk 被重复 boost

hooks learn 后：新 chunk 拿初始 0.30 → 用 `summary_markdown` 检索存量 chunks 时，
语义高度相似的新 chunk 也会被匹配回来 → 刚拿 0.30 又被额外加 0.20。

**修复**：交叉验证时**排除本次 learn 新生成的 chunk_ids**，
只 boost `chunk_id NOT IN (learn_chunk_ids)` 的存量 chunks。

### 10.3 后台扫描的原子性

learn 成功、交叉验证失败时，必须确保 learn dedupe 已写入，
下次扫描不会重复生成同一份知识。

**修复**：扫描流程分两阶段写入：
1. learn 阶段成功后**立即写入** dedupe 记录（`session_id → {learned: true, memory_updated: false}`）
2. memory 阶段完成后再更新为 `{learned: true, memory_updated: true}`
3. 下次扫描时看到 `memory_updated=false` 的记录 → 补做一次交叉验证（不重复 learn）

---

## 十一、实现步骤

### 阶段一：记忆基座 + 分类 + 分片关联 + 树形切片

1. `_init_db`：`kb_chunks` 新增 `categories TEXT DEFAULT ''` + `heading_path TEXT DEFAULT ''`，新增 `kb_chunk_memory` 表
2. `SqliteKbIndexStore` 新增 `read_memory_map` / `update_memory` / `cleanup_expired_memory` / `clear_memory` / `get_sibling_chunks`
3. `_split_into_sections` → `_chunk_markdown_tree`：内置标题树解析器（正则 H1-H6），删除 `markdown-chunker` 依赖；超长 section 按 list 项边界拆分
4. `rebuild_file` 返回值新增 `chunk_ids` 字段，重建时从 .learn.md 头部提取 categories 写入 kb_chunks
5. `_normalize_weight_pair` → `_normalize_weights`（两路升级多路）
6. `_settings` 新增 memory 配置项 + category_config + category_bonus
7. `_build_scored_hit` / `_score_documents` 支持 memory + 分类加权 + 分片合并 + parent_bonus
8. `_retrieve` 核心集成：树形加权 → 分片合并 → 记忆融合 → 返回
9. health 接口新增 memory 指标

### 阶段二：文档模板改进

1. `_generate_learn_summary` prompt 新增 `keywords` + `categories` 字段
2. `_render_learn_document` 改用新模板（`**粗体**` + 关键词行 + 分类行）
3. learn 调用方用 `rebuild_file.chunk_ids` 写初始记忆值（排除自身做交叉验证）
4. 存量 `.learn.md` 兼容性

### 阶段三：自动整理记忆

1. 消息计数器 + 定时周期检查器（集成到 `_retrieve` 路径）
2. 状态持久化（`organizer_state.json`）
3. 异步触发编排（`organize()` → scan + cleanup + cross_validate）
4. 两阶段原子写入（learn dedupe 先落盘，memory 后补）

### 阶段四：Session Scanner（手动，默认关闭）

1. Session JSONL 解析器（Codex + Claude Code 双格式自动检测）
2. `POST /api/markdown-kb/scan-sessions` 路由
