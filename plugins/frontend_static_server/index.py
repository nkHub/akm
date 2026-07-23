"""Vue、React 等单页应用构建产物的静态托管插件。"""

from __future__ import annotations

from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.staticfiles import StaticFiles

from akm.plugins import PluginBase


_RESERVED_PREFIXES = ("/api", "/v1", "/admin", "/health", "/debug")


class _SpaStaticFiles(StaticFiles):
    """在静态文件不存在时，为单页应用的前端路由返回入口页。"""

    def __init__(self, directory: Path, plugin: "Plugin"):
        super().__init__(directory=str(directory), html=False, check_dir=True)
        self._plugin = plugin

    async def __call__(self, scope, receive, send):
        """插件禁用后保留已注册路由，但不再对外提供站点内容。"""
        if not self._plugin.enabled:
            await PlainTextResponse("frontend_static_server 插件未启用", status_code=503)(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

    async def get_response(self, path: str, scope):
        """仅回退无扩展名路径，保证缺失的 JS、CSS 和图片仍明确返回 404。"""
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or not self._plugin.spa_fallback or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)


class Plugin(PluginBase):
    """根据插件配置挂载一个前端构建目录。"""

    async def on_load(self):
        """在配置有效时注册一次静态站点挂载。路由替换由服务重启完成。"""
        if getattr(self, "_mounted", False):
            return

        build_dir = self._build_dir()
        route_prefix = self._route_prefix()
        if build_dir is None or route_prefix is None:
            return

        # 注意：不可使用 _static_dir 方法名，PluginManager 会注入同名 Path 属性
        asset_dir = self._resolve_asset_dir()
        if asset_dir is False:
            return

        index_file = build_dir / "index.html"
        if not index_file.is_file():
            self.logger.warning("[frontend_static_server] 未找到入口文件: %s", index_file)
            return

        self.spa_fallback = bool((self.config or {}).get("spa_fallback", True))
        if asset_dir is not None:
            self.app.mount(
                f"{route_prefix}/static",
                StaticFiles(directory=str(asset_dir), check_dir=True),
                name=f"frontend_static_assets_{self.name}",
            )
        self.app.mount(route_prefix, _SpaStaticFiles(build_dir, self), name=f"frontend_static_{self.name}")
        self._mounted = True
        # 仅在挂载真正完成后记录入口，管理台据此展示可访问的站点链接。
        # 配置保存后的新路径在服务重启前不会覆盖这里的值，避免跳转到尚未注册的路由。
        self.site_path = route_prefix
        self.logger.info("[frontend_static_server] 已挂载 %s 到 %s", build_dir, route_prefix)

    def _build_dir(self) -> Path | None:
        """解析目录并拒绝空值或非目录配置，避免意外挂载当前工作目录。"""
        raw_path = str((self.config or {}).get("build_dir", "") or "").strip()
        if not raw_path:
            self.logger.warning("[frontend_static_server] 未配置 build_dir，跳过挂载")
            return None

        build_dir = Path(raw_path).expanduser().resolve()
        if not build_dir.is_dir():
            self.logger.warning("[frontend_static_server] 构建目录不存在或不是目录: %s", build_dir)
            return None
        return build_dir

    def _resolve_asset_dir(self) -> Path | None | bool:
        """解析可选独立资源目录；明确配置错误时停止整个站点挂载。

        返回值约定：
        - Path：有效目录，将挂载到 <route_prefix>/static
        - None：未配置 static_dir，跳过独立资源挂载
        - False：配置了路径但无效，中止整站挂载
        """
        raw_path = str((self.config or {}).get("static_dir", "") or "").strip()
        if not raw_path:
            return None

        asset_dir = Path(raw_path).expanduser().resolve()
        if not asset_dir.is_dir():
            self.logger.warning("[frontend_static_server] 独立静态资源目录不存在或不是目录: %s", asset_dir)
            return False
        return asset_dir

    def _route_prefix(self) -> str | None:
        """规范化自定义路径，并保护 AKM 自身的 API、管理台和健康检查路由。"""
        route_prefix = str((self.config or {}).get("route_prefix", "/web") or "").strip()
        if not route_prefix.startswith("/"):
            route_prefix = f"/{route_prefix}"
        route_prefix = route_prefix.rstrip("/") or "/"

        if route_prefix == "/" or any(
            route_prefix == reserved or route_prefix.startswith(f"{reserved}/")
            for reserved in _RESERVED_PREFIXES
        ):
            self.logger.warning("[frontend_static_server] 不允许使用受保护路径: %s", route_prefix)
            return None
        return route_prefix
