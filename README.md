# AI Key Manager

本地 AI API Key 管理代理服务。集中管理多个 AI 供应商的 API Key，自动根据优先级选择可用 Key，支持故障切换、请求代理转发及完整审计日志。

## 安装

```bash
pip install -e .
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

# 可选：清理构建缓存后重新打包
rm -rf build dist
python setup.py py2app
```

应用图标由 `logo.icns` 提供，通过 `setup.py` 中的 `iconfile` 选项配置。

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

# Key 管理
akm key add <别名> <供应商>     # 添加 Key
akm key list                   # 列出所有 Key
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

# 日志
akm log list                   # 查看最近日志
akm log clean --before YYYY-MM-DD # 清理旧日志
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

## Web 管理台

`akm serve` 启动后访问 `http://127.0.0.1:8800/admin`

| 页面 | 功能 |
|------|------|
| 统计 | Token 用量仪表盘（骨架屏加载、缓存命中独立展示、输入Token不含缓存、按 Key/模型/日期分组、K/M 格式、1天/7天/30天切换） |
| 审计 | 请求日志（输入/缓存/输出Token列、时间筛选、Key/状态过滤、成功/失败切换、正倒序、每页10条、loading动画、Markdown 对话回放） |
| Key管理 | 增删改查、启用/禁用、连通性测试、一键导出备份 |
| 设置 | 服务端口、日志保留天数、清空日志、自动打开管理台 |
| 关于 | 版本与功能简介 |

## 配置

配置文件位于 `~/.akm/config.json`，可通过 Web 设置页面修改：

```json
{
  "auto_open_admin": true,
  "log_retention_days": 30,
  "server_port": 8800
}
```

Key 和日志数据存储在 `~/.akm/akm.db`（SQLite）。

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI Chat Completions 接口 |
| POST | `/v1/responses` | OpenAI Responses API 接口（Codex 兼容） |
| GET | `/v1/models` | 模型列表 |
| GET | `/health` | 健康检查 |
| GET | `/api/keys` | Key 列表（脱敏） |
| POST | `/api/keys` | 添加 Key |
| PUT | `/api/keys/{alias}` | 编辑 Key |
| PATCH | `/api/keys/{alias}/status` | 启用/禁用 Key |
| DELETE | `/api/keys/{alias}` | 删除 Key |
| POST | `/api/keys/{alias}/test` | 测试连通性 |
| GET | `/api/keys/export` | 导出 Key 配置（含完整密钥） |
| GET | `/api/logs` | 审计日志（支持 status/days/key_alias 筛选） |
| POST | `/api/logs/clean` | 清空日志 |
| GET | `/api/stats` | Token 统计（支持 days 时间范围） |
| GET/POST | `/api/config` | 配置读写 |

## 故障切换策略

| 状态码 | 行为 |
|--------|------|
| 429 | 标记限流，60 秒冷却后恢复 |
| 401/403 | 禁用 Key |
| 402 | 禁用 Key（余额不足） |
| 5xx | 指数退避重试（同 Key 最多 3 次），失败后切换 Key |
| 连接/超时 | 指数退避重试后切换 Key |

Key 选择分两阶段：优先精确匹配当前 model 的 Key（按优先级依次尝试，已失败 Key 自动排除），精确匹配全部不可用时回退到 `models='*'` 通配符 Key。

> 应用重启后，数据库中残留的 `rate_limited` 状态会自动恢复为 `active`。
> Key 的 models 字段存储时自动规范化（去除逗号前后空格），防止匹配失败。

## 流式转发

内部所有请求统一向上游发 `stream=true`，边收边拼以减少首 token 延迟：

- 客户端 `stream=true` → 逐块透传 SSE
- 客户端 `stream=false` → 收集全部 chunk 后转为标准 JSON 返回
- 流式结束后异步写入审计日志（完整响应体用于统计和对话回放）

## 认证头配置

Key 编辑表单支持自定义认证头模板，`{api_key}` 占位符会被替换为实际 Key：

| 场景 | auth_header |
|------|------------|
| OpenAI / DeepSeek 官方 | `Bearer {api_key}` |
| 第三方中转（如 dmxapi.cn） | `{api_key}` |
| 其他自定义 | `Api-Key {api_key}` 等 |

## 技术栈

- Python 3.12+ / FastAPI / uvicorn
- httpx 共享连接池（lifespan 管理，TCP keep-alive 复用）
- SQLite（审计日志持久化，WAL 模式）
- Fernet 加密（Key 存储）
- rumps（macOS 菜单栏）
- Tailwind CSS / marked.js（Web UI）
