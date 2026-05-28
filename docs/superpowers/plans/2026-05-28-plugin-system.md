# 插件系统实现计划

> 状态：规划中 | 基于设计文档 [plugin-system.md](../design/plugin-system.md)

**Goal:** 实现 akm 插件系统，包含插件管理界面（列表、开启/关闭/删除）和将现有三个协议适配器改造为内置无界面插件。

## 可行性评估

**适配器改插件的结论：完全可行。**

| 维度 | 评估 |
|------|------|
| 系统耦合 | 适配器零耦合 — 不依赖 app、db、config、key_pool、FastAPI Request/Response，仅做纯数据转换 |
| 构造参数 | 全部无参构造（`ResponsesAdapter()`、`MessagesAdapter()`），可直接作为插件实例化 |
| 职责边界 | 每个适配器职责单一，天然对应"协议转换"插件 |
| 改造量 | 需要在 agent.py 和 proxy.py 中把硬编码的适配器引用改为通过 PluginManager 查询，改动可控 |

**需要解决的问题**：当前 `agent.needs_conversion()` 硬编码了 `supports_responses`/`supports_chat` 和适配器的对应关系 → 需要改为插件注册 + PluginManager 查询的模式。

---

## 文件结构

```
akm/
├── plugins/
│   ├── __init__.py
│   ├── base.py                    # PluginBase 基类
│   ├── plugin_manager.py          # PluginManager
│   └── builtin/                   # 内置插件（协议转换）
│       ├── responses_converter/
│       │   ├── plugin.json
│       │   └── index.py
│       ├── messages_converter/
│       │   ├── plugin.json
│       │   └── index.py
│       └── chat_converter/
│           ├── plugin.json
│           └── index.py
├── adapters/                      # 保留，但转为插件内部引用
│   ├── __init__.py
│   ├── responses_adapter.py
│   ├── messages_adapter.py
│   └── chat_adapter.py
├── server.py                      # 集成 PluginManager
└── agent.py                       # 移除硬编码适配器，改为插件查询
```

---

## Task 1: PluginBase 基类 + PluginManager 核心

**目标**: 实现插件基础设施，支持扫描、加载、生命周期、配置读写

- [ ] **Step 1.1: 创建 `akm/plugins/base.py` — PluginBase 基类**
  - 属性注入：`name`、`app`、`router`、`meta`、`logger`、`_static_dir`
  - 生命周期：`on_load()`、`on_unload()`（默认空实现）
  - hook：`on_request(request)`、`on_response(request, response)`
  - 辅助属性：`self.config`（自动从 config.json 读）、`self.db`（共享连接）
  - 内部方法：`_set_context(name, app, meta, static_dir)`、`_inject_db(db)`、`_inject_config_manager(config_get, config_set)`

- [ ] **Step 1.2: 创建 `akm/plugins/plugin_manager.py` — PluginManager**
  - `load_all(app, db)`：扫描 `plugins/` 下所有子目录
    - 读取 `plugin.json` → 解析 PluginMeta
    - 动态导入 `index.py` → 实例化 `Plugin` 类
    - 注入上下文（app, db, config, logger）
    - 调用 `plugin.on_load()`
    - `app.include_router(plugin.router, prefix=routes_prefix)`
    - 若 `has_menu` + `views/` 存在，挂载静态文件和前端路由
    - 存入 `self.plugins`
  - `get_menu()`：生成有菜单插件的菜单列表（按 order 排序）
  - `get_plugin_metas()`：返回所有插件元数据（含 settings schema，供设置页渲染）
  - `get_hook_plugins(hook)`：返回注册了指定 hook 的插件实例
  - `run_hook(hook, request, response)`：遍历执行，异常隔离
  - `get_config(name)` / `set_config(name, data)`：插件配置读写
  - `enable_plugin(name)` / `disable_plugin(name)` / `delete_plugin(name)`：运行状态管理
  - `get_converter(from_format, to_format) -> Plugin | None`：查询协议转换插件

- [ ] **Step 1.3: PluginMeta Pydantic 模型**
  ```python
  class SettingDef(BaseModel):
      key: str; label: str; type: str  # string/number/boolean/select/text
      default: Any; description: str = ""; min: int = None; max: int = None
      options: list = []; required: bool = False

  class PluginMeta(BaseModel):
      name: str; version: str; has_menu: bool = False
      description: str = ""
      menu: dict = {}           # { title, icon, order }
      routes_prefix: str = ""
      hooks: dict = {"on_request": False, "on_response": False}
      settings: list[SettingDef] = []
      converts: dict = None     # 协议转换声明，如 { "from": "responses", "to": "chat" }
  ```

---

## Task 2: 适配器改造为内置插件

**目标**: 将现有的三个适配器类包装成 PluginBase 子类，放入 `plugins/builtin/`，register 协议转换映射

