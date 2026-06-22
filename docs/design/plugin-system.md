# 插件系统设计

> 版本：WIP | 状态：设计中

## 目标

为 akm 提供可扩展的插件机制，支持第三方在不修改核心代码的情况下：
- 注册自定义 API 路由
- 提供独立的前端界面（集成到管理台菜单）
- 拦截请求/响应做自定义处理（日志、审计、过滤等）
- 访问项目数据库、配置、日志等上下文

## 一、插件分类

根据在代理转发链路中的职责不同，插件分为以下类别：

```
请求到达 → [请求处理] → [Key匹配] → [格式转换] → 上游转发 → [错误处理] → [响应处理] → 返回
             category=      category=   category=                category=    category=
             filter         matcher     converter               handler      post
```

| `category` | 名称 | 职责 | 核心 Hook | 示例 |
|------------|------|------|-----------|------|
| `filter` | 请求处理 | 请求到达时对数据做预处理（加密、参数注入、内容屏蔽） | `on_request` | 请求体加密、敏感词过滤、单向脱敏 |
| `matcher` | 模型匹配 | 根据请求模型名选择/映射到实际的 key 或模型 | `on_key_selected` | 模型别名映射、权重路由 |
| `converter` | 格式转换 | 请求/响应在不同协议格式间转换 | `convert_request` / `convert_sse_stream` | Responses→Chat、JSON→YAML |
| `handler` | 错误处理 | 上游返回错误时的重试、切换、降级策略 | `on_upstream_error` | 5xx 重试、429 切换 key |
| `post` | 响应处理 | 响应返回后的日志、统计、缓存 | `on_response` | 审计增强、耗时统计 |
| `app` | 应用插件 | 有独立前端界面，注册 API 路由 | `self.router` + `views/` | 管理台、数据面板 |

一个插件可以注册多个 hook，跨多个 category。`category` 字段仅用于管理界面分类展示。

## 二、来源与优先级

| 来源 | 路径 | 说明 |
|------|------|------|
| 内置 | `akm/plugins/` | 随项目分发，可禁用 |
| 项目本地 | `plugins/` | 跟随当前仓库加载，适合样例插件或开发中的实验插件 |
| 第三方 | `~/.akm/plugins/` | 用户上传安装，可禁用/删除 |

> **插件名全局唯一**。第三方安装时若与内置同名，拒绝安装并提示「与内置插件冲突，请先禁用或重命名」。内置插件之间不可重名。

## 三、插件结构

所有插件结构统一。`has_menu: true` 表示在管理台显示菜单入口：

| `has_menu` | 描述 | 必需文件 |
|----------|------|----------|
| `true` | 在管理台显示菜单入口，提供 `views/` 目录，自动注册前端路由。插件列表中的 `converts` 仅在「插件已启用」时可点击进入页面，未启用时仅灰显展示。 | `plugin.json` + `index.py` + `views/index.html` |
| `false` | 不显示菜单，可注册 API 路由、请求/响应 hook。插件列表不展示 `converts` 节点。 | `plugin.json` + `index.py` |

> `has_menu` 默认为 `false`，不填即视为无需菜单入口。部分关键内置插件（如 model_matcher）标记为 `required: true`，不可禁用，保证核心链路至少有一个生效。

当前实现里，有菜单插件的访问路径建议区分两层：

- `/plugins/<name>`：AKM 后台宿主页，保留左侧菜单、顶部栏和统一外壳
- `/plugins/<name>/raw`：插件原始 `views/index.html`，通常由宿主页内的 iframe 加载

这样做的目的是在不强迫每个插件都重写为 AKM 模板语法的前提下，仍然保留统一后台导航与页面结构。

对于插件配置交互，当前实现也已经统一成一条默认规则：只要插件声明了 `settings`，插件列表页就默认通过“配置”按钮打开弹窗编辑，不再在列表卡片里额外展开内联表单。这样可以避免同一个插件同时出现“弹窗配置”和“展开配置”两套入口。

