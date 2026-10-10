# 版本变更记录

## v0.1.55

修复侧边栏动态插入的插件菜单项（如 `markdown_kb` 的「知识库」）悬停时文字几乎不可见的问题：服务端拼接的菜单（`akm/server.py` 的 `_build_sidebar_plugin_menu`）非高亮分支误用了 `hover:text-white`，与侧边栏浅色主题（悬停背景变浅灰）叠加导致对比度过低；现与静态菜单项统一为 `hover:bg-surface-hover hover:text-strong`，active 高亮分支（`text-indigo-400`）不变。

应用图标按 Apple 图标规范修正：原 `logo.icns` 源图图形占画布 89.9%（921/1024，边距仅 51px），不符合 Big Sur 起「图形约 824/1024（80.5%）、四周各留约 100px 透明边距」的图标网格要求，导致 Dock 中图标比其他应用大一圈。现裁掉原图透明边后缩放至 824×824、居中放入 1024×1024 画布并重新生成全尺寸 `logo.icns`；`logo.png` 保持不动（它同时用作菜单栏托盘图标与网页 favicon，托盘图标按全画布缩放，改动会连带变小）。

该版本不改变更新管理策略与打包口径，无新增模板 / 静态文件。`agent_chat` 插件包 v0.1.15 的续跑回填修复随本版本首次正式发布。

## 未发布
`agent_chat` 插件发布 **v0.1.15**：修复客户端工具（`ui_list_sessions` / `ui_load_session`）续跑时被 DeepSeek 以 `Messages with role 'tool' must be a response to a preceding message with 'tool_calls'` 拒绝（HTTP 400）的问题。根因是消息拼接协议：服务端 `client_tool_call` 事件下发的 `messages` 末尾已带一条占位 tool 消息（`{"status": "awaiting_client"}`，`tool_call_id` 与本轮调用一致），前端此前在本地执行完工具后又**追加**一条新的 `role: "tool"` 结果消息，导致出现「tool 后紧跟第二条 tool」的结构——DeepSeek 严格校验 tool 消息必须紧跟 assistant 的 `tool_calls`，直接报错（OpenAI 等宽松上游不报错，但结构同样不对）。修复在前端续跑处（chat 项目 `App.tsx`）：按 `tool_call_id` 把真实结果**回填**进占位 tool 消息（一个 tool_call 恰好对应一条 tool 结果），仅在返回不含占位（旧版服务端）时降级为追加。协议文档 `akm/agent_runtime/agent.md` 的「客户端工具执行」、SSE 事件表及 `plugins/agent_chat/README.md`、chat 仓库 `README.md` 的续跑约定已同步改为「回填」口径；服务端 `loop.py` 行为未变。`plugins/agent_chat/dist/` 已重建（`VITE_BASE='./'` 相对路径），`npm run check:history` 26 项断言通过。该修复不改变 AKM 应用版本号、更新管理策略与打包口径，无新增模板 / 静态文件。

修复自动更新在「Release 只有本机架构更新包」时的错误兜底：此前 Intel（x86_64）机器匹配不到 arm64 zip 会退回任意 zip 并静默安装，而发布产物从未提供过 x86_64 包，装错架构的包会导致替换成功、应用启动即崩，且旧版已不在原位（即「不同设备更新后启动失败」）。现在 `_pick_zip_download_url` 严格按当前机器架构（arm64/x86_64，aarch64/amd64 归一化）匹配 zip 资产，匹配不到即返回空串、走「未提供更新包」提示；更新安装流程在替换旧 `.app` 前新增架构守卫（用 `file` 探测新包主可执行文件，通用二进制放行，探测失败不阻断更新），不匹配即中止安装并原位保留旧版。新增回归测试 `tests/test_menubar_update.py`（12 项）。本修复不改变版本号与打包口径，无新增模板 / 静态文件；README 打包与更新说明及 release-guide 方案 C 已同步，插件与 Web 组件文档经检查无需更新。

## v0.1.54

修复显式出站代理模式下连接池页面只探测默认 HTTPX transport、漏掉 `_mounts` 实际代理连接池而持续显示「无连接」的问题。状态快照、排队请求统计与「清理空闲连接」现在同时覆盖默认池和代理 mounts。`/api/keys/{alias}/test` 的 Key 连通性测试继续使用 AKM 显式出站代理（不读取环境代理），并补充回归测试确认代理 URL 会传给 HTTPX。

