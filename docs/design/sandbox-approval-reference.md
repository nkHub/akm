# 沙箱与审批：参考 deepseek-harness 的分离设计

> 状态：调研记录，**不含代码改动**
> 参考实现：`Ecology/deepseek-harness`
> 本文只回答一个问题：AKM 现在的沙箱为什么处处误伤，以及业界更成熟的划法是哪种。

---

## 1. 一句话结论

deepseek-harness 把「沙箱」和「审批」拆成**两个互不耦合的旋钮**，而 AKM 现在把两条边界都压在同一个东西上——**路径白名单**。这是误伤的根源，不是策略写得不够严。

---

## 2. 两个旋钮

### 2.1 SandboxMode —— 只管文件系统效果

```ts
type SandboxMode = 'read-only' | 'workspace-write' | 'danger-full-access'
```

| 值 | 语义 |
|---|---|
| `read-only` | 拒绝写入（POSIX runner 仍授予 `/dev/null` 所需的接收器） |
| `workspace-write` | 允许写工作区根目录 + 后端承诺的临时区域 |
| `danger-full-access` | 绕过隔离，消费方直接 spawn 原始 argv，不调用 `ctx.sandbox` |

**最关键的一句，原文引用：**

> `SandboxMode` 仅管控文件系统效果。……**网络与进程可见性不在此处的定义范围内。**

也就是说：**「读」不受这个旋钮管。** 这一条直接决定了 AKM 遇到的三个问题在 harness 里都不会发生——`certifi/cacert.pem` 读不到、`cd` 被 metadata 拦、`~/.pyenv` 执行被拒，三者全是「读」或「执行」，不在沙箱管辖范围。

### 2.2 ApprovalPolicy —— 决定「问不问」

```ts
type ApprovalPolicy = 'ask' | 'never'
```

| 值 | 语义 |
|---|---|
| `ask` | 委托给组合的应答者链；**链上无应答者时降级为 `unavailable`（即拒绝）** |
| `never` | **确定性返回 `rejected`，不分发任何应答者** |

⚠️ **`never` 不等于「全部放行」。** 它的含义是「永不询问，直接拒绝」，是无人值守 / CI 的严格姿态。这个语义很容易理解反，实现时务必对齐。

### 2.3 为什么必须拆开

一个枚举同时表达「能不能写」和「要不要问人」，语义必然打架。AKM 现在的 `accept-edits` 就是典型：它到底指「不问」还是「允许写」？说不清。

---

## 3. 审批失败必须闭合

```ts
type ApprovalOutcome = 'allowed-once' | 'rejected' | 'cancelled' | 'unavailable'
```

- `allowed-once` **仅授权所询问的那一个操作**，不是一类操作
- `rejected` / `cancelled` / `unavailable` 三种，调用方一律按拒绝处理
- **应答者缺失、不负责该请求、抛异常、不合规，全部产生 `unavailable`，而不是放行**

这条设计的意义：**没有 UI 的时代也是安全的。** 模型发起的越界请求不会因为「没人能回答」而变成通过。

---

## 4. 预设层：把两旋钮捆成具名档位

### 4.1 预设表

```ts
interface PresetSpec {
  sandbox: SandboxMode
  approval: ApprovalPolicy
  name?: string          // 展示标签，缺省用表键
  description?: string   // 一句话说明
}
```

默认配置表自带两组：

| 预设 | sandbox | approval |
|---|---|---|
| `workspace-write` | `workspace-write` | `ask` |
| `danger-full-access` | `danger-full-access` | `never` |

`custom` 与 `auto` 是**保留名称，不能配置**——配置表里出现这两个名字，插件加载时直接抛异常。

**配置错误在加载时失败，不是运行时才发现。** 很不显眼但很重要：`workspace-write` 被组合到「不施加隔离的 bash 执行器」之上时，同样在加载阶段抛错，因为预设捆绑了一个沙箱模式，而底层执行器没有 `sandboxMode` 能力事实。

### 4.2 `custom` 是派生值，不是可选项

> `custom` 只是派生值：客户端可以把它显示为当前值，但它**绝不是切换目标**，也绝不出现在事件 payload 中。

