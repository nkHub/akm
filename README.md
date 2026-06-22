# AI Key Manager

本地 AI API Key 管理代理服务。集中管理多个 AI 供应商的 API Key，自动根据优先级选择可用 Key，支持故障切换、请求代理转发及完整审计日志。

## 安装

```bash
pip install -e .
```

如果希望 `markdown_kb` 真正启用 `sqlite-vec` 做第一阶段向量召回，而不是回退到 NumPy / Python 路径，当前 Python 运行时还需要满足一个额外条件：内置 `sqlite3` 必须支持 `enable_load_extension()`。仓库已经把 `sqlite-vec` 加进项目依赖，但像默认构建的部分 `pyenv` Python 仍可能因为底层 SQLite 绑定不支持扩展加载而自动回退。

最直接的判断方式不是“包有没有装上”，而是看 `markdown_kb` 状态接口返回：

- `vec_available`: 当前运行时是否具备加载 `sqlite-vec` 的基础能力
- `vec_ready`: 当前索引是否已经准备好 vec 虚表
- `vec_enabled`: 当前这份索引是否允许走 vec 粗召回
- `vec_version`: 成功加载时对应的 `sqlite-vec` 版本
- `vector_retrieval_backend`: 当前第一阶段粗召回最终实际走的是 `sqlite-vec` 还是 Python / NumPy 回退链路

当前在这台 Apple Silicon macOS 机器上，下面这组命令已经实测可行：会让 `pyenv 3.12.13` 链接 Homebrew SQLite，并成功加载 `sqlite-vec`。

```bash
env \
  PYTHON_CONFIGURE_OPTS='--enable-loadable-sqlite-extensions' \
  LDFLAGS='-L/opt/homebrew/opt/sqlite/lib' \
  CPPFLAGS='-I/opt/homebrew/opt/sqlite/include' \
  PKG_CONFIG_PATH='/opt/homebrew/opt/sqlite/lib/pkgconfig' \
  pyenv install -f 3.12.13

~/.pyenv/versions/3.12.13/bin/python -m pip install sqlite-vec

~/.pyenv/versions/3.12.13/bin/python - <<'PY'
import sqlite3, sqlite_vec
conn = sqlite3.connect(':memory:')
conn.enable_load_extension(True)
sqlite_vec.load(conn)
print(sqlite3.sqlite_version)
print(conn.execute('select vec_version()').fetchone()[0])
PY
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

应用图标由 `logo.icns` 提供，通过 `setup.py` 中的 `iconfile` 选项配置。当前 `py2app` 打包入口也已显式包含 `sqlite_vec`，避免菜单栏应用里因为动态导入丢包而让 `markdown_kb` 退回到非 vec 路径。详细打包规范、版本号管理及更新方案见 [docs/release-guide.md](docs/release-guide.md)。

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
akm status                    # 查看服务 / Key / 日志 / 插件总览

# 配置
akm config get                # 查看完整配置
akm config get server_port    # 查看单个配置项
akm config set server_port 8801 # 修改配置项

# Key 管理
akm key add <别名> <供应商>     # 添加 Key
akm key list                   # 列出所有 Key
akm key show <别名>             # 查看单个 Key 详情
akm key edit <别名> --priority 1 --models gpt-4o,gpt-4.1 # 统一编辑 Key
akm key health                 # 批量巡检 Key 可用性
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

# 图片
akm image generate "a cat astronaut"                  # 通过本地代理生成图片并输出 JSON
akm image generate "a cat astronaut" --model gpt-image-2 --size 1024x1024
akm image edit ./cat.png --prompt "remove background" # 通过本地代理编辑图片并输出 JSON
akm image edit ./cat.png --prompt "replace sky" --mask ./mask.png --model gpt-image-2

# 日志
akm log list                   # 查看最近日志
akm log stats                  # 查看日志聚合统计
akm log clean --before YYYY-MM-DD # 清理旧日志

# 插件
akm plugin list               # 查看插件列表
akm plugin enable <名称>      # 启用插件
akm plugin disable <名称>     # 禁用插件
akm plugin config get <名称> [键] # 读取插件配置
akm plugin config set <名称> <键> <值> # 修改插件配置

# 自检
akm doctor                    # 检查配置 / 数据库 / 插件 / 服务状态
```

## 自定义 MCP 脚本