该版本不改变更新管理策略（GitHub Release 检查 / 自动更新 / 更新包缓存策略均未变），也没有新增模板或静态文件。

本文件归档 AKM 各版本的功能与修复说明（从 `README.md` 与 `docs/release-guide.md` 中原有的版本段落迁移而来，起自 v0.1.46）。更早版本请查阅 Git 历史或 GitHub Releases。

约定：

- 新版本记录**追加在最上方**，标题用 `## vX.Y.Z`，正文保持在“改了什么、影响范围、兼容性”三件事上。
- 面向用户的变更写在这里；版本号如何统一与同步（`akm/__init__.py` / `pyproject.toml` / `uv.lock`）、打包与自动更新流程见 [release-guide.md](release-guide.md)。

## v0.1.53

本次发布同时完成以下修正：会话历史现仅由浏览器 IndexedDB 保存；服务端自动落盘配置与旧目录管理代码已移除。更新包缓存清理开启时会永久清除旧版遗留的 `agent_sessions/`（不备份），且新版本不再创建该目录。`agent_chat` 生产构建使用相对资源路径，修复挂载在 `/chat` 时 JS/CSS/favicon 请求落到站点根路径而 404 的问题。
`/v1/agent` 新增**客户端工具执行协议**：能力的数据或句柄只存在于客户端本地时（浏览器 IndexedDB、编辑器缓冲区、桌面窗口等），不再需要在服务端写 Python 工具。客户端在请求里声明工具——**推荐顶层 `client_tools` 字段**（与 `tools` 相互独立，因此「只声明自己的客户端工具」不会因为传了 `tools` 而丢掉服务端的默认工具注入策略），也兼容给 `tools` 里的声明加 `"x-akm-client-tool": true`；服务端把声明一并下发给模型（标记字段在注入上游前剥掉，避免未知字段被严格校验的供应商拒绝）但**不注册 handler**，模型调用时本轮编排中断、把调用交回客户端（流式 `client_tool_call` 事件 / 非流式同名字段，`data` 含 `tool_call_id` / `name` / `arguments` / `messages` / `turns` / `usage`，`messages` 已含本轮 `tool_calls` 与已执行完的服务端工具结果），客户端本地执行后把 `role: "tool"` 结果追加入 `messages` 重新请求，同一轮 Agent 从该结果继续——与 `akm_ask_user` 的续跑约定同构。**同名规则：服务端已注册的工具优先**，命中 `ToolRegistry` 的名字一律按服务端工具处理、客户端标记被忽略并记警告，避免客户端静默替换服务端实现，保证审计日志里的工具名始终代表真实执行方。**授权**：客户端工具名一律进本次请求的授权名单（否则模型伪造未声明名字即可触发客户端执行），而未注册又没打客户端标记的声明**不进**名单——只传 schema 拿不到执行权，维持「服务端只读」的安全默认，模型调用它只会拿到一条带修复提示的 `工具未获本次请求授权` 结果（此前这条路径会退化成无法定位原因的 `未找到工具`）。非流式 `run` 与流式 `run_stream` 的工具注入与授权判定合并到 `AgentLoop._plan_tools` 一份实现，新增 `x-akm-client-tool` 标记常量。

内置聊天界面（`agent_chat` 插件，`dist/` 已重建）以 `ui_list_sessions` / `ui_load_session` 两个客户端工具直接从浏览器 IndexedDB 读会话历史。实测模型仍会调用服务器端旧工具 `akm_load_session`，因此彻底移除服务端会话历史链路（不再配置、不再写盘、不再注册工具）；会话历史只由浏览器持有。