`current(session)` 的求值顺序：

1. 仍然匹配的「上次选择」优先（解决两个预设共享同一组旋钮值时的歧义）
2. 否则取第一个匹配的配置条目
3. 否则返回 `CUSTOM_PRESET`（`'custom'`）

**为什么这个设计好：** 用户可以手动把旋钮调成任意组合，界面会如实显示 `custom`，但他**没法「切到 custom」**——避免「不知道自己处在哪个档」。

### 4.3 会话级切换 vs 未来会话默认值

两层 UI，职责严格分开：

| 位置 | 影响范围 |
|---|---|
| General Settings 行 | 只改**未来会话**的默认值，绝不影响已打开会话 |
| composer 控件 + `/permission` 命令 | 切**当前会话** |

---

## 5. 事件与持久化

### 5.1 审计事件对

`approval/asked` + `approval/decided` **成对写入会话日志，仅写日志，不进入模型 transcript**。

模型能看到的只是运行时上下文快照里折叠出来的策略描述。这条划得很清：**审计是给人的，不是给模型的。**

### 5.2 `permission/preset` 是「用户意图」而非「执行指令」

> `permission/preset` 是持久、仅记日志的用户意图：它不进入模型 transcript……**它存在是为了在两个预设共享同一个旋钮组合时，让 `current()` 仍能保住用户选择的究竟是哪一个预设。**

同一轮次内，**选择事件先于旋钮事件**出现；重新选择当前生效的预设则什么都不追加。

### 5.3 `permissions` 投影只含 `currentValue`

目录变化（`permission-presets/catalog-changed`）**不会**追加 Session 事件、不会发布投影帧、不改变 Session 序列。通知是**无 payload** 的：消费方先订阅，再重新读取完整目录。

---

## 6. 强制执行完整性是「报告的事实」，不是布尔

```ts
type SandboxEnforcement = 'full' | 'partial'
```

`partial` 表示活跃后端或较旧内核 ABI 只能管控承诺效果的子集。已知的 partial 情形：较旧的 Landlock ABI、Windows ACL runner 的 Everyone 与硬链接边界。

**要求绝对保证的消费方必须拒绝或向上暴露这一区别，不能把 `partial` 当 `full`。**

---

## 7. 与 AKM 现状对照

| 维度 | AKM 现在 | deepseek-harness |
|---|---|---|
| 隔离粒度 | **路径白名单**（allow 工作区 + TMP，其余 deny） | 文件系统**效果**（读不受限，只管写） |
| 读写区分 | 不区分，读也被拦 | **只管写**，读天然放行 |
| 网络 | 未纳入策略 | **明确声明不在范围内** |
| 敏感文件 | `.pem`/`.key`/`.env` 一律拦内容 | 不在沙箱层拦；由审批层与工具层处理 |
| 审批 | `allowlist` 模式，逐条放行（当前仅 `Shell(ls)`） | 两个策略值 + 越界时按需升级 + 闭合结果 |
| 工具链可用性 | `~/.pyenv` 不在白名单 → 打包失败 | 不受影响（读/执行不在管辖内） |
| 反馈时机 | **事前**逐条拦截 | **事后**越界才问人 |
| 失败语义 | 拒绝 | 关闭（`unavailable` → 拒绝） |

### 7.1 AKM 的具体误伤点（已实测）

| 现象 | 根因位置 |
|---|---|
| `certifi/cacert.pem` 复制失败，打包中断 | `tools.py` 的 `_WORKSPACE_SENSITIVE_SUFFIXES` 含 `.pem` + seatbelt 正则同步 deny |
| `cd /Users/nk` / `cd .` / `cd ..` 全报 `Not a directory` | `deny file-read-metadata (subpath home)` 拦了整棵树的 metadata，`cd` 解析路径要先过父级 |
| `.venv/bin/python` → `~/.pyenv/...` 执行被拒 | `~/.pyenv` 不在 allow 列表 |
| `git push` 取不到凭据 | `~/.ssh`、`~/Library/Keychains` 被 deny，`gh` 未装 |
| 敏感名单对 shell 无效 | 名单只在文件工具层生效；`akm_run_shell` 里 `cat .env` 直接成功 |

