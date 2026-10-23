# models.dev 数据接入计划（单价表 / 供应商）

> 状态：口径已确认，待执行 | 无代码改动
> 结论先行：**单价表接入可行（做），供应商数据只做"模型清单 + base_url 建议"（不做协议能力替换）**

---

## 一、已确认口径（本轮拍板）

| # | 决策 | 确定值 |
|---|---|---|
| A | 取价规则 | **三段式**：① key 的供应商存在且名下有该模型 → 取该供应商价；② 供应商不匹配 → 按官方价兜底（开关 `models_dev_official_fallback`，**默认开启**）；③ 两段都失败 → 按 0 |
| B | 默认数据源 | **默认启用 models.dev 快照**（不再是纯手写 `manual`） |
| C | 执行顺序 | **① 数据拉取并入库（只剔 null）→ ② 模型价格适配 → ③ 供应商适配** |
| D | 过滤规则 | **`cost` 存在即保留（`0/0/0/0` 这类全 0 价也留）；`cost` 为 `null` 或缺失才剔除；某供应商下所有模型 `cost` 全为 null → 整条供应商剔除** |
| E | Key 管理页 | **供应商下拉改成可搜索（`akm-combobox`，221 个选项必须能搜）+ 补「自定义…」选项；`base_url` 默认禁用，只有「自定义…」可编辑；存量 key 若手填过非默认地址（如 `gs-codex` → `yunxiang.lol`）则自动识别为自定义态、保持可编辑**（口径 A）；模型输入框保持现状不动 |
| F | `/v1/models` 扩展 | **返回体在 OpenAI 兼容字段之外，追加供应商信息与对应模型信息（价格 / 上下文 / 能力）** |
| G | 存储方式 | **写入 SQLite（`~/.akm/akm.db` 新增表），不再用独立 JSON 快照文件**；费用统计走 SQL JOIN 聚合 + 进程内索引 |
| H | 官方兜底开关 | **`models_dev_official_fallback`，默认 `true`**；关闭后只认严格匹配、其余按 0 |
| I | `/v1/models` 能力过滤 | **接口提供可选参数 `?capability=chat`（默认不带 = 不过滤，向后兼容）**；由调用方（`agent_chat`）显式传参决定，后端不擅自改默认返回 |
| J | 协议能力来源 | **`npm` 只作为「新建自定义供应商」时的初始勾选建议**；内置 3 个以 `BUILTIN_AGENTS` 为准、已有自定义 agent 不动；`/anthropic` 默认永远关，只认手动 |
| K | 换模型检测 | **审计日志记录上游实际返回的 model，与请求 model 不一致时打标记**，防止三方偷偷换模型；从上游**原始响应**提取，不受转换与正文开关影响 |

---

## 二、数据源事实（实测）

| 项 | 值 |
|---|---|
| `https://models.dev/json` | **302 跳首页**，无数据 |
| 真实地址 | `https://models.dev/api.json`（5.87 MB） |
| 备用 | `https://models.dev/catalog.json?type=all`（6.42 MB，多一层 `{"providers":{...}}`） |
| 结构 | `{provider_id: {id, name, api?, doc?, npm, env[], models: {model_id: {cost, limit, canonical_model_id}}}}` |
| 原始规模 | 230 provider / 8850 条模型 / 3763 个唯一已定价 model_id |
| 单位 | 每 1M tokens 的美元 —— **与现有单价表天然一致，零换算** |
| 缓存 | 有 `etag`、`cache-control: public, max-age=0, must-revalidate` → 可条件请求 |
| 成本字段 | `input / output / cache_read / cache_write / reasoning / input_audio / output_audio / tiers / context_over_200k` |

### 2.1 按 D 口径过滤后的规模（实测）

判定「**保留**」= `cost` 是对象且 `cost.input` **不为 null**（`0` 也保留）

| 指标 | 过滤前 | 过滤后 |
|---|---|---|
| provider | 230 | **221**（剔除 9 个） |
| 模型记录 | 8850 | **8457**（剔除 393 条 `cost` 为 null 的） |
| 其中「全 0 价」（`0/0/0/0`）供应商 | — | **36 个**（保留） |
| 入库体积（SQLite + 主键索引） | — | **约 900 KB** |

被剔除的 9 个（所有模型 `cost` 全为 null）：`trustedrouter`、`stepfun-step-plan`、`stepfun-ai-step-plan`、`model-oracle-ai`、`the-grid-ai`、`anyapi`、`liquid`、`blueclaw`、`agentrouter`。

各主流供应商剩余模型数：`openai` 51 / `anthropic` 17 / `deepseek` 4 / `xai` 8 / `google` 34 / `commandcode` 71 / `moonshotai` 4 / `zai` 15 / `minimax` 8 / `siliconflow` 57 / `openrouter` 366。
**`dmxapi` 不在 models.dev 中**（你的中转站 key 需要靠自定义兜底）。

> 与上一版口径（只留 `input > 0`，185 / 7897）相比：多出的 36 个全 0 价供应商与 560 条 0 价模型保留下来了。它们在供应商下拉与 `/v1/models` 元数据里有用；参与计价时按 0 计——费用为 0 是真实结果（免费/订阅渠道），不是漏算。

---

## 三、取价三段式实测效果

对你 30 天 **26797 行**真实日志，按「① 供应商存在且名下有该模型 → ② 供应商不匹配则按官方价兜底 → ③ 都失败按 0」逐条核对：

### 3.1 第一段：供应商 + 模型都命中（16684 行，占 62.2%）

```
10962 次  provider=deepseek     deepseek-flash                    -> deepseek 下 0.15/0.003/0.6
 4443 次  provider=commandcode  deepseek/deepseek-v4.1-flash      -> commandcode 下 0.15/0.003/0.6
  397 次  provider=commandcode  deepseek/deepseek-v4.1-flash-fast -> commandcode 下 0.16/0.016/0.58
  316 次  provider=openai       gpt-6.1-sol                       -> openai 下 2/0.1/10
  312 次  provider=openai       gpt-6-luna                        -> openai 下 0.1/0.01/0.5
   61 次  provider=commandcode  deepseek/deepseek-v4-flash        -> commandcode 下 0.15/0.003/0.6
```