聊天界面侧同时给会话历史工具加了**规模约束**，因为用户侧对单会话消息数不做硬上限（实测有 1700 条的会话），全量读取会把上下文和界面一起拖垮：`ui_load_session` 合并为「列表 / 搜索 / 读页」三合一入口——传 `query` 时在所有会话里搜索并返回命中片段与 `offset_from_latest`，传 `name` 时读**一页**消息（`offset 0` 为最新一页，**从最近往前翻**，单次最多 50 条、默认 20 条，返回 `total` / `has_more` / `range` 供翻页），两者都不传则等同于列出会话元数据。`limit` 在实现层被夹紧到 `[1, 50]`，模型要多大都不会一次拉全量；搜索命中总数单独统计、只回传 20 条（`truncated` 标明还有更多），且**优先保留最近的命中**——正序扫描会让 1700 条会话里最早的 20 条命中占满配额、把用户真正关心的近期记录全截掉。分页与搜索的纯逻辑抽到 `src/lib/session-history.ts` 并用 `npm run check:history`（esbuild 打包 + node 断言，26 项）覆盖边界：翻页不重不漏、`offset` 翻过头收敛到最早一页而不是给出空页、逐条夹紧。系统提示词同步加入检索纪律：**不要大量读取聊天记录或试图读完整个会话**，先 `ui_list_sessions` 了解有哪些会话、再用 `query` 定位、只读相关的一两页，且只在用户明确要求回顾历史时才查。

**循环次数配置真正生效**：`agent_max_turns` 本来就是配置项，但聊天界面在请求体里硬编码 `max_turns: 50` 把它覆盖掉了，改配置完全不生效——这是本轮修掉的一个真实回归。前端不再覆盖（传 `0` 表示沿用服务端配置），`AgentLoop` 也从「构造期缓存」改为**每次请求实时读配置**，因此界面上改完下一次请求即生效、不必重启；默认值 100 → 200（即「放大一倍」），归一化夹到 `[1, 2000]`。另新增 `GET /v1/agent/config`（同 `GET /agent/config`），返回当前生效的 `max_turns` / `max_context_tokens`，让客户端发现配置而不是重新写死一份。注意用户 `config.json` 里若已存有 `agent_max_turns: 100`，它现在才第一次真正生效——想要 200 需在配置里显式改。

**旧会话目录清理**：Agent 请求现在完全不写入 `~/.akm/agent_sessions/`；自动保存与会话保留期/文件数配置均已删除。为处理旧版本遗留数据，`run_auto_maintenance()` 在更新包缓存清理开启时会检测 `agent_sessions/`，存在就永久递归删除，不留备份；清理在启动/系统唤醒维护时执行，不在聊天请求路径上执行。

**服务端会话历史工具移除**：原 `akm_list_sessions` / `akm_load_session`（读 `~/.akm/agent_sessions/` 磁盘快照）已整体删除，连同它们的 handler 一并移除。触发原因是一个实测故障：客户端工具接上后模型**仍然**调用了 `akm_load_session`——两套会话工具同时在工具列表里，模型挑了名字更眼熟的那个，于是读到的不是用户当前正在聊的历史，而是服务端落盘的旧快照（协议接上了、数据却是错的）。修复不是「让提示词更用力」，而是让错误选项不存在：权威副本在客户端浏览器（IndexedDB），服务端就不再提供这套工具。旧版会话目录不再使用；更新包缓存清理开启时会清除遗留目录。新增回归测试 `test_builtin_session_tools_are_not_registered` 防止它们被重新注册回来。

**兼容性**：`client_tools` 是新增可选字段，不传时行为与旧版一致（未注册声明的授权判定收紧属修复——那种声明本来也无法执行，只是报错信息更难定位）；Agent 会话落盘实现已整体移除；旧配置键会在读取或保存配置时丢弃。`plugins/agent_chat/dist/` 被 `.gitignore` 忽略、不进发布包，打包前需按 `plugins/agent_chat/README.md` 重建。测试覆盖：`tests/test_agent_loop.py`（客户端工具下发与交回、续跑、服务端同名优先、未标记声明被拒、`client_tools` 不改变默认注入、同名被忽略、轮次上限实时读配置与夹紧、配置驱动轮次生效）、`tests/test_agent_router.py`（`client_tools` 透传与非法 JSON、`/v1/agent/config` 的取值/夹紧/双前缀）、`tests/test_cleanup.py`（更新包维护时删除遗留 `agent_sessions/` 目录）、`tests/test_agent_tools.py`（会话工具确实不再注册），前端分页/搜索另有 `npm run check:history` 的 26 项断言。`akm/agent_runtime/agent.md` 新增「客户端工具执行」「会话历史工具已移除」「运行时配置发现」三节；会话历史只在客户端浏览器保存并更新 SSE 事件表与 `agent_max_turns` 配置说明，`docs/design/plugin-system.md`、`plugins/agent_chat/README.md` 与前端仓库 `README.md` 同步。安全边界提示：该协议让客户端工具跑在用户已信任的边界内（对比服务端 `akm_run_shell` 那类默认关闭 + 沙箱的写工具），但服务端**不校验客户端回传的工具结果**——它能保证「工具名指向的执行方」可信，不能验证结果内容。

