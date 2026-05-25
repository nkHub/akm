# AI Key Manager — 设计文档

## 概述

一个本地 AI Key 管理代理服务，支持多供应商 key 配置、优先级调度、故障自动切换、请求代理转发及完整审计日志。

- **使用形态：** HTTP 服务 + CLI 管理
- **使用场景：** 个人本地开发
- **监听地址：** `127.0.0.1:8800`，无认证，不暴露局域网

---

## 功能清单

| 功能 | 说明 |
|------|------|
| 多 key 管理 | 支持 openai / deepseek / codex 三个供应商 |
| 优先级调度 | key 按 priority 数值排序，越小越优先 |
| 自动切换 | 429 冷却 60s、402 禁用、5xx 重试后切换 |
| 代理转发 | 完全兼容 OpenAI `/v1/chat/completions` 接口 |
| 审计日志 | 记录完整请求/响应体 |
| CLI 管理 | `akm` 命令管理 key 和日志 |

---

## 架构

```
┌──────────────┐     ┌─────────────────────────────────┐
│  你的业务代码  │────▶│  ai-key-manager (FastAPI)       │
│  (HTTP 调用)  │     │  localhost:8800                 │
└──────────────┘     │                                 │
                     │  ┌──────────┐  ┌─────────────┐  │
                     │  │ 路由引擎  │  │ 审计日志模块  │  │
                     │  └────┬─────┘  └─────────────┘  │
                     │       │                          │
                     │  ┌────▼─────┐                    │
                     │  │ Key 池   │  ◀── CLI 管理      │
                     │  │ 优先级队列│      (key增删改查) │
                     │  └────┬─────┘                    │
                     └───────┼─────────────────────────┘
                             │
                    ┌────────┼────────┐
                    ▼        ▼        ▼
                 OpenAI  DeepSeek  Codex
```

### 组件

| 组件 | 文件 | 职责 |
|------|------|------|
| CLI 入口 | `akm/cli.py` | `akm` 命令注册，serve / key / log 子命令 |
| 服务 | `akm/server.py` | FastAPI 应用，`/v1/chat/completions` 路由 |
| Key 池 | `akm/key_pool.py` | key 增删改查、优先级排序、状态标记 |
| 代理 | `akm/proxy.py` | 转发请求到上游、重试策略、切换逻辑 |
| 审计 | `akm/audit.py` | 日志写入、查询、清理 |
| 数据库 | `akm/db.py` | SQLite 连接管理、建表 |
| 模型 | `akm/models.py` | Pydantic 数据模型 |

### 代理端口映射

| 供应商 | 默认 base_url |
|--------|--------------|
| openai | `https://api.openai.com` |
| deepseek | `https://api.deepseek.com` |
| codex | `https://api.openai.com` |

用户可自定义 base_url，如代理转发到自定义端点或兼容服务。

---

## 数据库设计

### keys 表

| 字段 | 类型 | 说明 |
|------|------|------|
| alias | TEXT PRIMARY KEY | 别名，如 `my-openai-1` |
| provider | TEXT NOT NULL | `openai` / `deepseek` / `codex` |
| api_key | TEXT NOT NULL | 加密存储 |
| base_url | TEXT | 自定义 API 地址，NULL 则用默认 |
| models | TEXT DEFAULT '*' | 支持的模型列表，逗号分隔，`*` 表示全部 |
| priority | INTEGER DEFAULT 0 | 越小越优先 |
| status | TEXT DEFAULT 'active' | `active` / `disabled` / `rate_limited` |
| created_at | TEXT | ISO 格式时间戳 |

### audit_logs 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 自增主键 |
| timestamp | TEXT NOT NULL | ISO 格式请求时间 |
| provider | TEXT | 供应商 |
| key_alias | TEXT | 使用的 key 别名 |
| model | TEXT | 请求模型名 |
| request_body | TEXT | 完整请求体 JSON |
| response_body | TEXT | 完整响应体 JSON |
| status_code | INTEGER | 上游响应码 |
| latency_ms | INTEGER | 响应延迟(毫秒) |
| error | TEXT | 错误信息 |

---

## 自动切换策略

请求到达时：
1. 解析请求体中的 `model` 参数（如 `gpt-4`、`deepseek-chat`）
2. 筛选所有 `status='active'` 且 `models` 字段包含该 model（`*` 通配）的 key
3. 按 `priority ASC` 排序，取第一个
3. 用该 key 转发请求到上游

响应处理：
- **200 OK** → 正常返回
- **429 Too Many Requests** → 标记当前 key 为 `rate_limited`，60s 后自动恢复为 `active`，换下一个 key 重试
- **402 Payment Required** → 标记当前 key 为 `disabled`，换下一个 key 重试
- **5xx** → 最多重试 2 次，仍失败则换下一个 key
- **所有 key 不可用** → 返回 503 Service Unavailable

---

## CLI 命令

```
akm key list                              # 列出所有 key
akm key add <alias> <provider> [--models gpt-4,gpt-3.5]  # 添加 key（交互提示输入 api_key）
akm key remove <alias>                    # 删除 key
akm key set-priority <alias> <n>          # 调整优先级
akm key disable <alias>                   # 禁用 key
akm key enable <alias>                    # 启用 key

akm serve [--port 8800]                   # 启动代理服务

akm log list [--provider X] [--limit N]   # 查看日志
akm log clean --before 2025-01-01         # 清理旧日志
```

---

## 项目结构

```
ai-key-manager/
├── pyproject.toml
├── akm/
│   ├── __init__.py
│   ├── cli.py
│   ├── server.py
│   ├── key_pool.py
│   ├── proxy.py
│   ├── audit.py
│   ├── db.py
│   └── models.py
```

---

## 技术栈

| 组件 | 技术 |
|------|------|
| HTTP 框架 | FastAPI + uvicorn |
| HTTP 客户端 | httpx (async) |
| CLI 框架 | click |
| 数据库 | SQLite (stdlib `sqlite3`) |
| 加密 | cryptography (Fernet) |
| 数据校验 | Pydantic |

---

## 非功能需求

- **安全性：** api_key 使用 Fernet 对称加密存储在 SQLite 中，加密密钥存放在 `~/.akm/secret.key`
- **监听范围：** 仅绑定 `127.0.0.1`，不暴露局域网
- **日志清理：** 不自动清理，由用户通过 CLI 手动执行
- **并发：** 单用户本地使用，不特别处理并发冲突
- **平台：** macOS / Linux，Python 3.10+
