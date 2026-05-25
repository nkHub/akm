# AI Key Manager

本地 AI API Key 管理代理服务。集中管理多个 AI 供应商的 API Key，自动根据优先级选择可用 Key，支持故障切换、请求代理转发及完整审计日志。

## 安装

```bash
pip install -e .
```

## 打包为 macOS 应用

```bash
# 开发模式运行（无需打包，直接测试）
python -m akm.menubar

# 安装打包工具
pip install py2app pillow

# 打包（生成 dist/AI Key Manager.app）
python setup.py py2app

# 可选：清理构建缓存后重新打包
rm -rf build dist
python setup.py py2app
```

打包后的 `.app` 位于 `dist/` 目录，双击即可运行。打包配置见 `setup.py`。

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
akm-menubar                    # 启动菜单栏应用
```

状态栏显示图标，下拉菜单可查看运行状态（🟢🟡🔴）、打开管理台。

## Web 管理台

`akm serve` 启动后访问 `http://127.0.0.1:8800/admin`

| 页面 | 功能 |
|------|------|
| 统计 | Token 用量仪表盘（按供应商/模型/日期） |
| 审计 | 请求日志（分页、抽屉详情、自动刷新） |
| Key管理 | 增删改查、启用/禁用、连通性测试 |
| 设置 | 日志保留天数、清空日志、自动打开管理台 |
| 关于 | 版本与功能简介 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI 兼容接口 |
| GET | `/v1/models` | 模型列表 |
| GET | `/health` | 健康检查 |
| GET | `/api/keys` | Key 列表（脱敏） |
| POST | `/api/keys` | 添加 Key |
| DELETE | `/api/keys/{alias}` | 删除 Key |
| POST | `/api/keys/{alias}/test` | 测试连通性 |
| GET | `/api/logs` | 审计日志（分页） |
| POST | `/api/logs/clean` | 清空日志 |
| GET | `/api/stats` | Token 统计 |
| GET/POST | `/api/config` | 配置读写 |

## 故障切换策略

| 状态码 | 行为 |
|--------|------|
| 429 | 标记限流，60 秒冷却后恢复 |
| 401/403 | 禁用 Key |
| 402 | 禁用 Key（余额不足） |
| 5xx | 同 Key 重试 2 次后切换 |

## 技术栈

- Python 3.10+ / FastAPI / uvicorn
- SQLite（审计日志持久化）
- Fernet 加密（Key 存储）
- rumps（macOS 菜单栏）
- Tailwind CSS（Web UI）
