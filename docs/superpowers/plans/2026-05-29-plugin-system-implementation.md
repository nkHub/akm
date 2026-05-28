# 插件系统实现计划（分阶段）

> 状态：执行中 | 基于设计文档 [plugin-system.md](../design/plugin-system.md)

**Goal:** 分阶段实现插件系统，先建基础设施+管理界面，再逐个迁移现有功能到插件。
**三个协议转换器合并为一个插件。**

**Architecture:** PluginBase 基类 + PluginManager 核心 → 管理 API → 管理 UI → 逐个迁移插件 → 核心重构去适配器化

---

## 阶段一：插件基础设施 + 管理界面

### Task 1: PluginMeta 模型 + PluginBase 基类

**Files:**
- Create: `akm/plugins/__init__.py`
- Create: `akm/plugins/models.py`
- Create: `akm/plugins/base.py`

- [x] **Step 1.1: 创建 `akm/plugins/models.py` — PluginMeta 数据模型**

```python
"""插件元数据模型"""
from pydantic import BaseModel


class SettingDef(BaseModel):
    """单个配置项定义"""
    key: str
    label: str
    type: str = "text"          # number / boolean / select / text
    default: str | int | bool = ""
    description: str = ""
    options: list[str] = []      # select 类型时的选项列表
    min: int | None = None
    max: int | None = None


class PluginMeta(BaseModel):
    """插件的 plugin.json 映射"""
    name: str
    version: str
    has_menu: bool = False
    category: str = ""           # filter / matcher / converter / handler / post / app
    description: str = ""
    builtin: bool = False        # 内置插件标记
    required: bool = False       # 不可禁用
    priority: int = 100          # 同 hook 执行优先级，越小越先，0-999
    menu: dict = {}
    routes_prefix: str = ""
    hooks: dict = {
        "on_request": False,
        "on_key_selected": False,
        "on_upstream_error": False,
        "on_response": False
    }
    settings: list[SettingDef] = []
    converts: dict | None = None  # { "from": "responses", "to": "chat" }
```

- [x] **Step 1.2: 创建 `akm/plugins/base.py` — PluginBase 基类**

```python
"""插件基类 — 提供上下文注入、生命周期、hook 方法"""
import logging
from pathlib import Path

from fastapi import APIRouter


class PluginBase:
    """插件基类，所有插件必须继承此类"""

    name: str = ""               # 由 PluginManager 注入
    builtin: bool = False        # 由 PluginManager 注入
    enabled: bool = True         # 由 PluginManager 注入

    # — 上下文注入（由 PluginManager 调用） —
    app = None                   # FastAPI 实例
    db = None                    # 共享 SQLite 连接
    config: dict = {}            # ~/.akm/config.json 中该插件配置
    logger: logging.Logger = None

    # — 子类覆盖 —
    router = None                # APIRouter（可选）
    meta: "PluginMeta" = None    # 由 PluginManager 注入

    # — 静态资源路径 —
    _static_dir: Path = Path(".")

    # ── 生命周期 ──

    async def on_load(self):
        """插件加载回调（路由注册后调用），可在此建表、初始化资源"""
        pass

    async def on_unload(self):
        """插件卸载回调（应用关闭前调用），可在此清理资源"""
        pass

    # ── Hook 方法（子类按需重写） ──

    async def on_request(self, request) -> dict | None:
        """请求到达回调。返回 dict 可改写请求数据（如注入参数/模型名映射）"""
        pass

    async def on_key_selected(self, model: str, key: dict, request) -> dict | None:
        """Key 匹配后回调。返回 dict 可替换 key"""
        pass

    async def on_upstream_error(self, request, response, key) -> str | None:
        """上游错误回调。返回 "retry" / "switch" / None"""
        pass

    async def on_response(self, request, response) -> None:
        """响应返回回调（纯观察，无状态传递）"""
        pass

    # ── 转换方法（converter 类插件重写） ──

    def convert_request(self, body: dict) -> dict:
        """请求体格式转换"""
        return body

    def convert_response(self, body: str) -> str:
        """非流式响应转换"""
        return body

    async def convert_sse_stream(self, upstream_stream):
        """流式 SSE 转换（异步生成器）"""
        async for chunk in upstream_stream:
            yield chunk
```