- [ ] **Step 2.1: responses_converter 插件**
  ```python
  # plugins/builtin/responses_converter/index.py
  from plugins.base import PluginBase
  from akm.adapters.responses_adapter import ResponsesAdapter

  class Plugin(PluginBase):
      def __init__(self):
          super().__init__()
          self._adapter = ResponsesAdapter()

      async def convert_request(self, body: dict) -> dict:
          return self._adapter.convert_request(body)

      async def convert_sse_stream(self, stream):
          async for event in self._adapter.convert_sse_stream(stream):
              yield event
  ```

  **plugin.json**:
  ```json
  {
      "name": "responses_converter",
      "has_menu": false,
      "version": "1.0.0",
      "description": "将 OpenAI Responses API 请求转为 Chat Completions 格式（DeepSeek 等供应商使用）",
      "converts": { "from": "responses", "to": "chat" },
      "system": true
  }
  ```

- [ ] **Step 2.2: messages_converter 插件**
  - 同模式，`converts: { "from": "messages", "to": "chat" }`

- [ ] **Step 2.3: chat_converter 插件**
  - 同模式，`converts: { "from": "chat", "to": "messages" }`
  - 当前 ChatAdapter 为透传空壳，后续可扩展

- [ ] **Step 2.4: 适配器注册到 PluginManager**
  - `PluginManager.get_converter("responses", "chat")` → 返回 `responses_converter` 插件实例
  - 返回 `None` 表示没有对应的转换插件（即无需转换，供应商原生支持该格式）

---

## Task 3: agent.py / proxy.py 改造

**目标**: 将硬编码的适配器引用改为通过 PluginManager 动态查询

- [ ] **Step 3.1: agent.py — 移除硬编码适配器属性**
  - 删除 `responses_adapter`、`messages_adapter`、`chat_adapter` 三个懒加载属性（约 30 行）
  - 保留 `supports_responses`、`supports_chat`、`supports_messages` 能力标记
  - `needs_conversion()` 保持不变（判断是否需要转换），但不再返回 adapter 实例，只返回目标格式

- [ ] **Step 3.2: proxy.py 改造**
  ```python
  # 改造前
  adapter = agent.responses_adapter  # 硬编码
  converted_body = adapter.convert_request(body)

  # 改造后
  plugin_manager = app.state.plugin_manager  # 或通过参数传入
  converter = plugin_manager.get_converter("responses", "chat")
  if converter and converter.enabled:
      converted_body = converter.convert_request(body)
  else:
      raise UnsupportedFormat("vendor doesn't support responses and no converter plugin available")
  ```

- [ ] **Step 3.3: server.py — 传递 PluginManager 给 proxy**
  - `forward_request()` 新增参数 `plugin_manager`
  - 或通过 `request.app.state.plugin_manager` 获取（如果 proxy 能拿到 request）

---

## Task 4: 插件管理界面

**目标**: 管理台新增插件管理页面，列表展示所有已加载插件，支持开启/关闭/删除

### 4.1 API 端点

- [ ] **`GET /api/plugins`** — 插件列表
  ```json
  {
      "data": [
          {
              "name": "responses_converter",
              "version": "1.0.0",
              "description": "Responses → Chat 协议转换",
              "has_menu": false,
              "system": true,
              "enabled": true,
              "status": "loaded"
          },
          {
              "name": "model_mapper",
              "version": "1.0.0",
              "description": "模型名称映射",
              "has_menu": true,
              "system": false,
              "enabled": true,
              "status": "loaded",
              "menu": { "title": "模型映射", "icon": "swap" }
          }
      ]
  }
  ```

- [ ] **`POST /api/plugins/{name}/enable`** — 启用插件
- [ ] **`POST /api/plugins/{name}/disable`** — 禁用插件（路由不卸载，但 hook 和转换不再执行）
- [ ] **`DELETE /api/plugins/{name}`** — 删除插件（仅非 system 插件，system 插件不可删除）

### 4.2 前端页面

- [ ] **管理台新增「插件管理」入口**（/plugins/manage）
  - 插件列表表格：名称、版本、描述、类型（内置/第三方）、状态开关、操作按钮
  - 状态开关：点击切换启用/禁用
  - 删除按钮：确认后删除（仅第三方插件）
  - 内置插件（system: true）标记为灰色，不可删除

---

## Task 5: 集成与端到端测试

- [ ] **Step 5.1: server.py lifespan 集成**
  ```python
  plugin_manager = PluginManager()
  plugin_manager.load_all(app, db)
  app.state.plugin_manager = plugin_manager
  ```

- [ ] **Step 5.2: 现有测试适配**
  - 现有 31 个适配器测试保持通过（适配器类本身不动）
  - 新增插件加载流程测试
  - 新增协议转换通过插件查询的测试

- [ ] **Step 5.3: 端到端验证**
  - Codex + DeepSeek → `/v1/responses` → 插件提供的 responses→chat 转换 → 正常返回
  - Anthropic → `/v1/chat/completions` → 插件提供的 chat→messages 转换 → 正常返回
  - 禁用 responses_converter 插件后 → DeepSeek `/v1/responses` 请求返回 400 Unsupported

---

## 风险评估

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 插件系统增加请求链路延迟 | 低 | 低 | 适配器本身已是纯函数，插件包装开销可忽略；无需额外 IPC |
| `system: true` 插件误删致核心功能不可用 | 低 | 高 | 前端禁用删除按钮 + API 层二次校验 |
| 插件启用/禁用状态未持久化 | 中 | 中 | 状态写入 `config.json`，加载时恢复 |
| 动态导入 index.py 的 importlib 安全问题 | 低 | 中 | 仅信任本地插件目录，不暴露远程安装能力 |