另外，setting schema 现在支持通过 `type="select" + options_source="/v1/models"` 声明一个基于当前模型列表的动态下拉；`allow_empty_option` 与 `empty_option_label` 可控制是否允许空值和空值文案。这样像 `markdown_kb` 这类需要选择 embedding / rerank / chat 模型的插件，就不需要再把模型列表逻辑硬编码在公共模板里。当前 `markdown_kb` 还额外使用了普通 number setting 来表达检索调优项：`top_k`（默认 `4`、最大 `10`）、`score_threshold`（`0~1`，默认 `0.7`）以及第一阶段混合召回使用的 `semantic_weight / keyword_weight`。其中 `keyword_weight` 当前不再是轻量覆盖率信号，而是对应归一化后的 BM25 字面分；因此即使启用 rerank，第一阶段候选召回也仍然会保留“向量分 + BM25 分”的混合粗召回，再由 rerank 做第二阶段重排。`markdown_kb` 现在优先使用第三方 `markdown-chunker` 做结构感知切片，并在依赖缺失或第三方异常时回退到内置的标题优先切片器；这样可以在不牺牲可用性的前提下，提高标题、表格、代码块和列表的结构保持能力。当前 BM25 的中文 tokenization 也已经升级为“`jieba3` 的 `small` 模型优先、2~4 字滑窗回退”：英文 token 逻辑保持不变，中文连续片段优先走自然分词，只有在本地环境未安装 `jieba3` 或初始化失败时才回退到滑窗策略。`markdown_kb` 的测试页还会基于当前文件列表额外渲染一个去重后的 “Workspace 范围” 下拉：默认不选时继续按请求 `workspace` 过滤；如果显式选中某个 workspace，则会把该值写入 `workspace_root / working_directory`，让 query / ask 只在“公共文档 + 该 workspace 文档”范围内执行。当前 `markdown_kb` 的 `on_request` 也已接到三类文本入口：插件启用后会对 `/v1/chat/completions`、`/v1/messages`、`/v1/responses` 自动尝试抽取最后一条用户问题并执行检索，只有命中非空时才按各协议原生字段把参考资料注入回请求体，未命中的请求则原样透传。

## 四、目录结构

```
akm/
├── plugins/                      # 内置插件（随项目分发，可禁用）
│   ├── __init__.py
│   ├── base.py                   # PluginBase 基类（提供上下文方法）
│   ├── plugin_manager.py         # 插件管理器
│   ├── responses_converter/      # Responses → Chat 协议转换
│   │   ├── plugin.json
│   │   └── index.py
│   ├── messages_converter/       # Messages → Chat 协议转换
│   │   ├── plugin.json
│   │   └── index.py
│   ├── chat_converter/           # Chat → Messages 协议转换
│   │   ├── plugin.json
│   │   └── index.py
│   ├── model_matcher/            # 默认模型匹配（不可禁用）
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
            ├── index.html
            ├── style.css
            └── app.js
```

**插件来源**：

| 来源 | 路径 | 特点 |
|------|------|------|
| 内置 | `akm/plugins/` | 随项目分发，`plugin.json` 中 `builtin: true`，可禁用但建议保留 |
| 项目本地 | `plugins/` | 与当前仓库一起开发和提交，适合作为样例、PoC 或尚未打包的本地插件 |
| 第三方 | `~/.akm/plugins/` | 用户自行安装，可安装/启用/禁用/删除 |

当前实现中，`PluginManager` 的实际加载顺序是：`akm/plugins/` 内置插件 -> 项目根目录 `plugins/` -> `~/.akm/plugins/` 第三方插件。三者仍共享同一套“插件名全局唯一”约束，后加载来源遇到重名时会被跳过。


## 五、plugin.json 定义

### 5.1 有菜单插件（`has_menu: true`）

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

### 5.2 无菜单插件（`has_menu: false`）

**请求日志插件示例**：
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

**协议转换插件示例**（`category: "converter"`）：
```json
{
    "name": "responses_converter",
    "category": "converter",
    "has_menu": false,
    "builtin": true,
    "version": "1.0.0",
    "description": "Responses → Chat 协议转换",
    "converts": { "from": "responses", "to": "chat" }
}
```

> `converts` 字段声明源格式和目标格式，PluginManager 通过 `get_converter(from, to)` 查找匹配的转换插件。

当前内置 `protocol_converter` 已合并 Responses / Messages / Chat 三类转换能力，而不是按旧设计拆成多个 converter 插件。它在 Responses → Chat 链路中还维护一层轻量内存会话缓存，用于 Codex 通过 `previous_response_id` 续接 Chat-only 上游时恢复上一轮 Chat 历史；缓存内容包含 `assistant` 文本、`reasoning_content`、`tool_calls` 与后续 `tool` 结果所需的 `tool_call_id`，默认最多保留 256 条、24 小时，进程重启后清空。

`protocol_converter` 针对 Codex + DeepSeek thinking/tool-call 场景做了以下兼容处理：