项目内的自定义 MCP 统一放在 `scripts/` 目录中。仓库当前提供 `scripts/translate-mcp.py`，用于启动一个基于 `translators` 与 `langdetect` 的本地翻译 MCP Server。它暴露两个工具：`translate`（文本翻译）与 `detect_language`（语言检测），适合在本地智能体工作流中直接复用，且无需额外 API Key。

```bash
uv run scripts/translate-mcp.py
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

当前版本的菜单栏应用在 macOS 从休眠恢复后，会自动执行一次分级自愈检查：先等待约 8 秒让网络、VPN 和本地路由恢复，再检查本地服务端口与 `/health/ready`。如果发现本地服务线程未就绪或端口不可达，AKM 会自动重启本地代理服务；如果本地服务已经 ready，则会继续挑选一个已启用且带模型列表的 key，复用现有连通性测试链路发起一次真实上游最小探活。只有当“本地服务未 ready”或“本地 ready 但真实上游探活失败”时，AKM 才会自动重启本地代理服务，以减少无意义重启并更快覆盖休眠后上游链路未恢复的场景。

## Web 管理台

`akm serve` 启动后访问 `http://127.0.0.1:8800/admin`

| 页面 | 功能 |
|------|------|
| 统计 | Token 用量仪表盘（骨架屏加载、缓存命中与缓存创建独立展示、输入 Token 不含缓存、按 Key/模型/日期分组、K/M 格式、1d/7d/30d 自然日切换、时间筛选本地缓存、页面重新可见时自动刷新） |
| 审计 | 请求日志（输入/缓存/输出 Token 列、Key/状态筛选、成功/失败切换、筛选持久化、正倒序、每页 10 条、JSON/会话 WebComponent 渲染、超长内容阈值控制；日志头可查看 token 回填来源 flags） |
| 管理 | Key 增删改查、启用/禁用、优先级排序、最近 10 次成功请求平均延迟展示、连通性测试、自定义测试结果弹窗、一键导出备份、一键刷新提供商模型列表、模型标签点击复制、展示提供商模型列表、每页 12 条分页 |
| 插件 | 插件列表、启用/禁用开关、上传 .zip 安装、插件配置读写（无界面插件不显示转换节点；有界面插件仅在启用后可点击进入页面） |
| 设置 | 分区布局（服务/日志/供应商代理）、端口配置、日志保留天数、日志体积控制（请求/响应体开关）、并排双按钮（清空日志 / 清空请求响应体，显示数据库大小与审计日志条数）、JSON 渲染阈值、供应商代理管理（添加改为弹窗 Web Component） |
| 关于 | 版本与功能简介（展示内置插件能力） |

管理台内部的通用 Web Component 约定见 `docs/design/web-components.md`，当前统一沉淀了开关、分页、分段按钮、空态、弹窗、抽屉和设置卡片等基础壳组件，供后续页面复用。

仓库当前还提供一个**内置但默认关闭**的 `markdown_kb` 插件，目录位于 `akm/plugins/markdown_kb/`。

它会随 AKM 一起分发，并在用户显式启用后提供一个最小但真实可用的 Markdown 知识库页面。

当前能力包括：
- 查看状态、批量上传/列出/删除 `.md` 文件
- 优先使用 `markdown-chunker` 做结构感知切片；依赖缺失或第三方异常时自动回退到内置结构化切片器
- 全量重建索引、单文件重建、增量同步预览/执行、清空索引
- 通过本地 AKM `/v1/embeddings`、可选 `/v1/rerank` 与 `/v1/chat/completions` 完成 `query / ask` 闭环

这里的 `embedding_model` 是必填项，`reranker_model` 是可选项。页面侧已经接通状态总览、检索配置、文件管理、索引重建、单文件重建、同步预览/执行、检索测试、问答测试、清空索引和健康状态展示；并且 health / sync 结果已经从摘要数字升级成具体文件列表展示。

当前页面已经直接提供 `markdown_kb` 自己的配置入口，不用再回插件列表弹窗调整检索参数；`embedding_model / reranker_model / chat_model` 继续使用当前 `/v1/models` 驱动的动态下拉。

插件配置弹窗的布局现在也支持按插件声明单列或双列：默认单列，`markdown_kb` 当前显式使用双列，让 `top_k / score_threshold / semantic_weight / keyword_weight` 这类短配置项能更紧凑地一行两个显示。上传文档页面现在也支持一次选择多个 `.md` 文件批量保存，但仍然不会在上传阶段自动重建索引。