### 3.2 第二段：供应商不匹配 → 官方兜底（7402 行，占 27.6%）

| 次数 | key 的 provider | 模型 | 兜底到 |
|---|---|---|---|
| 3766 | `openai`（实为 yunxiang.lol 中转站） | `grok-4.7` | `xai` 2/0.5/6 |
| 1620 | `dmxapi`（不在 models.dev） | `text-embedding-3-small` | `openai` 0.02/0/0 |
| 1314 | `common`（不在 models.dev） | `glm-5.3-flash` | `zai` 0.15/0.03/0.5 |
| 314 | `openai` | `grok-4.6` | `xai` 2/0.5/6 |
| 71 | `openai` | `deepseek-v4-flash-0731` | `alibaba` |
| 66 | `common` | `gpt-6-luna` | `openai` 0.1/0.01/0.5 |
| 56 | `openai` | `grok-4.5` | `xai` 2/0.5/6 |
| 41 | `(空)` | `claude-opus-5-5` | `anthropic` 4/0.2/20 |

> 根因：这些 key 的 `provider` 名写的是 `openai`，实际 `base_url` 指向中转站，跑的是别家模型。官方兜底正是为这种情况准备的。

### 3.3 第三段：两段都失败 → 按 0（2758 行，占 10.3%）

```
1333 次  provider=common   deepseek/deepseek-v4.1-flash   （带前缀的 id，官方名不含前缀）
 355 次  provider=openai   deepseek-v4.1-flash            （官方 deepseek 名下没有这个变体）
 352 次  provider=dmxapi   bge-m3                          （官方无此 embedding）
 340 次  provider=openai   monk-coding                     （中转站私有别名）
 155 次  provider=common   gemini-3.7-flash-high           （-high 变体后缀）
  77 次  provider=(空)     deepseek/deepseek-v4.1-flash
```

这部分**不再补手写表**（已定口径）：继续算 0 属预期行为，stats 里靠「未匹配计数 / Top 未匹配模型」显式暴露，避免被误读成"免费"。

### 3.4 兜底开关的价值（对比）

| 官方兜底 | 命中率 | 30 天费用 |
|---|---|---|
| **关**（只严格匹配） | 62.2% | **$62.47** |
| **开**（默认） | **89.7%** | **$497.82** |

开关一开，命中率 +27.5 个百分点，救回 7402 行。剩下 10.3% 才需要手写表兜。

---

## 四、⚠️ 成本影响（执行前必须知道）

重算你 30 天 **26797 行**日志（官方兜底默认开启）：

| 方案 | 30 天估算成本 | 相对现状 | 命中率 |
|---|---|---|---|
| ① 现状（纯手写 + `*=0/0/0` 兜底） | **$366.29** | — | — |
| ② `snapshot`：严格 → 官方兜底 → 0（**默认**） | **$497.82** | **+36%** | **89.7%** |
| ③ `snapshot` 但关掉官方兜底 | **$62.47** | **−83%** | 62.2% |
| ④ `hybrid`：手写优先 → 严格 → 官方兜底 → 0 | **$792.99** | **+117%** | 89.7% |

**关键判断**：

- 方案 ② 是默认推荐：官方兜底救回 7402 行（27.6%），只剩 10.3% 归 0，费用 $498 与你的实际账单量级接近。
- 方案 ③ 是"关掉兜底"的对照：命中率掉到 62.2%，费用只有 $62 —— 那不是便宜，是 4 成请求没算钱。
- 方案 ④ 把 `monk-coding`、`deepseek-flash` 等手写条目也叠上来（手写优先），费用升到 $793。差异主要来自手写表里 calibrate 过的 deepseek 价（比官方高 13 倍）。

**手写表与快照冲突的 3 条**（都是 deepseek，偏差 13 倍）：

```
deepseek-flash              手写 2/0.04/8        快照 0.15/0.003/0.6
deepseek/deepseek-v4-flash  手写 0.22/0.007/0.66 快照 0.15/0.003/0.6
deepseek-v4-flash           手写 0.22/0.007/0.66 快照 0.15/0.003/0.6
```

你表里注明了「页面显示 off-peak 价；peak 高一倍」，是特意按账单校准过的中转站价。默认 `snapshot` 下不生效；需要它们生效就切 `hybrid`。

---

## 五、两个必须先解决的硬问题

### 5.1 性能：现有 `match_price` 线性扫描，全量规则不可用

| 场景 | 实测耗时 |
|---|---|
| `parse_pricing`（3763 行） | 6.5 ms —— 可接受 |
| `estimate_row_cost_detail` 线性匹配 | 0.23 ms/行 → **26657 行 ≈ 6.1 秒** |
| 同样数据 dict 精确查找 | 26657 行 **4.8 ms** |

`_get_stats` 要遍历整个窗口，stats 缓存 TTL 仅 60 秒 → 每分钟卡 6 秒。

**必须做**：`akm/cost_estimate.py` 增加索引化匹配，且**语义与线性严格等价**。等价性要点（易错）：现有语义是「按书写顺序首个命中」，所以 `gpt*=1/1/1` 写在 `gpt-5=2/2/2` 之前时，`gpt-5` 命中的是通配符。

实现：`build_price_index(rules)` → `(exact: dict[(provider,model)] → (seq, prices), wildcards: list[(seq, prefix, prices)], fallback)`；匹配时精确命中与通配命中比 `seq`，取较小者。`estimate_row_cost_detail` 检测到索引对象走索引路径，否则维持线性（兼容 `budget_gate` 现有调用）。

### 5.2 存储：不写 `config.json`，写 SQLite 表

