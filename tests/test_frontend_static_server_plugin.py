"""前端静态站点服务插件的路由与 SPA 回退测试。"""

import asyncio
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.frontend_static_server.index import Plugin


def _load_plugin(
    app: FastAPI,
    build_dir,
    route_prefix="/web",
    spa_fallback=True,
    static_dir="",
):
    """构造已注入上下文的插件实例，模拟 PluginManager 的启用加载行为。"""
    plugin = Plugin()
    plugin.app = app
    plugin.logger = logging.getLogger("test.frontend_static_server")
    plugin.enabled = True
    plugin.name = "frontend_static_server"
    # PluginManager 会为所有插件注入 views 目录，名称固定为 _static_dir。
    plugin._static_dir = build_dir / "views"
    plugin.config = {
        "build_dir": str(build_dir),
        "route_prefix": route_prefix,
        "spa_fallback": spa_fallback,
        "static_dir": str(static_dir),
    }
    asyncio.run(plugin.on_load())
    return plugin


def test_frontend_static_server_serves_assets_and_spa_routes(tmp_path):
    """真实资源应正常响应，History 路由应回退到入口页。"""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<div id='app'>site</div>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("console.log('site')", encoding="utf-8")
    app = FastAPI()
    plugin = _load_plugin(app, tmp_path)
    client = TestClient(app)

    assert client.get("/web/assets/app.js").status_code == 200
    assert client.get("/web/dashboard/settings").text == "<div id='app'>site</div>"
    assert client.get("/web/assets/missing.js").status_code == 404
    assert plugin.site_path == "/web"


def test_frontend_static_server_rejects_core_routes(tmp_path):
    """自定义挂载不得覆盖 AKM 的核心服务接口。"""
    (tmp_path / "index.html").write_text("site", encoding="utf-8")
    app = FastAPI()
    plugin = _load_plugin(app, tmp_path, route_prefix="/api/site")

    assert not getattr(plugin, "_mounted", False)
    assert TestClient(app).get("/api/site").status_code == 404


def test_frontend_static_server_mounts_custom_static_directory(tmp_path):
    """构建目录外的资源目录应固定挂载到站点路径的 static 子路径。"""
    build_dir = tmp_path / "dist"
    static_dir = tmp_path / "public"
    build_dir.mkdir()
    static_dir.mkdir()
    (build_dir / "index.html").write_text("site", encoding="utf-8")
    (static_dir / "logo.txt").write_text("custom asset", encoding="utf-8")
    app = FastAPI()
    _load_plugin(app, build_dir, static_dir=static_dir)

    response = TestClient(app).get("/web/static/logo.txt")
    assert response.status_code == 200
    assert response.text == "custom asset"