- `function_call_output.output` 会统一序列化为字符串，避免生成非法 Chat `role=tool` 消息。
- structured output 的 `response_format.json_schema.schema` 与 `text.format.json_schema.schema` 会复用 schema 清洗逻辑，移除 `strict` / `additionalProperties`。
- 流式工具调用同时发 legacy 事件（`response.output_item.*`、`response.function_call_arguments.*`）和现代事件（`response.output_tool_call.begin/delta/end`、`response.done`），兼容不同 Codex 版本。
- 现代 continuation 形态（`role="tool"`、`tool_call_id`、typed output content）会转换为 Chat `tool` 消息。

### 5.3 字段说明

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | ✓ | 插件唯一标识，作为目录名 |
| `category` | string | | 插件分类：`filter`/`matcher`/`converter`/`handler`/`post`/`app` |
| `has_menu` | bool | | 是否在管理台显示菜单入口，默认 `false` |
| `version` | string | ✓ | 语义化版本号 |
| `description` | string | | 功能描述 |
| `menu` | object | has_menu 时必需 | 菜单配置 |
| `menu.title` | string | ✓ | 菜单显示名称 |
| `menu.icon` | string | | 菜单图标，默认 `"plugin"` |
| `menu.order` | int | | 菜单位置排序，默认 `100` |
| `routes_prefix` | string | | API 路由前缀，默认 `/{name}` |
| `settings_columns` | int | | 配置表单列数，当前支持 `1` 或 `2`，默认 `1` |
| `hooks.on_request` | bool | | 是否接收请求对象（可改写请求体） |
| `hooks.on_key_selected` | bool | | 是否接收 key 选择事件 |
| `hooks.on_upstream_error` | bool | | 是否接收上游错误事件 |
| `hooks.on_response` | bool | | 是否接收响应对象 |
| `builtin` | bool | | 是否为内置插件，默认 `false` |
| `required` | bool | | 是否不可禁用，默认 `false` |
| `priority` | int | | 同 hook 插件的执行优先级，0-999，越小越先，默认 `100` |
| `converts` | object | converter 时必需 | `{ "from": "responses", "to": "chat" }` |
| `settings` | object[] | | 配置项定义，见 5.4 节 |

### 5.4 插件配置

插件可声明 `settings` 字段，定义自己的配置项。配置统一存储在 `~/.akm/config.json` 的 `plugin_configs` 字段中，格式为 `{ "插件名": { "key": "value" } }`。另外可选声明 `settings_columns` 控制插件配置弹窗布局：默认单列，声明 `2` 时会按双列渲染，适合 `markdown_kb` 这类短数值项较多的插件。

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

## 六、PluginBase 基类

### 6.1 设计

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
    async def on_request(self, request) -> dict | None:
        """请求到达时调用（需在 plugin.json 中声明 hooks.on_request: true）
        
        返回 dict 则替换请求体（如模型名映射）
        """
        pass

    async def on_key_selected(self, model: str, key: dict, request) -> dict | None:
        """Key 被匹配后调用，可修改 key 或用另一个 key 替换
        返回 None 表示不修改，返回 dict 替换当前 key
        """
        pass

    async def on_upstream_error(self, request, response, key) -> str | None:
        """上游返回错误时调用，返回 "retry" 重试 / "switch" 切换 key / None 继续默认处理"""
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

### 6.2 上下文能力一览

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

### 6.3 生命周期

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

## 七、PluginManager 设计

### 7.1 核心类

```python
class PluginManager:
    root: Path                              # 插件根目录
    plugins: Dict[str, PluginBase]           # 已加载的插件实例（name → PluginBase）

    load_all(app: FastAPI, db)              # 扫描并加载全部插件
    get_menu() -> list                      # 生成前端菜单结构（仅 has_menu 的插件）
    get_plugin_metas() -> list              # 获取所有插件元数据（含 settings schema）
    get_hook_plugins(hook: str)             # 获取注册了指定 hook 的插件实例列表
    run_hook(hook, **kwargs) -> Any          # 管道执行：按 priority 从小到大，前一个返回值传给下一个（带崩溃隔离）
    get_config(name: str) -> dict           # 读取插件配置（合并默认值）
    set_config(name: str, data: dict)       # 保存插件配置到 config.json
    install_plugin(file: UploadFile)        # 解压 .zip 到 ~/.akm/plugins/
    delete_plugin(name: str)                # 删除 ~/.akm/plugins/{name}/
    get_plugin_list() -> list               # 全部插件状态（含加载失败）
    get_converter(from, to) -> Plugin|None  # 查找启用的转换插件
```