检索测试和问答测试页面里的模型下拉现在都按“跟随默认 / 手动覆盖”的方式绑定：`Embedding 模型` 和 `问答模型` 可以选择跟随插件默认值或单次手动指定；`Reranker 模型` 额外保留了第三种“明确不启用 rerank”的状态。这样既能正确显示当前插件默认配置，也不会再因为测试页的展示值和实际请求值不一致而让人误判当前链路到底有没有启用 rerank。

当前版本还给 `markdown_kb` 补上了更接近 Dify 使用习惯的检索调优项：`top_k` 默认 `4`、最大 `10`，无论是否启用 rerank 都会控制最终保留条数；
  `score_threshold` 采用 `0~1` 区间，默认 `0.7`，用于过滤低相关片段；
  `semantic_weight / keyword_weight` 会共同参与第一阶段排序，用来调节“语义召回”和“BM25 字面召回”各自的占比。

当前配置还新增了 `document_workspace_root`：它用于声明当前这批 Markdown 文档所属的工作目录，重建索引后会把该工作目录写入 SQLite 元数据。

检索阶段的规则是：如果当前请求里能提取出工作目录，则会检索“`workspace_root` 为空的公共文档 + 当前工作目录对应的文档”；如果当前请求里提取不到工作目录，则只检索 `workspace_root` 为空的公共文档。这样既兼容 OpenCode / Codex / Claude 这类能提供工作域上下文的请求，也允许把未绑定工作目录的通用文档作为兜底公共知识使用。

针对字面检索，当前版本已经把原来的轻量关键词覆盖率升级成了 BM25 融合：query 和 chunk 会继续复用同一套中英文 tokenization，英文仍按 token 拆分；中文连续片段则优先使用 `jieba` 做自然分词，若当前环境未安装 `jieba` 再自动回退到 2~4 字滑窗；在此基础上，第一阶段会把向量分和归一化后的 BM25 分按 `semantic_weight / keyword_weight` 做线性融合。因此像“参考考试大纲生成复习计划”或 “generate a study plan from the exam outline” 这类问题，不再要求整句在文档里逐字出现，也能通过“考试大纲 / 复习计划”或 “exam outline / study plan” 这类局部短语拿到更稳定的字面相关性分数。

当前版本已经按“方案一”把默认索引持久化切到插件私有 `~/.akm/markdown_kb/index_store/kb.db`：保留 `docs/` 原文目录、忽略旧 `index.json`、通过全量 `rebuild` 重新写入 SQLite，并支持只清空索引或连同原始文档一起删除。

当前默认实现是 `SqliteKbIndexStore`。向量会继续以 JSON 形式保存在 SQLite 普通表里作为回退数据源；如果当前运行时支持加载 `sqlite-vec`，第一阶段粗召回会优先在 SQLite 内完成 KNN 查询，并把 workspace / selected_doc 过滤条件前推到 SQL 层，避免别的项目 chunk 先占满 top-N 候选；如果本地 Python 的 SQLite 绑定不支持扩展加载，或索引里混入了不同 embedding 维度的数据，则会自动回退到现有的内存预加载 + NumPy 矩阵化相似度计算，若当前环境尚未安装 `numpy` 则继续回退到 Python 循环计算。第一阶段会统一输出 `vector_score / keyword_score / hybrid_score` 方便观察召回原因，其中 `keyword_score` 表示归一化后的 BM25 分；启用 rerank 后，第一阶段仍保留“向量分 + BM25 分”的混合粗召回，第二阶段再把候选交给 `rerank_score` 重排。状态接口现在也会额外返回 `vec_available / vec_ready / vec_enabled / vec_version / vector_retrieval_backend`，便于直接判断当前请求是否真的走到了 `sqlite-vec`。

当前仓库内置依赖名是 `sqlite-vec` / `sqlite_vec`，不是 `sqlite-sec`。如果后续讨论里提到 `sqlite-sec`，应按 `sqlite-vec` 这条向量召回链路理解。

插件页面本身会先进入 AKM 后台宿主页，因此左侧菜单和顶部栏会保留；真正的插件原始 HTML 则通过 `/plugins/<name>/raw` 供宿主页 iframe 加载。

