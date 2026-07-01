# Markdown KB Auto Sync Skill - API 请求规范

本文档为Skill API 请求相关的规范，用于指导调用 `markdown_kb` 插件的 HTTP 接口。

---

## 1.状态接口

用于获取 `markdown_kb` 插件的当前状态信息，包括文档目录、索引目录和同步状态。

### 请求地址

```
GET http://127.0.0.1:8800/api/markdown-kb/status
```

### 使用目的

- 获取当前 `docs_dir`（原始文档目录）
- 获取当前 `index_store_dir`（索引数据库目录）
- 判断最近更新时间、最近重建时间和同步状态

### 调用时机

- 执行任何操作前应先调用此接口确认当前 `docs_dir`
- 在 sync/rebuild 后调用此接口确认状态已更新
- 排查“检索不到新文档”问题时优先调用

---

## 2.上传接口

上传接口作为辅助入口，用于将 Markdown 文件通过 HTTP 上传到 `docs_dir`。

### 请求地址

```
POST http://127.0.0.1:8800/api/markdown-kb/files/upload
```

### 请求类型

```
multipart/form-data
```

### 表单字段

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `files` | file | 一个或多个 `.md` 文件 |

### 使用说明

- 文件会被保存到 `docs_dir`
- **不会自动重建索引**，需要额外执行 `sync` 或 `rebuild`
- 如果已经直接维护了 `docs_dir`，通常不需要调用此接口

---

## 3.绑定工作目录接口

用于给单个 Markdown 文档绑定工作目录，绑定关系持久化到插件侧的文件映射中。

### 请求地址

```
POST http://127.0.0.1:8800/api/markdown-kb/files/bind-workspace
```

### 推荐请求体

```json
{
  "file_name": "AI Key Manager.md",
  "workspace_root": "/Users/nk/Desktop/ccs"
}
```

### 使用说明

- 用于给单个 Markdown 文档绑定工作目录
- 绑定会持久化到插件侧的文件映射中
- 绑定后需要再执行 `rebuild-file`、`sync` 或 `rebuild` 才会进入索引
- 当请求里没有工作目录时，检索只会命中未绑定工作目录的公共文档

---

## 4.增量同步接口

用于增量同步 `docs_dir` 的变更到索引数据库。

### 请求地址

```
POST http://127.0.0.1:8800/api/markdown-kb/sync
```

### 推荐请求体

```json
{
  "apply": true
}
```

### 使用说明

| apply 值 | 行为 |
|----------|------|
| `false` 或省略 | 预览新增、变化、删除，不实际执行 |
| `true` | 真正执行增量同步 |

### 适用场景

- 日常小范围变更（少量新增、修改或删除文档）
- 需要预览变更影响时使用 `apply=false`

---

## 5.全量重建接口

用于全量重建索引数据库，比 `sync` 更重但更彻底。

### 请求地址

```
POST http://127.0.0.1:8800/api/markdown-kb/rebuild
```

### 请求体

无需请求体。

### 适用场景

- 一次性替换大量文档
- 调整了切片配置
- 怀疑索引状态已经漂移
- 批量替换文档目录后使用

---

## 6.目录规则

### 真实目录获取优先级

1. 优先调用 `GET /api/markdown-kb/status`，读取运行时返回的 `docs_dir`
2. 如果状态接口不可用，回退到当前默认目录 `~/.akm/markdown_kb/docs`

### 重要提醒

不要把 `~/.akm/markdown-kb/docs` 当成当前既定实现——仓库中的真实目录名是下划线 `_`。

---

## 7.数据分层说明

### 原始文档层

| 属性 | 说明 |
|------|------|
| 目录 | `docs_dir` |
| 内容 | Markdown 原文文件 |
| 作用 | 作为切片和建索引的唯一来源 |

### 索引数据库层

| 属性 | 说明 |
|------|------|
| 目录 | 通常位于 `~/.akm/markdown_kb/index_store/` |
| 内容 | 切片结果、向量、索引元数据 |
| 作用 | 供 query / ask 实际检索使用 |

### 关键注意事项

- 上传接口只会把文件写入原始文档层
- 直接维护 `docs_dir` 不会天然丢失原文
- 目录变了但没执行 `sync` 或 `rebuild`，索引数据库就会过期

---

## 8.执行顺序建议

### 日常维护

1. `GET /api/markdown-kb/status` 确认 `docs_dir`
2. 直接把 `.md` 文件写入/更新/删除到 `docs_dir`
3. `POST /api/markdown-kb/sync` 传 `{"apply": true}`
4. 再次 `GET /api/markdown-kb/status` 确认索引已更新

### 批量替换

1. 把 `docs_dir` 调整到最终目标状态
2. `POST /api/markdown-kb/rebuild`
3. 检查状态、健康和检索结果