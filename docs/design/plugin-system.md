# 插件系统设计

> 版本：WIP | 状态：设计中

## 目标

为 akm 提供可扩展的插件机制，支持第三方在不修改核心代码的情况下：
- 注册自定义 API 路由
- 提供独立的前端界面（集成到管理台菜单）
- 拦截请求/响应做自定义处理（日志、审计、过滤等）

## 一、插件类型

分两种，通过 `plugin.json` 中的 `type` 字段区分：

| 类型 | `type` 值 | 描述 | 必需 |
|------|----------|------|------|
| 应用插件 | `"app"` | 有自定义前端界面，注册 API 路由 + 提供 views/ 目录 | `plugin.json` + `router.py` + `views/index.html` |
| 服务端插件 | `"server"` | 纯后端，无前端，可注册 API 路由和请求/响应 hook | `plugin.json` + `router.py` |

## 二、目录结构

```
akm/
├── plugins/                      # 插件根目录
│   ├── __init__.py
│   ├── plugin_manager.py         # 插件管理器
│   ├── app_plugins/              # 有界面插件
│   │   └── model_mapper/
│   │       ├── plugin.json       # 元数据 + 菜单配置
│   │       ├── router.py         # API 路由（约定导出 `router` 对象）
│   │       └── views/            # 前端页面（最少 index.html）
│   │           ├── index.html
│   │           ├── style.css
│   │           └── app.js
│   └── server_plugins/           # 无界面插件
│       └── request_logger/
│           ├── plugin.json
│           └── router.py
```

## 三、plugin.json 定义

### 3.1 应用插件（`type: "app"`）

```json
{
    "name": "model_mapper",
    "type": "app",
    "version": "1.0.0",
    "description": "模型名称映射配置插件，支持自定义模型别名",
    "menu": {
        "title": "模型映射",
        "icon": "swap",
        "order": 10
    },
    "routes_prefix": "/api/mapper",
    "hooks": {
        "on_request": false,
        "on_response": false
    }
}
```

### 3.2 服务端插件（`type: "server"`）

```json
{
    "name": "request_logger",
    "type": "server",
    "version": "1.0.0",
    "description": "增强请求日志，记录完整请求/响应内容到独立存储",
    "routes_prefix": "/api/logger",
    "hooks": {
        "on_request": true,
        "on_response": true
    }
}
```

### 3.3 字段说明

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | ✓ | 插件唯一标识，作为目录名 |
| `type` | string | ✓ | `"app"` 或 `"server"` |
| `version` | string | ✓ | 语义化版本号 |
| `description` | string | | 功能描述 |
| `menu` | object | app 必需 | 菜单配置，仅 app 类型 |
| `menu.title` | string | ✓ | 菜单显示名称 |
| `menu.icon` | string | | 菜单图标，默认 `"plugin"` |
| `menu.order` | int | | 菜单位置排序，默认 `100` |
| `routes_prefix` | string | | API 路由前缀，默认 `/{name}` |
| `hooks.on_request` | bool | | 是否接收原始请求对象 |
| `hooks.on_response` | bool | | 是否接收原始响应对象 |
| `settings` | object[] | | 配置项定义，见下方 3.4 节 |

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

插件管理器自动注册 `/api/plugin-config/{name}` 端点：

```
GET  /api/plugin-config/{name}      # 读取插件配置（含默认值）
POST /api/plugin-config/{name}      # 保存插件配置
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

插件在 `router.py` 中通过 `request.app.state.plugin_manager.get_config(name)` 读取：

```python
from fastapi import Request

@router.get("/stats")
async def get_stats(request: Request):
    cfg = request.app.state.plugin_manager.get_config("request_logger")
    if cfg.get("enable_cache"):
        ...
```

## 四、PluginManager 设计

### 4.1 核心类

```python
class PluginManager:
    root: Path                              # 插件根目录
    app_plugins: Dict[str, PluginInfo]       # 已加载的应用插件
    server_plugins: Dict[str, PluginInfo]    # 已加载的服务端插件

    load_all(app: FastAPI)                  # 扫描并加载全部插件
    get_menu() -> list                      # 生成前端菜单结构
    get_plugin_metas() -> list              # 获取所有插件元数据（含 settings schema）
    get_hook_plugins(hook: str)             # 获取注册了指定 hook 的插件列表
    run_hook(hook, request, response)       # 执行 hook
    get_config(name: str) -> dict           # 读取插件配置（合并默认值）
    set_config(name: str, data: dict)       # 保存插件配置到 config.json
```

### 4.2 加载流程

```
PluginManager.load_all(app)
  ├── 扫描 app_plugins/ 目录
  │   ├── 读取 plugin.json → 解析 PluginMeta
  │   ├── 动态导入 router.py → 获取 APIRouter 实例
  │   │   └── app.include_router(router, prefix=routes_prefix)
  │   ├── StaticFiles 挂载 views/ → /plugins/{name}/static
  │   ├── 注册前端页面路由 /plugins/{name} → views/index.html
  │   └── 存入 self.app_plugins
  └── 扫描 server_plugins/ 目录
      ├── 读取 plugin.json
      ├── 动态导入 router.py → include_router
      └── 存入 self.server_plugins