该版本不改变更新管理策略（GitHub Release 检查 / 自动更新 / 更新包缓存策略均未变）。

## v0.1.52

打包签名从「每次重建都是新身份」改为**可用固定身份固化**，解决「每次启动 AKM 都提示要访问桌面」这一类 macOS 目录授权反复弹窗的问题。**根因**：`scripts/build_app.sh` 以前固定用 `codesign --force --deep --sign -`（ad-hoc），而 ad-hoc 应用的指定要求（designated requirement）是 `cdhash H"…"`——产物内容一变就变，于是每次重建/自更新后系统都把它当成一个**新 App**，桌面/文稿/下载等目录授权要重新询问；实测同一份构建的 TCC 日志只有「放在桌面上直接运行的那个包」触发过 `kTCCServiceSystemPolicyDesktopFolder`，而 ad-hoc 与固定证书签名的指定要求对比是 `cdhash H"ca43a141…"` vs `identifier "com.akm.app" and certificate root = H"02a342de…"`——后者与产物内容无关。**改动**：新增 `scripts/make_signing_cert.sh`（幂等：证书已存在且有效只校验，同名但缺「代码签名」信任设置只补信任，都不重建，避免留下两张同名证书导致签名身份漂移；证书 RSA-2048 / SHA-256 / 10 年、扩展含 `codeSigning`，材料落在 `~/Library/Application Support/AKM/signing`，目录 `0700`、私钥与 `.p12` `0600`、不在仓库内；macOS 自带 LibreSSL 不支持 `openssl req -addext`，故用配置文件声明扩展；`security import` 用 `-A` 换取 `codesign` 免交互，代价是该私钥对本机任意程序可用）；`scripts/build_app.sh` 改为按 `AKM_SIGN_IDENTITY` → 本机自签证书「AKM Local Signing」→ ad-hoc 的顺序选取身份，回退 ad-hoc 时打印告警并说明后果，签名后仍执行 `codesign --verify --deep --strict`；`setup.py` 的 plist 增加 `NSDesktopFolderUsageDescription` / `NSDocumentsFolderUsageDescription` / `NSDownloadsFolderUsageDescription`，让弹窗能说明「为什么需要这个目录」（声明本身不授予权限，仍需用户确认一次）。

**效果与兼容性**：换成固定身份后，应用**第一次启动会再弹一次**目录授权（身份换代，属预期），点「允许」后重建与自动更新都不再询问；从 `/Applications` 还是 `dist/` 启动共享同一条授权记录。没有可用签名身份的机器（CI、其他开发者）行为与老版本一致——回退 ad-hoc 并打印告警，**构建不会失败**；不想生成证书也可以用 `AKM_SIGN_IDENTITY` 指定已有身份。`scripts/build_m1_dmg.sh` 复用 `build_app.sh`，因此 DMG/zip 产物自动带上同一身份。自签身份不等同于 Developer ID 签名或 Apple 公证，分发到其他机器首次打开仍会走 Gatekeeper 提示。本次未改运行时行为，`akm` 代码路径一行未动（`NS*UsageDescription` 是打包元数据）；同时更新了 [docs/design/key-custody.md](design/key-custody.md) 第 10.2/10.5 节——稳定身份消除了「自更新换签名导致钥匙串 ACL 弹授权框」这一风险，改 `secret_backend` 默认值的前置条件只剩「在签名打包后的 `.app` 上跑一遍钥匙串后端」。

该版本不涉及更新管理流程本身（GitHub Release 检查 / 自动更新 / 更新包缓存策略均未变），但提升了自更新后的体验：更新替换 `.app` 不再改变应用身份，目录授权不会因更新而失效。

## v0.1.51

