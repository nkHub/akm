"""AKM /v1/agent Web 聊天界面插件（AetherAI 对话窗口）。

将 chat 项目构建产物打进本插件 dist/ 目录，启用后即可在
`<route_prefix>`（默认 /chat）访问，无需配置绝对路径。界面直接请求
同源 /v1/agent，图片等上传资源走 /agent-uploads/ 相对路径。
"""

from __future__ import annotations

from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from akm.plugins import PluginBase


_RESERVED_PREFIXES = ("/api", "/v1", "/admin", "/health", "/debug")


class _SpaStaticFiles(StaticFiles):
    """按插件就绪状态提供静态文件，并可为单页应用回退入口页。"""

    def __init__(self, directory: Path, plugin: "Plugin", *, spa_fallback: bool = False):
        super().__init__(directory=str(directory), html=False, check_dir=True)
        self._plugin = plugin
        self._spa_fallback = spa_fallback

    async def __call__(self, scope, receive, send):
        """插件禁用、卸载或未完成初始化时，不再对外提供站点内容。"""
        if not self._plugin.enabled or not self._plugin.runtime_ready:
            await PlainTextResponse("agent_chat 插件当前不可用", status_code=503)(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

    async def get_response(self, path: str, scope):
        """仅回退无扩展名路径，保证缺失的 JS、CSS 和图片仍明确返回 404。"""
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or not self._spa_fallback or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)


class Plugin(PluginBase):
    """把内置的 chat 构建产物挂载为 /v1/agent Web 聊天界面。"""

    async def on_load(self):
        """在产物存在时注册一次静态站点挂载。路由替换由服务重启完成。"""
        if getattr(self, "_mounted", False):
            return False

        route_prefix = self._route_prefix()
        if route_prefix is None:
            return False

        # 产物目录固定为插件目录下的 dist/，由 PluginManager 注入的
        # _static_dir（插件目录/views）的父目录定位插件目录。
        plugin_dir = Path(self._static_dir).parent
        build_dir = plugin_dir / "dist"
        if not build_dir.is_dir():
            self.logger.warning("[agent_chat] 未找到内置构建产物目录: %s", build_dir)
            return False

        index_file = build_dir / "index.html"
        if not index_file.is_file():
            self.logger.warning("[agent_chat] 未找到入口文件: %s", index_file)
            return False

        app = self.app
        if app is None:
            self.logger.warning("[agent_chat] FastAPI app 未注入，跳过挂载")
            return False

        # 清除同名旧挂载，避免进程内重启后旧实例残留路由遮蔽新实例。
        mount_names = {f"agent_chat_{self.name}"}
        old_count = len(app.router.routes)
        app.router.routes = [
            route
            for route in app.router.routes
            if not (isinstance(route, Mount) and route.name in mount_names)
        ]
        removed = old_count - len(app.router.routes)
        if removed:
            self.logger.info("[agent_chat] 清理了 %d 个旧挂载路由（进程内重启）", removed)

        self.spa_fallback = bool((self.config or {}).get("spa_fallback", True))
        app.mount(
            route_prefix,
            _SpaStaticFiles(build_dir, self, spa_fallback=self.spa_fallback),
            name=f"agent_chat_{self.name}",
        )
        self._mounted = True
        self.site_path = route_prefix
        self.logger.info("[agent_chat] 已挂载 Web 聊天界面到 %s", route_prefix)
        return True

    def _route_prefix(self) -> str | None:
        """规范化自定义路径，并保护 AKM 自身的 API、管理台和健康检查路由。"""
        route_prefix = str((self.config or {}).get("route_prefix", "/chat") or "").strip()
        if not route_prefix.startswith("/"):
            route_prefix = f"/{route_prefix}"
        route_prefix = route_prefix.rstrip("/") or "/"

        if route_prefix == "/" or any(
            route_prefix == reserved or route_prefix.startswith(f"{reserved}/")
            for reserved in _RESERVED_PREFIXES
        ):
            self.logger.warning("[agent_chat] 不允许使用受保护路径: %s", route_prefix)
            return None
        return route_prefix
