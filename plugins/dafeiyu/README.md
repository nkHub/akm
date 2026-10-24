# 大肥鱼桌宠（dafeiyu）

把 AKM 转发链路状态映射成一只 macOS 原生桌宠：无边框透明置顶窗口里的大肥鱼会随着请求
进入思考、转发、成功或失败，气泡里同时显示最近一次请求结果与 DeepSeek 余额。

> 移植自 [QCYTSN/dsh-dafeiyu](https://github.com/QCYTSN/dsh-dafeiyu)（DSH 的桌面宠物插件）。
> 原版吃的是 DSH 的会话事件（思考/查找/修改/执行/验证），AKM 是转发网关，没有会话事件模型，
> 因此这里只保留「请求生命周期」这一层语义。差异见文末「与原版的差异」。


## 素材来源与授权

| 资源 | 来源 | 授权 |
|------|------|------|
| 角色动画帧（`assets/pet/`，3444 帧 / 59MB WebP） | [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)（`assets/webm/`，固定在上游 commit [`549fff2`](https://github.com/PC2005-cloud/dsh-pet/tree/549fff2ea33c2acfb16612c76912c6137da4a758/dsh-pet/assets/webm)） | MIT，见 `assets/dsh-pet-LICENSE.txt` |
| 素材清单（`assets/pet-manifest.json`） | 同上，记录每个源视频的 SHA-256 与帧窗口 | MIT |
| 甩抛 / 漫游物理常量 | [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet) 的 `src/shared/physics.ts` | MIT |
| 插件代码（`index.py` / `pet/` / `views/`） | 本项目 | 同仓库 |

素材是 AI 生成的绿幕动画，上游转成透明 VP9 WebM，这里再按原生 24fps 重编码成
透明 WebP 帧序列（共用同一裁剪窗口，编码参数与逐帧 SHA-256 记在
`assets/pet-manifest.json`）。完整授权说明见
[`assets/ASSET_LICENSE.md`](assets/ASSET_LICENSE.md)。

**素材与代码授权分离**：代码遵循本仓库授权，角色素材始终受上游 MIT 条款约束。
## 一、适用范围

- **仅 macOS**：原生窗口依赖 AppKit（pyobjc）。Windows / Linux 会返回明确的「不支持」，
  不会静默失败。
- **仅 AKM 宿主**：状态来源是 AKM 的插件 hook 与 Key 池，不连接 DSH。
- 需要宿主环境里有能 `import AppKit` 的 Python。AKM 打包成 `.app` 时用的是项目 `.venv`，
  其中已包含 pyobjc；用纯 `uvicorn` 启动时同样可用。

## 二、目标与最终规则

### 2.1 状态映射

AKM 只有请求生命周期，因此把 hook 事件压成 6 个状态：

| 触发点 | 状态 | 素材动作 |
|--------|------|----------|
| 空闲（无在途请求且超过 `idle_after_s`） | `idle` | `idle` |
| `on_request`，且 `_inflight == 1`（本轮第一个请求） | `thinking` | `thinking` |
| `on_request`，且 `_inflight >= 2`（并发第 2+ 个） | `working` | `working` |
| 在途请求中 **≥ 90%** 已选 Key、在等上游返回 | `waiting` | `waiting` |
| 静默期（1.5s）过后，本轮成功率 ≥ 50% | `success` | `success` |
| 静默期（1.5s）过后，本轮成功率 < 50% | `error` | `error` |
| 任意时刻 `on_upstream_error`（重试期间就要看到） | `error` | `error` |

**轮次（round）驱动的展示逻辑**：一个「轮回」= 从第一个请求进来，到静默期结束。

- `on_request` 时 `_inflight += 1`：
  - `_inflight == 1`（新轮回的第一个请求）→ 清零本轮计数、作废静默期 → **`thinking`**
  - `_inflight >= 2`（后面还有请求）→ **`working`**（转发中，表示并发在处理）
- `_inflight > 0` 期间**不接受终态**：先完成的请求不立刻切 `success`/`error`，
  只累计本轮成功/失败数。这样「后续请求会延长忙碌动画」，角色不会在还有请求
  在飞时就开始庆祝或哀悼。
- `_inflight` 归 0 → 启动**静默期** `ROUND_QUIET_S = 1.5s`，先不播终态：
  - 静默期内来了新请求 → 静默期作废，直接进 `thinking`/`working`，**跳过终态**
  - 静默期平安过去 → 按**过半判定**落定终态
- **过半判定**：本轮成功率 `≥ 50%` → `success`，否则 `error`。
  平票（1 成 1 败）算成功——用户对「有成功」更敏感，也避免一次偶发失败
  就让整轮看起来是坏的。**不是「最后一个请求的结果」**：3 个请求 2 成 1 败时，
  无论谁最后返回都显示 `success`。
- 终态落定后清空本轮计数，下一个请求进来开启新轮回。
- `_state_since` 只在状态真正变化时更新，所以连续请求都处于 `thinking`/`working`
  时计时不动，循环动画不会被打断。
- 上游报错走 `force=True` 立即展示，不被挂起保护拦下（重试成功会再覆盖）。

**为什么需要静默期**：`success` 动画要 **10.12 秒**，而高频请求可能 2 秒一个。
若「结束即播终态」，实测模拟 5 个请求会有 4 个被 10 秒动画挡住，角色一直在
庆祝、完全看不出还在处理新请求。1.5 秒静默期把两种情况分开：之后没请求了
就播完整 `success`/`error`（用户能看到反馈），之后马上又来就跳过终态。

**`waiting` 状态（按请求占比判定）**：显示条件不是「等了多久」，而是
**当前在途请求里有 9 成以上在等上游返回**（`WAITING_RATIO = 0.9`）。

- **怎么知道「在等上游」**：插件注册了 `on_key_selected` hook——Key 已选、
  请求即将发往上游，这是 AKM 里唯一准确的「已发出」时点。每个请求在
  `_pending_requests` 登记表里记 `sent` 标记，`on_response` 时移除。
  `waiting = sent 数 / 登记总数`。
- **不满足 9 成就不切**：少量请求慢不该影响整体显示，其余状态逻辑照常
  （不动 `_inflight` / 轮次计数 / 静默期）。
- **已在 `success`/`error` 时不切**：那一轮的终态动画还没播完，切走会让
  success 一闪就没了。
- **`waiting` 后的走向**：
  - 成功 → 先切 `working`（「上游回话了，正在收尾」），**等 working 动画
    完整播完一轮**（10.12s）才落定 `success`；
  - 失败 → **直接 `error`**，不插 `working`（失败要尽快反馈）。
- 一般用不到，只有网络异常 / 上游挂住才会进入这条路径。

**回落规则**：`_inflight == 0` 且距最后一次状态变化超过 `idle_after_s` 时，
**所有非 idle 状态**都会回落 `idle`（`thinking` 早期不在白名单里，导致状态
永久卡死，日志实测卡过 1510 / 1591 秒）。`success` / `error` 的展示时长按
**动画实际时长兜底**（`max(idle_after_s, 动画时长 + 1)`）：`success` 需 241×42ms
≈ 10.1s，`error` 需 118×42ms ≈ 5.0s，否则动画播不完就被切走。

`on_upstream_error` 只做展示，**不返回动作字符串**——重试、换 Key、降级仍由 `error_handler`、
`fallback_router` 等策略插件决定，避免本插件改变转发行为。
### 2.2 余额查询

- 取 `provider=deepseek` 的 Key（优先 `base_url` 含 deepseek 的），请求 `{base_url}/user/balance`。
- 金额按字符串原样透传，不在插件侧做浮点换算。
- 失败保留上次成功结果并标 `stale`，气泡显示「余额(上次)」；无 Key 时显示「未配置 DeepSeek Key」，
  不影响状态动作。
- 自动刷新间隔 `balance_interval_s`（默认 60s），手动刷新最快 5s 一次。
- 凭据与上游原始错误不会进入状态接口、气泡或事件流。

### 2.3 桌宠进程

- 插件按需 spawn `pet/pet_app.py` 子进程；子进程轮询 `GET /api/dafeiyu/state`（1s）。
- 独立进程的原因：AppKit 的 `NSApplication` 事件循环独占主线程，塞进 FastAPI 事件循环会卡死服务；
  独立进程还能做到「关掉桌宠不影响代理转发」。
- 窗口：`NSPanel` + `borderless` + 透明背景 + `NSStatusWindowLevel` + `canJoinAllSpaces`，
  与原版 Swift 实现同构。
- 交互：拖动（屏幕绝对坐标 1:1，超过 5px 阈值才算拖动）、点击摸头（`head_pat`，缺失时退回 `poke`）。
- 右键菜单：**选择动画**（子菜单列出素材里全部 16 组动作，选中的打勾；另有「跟随状态」
  恢复由 AKM 状态驱动）、**跟我说话…**（弹输入框聊天）、**说点什么**（手动触发一次碎碎念）、
  **隐藏这次运行**、**退出桌宠**。大小 / 减少动态 / 始终置顶等外观项统一走插件配置，
  避免右键与配置两处入口状态不一致。
- 播放优先级：手动锁定（右键选的动画）> 一次性动作（摸头 / 拖拽 / 待机装饰）> 状态动作。
  手动锁定期间不插播摸头与装饰动作，且素材标了 `loop=False` 的动作也会循环播放，
  直到主动点「跟随状态」。
- 鼠标事件：角色图层（`PetImageView`）与气泡（`PassThroughView`）都重写 `hitTest:` 返回 `None`，
  事件统一落到 `PetView`。注意 `setIgnoresMouseEvents:` 只有 `NSWindow` 有，视图上调会抛
  `AttributeError`（曾因此导致建窗中断、桌宠完全不出现）。
- 位置持久化到 `~/.akm/dafeiyu/layout.json`；对话与碎碎念记忆存 `~/.akm/dafeiyu/memory.json`。
- 素材按 clip 懒加载并缓存，避免一次载入 3444 帧。

### 2.4 物理与交互（移植自 dsh-pet）

物理逻辑单独成 `pet/physics.py`（纯函数、无 AppKit 依赖），常量与
[`dsh-pet/src/shared/physics.ts`](https://github.com/PC2005-cloud/dsh-pet) 逐条对齐：

| 能力 | 关键参数 | 说明 |
|------|----------|------|
| 拖拽弹簧跟手 | `SPRING_K=200` / `SPRING_C=30` | 窗口位置追鼠标而非硬跟，快速甩动有轻微滞后与回弹 |
| 甩抛抛物线 | `GRAVITY=1400` / `RESTITUTION=0.78` | 松手速度由拖拽轨迹估算，低于死区（500px/s）视为「温柔放下」原地停住 |
| 边缘反弹 | `RESTITUTION=0.78` | 撞屏幕左右边缘反弹并衰减；顶部默认不夹（可飞出屏幕再落回） |
| 落地摩擦 | `GROUND_FRICTION=2.5` | 落地后水平速度衰减，直到静止 |
| Q 弹挤压 | `SQ_SQUASH=0.55` / `SQ_DURATION_MS=220` | 下压 ease-in + 回弹 easeOutBack（过冲约 4%）；冲击越大压得越狠，轻落地只压到 0.8 |
| 多屏边界 | — | 用 `NSScreen` 的 `visibleFrame()` 做边界、`frame()` 做跨屏探测：屏缝两侧有屏就放行（可跨屏飞），空洞当墙 |

「锁定在当前屏幕」（`lock_screen`）开启后，屏缝与空洞一样当墙，甩不出去。

### 2.5 屏幕漫游

待机时朝朝向方向走，走到目标停下等下一轮（间隔 6–14 秒，速度 90px/s）。
落点由 `plan_move` 规划：身体左右缘各外扩安全距后都必须落在某块屏上，
越界返回 `None` 则翻转朝向重试一次。只在 `idle` 状态走，有任务状态、
拖拽中、甩抛中、手动锁定动画时都不走。

### 2.6 对话与碎碎念

- **对话**：右键「跟我说话…」，输入框**直接嵌在气泡最下面一行**（不弹独立窗口），
  回车或点「发送」提交、Esc 取消。走 AKM 自己的 `/v1/chat/completions`
  （计费 / 审计 / 限流照常生效），回复进气泡并保持 8 秒不被状态文案覆盖。
  气泡因此加高到 124px：上面三行继续显示状态 / 结果 / 余额，底部留给输入行。
- **碎碎念**：待机时按 `whisper_interval_s`（默认 180s）自动说一句，也可右键手动触发。
  **模型同时决定「说什么」和「配哪个动画」**：提示词要求输出
  `{"text": "...", "clip": "..."}`，`clip` 只能从 `WHISPER_CLIPS`
  （`head_pat` / `poke` / `tail` / `eat_token` / `idle_stretch` / `idle_yawn`）里选。
  收到后先播该动作、同时用打字机显示文本。`parse_whisper_reply()` 做容错：
  剥 markdown 围栏、抠 JSON、clip 不在列表就丢弃、纯文本则整段当气泡文本
  （绝不编造动作名——不存在的 clip 会让桌宠静默不播）。
  **气泡强制显示**：碎碎念本身就是一句话，若用户关了「显示气泡」就完全看不到。
  所以展示期临时打开气泡（复用 `_bubble_was_enabled` 机制），
  **展示期结束自动还原成隐藏**，不会改掉用户的设置。
- **记忆持久化**：对话保留最近 20 轮、碎碎念最近 30 条，落在
  `~/.akm/dafeiyu/memory.json`；文件损坏按空记忆继续，不影响启动。
- **失败不伪造**：模型不可用时气泡明确显示失败原因，不编造回复。
- **模型未配置时**对话与碎碎念不可用（气泡提示去插件设置里选模型）。

### 2.7 系统通知

窗口不在前台时，对话回复与碎碎念改用 `osascript` 弹 macOS 系统通知
（节流 10 秒）。不用 `NSUserNotification`：它在较新 macOS 上已废弃且需要签名授权。

### 2.8 运行日志与自查

桌宠子进程的 stderr 全部重定向到 `~/.akm/dafeiyu/pet.log`（超过 2MB 轮转为 `pet.log.1`），
每次启动写一行 `===== 桌宠启动 <时间> =====` 分隔。日志包含：

- **鼠标事件**：按下 / 松开（含 `clickCount`）/ 开始拖拽 / 甩出速度
- **心跳**：每 30 秒一行，记录当前 clip、override、manual、dragging、throwing、frame
- **异常栈**：`tick`、对话等关键路径的异常（同种异常 5 秒内只记一次）

**排查「卡住」的方法**：看心跳是否还在跳——停了说明进程僵死；还在跳说明是动画/渲染层问题。
插件页有「运行日志」面板可直接看，也可以 `GET /api/dafeiyu/log?lines=200`。

## 三、接口

前缀由 `plugin.json` 的 `routes_prefix` 提供（`/api/dafeiyu`）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/state` | 桌宠轮询用：状态、动作、文案、计数、余额、窗口配置 |
| GET | `/status` | 插件页用：`state` + `pet`（进程/平台/错误）+ `assets`（动作与帧数） |
| POST | `/pet/start` | 启动桌宠进程 |
| POST | `/pet/stop` | 停止桌宠进程 |
| POST | `/pet/restart` | 重启（改完窗口配置后调用） |
| POST | `/balance/refresh` | 手动刷新余额（5s 限频） |
| GET | `/assets/manifest` | 素材清单 |
| GET | `/assets-static/*` | 素材只读静态挂载（设置页预览首帧用） |

`dashboard_card()` 在管理台首页渲染一张卡片：当前状态（中文）、请求 / 成功 / 失败计数、余额。
不含「最近请求」列表——卡片只放聚合指标，逐条记录看审计日志。

## 四、关键实现位置

| 文件 | 职责 |
|------|------|
| `plugin.json` | 插件元数据、菜单、hook 声明、19 项 settings |
| `index.py` | 状态采集、余额查询、桌宠进程管理、HTTP 路由、首页卡片 |
| `pet/pet_app.py` | 原生窗口进程：建窗、逐帧播放、鼠标交互、布局持久化 |
| `pet/physics.py` | 甩抛 / Q 弹 / 弹簧 / 漫游的纯函数物理（无 AppKit 依赖，可单测） |
| `pet/companion.py` | 对话与碎碎念：提示词、记忆读写、LLM 调用、回复清洗 |
| `tests/test_dafeiyu_physics.py` | 物理 41 项单测（常量对齐、曲线性质、边界行为） |
| `tests/test_dafeiyu_companion.py` | 对话 26 项单测（记忆裁剪、提示词、清洗、错误路径） |
| `views/index.html` | 插件设置页（iframe 内）：运行状态、余额、动作预览、最近请求 |
| `assets/pet/` | 16 组动作、3444 帧 WebP（412×344） |
| `assets/pet-manifest.json` | 帧清单（clip → 帧列表 / frameMs / loop） |

## 五、配置项

| 设置 | 默认 | 说明 |
|------|------|------|
| `enabled` | 开 | 关闭后不采集状态、不拉起桌宠 |
| `autostart_pet` | 开 | 插件启用时自动拉起桌宠 |
| （无） | — | 角色大小固定 50%，不提供设置项（100% 会挡视线，50% 是默认观感） |
| `bubble_enabled` | 开 | 显示气泡 |
| `bubble_show_balance` | 开 | 气泡显示余额 |
| `balance_interval_s` | 60 | 余额刷新间隔 |
| `idle_after_s` | 8 | 结果状态回落待机的秒数 |
| `reduce_motion` | 关 | 固定姿势，停止动画 |
| `sound_enabled` | 关 | 成功/失败播放系统提示音（`afplay`） |
| `allow_drag` | 开 | 允许拖动 |
| `always_on_top` | 开 | 始终置顶 |
| `track_agent_requests` | 关 | 是否统计 AKM 自身 `/v1/agent` 的内部转发 |
| `chat_model` | 空 | 对话模型（下拉来自 `/v1/models`）。留空则对话与碎碎念不可用 |
| `chat_prompt` | 见 plugin.json | 对话人设（系统提示词） |
| `whisper_enabled` | 开 | 待机时自动碎碎念 |
| `whisper_interval_s` | 180 | 两次碎碎念最短间隔（每次都会调一次模型） |
| `whisper_prompt` | 见 plugin.json | 碎碎念人设 |
| `notify_unfocused` | 开 | 窗口不在前台时弹系统通知 |
| `lock_screen` | 关 | 漫游与甩抛只在本屏内，不跨显示器 |

## 六、验证结果

在本机（Darwin arm64 / Python 3.12.13 / pyobjc 12.2.2）实测：

- 宿主加载插件、`/state`、`/status`、`/assets/manifest`、`/assets-static/*` 均返回 200。
- 桌宠进程真实弹出无边框透明窗口，`idle` 动作正常播放，三行气泡显示状态 / 结果 / 余额。
- 一次真实转发（无 Key）把状态推到 `error`，`counters` 与 `recent` 正确记录 `503 / select_key`。
- `/pet/stop` → `running: false`；`/pet/start` → 新 pid；`/balance/refresh` 在无 Key 时返回 `no_key`。
- 插件宿主页 `/plugins/dafeiyu`、iframe `/plugins/dafeiyu/raw`、首页卡片、侧边栏菜单均正常。
- 物理与对话模块单测：`tests/test_dafeiyu_physics.py`（41 项）+ `tests/test_dafeiyu_companion.py`（26 项）全通过。
- 漫游实测：注入临时日志确认规划出落点（`target=955.0 dir=-1`）并真实移动。
- 仓库测试 `1079 passed`。

## 七、已知边界

- **仅 macOS**。Windows / Linux 的原生窗口需要另写实现（原版用 Qt Helper）。
- **状态粒度粗**。没有 searching / editing / testing 这类工具语义，只有请求阶段。
- **`working` / `waiting` 不是 hook 直给的状态**：AKM 没有「上游开始返回」
  这类细粒度回调。`working` 由「并发第 2+ 个请求」触发；
  `waiting` 由「在途请求里 ≥ 90% 已选 Key、在等上游」触发（`on_key_selected`
  标记 + `on_response` 移除，靠登记表算比例）。因此这两个状态的触发时机
  取决于请求并发度与上游响应速度，不是精确的事件边界。
- **碎碎念会消耗额度**：每次自动碎碎念都是一次真实模型调用。默认 180s 间隔，长时间挂着会持续计费，
  不想付费就把 `whisper_enabled` 关掉。
- **`Quartz` / `CoreGraphics` 不可用**：本环境只有 pyobjc 的 AppKit + Foundation，因此 Q 弹挤压用
  `NSAffineTransform` 而不是 `CATransform3DMakeScale`。
- **系统通知走 `osascript`**：首次可能触发「终端/应用想发送通知」的授权提示。
- **Q 弹挤压不能用 layer.anchorPoint**：改锚点会连带改变图层定位基准而 `position`
  不补偿——实测挤压一次后 `anchorPoint` 从 `(0,0)` 变 `(0.5,0)`，图层永久左移半个宽度，
  角色只剩右半边（用户报的「只剩一半」）。现在直接调整 view 的 frame（横向居中、
  纵向贴底），不碰 layer 状态，挤压结束能精确还原到 412×344。
- **字号下限 12pt**：字号低于 12pt 就看不清了，所以 `max(12.0, base * scale)`。
  连带两个后果：① 气泡高度不能再机械乘 `scale`（小尺寸下装不下三行 12pt 文字），
  改为按 `line_h * 3 + 边距` 撑开；② 行间距用固定 8px 而非 `8 * scale`，
  否则 40% 时只有 3.2px 太挤。
- **窗口位置必须夹进屏幕**：`layout.json` 存的是上次坐标，之后把角色调大或换分辨率，
  旧坐标会让窗口右半截跑到屏幕外——「发送」按钮正好在右边缘就被屏幕裁掉
  （用户截图里「发送」只剩「发」就是这个原因，不是布局 bug）。
  `_clampToScreens()` 在建窗、`_resizeWindow`、以及 **`_save_layout` 写盘前**都要调用。
  漏掉保存这一步会形成死循环：启动时夹取把窗口拉回屏幕内，但保存又写回越界坐标，
  于是每次启动都在屏幕外——这正是「改了还是被裁」的原因。
- **`CHAT_ROW_H` 必须 ≥ 输入框高 + 底边距 + 文字间隙**：26 + 8 + 2 ≈ 34。
  取 24 时文字区被压缩、间距变挤。
- **输入框底边距用 `pad` 而不是 `pad-2`**：输入框有 1px 边框，贴太近会被气泡的
  `masksToBounds` 裁掉下边框（截图里输入框下沿缺失就是这个）。
- **发送按钮高度必须与输入框一致**：用 `line_h + 2` 时是 30px，比输入框高 6px，
  会顶出气泡底部被裁——截图里「发送」只剩「发」就是这个原因。
- **`_bubbleHeight()` 里不能用 `len(self.bubble_rows)`**：建窗过程中
  `_buildBubble` 还没填 rows，此时 len() 是 0，会算出只有边距高的气泡（20px）。
  用常量 `BUBBLE_ROWS=3`。
- **输入框高度与行高解耦**：`CHAT_INPUT_H=24`，不要用 `line_h + 4`（行高 28 时
  输入框会高达 32px，显得又高又笨）。
- **发送按钮宽度 60px**：50px 放不下「发送」两个字，会被裁成「发」。
- **字号必须跟随 scale，且有下限**：行高按 `scale` 缩放而字号不缩，40% 时行框
  只有 11px 却要放 15px 的文字，三行直接重叠。字号改为 `max(8, base * scale)`，
  行高按「最大文字高度 + 间距」反推而不是机械乘 scale——这样即使字号触到
  8pt 下限，行高也会跟着撑开，永不重叠。
- **label 高度要贴合字体，不能等于行高**：`NSTextField` 把文字画在 cell **顶部**，
  不是垂直居中。若 label 高 22px 而文字只占 17px，多出的 5px 全落在文字下方，
  三行看起来就紧贴在一起。正确做法：label 的 frame 高度取
  `ascender - descender + 1`，行距由外层 `BUBBLE_LINE_H`（22px）提供，
  label 在行框内垂直居中。
- **聊天行不随 scale 缩放**：输入框字号固定 12pt、发送按钮标题固定 11pt，
  两者都不跟角色大小变。所以输入框高度、按钮宽高都用固定值
  （`CHAT_INPUT_H=26`、`CHAT_BTN_W=50`、`CHAT_ROW_H=34`），只有文字区和角色
  画布乘 scale。曾经把这行也乘 scale，`scale=50` 时输入框只剩 13px、
  按钮只剩 25px，装不下 22.5px 的「发送」→ 被裁成「发」。
- **发送按钮要用 mini（`controlSize=3`）**：small 的 bezel 绘制高度固定 14px，
  比 26px 的输入框矮一截，视觉上「按钮没对齐」；mini 的 bezel 约 24px，与输入框齐平。
- **输入框文字要自己垂直居中**：`NSTextField` 把文字画在
  `drawingRectForBounds:` 返回区顶部，26px 框里文字贴顶、下方留白。
  `CenteredTextFieldCell` 重写该方法，返回「文字高度、纵向居中」的
  `NSMakeRect`（**必须用 NSMakeRect 返回 NSRect**，返回元组 ObjC 侧会拿不到值）。
- **输入框与文字靠几何关系避让**：打开输入框时文字区**从输入框上沿往上排**，
  而不是靠调 `CHAT_ROW_H` 凑数。凑数会在某个 scale 下压到第三行。
- **余额开关要真的生效**：`bubble_show_balance` 下发到 config 后必须由
  `_format_lines` 检查，否则用户关了开关仍能看到余额（连「未配置 Key」提示也不该出现）。
- **气泡高度是动态的**：基础态 = 三行行高 + 上下边距；打开对话输入框时才加
  固定的 `CHAT_ROW_H=34`。固定高度会在平时底部空一块。开关输入框都要
  调 `_resizeWindow()`，否则窗口尺寸不同步、输入框被裁掉。
- **`NameError` 会让桌宠彻底停摆**：`tick` 是 NSTimer 回调，函数体里任何
  `NameError`（例如漏 `import math`）都会让定时器停止重复触发。历史上真踩过
  `math` 与 `subprocess` 两次漏导入。`tests/test_dafeiyu_static.py` 用 AST
  检查「函数体引用了但模块级不存在」的名字，把这类问题挡在提交前。
- **第 0 行允许换行、后两行单行截断**：第 0 行是对话/碎碎念回复或状态文案，
  最容易超长（曾经被截成「你好哇～大肥鱼甩甩鳍，溅起一…」）。现在第 0 行
  `cell.setWraps_(True)` + `setLineBreakMode_(0)`（按词换行）+ `maximumNumberOfLines_`
  上限 `BUBBLE_TEXT_LINES=3`；第 1/2 行是结构化短信息，保持 `wraps=False` + 尾部省略。
  三处都要设，缺一不可——只设 `label.setLineBreakMode_` 无效，真正排版的是内部 cell。
  **气泡高度必须按实际行数撑开**：`_titleLineCount()` 用
  `NSString.boundingRectWithSize_options_attributes_` 测量换行行数
  （`options` 必须含 `1` = UsesLineFragmentOrigin，否则永远返回 1 行；
  Python `str` 没有该方法，要先包成 `NSString`）。
- **空行不占高度**：对话回复时第 1/2 行是空的，若也按行占位，气泡底部会空出
  一大块（文字与输入框之间明显留白）。`_bubbleHeight` 与 `_placeRows` 都只统计
  「有内容」的行。
- **打字机效果**：对话/碎碎念回复用 `_setBubbleLines(..., typewriter=True)` 逐字
  显示，`_tickTypewriter` 在 tick 里按 `TYPEWRITER_CHAR_S=0.035s/字` 推进，
  长回复按 `TYPEWRITER_MAX_S=6s` 反推间隔；掉帧时按经过时间补字。
  **测量要用完整文本**：逐字时 label 里只有前几个字，`_titleLineCount()` 优先读
  `_typewriter_text`，否则行数永远是 1、气泡不会撑开、后半段被裁。
- **任何地方都不能强制中断正在播的动画**：原来服务端 `clip` 一变就立刻切
  `state_clip` 并把 `frame_index` 归零，动画从第 1 帧重头开始——高频状态切换
  （thinking→working→waiting）时看起来就是一帧一帧地闪。现在改为**排队等待**：
  新状态记到 `_pending_state_clip`、新的临时动作记到 `_pending_override_clip`，
  由 `_tickOnce` 在**当前动作播完一轮的轮末**才真正切换。循环动作
  （idle/thinking/working/waiting）自然走到轮末，一次性动作（success/error/
  摸头/拖拽）播完才切，都不会被打断。唯一例外是用户在右键菜单**主动**选动画
  （显式意图，允许立即切换）。
- **关闭 AKM 时桌宠必须一起退出**：子进程用 `start_new_session=True` 脱离进程组，
  宿主被强杀/崩溃时收不到 SIGTERM，会留下孤儿窗口。两道保险：
  ① 正常关闭走 `on_unload → stop_pet()`（terminate，3 秒后 kill）；
  ② 宿主 pid 通过 `AKM_DAFEIYU_PARENT_PID` 传给子进程，桌宠每 2 秒用
  `os.kill(pid, 0)` 探测一次，父进程不存在就 `AppHelper.stopEventLoop()` 自行结束。
- **`hitTest:` 不能加 `@objc.python_method`**：加了之后方法只存在于 Python 侧，
  ObjC 调 `hitTest:` 仍走 NSView 原生实现并返回视图自己，鼠标事件被角色/气泡层吃掉，
  表现为「点不动、拖不动、右键失灵」。必须是真正的 ObjC 方法。
- **`tick` 必须整体包 try/except**：它是 NSTimer 回调，一旦抛异常 NSTimer 会停止重复
  触发，动画与物理永久停摆——这正是「卡住」的直接机制。
- **对话输入窗必须能成为 key window**：应用以 `Accessory` 策略运行，普通 `NSPanel`
  在该策略下 `canBecomeKeyWindow` 为 False——窗口能显示但拿不到键盘焦点，表现为
  「输入后没反应」。`ChatPanel` 子类显式重写该方法，并在显示时 `activateIgnoringOtherApps_`。
  另有「发送」按钮带 `\r` keyEquivalent 作为回车兜底。
- **素材随仓库提交，但不进插件市场分发**：素材已随本仓库 git 全量提交（与上游
  [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet) 一致的做法：直接入库、
  不用 git-lfs），克隆后开箱可用。但**发布成插件市场 zip 时仍应剔除素材**——59MB /
  3444 帧会让包体过大。素材授权与代码授权分离（见 `assets/ASSET_LICENSE.md`、
  `assets/dsh-pet-LICENSE.txt`），分发前需先确认体积与授权策略。
- **打包成 `.app` 后**：`sys.executable` 指向 App 内二进制，`_resolve_python()` 会回退到系统
  Python 探测；若系统 Python 没有 pyobjc，桌宠无法启动，会在插件页给出明确错误。
- 窗口位置只记坐标，不跟随多屏热插拔重算。

## 八、与原版的差异

| 维度 | dsh-dafeiyu | 本插件 |
|------|-------------|--------|
| 宿主 | DSH（DeepSeek Harness） | AKM（转发网关） |
| 状态源 | DSH 会话事件 | AKM 请求生命周期 hook |
| 平台 | Windows / Linux / macOS（三套 Helper） | 仅 macOS（pyobjc） |
| 页面内角色 | 支持（DSH 页面右下角） | 不做 |
| 托盘 / 全局热键 / 开机自启 | 支持 | 不做 |
| 余额来源 | DSH 凭据服务 + 已登录账户 | AKM Key 池（`/user/balance`） |
| 素材 | 随 npm 包分发 | 随 git 仓库提交（MIT，来自 dsh-pet），但不进插件市场 zip |