- [x] **Step 1.3: 创建 `akm/plugins/__init__.py`**

```python
"""AKM 插件系统"""
from .base import PluginBase
from .models import PluginMeta, SettingDef
```

- [ ] **Step 1.4: 验证导入**

```bash
cd /Users/nk/Desktop/ccs && python -c "from akm.plugins import PluginBase, PluginMeta, SettingDef; print('OK')"
```

---

### Task 2: PluginManager 核心

**Files:**
- Create: `akm/plugins/plugin_manager.py`

- [ ] **Step 2.1: 创建 `akm/plugins/plugin_manager.py` — 文件头与导入**

```python
"""插件管理器 — 扫描、加载、生命周期管理、配置读写、Hook 管道执行"""
import json
import logging
import zipfile
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from .models import PluginMeta, SettingDef
from .base import PluginBase

logger = logging.getLogger("akm.plugin_manager")
```

- [ ] **Step 2.2: 实现 `PluginManager.__init__` 和配置路径**

```python
class PluginManager:
    """插件管理器"""

    def __init__(self, app: FastAPI = None, db=None):
        self.app = app
        self.db = db
        self.plugins: dict[str, PluginBase] = {}           # name → PluginBase 实例
        self._plugin_metas: dict[str, PluginMeta] = {}     # name → PluginMeta
        self._plugin_sources: dict[str, str] = {}          # name → "builtin" / "third_party"
        self._builtin_dir = Path(__file__).resolve().parent
        # 内置插件目录: akm/plugins/ 下的子目录（排除 __pycache__, base.py 等）
        self._third_party_dir = Path.home() / ".akm" / "plugins"
        self._config_path = Path.home() / ".akm" / "config.json"
```

- [ ] **Step 2.3: 实现 `_load_config_json` — 读取配置状态**

```python
    def _load_config_json(self) -> dict:
        """读取 ~/.akm/config.json"""
        if not self._config_path.exists():
            return {}
        try:
            return json.loads(self._config_path.read_text("utf-8"))
        except Exception:
            return {}
```

- [ ] **Step 2.4: 实现 `_save_config_json` — 写入配置状态**

```python
    def _save_config_json(self, data: dict):
        """写入 ~/.akm/config.json"""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
```

- [ ] **Step 2.5: 实现 `_load_plugin` — 扫描单个插件目录**

```python
    def _load_plugin(self, plugin_dir: Path, source: str) -> Optional[PluginBase]:
        """从目录加载单个插件

        Args:
            plugin_dir: 插件目录（如 akm/plugins/responses_converter/）
            source: "builtin" 或 "third_party"

        Returns:
            加载成功返回 PluginBase 实例，失败返回 None
        """
        json_path = plugin_dir / "plugin.json"
        py_path = plugin_dir / "index.py"

        if not json_path.exists() or not py_path.exists():
            return None

        # ── 解析 plugin.json ──
        try:
            meta = PluginMeta.model_validate_json(json_path.read_text("utf-8"))
        except Exception as e:
            logger.warning(f"[PluginManager] 解析 plugin.json 失败: {plugin_dir} — {e}")
            return None

        name = meta.name

        # ── 重名检测：全局唯一 ──
        if name in self.plugins:
            logger.info(f"[PluginManager] 插件名冲突，跳过第三方: {name} (已有同名插件)")
            return None

        # ── 动态导入 index.py ──
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            f"plugin_{name}", str(py_path)
        )
        if spec is None or spec.loader is None:
            logger.warning(f"[PluginManager] 无法加载模块: {name}")
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules[f"akm_plugin_{name}"] = module
        spec.loader.exec_module(module)

        PluginClass = getattr(module, "Plugin", None)
        if PluginClass is None or not issubclass(PluginClass, PluginBase):
            logger.warning(f"[PluginManager] {name}: 未找到继承 PluginBase 的 Plugin 类")
            return None

        # ── 实例化并注入上下文 ──
        plugin: PluginBase = PluginClass()
        plugin.name = name
        plugin.builtin = meta.builtin
        plugin.meta = meta
        plugin.logger = logging.getLogger(f"akm.plugins.{name}")
        plugin._static_dir = plugin_dir / "views"

        if self.app is not None:
            plugin.app = self.app
        if self.db is not None:
            plugin.db = self.db

        # ── 注册路由 ──
        if plugin.router is not None:
            routes_prefix = meta.routes_prefix or f"/{name}"
            self.app.include_router(plugin.router, prefix=routes_prefix)
            logger.info(f"[PluginManager] 注册路由: {routes_prefix}")

        # ── 注册静态文件 + 前端路由（has_menu） ──
        if meta.has_menu and plugin._static_dir.exists():
            static_path = f"/plugins/{name}/static"
            self.app.mount(
                static_path,
                StaticFiles(directory=str(plugin._static_dir)),
                name=f"plugin_static_{name}",
            )

        # ── 读取启停状态 ──
        cfg = self._load_config_json()
        plugin_states = cfg.get("plugin_states", {})
        if name in plugin_states:
            plugin.enabled = plugin_states[name]

        self.plugins[name] = plugin
        self._plugin_metas[name] = meta
        self._plugin_sources[name] = source

        logger.info(
            f"[PluginManager] 加载插件: {name} v{meta.version} "
            f"(来源: {source}, 分类: {meta.category}, {'启用' if plugin.enabled else '已禁用'})"
        )
        return plugin
```

