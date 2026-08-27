"""插件市场更新热重载（_hot_reload_plugin）的单元测试。

覆盖场景：
1. 已加载的第三方插件被市场版本覆盖后，热重载会用磁盘新代码重新实例化；
2. 旧实例先被卸载（on_unload 触发），新的 plugin.json（版本号）与新 index.py
   （行为标记）都已生效；插件仍保留启用与就绪状态，restart 标记为 False。
"""
import asyncio
import sys
from pathlib import Path

import pytest

from akm.plugins.plugin_manager import PluginManager

PLUGIN_V1 = {
    "name": "demo_plugin",
    "version": "1.0.0",
    "has_menu": False,
    "category": "guard",
    "description": "热重载测试插件 v1",
    "builtin": False,
    "default_enabled": True,
    "required": False,
    "hooks": {},
    "settings": [],
}

PLUGIN_V2 = {**PLUGIN_V1, "version": "2.0.0", "description": "热重载测试插件 v2"}

INDEX_V1 = '''"""热重载测试插件 v1"""
from pathlib import Path

from akm.plugins import PluginBase


class Plugin(PluginBase):
    """v1 版本：on_unload 时写入 sentinel 文件，供测试断言卸载已触发"""

    async def on_load(self):
        self.marker = "v1"
        return True

    async def on_unload(self):
        Path(self._static_dir.parent, ".unloaded").write_text("ok", encoding="utf-8")
'''

INDEX_V2 = '''"""热重载测试插件 v2"""
from akm.plugins import PluginBase


class Plugin(PluginBase):
    """v2 版本：marker 更新为 v2，证明加载的是覆盖后的新代码"""

    async def on_load(self):
        self.marker = "v2"
        return True
'''


def _run(coro):
    """在同一进程内跑异步用例（插件生命周期短，无事件循环冲突）"""
    return asyncio.run(coro)


def _write_plugin(root: Path, plugin_json: dict, index_py: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        __import__("json").dumps(plugin_json, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "index.py").write_text(index_py, encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _clean_plugin_module():
    """清理模块缓存，避免跨测试复用旧的 akm_plugin_demo_plugin"""
    yield
    sys.modules.pop("akm_plugin_demo_plugin", None)


def test_market_update_hot_reloads_third_party_plugin(tmp_path):
    third_party = tmp_path / "third_party"
    plugin_dir = _write_plugin(third_party / "demo_plugin", PLUGIN_V1, INDEX_V1)

    manager = PluginManager()
    manager._third_party_dir = third_party
    manager._config_path = tmp_path / "config.json"

    old = manager._load_plugin(plugin_dir, "third_party")
    assert old is not None, "v1 插件应能加载"
    old.enabled = True
    old.runtime_ready = True  # 模拟 on_load 成功后的就绪态

    # 市场覆盖：磁盘写入新版本代码 + 新 plugin.json
    _write_plugin(plugin_dir, PLUGIN_V2, INDEX_V2)

    result = _run(manager._hot_reload_plugin("demo_plugin", plugin_dir, "2.0.0"))
    assert result["ok"] is True
    assert result["restart"] is False, "热重载成功后不应再要求重启"
    assert result["hot"] is True
    assert result["version"] == "2.0.0"

    new = manager.plugins.get("demo_plugin")
    assert new is not None
    assert new is not old, "热重载后应是全新的插件实例"
    assert new.meta.version == "2.0.0"
    assert new.marker == "v2", "应执行覆盖后的磁盘新代码"
    assert new.enabled is True
    assert new.runtime_ready is True
    assert manager._plugin_sources["demo_plugin"] == "third_party"
    assert plugin_dir.joinpath(".unloaded").exists(), "旧实例应被调用 on_unload"


def test_hot_reload_keeps_disabled_state(tmp_path):
    """插件原先为禁用状态时，热重载保留禁用态且不调用 on_load"""
    third_party = tmp_path / "third_party"
    plugin_dir = _write_plugin(third_party / "demo_plugin", PLUGIN_V1, INDEX_V1)

    manager = PluginManager()
    manager._third_party_dir = third_party
    manager._config_path = tmp_path / "config.json"
    manager._save_config_json({"plugin_states": {"demo_plugin": False}})

    old = manager._load_plugin(plugin_dir, "third_party")
    assert old is not None
    assert old.enabled is False

    _write_plugin(plugin_dir, PLUGIN_V2, INDEX_V2)

    result = _run(manager._hot_reload_plugin("demo_plugin", plugin_dir, "2.0.0"))
    assert result["ok"] is True
    assert result["restart"] is False
    assert result["hot"] is True

    new = manager.plugins.get("demo_plugin")
    assert new is not None
    assert new is not old
    assert new.enabled is False
    assert new.runtime_ready is False, "禁用态插件不应触发 on_load"
    assert not plugin_dir.joinpath(".unloaded").exists(), "未就绪的旧实例不应调用 on_unload"