# 版本变更记录

本文件归档 AKM 各版本的功能与修复说明（从 `README.md` 与 `docs/release-guide.md` 中原有的版本段落迁移而来，起自 v0.1.46）。更早版本请查阅 Git 历史或 GitHub Releases。

约定：

- 新版本记录**追加在最上方**，标题用 `## vX.Y.Z`，正文保持在“改了什么、影响范围、兼容性”三件事上。
- 面向用户的变更写在这里；版本号如何统一与同步（`akm/__init__.py` / `pyproject.toml` / `uv.lock`）、打包与自动更新流程见 [release-guide.md](release-guide.md)。

## v0.1.50

管理台新增**连接池页**（`/pool`，侧边栏「连接池」，在「设置」与「插件」之间）。后端新增 `GET /api/pool/status` 与 `POST /api/pool/action`：前者返回 `HttpClientPoolManager.snapshot()` —— 聚合指标（路由池数量/上限与占用率、活跃 / 空闲 / 建连中 / 失败 / 过期连接数、排队请求数、累计路由请求数、建池 / 空闲回收 / LRU 淘汰 / 手动关池 / 建池失败计数）、当前配置、实例 id 与运行时长、聚合状态等级（正常 / 繁忙 / 空闲 / 异常 / 拥塞），以及逐路由池明细（`provider / key / model / path`、状态、连接分类、排队、已路由请求、空闲时长与是否「待回收」、该池每条连接的源站 / 协议 / 状态 / 已发请求数）；连接状态直接探测 httpx 底层 httpcore 连接池（`client._transport._pool.connections` 与 `_requests`），`_pool_object()` 取不到连接池对象时退化为只展示 AKM 侧统计，不影响转发链路（注意：若 httpx/httpcore 只是改了私有属性名，连接会被误判成「建连中 / 失败」，而不是走进这条退化路径）。后者支持四个动作：`close_idle_connections`（只关各池的空闲 keep-alive 连接、保留路由池）、`evict_idle_pools`（按 `http_client_idle_ttl_sec` 立刻回收空闲路由池）、`close_pool`（按 `pool_key` 关闭单个路由池）、`rebuild`（用最新配置强制重建连接池——`_recreate_http_client_pool` 新增 `force` 参数，跳过 `should_recreate_http_client()` 的「上游连续失败次数」门槛；该计数由重建本身清零，并非时限冷却，重建后上游继续失败仍会自动触发，现有自动调用行为不变），每个动作都回传最新快照与最近运行时事件，页面无需二次请求。页面提供状态总览卡片、路由池明细表（状态徽章 + 连接明细悬浮、窄窗口只省略号截断不横向溢出；连接列固定两行展示「共 N 条 + 异常标记」与「活跃 · 空闲」，宽度不随文字长度抖动）、逐池「关闭」、重建确认弹窗、3 秒自动刷新开关与操作结果通知，并展示 `/debug/runtime/history` 的最近事件；自动刷新按内容去重（内容未变不重建 DOM），`akm-tooltip` 新增宿主销毁时的清理（收起浮层并摘除节点），因此刷新不会再让悬浮说明永久留在页面上或逐次堆积节点；`stats()` 在保持 `pool_count` / `max_pools` / 单池连接上限等既有字段不变的前提下增补实例 id、起始时间与累计计数，`/debug/runtime` 与既有调用不受影响。新模板 `akm/templates/pool.html` 已登记进 `setup.py` 的 `DATA_FILES` 显式清单，发布前按 release-guide 的「新增模板 / 静态文件必须登记」自查。该改动不涉及更新管理流程本身（GitHub Release 检查 / 自动更新 / 更新包缓存策略均未变）。

## v0.1.49

管理台新增**主题切换**——右上角三个按钮「跟随系统 / 浅色 / 深色」，**默认深色且不随系统外观变化**（只有显式点「跟随系统」才会跟随 `prefers-color-scheme`），选择记在浏览器 `localStorage['akm.theme']`、刷新后保持，「跟随系统」时系统外观变化实时生效。配色改为两套 CSS 变量 token（`_styles.html` 里的 `html` 与 `html[data-theme="light"]`），再由 `tailwind.config` 映射成 `rgb(var(--c-x) / <alpha-value>)`，因此模板与组件里不再需要任何 `dark:` 变体，新页面沿用 `surface` / `surface-light` / `border` / `text-strong` 这套类名即自动跟随主题；图表中性色（网格线 / 刻度 / 图例 / 数据点）、浮层、开关、JSON 视图与对话视图（Shadow DOM，CSS 变量会继承进去并带深色兜底值）同步适配，曲线序列色与实心强调色按钮保持原色。`data-theme` 由 `<head>` 内联脚本在首次绘制前落到 `<html>`，不会先闪一下再切换。同时修复刷新时的界面抖动：浅色下侧边栏导航 hover 变白字（上一轮把落在此类表面上的 `text-white` 改为 `text-strong` 时，导航项类名里的 Jinja 条件分支被整条误判成强调色底，6 项全部漏改）；统计页骨架屏与真实内容不等高（图表卡骨架表头比真实表头矮 18px、7d/30d 缺「每日用量」占位少 327px、插件卡片区要等统计数据回来才显示少 199px），以及「时间范围」那一行（`akm-range-tabs` 在 `setOptions` 之前高度为 0，JS 跑起来后长到 30px，把下方内容整体顶 14px）——现在骨架与真实内容逐块等高（1500 / 1300 / 1200 / 1024 四种宽度实测差均为 0），首屏之后不再有任何位移。