- [ ] **Step 2.6: 实现 `load_all` — 扫描全部插件**

```python
    async def load_all(self, app: FastAPI, db=None):
        """启动时扫描并加载所有插件

        加载顺序：内置先加载，第三方后加载（重名跳过第三方）
        """
        self.app = app
        self.db = db

        # ── 1. 加载内置插件 (akm/plugins/ 子目录) ──
        if self._builtin_dir.exists():
            for entry in sorted(self._builtin_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if entry.name.startswith("__"):
                    continue
                self._load_plugin(entry, "builtin")

        # ── 2. 加载第三方插件 (~/.akm/plugins/ 子目录) ──
        self._third_party_dir.mkdir(parents=True, exist_ok=True)
        if self._third_party_dir.exists():
            for entry in sorted(self._third_party_dir.iterdir()):
                if not entry.is_dir():
                    continue
                self._load_plugin(entry, "third_party")

        # ── 3. 首次加载时自动启用所有内置插件 ──
        cfg = self._load_config_json()
        plugin_states = cfg.get("plugin_states", {})
        for name, plugin in self.plugins.items():
            if name not in plugin_states and plugin.builtin:
                plugin.enabled = True
                plugin_states[name] = True
                logger.info(f"[PluginManager] 首次加载，自动启用内置插件: {name}")

        cfg["plugin_states"] = plugin_states
        self._save_config_json(cfg)

        # ── 4. 调用 on_load 生命周期 ──
        for plugin in self.plugins.values():
            if plugin.enabled:
                try:
                    await plugin.on_load()
                except Exception as e:
                    logger.error(f"[PluginManager] {plugin.name} on_load 异常: {e}")

        logger.info(f"[PluginManager] 共加载 {len(self.plugins)} 个插件")
```

- [ ] **Step 2.7: 实现 `run_hook` — 管道执行**

```python
    async def run_hook(self, hook: str, **kwargs):
        """管道执行 hook：按 priority 从小到大，前一个返回值传给下一个

        Args:
            hook: hook 名称（on_request / on_key_selected / on_upstream_error / on_response）
            **kwargs: 传递给 hook 的关键字参数

        Returns:
            管道末端的状态（对 on_upstream_error 返回第一个非 None 的 action）
        """
        # 筛选注册了该 hook 的已启用插件
        candidates = [
            p for p in self.plugins.values()
            if p.enabled and p.meta.hooks.get(hook)
        ]
        # 按 priority 升序
        candidates.sort(key=lambda p: p.meta.priority)

        current = kwargs
        action = None  # 仅 on_upstream_error 使用

        for plugin in candidates:
            try:
                ret = await getattr(plugin, hook)(**current)

                if hook == "on_upstream_error":
                    # on_upstream_error: 第一个非 None 即为最终决策
                    if ret is not None and action is None:
                        action = ret
                elif hook == "on_key_selected" and ret is not None:
                    # on_key_selected: 返回的 key 替换当前 key
                    current["key"] = ret
                elif hook == "on_request" and ret is not None:
                    # on_request: 返回的 request 替换当前 request
                    current["request"] = ret
                # on_response: 无返回值，纯观察

            except Exception as e:
                logger.error(f"[PluginManager] {plugin.name}.{hook} 异常: {e}")
                continue

        if hook == "on_upstream_error":
            return action
        return current
```