主密钥（`secret.key`）保管加固，新增 `akm/secret_store.py` 集中负责密钥的存放位置、权限与后端。**P0 权限**：密钥文件按 `0600`、密钥目录按 `0700` 创建（`O_CREAT|O_EXCL` 临时文件 + `os.replace` 原子落盘，避免权限竞态与半截文件），读取既有文件时发现权限过宽会自愈并告警，`chmod` 失败只告警、不阻断启动与解密。**P1 位置**：主密钥默认路径从数据目录移到 `~/Library/Application Support/AKM/secret.key`（非 macOS 为 `~/.config/akm`），新增 `AKM_SECRET_DIR` / `AKM_SECRET_FILE` / `AKM_SECRET_KEY` 三个环境覆盖；老位置（数据目录下的 `secret.key`，仍尊重 `AKM_DB_DIR`）作为遗留路径继续被识别，首次读取时**复制**到新路径完成迁移并**保留原文件**以便回滚，新路径不可写时回退使用遗留密钥。**P2 钥匙串（已实现，默认关闭）**：新增 macOS 钥匙串后端，用 ctypes 直连 Security.framework 的 legacy keychain（不用 Data Protection Keychain——实测 `SecItemAdd` 返回 `-34018 errSecMissingEntitlement`，未签名进程不可用），增删改查与回读校验在真机全部通过且无需授权弹框；入口是 `akm secret migrate --to keychain`（复制语义，默认保留明文文件，加 `--purge-file` 才删），默认后端仍为 `file`——应用自更新重新签名后钥匙串 ACL 可能弹一次系统授权框，这一点必须在打包签名后的 `.app` 上验证过才考虑改默认值，本轮无法覆盖。**P3 轮换**：密钥环支持「当前 + 历史密钥」（解密依次尝试、加密只用当前），`akm secret rotate` 轮换主密钥并把库中存量 `api_key` 用新密钥重新加密（单事务提交；解不开的行单独上报为 `undecryptable` 而不阻塞其余 Key），默认保留 1 个历史密钥使旧密文仍可解，`--drop-previous` 可显式丢弃。**清理与防锁死**：`akm secret purge-file`（以及 `migrate --purge-file`）在删除明文密钥文件前逐个比对文件内容是否属于当前密钥环，**内容对不上就拒绝删除**并列出文件名——这是真机全链路验证时暴露并修复的真实缺陷（钥匙串里是密钥 A、明文文件里是密钥 B 时会误删 B）；`purge-file` 还要求 `secret_backend` 已切到 `keychain`（否则明文文件正是配置内的密钥来源）。生成新主密钥是唯一不可逆的动作，因此明文文件缺失时会先用钥匙串里的同一把密钥兜底（覆盖"文件被误删""迁移后把配置改回 `file`"这类会把已有 Key 锁死的路径），确实找不到任何来源才生成；加载阶段若检测到「钥匙串与明文文件不一致」会显式告警，不再静默以钥匙串为准。`akm secret rotate` / `migrate` 会探测本地服务，服务在运行时提示先重启（运行中的进程仍持有旧密钥，重启前不要 `--drop-previous` 或删明文文件）。**观测与配置**：新增 `akm secret status`（`--json` / `--probe`）、`akm doctor` 增加 `master-key` 检查（密钥不可用时报 FAIL）、`config.json` 新增 `secret_backend`（`file` / `keychain`，非法值回落 `file`）。加密格式未变（仍是与 `cryptography.fernet` 互通的 `_MiniFernet`），老密钥文件可直接读取并在首次读取时迁移；未改动 `GET /api/keys/export` 等既有接口；用户手工备份（如 `~/.akm/secret.key.zip`）不被任何流程读取、打包或删除。打包登记：新模块 `akm/secret_store.py` 已加入 `setup.py` 的 `includes`，无新增模板/静态文件。评估过程、真机证据与遗留项见 [docs/design/key-custody.md](design/key-custody.md)。

该版本不改变更新管理策略（GitHub Release 检查 / 自动更新 / 更新包缓存策略均未变）。

## v0.1.50