**不写 config 的理由**：221 个供应商 / 8457 条模型若按现有文本格式展平约 **300 KB**，而 `~/.akm/config.json` 现在仅 11 KB；`load_config()` 在 5 处热路径被调用（每请求多次），设置页 textarea 要渲染几十万字符；且严格匹配需要 `provider → model` 二维结构，文本表表达不了。

**改存 SQLite 的实测依据**（26797 行真实日志，建表 + 插入 8457 行仅 0.019 s）：

| 查询方式 | 耗时 | 说明 |
|---|---|---|
| 逐行 `SELECT`（每行一条 SQL） | **0.26 s** | 比内存慢约 15 倍，热路径不可用 |
| 一次性载入内存 dict（0.009 s）+ 逐行查找 | **0.018 s** | 逐行场景最快 |
| **纯 SQL JOIN 聚合**（费用直接算在 SQL 里） | **0.021 s** | 一条 SQL 出总费用，代码最简 |
| 表体积（8457 行 + 主键索引） | **900 KB** | `akm.db` 现 22 MB，可忽略 |

**结论**：DB 的优势不是「单条查找更快」（单条查找内存 dict 快 15 倍），而是：

1. **SQL JOIN 聚合** —— 2.6 万行费用可一条 SQL 算完（0.021 s），与 `audit_logs` 同库直接 JOIN，代码最简；
2. **增量同步** —— 按 provider 局部 upsert / 删除，不必重写整个 JSON；
3. **事务安全 + 可查询** —— 同步失败不影响旧数据，CLI / 插件 / 菜单栏都能直接查。

⇒ 架构定为：**入库 `~/.akm/akm.db`（新增两张表）+ 统计走 SQL 聚合 + 明细场景走进程内索引**。

---

## 六、目标架构

```
models.dev/api.json (5.9 MB)
      │  ETag 条件请求（304 不下载）
      ▼
【步骤① 数据拉取并入库】
  过滤（D 口径）：cost 非 null 即留（含全 0）；全 null 的供应商整条剔除
      ▼
~/.akm/akm.db  （新增两张表，事务内替换）
  models_dev_providers(id PK, name, api, doc, synced_at)
  models_dev_models(provider_id, model_id,
                    input, cache_read, cache_write, output,
                    context, max_output, modalities, family, release_date,
                    reasoning, tool_call, canonical_model_id,
                    PRIMARY KEY(provider_id, model_id))
  models_dev_meta(key, value)   -- etag / fetched_at / provider_count / model_count
  规模：221 供应商 / 8457 模型 / 约 900 KB
      ▼
【步骤② 模型价格适配】
  取价规则（A 口径，三段式）：
    1. key.provider 在 models_dev_providers 存在，且其名下能按名称匹配到该模型
       （全名 → 去 provider/ 前缀 → 去日期后缀）→ 取该供应商该模型的价（0 也照用）
    2. 第 1 段不匹配 且 models_dev_official_fallback=true（默认）
       → 按 models_dev_prefer_providers 顺序在原厂供应商里找同名模型取价
    3. 两段都失败 → 该笔按 0
  （可选层）cost_pricing_source=hybrid 时，手写表优先于以上三段；
  =manual 时只用手写表现状逻辑。默认 snapshot。
      ▼
  取数方式：统计走 SQL JOIN 聚合（0.021 s / 2.6 万行）；
           明细与 /v1/models 走进程内 dict 索引（0.018 s / 2.6 万行）
      ▼
【步骤③ 供应商适配】
  - 供应商下拉 = akm-combobox（可搜索），数据源 = models_dev_providers（221）+ 内置 + 自定义 agents
  - 补「自定义…」选项；选中它才解锁 base_url 与 provider 名
  - base_url 默认 disabled，显示该供应商默认地址；老 key 手填过则自动切自定义态
  - 模型输入框**保持现状**（自由文本 / `*`），不做下拉改造
  - 不覆盖 BUILTIN_AGENTS / custom_agents 的协议能力字段
      ▼
【步骤④ /v1/models 扩展】
  - 顶层 id / object / owned_by 保持不变（OpenAI 兼容）
  - 新增字段**直接平铺在顶层**：name / context_length / modalities / pricing / provider
    （与 OpenRouter /models 惯例一致，不做命名空间包裹）
  - 沿用 first-wins 去重：一个模型只报一个 provider，与顶层 owned_by 一致
  - **能力过滤**：接口提供可选参数 `?capability=chat`，由调用方（agent_chat）传参；
    不传 = 不过滤（插件配置页仍需全量选 embedding 模型）
```

### 6.1 新增配置项（`akm/config.py` DEFAULTS）

| key | 默认 | 说明 |
|---|---|---|
| `cost_pricing_source` | `"snapshot"` | `snapshot`（**新默认**，三段式取价）/ `hybrid`（手写优先 → 三段式）/ `manual`（现状，纯手写） |
| `models_dev_official_fallback` | `True` | **官方兜底开关（默认开启）**：严格匹配失败时按原厂价兜底；关闭则直接按 0 |
| `models_dev_prefer_providers` | `"openai,anthropic,deepseek,google,xai,moonshotai,zai,minimax,alibaba"` | 官方兜底的查找顺序（只在开关开启时生效） |
| `models_dev_sync_enabled` | `True` | 后台自动刷新开关 |
| `models_dev_sync_interval_hours` | `24` | 自动刷新间隔 |

---

## 七、分阶段任务（按你指定的顺序）

### 步骤①：数据拉取并入库（只剔 null）