- [ ] **Step 2.8: 实现 `get_converter`**

```python
    def get_converter(self, from_format: str, to_format: str) -> Optional[PluginBase]:
        """根据转换声明查找启用的转换插件"""
        for plugin in self.plugins.values():
            if not plugin.enabled:
                continue
            c = plugin.meta.converts
            if c and c.get("from") == from_format and c.get("to") == to_format:
                return plugin
        return None
```

- [ ] **Step 2.9: 实现 `install_plugin` — zip 上传安装**

```python
    async def install_plugin(self, file: UploadFile) -> dict:
        """上传 .zip 插件包，解压到 ~/.akm/plugins/"""
        if not file.filename or not file.filename.endswith(".zip"):
            return {"ok": False, "error": "仅支持 .zip 格式"}

        # 解压到临时目录，读取 plugin.json 获取 name
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            content = await file.read()
            zippath = tmp / "plugin.zip"
            zippath.write_bytes(content)

            with zipfile.ZipFile(zippath, "r") as zf:
                zf.extractall(tmp)

            # 查找 plugin.json
            json_candidates = list(tmp.rglob("plugin.json"))
            if not json_candidates:
                return {"ok": False, "error": "zip 包中未找到 plugin.json"}

            meta_path = json_candidates[0]
            plugin_root = meta_path.parent

            try:
                meta = PluginMeta.model_validate_json(meta_path.read_text("utf-8"))
            except Exception as e:
                return {"ok": False, "error": f"plugin.json 格式错误: {e}"}

            name = meta.name

            # ── 重名检测 ──
            dest = self._third_party_dir / name
            if dest.exists():
                return {"ok": False, "error": f"插件 '{name}' 已存在"}
            if any(
                d.name == name for d in self._builtin_dir.iterdir()
                if d.is_dir() and not d.name.startswith("__")
            ):
                return {"ok": False, "error": f"插件 '{name}' 与内置插件重名，无法安装"}

            # ── 验证 index.py 存在 ──
            if not (plugin_root / "index.py").exists():
                return {"ok": False, "error": "zip 包中未找到 index.py"}

            # ── 复制到 ~/.akm/plugins/{name}/ ──
            shutil.copytree(plugin_root, dest)

        return {"ok": True, "name": name, "message": f"已安装到 ~/.akm/plugins/{name}/，重启 akm 后生效"}
```

- [ ] **Step 2.10: 实现 `delete_plugin` — 删除第三方插件**

```python
    def delete_plugin(self, name: str) -> dict:
        """删除 ~/.akm/plugins/{name}/ 目录（仅第三方）"""
        if name not in self._plugin_sources:
            return {"ok": False, "error": "插件不存在"}

        source = self._plugin_sources[name]
        if source == "builtin":
            return {"ok": False, "error": "内置插件不可删除"}

        dest = self._third_party_dir / name
        if dest.exists():
            shutil.rmtree(dest)

        # 清除插件状态
        self.plugins.pop(name, None)
        self._plugin_metas.pop(name, None)
        self._plugin_sources.pop(name, None)

        cfg = self._load_config_json()
        plugin_states = cfg.get("plugin_states", {})
        plugin_states.pop(name, None)
        cfg["plugin_states"] = plugin_states
        self._save_config_json(cfg)

        return {"ok": True, "message": f"已删除 {name}"}
```

- [ ] **Step 2.11: 实现 `toggle_plugin` — 启用/禁用**