管理台新增**连接池页**（`/pool`，侧边栏「连接池」，在「设置」与「插件」之间）。后端新增 `GET /api/pool/status` 与 `POST /api/pool/action`：前者返回 `HttpClientPoolManager.snapshot()` —— 聚合指标（路由池数量/上限与占用率、活跃 / 空闲 / 建连中 / 失败 / 过期连接数、排队请求数、累计路由请求数、建池 / 空闲回收 / LRU 淘汰 / 手动关池 / 建池失败计数）、当前配置、实例 id 与运行时长、聚合状态等级（正常 / 繁忙 / 空闲 / 异常 / 拥塞），以及逐路由池明细（`provider / key / model / path`、状态、连接分类、排队、已路由请求、空闲时长与是否「待回收」、该池每条连接的源站 / 协议 / 状态 / 已发请求数）；连接状态直接探测 httpx 底层 httpcore 连接池（`client._transport._pool.connections` 与 `_requests`），`_pool_object()` 取不到连接池对象时退化为只展示 AKM 侧统计，不影响转发链路（注意：若 httpx/httpcore 只是改了私有属性名，连接会被误判成「建连中 / 失败」，而不是走进这条退化路径）。后者支持四个动作：`close_idle_connections`（只关各池的空闲 keep-alive 连接、保留路由池）、`evict_idle_pools`（按 `http_client_idle_ttl_sec` 立刻回收空闲路由池）、`close_pool`（按 `pool_key` 关闭单个路由池）、`rebuild`（用最新配置强制重建连接池——`_recreate_http_client_pool` 新增 `force` 参数，跳过 `should_recreate_http_client()` 的「上游连续失败次数」门槛；该计数由重建本身清零，并非时限冷却，重建后上游继续失败仍会自动触发，现有自动调用行为不变），每个动作都回传最新快照与最近运行时事件，页面无需二次请求。页面提供状态总览卡片、路由池明细表（状态徽章 + 连接明细悬浮、窄窗口只省略号截断不横向溢出；连接列固定两行展示「共 N 条 + 异常标记」与「活跃 · 空闲」，宽度不随文字长度抖动）、逐池「关闭」、重建确认弹窗、3 秒自动刷新开关与操作结果通知，并展示 `/debug/runtime/history` 的最近事件；自动刷新按内容去重（内容未变不重建 DOM），`akm-tooltip` 新增宿主销毁时的清理（收起浮层并摘除节点），因此刷新不会再让悬浮说明永久留在页面上或逐次堆积节点；`stats()` 在保持 `pool_count` / `max_pools` / 单池连接上限等既有字段不变的前提下增补实例 id、起始时间与累计计数，`/debug/runtime` 与既有调用不受影响。新模板 `akm/templates/pool.html` 已登记进 `setup.py` 的 `DATA_FILES` 显式清单，发布前按 release-guide 的「新增模板 / 静态文件必须登记」自查。该改动不涉及更新管理流程本身（GitHub Release 检查 / 自动更新 / 更新包缓存策略均未变）。

## v0.1.49

管理台新增**主题切换**——右上角三个按钮「跟随系统 / 浅色 / 深色」，**默认深色且不随系统外观变化**（只有显式点「跟随系统」才会跟随 `prefers-color-scheme`），选择记在浏览器 `localStorage['akm.theme']`、刷新后保持，「跟随系统」时系统外观变化实时生效。配色改为两套 CSS 变量 token（`_styles.html` 里的 `html` 与 `html[data-theme="light"]`），再由 `tailwind.config` 映射成 `rgb(var(--c-x) / <alpha-value>)`，因此模板与组件里不再需要任何 `dark:` 变体，新页面沿用 `surface` / `surface-light` / `border` / `text-strong` 这套类名即自动跟随主题；图表中性色（网格线 / 刻度 / 图例 / 数据点）、浮层、开关、JSON 视图与对话视图（Shadow DOM，CSS 变量会继承进去并带深色兜底值）同步适配，曲线序列色与实心强调色按钮保持原色。`data-theme` 由 `<head>` 内联脚本在首次绘制前落到 `<html>`，不会先闪一下再切换。同时修复刷新时的界面抖动：浅色下侧边栏导航 hover 变白字（上一轮把落在此类表面上的 `text-white` 改为 `text-strong` 时，导航项类名里的 Jinja 条件分支被整条误判成强调色底，6 项全部漏改）；统计页骨架屏与真实内容不等高（图表卡骨架表头比真实表头矮 18px、7d/30d 缺「每日用量」占位少 327px、插件卡片区要等统计数据回来才显示少 199px），以及「时间范围」那一行（`akm-range-tabs` 在 `setOptions` 之前高度为 0，JS 跑起来后长到 30px，把下方内容整体顶 14px）——现在骨架与真实内容逐块等高（1500 / 1300 / 1200 / 1024 四种宽度实测差均为 0），首屏之后不再有任何位移。