### 7.2 加载流程

> **注意：插件启用/禁用/安装后需手动重启 akm 服务生效。** 不支持热重载。

```
PluginManager.load_all(app, db)
  ├── 扫描 akm/plugins/ 下所有子目录（内置插件）
  │   ├── 同上：解析 meta、导入 index.py、注入上下文、on_load、注册路由
  │   └── 存入 self.plugins[name]
  └── 扫描 ~/.akm/plugins/ 下所有子目录（第三方插件）
      ├── 同上加载流程
      └── 存入 self.plugins[name]（若与已加载的内置插件重名，跳过并记录警告）
```

### 7.3 路由注册规则

- **API 路由**：插件的 `self.router`（在 `__init__` 中定义的 APIRouter）挂载到 `{routes_prefix}` 下
- **前端路由**（仅 has_menu 插件）：`/plugins/{name}` 和 `/plugins/{name}/{rest:path}` → `views/index.html`（SPA 支持）
- **静态文件**（仅 has_menu 插件）：`/plugins/{name}/static` → `views/` 目录（CSS/JS/图片等）

## 八、Hook 机制

### 8.1 触发时机

| Hook | 触发点 | 参数 | 用途 |
|------|--------|------|------|
| `on_request` | proxy 转发请求之前 | `request: Request` | 请求日志、参数校验、请求改写（含模型名映射） |
| `on_key_selected` | 根据 model 匹配到 key 之后 | `model, key, request` | 模型匹配插件修改 key 选择结果 |
| `on_upstream_error` | 上游返回错误（非 2xx） | `request, response, key` | 错误处理插件决定是否重试、切换模型 |
| `on_response` | proxy 每次上游尝试结束后（成功/失败） | `request: dict, response: dict` | 响应日志、并发回收、告警通知 |

### 8.2 约定

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

### 8.3 执行顺序与状态传递

同一 hook 的多个插件按 `priority` 从小到大依次执行（越小越优先），形成**管道链**：

```
request → [plugin A (priority=10)] → [plugin B (priority=50)] → [plugin C (priority=100)] → 下一环节
              ↓ 可改写 request              ↓ 基于 A 的输出继续处理       ↓ 最终处理
```

每个 hook 的返回值作为下一个同类型 hook 的输入：

| Hook | 输入 | 返回值 | 管道传递 |
|------|------|--------|---------|
| `on_request` | `request` | `request`（可修改后返回） | 前一个返回的 request → 下一个的输入 |
| `on_key_selected` | `(model, key, request)` | `key`（可替换后返回） | 前一个返回的 key → 下一个的输入 |
| `on_upstream_error` | `(request, response, key)` | `"retry"` / `"switch"` / `None` | 第一个非 None 返回值即为最终决策 |
| `on_response` | `(request, response)` | `None`（无状态传递） | 纯粹观察，按优先级依次执行 |

`on_response` 当前由 proxy 传入的 `response` 为结构化元信息（并非 FastAPI Response 对象），常用字段如下：

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | bool | 本次上游尝试是否成功 |
| `phase` | string | 阶段：`select_key` / `request` / `upstream` / `read_stream` / `converter` / `exhausted` |
| `status_code` | int | 上游 HTTP 状态码，网络错误时为 `0` |
| `key_alias` | string | 本次尝试使用的 key 别名 |
| `provider` | string | key 对应 provider |
| `model` | string | 本次请求模型 |
| `latency_ms` | int | 本次尝试耗时毫秒 |
| `error` | string | 错误信息（成功时为空） |
| `error_type` | string | 错误类型（如 `timeout` / `connect` / `http` / `chunk`） |
| `attempt` | int | 当前 key 内部重试序号 |
| `action` | string | 错误策略决策（`retry` / `switch` / `block`） |
| `api_path` | string | 客户端请求路径（如 `chat/completions`） |
| `upstream_api_path` | string | 转换后的上游路径（如 `messages`） |
| `stream` | bool | 是否流式（仅成功场景提供） |
| `response_body` | string | 非流式成功响应正文（仅允许需要安全处理的插件读取/改写） |

对于 `on_response`，当前实现除“纯观察”外，也允许插件返回新的 `response` 元信息字典，用于最小范围内改写非流式响应结果。典型场景包括后处理标注、补充审计字段或做协议相关的二次整理；若插件需要附加安全/诊断信息，也可以通过 `x-akm-security` 与 `x-akm-flags` 写入审计头，供日志页展示和后续事件分析。