该版本不涉及更新管理流程本身（GitHub Release 检查 / 自动更新 / 更新包缓存策略均未变）。

## v0.1.48

统计页新增**按来源分组**与**趋势折线图（报错次数 / 成功率 / P95 延迟）**。来源标签由新模块 `akm/request_source.py` 统一推导（`x-akm-source` 内部标记优先，其次 User-Agent 关键词，兜底 UA 产品名，无法识别归入「其他」），审计页「来源」列与统计页「按来源」共用同一套规则；`GET /api/stats` 新增 `by_source`（与 `by_key`/`by_model` 同结构的 Token/请求/费用分桶）与 `errors`（失败请求时序：days=1 按今天每小时 24 点，7/30 天按自然日分桶；失败口径为 status 非 2xx，含没有 key_alias 的选 Key/前置失败；每个时间点附 `top_errors` —— 该时段前 3 条高频报错，以及同口径的请求量 / 成功率 / 延迟分位数），`GET /api/logs` 新增 `source_label` 派生字段；管理台通用组件新增 `akm-line-chart`（内联 SVG 折线图，宽度自适应容器、支持 `fill` 垂直拉伸到父容器高度并按 `maxHeight` 封顶，支持 `format` 自定义提示数值、`details` 附加行自定义悬浮浮层），统计页在其上渲染趋势（卡片内用 `akm-range-tabs` 在 报错次数 / 成功率 / P95 延迟 之间切换，不重新请求接口；趋势卡片体固定 260px 高（与左侧「按来源」卡片一致），图表在盒内垂直拉伸填满、上限 260px），数据点悬浮可看该时段前几条报错或样本量；按 Key / 按模型 / 按来源三张卡片同样可用 `akm-range-tabs` 在**「表 / 图」之间切换（默认图）**，图视图为新增的 `akm-donut-chart` 环形图（Top 6 + 「其他」合并、指标可在 请求 / Token / 费用 间切换、悬浮扇区或图例即显示该行数值与占比，费用开启时浮层直接给出与表格 ⓘ 相同的费用拆解），三张卡片体固定 260px 高、表格按每页 6 行分页（复用 `akm-pagination`，不再有卡片内滚动条），因此三表等高、切换视图不跳高度。

该版本不涉及更新管理流程本身。

## v0.1.47

新增本地数据目录自动维护。服务启动与系统唤醒恢复时执行一次维护（`akm.cleanup.run_auto_maintenance`），含三步互相独立的动作：① 按 `log_retention_days` 清理过期审计日志并 VACUUM（始终执行）；② **更新包缓存清理**（`update_cache_cleanup`，默认开启）——`~/.akm/updates/` 只保留最新更新包与一个可回滚的旧版本 `.app` 备份，其余历史包与旧备份自动删除，10 分钟内修改过的文件视为更新进行中而跳过；③ **文本日志轮转**（`text_log_rotation`，默认关闭）——`error.log`、`keys.log`、`wake_recovery.log`、`plugin.launch.log` 等根目录 append-only 日志超过 `log_file_max_mb` 后转存 `.1` 并保留一代。维护只处理 AKM 自己产生的派生数据，不触碰 `config.json`、`secret.key`、`akm.db`、`plugins/`、`agent_sessions/`、`markdown_kb/` 等用户数据与插件目录；设置页「日志与存储」提供两个开关。

该版本不涉及更新管理流程本身。

## v0.1.46

修复上游转发 HTTP client 的 TLS 信任库构造缺陷——不再依赖 `certifi` 解包到临时目录的 `cacert.pem`（该临时文件被系统清理后会抛 `FileNotFoundError`，表现为审计日志成片「无法创建到上游的 HTTP 客户端」），改由 `build_upstream_ssl_context()` 显式按 `SSL_CERT_FILE`（打包 `.app` 自带 CA）→ `certifi` → 系统默认三级取用；同时静默自动更新改为**避让进行中的转发请求**，检测到在途请求/流式响应时推迟下载、替换前等待请求排空再重启，避免更新掐断请求。

该版本涉及自动更新的行为修复（静默更新避让在途请求），更新检查与安装流程本身未变。
