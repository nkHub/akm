# 主密钥保管（`secret.key`）现状评估与加固方案

> 状态：**方案评估，未实施**。本文只做现状评估与候选方案设计，动手前先定「第 9 节 待决策项」。
> 相关实现：[akm/crypto.py](../../akm/crypto.py)（加密层）、[akm/key_pool.py](../../akm/key_pool.py)（调用方）、[akm/cleanup.py](../../akm/cleanup.py)（数据目录维护）。

## 1. 背景

`akm.db` 里的 `api_key` 是密文，解密凭据是主密钥文件 `~/.akm/secret.key`。这个文件一旦被拿走，库里所有 Key（历史与未来）都能解开。因此它属于"数据目录里唯一需要单独对待的文件"，但目前的实现把它当作普通数据文件在管理。

问题不是密码学实现（见 2.3，实现是对的），而是**密钥保管**：文件权限、存放位置、副本治理、轮换能力。

## 2. 现状

### 2.1 代码路径

| 环节 | 位置 | 行为 |
|---|---|---|
| 目录与路径 | `crypto.py:_get_secret_path()` | `os.makedirs(SECRET_DIR, exist_ok=True)`，默认权限受 umask 决定 |
| 首次生成 | `crypto.py:_load_cipher()` | `open(key_path, "wb")` + `write(key)`，**无 `chmod`、无 `O_EXCL`** |
| 既有读取 | `crypto.py:_load_cipher()` | `open(key_path, "rb")`，不做权限检查与自愈 |
| 进程缓存 | `crypto.py:_cipher` | 进程级缓存，命中后不再读盘 |
| 调用方 | `key_pool.py` | 仅 `_encrypt` / `_decrypt`，不关心密钥来源 |
| 维护排除 | `cleanup.py` | `secret.key` 在"永不删除"名单内（正确，但这也说明它被当普通数据文件管理） |
| 测试隔离 | `tests/test_key_pool_crypto.py`、`tests/conftest.py` | 靠 monkeypatch `crypto.SECRET_DIR`，**密钥路径没有环境变量覆盖**（`AKM_DB_DIR` 只覆盖数据库） |

### 2.2 磁盘实测

```
$ stat -f "%Sp %z %N" ~/.akm ~/.akm/secret.key ~/.akm/secret.key.zip
drwxr-xr-x        -    ~/.akm
-rw-r--r--       46    ~/.akm/secret.key
-rw-r--r--      246    ~/.akm/secret.key.zip
```