```python
    def toggle_plugin(self, name: str, enable: bool) -> dict:
        """切换插件启用/禁用状态，写入 config.json"""
        if name not in self.plugins:
            return {"ok": False, "error": "插件不存在"}

        plugin = self.plugins[name]
        if not enable and plugin.meta.required:
            return {"ok": False, "error": f"插件 '{name}' 是必需的，不可禁用"}

        plugin.enabled = enable
        cfg = self._load_config_json()
        plugin_states = cfg.get("plugin_states", {})
        plugin_states[name] = enable
        cfg["plugin_states"] = plugin_states
        self._save_config_json(cfg)

        return {
            "ok": True,
            "name": name,
            "enabled": enable,
            "message": "状态已保存，重启 akm 后生效",
        }
```

- [ ] **Step 2.12: 实现 `get_config` / `set_config`**

```python
    def get_config(self, name: str) -> dict | None:
        """读取插件配置（合并默认值）"""
        if name not in self._plugin_metas:
            return None
        meta = self._plugin_metas[name]
        defaults = {}
        for s in meta.settings:
            defaults[s.key] = s.default
        cfg = self._load_config_json()
        plugin_configs = cfg.get("plugin_configs", {})
        return {**defaults, **plugin_configs.get(name, {})}

    def set_config(self, name: str, data: dict) -> dict:
        """保存插件配置"""
        if name not in self._plugin_metas:
            return {"ok": False, "error": "插件不存在"}
        cfg = self._load_config_json()
        plugin_configs = cfg.get("plugin_configs", {})
        plugin_configs[name] = data
        cfg["plugin_configs"] = plugin_configs
        self._save_config_json(cfg)
        return {"ok": True}
```

- [ ] **Step 2.13: 实现 `get_plugin_list` — 管理界面用**

```python
    def get_plugin_list(self) -> list:
        """返回全部插件信息（供管理界面）"""
        cfg = self._load_config_json()
        plugin_states = cfg.get("plugin_states", {})

        result = []
        for name, plugin in self.plugins.items():
            meta = plugin.meta
            result.append({
                "name": name,
                "version": meta.version,
                "category": meta.category,
                "description": meta.description,
                "has_menu": meta.has_menu,
                "builtin": plugin.builtin,
                "required": meta.required,
                "priority": meta.priority,
                "enabled": plugin.enabled,
                "source": self._plugin_sources.get(name, "unknown"),
                "hooks": meta.hooks,
                "settings": [s.model_dump() for s in meta.settings],
                "converts": meta.converts,
            })
        return result

    def get_menu(self) -> list:
        """返回已启用的有菜单插件信息（供侧边栏）"""
        items = []
        for plugin in self.plugins.values():
            if plugin.enabled and plugin.meta.has_menu:
                items.append({
                    "name": plugin.meta.name,
                    "title": plugin.meta.menu.get("title", plugin.meta.name),
                    "icon": plugin.meta.menu.get("icon", "plugin"),
                    "order": plugin.meta.menu.get("order", 100),
                    "route": f"/plugins/{plugin.meta.name}",
                })
        items.sort(key=lambda x: x["order"])
        return items

    def get_plugin_metas(self) -> list:
        """返回所有插件元数据（含 settings schema，供设置页表单渲染）"""
        return [self._plugin_metas[name].model_dump() for name in self.plugins]
```

- [ ] **Step 2.14: 验证 PluginManager 基本结构**

```bash
cd /Users/nk/Desktop/ccs && python -c "
from akm.plugins.plugin_manager import PluginManager
pm = PluginManager()
print('PluginManager OK:', type(pm).__name__)
"
```

---

### Task 3: 插件管理 API 集成到 server.py

**Files:**
- Modify: `akm/server.py`（新增 7 个 API 路由 + lifespan 集成）

- [ ] **Step 3.1: 在 server.py 添加导入**

在 `akm/server.py` 顶部现有导入之后添加：

```python
from .plugins.plugin_manager import PluginManager
```

- [ ] **Step 3.2: 在 lifespan 中初始化 PluginManager**

在 `lifespan` 函数中，`load_custom_agents(app)` 之后（约第 43 行）添加：

```python
    # ── 初始化插件管理器 ──
    plugin_manager = PluginManager()
    await plugin_manager.load_all(app, db)
    app.state.plugin_manager = plugin_manager
```