| # | 任务 | 文件 | 验证 |
|---|---|---|---|
| 1.1 | 新建模块：ETag 条件请求、超时、失败降级；拉取失败时保留旧库数据不动 | 新增 `akm/models_dev.py` | 离线 / 404 / 坏 JSON 时不抛异常、不影响启动，旧数据仍在 |
| 1.2 | 过滤：`cost` 为 `null` 或缺失的模型跳过（**`0` 保留**）；某供应商下所有模型都被跳过 → 整条供应商剔除 | `akm/models_dev.py` | 结果 = **221 provider / 8457 模型**，与实测一致；36 个全 0 价供应商必须保留 |
| 1.3 | 建表 + 迁移：`models_dev_providers` / `models_dev_models` / `models_dev_meta` 三张表纳入 `init_db`（沿用现有 `_migrate_*` 风格，`CREATE TABLE IF NOT EXISTS`） | `akm/db.py` | 老库启动自动建表；重复执行无副作用 |
| 1.4 | 入库：单事务内 `DELETE` + `executemany INSERT` 替换全量（8457 行约 0.02 s），失败自动回滚保留旧数据；meta 记 `etag / fetched_at / provider_count / model_count` | `akm/models_dev.py` | 中断写入后旧数据完整；重复同步不产生重复行 |
| 1.5 | 进程内索引：从表一次性载入 `{(provider_id, model_id): (input, cache_read, output)}`，按 `models_dev_meta.fetched_at` 失效（载入 0.009 s） | `akm/models_dev.py` | 同步后索引自动重建；100 次连续查询耗时稳定 |
| 1.6 | CLI / 接口：`akm pricing sync` + `POST /api/pricing/sync`（返回条数与剔除统计） | `akm/cli.py`、`akm/server.py` | 304 时不重建表；返回体含 provider/model 计数与 skipped 数 |

### 步骤②：模型价格适配

| # | 任务 | 文件 | 验证 |
|---|---|---|---|
| 2.1 | 索引化匹配（语义等价） | `akm/cost_estimate.py` | 等价性单测：随机 200 规则 × 200 模型，索引结果 == 线性结果 |
| 2.2 | **第一段：严格取价**（provider 存在 → 该 provider 下命中模型 → 取价） | `akm/models_dev.py`、`akm/cost_estimate.py` | 第三节 3.1 命中样本逐条对齐 |
| 2.3 | **第二段：官方兜底 + 开关**：`models_dev_official_fallback`（默认 `True`）为真时按 `models_dev_prefer_providers` 顺序找原厂价；为假直接跳到第三段 | `akm/models_dev.py` | 开关切 true/false 时，3.2 的 7402 行分别命中/归 0 |
| 2.4 | 模型名匹配阶梯（两段共用）：全名 → 去 `provider/` 前缀 → 去日期后缀 | `akm/models_dev.py` | 官方兜底段不跨原厂列表外的供应商借价 |
| 2.5 | 统一入口 `resolve_rules(cfg)`，替换 5 处消费点 | `cost_estimate.py`、`server.py`(2 处)、`agent_runtime/tools.py`、`plugins/budget_gate/index.py` | `cost_pricing_source=manual` 时与改前逐字节一致 |
| 2.6 | **`_get_stats` 改走 SQL JOIN 聚合**：`audit_logs` 先 LEFT JOIN 本供应商，再 LEFT JOIN 兜底供应商，费用与未匹配行数都在 SQL 里算（实测 0.021 s） | `akm/server.py` | 与逐行 Python 结果一致（误差 < 1e-6）；30 天 < 100 ms |
| 2.7 | 明细场景（`/api/logs` 单页、`/v1/models`、`budget_gate`）用进程内索引（两个 dict：严格索引 + 官方索引），不逐行查库 | `akm/server.py`、`plugins/budget_gate/index.py` | 逐行查库（0.26 s）与索引（0.018 s）差距不得回归 |
| 2.8 | 未匹配计数：stats 暴露「未匹配请求数 / 占比 / Top 未匹配模型」，避免费用静默归 0 | `akm/server.py` | 默认口径（兜底开）应显示 2758 行 / 10.3% |
| 2.9 | 性能回归 | — | 26797 行 30 天统计 < 200 ms |

### 步骤③：供应商适配（可搜索下拉 + base_url 联动；模型输入框不动）

| # | 任务 | 文件 | 验证 |
|---|---|---|---|
| 3.1 | `GET /api/pricing/providers`：返回过滤后的 185 个有价供应商（id/name/doc/api/有价模型数） | `akm/server.py` | 只返回有价供应商，顺序稳定 |
| 3.2 | **新增可搜索下拉组件 `akm-combobox`**（原生 `<select>` 装不下 221 个供应商）：输入框 + 过滤列表，支持按 `id`/`name` 模糊匹配、↑↓ 键选择、Enter 确认、Esc/点击外部关闭、选中后回显名称 | `akm/static/akm-ui.js` | 输入 `deep` 只剩 deepseek 系；键盘可完整操作；无匹配时显示"无匹配供应商" |
| 3.3 | **Key 管理页 · 供应商换成 `akm-combobox`**：数据源 = `BUILTIN + custom_agents + models.dev 有价供应商 + 「自定义…」`，按「内置 / 自定义 / models.dev」分组展示 | `akm/templates/keys.html` | 221 个选项下可搜索；分组标题不参与搜索匹配 |
| 3.4 | **base_url 联动禁用**：默认 `disabled`（灰色只读，显示该供应商的默认地址）；**只有选中「自定义…」时才 `disabled=false`** | `akm/templates/keys.html` | 选内置/models.dev 供应商时无法编辑 base_url；选「自定义…」后立即可编辑 |
| 3.5 | **老 key 编辑时的兼容**：若回显的 `base_url` 与该 provider 默认值**不同**（说明历史上手填过中转站地址，如 `gs-codex` → `yunxiang.lol`），自动切到「自定义」模式以保持可编辑，避免锁死 | `akm/templates/keys.html` | 打开 `gs-codex` 能继续改 base_url；打开 `gs-ds`（等于默认值）时是禁用态 |
| 3.6 | 设置页：数据源单选（snapshot / manual / hybrid）+ 快照状态（条数、更新时间、剔除供应商）+ 「立即同步」 | `akm/templates/settings.html` | 切换数据源后 stats 立即变化 |

#### 3.7~3.8 协议能力（Chat / Responses / Messages / `/anthropic`）怎么来