> 最后一条要特别注意：**这道名单是「防手滑」，不是「防越权」。** 只要 `agent_run_shell_enabled=true`，路径层的拦截就没有安全意义。文档里不能把它写成安全边界。

---

## 8. 迁移路线（有顺序约束）

### 第一步：沙箱只管写，不管读

用 `read-only` / `workspace-write` / `danger-full-access` 三档取代路径白名单。

效果：`cacert.pem`、`cd`、`~/.pyenv` 三类问题一次性消失。

### 第二步：审批改成「越界升级」

现在的 `allowlist`（逐条拦）→ 改成「执行时放行，被沙箱拒绝后带升级意图重试时才问人」。

**前提条件：审批 UI 必须先落地。** 否则模型申请升级无人应答，只会 fail-closed 成 `unavailable`，等于什么都没批。

### 第三步：`custom` 的呈现规则

手动拼出的组合要在界面上显示成 `custom`，但**不可作为切换目标**。

---

## 9. 不要照抄的地方

**1. 不要把 `danger-full-access` + `never` 当默认。** harness 里这组是 Auto 预设，它自洽的前提是「沙箱已经全面放开，没有越界可拦，因此无需审批」。但在 AKM 里，如果审批 UI 还没做，照抄这个组合等于**两层边界同时消失**。

**2. 不要把沙箱当安全边界宣传。** harness 自己的 `SAFETY.zh.md` 写得很老实：

> 沙箱、审批提示与权限控制可以降低风险，但不保证隔离……**不要把 DeepSeek Harness 当作不可信工作负载唯一的安全控制措施。**

这层边界的定位是**减少事故**，不是对抗恶意。AKM 的文档也应保持同样口径。

**3. `partial` 不能当 `full` 用。** 如果 AKM 未来接入多后端，必须把 enforcement 完整性作为事实上报，而不是自己假设「一定管得住」。

---

## 10. 短期可做（不涉及架构改动）

**`.pem` 按文件名分段处理**，今晚就能完成：

- 放行读取：`cacert.pem`、`cert.pem`、`chain.pem`、`ca-bundle.crt`、`ca-certificates.crt`
- 继续拦截：`.key`、`.p12`、`.pfx`、`.keystore`、`.jks`，以及 `*key*.pem`、`*priv*.pem`、`*secret*.pem`
- 文件工具层额外做**内容嗅探**：读到 `-----BEGIN ... PRIVATE KEY-----` 就拒绝内容读取

理由：`cacert.pem` 是 certifi 打包的公钥基础设施，公开无秘密，拦它纯属误伤；私钥几乎不会叫 `cacert.pem`，命名足以识别绝大多数情况。

---

## 11. 参考资料

| 内容 | 路径 |
|---|---|
| 沙箱模式与强制执行 | `deepseek-harness/docs/subsystems/sandbox.zh.md` |
| 审批策略与结果 | `deepseek-harness/docs/subsystems/approval.zh.md` |
| 权限预设层 | `deepseek-harness/docs/subsystems/permission-presets.zh.md` |
| 预设配置表 | `deepseek-harness/bundle/base/cordis.patch.yml` |
| 客户端 UI 包 | `deepseek-harness/packages/client/ui-permission-presets` |
| 预设服务包 | `deepseek-harness/packages/interaction/permission-presets` |
| 与 Claude Code 的对比 | `ccs/docs/sandbox-vs-claude-code.md`（本文前一篇） |

---

## 附：与 Claude Code 的关系

Claude Code 是**审批 + 沙箱两层**。`--dangerously-skip-permissions` 跳过的是**审批**，沙盒仍然生效——它不是「关掉沙箱的开关」，而是「拿掉人审的开关」。

harness 比 Claude Code 更进一步的地方在于：**它把沙箱也收窄到「只管写」**，所以「读不到 `cacert.pem`」这类问题从设计上就不存在。代价是它对**读**的边界更松，需要审批层来兜。
