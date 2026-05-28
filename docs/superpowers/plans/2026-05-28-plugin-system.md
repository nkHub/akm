# 插件系统实现计划

> 状态：规划中 | 基于设计文档 [plugin-system.md](../design/plugin-system.md)

**Goal:** 实现 akm 插件系统，核心原则：项目主体只提供纯粹的请求转发 + 审计日志功能，协议格式转换作为可选插件提供。

## 架构原则

```
┌────────────────────────────────────────┐
│  akm 核心 (Core)                       │
│  ┌──────────┐ ┌──────────┐            │
│  │ 请求转发  │ │ 审计日志  │  ← 永久存在 │
│  │ (proxy)  │ │ (audit)  │   不可移除   │
│  └──────────┘ └──────────┘            │
│                                        │
│  ┌──────────────────────────────────┐  │
│  │ 插件系统 (Plugin System)         │  │
│  │  ┌────────────┐ ┌────────────┐  │  │
│  │  │ 协议转换    │ │ 自定义功能  │  │  │
│  │  │ (可选)     │ │ (可选)     │  │  │
│  │  └────────────┘ └────────────┘  │  │
│  └──────────────────────────────────┘  │
└────────────────────────────────────────┘
```

- **核心**：纯粹的请求转发 + 审计日志，不承担任何协议转换职责
- **插件**：协议格式转换（Responses/Chat/Messages 互转）作为可选功能，用户通过插件管理界面开启/关闭
- **加载**：所有插件（内置 + 第三方）统一从 `plugins/` 目录加载，内置插件默认随项目分发但可禁用

## 可行性评估

| 维度 | 评估 |
|------|------|
| 系统耦合 | 转换逻辑仅做纯数据转换，不依赖 app/db/config/FastAPI Request/Response |
| 迁移方案 | 将 `akm/adapters/*.py` 的逻辑直接内联到对应插件 `index.py`，删除空壳 adapter 类 |
| 核心独立 | proxy 不硬编码适配器引用，找不到转换插件时返回明确错误而非崩溃 |

**需重构的关键点**：`agent.py` 中硬编码的 `responses_adapter`/`messages_adapter` 属性 → 删除，`proxy.py` 改为通过 `PluginManager.get_converter()` 查询。

---

## 文件结构

```
akm/
├── plugins/                      # 内置插件（随项目分发）
│   ├── __init__.py
│   ├── base.py                   # PluginBase 基类
│   ├── plugin_manager.py         # PluginManager
│   ├── responses_converter/      # 协议转换插件
│   │   ├── plugin.json
│   │   └── index.py              # 内联原 ResponsesAdapter 全部转换逻辑
│   ├── messages_converter/
│   │   ├── plugin.json
│   │   └── index.py              # 内联原 MessagesAdapter 全部转换逻辑
│   └── chat_converter/
│       ├── plugin.json
│       └── index.py              # 内联原 ChatAdapter 透传逻辑

~/.akm/
├── config.json
├── akm.db
└── plugins/                      # 第三方插件（用户自行安装）
    └── model_mapper/
        ├── plugin.json
        ├── index.py
        └── views/
            └── index.html

├── server.py                     # 集成 PluginManager
└── agent.py                      # 移除硬编码适配器属性
```

---

## Task 1: PluginBase 基类 + PluginManager 核心

- [ ] **Step 1.1: 创建 `akm/plugins/base.py`**
  - 属性注入：`name`、`app`、`router`、`meta`、`logger`、`_static_dir`
  - 生命周期：`on_load()`、`on_unload()`（默认空实现）
  - hook：`on_request(request)`、`on_response(request, response)`
  - 辅助：`self.config`（自动读 config.json）、`self.db`（共享连接）、`self.enabled`