**结论：`npm` 只能当「新建供应商时的初始建议」，绝不能当真源，也不回写已有配置。**

**反例（你自己代码里就有）**：

```
  akm/agent.py 的 deepseek：supports_responses=True, supports_chat=True,
                            supports_messages=True, messages_use_anthropic_path=True
  models.dev 的 deepseek：  npm = "@ai-sdk/openai-compatible"
```

若按 npm 推导，`deepseek` 只会得到「Chat ✓，其余全 ✗」——把 Responses、Messages、`/anthropic` 三个已经手工验证过、README 也写明的能力全丢掉。原因是 `npm` 只说明「官方 SDK 怎么接这个供应商」，不说明「该供应商的 HTTP 端点到底开了哪些路径」；中转站版本更不可能与官方一致。

**npm 分布（230 个供应商实测）**：

| npm | 数量 | 含义 |
|---|---|---|
| `@ai-sdk/openai-compatible` | **191**（83%） | 走 OpenAI 兼容层，通常只有 `/v1/chat/completions` |
| `@ai-sdk/anthropic` | 8 | 走 Anthropic Messages 协议 |
| `@ai-sdk/openai` | 6 | 官方 OpenAI SDK（含 `openai` 自己） |
| `@ai-sdk/azure` | 2 | Azure 变体 |
| 其余 25 种（google / xai / bedrock / cohere / mistral / groq …） | 各 1 | 各厂商专有 SDK |

**建议的推导表（仅用于新建自定义供应商时的初始勾选）**：

| npm | Chat | Responses | Messages | `/anthropic` |
|---|---|---|---|---|
| `@ai-sdk/openai` | ✓ | ✓ | – | – |
| `@ai-sdk/openai-compatible` | ✓ | – | – | – |
| `@ai-sdk/anthropic` | – | – | ✓ | – |
| `@ai-sdk/google-vertex/anthropic` | – | – | ✓ | ✓ |
| `@ai-sdk/azure` | ✓ | – | – | – |
| 其他 / 缺失 | ✓ | – | – | – |

**落地规则**：

1. **内置 3 个供应商（openai / deepseek / anthropic）的协议能力一律以 `akm/agent.py` 的 `BUILTIN_AGENTS` 为准**，models.dev 与 `npm` 都不参与。
2. **已有自定义 agent 完全不动**（`custom_agents` 里 dmxapi / commandcode / common 的现有值保持）。
3. 只有在「设置页新建自定义供应商」且能从 models.dev 匹配到同名 provider 时，把上表结果作为**初始勾选值**填进表单，用户可改。
4. 表格新增「协议来源」列：`手动` / `建议（models.dev: <npm>）`，让用户一眼看出哪些是猜的。
5. `/anthropic` 开关**默认永远关**，只认手动开启——猜错会让请求打到不存在的 `/anthropic/v1/messages` 直接 404。

| # | 任务 | 文件 | 验证 |
|---|---|---|---|
| 3.7 | `npm` → 协议能力初始值的映射函数 `npm_protocol_hints(npm)` | `akm/models_dev.py` | `@ai-sdk/openai` → chat+responses；`@ai-sdk/openai-compatible` → 仅 chat；`@ai-sdk/anthropic` → 仅 messages；未知 → 仅 chat |
| 3.8 | 设置页新建自定义供应商时预填协议勾选 + 表格加「协议来源」列 | `akm/templates/settings.html`、`akm/server.py` | 新建 `openrouter` 预填「仅 Chat」；`anthropic` 类预填「仅 Messages」；已有 3 内置 + 3 自定义 agent 显示「手动」且值不变 |

**现状核对（重要）**：Key 弹窗的供应商下拉**目前没有「自定义」选项**，只有 `/api/agents` 返回的供应商（3 个内置 + 设置页创建的自定义 agent）；`README.md` 里「保留『自定义』选项用于手工填写 base_url/auth_header」这句与实现不一致，属历史残留。所以 3.3 是**补齐**该入口，而不是保留。

**明确不做（本轮）**：Key 管理页的**模型输入框保持现状**（自由文本 / `*`），不改成下拉。原因：模型数量普遍很多（单个聚合商可达 366 个），改造后再叠加"可搜索"成本翻倍，而现有 `*` + `provider_models` 自动同步机制已够用。

**3.3 / 3.4 的硬约束**（来自现有实现，不能破坏）：
- 供应商下拉现在靠 `/api/agents` 填充，改造后不能断掉现有 `_agentsMap` 的 base_url 自动带入逻辑（选中供应商 → 带入默认地址到 base_url 输入框）。
- base_url 禁用只是 UI 态，**提交时仍要正常带值**（`submitForm` 读 `key-baseurl` 的 value，disabled 不影响 `.value` 读取）。
- `provider_models` 只在 `models='*'` 时由 `{base_url}/models` 同步；显式模型列表时主动清空（`server._resolve_key_save_payload`）——模型输入框不动，这条天然保持。
- `*` 与自定义模型**不能共存**（前端 `/api/keys` 提交前已校验）——同上，不动。

### 步骤④：`/v1/models` 携带供应商与模型信息

**现状**（`akm/server.py` 的 `list_models`，`/v1/models` 与 `/models` 同函数）：

```python
for k in list_keys():
    if k["status"] != "active": continue
    for m in key_model_list(k):
        if m and m not in seen:            # 按模型名去重，先到先得
            data.append({"id": m, "object": "model", "owned_by": k["provider"]})
return {"object": "list", "data": data}
```

已知消费方（都必须继续可用）：

| 消费方 | 用到的字段 | 影响 |
|---|---|---|
| `plugins/agent_chat` 前端 | 只 `.data[].id` | 安全 |
| `plugins.html` 插件配置动态下拉（`options_source=/v1/models`） | 只 `.data[].id` | 安全 |
| `akm/flow/models.py` | 不走 HTTP，直接用 `key_model_list` | 不受影响 |
| 外部 OpenAI 兼容客户端（Codex CLI 等） | 通常只读 `id`/`owned_by` | 需保持标准字段语义不变 |

