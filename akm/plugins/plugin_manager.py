"""插件管理器 — 扫描、加载、生命周期管理、配置读写、Hook 管道执行"""
import json
import logging
import re
import time
import traceback
import zipfile
import shutil
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import PluginMeta
from .base import NotifyFn, PluginBase
from .context import RequestContext
from akm.error_log import write_error_log
from akm.version import version_greater

logger = logging.getLogger("akm.plugin_manager")

# 插件会在 AKM 进程内执行，安装包仅接受扁平的、安全名称；同时限制压缩包，
# 避免管理接口被异常归档占满内存、临时目录或磁盘。
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PLUGIN_ARCHIVE_MAX_BYTES = 20 * 1024 * 1024
_PLUGIN_ARCHIVE_MAX_FILES = 500
_PLUGIN_ARCHIVE_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024

# ── 插件市场（GitHub 源） ──────────────────────────────────────────
# 市场仓库与分支：插件列表来自该仓库的 plugins/ 目录（git/trees 一次拉全树，
# 单文件内容用 raw.githubusercontent.com 拉取）。与 App 更新用的 GITHUB_REPO 一致。
_MARKET_REPO = "nkHub/akm"
_MARKET_BRANCH = "main"
_MARKET_TREE_URL = f"https://api.github.com/repos/{_MARKET_REPO}/git/trees/{_MARKET_BRANCH}?recursive=1"
_MARKET_RAW_URL = f"https://raw.githubusercontent.com/{_MARKET_REPO}/{_MARKET_BRANCH}/"
# 市场数据内存缓存有效期（秒）。打开插件页会拉取一次，5 分钟内不重复请求，
# 避免频繁命中 GitHub API 限流（未认证 60 次/小时）。
_MARKET_CACHE_TTL = 300
# 单插件目录最大文件数与总下载大小，避免异常目录拖垮请求。
_MARKET_PLUGIN_MAX_FILES = 200
_MARKET_PLUGIN_MAX_BYTES = 30 * 1024 * 1024


def _validate_plugin_archive(zf: zipfile.ZipFile) -> None:
    """校验上传归档的成员路径与解压预算，异常时在写盘前拒绝。"""
    members = zf.infolist()
    if len(members) > _PLUGIN_ARCHIVE_MAX_FILES:
        raise ValueError(f"压缩包文件数不能超过 {_PLUGIN_ARCHIVE_MAX_FILES}")
    total_size = 0
    for info in members:
        member = Path(info.filename)
        if member.is_absolute() or ".." in member.parts:
            raise ValueError("压缩包包含非法路径")
        total_size += info.file_size
        if total_size > _PLUGIN_ARCHIVE_MAX_UNCOMPRESSED_BYTES:
            raise ValueError(
                f"压缩包解压后总大小不能超过 {_PLUGIN_ARCHIVE_MAX_UNCOMPRESSED_BYTES // (1024 * 1024)}MB"
            )


def _is_valid_plugin_name(name: str) -> bool:
    """只接受用于目录、URL 和模块标识的安全插件名，拒绝绝对路径和穿越片段。"""
    return bool(_PLUGIN_NAME_RE.fullmatch(str(name or "")))