该版本不改变更新管理策略（GitHub Release 检查 / 自动更新 / 更新包缓存策略均未变）。

## v0.1.48

统计页新增**按来源分组**与**趋势折线图（报错次数 / 成功率 / P95 延迟）**。来源标签由新模块 `akm/request_source.py` 统一推导（`x-akm-source` 内部标记优先，其次 User-Agent 关键词，兜底 UA 产品名，无法识别归入「其他」），审计页「来源」列与统计页「按来源」共用同一套规则；`GET /api/stats` 新增 `by_source`（与 `by_key`/`by_model` 同结构的 Token/请求/费用分桶）与 `errors`（失败请求时序：days=1 按今天每小时 24 点，7/30 天按自然日分桶；失败口径为 status 非 2xx，含没有 key_alias 的选 Key/前置失败；每个时间点附 `top_errors` —— 该时段前 3 条高频报错，以及同口径的请求量 / 成功率 / 延迟分位数），`GET /api/logs` 新增 `source_label` 派生字段；管理台通用组件新增 `akm-line-chart`（内联 SVG 折线图，宽度自适应容器、支持 `fill` 垂直拉伸到父容器高度并按 `maxHeight` 封顶，支持 `format` 自定义提示数值、`details` 附加行自定义悬浮浮层），统计页在其上渲染趋势（卡片内用 `akm-range-tabs` 在 报错次数 / 成功率 / P95 延迟 之间切换，不重新请求接口；趋势卡片体固定 260px 高（与左侧「按来源」卡片一致），图表在盒内垂直拉伸填满、上限 260px），数据点悬浮可看该时段前几条报错或样本量；按 Key / 按模型 / 按来源三张卡片同样可用 `akm-range-tabs` 在**「表 / 图」之间切换（默认图）**，图视图为新增的 `akm-donut-chart` 环形图（Top 6 + 「其他」合并、指标可在 请求 / Token / 费用 间切换、悬浮扇区或图例即显示该行数值与占比，费用开启时浮层直接给出与表格 ⓘ 相同的费用拆解），三张卡片体固定 260px 高、表格按每页 6 行分页（复用 `akm-pagination`，不再有卡片内滚动条），因此三表等高、切换视图不跳高度。

该版本不涉及更新管理流程本身。

## v0.1.47

新增本地数据目录自动维护。服务启动与系统唤醒恢复时执行一次维护（`akm.cleanup.run_auto_maintenance`），含三步互相独立的动作：① 按 `log_retention_days` 清理过期审计日志并 VACUUM（始终执行）；② **更新包缓存清理**（`update_cache_cleanup`，默认开启）——`~/.akm/updates/` 只保留最新更新包与一个可回滚的旧版本 `.app` 备份，其余历史包与旧备份自动删除，10 分钟内修改过的文件视为更新进行中而跳过；③ **文本日志轮转**（`text_log_rotation`，默认关闭）——`error.log`、`keys.log`、`wake_recovery.log`、`plugin.launch.log` 等根目录 append-only 日志超过 `log_file_max_mb` 后转存 `.1` 并保留一代。维护只处理 AKM 自己产生的派生数据，并会在更新包清理开启时一并删除废弃的 `agent_sessions/`；不会触碰 `config.json`、`secret.key`、`akm.db`、`plugins/`、`markdown_kb/` 等用户数据与插件目录；设置页「日志与存储」提供更新包清理与文本日志轮转开关。

该版本不涉及更新管理流程本身。

## v0.1.46

修复上游转发 HTTP client 的 TLS 信任库构造缺陷——不再依赖 `certifi` 解包到临时目录的 `cacert.pem`（该临时文件被系统清理后会抛 `FileNotFoundError`，表现为审计日志成片「无法创建到上游的 HTTP 客户端」），改由 `build_upstream_ssl_context()` 显式按 `SSL_CERT_FILE`（打包 `.app` 自带 CA）→ `certifi` → 系统默认三级取用；同时静默自动更新改为**避让进行中的转发请求**，检测到在途请求/流式响应时推迟下载、替换前等待请求排空再重启，避免更新掐断请求。

该版本涉及自动更新的行为修复（静默更新避让在途请求），更新检查与安装流程本身未变。
