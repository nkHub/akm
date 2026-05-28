# 插件系统设计

> 版本：WIP | 状态：设计中

## 目标

为 akm 提供可扩展的插件机制，支持第三方在不修改核心代码的情况下：
- 注册自定义 API 路由
- 提供独立的前端界面（集成到管理台菜单）
- 拦截请求/响应做自定义处理（日志、审计、过滤等）
- 访问项目数据库、配置、日志等上下文

## 一、插件类型

所有插件结构统一，通过 `plugin.json` 中的 `has_menu` 字段区分：

| `has_menu` | 描述 | 必需文件 |
|----------|------|----------|
| `true` | 在管理台显示菜单入口，提供 `views/` 目录，自动注册前端路由 | `plugin.json` + `index.py` + `views/index.html` |
| `false` | 不显示菜单，可注册 API 路由和请求/响应 hook | `plugin.json` + `index.py` |

> `has_menu` 默认为 `false`，不填即视为无需菜单入口。

## 二、目录结构

```
akm/
├── plugins/                      # 插件根目录
│   ├── __init__.py
│   ├── base.py                   # PluginBase 基类（提供上下文方法）
│   ├── plugin_manager.py         # 插件管理器
│   ├── builtin/                  # 内置插件（随项目分发，可禁用）
│   │   ├── responses_converter/  # Responses → Chat 协议转换
│   │   │   ├── plugin.json
│   │   │   └── index.py
│   │   ├── messages_converter/   # Messages → Chat 协议转换
│   │   │   ├── plugin.json
│   │   │   └── index.py
│   │   └── chat_converter/       # Chat → Messages 协议转换
│   │       ├── plugin.json
│   │       └── index.py
│   └── model_mapper/             # 示例插件（有菜单）
│       ├── plugin.json
│       ├── index.py              # 插件入口（导出 Plugin 类，继承 PluginBase）
│       └── views/
│           ├── index.html
│           ├── style.css
│           └── app.js
```

所有插件在同一层级，`builtin/` 只是组织方式，加载时同等待遇。通过 `plugin.json` 中的 `has_menu` 和 `builtin` 字段区分行为和来源。

## 三、plugin.json 定义

### 3.1 有菜单插件（`has_menu: true`）

```json
{
    "name": "model_mapper",
    "has_menu": true,
    "version": "1.0.0",
    "description": "模型名称映射配置插件，支持自定义模型别名",
    "menu": {
        "title": "模型映射",
        "icon": "swap",
        "order": 10
    },
    "routes_prefix": "/api/mapper"
}
```

### 3.2 无菜单插件（`has_menu: false`）

```json
{
    "name": "request_logger",
    "has_menu": false,
    "version": "1.0.0",
    "description": "增强请求日志",
    "routes_prefix": "/api/logger",
    "hooks": {
        "on_request": true,
        "on_response": true
    },
    "settings": [
        {
            "key": "max_retries",
            "label": "最大重试次数",
            "type": "number",
            "default": 3
        }
    ]
}
```

### 3.3 字段说明

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | ✓ | 插件唯一标识，作为目录名 |
| `has_menu` | bool | | 是否在管理台显示菜单入口，默认 `false` |
| `version` | string | ✓ | 语义化版本号 |
| `description` | string | | 功能描述 |
| `menu` | object | has_menu 时必需 | 菜单配置 |
| `menu.title` | string | ✓ | 菜单显示名称 |
| `menu.icon` | string | | 菜单图标，默认 `"plugin"` |
| `menu.order` | int | | 菜单位置排序，默认 `100` |
| `routes_prefix` | string | | API 路由前缀，默认 `/{name}` |
| `hooks.on_request` | bool | | 是否接收原始请求对象 |
| `hooks.on_response` | bool | | 是否接收原始响应对象 |
| `settings` | object[] | | 配置项定义，见 3.4 节 |

### 3.4 插件配置

插件可声明 `settings` 字段，定义自己的配置项。配置统一存储在 `~/.akm/config.json` 的 `plugin_configs` 字段中，格式为 `{ "插件名": { "key": "value" } }`。

#### 配置项定义

```json
{
    "settings": [
        {
            "key": "max_retries",
            "label": "最大重试次数",
            "type": "number",
            "default": 3,
            "min": 1,
            "max": 10,
            "description": "请求失败时最大重试次数"
        },
        {
            "key": "enable_cache",
            "label": "启用缓存",
            "type": "boolean",
            "default": true
        },
        {
            "key": "log_level",
            "label": "日志级别",
            "type": "select",
            "default": "info",
            "options": [
                { "label": "调试", "value": "debug" },
                { "label": "信息", "value": "info" },
                { "label": "警告", "value": "warn" }
            ]
        }
    ]
}
```