| # | 任务 | 文件 | 验证 |
|---|---|---|---|
| 4.1 | 返回体增量扩展：**新增字段直接平铺在顶层**（同 OpenRouter `/models` 惯例），只保证 `id`/`object`/`owned_by` 语义一字不改 | `akm/server.py` | 现有 `test_list_models` 通过；`owned_by` 仍为首次命中的 provider |
| 4.2 | **单值结构**：每个模型条目只带**一个** provider 与**一份**模型数据（沿用现有 first-wins 去重语义，不做多来源聚合） | `akm/server.py` | 与现状 `owned_by` 完全一致，不引入新语义 |
| 4.3 | 单价：按步骤②的严格规则（provider 存在 → 该 provider 下命中模型 → 取其价，否则**不返回 pricing 字段**），带 `source` 标记来源 | `akm/server.py` + `akm/models_dev.py` | 与 `/api/stats` 同一口径；未匹配时不编造价格 |
| 4.4 | 模型信息：`name`/`context_length`/`max_output_tokens`/`modalities`/`release_date`/`reasoning`/`tool_call`/`canonical_model_id`，仅快照有才返回 | `akm/server.py` | 快照缺失时该字段整体省略，不返回 null 堆砌 |
| 4.5 | 体积保护：新增 `?detail=full\|basic`（默认 `full`），`basic` 时只回标准三字段 | `akm/server.py` | 366 个模型场景（openrouter）实测体积可控 |
| 4.6 | 缓存：同一快照版本内复用 `model_id → 元数据` 查找表，避免每次请求重算 | `akm/server.py` | 100 次连续调用耗时稳定 |

#### 4.7~4.10 能力过滤（接口提供参数，由调用方决定）

**需求**：`agent_chat` 的模型下拉现在会把 `gpt-image-2`、`text-embedding-3-small`、`bge-m3` 这些不能对话的模型也列出来。做法是**后端提供可选参数，`agent_chat` 显式传参**；`/v1/models` 默认返回不变（向后兼容，插件配置页仍能选 embedding 模型）。

**只看 `modalities.output` 不够**（实测）：embedding 模型的 output 也是 `text`：

| 模型 | `modalities.output` | `family` | `limit.output` | `cost.output` | 对话可用 |
|---|---|---|---|---|---|
| `gpt-5.6-luna` | `["text"]` | `gpt-luna` | 128000 | 1.2 | ✅ |
| `text-embedding-3-small` | `["text"]` ← 骗人 | `text-embedding` | **1536** | **0** | ❌ |
| `bge-m3` | `["text"]` ← 骗人 | `bge` | **1024** | **0** | ❌ |
| `gpt-image-2` | `["image"]` | `gpt-image` | 0 | 30 | ❌ |

**`capability=chat` 的判定规则（按顺序，命中即排除）**：

1. `modalities.output` 不含 `text` → 排除（覆盖图像 / 语音，实测全库 214 个）
2. `family` 命中 `embed|bge|rerank`（不区分大小写）→ 排除
3. `cost.output == 0` 且 `limit.output <= 4096` → 排除（embedding 的向量维度被记进了 max output）
4. **models.dev 查不到该模型** → 退回名字规则：命中 `embed|bge|rerank|image|dall-e|flux|stable-diffusion|whisper|tts` 则排除，否则**默认保留**（宁可多留不可误杀）

**对你当前 5 个"脏"模型的实测结果**：

| 模型 | 排除依据 |
|---|---|
| `gpt-image-2` | 规则 1：`output=["image"]` |
| `gpt-image-2.5` | 规则 4：models.dev 无此模型 → 名字含 `gpt-image` |
| `text-embedding-3-small` | 规则 2：`family=text-embedding` |
| `bge-m3` | 规则 2：`family=bge`（规则 3 同时命中） |
| `bge-reranker-v2-m3-free` | 规则 4：无元数据 → 名字含 `bge` |

其余 21 个 chat 模型（`deepseek-*`、`grok-*`、`monk*`、`glm-5.3-flash` 等）全部保留，无误杀。

**字段来源**：规则需要 `modalities` / `family` / `limit.output` / `cost.output` → 表结构必须含 `family`（models.dev 覆盖率 95.9%，`modalities` 与 `limit.output` 均 100%）。

| # | 任务 | 文件 | 验证 |
|---|---|---|---|
| 4.7 | 表结构补 `family` 列（规则 2 依赖） | `akm/db.py`、`akm/models_dev.py` | 入库后 `family` 非空率 ≈ 95.9% |
| 4.8 | 实现 `is_chat_capable(model_id, meta)` 四段判定（meta 查不到走名字规则） | `akm/models_dev.py` | 上表 5 个模型全部排除；`deepseek-*`/`grok-*`/`monk*` 全部保留 |
| 4.9 | `/v1/models` 增加**可选**参数 `?capability=chat`；**不传 = 完全不过滤**（与改造前逐条一致） | `akm/server.py` | 不传参与今日返回完全相同；传 `chat` 时不含 image/embedding |
| 4.10 | `agent_chat` 前端 `fetchModels()` 改为请求 `/v1/models?capability=chat` | `/Users/nk/Desktop/Ecology/chat/src/lib/agent-api.ts`（独立仓库） | 模型下拉无 image/embedding |
| 4.11 | 重建插件产物并同步两处 dist：`VITE_BASE='./' VITE_AKM_API_URL='' npm run build` → `ccs/plugins/agent_chat/dist` 与 `~/.akm/plugins/agent_chat/dist` | `plugins/agent_chat/dist` | `grep -c 'akm-api' dist/assets/*.js` = 0；`dist/index.html` 引用为 `./assets/...`；产物内含 `capability=chat` |

**参数命名说明**：不用 `?modalities=text`，因为判定不只看 `modalities`（还依赖 `family` / `limit.output` / `cost.output`），叫 `capability=chat` 才名实相符。后续如需按 embedding / image 单独筛，扩展 `capability=embedding|image|all` 即可。

