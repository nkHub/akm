"""config.py 的 agent_config 归组行为测试。

归组约定：磁盘 config.json 中 agent 相关配置全部收进嵌套 ``agent_config`` 对象，
但内存层（load_config 返回值）始终保持扁平 agent_* 键，业务代码无感。
"""

import json

import pytest

import akm.config as cfg


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """把 CONFIG_DIR / CONFIG_PATH 指向临时目录，隔离真实配置。"""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr(cfg, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(cfg_dir / "config.json"))
    yield cfg_dir


def _read_raw(cfg_dir) -> dict:
    """直接读磁盘上的 config.json 原文。"""
    with open(cfg_dir / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def test_save_groups_all_agent_keys_into_nested_config(isolated_config):
    """save 后磁盘顶层不再有 agent_* 与 tavily_api_key，全部收进 agent_config。"""
    cfg.save_config({"agent_max_turns": 5, "http_proxy_enabled": True})
    raw = _read_raw(isolated_config)
    # 顶层不再散落 agent 相关键
    for key in ("agent_max_turns", "agent_write_tools_enabled", "tavily_api_key"):
        assert key not in raw, f"顶层不应再出现 {key}"
    # agent_config 含全部归组键
    assert set(raw["agent_config"].keys()) == set(cfg.AGENT_GROUP_KEYS)
    assert raw["agent_config"]["agent_max_turns"] == 5
    # 非 agent 键仍在顶层
    assert raw["http_proxy_enabled"] is True


def test_load_expands_agent_config_nested(isolated_config):
    """load 把嵌套 agent_config 展开到扁平键，并应用默认值。"""
    (isolated_config / "config.json").write_text(
        json.dumps({"agent_config": {"agent_max_turns": 7, "agent_email_enabled": True}}),
        encoding="utf-8",
    )
    cfg_loaded = cfg.load_config()
    assert cfg_loaded["agent_max_turns"] == 7
    assert cfg_loaded["agent_email_enabled"] is True
    # 未配置的归组键仍回默认值
    assert cfg_loaded["agent_git_enabled"] is False
    # 内存层保持扁平，不出现嵌套
    assert "agent_config" not in cfg_loaded


def test_load_nested_prefers_over_flat_legacy(isolated_config):
    """同时存在顶层旧键与嵌套时，嵌套优先（兼容用户既有顶层配置）。"""
    (isolated_config / "config.json").write_text(
        json.dumps({"agent_max_turns": 3, "agent_config": {"agent_max_turns": 9}}),
        encoding="utf-8",
    )
    cfg_loaded = cfg.load_config()
    assert cfg_loaded["agent_max_turns"] == 9


def test_save_accepts_nested_agent_config_data(isolated_config):
    """调用方直接传嵌套 agent_config 时，保存后不丢配置。"""
    cfg.save_config({"agent_config": {"agent_max_turns": 11, "agent_max_tool_calls": 22}})
    cfg_loaded = cfg.load_config()
    assert cfg_loaded["agent_max_turns"] == 11
    assert cfg_loaded["agent_max_tool_calls"] == 22


def test_save_roundtrip_keeps_nested_config(isolated_config):
    """load 后修改扁平键再 save，嵌套归组不丢。"""
    cfg.save_config({"agent_config": {"agent_max_turns": 13}})
    cfg_loaded = cfg.load_config()
    cfg_loaded["agent_max_turns"] = 15
    cfg.save_config(cfg_loaded)
    raw = _read_raw(isolated_config)
    assert raw["agent_config"]["agent_max_turns"] == 15
    assert "agent_max_turns" not in raw
    assert cfg.load_config()["agent_max_turns"] == 15


def test_load_defaults_when_file_missing(isolated_config):
    """配置文件不存在时返回默认值，且不出现嵌套键。"""
    cfg_loaded = cfg.load_config()
    assert cfg_loaded["agent_max_turns"] == 100
    assert "agent_config" not in cfg_loaded