- [ ] **Step 3.3: 添加插件管理 API 路由**

在 server.py 的 Key 管理 API 附近（约第 160 行之前）添加：

```python
# ── 插件管理 API ─────────────────────────────────────────────

@app.get("/api/plugins")
async def list_plugins(request: Request):
    """返回插件列表（含启用/禁用状态）"""
    pm = request.app.state.plugin_manager
    return pm.get_plugin_list()


@app.post("/api/plugins/upload")
async def upload_plugin(file: UploadFile, request: Request):
    """上传 .zip 插件包，解压到 ~/.akm/plugins/"""
    pm = request.app.state.plugin_manager
    result = await pm.install_plugin(file)
    if result.get("ok"):
        return result
    return JSONResponse(result, status_code=400)


@app.post("/api/plugins/{name}/enable")
async def enable_plugin(name: str, request: Request):
    """启用插件"""
    pm = request.app.state.plugin_manager
    result = pm.toggle_plugin(name, True)
    if result.get("ok"):
        return result
    return JSONResponse(result, status_code=400)


@app.post("/api/plugins/{name}/disable")
async def disable_plugin(name: str, request: Request):
    """禁用插件"""
    pm = request.app.state.plugin_manager
    result = pm.toggle_plugin(name, False)
    if result.get("ok"):
        return result
    return JSONResponse(result, status_code=400)


@app.delete("/api/plugins/{name}")
async def delete_plugin(name: str, request: Request):
    """删除第三方插件"""
    pm = request.app.state.plugin_manager
    result = pm.delete_plugin(name)
    if result.get("ok"):
        return result
    return JSONResponse(result, status_code=400)


@app.get("/api/plugin-menu")
async def plugin_menu(request: Request):
    """插件菜单（供侧边栏动态注入）"""
    return request.app.state.plugin_manager.get_menu()


@app.get("/api/plugin-config/{name}")
async def plugin_get_config(name: str, request: Request):
    """读取插件配置"""
    pm = request.app.state.plugin_manager
    cfg = pm.get_config(name)
    if cfg is None:
        return JSONResponse({"error": "插件不存在"}, status_code=404)
    return cfg


@app.post("/api/plugin-config/{name}")
async def plugin_save_config(name: str, request: Request):
    """保存插件配置"""
    pm = request.app.state.plugin_manager
    body = await request.json()
    return pm.set_config(name, body)
```

- [ ] **Step 3.4: 验证 API 可用**

```bash
# 启动 akm 后测试
curl http://localhost:8800/api/plugins
# 预期: [] (暂时没有加载任何插件)
```

---

### Task 4: 插件管理 UI 页面

**Files:**
- Create: `akm/templates/plugins.html`
- Modify: `akm/templates/_sidebar.html`（新增导航项）
- Modify: `akm/server.py`（新增页面路由）

- [ ] **Step 4.1: 在 `_sidebar.html` 添加「插件管理」导航**

在 `_sidebar.html` 的 `<nav>` 中，`about` 链接之前添加：

```html
    <a href="/plugins" class="flex items-center gap-3 px-3 py-2 rounded text-sm {{ 'text-indigo-400 bg-indigo-400/10' if active == 'plugins' else 'text-gray-400 hover:bg-surface-hover hover:text-white transition-colors' }} cursor-pointer whitespace-nowrap overflow-hidden">
      <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 14v6m-3-3h6M6 10h2a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v2a2 2 0 002 2zm10 0h2a2 2 0 002-2V6a2 2 0 00-2-2h-2a2 2 0 00-2 2v2a2 2 0 002 2zM6 20h2a2 2 0 002-2v-2a2 2 0 00-2-2H6a2 2 0 00-2 2v2a2 2 0 002 2z"/></svg>
      <span class="sidebar-label">插件</span>
    </a>
```

- [ ] **Step 4.2: 在 `server.py` 添加 `/plugins` 页面路由**

```python
@app.get("/plugins")
async def plugins_page(request: Request):
    """插件管理页面"""
    return HTMLResponse(_render_template("plugins.html", title="插件管理", active="plugins"))
```