`markdown_kb` 插件启用后，现在会默认覆盖 `/v1/chat/completions`、`/v1/messages`、`/v1/responses` 三类文本请求：它会自动抽取最后一条用户问题执行检索，只有确实命中知识库片段时，才会把参考资料注入到原请求里；未命中的请求继续按原样透传。当前实现已经不再依赖 `kb:` 这类模型名前缀来触发知识库注入，应以这条自动注入链路为准。

`markdown_kb` 的切片现在优先走 `markdown-chunker`，再回退到内置结构化切片器。

### `markdown_kb` 概览

- 插件位置：`akm/plugins/markdown_kb/`
- 默认状态：内置但默认关闭，需用户显式启用
- 文档能力：支持状态查看、批量上传/列出/删除 `.md` 文件
- 分块策略：优先使用 `markdown-chunker` 做结构感知切片，失败时回退到内置结构化切片器
- 索引能力：支持全量重建、单文件重建、增量同步预览/执行和清空索引
- 检索链路：通过本地 AKM `/v1/embeddings`、可选 `/v1/rerank` 与 `/v1/chat/completions` 完成 `query / ask`
- 页面能力：状态总览、检索配置、文件管理、重建、同步、检索测试、问答测试、健康状态展示

## 配置

`markdown_kb` 还额外开放了一个文件级工作目录绑定接口：`POST /api/markdown-kb/files/bind-workspace`。调用方可以按 `file_name` 为单个 Markdown 文档绑定 `workspace_root`，接口成功后会返回 `needs_rebuild=true`；这表示绑定关系已经持久化，但仍需再执行一次 `rebuild-file`、`sync` 或 `rebuild`，新的工作目录绑定才会真正进入索引并参与检索过滤。

`markdown_kb` 的测试页现在会基于当前文件列表渲染一个去重后的 “Workspace 范围” 下拉：检索测试和问答测试默认不选，此时继续沿用请求里的 `workspace_root / working_directory` 语义，只检索“公共文档 + 当前工作域文档”；如果显式选中某个 workspace，则会直接把该 workspace 作为当前请求的工作域传给 `query / ask`，从而只保留“公共文档 + 该 workspace 文档”的候选范围。对应地，`POST /api/markdown-kb/query` 与 `POST /api/markdown-kb/ask` 也支持直接从请求体显式接收 `workspace_root / working_directory`，不强依赖 OpenCode / Codex / Claude 自动注入环境上下文。

配置文件位于 `~/.akm/config.json`，可通过 Web 设置页面修改：

```json
{
  "auto_open_admin": true,
  "log_retention_days": 30,
  "server_port": 8800,
  "log_request_body": false,
  "log_response_body": false,
  "stats_include_estimated_usage": false,
  "json_viewer_max_text_length": 600000,
  "image_request_timeout_sec": 300,
  "wake_recover_delay_sec": 8,
  "use_native_user_agent": false
}
```

Key 和日志数据存储在 `~/.akm/akm.db`（SQLite）。另外，Key 的增删改、启停和模型刷新会额外追加写入 `~/.akm/keys.log`，它的定位是“Key 配置/状态审计日志”，主要用于复盘谁在什么时间改了哪些 Key 元数据，不包含 `api_key` 明文；事件名统一采用 `key.config.*`、`key.status.*`、`key.models.*` 这种层级化审计风格。菜单栏应用的休眠恢复链路则会单独把关键节点追加写入 `~/.akm/wake-recovery.log`，采用逐行 JSON 的形式记录收到唤醒、等待、探针结果、重启动作和最终恢复结果，便于判断到底是等待时间过短、本地服务未 ready，还是上游链路尚未恢复。运行时卡顿、请求堆积、连接池重建等问题请优先查看 `/health/detail` 与 `/debug/runtime`。

`image_supported_models` 也是 `config.json` 中的全局配置项，默认值为 `gpt-image-2`。它支持逗号分隔多个图片模型，首项会作为 `/v1/images/generations` 与 `/v1/images/edits` 在未显式传 `model` 时的默认回填值；只有当当前存在 active key 支持该默认模型时才会自动补齐，否则接口会直接返回明确报错。

`image_request_timeout_sec` 也是 `config.json` 中的隐藏配置项，默认值为 `300`。它仅作用于 `/v1/images/generations`、`/v1/images/edits` 以及 CLI 复用的本地图片调用链路，用来把图片请求超时放宽到比聊天接口更长的时间；该项不会在设置页单独展示。