**其他调用方不受影响**：`plugins.html` 的插件配置下拉（`options_source=/v1/models`）、外部 OpenAI 兼容客户端都请求无参 URL → 保持全量。

**目标返回体（示意）** —— provider / 模型信息 / 单价都是**单值、顶层平铺**：

```json
{
  "id": "gpt-5.6-luna",
  "object": "model",
  "owned_by": "openai",
  "name": "GPT-5.6 Luna",
  "context_length": 1050000,
  "max_output_tokens": 128000,
  "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]},
  "release_date": "2026-07-09",
  "reasoning": true,
  "tool_call": true,
  "canonical_model_id": "openai/gpt-5.6-luna",
  "pricing": {
    "input": 0.2, "cache_read": 0.02, "cache_write": 0.25, "output": 1.2,
    "currency": "USD", "unit": "per_1m_tokens",
    "source": "models.dev:openai"
  },
  "provider": {
    "name": "openai",
    "base_url": "https://yunxiang.lol",
    "doc": "https://platform.openai.com/docs/models",
    "key_alias": "gs-codex"
  }
}
```

**为什么不用 `akm` 命名空间包裹**：实测 OpenRouter 的 `/models` 就是顶层平铺（`id / name / created / context_length / pricing / top_provider` 全在顶层），LiteLLM、vLLM 等网关同样如此；OpenAI 官方 SDK 对未知字段按忽略处理，从未因多字段报错。反而包裹一层会让所有调用方多敲一层路径 `data[0].akm.pricing.input`，且和"扁平即标准"的行业惯例相悖。体积上也几乎无差（平铺 578 字节 vs 包裹 584 字节）。

**为什么单值而不是数组**（依据实测）：你的 6 个 active key 去重后共 26 个模型，其中**只有 2 个**被多个 key 提供（`deepseek-flash`、`deepseek-v4-pro`）。而现有实现就是「按模型名去重、先到先得」，`owned_by` 只取第一个命中的 provider。若改成 `providers[]` 数组，等于凭空引入一套现状不存在的多来源聚合语义，且与 `owned_by` 的语义打架（顶层说一个、数组说多个）。故维持单值，与现状严格一致。

> 那 2 个多 key 模型：`owned_by` / `provider` 取 `/v1/models` 遍历时第一个命中的 key（与今天完全一样），其余不体现。

**字段命名对齐**：`context_length` / `canonical_slug` 这类名字跟随 OpenRouter，便于已适配 OpenRouter 的客户端直接复用；`pricing` 额外带 `currency` 与 `unit`，因为 models.dev 与 AKM 都是「每 1M tokens 的美元」，而 OpenRouter 是「每 token 的字符串」，不写清会误读。

**体积实测**（单条目 JSON 约 578 字节）：当前 6 个 active key / 26 条 ≈ **14.7 KB**；极端场景（单个 openrouter key 366 个模型）≈ **207 KB**，故提供 `?detail=basic` 回退。

### 步骤⑤：换模型检测（审计标记）

**要解决的问题**：三方中转站可能把请求的 `gpt-5.6-luna` 偷偷路由成便宜模型（或降级到旧版本），客户端和账单都看不出来。审计里记下上游**实际返回的 model**，与请求 model 不一致就打标记。

**现有链路已核实（`akm/server.py`）**：

| 链路 | 可用的原始响应 | 位置 |
|---|---|---|
| 非流式 | `result["upstream_response_body_for_log"]` —— **转换前**的上游 body | `proxy.py:1094` 赋值，`1114` 之后才被转换覆盖 `result["body"]` |
| 流式 | `raw_body_str`（上游原文捕获）+ `_IncrementalSSEUsageTracker` 已逐行解析 SSE | `server.py:3329-3330`、`1709-1761` |

**取哪一份是关键**：不能从 `result["body"]` / `body_str` 取——那是**协议转换之后**给客户端的内容，转换器可能重写 `model` 字段，会误报「被换模型」。必须取上游原文。

**三种协议的 model 字段位置**（实现时用真实响应再确认一遍）：

| 协议 | 非流式 | 流式 |
|---|---|---|
| Chat | 顶层 `model` | 每个 chunk 的 `model` |
| Responses | 顶层 `model` | `response.created` / `response.completed` → `response.model` |
| Messages | 顶层 `model` | `message_start` → `message.model` |

**实现方案**：

1. 复用 `_IncrementalSSEUsageTracker`：它本来就在逐行解析 SSE，`_state` 里加一个 `model`，`_update_sse_usage_state()` 顺带提取 → **零额外解析开销**
2. 非流式：新增 `_extract_response_model(raw_body)` 解析上游原文
3. 宽松比较（避免误报）：
   - 转小写、去首尾空白
   - 去掉日期后缀（`-2026-07-09`、`-0813`、`-1017`）
   - 去掉 `vendor/` 前缀（`deepseek/deepseek-v4.1-flash` vs `deepseek-v4.1-flash`）
   - 归一后**不相等**才判 `mismatch=1`
4. DB 加两列（沿用 `converted INTEGER` 的既有先例）：
   - `response_model TEXT DEFAULT ''` —— 上游返回的原始 model 名
   - `model_mismatch INTEGER` —— `NULL`=没取到（流式异常/无 model 字段）、`0`=一致、`1`=被换
5. 写入点：`server.py` 两处 `_submit_audit_log`（流式 ~3392 / 非流式 ~3471）各加两个字段
6. UI：`logs.html` 在模型名旁打黄色警示标记（hover 显示「请求 X → 实际 Y」），筛选器加「模型被换」；`/api/logs` 支持 `mismatch=1` 过滤

**边界**：

- 取不到 `model` 时写 `NULL` 而不是 0——「没证据」和「确认一致」必须分开，否则统计会把未知当成干净
- 上游返回错误（4xx/5xx）没有 model 字段，走 NULL
- 该功能不改变转发行为，纯观测；不加额外上游请求

