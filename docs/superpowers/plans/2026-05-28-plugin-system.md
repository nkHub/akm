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

## 可行性与评估

### 分类覆盖

现有插件系统通过 `category` 字段 + hook 组合覆盖所有代理链路环节：

| `category` | 名称 | Hook | 已有内置插件 | 可扩展场景 |
|------------|------|------|-------------|-----------|
| `filter` | 请求处理 | `on_request` | — | 请求加密、敏感词过滤、参数注入 |
| `matcher` | 模型匹配 | `on_key_selected` | model_matcher | 权重路由、A/B 测试分流、自定义别名 |
| `converter` | 格式转换 | `convert_*` | 3 个转换器 | JSON→YAML、自定义协议 |
| `handler` | 错误处理 | `on_upstream_error` | error_handler | 自定义降级、通知告警 |
| `post` | 响应处理 | `on_response` | — | 审计增强、Token 统计、缓存 |
| `app` | 应用插件 | router + views | — | 管理面板、数据仪表盘 |

**结论：完全适配。** 所有代理链路的扩展点都已预留 hook，只需在 plugin.json 中声明对应 `category` 和 `hooks`。

### 适配器迁移评估

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
│   ├── model_matcher/            # 默认模型匹配（required: true，不可禁用）
│   │   ├── plugin.json
│   │   └── index.py
│   └── error_handler/            # 错误处理 + 故障切换
│       ├── plugin.json
│       └── index.py

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
  - hook：`on_request(request)`、`on_key_selected(model, key, request)`、`on_upstream_error(request, response, key)`、`on_response(request, response)`
  - 辅助：`self.config`（自动读 config.json）、`self.db`（共享连接）、`self.enabled`

- [ ] **Step 1.2: 创建 `akm/plugins/plugin_manager.py`**
  - `load_all(app, db)`：扫描 `akm/plugins/`（内置）+ `~/.akm/plugins/`（第三方）
  - `get_menu()`：仅返回已加载且 enabled 的有菜单插件
  - `get_plugin_list()`：返回全部插件（含加载失败的），供管理界面使用
  - `get_converter(from_format, to_format) -> Plugin | None`：查询启用的转换插件
  - `install_plugin(file: UploadFile)`：解压 .zip 到 `~/.akm/plugins/`；重名时拒绝并提示冲突
  - `enable_plugin(name)` / `disable_plugin(name)`：required 插件不可禁用，状态写入 config.json
  - `delete_plugin(name)`：仅第三方插件可删，物理删除 `~/.akm/plugins/{name}/`
  - `run_hook(hook, **kwargs)`：按 priority 从小到大管道执行，前一个返回值传给下一个，崩溃隔离

> 插件名全局唯一。加载时内置 + 第三方目录扫描，重名跳过第三方；上传安装时检查重名。

- [ ] **Step 1.3: PluginMeta 模型**
  ```python
  class PluginMeta(BaseModel):
      name: str; version: str; has_menu: bool = False
      category: str = ""           # filter/matcher/converter/handler/post/app
      description: str = ""
      builtin: bool = False       # 内置插件标记
      menu: dict = {}
      routes_prefix: str = ""
      required: bool = False      # 不可禁用
      priority: int = 100         # 同 hook 的执行优先级，0-999，越小越先
      hooks: dict = {"on_request": False, "on_key_selected": False, "on_upstream_error": False, "on_response": False}
      settings: list[SettingDef] = []
      converts: dict = None       # { "from": "responses", "to": "chat" }
  ```

---

## Task 2: 内置插件实现

**目标**：将现有核心逻辑从 proxy.py / key_pool.py 抽离为内置插件，删除 `akm/adapters/` 目录。

### 2.1 协议转换插件

- [ ] **responses_converter** — `converts: { "from": "responses", "to": "chat" }`
- [ ] **messages_converter** — `converts: { "from": "messages", "to": "chat" }`
- [ ] **chat_converter** — `converts: { "from": "chat", "to": "messages" }`
- [ ] 删除 `akm/adapters/` 目录，迁移现有测试到对应插件

### 2.2 模型匹配插件（model_matcher）

**来源**：原 `key_pool.py` 的 `pick_key(model)` 逻辑

**职责**：
- `on_key_selected(model, key, request)` → 根据 models 字段匹配（`*` 通配 / 逗号分隔精确匹配），返回
- 可在 `on_request` 中改写 model 名（别名映射）

**plugin.json**：
```json
{
    "name": "model_matcher",
    "category": "matcher",
    "has_menu": false,
    "builtin": true,
    "required": true,
    "version": "1.0.0",
    "description": "默认模型匹配规则：根据 key 的 models 字段选择匹配的 API key",
    "hooks": { "on_key_selected": true },
    "settings": [
        {
            "key": "aliases",
            "label": "模型别名映射",
            "type": "text",
            "default": "",
            "description": "一行一个，格式：原名→别名（如 gpt-5→gpt-4o）"
        }
    ]
}
```