`wake_recover_delay_sec` 也是 `config.json` 中的隐藏配置项，默认值为 `8`。它仅作用于菜单栏应用收到系统唤醒事件后的恢复流程，用来控制在执行本地 `ready` 检查和真实上游探活之前要等待多久，适合在 VPN、Wi-Fi 或代理路由恢复偏慢的机器上手动调大；该项不会在设置页单独展示。

`use_native_user_agent` 也是 `config.json` 中的隐藏配置项，默认值为 `false`。默认情况下 AKM 会统一把上游请求的 `User-Agent` 写成 `akm/<version>`；当该项设为 `true` 时，如果当前 HTTP 请求带有原始 `User-Agent`，AKM 会优先透传客户端原值。聊天、图片、模型探测等走统一 `build_headers()` 的链路都会复用这套规则；如果当前链路没有原始请求头可用，则仍会回退到 `akm/<version>`。

如果需要给本地智能体接入图片能力，优先使用仓库内置的 `skills/akm-image-local/SKILL.md`。这个 skill 的定位是让图片生成与编辑统一走本地 AKM 服务，而不是继续依赖单独的图片 MCP 网关；它已经约定了默认模型、尺寸、质量和提示词组织方式，适合直接复用到智能体工作流中。

如果需要给本地智能体接入 Markdown 知识库文档同步能力，仓库现在还提供 `skills/markdown-kb-auto-sync/SKILL.md`。这个 skill 的定位是把本地 `.md` 文档直接同步进 `markdown_kb` 当前运行时使用的 `docs_dir`，并在目录更新后继续调用 `sync` 或 `rebuild` 刷新索引，避免把“文件已写入文档目录”和“文件已进入索引数据库”混为一谈。当前 skill 还额外支持一种“初始化知识库”工作流：当用户只想先给某个项目落一份初始知识库文档时，skill 会默认以项目名称作为初始化文档文件名，基于当前仓库资料生成一份五模块结构的知识文档并写入 `docs_dir`，再执行一次显式 `sync`；五个模块分别是 `P1 方法论`、`P2 问题解决方案`、`P3 概念原理`、`P4 外部知识精炼`、`P5 关联映射`。其中每个模块都会优先写成条目化内容：方法论写流程和 SOP，问题解决方案写现象-原因-修复，概念原理写是什么和为什么，外部知识精炼写消化后的摘要，关联映射写对比、依赖和选型关系。这个初始化动作只创建知识文档，不会真的在磁盘上批量创建目录树。

底层实际调用仍然是下面两个本地命令：

- `akm image generate <prompt>`
- `akm image edit <image_path> --prompt <prompt>`

这两个命令都会请求本地代理，成功时只输出原始 JSON，便于 skill 或其他外部程序直接解析 `data[].url` 或 `data[].b64_json`。其中 `image edit` 会自动把本地文件组装为 `multipart/form-data`，调用方无需自己处理上传细节。

`stats_include_estimated_usage` 仅作为 `config.json` 隐藏配置项存在，默认 `false`，不会在设置页单独展示；如需让首页统计把 `usage_estimated_light` 这类估算 token 计入总量与请求数，可手动改为 `true`。

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI Chat Completions 接口（按 `model` 选 key；必要时会按目标 key 的 provider 能力做协议转换） |
| POST | `/v1/messages` | Anthropic Messages 接口（兼容 Claude Code，非 OpenAI 格式；按 `model` 选 key；必要时会按目标 key 的 provider 能力做协议转换） |
| POST | `/v1/responses` | OpenAI Responses API 接口（Codex 兼容；按 `model` 选 key；必要时会按目标 key 的 provider 能力做协议转换） |
| POST | `/v1/embeddings` | Embeddings 转发接口（按 `model` 选 key 后纯透传，不做协议转换） |
| POST | `/v1/rerank` | Rerank 转发接口（按 `model` 选 key 后纯透传，不做协议转换） |
| POST | `/v1/images/generations` | Images Generations 转发接口（仅透传，不做协议转换；未传 model 时仅在存在支持该模型的 active key 时默认补 `gpt-image-2`，否则直接报错） |
| POST | `/v1/images/edits` | Images Edits 转发接口（接收 `multipart/form-data` 纯透传；未传 model 时仅在存在支持该模型的 active key 时默认补 `gpt-image-2`，否则直接报错） |
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

