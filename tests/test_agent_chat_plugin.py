"""agent_chat Web 聊天界面插件的路由测试。"""

import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.agent_chat.index import Plugin

# 真实内置产物目录（插件仓库内）
_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "agent_chat"


def _load_plugin(
    app: FastAPI,
    plugin_dir: Path,
    route_prefix="/chat",
    spa_fallback=True,
):
    """构造已注入上下文的插件实例，模拟 PluginManager 的启用加载行为。"""
    plugin = Plugin()
    plugin.app = app
    plugin.logger = logging.getLogger("test.agent_chat")
    plugin.enabled = True
    plugin.name = "agent_chat"
    # PluginManager 会为所有插件注入 views 目录，名称固定为 _static_dir；
    # 插件目录通过 _static_dir.parent 定位。
    plugin._static_dir = plugin_dir / "views"
    plugin.config = {
        "route_prefix": route_prefix,
        "spa_fallback": spa_fallback,
    }
    plugin.runtime_ready = asyncio.run(plugin.on_load()) is not False
    return plugin


def _copied_dist(tmp_path: Path) -> Path:
    """把真实 dist 复制到临时插件目录，模拟打包分发后的目录结构。"""
    plugin_dir = tmp_path / "agent_chat"
    shutil.copytree(_PLUGIN_DIR, plugin_dir)
    return plugin_dir


def test_agent_chat_serves_index_and_assets(tmp_path):
    """真实产物应能正常响应入口页、JS、CSS 与 favicon。"""
    plugin_dir = _copied_dist(tmp_path)
    app = FastAPI()
    plugin = _load_plugin(app, plugin_dir)
    client = TestClient(app)

    index = client.get("/chat")
    assert index.status_code == 200
    assert "AI Chat Window" in index.text
    assert "./assets/index-DtK7hnPg.js" in index.text

    assert client.get("/chat/assets/index-DtK7hnPg.js").status_code == 200
    assert client.get("/chat/assets/index-Bycx4iDa.css").status_code == 200
    assert client.get("/chat/favicon.svg").status_code == 200
    assert plugin.site_path == "/chat"


def test_agent_chat_rejects_core_routes(tmp_path):
    """自定义挂载不得覆盖 AKM 的核心服务接口。"""
    plugin_dir = _copied_dist(tmp_path)
    app = FastAPI()
    plugin = _load_plugin(app, plugin_dir, route_prefix="/api/site")

    assert not getattr(plugin, "_mounted", False)
    assert TestClient(app).get("/api/site").status_code == 404


def test_agent_chat_spa_fallback_and_missing_asset(tmp_path):
    """无扩展名路径应回退 index.html，缺失资源应返回 404。"""
    plugin_dir = _copied_dist(tmp_path)
    app = FastAPI()
    _load_plugin(app, plugin_dir)
    client = TestClient(app)

    assert client.get("/chat/some/deep/route").text == client.get("/chat").text
    assert client.get("/chat/assets/missing.js").status_code == 404


def test_agent_chat_unready_returns_503(tmp_path):
    """禁用或删除后的残留挂载不能继续暴露站点。"""
    plugin_dir = _copied_dist(tmp_path)
    app = FastAPI()
    plugin = _load_plugin(app, plugin_dir)
    client = TestClient(app)

    plugin.enabled = False
    plugin.runtime_ready = False

    assert client.get("/chat").status_code == 503
    assert client.get("/chat/assets/index-DtK7hnPg.js").status_code == 503