class _PluginStaticFiles(StaticFiles):
    """仅在所属插件已完成初始化时提供静态资源。"""

    def __init__(self, plugin: PluginBase, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._plugin = plugin

    async def __call__(self, scope, receive, send) -> None:
        if not self._plugin.enabled or not self._plugin.runtime_ready:
            # StaticFiles 是独立挂载的 ASGI 应用，不能依赖 FastAPI 的异常处理器。
            # 直接发送响应，避免未初始化插件的静态请求变成未处理异常。
            response = JSONResponse(
                status_code=503,
                content={"detail": f"插件 '{self._plugin.name}' 当前不可用"},
            )
            await response(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


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
        self.db: Any = None
        # 宿主（菜单栏 App）注册的系统通知回调；纯 uvicorn 启动时保持 None
        self._notify: NotifyFn | None = None
        # 插件市场缓存：self._market_cache 保存最近一次拉取的市场列表，
        # self._market_cache_at 记录拉取时间，超时后重新请求。
        self._market_cache: list | None = None
        self._market_cache_at: float = 0.0

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

        module_key = f"akm_plugin_{name}"
        try:
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_key] = module
            spec.loader.exec_module(module)
            plugin_class = getattr(module, "Plugin", None)
            if not isinstance(plugin_class, type) or not issubclass(plugin_class, PluginBase):
                raise TypeError("未找到继承 PluginBase 的 Plugin 类")
        except Exception as exc:
            # 单个插件的依赖或模块顶层代码出错时，其他插件仍应继续加载。
            sys.modules.pop(module_key, None)
            logger.warning(
                f"[PluginManager] 加载插件模块失败 {name}: {exc}", exc_info=True
            )
            return None

        # ── 实例化并注入上下文 ──
        plugin: PluginBase = plugin_class()
        plugin.name = name
        plugin.builtin = meta.builtin
        plugin.meta = meta
        plugin.logger = logging.getLogger(f"akm.plugins.{name}")
        plugin._static_dir = plugin_dir / "views"
        # 若菜单栏已注册 notify，新加载的插件立即拿到宿主通知能力
        plugin.notify = self._notify

        # 局部绑定便于类型收窄：后续 include_router/mount 只在 app 非空时调用
        app = self.app
        if app is not None:
            plugin.app = app
        if self.db is not None:
            plugin.db = self.db

        # ── 注册路由 ──
        if plugin.router is not None and app is not None:
            def ensure_plugin_ready() -> None:
                if not plugin.enabled or not plugin.runtime_ready:
                    raise HTTPException(
                        status_code=503,
                        detail=f"插件 '{plugin.name}' 当前不可用",
                    )

            routes_prefix = meta.routes_prefix or f"/{name}"
            app.include_router(
                plugin.router,
                prefix=routes_prefix,
                dependencies=[Depends(ensure_plugin_ready)],
            )
            logger.info(f"[PluginManager] 注册路由: {routes_prefix}")

        # ── 注册静态文件 + 前端路由（has_menu） ──
        if meta.has_menu and plugin._static_dir.exists() and app is not None:
            static_path = f"/plugins/{name}/static"
            app.mount(
                static_path,
                _PluginStaticFiles(plugin, directory=str(plugin._static_dir)),
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
            for entry in sorted(self._builtin_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if entry.name.startswith("__"):
                    continue
                if entry.name in ("base.py", "models.py", "plugin_manager.py", "__pycache__"):
                    continue
                self._load_plugin(entry, "builtin")
        except NotADirectoryError:
            for plugin_name in self._list_zip_builtin_plugins():
                # 从 zip 包中提取插件到临时目录再加载
                self._load_plugin_from_zip(plugin_name, "builtin")

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
                enabled = bool(self._plugin_metas[name].default_enabled)
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
                    plugin.runtime_ready = await plugin.on_load() is not False
                except Exception as e:
                    plugin.runtime_ready = False
                    logger.error(
                        f"[PluginManager] {plugin.name} on_load 异常: {e}"
                    )
                # ── on_load 成功后插件就绪 ──

        logger.info(f"[PluginManager] 共加载 {len(self.plugins)} 个插件")

    async def unload_all(self) -> None:
        """关闭服务前依次卸载已初始化的插件，单个异常不影响其余清理。"""
        for plugin in reversed(list(self.plugins.values())):
            if not plugin.runtime_ready:
                continue
            try:
                await plugin.on_unload()
            except Exception as exc:
                logger.error(f"[PluginManager] {plugin.name} on_unload 异常: {exc}")
            finally:
                plugin.runtime_ready = False

    def set_notify(self, notify_fn: NotifyFn | None) -> None:
        """注入宿主系统通知回调，并同步到所有已加载插件。

        由菜单栏 App 在服务 ready 后调用；参数签名约定为
        ``notify_fn(title, subtitle="", message="")``。
        传入 None 可清空（例如宿主退出前），纯 uvicorn 场景无需调用。
        """
        self._notify = notify_fn
        for plugin in self.plugins.values():
            plugin.notify = notify_fn
        # 同步挂到 app.state，便于插件在 notify 尚未注入时兜底读取
        if self.app is not None:
            try:
                self.app.state.host_notify = notify_fn
            except Exception:
                pass
        logger.info(
            "[PluginManager] 宿主通知已%s",
            "注册" if callable(notify_fn) else "清空",
        )

    # ── Hook 管道执行 ──

    async def run_hook(self, hook: str, ctx: RequestContext | None = None, **kwargs):
        """管道执行 hook：按 priority 从小到大，共享同一 RequestContext。

        Args:
            hook: hook 名称（on_request / on_key_selected / on_upstream_error / on_response）
            ctx: 请求级上下文；proxy/server 应始终传入同一实例。
                 若缺省则从 kwargs 中的 request 等字段临时构造（兼容旧测试）。
            **kwargs: 额外参数：
                - on_upstream_error: status_code / error_type / attempt / key
                - on_key_selected: model / key（写入 ctx）
                - on_response: response（写入 ctx.response）

        Returns:
            - on_upstream_error: 第一个非 None 的动作字符串
            - 其它 hook: 始终返回同一个 RequestContext
        """
        # 兼容：旧调用方只传 request=... 时补齐 RequestContext
        if ctx is None:
            request = kwargs.get("request")
            if not isinstance(request, dict):
                request = {}
            ctx = RequestContext(
                request,
                api_path=str(kwargs.get("api_path", "") or request.get("__akm_api_path__", "") or ""),
                client_user_agent=str(
                    kwargs.get("client_user_agent", "")
                    or request.get("__akm_client_user_agent__", "")
                    or ""
                ),
            )
        if "response" in kwargs and isinstance(kwargs.get("response"), dict):
            ctx.response = kwargs["response"]
        if "key" in kwargs and isinstance(kwargs.get("key"), dict):
            ctx.key = kwargs["key"]
        if "model" in kwargs and kwargs.get("model") is not None:
            ctx.model = str(kwargs.get("model") or ctx.model)

        candidates = [
            p for p in self.plugins.values()
            if p.enabled and p.runtime_ready and p.meta is not None and p.meta.hooks.get(hook)
        ]
        candidates.sort(key=lambda p: self._plugin_metas[p.name].priority)

        upstream_action = None  # 仅 on_upstream_error 使用

        for plugin in candidates:
            try:
                if hook == "on_upstream_error":
                    ret = await plugin.on_upstream_error(
                        ctx,
                        status_code=int(kwargs.get("status_code", 0) or 0),
                        error_type=str(kwargs.get("error_type", "http") or "http"),
                        attempt=int(kwargs.get("attempt", 0) or 0),
                        key=kwargs.get("key") if kwargs.get("key") is not None else ctx.key,
                    )
                    # 第一个非 None 即为最终决策
                    if ret is not None and upstream_action is None:
                        upstream_action = ret
                    continue

                if hook == "on_key_selected":
                    # 每轮选 Key 前清除上一轮 skip 标记
                    if ctx.is_skip_key:
                        ctx.clear_action()
                    ret = await plugin.on_key_selected(ctx)
                    if ctx.is_skip_key:
                        # 立即停止管道，避免后续插件把已跳过的 key 计入 in-flight
                        break
                    # 兼容：插件仍可通过返回 dict 替换 key；
                    # 或返回带 type/__akm_action__ 的 skip 结构。
                    if isinstance(ret, dict):
                        if ret.get("type") == "skip_key" or ret.get("__akm_action__") == "skip_key":
                            if not ctx.is_skip_key:
                                ctx.set_skip_key(
                                    error=str(ret.get("error", "") or ""),
                                    security_action=str(ret.get("security_action", "quota") or "quota"),
                                )
                            break
                        # 返回值视为替代 key
                        ctx.key = ret
                    continue

                if hook == "on_request":
                    ret = await plugin.on_request(ctx)
                    # 插件可直接 ctx.set_block；也可返回控制结构（兼容旧写法）
                    if isinstance(ret, dict) and (
                        ret.get("type") == "block" or ret.get("__akm_action__") == "block"
                    ):
                        if not ctx.is_block:
                            ctx.set_block(
                                status_code=int(ret.get("status_code", 400) or 400),
                                error=str(ret.get("error", "") or ""),
                                body=ret.get("body"),
                                security_action=str(ret.get("security_action", "block") or "block"),
                                security_reason=str(ret.get("security_reason", "") or ""),
                            )
                        break
                    # 返回新 request dict 时替换引用（in-place 改写则 ret 为 None 或原对象）
                    if isinstance(ret, dict) and ret is not ctx.request:
                        # 排除误把 block 结构当 request 的情况已在上面处理
                        if "type" not in ret and "__akm_action__" not in ret:
                            ctx.set_request(ret)
                    elif ret is None:
                        # in-place 改写后同步 model
                        ctx.sync_model_from_request()
                    continue

                if hook == "on_response":
                    ret = await plugin.on_response(ctx)
                    if isinstance(ret, dict):
                        ctx.response = ret
                    continue

                # 未知 hook：尽力以 ctx 调用
                ret = await getattr(plugin, hook)(ctx)
                if ret is not None:
                    logger.debug(
                        f"[PluginManager] {plugin.name}.{hook} 返回了非标准值: {type(ret)}"
                    )

            except Exception as e:
                logger.error(
                    f"[PluginManager] {plugin.name}.{hook} 异常: {e}"
                )
                continue

        if hook == "on_upstream_error":
            return upstream_action
        return ctx

    # ── 转换器查询 ──

    def get_converter(self, from_format: str, to_format: str) -> Optional[PluginBase]:
        """根据转换声明查找启用的转换插件"""
        for plugin in self.plugins.values():
            meta = plugin.meta
            if not plugin.enabled or not plugin.runtime_ready or meta is None:
                continue
            for c in meta.converts:
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
            content = await file.read(_PLUGIN_ARCHIVE_MAX_BYTES + 1)
            if len(content) > _PLUGIN_ARCHIVE_MAX_BYTES:
                return {
                    "ok": False,
                    "error": f"压缩包不能超过 {_PLUGIN_ARCHIVE_MAX_BYTES // (1024 * 1024)}MB",
                }
            zippath = tmp / "plugin.zip"
            zippath.write_bytes(content)

            try:
                with zipfile.ZipFile(zippath, "r") as zf:
                    _validate_plugin_archive(zf)
                    zf.extractall(tmp)
            except (ValueError, zipfile.BadZipFile) as exc:
                return {"ok": False, "error": f"插件压缩包无效: {exc}"}

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
                write_error_log(
                    source="plugin_manager.install",
                    error=str(e),
                    traceback_str=traceback.format_exc(),
                )
                return {"ok": False, "error": "plugin.json 格式错误"}

            name = meta.name
            if not _is_valid_plugin_name(name):
                return {"ok": False, "error": "插件名只能包含字母、数字、下划线和连字符"}

            # ── 重名检测 ──
            dest = self._third_party_dir / name
            if dest.exists():
                return {"ok": False, "error": f"插件 '{name}' 已存在"}

            # 检查是否与内置插件重名。py2app 中内置目录位于 zip，无法直接 iterdir。
            try:
                builtin_names = {
                    entry.name
                    for entry in self._builtin_dir.iterdir()
                    if entry.is_dir()
                }
            except NotADirectoryError:
                builtin_names = self._list_zip_builtin_plugins()
            if name in builtin_names:
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
                    plugin.runtime_ready = await plugin.on_load() is not False
                except Exception as e:
                    plugin.runtime_ready = False
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

    # ── 插件市场（GitHub 源拉取） ──

    async def _fetch_market_file(
        self, rel_path: str, client: httpx.AsyncClient | None = None
    ) -> str | None:
        """从市场仓库 raw 地址下载单个文件内容；失败返回 None。"""
        try:
            if client is not None:
                resp = await client.get(_MARKET_RAW_URL + rel_path)
            else:
                async with httpx.AsyncClient(timeout=30) as c:
                    resp = await c.get(_MARKET_RAW_URL + rel_path)
            if resp.status_code != 200:
                return None
            return resp.text
        except Exception as e:
            logger.warning(f"[PluginManager] 拉取市场文件 {rel_path} 失败: {e}")
            return None

    def _read_local_plugin_version(self, plugin_dir: Path, name: str) -> str | None:
        """读取磁盘上插件目录 plugin.json 中的版本号，读取失败返回 None。"""
        pj = plugin_dir / "plugin.json"
        if not pj.exists():
            return None
        try:
            return PluginMeta.model_validate_json(pj.read_text("utf-8")).version
        except Exception:
            return None

    def _build_market_item(self, name: str, meta: PluginMeta) -> dict:
        """组装单条市场插件信息，并比对本地安装状态（仅第三方来源可更新）。"""
        local_source = self._plugin_sources.get(name)
        third_party_dir = self._third_party_dir / name

        # 已安装：磁盘上有第三方目录，或以任意来源加载过（内置/本地源码/第三方）
        installed = third_party_dir.exists() or local_source is not None

        # 本地版本：优先读磁盘第三方目录，否则取当前加载的 meta
        installed_version = None
        if third_party_dir.exists():
            installed_version = self._read_local_plugin_version(third_party_dir, name)
        elif local_source is not None:
            installed_version = self._plugin_metas[name].version

        # 仅对"已安装且来源为第三方"的插件标记可更新；内置/项目源码跟随 App/源码，
        # 不通过市场更新，避免覆盖开发环境或内置代码。
        has_update = bool(
            installed
            and local_source == "third_party"
            and installed_version
            and version_greater(meta.version, installed_version)
        )
        return {
            "name": meta.name,
            "version": meta.version,
            "description": meta.description,
            "category": meta.category,
            "has_menu": meta.has_menu,
            "installed": installed,
            "installed_version": installed_version,
            "local_source": local_source,
            "has_update": has_update,
        }

    async def fetch_market_plugins(self) -> dict:
        """拉取 GitHub 插件市场列表（git/trees 全树 + raw 逐个 plugin.json）。

        结果带 5 分钟内存缓存：打开插件页自动拉取，短时间内重复打开不重复请求，
        降低 GitHub 未认证 API 限流风险。返回 {"ok": True, "cached": bool, "plugins": [...]}。
        """
        now = time.time()
        if self._market_cache is not None and now - self._market_cache_at < _MARKET_CACHE_TTL:
            return {"ok": True, "cached": True, "plugins": self._market_cache}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                tree_resp = await client.get(_MARKET_TREE_URL)
                if tree_resp.status_code != 200:
                    return {"ok": False, "error": f"获取插件市场目录失败: HTTP {tree_resp.status_code}"}
                tree = tree_resp.json().get("tree", [])

                # 筛选 plugins/*/plugin.json，得到插件目录清单
                plugin_names = []
                for entry in tree:
                    path = entry.get("path", "")
                    parts = path.split("/")
                    if (
                        entry.get("type") == "blob"
                        and len(parts) == 3
                        and parts[0] == "plugins"
                        and parts[2] == "plugin.json"
                    ):
                        plugin_names.append(parts[1])

                # 逐个拉取 plugin.json，与本地安装状态比对
                result = []
                for name in sorted(set(plugin_names)):
                    content = await self._fetch_market_file(
                        f"plugins/{name}/plugin.json", client=client
                    )
                    if not content:
                        continue
                    try:
                        meta = PluginMeta.model_validate_json(content)
                    except Exception:
                        continue
                    result.append(self._build_market_item(name, meta))
        except Exception as e:
            logger.error(f"[PluginManager] 拉取插件市场失败: {e}")
            return {"ok": False, "error": "无法连接 GitHub，插件市场拉取失败"}

        self._market_cache = result
        self._market_cache_at = time.time()
        return {"ok": True, "cached": False, "plugins": result}

    async def install_market_plugin(self, name: str) -> dict:
        """从 GitHub 市场拉取插件目录并覆盖到 ~/.akm/plugins/{name}/。

        按用户决策不热重载：已加载的第三方插件覆盖后提示重启服务生效；
        全新插件则沿用 zip 安装的即时加载逻辑。
        """
        if not _is_valid_plugin_name(name):
            return {"ok": False, "error": "非法插件名"}
        # 内置插件跟随 App 版本，不通过市场更新
        if self._plugin_sources.get(name) == "builtin":
            return {"ok": False, "error": "内置插件跟随 App 版本，不通过市场更新"}

        # 1) 拉取仓库全树，筛选该插件目录下的全部文件路径
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                tree_resp = await client.get(_MARKET_TREE_URL)
                if tree_resp.status_code != 200:
                    return {"ok": False, "error": f"获取插件目录失败: HTTP {tree_resp.status_code}"}
                tree = tree_resp.json().get("tree", [])
        except Exception as e:
            logger.error(f"[PluginManager] 拉取插件目录失败 {name}: {e}")
            return {"ok": False, "error": "无法连接 GitHub，请检查网络"}

        prefix = f"plugins/{name}/"
        blob_paths = [
            entry.get("path", "")
            for entry in tree
            if entry.get("type") == "blob" and entry.get("path", "").startswith(prefix)
        ]
        if not blob_paths:
            return {"ok": False, "error": f"GitHub 上未找到插件 '{name}'"}
        if len(blob_paths) > _MARKET_PLUGIN_MAX_FILES:
            return {"ok": False, "error": f"插件 '{name}' 文件过多，已中止下载"}

        # 2) 逐个下载到临时目录（复用同一 client，避免每次开连接）
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            total = 0
            async with httpx.AsyncClient(timeout=30) as client:
                for rel in blob_paths:
                    content = await self._fetch_market_file(rel, client=client)
                    if content is None:
                        return {"ok": False, "error": f"下载 {rel} 失败，请稍后重试"}
                    total += len(content)
                    if total > _MARKET_PLUGIN_MAX_BYTES:
                        return {"ok": False, "error": f"插件 '{name}' 体积超出限制，已中止"}
                    target = root / rel[len("plugins/"):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, "utf-8")

            plugin_root = root / name
            meta_path = plugin_root / "plugin.json"
            if not meta_path.exists():
                return {"ok": False, "error": "插件缺少 plugin.json"}
            try:
                meta = PluginMeta.model_validate_json(meta_path.read_text("utf-8"))
            except Exception:
                return {"ok": False, "error": "plugin.json 格式错误"}
            if not (plugin_root / "index.py").exists():
                return {"ok": False, "error": "插件缺少 index.py"}

            # 3) 覆盖 ~/.akm/plugins/{name}/
            dest = self._third_party_dir / name
            self._third_party_dir.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(plugin_root, dest)

        # 4) 使市场缓存失效，下次拉取为最新
        self._market_cache = None

        # 5) 已加载的第三方插件：覆盖 + 提示重启（不热重载）
        local_source = self._plugin_sources.get(name)
        if local_source == "third_party":
            return {
                "ok": True,
                "name": name,
                "version": meta.version,
                "restart": True,
                "message": f"已更新 ~/.akm/plugins/{name}/，重启服务后生效",
            }
        if local_source == "project":
            return {
                "ok": True,
                "name": name,
                "version": meta.version,
                "restart": True,
                "message": (
                    f"已覆盖 ~/.akm/plugins/{name}/；当前进程加载的是项目源码插件，"
                    f"如需使用市场版本请删除项目 plugins/{name} 后重启"
                ),
            }

        # 全新插件：沿用 zip 安装的即时加载逻辑
        if self.app is not None:
            plugin = self._load_plugin(dest, "third_party")
            if plugin is None:
                return {
                    "ok": True,
                    "name": name,
                    "version": meta.version,
                    "restart": True,
                    "message": f"已安装到 ~/.akm/plugins/{name}/，但即时加载失败，请重启服务",
                }
            if plugin.enabled:
                plugin.config = self.get_config(name) or {}
                try:
                    plugin.runtime_ready = await plugin.on_load() is not False
                except Exception as e:
                    plugin.runtime_ready = False
                    logger.error(f"[PluginManager] 市场安装后 on_load 失败 {name}: {e}")
                    return {
                        "ok": True,
                        "name": name,
                        "version": meta.version,
                        "restart": True,
                        "message": f"已安装并注册 {name}，但 on_load 失败: {e}",
                    }
            return {
                "ok": True,
                "name": name,
                "version": meta.version,
                "restart": False,
                "message": f"已安装并加载 {name}（即时生效）",
            }

        return {
            "ok": True,
            "name": name,
            "version": meta.version,
            "restart": True,
            "message": f"已安装到 ~/.akm/plugins/{name}/，下次启动服务后生效",
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
        if plugin is not None:
            # 已注册的路由会保留对实例的闭包引用，必须先将其置为不可用，
            # 使卸载期间及删除后的请求稳定返回 503 而非继续访问旧资源。
            plugin.enabled = False
            was_runtime_ready = plugin.runtime_ready
            plugin.runtime_ready = False
        else:
            was_runtime_ready = False
        if plugin is not None and was_runtime_ready:
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
        if not enable and self._plugin_metas[name].required:
            return {
                "ok": False,
                "error": f"插件 '{name}' 是必需的，不可禁用",
            }

        was_enabled = bool(plugin.enabled)
        was_runtime_ready = plugin.runtime_ready
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
                plugin.runtime_ready = True
            else:
                await plugin.on_unload()
                plugin.runtime_ready = False
        except Exception as e:
            # 回滚内存与配置，避免"配置已开但生命周期失败"
            plugin.enabled = was_enabled
            plugin.runtime_ready = was_runtime_ready
            plugin_states[name] = was_enabled
            cfg["plugin_states"] = plugin_states
            self._save_config_json(cfg)
            logger.error(
                f"[PluginManager] {name} 热{'启用' if enable else '禁用'}失败: {e}"
            )
            write_error_log(
                source="plugin_manager.lifecycle",
                error=str(e),
                traceback_str=traceback.format_exc(),
                extra={"plugin": name, "action": "enable" if enable else "disable"},
            )
            return {
                "ok": False,
                "error": f"插件 '{name}' {'启用' if enable else '禁用'}失败",
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
        # 同步更新内存中的 plugin.config，并让缓存配置值的插件立即刷新状态。
        if name in self.plugins:
            defaults = {}
            for s in self._plugin_metas[name].settings:
                defaults[s.key] = s.default
            plugin = self.plugins[name]
            old_config = plugin.config
            plugin.config = {**defaults, **data}
            try:
                plugin.on_config_changed(old_config, plugin.config)
            except Exception as e:
                logger.error(f"[PluginManager] {name} 配置热更新回调失败: {e}")
        return {"ok": True}

    # ── 查询 ──

    def get_plugin_list(self) -> list:
        """返回全部插件信息（供管理界面）"""
        result = []
        for name, plugin in self.plugins.items():
            meta = self._plugin_metas[name]
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
            meta = plugin.meta
            if plugin.enabled and plugin.runtime_ready and meta is not None and meta.has_menu:
                items.append({
                    "name": meta.name,
                    "title": meta.menu.get("title", meta.name),
                    "icon": meta.menu.get("icon", "plugin"),
                    "order": meta.menu.get("order", 100),
                    "route": f"/plugins/{meta.name}",
                })
        items.sort(key=lambda x: x["order"])
        return items

    def get_plugin_metas(self) -> list:
        """返回所有插件元数据（含 settings schema，供设置页表单渲染）"""
        return [self._plugin_metas[name].model_dump() for name in self.plugins]