Key 选择分两阶段：优先精确匹配当前 model 的 Key（按优先级依次尝试，已失败 Key 自动排除），精确匹配全部不可用时回退到 `models='*'` 的 Key。`*` 不再表示“无条件匹配全部模型”，而是表示“仅在保存为 `*` 时自动同步 `{base_url}/models`，并在这些提供商模型列表中参与匹配”；如果该 key 没有可用的 provider 模型列表，则不会参与 wildcard 匹配。

> 应用重启后，数据库中残留的 `rate_limited` 状态会自动恢复为 `active`。
> Key 的 models 字段存储时自动规范化（去除逗号前后空格），防止匹配失败。
> Key 管理页仅在 `models='*'` 时会自动同步 `{base_url}/models`；`*` 不能和自定义模型同时使用。测试 wildcard Key 时也会直接使用这份已同步模型列表；如果列表为空，需要先保存或刷新模型。

## 流式转发

请求转发会跟随客户端的流式意图：

- 客户端 `stream=true` → 逐块透传 SSE
- 客户端 `stream=false` → 直接请求上游普通 JSON 并原样/按协议转换后返回
- 流式结束后异步写入审计日志（完整响应体用于统计和对话回放）
- 审计日志中的 `request_body` 默认优先记录“实际转发给上游的请求体”，而不是入口原始请求体；这样可以准确反映协议转换、插件改写、脱敏或默认值补齐后的最终出站内容，便于排查“AKM 实际发了什么”。代价是日志不再完全等同于客户端最初提交的原始输入。
- 流式请求的插件 `on_response` 生命周期会等到 SSE 真正结束后才触发，避免并发计数过早回收导致慢 key 持续拥塞
- 流式响应的内存捕获已改为有界模式：默认最多保留 `256KB`（配置项 `stream_capture_max_bytes`），超出后仅保留头尾两段并追加截断标记，日志 flags 会记录 `stream_capture_truncated`

## 健康监护

当前版本已内置轻量监护：

- 后台心跳会周期性检测事件循环卡顿和 SQLite 探针状态
- AI 请求会登记 in-flight 请求数，流式请求会登记 active streams
- 审计日志写入已改为“有界队列 + 单 worker”模式：高峰期会优先保护主请求链路，队列满时丢弃新增审计日志，并把 backlog / dropped 信号暴露给健康探针
- 上游连续失败次数会被聚合进健康状态，便于区分“本地卡住”和“上游雪崩”
- 上游请求按 `provider/key_alias/model/upstream_api_path` 懒创建隔离 `http_client` 连接池，避免单个慢流式链路占满全局连接池后拖慢其他模型或 Key
- 当上游连续失败次数达到阈值时，服务会自动软重建隔离连接池管理器，尝试恢复异常连接池或脏连接状态

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
- `http_client.pool_count`、`max_pools` 与单池连接上限

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

插件列表页当前按“展示生命周期”排序：`model_matcher` 固定置顶，`error_handler` 固定置底，其余插件按 `filter → converter → post → handler → app` 顺序展示，便于按转发链路理解每个插件处于哪一层。

`model_matcher` 新增可配置的并发/慢 key 旁路策略（默认关闭，保守模式）：

- `enable_inflight_bypass`：是否启用拥塞旁路（默认 `false`）
- `max_inflight_per_key`：单 key 并发阈值（默认 `3`）

`model_matcher` 的 `aliases` 配置仅支持显式映射（如 `gpt-4=gpt-4.1`），用于在请求进入转发链路前直接替换 `model`。未命中显式映射时，请求会保留原始模型名继续做 key 匹配。
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

##### Messages → Chat 发送方向

- `system` → 首条 `system` 消息
- `stop_sequences` → `stop`
- `metadata` 默认透传；`metadata.user_id` 会在未显式传 `user` 时映射到 Chat `user`
- `max_tokens` 默认映射到 `max_tokens`；当当前选中的供应商为 `openai` 时，会额外补一份 `max_completion_tokens`
- `reasoning.effort` / `reasoning_effort` 仅在当前选中的供应商为 `openai`、且调用方未显式关闭 `thinking` 时，保守映射为 `reasoning_effort`
- `tool_result.content` 为纯文本时会展平为 Chat `tool` 文本；若包含非文本结构，则保留整份原始内容并序列化为 JSON 字符串
- `thinking` 不直接透传到 Chat-only 上游，避免向不兼容供应商注入 Anthropic 专有字段