- [ ] **Step 4.3: 创建 `akm/templates/plugins.html`**

```html
{% extends "_layout.html" %}
{% block content %}
<div class="space-y-6">

  <!-- 提示 -->
  <div class="bg-amber-400/10 border border-amber-400/30 rounded-lg p-3 flex items-center gap-2">
    <svg class="w-4 h-4 text-amber-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z"/></svg>
    <p class="text-xs text-amber-300">插件启用/禁用/安装后，需<b>手动重启 akm 服务</b>才能生效。</p>
  </div>

  <!-- 上传区域 -->
  <section>
    <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">安装插件</h3>
    <div class="bg-surface-light border border-border rounded-lg p-4">
      <div class="flex items-center gap-4">
        <label class="bg-indigo-600 hover:bg-indigo-500 text-white text-xs px-4 py-2 rounded cursor-pointer transition-colors">
          选择 .zip 文件
          <input type="file" id="plugin-file" accept=".zip" class="hidden" onchange="uploadPlugin()">
        </label>
        <span id="upload-status" class="text-xs text-gray-400">支持 .zip 格式，自动解压到 ~/.akm/plugins/</span>
      </div>
    </div>
  </section>

  <!-- 插件列表 -->
  <section>
    <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">已加载插件 (<span id="plugin-count">0</span>)</h3>
    <div id="plugin-list" class="space-y-2">
      <div class="text-xs text-gray-500">加载中...</div>
    </div>
  </section>

</div>
{% endblock %}

{% block extra_js %}
<script>
function esc(s) { if(!s)return''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

var _plugins = [];

function loadPlugins() {
  fetch('/api/plugins').then(function(r){return r.json()}).then(function(data){
    _plugins = data;
    document.getElementById('plugin-count').textContent = data.length;
    renderPluginList(data);
  }).catch(function(e){
    document.getElementById('plugin-list').innerHTML = '<div class="text-xs text-red-400">加载失败: ' + esc(e.message) + '</div>';
  });
}

var CATEGORY_LABELS = {
  filter: '请求处理', matcher: '模型匹配', converter: '格式转换',
  handler: '错误处理', post: '响应处理', app: '应用插件'
};

function renderPluginList(plugins) {
  var html = '';
  if (plugins.length === 0) {
    html = '<div class="text-gray-600 text-sm">暂无插件</div>';
  } else {
    plugins.forEach(function(p) {
      var cat = CATEGORY_LABELS[p.category] || p.category || '未分类';
      var sourceBadge = p.builtin
        ? '<span class="text-xs px-1.5 py-0.5 rounded bg-indigo-400/10 text-indigo-300">内置</span>'
        : '<span class="text-xs px-1.5 py-0.5 rounded bg-emerald-400/10 text-emerald-300">第三方</span>';
      var requiredBadge = p.required
        ? '<span class="text-xs px-1.5 py-0.5 rounded bg-amber-400/10 text-amber-300">必需</span>'
        : '';

      html += '<div class="bg-surface-light border border-border rounded-lg p-4 flex items-center justify-between">';
      html += '<div class="flex-1 min-w-0">';
      html += '<div class="flex items-center gap-2 mb-1">';
      html += '<h4 class="text-sm font-semibold text-white">' + esc(p.name) + '</h4>';
      html += sourceBadge + requiredBadge;
      html += '<span class="text-xs text-gray-500">v' + esc(p.version) + '</span>';
      html += '<span class="text-xs px-1.5 py-0.5 rounded bg-surface border border-border text-gray-400">' + esc(cat) + '</span>';
      html += '</div>';
      html += '<p class="text-xs text-gray-500">' + esc(p.description) + '</p>';

      // hooks
      var hooks = [];
      if (p.hooks.on_request) hooks.push('on_request');
      if (p.hooks.on_key_selected) hooks.push('on_key_selected');
      if (p.hooks.on_upstream_error) hooks.push('on_upstream_error');
      if (p.hooks.on_response) hooks.push('on_response');
      if (hooks.length) {
        html += '<p class="text-xs text-gray-600 mt-1">Hooks: ' + hooks.join(', ') + '</p>';
      }
      html += '</div>';

      // 开关 + 删除按钮
      html += '<div class="flex items-center gap-3 shrink-0 ml-4">';
      if (p.required) {
        html += '<span class="text-xs text-gray-500">系统必需</span>';
      } else {
        html += '<label class="flex items-center cursor-pointer select-none switch-' + (p.enabled ? 'on' : 'off') + '" onclick="togglePlugin(\'' + esc(p.name) + '\', ' + p.enabled + ')">';
        html += '<div class="switch-track ' + (p.enabled ? 'bg-indigo-600' : 'bg-gray-600') + ' flex items-center"><div class="switch-thumb bg-white"></div></div>';
        html += '</label>';
      }
      if (!p.builtin) {
        html += '<button onclick="deletePlugin(\'' + esc(p.name) + '\')" class="text-red-400 hover:text-red-300 text-xs cursor-pointer">删除</button>';
      }
      html += '</div>';

      html += '</div>';
    });
  }
  document.getElementById('plugin-list').innerHTML = html;
}

function togglePlugin(name, currentEnabled) {
  var endpoint = currentEnabled ? '/api/plugins/' + name + '/disable' : '/api/plugins/' + name + '/enable';
  fetch(endpoint, {method: 'POST'}).then(function(r){return r.json()}).then(function(data){
    if (data.ok) {
      loadPlugins();
    } else {
      alert(data.error || '操作失败');
    }
  }).catch(function(e){
    alert('网络错误: ' + e.message);
  });
}

function deletePlugin(name) {
  if (!confirm('确认删除插件 "' + name + '"？此操作不可撤销。')) return;
  fetch('/api/plugins/' + encodeURIComponent(name), {method: 'DELETE'}).then(function(r){return r.json()}).then(function(data){
    if (data.ok) {
      loadPlugins();
    } else {
      alert(data.error || '删除失败');
    }
  }).catch(function(e){
    alert('网络错误: ' + e.message);
  });
}

function uploadPlugin() {
  var fileInput = document.getElementById('plugin-file');
  var status = document.getElementById('upload-status');
  var file = fileInput.files[0];
  if (!file) return;

  status.textContent = '上传中...';
  status.className = 'text-xs text-gray-400';

  var formData = new FormData();
  formData.append('file', file);

  fetch('/api/plugins/upload', {method: 'POST', body: formData}).then(function(r){return r.json()}).then(function(data){
    if (data.ok) {
      status.textContent = data.message;
      status.className = 'text-xs text-emerald-400';
      fileInput.value = '';
    } else {
      status.textContent = data.error || '上传失败';
      status.className = 'text-xs text-red-400';
    }
  }).catch(function(e){
    status.textContent = '上传失败: ' + e.message;
    status.className = 'text-xs text-red-400';
  });
}

document.addEventListener('DOMContentLoaded', loadPlugins);
</script>
{% endblock %}
```