| 字段 | 说明 |
|------|------|
| `key` | 配置键名，存在 `config.json` 中 |
| `label` | 设置页显示名称 |
| `type` | `"string"` / `"number"` / `"boolean"` / `"select"` / `"text"` (多行) |
| `default` | 默认值 |
| `description` | 辅助说明文字 |
| `min` / `max` | 数值范围（type=number 时） |
| `options` | 下拉选项（type=select 时） |
| `required` | 是否必填，默认 `false` |

#### 配置的读写

插件通过 `self.config` 直接读取当前插件配置，无需手动查找：

```python
# index.py — Plugin 类中
async def on_request(self, request):
    if self.config.get("enable_cache"):
        ...
```

设置页通过 `settings` schema 自动渲染表单，保存后写入 `~/.akm/config.json`：

```json
{
    ...
    "plugin_configs": {
        "request_logger": {
            "max_retries": 5,
            "enable_cache": false,
            "log_level": "debug"
        }
    }
}
```

## 四、PluginBase 基类

### 4.1 设计

每个插件的 `index.py` 导出名为 `Plugin` 的类，继承自 `plugins.base.PluginBase`。PluginBase 封装了插件可访问的全部上下文和方法：

```python
# akm/plugins/base.py
import logging
from pathlib import Path
from fastapi import FastAPI, APIRouter

class PluginBase:
    """插件基类，由 PluginManager 在加载时注入上下文"""

    # ——— 由 PluginManager 注入的属性 ———
    name: str              # 插件名称（来自 plugin.json）
    app: FastAPI           # FastAPI 应用实例
    router: APIRouter      # 本插件的 APIRouter（可在 __init__ 中自定义）
    meta: dict             # plugin.json 的原始数据
    logger: logging.Logger # 本插件专属 logger

    # ——— 可重写的生命周期方法 ———
    async def on_load(self):
        """插件加载完成时调用（路由已注册），可做初始化操作"""
        pass

    async def on_unload(self):
        """插件卸载时调用，可做清理操作"""
        pass

    # ——— 可重写的 hook 方法 ———
    async def on_request(self, request) -> None:
        """请求到达时调用（需在 plugin.json 中声明 hooks.on_request: true）"""
        pass

    async def on_response(self, request, response) -> None:
        """响应返回后调用（需在 plugin.json 中声明 hooks.on_response: true）"""
        pass

    # ——— 辅助属性 ———
    @property
    def config(self) -> dict:
        """当前插件的配置（已合并 settings 默认值）"""
        return self._get_config()

    @property
    def db(self):
        """数据库连接（SQLite，与项目共享同一实例）"""
        return self._get_db()

    @property
    def static_dir(self) -> Path:
        """本插件 views/ 目录的绝对路径"""
        return self._static_dir

    # ——— 内部方法（由 PluginManager 设置） ———
    def _set_context(self, name: str, app: FastAPI, meta: dict, static_dir: Path):
        self.name = name
        self.app = app
        self.meta = meta
        self._static_dir = static_dir
        self.router = APIRouter()
        self.logger = logging.getLogger(f"plugin.{name}")

    def _get_config(self) -> dict: ...
    def _get_db(self): ...
```

### 4.2 上下文能力一览

| 属性/方法 | 类型 | 说明 |
|-----------|------|------|
| `self.name` | `str` | 插件名称 |
| `self.app` | `FastAPI` | 应用实例，可注册中间件、事件处理器等 |
| `self.router` | `APIRouter` | 本插件路由，在 `__init__` 中定义端点并自动挂载 |
| `self.config` | `dict` | 本插件配置（含默认值），运行时自动从 config.json 加载 |
| `self.db` | `sqlite3.Connection` | 项目共享数据库连接，可直接执行 SQL |
| `self.logger` | `Logger` | 插件专用 logger，输出格式 `[plugin.xxx]` |
| `self.meta` | `dict` | plugin.json 原始数据（含 settings schema 等） |
| `self.static_dir` | `Path` | views/ 目录路径，用于读取静态资源 |

### 4.3 生命周期

```
PluginManager.load_all()
  └── 对每个插件目录：
       ├── 1. 读取 plugin.json
       ├── 2. 动态导入 index.py，获取 Plugin 类
       ├── 3. 实例化 plugin = Plugin()
       ├── 4. 调用 plugin._set_context(name, app, meta, static_dir)
       ├── 5. 调用 plugin.on_load()                    # ← 初始化钩子
       ├── 6. app.include_router(plugin.router)        # ← 注册路由
       └── 7. 存入 self.plugins

应用关闭时 lifecycle shutdown：
  └── 对每个已加载插件：
       └── 调用 plugin.on_unload()                     # ← 清理钩子
```