- 密钥文件 **0644**、目录 **0755**。家目录 `/Users/nk` 是 `drwxr-x---`，group 为 `staff` —— macOS 本地账号默认都在 `staff` 组，因此**同机其他本地账号可以遍历并读取该文件**。
- 同一目录下存在 `secret.key.zip`，内含同一把 46 字节密钥（用户手工备份）。这不是程序产物，但它与密钥同放、同样无保护。
- 对照：同一仓库的 [agent_runtime/sessions.py:74](../../akm/agent_runtime/sessions.py#L74)、[agent_runtime/tools.py:387](../../akm/agent_runtime/tools.py#L387)、[agent_runtime/router.py:121](../../akm/agent_runtime/router.py#L121) 都已经显式使用 `mode=0o700`。**`secret.key` 是唯一没做权限收紧的敏感文件**，更像漏改而非有意设计。

### 2.3 密码学实现（无问题，不在本方案改动范围）

`_MiniFernet` 与 `cryptography.fernet` 完全互通，且有参考向量测试（`tests/test_key_pool_crypto.py:29`）：

- AES-128-CBC + PKCS7，Encrypt-then-MAC（`HMAC-SHA256` 覆盖 `version || ts || iv || ct`）；
- 解密**先验 MAC 再解密密文**，`hmac.compare_digest` 常量时间比较，PKCS7 填充校验严格；
- 密钥为 32 字节随机数（`os.urandom`），不涉及口令派生，因此**没有弱 KDF 问题**。

**结论：不要在本次加固中改加密实现或令牌格式。**

## 3. 威胁模型

### 3.1 本方案要防的（现实且高频）

| 场景 | 现状后果 |
|---|---|
| Time Machine / 本地快照 / 网盘同步 `~/.akm` | 主密钥随备份离开本机 |
| 把 `~/.akm` 整体打包发给别人排障、或从备份恢复 | 同上 |
| 同机其他本地账号（`staff` 组） | 可读 0644 文件（家目录 0750 拦不住同组） |
| 误把数据目录内容提交进仓库 / 分享 | 密钥明文外流 |
| 其他程序"扫描用户目录"（同步盘、清理工具、IDE 索引） | 明文可读 |

### 3.2 本方案**防不住**的（不要对外宣传成"安全"）

- **同用户下运行的任意代码**：插件是同进程 importlib 执行的任意 Python（[plugins/plugin_manager.py:273](../../akm/plugins/plugin_manager.py#L273)），本机恶意程序同样以用户身份运行 —— 无论密钥放文件、Keychain 还是内存，都能拿到（进程内 `_cipher` 也可直接用）。
- **本机 HTTP 接口**：`GET /api/keys/export` 直接返回明文 `api_key`（[server.py:1382](../../akm/server.py#L1382)），服务绑定 `127.0.0.1` 且无鉴权。
- **自动更新链路**：更新流程下载 GitHub Release 的 zip 后直接解压覆盖 `.app`（[menubar.py:544 起](../../akm/menubar.py#L544)），只有 TLS + 资产名匹配，**没有签名/哈希校验**。能替换 `.app` 的能力等价于能读密钥。

所以本方案的实际收益是**降低离线泄露与误拷贝风险**，不是"防御本机攻击"。要后者得先做插件隔离与更新包签名校验（见附录 B）。

## 4. 问题清单（按严重度）

| # | 问题 | 严重度 | 修复成本 |
|---|---|---|---|
| 1 | 密钥文件 0644、目录 0755，且创建时不做权限设置 | 高（可立即修） | 极小 |
| 2 | 密钥与可写数据目录混放，任何"整目录"操作都会带走它 | 中高 | 中 |
| 3 | 无轮换：单密钥，泄露一次 = 历史与未来全解 | 中 | 中 |
| 4 | 密钥路径不可用环境变量覆盖（只有 `AKM_DB_DIR`），测试只能 monkeypatch | 低（可维护性） | 小 |
| 5 | 无"密钥丢失即不可恢复"的用户可见提示 | 低（运维） | 极小 |
| 6 | 测试名与实际行为不符（`test_load_cipher_prefers_keychain` 等仍叫 Keychain） | 低（可读性） | 极小 |

## 5. 候选方案（分档，可单独落地）

### P0 权限收紧与副本治理（建议立即做）

1. **创建即安全**：用 `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)` 一步完成，避免 `open()` 后再 `chmod` 的竞态窗口；目录用 `os.makedirs(SECRET_DIR, mode=0o700, exist_ok=True)`，对已存在的目录补一次 `os.chmod`。
2. **读取时自愈**：`stat` 检查 `mode & 0o077`，非 0 就 `chmod(0o600)` 并 `logger.warning` 记一次（不重复刷日志）。`chmod` 失败（FAT/exFAT、网络盘、只读挂载）时只告警、**不阻断启动**，并在启动日志/健康检查里留下可见痕迹。
3. **副本治理**：AKM 自身的导出、排障、日志、打包流程**永不读取、不打包密钥文件**；`cleanup.py` 的"永不删除"名单保持为唯一真相源。
   - **明确不删除用户自建副本**：数据目录里用户手工备份的 `secret.key.zip` 属于用户数据，自动维护与迁移流程一律不碰（这也符合用户"不要擅自清理我的备份"的要求）。README 里提示"手工备份主密钥请自行加密存放"即可。
4. **测试**：新建文件 0600、目录 0700、已存在的 0644 被自愈为 0600、`chmod` 失败时不抛异常、进程缓存不重复读盘（已有）。

P0 是**纯收益、无 UX 影响、无迁移风险**的一档，解决第 4 节问题 1 与部分问题 2。

### P1 密钥与数据目录解耦（建议紧随 P0）

把密钥移出 `~/.akm`，让数据目录可以放心整体备份/导出/重置：

- **默认新位置**：`~/Library/Application Support/AKM/secret.key`（目录 0700）。macOS 惯例位置，且不会被 `~/.akm` 的清理逻辑误伤。
- **解析顺序（读）**：`AKM_SECRET_KEY`（内联密钥，CI/测试用）→ `AKM_SECRET_FILE`（显式路径）→ macOS Keychain（若 P2 启用）→ 新默认路径 → **遗留路径 `~/.akm/secret.key`**。
- **写入**：只写当前最高优先级的可用位置。
- **迁移（保守）**：从遗留路径成功读取后，复制到新路径（0600），**保留原文件不删**，日志记录一次迁移事件；新路径写失败则继续用遗留路径，功能不受影响。
- **改动面**：`crypto.SECRET_DIR` 由模块常量改为解析函数（`_get_secret_path()` 已经是函数，改动面小）；[tests/conftest.py](../../tests/conftest.py) 的隔离方式改为设置 `AKM_SECRET_FILE`（比 monkeypatch 更贴近真实路径解析）；同步 `cleanup.py` 的注释与"不触碰"名单。

### P2 macOS Keychain（需先技术验证，不建议盲上）

目标：静态不再存在明文密钥文件。**两条实现路线语义不同，必须先验证再选**：

| 路线 | 条目 ACL 归属 | 弹框行为 | 实际防护 |
|---|---|---|---|
| A. `keyring`（进程内调用 Security.framework） | app 自身身份 | py2app 打包若签名不稳定，**每次自更新/重建后首次读取会弹授权框**（很可能就是 0.1.37 移除该方案的原因） | 对"文件被备份/拷贝"有效，对同机进程仍需授权 |
| B. 子进程 `/usr/bin/security`（`add-generic-password` / `find-generic-password -w`） | Apple 签名的 `security` 工具 | 通常不弹框 | **最弱**：本机任何进程都能用同一条命令读出，约等于换了个柜子 |

倾向：**只有 A 值得做**，且要接受一次授权弹框与明确恢复路径；B 属于"看起来安全"，不建议单独采用。

无论 A/B 都要满足：

- Keychain 条目版本化（如 `service=ai-key-manager, account=master-key`；轮换时用 `master-key:v2`）。
- **恢复路径写进文档**：`security find-generic-password -s ai-key-manager -a master-key -w`；换机/重装时的导出流程。
- Keychain 不可用（SSH、无 GUI 会话、CI）时回退文件 + 记 warning，不阻断启动。
- **迁移不删原文件**：读出文件密钥写入 Keychain 后保留 `~/.akm/secret.key`，只有用户显式执行 `akm secret migrate --purge-file` 才清理。

### P3 主密钥轮换（把"一次泄露永久全泄"降级）

- `encrypt` 永远用最新密钥；`decrypt` 依次尝试 `[current, previous...]`（密钥数量少，HMAC 校验失败很快，无需在令牌里加 key id）。
- 落盘：`secret.key`（当前）+ `secret.key.previous.<ts>`（Keychain 场景用多 account）。
- 触发：CLI `akm secret rotate` —— 全表扫描 `api_key`，用旧密钥解密、新密钥加密；重加密期间新旧混存也能解，完成后才移除 previous。
- 效果：泄露范围从"全部历史与未来"收敛为"泄露时刻之前的数据"。

### P4 Secure Enclave / 口令模式（暂不建议）

- 思路：主密钥由 enclave 内不可导出的 P-256 私钥包裹（ECIES）或直接派生，可加 `kSecAccessControlUserPresence`（Touch ID）。
- 代价：需要 ctypes/原生组件；**enclave key 丢失或换机 = 全部数据不可恢复**，必须配套导出恢复包；每次解锁提示与菜单栏常驻体验冲突。
- 除非把"安全"当作产品卖点，否则不建议现在做。

## 6. 迁移与回滚原则（P0–P3 共用）

1. **只新增、不删除**：任何自动流程都不删除用户文件（包括 `secret.key.zip` 这类手工副本）。
2. **降级可用**：任一步失败都退回"文件 + 0600"，启动不受影响，只记 warning。
3. **可观测**：新增 `akm secret status`（输出当前来源：env / Keychain / 新路径 / 遗留路径、文件路径、权限位、是否已迁移），排障时不必猜。
4. **可回滚**：P1/P2 上线后，把解析顺序里的新来源去掉即回到遗留文件；密文格式不变，因此回滚不丢数据。

## 7. 测试与验证清单

- 权限：新建密钥文件 0600、目录 0700；已存在 0644 自愈为 0600；`chmod` 失败告警但不抛异常。
- 来源优先级：`AKM_SECRET_KEY` > `AKM_SECRET_FILE` > Keychain（mock）> 新路径 > 遗留路径。
- 迁移：遗留 → 新路径后**原文件仍存在**、内容一致、`_encrypt/_decrypt` 往返一致；新路径不可写时仍走遗留路径。
- 回退：Keychain 读/写失败 → 文件路径可用。
- 边界：`cleanup` 与导出/日志流程不读取、不打包、不删除密钥文件（在现有"永不删除"名单上加断言）。
- 顺手修正误导性测试名：`test_load_cipher_prefers_keychain` / `test_load_cipher_migrates_fallback_file_to_keychain` / `test_load_cipher_uses_memory_key_when_keychain_unavailable`（当前实际测的是本地文件行为）。

## 8. 明确不做

- **不动用户手工备份的 `~/.akm/secret.key.zip`**，也不自动删除任何旧密钥文件。
- 不改 Fernet 令牌格式与加解密实现（已互通、有参考向量）。
- 不引入强制口令 / 启动即解锁（P4 之后再议）。
- 不在本方案内改插件隔离、更新包签名、`/api/keys/export` 鉴权（见附录 B，属于独立议题）。

## 9. 待决策项

1. P1 的新默认路径：`~/Library/Application Support/AKM/`，还是保持 `~/.akm` 只做权限收紧（跨平台与迁移成本权衡）？
2. P2 是否做？若做，选 A（Keychain 真 ACL + 可能的授权弹框）还是暂缓？需要一次**打包环境实测**：py2app 应用写入的条目在自更新/重新签名后是否仍免弹框。
3. P3 是否需要 CLI 轮换入口，还是仅预留"多密钥读取"能力？
4. 是否在 README 与设置页补"主密钥丢失 = 已存 Key 不可恢复，可用 `GET /api/keys/export` 做明文备份"的提示？

## 附录 A：实测证据

```bash
$ stat -f "%Sp %z %N" ~/.akm ~/.akm/secret.key ~/.akm/secret.key.zip
drwxr-xr-x        -    /Users/nk/.akm
-rw-r--r--       46    /Users/nk/.akm/secret.key
-rw-r--r--      246    /Users/nk/.akm/secret.key.zip

$ stat -f "%Sp %Su %Sg" ~ /Users
drwxr-x--- nk staff /Users/nk
drwxr-xr-x root admin /Users
```

代码位置：[crypto.py:26](../../akm/crypto.py#L26)（`SECRET_DIR`）、[crypto.py:32](../../akm/crypto.py#L32)（`_get_secret_path`）、[crypto.py:131-139](../../akm/crypto.py#L131-L139)（读/写）、[crypto.py:119-142](../../akm/crypto.py#L119-L142)（`_load_cipher`）、[cleanup.py:109](../../akm/cleanup.py#L109)（副本保留规则）。

## 附录 B：关联风险（超出本方案范围，但影响整体安全水位）

| 风险 | 位置 | 说明 |
|---|---|---|
| 明文导出接口无鉴权 | [server.py:1382](../../akm/server.py#L1382) | `GET /api/keys/export` 返回完整 `api_key`，localhost 任意进程可调 |
| 插件同进程任意代码 | [plugin_manager.py:273](../../akm/plugins/plugin_manager.py#L273) | 可读密钥文件、也可直接用进程内 `_cipher` |
| 更新包无签名校验 | [menubar.py:544 起](../../akm/menubar.py#L544) | 仅 TLS + 资产名匹配，能替换 `.app` |
| 密钥丢失不可恢复 | `crypto.py` | 无恢复设计；明文导出是唯一救生通道，值得写进 README |

排序建议：若目标是"真实防护等级"，**插件隔离与更新包签名校验的优先级高于主密钥存放位置**；P0 因为成本极低、收益明确，可以先做。