以上和供应商相关的兼容开关已下沉并收口到 `Agent` 能力层：当前内置 `openai` 会开启 `max_completion_tokens` / `reasoning_effort` 补齐，其他供应商默认走保守策略，避免在协议层再维护独立 provider profile。

##### Responses → Chat 发送方向

- `instructions` → `system` 角色消息
- `input` 中的 `function_call` / `function_call_output` → `tool` 角色消息
- 连续 `function_call` 合并为单条 `assistant` 消息（含多个 `tool_calls`）
- `function_call` 上的 `reasoning_content` 回写到合并后的 `assistant` 消息
- `function_call_output` 的结构化 `output` 会序列化为字符串，保证 Chat `role=tool` 消息合法
- `previous_response_id` 会通过本地内存会话缓存恢复上一轮 Chat 历史（含 `reasoning_content` / `tool_calls`），不再把该字段直接转发给 Chat-only 上游
- 兼容现代 continuation：`role=tool` / `tool_call_id` / typed output content 会统一转成 Chat `tool` 消息
- 消息重排序：`system` 移到 `assistant(tool_calls)` 之前
- `_clean_schema()` 递归移除工具 schema 与 structured output schema 中的 `additionalProperties` / `strict` 字段
- `namespace` 类型工具递归展开子工具（支持 MCP）
- `tool` 结尾时追加空 `user` 消息，触发 DeepSeek 继续生成
- `thinking` / `reasoning_effort` 的默认补齐也已收口到 `Agent` 能力层：当前仅 `deepseek` 会在未显式传入时补 `thinking={type:enabled}` 与 `reasoning_effort="high"`

##### Chat SSE → Responses SSE 接收方向

| Chat SSE delta | Responses SSE 事件 |
|----------------|-------------------|
| `delta.reasoning_content` | `response.output_item.added`（type=reasoning）+ `response.reasoning_summary_text.delta` |
| `delta.content` | `response.output_text.delta` |
| `delta.tool_calls`（首 chunk） | `response.output_item.added`（type=function_call）+ `response.output_tool_call.begin` |
| `delta.tool_calls`（后续） | `response.function_call_arguments.delta` + `response.output_tool_call.delta` |
| `finish_reason: tool_calls` | `response.function_call_arguments.done` + `response.output_tool_call.end` + `response.output_item.done` |
| 流结束 | `response.completed` + `response.done` |

推理内容通过独立的 `reasoning` 类型输出项流式推送，Codex 在「思考」面板中折叠展示，与正文分离。`protocol_converter` 会在内存中保留最近一批 Responses 会话快照（TTL 24 小时、最多 256 条），用于 `previous_response_id` 续接时恢复 DeepSeek thinking/tool-call 所需的 `reasoning_content` 和工具调用历史；该缓存不持久化到磁盘，服务重启后自动清空。

### 自定义供应商

在设置页「供应商代理管理」可添加自定义供应商（如第三方中转站），定义其默认 base_url、认证头模板和协议能力，持久化到 `~/.akm/config.json`。添加后新建 Key 选择该供应商时可省略 base_url 和认证头。

自定义供应商做连通性测试时，会优先使用该供应商第一个启用的协议能力发起请求：优先级为 `Chat -> Responses -> Messages`。

Key 管理页「添加 Key」中的供应商下拉会自动读取 `/api/agents`（即设置页维护的数据），并保留「自定义」选项用于手工填写 `base_url`/`auth_header`。当保存的 `models='*'` 时，管理台会同步请求该提供商的 `{base_url}/models`，并把返回的模型列表缓存到当前 key，用于前端展示以及 wildcard 模式下的模型匹配；显式自定义模型时不会自动请求该列表。

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
      "messages_use_anthropic_path": false,
      "inject_max_completion_tokens": false,
      "inject_reasoning_effort": false,
      "map_metadata_user_id_to_user": true,
      "responses_force_thinking_enabled": false,
      "responses_default_reasoning_effort": null
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
- httpx 隔离连接池（按 provider/key/model/协议链路懒创建，lifespan 统一回收）
- SQLite（审计日志持久化，WAL 模式，token 列直读 + 内存缓存优化统计性能）
- Fernet 加密（Key 存储）
- rumps（macOS 菜单栏）
- Tailwind CSS / marked.js（Web UI）