> 未注册对应 hook 的插件不参与该管道。同一个插件可注册多个 hook。

**崩溃隔离**：每个 hook 被 `try/except` 包裹，单个插件抛异常时跳过该插件（保留其输入原样传给下一个），不中断管道也不影响主链路。异常记录到日志。

### 8.4 matcher 并发/慢 key 旁路策略（model_matcher）

`model_matcher` 内置了一个默认关闭的保守策略，用于缓解单 key 拥塞：

- `enable_inflight_bypass=false`（默认）时，保持原有选 key 行为不变。
- 开启后，在 `on_key_selected` 阶段检查当前 key 的 in-flight 状态：
  - 当并发数 `>= max_inflight_per_key`（默认 `3`）时触发旁路；
  - 或最老 in-flight 请求时长 `>= slow_inflight_threshold_sec`（默认 `8` 秒）时触发旁路。
- 触发后尝试改选其他可用 key；若无可替代 key，则继续使用当前 key（不硬失败）。
- in-flight 计数在 `on_key_selected` 增加，在 `on_response` 按 `response.key_alias` 回收，形成闭环。

该策略的设计目标是“只在明显拥塞时轻量旁路”，避免对现有流量分配造成激进扰动。

## 九、与 server.py 集成

### 9.1 改动点

```python
# server.py

from .plugins.plugin_manager import PluginManager

# lifespan 中：
plugin_manager = PluginManager()
plugin_manager.load_all(app, db)  # db 传入共享数据库连接
app.state.plugin_manager = plugin_manager

# 新增插件管理 API
@app.post("/api/plugins/upload")
async def upload_plugin(file: UploadFile, request: Request):
    """上传 .zip 插件包，服务端自动解压到 ~/.akm/plugins/"""
    pm = request.app.state.plugin_manager
    return await pm.install_plugin(file)

@app.get("/api/plugins")
async def list_plugins(request: Request):
    """返回已加载插件列表（含启用/禁用状态）"""
    return request.app.state.plugin_manager.get_plugin_list()

@app.post("/api/plugins/{name}/enable")
@app.post("/api/plugins/{name}/disable")
async def toggle_plugin(name: str, request: Request):
    """启用/禁用插件（required 插件不可禁用），需重启生效"""
    return request.app.state.plugin_manager.toggle(name, enable=...)

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

# AI 请求端点注入 hook（管道模式：状态逐插件传递）
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    pm = request.app.state.plugin_manager
    request = await pm.run_hook("on_request", request=request)  # ← 前一个改写后的 request 传给下一个
    result = await _handle_ai_request(request, "chat/completions")
    await pm.run_hook("on_response", request=request, response=result)
    return result

@app.post("/v1/responses")
async def responses(request: Request):
    pm = request.app.state.plugin_manager
    request = await pm.run_hook("on_request", request=request)
    result = await _handle_ai_request(request, "responses")
    await pm.run_hook("on_response", request=request, response=result)
    return result

# run_hook 内部实现（管道）
async def run_hook(self, hook: str, **kwargs):
    """按 priority 从小到大依次执行，前一个返回值传给下一个"""
    plugins = sorted(
        [p for p in self.plugins.values() if p.enabled and p.meta.hooks.get(hook)],
        key=lambda p: p.meta.priority
    )
    result = kwargs
    for plugin in plugins:
        try:
            ret = await getattr(plugin, hook)(**result)
            if ret is not None:
                # 将返回值合并到 kwargs，传给下一个插件
                result = {**result, **self._unwrap_hook_result(hook, ret)}
        except Exception as e:
            self.logger.error(f"[{plugin.meta.name}] hook {hook} 异常: {e}")
    return result
```

### 9.2 前端集成

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

## 十、插件开发示例

### 10.1 有菜单插件：模型映射（操作数据库）

补充说明：当前内置 `model_matcher` 采用配置项 `aliases` 做轻量映射，格式为 `old=new` 的逗号分隔串，仅支持显式映射。实现时应仅在请求命中显式别名时改写 `model`，未命中时保留原始模型名继续参与后续 key 匹配。

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

### 10.2 无菜单插件：请求日志（hook + 配置）

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

## 十一、安全考虑

- 插件代码在 akm 进程中运行，拥有完整权限（包括数据库），仅应由信任的开发者编写
- `plugin.json` 中不包含可执行代码
- `PluginBase` 中的数据库访问为共享连接，插件需自行管理事务和锁
- 插件加载失败时打印警告但不阻止 akm 启动
- hook 执行异常被捕获，不影响请求正常流程