## 五、PluginManager 设计

### 5.1 核心类

```python
class PluginManager:
    root: Path                              # 插件根目录
    plugins: Dict[str, PluginBase]           # 已加载的插件实例（name → PluginBase）

    load_all(app: FastAPI, db)              # 扫描并加载全部插件
    get_menu() -> list                      # 生成前端菜单结构（仅 has_menu 的插件）
    get_plugin_metas() -> list              # 获取所有插件元数据（含 settings schema）
    get_hook_plugins(hook: str)             # 获取注册了指定 hook 的插件实例列表
    run_hook(hook, request, response)       # 执行 hook
    get_config(name: str) -> dict           # 读取插件配置（合并默认值）
    set_config(name: str, data: dict)       # 保存插件配置到 config.json
```

### 5.2 加载流程

```
PluginManager.load_all(app, db)
  ├── 扫描 plugins/ 目录下所有子目录
  │   ├── 读取 plugin.json → 解析 PluginMeta
  │   ├── 动态导入 index.py → 获取 Plugin 类
  │   ├── 实例化 plugin 并注入上下文（app, db, config, logger）
  │   ├── 调用 plugin.on_load()
  │   ├── app.include_router(plugin.router, prefix=routes_prefix)
  │   ├── 如果 has_menu: true 且 views/ 存在
  │   │   ├── StaticFiles 挂载 views/ → /plugins/{name}/static
  │   │   └── 注册前端页面路由 /plugins/{name} → views/index.html
  │   └── 存入 self.plugins[name] = plugin
```

### 5.3 路由注册规则

- **API 路由**：插件的 `self.router`（在 `__init__` 中定义的 APIRouter）挂载到 `{routes_prefix}` 下
- **前端路由**（仅 has_menu 插件）：`/plugins/{name}` 和 `/plugins/{name}/{rest:path}` → `views/index.html`（SPA 支持）
- **静态文件**（仅 has_menu 插件）：`/plugins/{name}/static` → `views/` 目录（CSS/JS/图片等）

## 六、Hook 机制

### 6.1 触发时机

| Hook | 触发点 | 参数 | 用途 |
|------|--------|------|------|
| `on_request` | proxy 转发请求之前 | `request: Request` | 请求日志、参数校验、请求改写 |
| `on_response` | proxy 转发响应之后 | `request: Request, response: Response` | 响应日志、结果缓存、告警通知 |

### 6.2 约定

插件在 `Plugin` 类中重写 `on_request` / `on_response` 方法，同时在 `plugin.json` 的 `hooks` 中声明为 `true`。PluginManager 在对应时机自动调用：

```python
# index.py
from plugins.base import PluginBase
from fastapi import Request

class Plugin(PluginBase):
    """请求日志插件"""

    def __init__(self):
        super().__init__()
        self._total = 0

    async def on_request(self, request):
        self._total += 1
        self.logger.info(f"[#{self._total}] {request.method} {request.url.path}")

    async def on_response(self, request, response):
        self.logger.info(f"[#{self._total}] done")
```

### 6.3 执行顺序

Hook 按插件加载顺序依次执行，单个 hook 异常不会中断后续 hook 的执行。

## 七、与 server.py 集成

### 7.1 改动点

```python
# server.py

from .plugins.plugin_manager import PluginManager

# lifespan 中：
plugin_manager = PluginManager()
plugin_manager.load_all(app, db)  # db 传入共享数据库连接
app.state.plugin_manager = plugin_manager

# 新增菜单 API
@app.get("/api/plugin-menu")
async def plugin_menu(request: Request):
    return request.app.state.plugin_manager.get_menu()

# 新增插件配置 API
@app.get("/api/plugin-config/{name}")
async def plugin_get_config(name: str, request: Request):
    return request.app.state.plugin_manager.get_config(name)

@app.post("/api/plugin-config/{name}")
async def plugin_save_config(name: str, request: Request):
    body = await request.json()
    request.app.state.plugin_manager.set_config(name, body)
    return {"ok": True}

# 插件元数据（含 settings schema，供设置页渲染表单）
@app.get("/api/plugin-metas")
async def plugin_metas(request: Request):
    return request.app.state.plugin_manager.get_plugin_metas()

# AI 请求端点注入 hook
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    pm = request.app.state.plugin_manager
    await pm.run_hook("on_request", request)          # ← 请求前 hook
    result = await _handle_ai_request(request, "chat/completions")
    await pm.run_hook("on_response", request, result) # ← 响应后 hook
    return result

@app.post("/v1/responses")
async def responses(request: Request):
    pm = request.app.state.plugin_manager
    await pm.run_hook("on_request", request)
    result = await _handle_ai_request(request, "responses")
    await pm.run_hook("on_response", request, result)
    return result
```