> `required: true` 保证至少一个模型匹配插件生效。用户可安装第三方模型匹配插件替换默认行为，但新插件必须也声明 `required: true` 才会替代默认的（即最多一个 required 模型匹配插件生效）。

### 2.3 错误处理插件（error_handler）

**来源**：原 `proxy.py` 的两层重试循环

**职责**：
- `on_upstream_error(request, response, key)` → 根据状态码决定：`"retry"` / `"switch"` / `None`（默认处理）

**plugin.json**：
```json
{
    "name": "error_handler",
    "category": "handler",
    "has_menu": false,
    "builtin": true,
    "version": "1.0.0",
    "description": "默认错误处理：429/402/401/403 切换 key，5xx 指数退避重试",
    "hooks": { "on_upstream_error": true },
    "settings": [
        {
            "key": "max_retries_per_key",
            "label": "单 key 最大重试",
            "type": "number",
            "default": 2,
            "min": 0,
            "max": 10
        },
        {
            "key": "max_key_tries",
            "label": "最大尝试 key 数",
            "type": "number",
            "default": 20,
            "min": 1,
            "max": 50
        }
    ]
}
```

---

## Task 3: 核心重构（agent.py / proxy.py / key_pool.py）

**目标**：核心只做纯粹的转发 + 日志，协议转换/模型匹配/错误处理均委托给插件。

- [ ] **Step 3.1: agent.py** — 删除 `responses_adapter`、`messages_adapter`、`chat_adapter` 三个懒加载属性，保留 `supports_*` 能力标记

- [ ] **Step 3.2: proxy.py** — 重构转发流程，新增插件 hook 调用点
  ```python
  # 转换插件查询
  target_format = agent.needs_conversion(api_path)
  if target_format:
      converter = plugin_manager.get_converter(api_path, target_format)
      if converter is None:
          return error("转换插件未启用", 400)

  # key 选择后触发 hook
  key = await pick_key_async(model)
  await plugin_manager.run_hook("on_key_selected", model, key, request)

  # 上游错误时触发 hook
  action = await plugin_manager.run_hook("on_upstream_error", request, response, key)
  # action: "retry" → 重试 / "switch" → 切换下一 key / None → 默认降级
  ```

- [ ] **Step 3.3: key_pool.py** — 删减为基础 CRUD（add/list/disable/delete），模型匹配逻辑移至 model_matcher 插件

- [ ] **Step 3.4: server.py** — `forward_request()` 新增 `plugin_manager` 参数

---

## Task 4: 插件管理界面

- [ ] **`GET /api/plugins`** — 插件列表（含加载状态、启用状态、内置/第三方标记）
- [ ] **`POST /api/plugins/upload`** — 上传 `.zip` 包，服务端解压到 `~/.akm/plugins/`
- [ ] **`POST /api/plugins/{name}/enable`** — 启用（需重启生效）
- [ ] **`POST /api/plugins/{name}/disable`** — 禁用（required 插件拒绝，需重启生效）
- [ ] **`DELETE /api/plugins/{name}`** — 删除（仅第三方，物理删除目录）
- [ ] **管理台新增「插件管理」页面**：
  - 表格展示所有插件（名称/版本/分类/来源/状态）
  - 状态开关（required 插件灰化不可操作）
  - 上传 .zip 按钮 → 调用 `/api/plugins/upload`
  - 第三方插件删除按钮
  - 顶部提示：「插件状态变更后需手动重启服务生效」（不支持热重载）

---

## Task 5: 集成与测试

- [ ] server.py lifespan 集成 PluginManager
- [ ] 适配现有 31 个适配器测试（迁移到对应插件目录）
- [ ] 新增：插件加载、协议转换查询、模型匹配、错误处理端到端测试
- [ ] 端到端：禁用 error_handler → 上游 5xx 不再重试，直接返回错误
- [ ] 端到端：model_matcher (required) 不可被 disable 接口禁用

---

## 风险评估

| 风险 | 缓解 |
|------|------|
| 内置插件默认启用才能保证向后兼容 | 首次加载时自动启用所有内置插件 |
| 用户误关转换插件导致 DeepSeek 不可用 | proxy 返回的报错信息指明缺少哪个插件 |
| model_matcher 被禁用导致无匹配规则 | `required: true` 标记禁止禁用，插件名全局唯一 |
| 插件启用/禁用状态丢失 | 状态写入 `~/.akm/config.json` 的 `plugin_states` 字段 |