- [ ] **Step 1.2: 创建 `akm/plugins/plugin_manager.py`**
  - `load_all(app, db)`：扫描 `akm/plugins/`（内置）+ `~/.akm/plugins/`（第三方）
  - `get_menu()`：仅返回已加载且 enabled 的有菜单插件
  - `get_plugin_list()`：返回全部插件（含加载失败的），供管理界面使用
  - `get_converter(from_format, to_format) -> Plugin | None`：查询启用的转换插件
  - `enable_plugin(name)` / `disable_plugin(name)`：状态写入 config.json
  - `delete_plugin(name)`：仅第三方插件可删，物理删除 `~/.akm/plugins/{name}/`
  - `run_hook(hook, request, response)`：仅对 enabled 的插件执行

- [ ] **Step 1.3: PluginMeta 模型**
  ```python
  class PluginMeta(BaseModel):
      name: str; version: str; has_menu: bool = False
      description: str = ""
      builtin: bool = False       # 内置插件标记
      menu: dict = {}
      routes_prefix: str = ""
      hooks: dict = {"on_request": False, "on_response": False}
      settings: list[SettingDef] = []
      converts: dict = None       # { "from": "responses", "to": "chat" }
  ```

---

## Task 2: 三个协议转换内置插件

**目标**：将现有适配器的转换逻辑直接内联到插件 `index.py` 中，删除 `akm/adapters/` 目录。每个插件暴露 `convert_request()` 和 `convert_sse_stream()` 方法，PluginManager 通过 `get_converter()` 查找。

- [ ] **Step 2.1: responses_converter** — `converts: { "from": "responses", "to": "chat" }`
- [ ] **Step 2.2: messages_converter** — `converts: { "from": "messages", "to": "chat" }`
- [ ] **Step 2.3: chat_converter** — `converts: { "from": "chat", "to": "messages" }`
- [ ] **Step 2.4: 删除 `akm/adapters/` 目录**，迁移现有 31 个测试到对应插件目录

---

## Task 3: 核心去适配器化（agent.py / proxy.py）

- [ ] **Step 3.1: agent.py — 删除硬编码适配器**
  - 删除 `responses_adapter`、`messages_adapter`、`chat_adapter` 三个懒加载属性
  - 保留 `supports_*` 能力标记和 `needs_conversion()`

- [ ] **Step 3.2: proxy.py — 通过 PluginManager 获取转换器**
  ```python
  # 改造后
  target_format = agent.needs_conversion(api_path)
  if target_format:
      converter = plugin_manager.get_converter(api_path, target_format)
      if converter is None:
          raise UnsupportedConversion(
              f"供应商 {agent.name} 不支持 {api_path} 格式，"
              f"且未找到可用的 {api_path}→{target_format} 转换插件。"
              f"请在插件管理中启用对应的转换插件。"
          )
      body = converter.convert_request(body)
  ```

- [ ] **Step 3.3: server.py 传递 PluginManager**
  - `forward_request()` 新增 `plugin_manager` 参数
  - lifespan 中将 `app.state.plugin_manager` 传给 proxy 调用点

---

## Task 4: 插件管理界面

- [ ] **`GET /api/plugins`** — 插件列表（含加载状态、启用状态）
- [ ] **`POST /api/plugins/{name}/enable`** — 启用
- [ ] **`POST /api/plugins/{name}/disable`** — 禁用
- [ ] **管理台新增「插件管理」页面** — 表格展示，状态开关，内置标记

---

## Task 5: 集成与测试

- [ ] server.py lifespan 集成 PluginManager
- [ ] 适配现有 31 个适配器测试
- [ ] 新增：插件加载、协议转换查询、启用/禁用端到端测试
- [ ] 端到端：禁用 responses_converter → DeepSeek `/v1/responses` 返回明确错误提示

---

## 风险评估

| 风险 | 缓解 |
|------|------|
| 内置插件默认启用才能保证向后兼容 | 首次加载时自动启用所有内置转换插件 |
| 用户误关转换插件导致 DeepSeek 不可用 | proxy 返回的报错信息指明缺少哪个插件 |
| 插件启用/禁用状态丢失 | 状态写入 `~/.akm/config.json` 的 `plugin_states` 字段 |