| # | 任务 | 文件 | 验证 |
|---|---|---|---|
| 5.1 | DB 加 `response_model` / `model_mismatch` 两列 + 迁移（`_migrate_audit_columns` 已有先例） | `akm/db.py`、`akm/audit.py` | 老库自动加列；新日志两列有值 |
| 5.2 | 流式：`_IncrementalSSEUsageTracker` 的 state 增加 `model`，`_update_sse_usage_state` 覆盖三种协议 | `akm/server.py` | Chat/Responses/Messages 流式均能取到；取不到为 None |
| 5.3 | 非流式：`_extract_response_model(upstream_raw_body)` | `akm/server.py` | 三种协议非流式均能取到 |
| 5.4 | 归一比较函数 `_is_model_mismatch(request_model, response_model)`（小写/去日期后缀/去 vendor 前缀） | `akm/server.py` | `deepseek/deepseek-v4.1-flash` vs `deepseek-v4.1-flash` 判为一致；`gpt-5.6-luna` vs `gpt-6-luna` 判为不一致 |
| 5.5 | 两处 `_submit_audit_log` 写入两字段 | `akm/server.py` | 审计行出现 `response_model`；被换时 `model_mismatch=1` |
| 5.6 | `/api/logs` 支持 `mismatch=1` 过滤 + `logs.html` 警示标记与筛选 | `akm/server.py`、`akm/templates/logs.html` | 筛选可用；标记 hover 显示请求/实际对比 |
| 5.7 | 统计口径：`/api/stats` 汇总「被换模型请求数」（可选，展示在后置面板） | `akm/server.py` | 数字与日志筛选结果一致 |

---

## 八、明确不做

1. 不把快照写回 `cost_pricing_table`（体积 + 会冲掉你手工校准的 off-peak 备注）。
2. 不用 models.dev 覆盖 `BUILTIN_AGENTS` / `custom_agents` 的协议能力字段（`supports_responses/chat/messages`、`messages_use_anthropic_path`、`inject_*`）——models.dev 完全没有这些字段，猜错直接 400。
3. 不把 5.9 MB 原始 JSON 打包进 app（运行时拉取，过滤后入库）。
4. 不实现 `tiers` / `context_over_200k` 长上下文档位计价（现有三段格式无此维度）。
5. 不改变 `/v1/models` 顶层字段语义（不加 `created`、不改 `owned_by` 语义），新增字段直接平铺但不覆盖标准三字段。

---

## 九、遗留待确认

| # | 问题 | 结论 |
|---|---|---|
| 1 | ~~默认数据源与兜底~~ | **已定**：`cost_pricing_source=snapshot` + `models_dev_official_fallback=true`，30 天 $497.82、命中 89.7% |
| 2 | ~~中转站模型下拉为空时如何交互~~ | **已作废**：模型输入框保持现状不改 |
| 3 | ~~`provider.key_alias` 是否露出~~ | **已定：露出**。别名是用户自定义的，且仅本地 127.0.0.1 可访问，`/v1/models` 返回 `provider.key_alias` |
| 4 | ~~剩余 10.3% 归 0 的模型要不要补手写表~~ | **已定：不补**。`deepseek/deepseek-v4.1-flash`(1333)、`monk-coding`(340)、`bge-m3`(352) 等继续算 0，属预期行为，stats 里靠「未匹配计数」显式暴露即可 |
| 5 | ~~存量 key 的 base_url 要不要锁死~~ | **已定（口径 A）**：手填过非默认地址的 key 自动进自定义态，保持可编辑 |

> **全部口径已定稿（A~K），无待确认项。** 按第七节步骤①→②→③→④→⑤ 依次实施。

---

## 十、文档同步清单（执行时一并更新）

| 文件 | 需改内容 |
|---|---|
| `README.md` | 配置项表新增 5 个 key（含 `models_dev_official_fallback` 默认开、`models_dev_prefer_providers`）；费用估算段说明「三段式取价：本供应商命中 → 官方兜底（可关）→ 按 0」；新增「models.dev 数据表」一节（三张表 + 同步命令 + 221/8457 规模）；API 表改写 `/v1/models` 一行，说明「标准三字段 + 顶层扩展 + 可选 `?capability=chat` 能力过滤（不传不过滤）+ `?detail=basic`」；Key 管理页说明供应商改为**可搜索下拉**（`akm-combobox`，221 个供应商）、`base_url` 默认禁用仅「自定义…」可编辑，模型输入框不变 |
| `plugins/agent_chat/README.md` | 补一句「前端请求 `/v1/models?capability=chat`，因此下拉不显示 image / embedding 模型」；记录重建命令 `VITE_BASE='./' VITE_AKM_API_URL='' npm run build` 与两处 dist 同步位置 |
| `/Users/nk/Desktop/Ecology/chat`（独立仓库） | `src/lib/agent-api.ts` 的 `fetchModels()` 改请求 `/v1/models?capability=chat`，重建产物后同步 `ccs/plugins/agent_chat/dist` 与 `~/.akm/plugins/agent_chat/dist` |
| `docs/release-guide.md` | `akm/models_dev.py` 登记进 `setup.py` 的 `includes`；`akm/db.py` 新增三张表的建表逻辑需在发版说明里写明「老库启动自动建表」；无新增模板 / 静态文件 |
| `docs/logs.md` | 新版本条目（追加最上方）；需写明 `audit_logs` 新增 `response_model` / `model_mismatch` 两列与「换模型检测」行为，以及日志页新增「模型被换」筛选 |
| 知识库（akm-md-kb） | 沉淀「models.dev 入库与三段式取价口径（本供应商 → 官方兜底可关 → 0）、cost 为 null 才剔除（0 价保留）、SQL 聚合与内存索引的分工、npm 不能推导协议能力的反例、换模型检测的提取点与 NULL 语义、Key 页模型输入保持自由文本的取舍」 |