### 7.2 前端集成

**菜单**：sidebar 调用 `/api/plugin-menu`，动态插入插件入口：

```javascript
fetch('/api/plugin-menu')
    .then(res => res.json())
    .then(items => items.forEach(item => sidebar.add(item)));
```

**设置页**：全局设置页调用 `/api/plugin-metas`，遍历每个插件的 `settings` 数组，按 schema 自动渲染表单（number→数字输入、boolean→开关、select→下拉、text→多行文本）。修改后 POST 到 `/api/plugin-config/{name}` 保存。

```javascript
fetch('/api/plugin-metas')
    .then(res => res.json())
    .then(metas => {
        metas.forEach(meta => {
            if (meta.settings?.length) {
                renderPluginSettings(meta.name, meta.settings);
            }
        });
    });
```

## 八、插件开发示例

### 8.1 有菜单插件：模型映射（操作数据库）

**plugins/model_mapper/plugin.json**
```json
{
    "name": "model_mapper",
    "has_menu": true,
    "version": "1.0.0",
    "description": "模型名称映射",
    "menu": { "title": "模型映射", "icon": "swap", "order": 10 },
    "routes_prefix": "/api/mapper"
}
```

**plugins/model_mapper/index.py**
```python
from plugins.base import PluginBase

class Plugin(PluginBase):
    """模型映射插件"""

    def __init__(self):
        super().__init__()
        # 定义路由
        self.router.add_api_route("/list", self.list_mappings)
        self.router.add_api_route("/add", self.add_mapping, methods=["POST"])

    async def on_load(self):
        """插件加载时初始化数据库表"""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS model_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original TEXT NOT NULL,
                mapped TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.db.commit()
        self.logger.info("映射表初始化完成")

    async def list_mappings(self):
        rows = self.db.execute("SELECT original, mapped FROM model_mappings").fetchall()
        return {"mappings": [{"original": r[0], "mapped": r[1]} for r in rows]}

    async def add_mapping(self, original: str, mapped: str):
        self.db.execute(
            "INSERT INTO model_mappings (original, mapped) VALUES (?, ?)",
            (original, mapped)
        )
        self.db.commit()
        return {"status": "ok", "original": original, "mapped": mapped}
```

**plugins/model_mapper/views/index.html**
```html
<!DOCTYPE html>
<html>
<head>
    <title>模型映射</title>
    <link rel="stylesheet" href="/plugins/model_mapper/static/style.css">
</head>
<body>
    <h1>模型映射配置</h1>
    <div id="app"></div>
    <script src="/plugins/model_mapper/static/app.js"></script>
</body>
</html>
```

### 8.2 无菜单插件：请求日志（hook + 配置）

**plugins/request_logger/plugin.json**
```json
{
    "name": "request_logger",
    "has_menu": false,
    "version": "1.0.0",
    "description": "增强请求日志",
    "routes_prefix": "/api/logger",
    "hooks": { "on_request": true, "on_response": true },
    "settings": [
        {
            "key": "max_retries",
            "label": "最大重试次数",
            "type": "number",
            "default": 3,
            "min": 1,
            "max": 10
        },
        {
            "key": "enable_stats",
            "label": "启用统计",
            "type": "boolean",
            "default": true
        }
    ]
}
```

**plugins/request_logger/index.py**
```python
from datetime import datetime
from plugins.base import PluginBase

class Plugin(PluginBase):
    """请求日志插件 — 通过 hook 拦截请求/响应"""

    def __init__(self):
        super().__init__()
        self._start_times = {}  # request_id → 开始时间

    async def on_request(self, request):
        if self.config.get("enable_stats"):
            rid = id(request)
            self._start_times[rid] = datetime.now()
            self.logger.info(f"→ {request.method} {request.url.path}")

    async def on_response(self, request, response):
        if self.config.get("enable_stats"):
            rid = id(request)
            start = self._start_times.pop(rid, None)
            if start:
                elapsed = (datetime.now() - start).total_seconds()
                self.logger.info(f"← {request.url.path} ({elapsed:.2f}s)")
```

## 九、安全考虑

- 插件代码在 akm 进程中运行，拥有完整权限（包括数据库），仅应由信任的开发者编写
- `plugin.json` 中不包含可执行代码
- `PluginBase` 中的数据库访问为共享连接，插件需自行管理事务和锁
- 插件加载失败时打印警告但不阻止 akm 启动
- hook 执行异常被捕获，不影响请求正常流程
