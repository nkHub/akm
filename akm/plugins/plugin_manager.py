"""插件管理器 — 扫描、加载、生命周期管理、配置读写、Hook 管道执行"""
import json
import logging
import zipfile
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile
from fastapi.staticfiles import StaticFiles

from .models import PluginMeta
from .base import PluginBase

logger = logging.getLogger("akm.plugin_manager")


class PluginManager:
    """插件管理器

    职责：
    1. 启动时扫描 akm/plugins/（内置）和 ~/.akm/plugins/（第三方）
    2. 动态导入 index.py、注入上下文、注册路由和静态文件
    3. 按 priority 管道执行 hook，崩溃隔离
    4. 插件配置读写、启用/禁用、zip 安装、删除
    """

    def __init__(self):
        self.plugins: dict[str, PluginBase] = {}           # name → PluginBase 实例
        self._plugin_metas: dict[str, PluginMeta] = {}     # name → PluginMeta
        self._plugin_sources: dict[str, str] = {}          # name → "builtin" / "project" / "third_party"
        self._builtin_dir = Path(__file__).resolve().parent
        self._project_dir = Path(__file__).resolve().parent.parent.parent / "plugins"
        self._third_party_dir = Path.home() / ".akm" / "plugins"
        self._config_path = Path.home() / ".akm" / "config.json"
        self.app: Optional[FastAPI] = None
        self.db = None

    # ── 配置读写（内部） ──

    def _load_config_json(self) -> dict:
        """读取 ~/.akm/config.json"""
        if not self._config_path.exists():
            return {}
        try:
            return json.loads(self._config_path.read_text("utf-8"))
        except Exception:
            return {}

    def _save_config_json(self, data: dict):
        """写入 ~/.akm/config.json"""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), "utf-8"
        )

    # ── 插件加载 ──

    def _list_zip_builtin_plugins(self) -> set[str]:
        """在 py2app zip 包中列出内置插件目录名，返回插件名称集合"""
        import zipfile

        path_str = str(self._builtin_dir)
        zip_path = None
        for parent in Path(path_str).parents:
            if parent.suffix == '.zip' and parent.exists():
                zip_path = parent
                break

        if not zip_path:
            return set()

        try:
            inner_prefix = str(self._builtin_dir.relative_to(zip_path)) + '/'
        except ValueError:
            return set()

        plugin_names = set()
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for name in zf.namelist():
                if not name.startswith(inner_prefix):
                    continue
                relative = name[len(inner_prefix):]
                parts = relative.split('/')
                if len(parts) >= 1 and parts[0] and not parts[0].startswith('_'):
                    plugin_names.add(parts[0])

        return {n for n in plugin_names if n not in ("__pycache__",)}

    def _load_plugin_from_zip(self, plugin_name: str, source: str) -> None:
        """从 py2app zip 包中提取内置插件到临时目录并加载"""
        import tempfile

        path_str = str(self._builtin_dir)
        zip_path = None
        for parent in Path(path_str).parents:
            if parent.suffix == '.zip' and parent.exists():
                zip_path = parent
                break

        if not zip_path:
            logger.warning(f"[PluginManager] 无法找到 zip 文件，跳过内置插件: {plugin_name}")
            return

        try:
            inner_prefix = str(self._builtin_dir.relative_to(zip_path))
        except ValueError:
            return

        zip_plugin_dir = f"{inner_prefix}/{plugin_name}"

        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                if (f"{zip_plugin_dir}/plugin.json" not in zf.namelist()
                        or f"{zip_plugin_dir}/index.py" not in zf.namelist()):
                    return

                tmp_root = Path(tempfile.mkdtemp(prefix=f"akm_plugin_{plugin_name}_"))
                for z_info in zf.infolist():
                    if z_info.filename.startswith(f"{zip_plugin_dir}/"):
                        target = tmp_root / z_info.filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        zf.extract(z_info, tmp_root)

                plugin_dir = tmp_root / inner_prefix / plugin_name
                self._load_plugin(plugin_dir, source)
        except Exception as e:
            logger.warning(f"[PluginManager] 从 zip 加载插件失败 {plugin_name}: {e}")

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
            logger.info(
                f"[PluginManager] 插件名冲突，跳过第三方: {name} (已有同名插件)"
            )
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
            logger.warning(
                f"[PluginManager] {name}: 未找到继承 PluginBase 的 Plugin 类"
            )
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
            logger.info(f"[PluginManager] 挂载静态文件: {static_path}")

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
            f"(来源: {source}, 分类: {meta.category}, "
            f"{'启用' if plugin.enabled else '已禁用'})"
        )
        return plugin

    async def load_all(self, app: FastAPI, db=None):
        """启动时扫描并加载所有插件

        加载顺序：内置 → 项目本地 → 第三方（重名跳过）
        """
        self.app = app
        self.db = db

        # ── 1. 加载内置插件 (akm/plugins/ 子目录) ──
        # py2app 打包后 akm/plugins/ 在 python312.zip 内，iterdir() 会抛 NotADirectoryError
        try:
            builtin_entries = sorted(self._builtin_dir.iterdir())
            use_zip_loading = False
        except NotADirectoryError:
            builtin_entries = self._list_zip_builtin_plugins()
            use_zip_loading = True

        for entry in builtin_entries:
            if use_zip_loading:
                # 从 zip 包中提取插件到临时目录再加载
                self._load_plugin_from_zip(entry, "builtin")
            else:
                if not entry.is_dir():
                    continue
                if entry.name.startswith("__"):
                    continue
                if entry.name in ("base.py", "models.py", "plugin_manager.py", "__pycache__"):
                    continue
                self._load_plugin(entry, "builtin")

        # ── 2. 加载项目本地插件 (项目根目录 plugins/ 子目录) ──
        if self._project_dir.exists():
            for entry in sorted(self._project_dir.iterdir()):
                if not entry.is_dir():
                    continue
                self._load_plugin(entry, "project")

        # ── 3. 加载第三方插件 (~/.akm/plugins/ 子目录) ──
        self._third_party_dir.mkdir(parents=True, exist_ok=True)
        for entry in sorted(self._third_party_dir.iterdir()):
            if not entry.is_dir():
                continue
            self._load_plugin(entry, "third_party")

        # ── 3. 首次加载时按默认值初始化插件状态 ──
        cfg = self._load_config_json()
        plugin_states = cfg.get("plugin_states", {})
        plugin_configs = cfg.get("plugin_configs", {})
        changed = False
        for name, plugin in self.plugins.items():
            if name not in plugin_states:
                enabled = bool(plugin.meta.default_enabled)
                # data_filter_guard 的早期配置页同时提供了“启用过滤”设置和
                # 插件总开关。旧配置只保存前者时，插件会始终停在 Hook 之外，
                # 用户看到“已启用”却没有任何实际效果。仅对这个历史配置做
                # 一次兼容迁移，不改变其他插件的默认启停语义。
                if name == "data_filter_guard":
                    saved_config = plugin_configs.get(name, {})
                    if isinstance(saved_config, dict) and saved_config.get("enabled") is True:
                        enabled = True
                plugin.enabled = enabled
                plugin_states[name] = enabled
                changed = True
                logger.info(
                    f"[PluginManager] 首次加载，设置插件状态: {name} -> {'启用' if plugin.enabled else '禁用'}"
                )

        if changed:
            cfg["plugin_states"] = plugin_states
            self._save_config_json(cfg)

        # ── 4. 调用 on_load 生命周期 ──
        for plugin in self.plugins.values():
            if plugin.enabled:
                # 注入插件配置（从 config.json 读取，合并默认值）
                plugin.config = self.get_config(plugin.name) or {}
                try:
                    await plugin.on_load()
                except Exception as e:
                    logger.error(
                        f"[PluginManager] {plugin.name} on_load 异常: {e}"
                    )

        logger.info(f"[PluginManager] 共加载 {len(self.plugins)} 个插件")

    # ── Hook 管道执行 ──

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
                    # on_key_selected 默认返回替代 key。配额等策略插件还可
                    # 返回 skip_key，让 proxy 排除当前 key 后继续选择下一个。
                    # 立即停止该 Hook 管道，避免后续插件把已跳过的 key 计入
                    # in-flight 等运行时状态。
                    if isinstance(ret, dict) and ret.get("__akm_action__") == "skip_key":
                        current["on_key_selected_skip"] = ret
                        break
                    current["key"] = ret
                elif hook == "on_request" and ret is not None:
                    # on_request: 默认返回新的 request；
                    # 对少数需要“在转发前直接阻断请求”的插件，允许显式返回控制结构。
                    if isinstance(ret, dict) and ret.get("__akm_action__") == "block":
                        current["on_request_block"] = ret
                    else:
                        current["request"] = ret
                elif hook == "on_response" and ret is not None:
                    # on_response: 允许插件在结构化元信息基础上补充/改写响应数据。
                    # 这样像“数据安全插件”这类能力可以在不侵入 proxy 主流程的前提下，
                    # 对非流式正文做拦截或替换。
                    current["response"] = ret

            except Exception as e:
                logger.error(
                    f"[PluginManager] {plugin.name}.{hook} 异常: {e}"
                )
                continue

        if hook == "on_upstream_error":
            return action
        return current

    # ── 转换器查询 ──

    def get_converter(self, from_format: str, to_format: str) -> Optional[PluginBase]:
        """根据转换声明查找启用的转换插件"""
        for plugin in self.plugins.values():
            if not plugin.enabled:
                continue
            for c in plugin.meta.converts:
                if c.get("from") == from_format and c.get("to") == to_format:
                    return plugin
        return None

    # ── 插件安装 ──

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
                meta = PluginMeta.model_validate_json(
                    meta_path.read_text("utf-8")
                )
            except Exception as e:
                return {"ok": False, "error": f"plugin.json 格式错误: {e}"}

            name = meta.name

            # ── 重名检测 ──
            dest = self._third_party_dir / name
            if dest.exists():
                return {"ok": False, "error": f"插件 '{name}' 已存在"}

            # 检查是否与内置插件重名
            for entry in self._builtin_dir.iterdir():
                if entry.is_dir() and entry.name == name:
                    return {
                        "ok": False,
                        "error": f"插件 '{name}' 与内置插件重名，无法安装",
                    }

            # ── 验证 index.py 存在 ──
            if not (plugin_root / "index.py").exists():
                return {"ok": False, "error": "zip 包中未找到 index.py"}

            # ── 复制到 ~/.akm/plugins/{name}/ ──
            shutil.copytree(plugin_root, dest)

        # 首次安装：写入默认启停（与 load_all 一致）
        cfg = self._load_config_json()
        plugin_states = cfg.get("plugin_states", {})
        if name not in plugin_states:
            plugin_states[name] = bool(meta.default_enabled)
            cfg["plugin_states"] = plugin_states
            self._save_config_json(cfg)

        # 运行中则立即加载，无需重启
        if self.app is not None:
            plugin = self._load_plugin(dest, "third_party")
            if plugin is None:
                return {
                    "ok": True,
                    "name": name,
                    "message": (
                        f"已安装到 ~/.akm/plugins/{name}/，"
                        f"但即时加载失败（请检查日志或重启服务）"
                    ),
                    "hot": False,
                }
            if plugin.enabled:
                plugin.config = self.get_config(name) or {}
                try:
                    await plugin.on_load()
                except Exception as e:
                    logger.error(f"[PluginManager] 安装后 on_load 失败 {name}: {e}")
                    return {
                        "ok": True,
                        "name": name,
                        "message": (
                            f"已安装并注册 {name}，但 on_load 失败: {e}"
                        ),
                        "hot": False,
                    }
            return {
                "ok": True,
                "name": name,
                "enabled": bool(plugin.enabled),
                "message": f"已安装并加载 {name}（即时生效）",
                "hot": True,
            }

        return {
            "ok": True,
            "name": name,
            "message": f"已安装到 ~/.akm/plugins/{name}/，下次启动服务后生效",
            "hot": False,
        }

    # ── 插件删除 ──

    async def delete_plugin(self, name: str) -> dict:
        """删除本地/第三方插件目录（内置插件不可删除）"""
        if name not in self._plugin_sources:
            return {"ok": False, "error": "插件不存在"}

        source = self._plugin_sources[name]
        if source == "builtin":
            return {"ok": False, "error": "内置插件不可删除"}

        if source == "project":
            dest = self._project_dir / name
        else:
            dest = self._third_party_dir / name

        plugin = self.plugins.get(name)
        if plugin is not None and plugin.enabled:
            try:
                await plugin.on_unload()
            except Exception as e:
                logger.error(f"[PluginManager] 删除前 on_unload 失败 {name}: {e}")

        if dest.exists():
            shutil.rmtree(dest)

        # 清除插件状态（已注册的 FastAPI 路由无法安全移除，禁用后 hook/页面不再命中）
        self.plugins.pop(name, None)
        self._plugin_metas.pop(name, None)
        self._plugin_sources.pop(name, None)

        cfg = self._load_config_json()
        plugin_states = cfg.get("plugin_states", {})
        plugin_states.pop(name, None)
        cfg["plugin_states"] = plugin_states
        plugin_configs = cfg.get("plugin_configs", {})
        plugin_configs.pop(name, None)
        cfg["plugin_configs"] = plugin_configs
        self._save_config_json(cfg)

        return {
            "ok": True,
            "message": f"已删除 {name}（即时生效；残留路由不会再被调度）",
            "hot": True,
        }

    # ── 启停管理 ──

    async def toggle_plugin(self, name: str, enable: bool, *, hot: bool = True) -> dict:
        """切换插件启用/禁用状态。

        hot=True（默认，运行中的服务）：立即调用 on_load / on_unload，hook 与菜单即时生效。
        hot=False：仅写 config（CLI 在服务未运行时使用），下次启动生效。

        说明：已注册的 FastAPI 路由/静态挂载不会在禁用时拆除（Starlette 限制），
        但 hook 管道与插件宿主页均以 enabled 为准，禁用后不再参与请求链路。
        """
        if name not in self.plugins:
            return {"ok": False, "error": "插件不存在"}

        plugin = self.plugins[name]
        if not enable and plugin.meta.required:
            return {
                "ok": False,
                "error": f"插件 '{name}' 是必需的，不可禁用",
            }

        was_enabled = bool(plugin.enabled)
        if was_enabled == bool(enable):
            return {
                "ok": True,
                "name": name,
                "enabled": enable,
                "hot": hot,
                "message": f"插件已是{'启用' if enable else '禁用'}状态",
            }

        plugin.enabled = enable
        cfg = self._load_config_json()
        plugin_states = cfg.get("plugin_states", {})
        plugin_states[name] = enable
        cfg["plugin_states"] = plugin_states
        self._save_config_json(cfg)

        if not hot:
            return {
                "ok": True,
                "name": name,
                "enabled": enable,
                "hot": False,
                "message": "状态已保存，下次启动服务后生效",
            }

        try:
            if enable:
                plugin.config = self.get_config(name) or {}
                await plugin.on_load()
            else:
                await plugin.on_unload()
        except Exception as e:
            # 回滚内存与配置，避免“配置已开但生命周期失败”
            plugin.enabled = was_enabled
            plugin_states[name] = was_enabled
            cfg["plugin_states"] = plugin_states
            self._save_config_json(cfg)
            logger.error(
                f"[PluginManager] {name} 热{'启用' if enable else '禁用'}失败: {e}"
            )
            return {
                "ok": False,
                "error": f"生命周期钩子失败: {e}",
            }

        action = "启用" if enable else "禁用"
        return {
            "ok": True,
            "name": name,
            "enabled": enable,
            "hot": True,
            "message": f"已{action} {name}（即时生效）",
        }

    # ── 配置读写 ──

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
        # 同步更新内存中的 plugin.config（避免重启后才能读取）
        if name in self.plugins:
            defaults = {}
            for s in self._plugin_metas[name].settings:
                defaults[s.key] = s.default
            self.plugins[name].config = {**defaults, **data}
        return {"ok": True}

    # ── 查询 ──

    def get_plugin_list(self) -> list:
        """返回全部插件信息（供管理界面）"""
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
                "site_path": getattr(plugin, "site_path", ""),
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