```

### 4.3 路由注册规则

- **API 路由**：`router.py` 中导出的 `router`（FastAPI `APIRouter` 实例）挂载到 `{routes_prefix}` 下
- **前端路由**（仅 app 类型）：`/plugins/{name}` 和 `/plugins/{name}/{rest:path}` → `views/index.html`（SPA 支持）
- **静态文件**（仅 app 类型）：`/plugins/{name}/static` → `views/` 目录（CSS/JS/图片等）

## 五、Hook 机制

### 5.1 触发时机

| Hook | 触发点 | 参数 | 用途 |
|------|--------|------|------|
| `on_request` | proxy 转发请求之前 | `request: Request` | 请求日志、参数校验、请求改写 |
| `on_response` | proxy 转发响应之后 | `request: Request, response: Response` | 响应日志、结果缓存、告警通知 |

### 5.2 约定

插件在 `router.py` 中声明与 hook 同名的异步函数，PluginManager 在对应时机自动调用：

```python
# router.py
from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter()

async def on_request(request: Request, response=None):
    """请求到达时记录"""
    print(f"[{request.method}] {request.url.path}")

async def on_response(request: Request, response=None):
    """响应返回时处理"""
    pass
```

### 5.3 执行顺序

Hook 按插件加载顺序依次执行，单个 hook 异常不会中断后续 hook 的执行。

## 六、与 server.py 集成

### 6.1 改动点

```python
# server.py

from .plugins.plugin_manager import PluginManager

# lifespan 中：
plugin_manager = PluginManager()
plugin_manager.load_all(app)
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

### 6.2 前端集成

**菜单**：sidebar 调用 `/api/plugin-menu`，动态插入插件入口：

```javascript
fetch('/api/plugin-menu')
    .then(res => res.json())
    .then(items => items.forEach(item => sidebar.add(item)));
```

**设置页**：全局设置页调用 `/api/plugin-metas`，遍历每个插件的 `settings` 数组，按 schema 自动渲染表单（number→数字输入、boolean→开关、select→下拉、text→多行文本）。修改后 POST 到 `/api/plugin-config/{name}` 保存。

```javascript
// settings.html 中
fetch('/api/plugin-metas')
    .then(res => res.json())
    .then(metas => {
        metas.forEach(meta => {
            if (meta.settings?.length) {
                renderPluginSettings(meta.name, meta.settings);
            }
        });
    });

function renderPluginSettings(name, settings) {
    const section = createSection(`${name} 设置`);
    settings.forEach(s => {
        let el;
        if (s.type === 'boolean') el = createToggle(s.label, s.default);
        else if (s.type === 'select') el = createSelect(s.label, s.options);
        else if (s.type === 'text') el = createTextarea(s.label, s.default);
        else el = createInput(s.label, s.type, s.default, s.min, s.max);
        section.append(el);
    });
    section.onSave(() => fetch(`/api/plugin-config/${name}`, { method: 'POST', body: collect(section) }));
}
```

## 七、插件开发示例

### 7.1 应用插件：模型映射

**plugins/app_plugins/model_mapper/plugin.json**
```json
{
    "name": "model_mapper",
    "type": "app",
    "version": "1.0.0",
    "description": "模型名称映射",
    "menu": { "title": "模型映射", "icon": "swap", "order": 10 },
    "routes_prefix": "/api/mapper"
}
```

**plugins/app_plugins/model_mapper/router.py**
```python
from fastapi import APIRouter

router = APIRouter()

@router.get("/list")
async def list_mappings():
    return {"mappings": {"gpt-4": "gpt-oss:20b", "claude": "claude-opus"}}

@router.post("/add")
async def add_mapping(original: str, mapped: str):
    # 写入数据库
    return {"status": "ok", "original": original, "mapped": mapped}
```

**plugins/app_plugins/model_mapper/views/index.html**
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

### 7.2 服务端插件：请求日志（带配置）

**plugins/server_plugins/request_logger/plugin.json**
```json
{
    "name": "request_logger",
    "type": "server",
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

**plugins/server_plugins/request_logger/router.py**
```python
from fastapi import APIRouter, Request
from datetime import datetime

router = APIRouter()

async def on_request(request: Request, response=None):
    cfg = request.app.state.plugin_manager.get_config("request_logger")
    if cfg.get("enable_stats"):
        request.state._logger_start = datetime.now()

async def on_response(request: Request, response=None):
    cfg = request.app.state.plugin_manager.get_config("request_logger")
    if cfg.get("enable_stats"):
        elapsed = (datetime.now() - request.state._logger_start).total_seconds()
        print(f"[logger] {request.url.path} in {elapsed:.2f}s")

@router.get("/stats")
async def get_stats(request: Request):
    cfg = request.app.state.plugin_manager.get_config("request_logger")
    return {"enabled": cfg.get("enable_stats"), "retries": cfg.get("max_retries")}
```

## 八、安全考虑

- 插件代码在 akm 进程中运行，拥有完整权限，仅应由信任的开发者编写
- `plugin.json` 中不包含可执行代码
- 插件加载失败时打印警告但不阻止 akm 启动
- hook 执行异常被捕获，不影响请求正常流程