- [ ] **Step 4.4: 验证页面可访问**

```bash
# 启动 akm 后浏览器访问 http://localhost:8800/plugins
# 预期: 显示空插件列表 + 上传区域 + 顶部提示
```

---

## 阶段二：协议转换插件（合并三个适配器为一个插件）

### Task 5: 协议转换插件

**目标:** 将 `responses_adapter.py` + `messages_adapter.py` + `chat_adapter.py` 合并为单一插件 `protocol_converter`，删除 `akm/adapters/` 目录。

**Files:**
- Create: `akm/plugins/protocol_converter/plugin.json`
- Create: `akm/plugins/protocol_converter/index.py`
- Delete: `akm/adapters/` (全部文件)
- Modify: `akm/agent.py` (移除硬编码适配器)
- Modify: `akm/proxy.py` (通过 PluginManager 获取转换器)

*(详细步骤将在阶段二展开时补充)*

---

## 阶段三：模型匹配插件

### Task 6: model_matcher 插件

*(将在阶段二完成后展开)*

---

## 阶段四：错误处理插件

### Task 7: error_handler 插件

*(将在阶段二完成后展开)*

---

## 阶段五：核心重构

### Task 8: agent.py / proxy.py / key_pool.py 精简

*(将在前面阶段完成后展开)*
