import pytest
from unittest.mock import AsyncMock

from akm.plugins.model_matcher.index import Plugin


@pytest.mark.asyncio
async def test_model_matcher_sets_required_tool_choice_for_gpt_when_enabled():
    plugin = Plugin()
    plugin.config = {"force_tool_choice_required_for_gpt": True}
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "请运行测试并修复失败"}],
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}],
    }
    out = await plugin.on_request(req)
    assert out["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_model_matcher_does_not_override_explicit_tool_choice():
    plugin = Plugin()
    plugin.config = {"force_tool_choice_required_for_gpt": True}
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}],
        "tool_choice": "auto",
    }
    out = await plugin.on_request(req)
    assert out is None
    assert req["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_model_matcher_respects_disable_flag_for_tool_choice_policy():
    plugin = Plugin()
    plugin.config = {"force_tool_choice_required_for_gpt": False}
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "请运行测试并修复失败"}],
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}],
    }
    out = await plugin.on_request(req)
    assert out is None
    assert "tool_choice" not in req


@pytest.mark.asyncio
async def test_model_matcher_does_not_force_tool_choice_for_small_talk():
    plugin = Plugin()
    plugin.config = {"force_tool_choice_required_for_gpt": True}
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "你好"}],
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}],
    }
    out = await plugin.on_request(req)
    assert out is None
    assert "tool_choice" not in req


@pytest.mark.asyncio
async def test_model_matcher_bypass_switches_to_alternate_key(monkeypatch):
    plugin = Plugin()
    plugin.config = {
        "enable_inflight_bypass": True,
        "max_inflight_per_key": 2,
        "slow_inflight_threshold_sec": 60,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    # 预置当前 key 已拥塞，触发旁路
    plugin._inflight_counts["k1"] = 2

    monkeypatch.setattr(
        "akm.plugins.model_matcher.index.pick_key_async",
        AsyncMock(return_value={"alias": "k2", "provider": "openai"}),
    )

    out = await plugin.on_key_selected(
        model="gpt-5",
        key={"alias": "k1", "provider": "openai"},
        request={"model": "gpt-5"},
    )
    assert out["alias"] == "k2"
    assert plugin._inflight_counts["k2"] == 1


@pytest.mark.asyncio
async def test_model_matcher_bypass_falls_back_when_no_alternate(monkeypatch):
    plugin = Plugin()
    plugin.config = {
        "enable_inflight_bypass": True,
        "max_inflight_per_key": 1,
        "slow_inflight_threshold_sec": 60,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    plugin._inflight_counts["k1"] = 1

    monkeypatch.setattr(
        "akm.plugins.model_matcher.index.pick_key_async",
        AsyncMock(return_value=None),
    )

    out = await plugin.on_key_selected(
        model="gpt-5",
        key={"alias": "k1", "provider": "openai"},
        request={"model": "gpt-5"},
    )
    assert out["alias"] == "k1"
    # 仍应正常登记 in-flight
    assert plugin._inflight_counts["k1"] >= 1


@pytest.mark.asyncio
async def test_model_matcher_on_response_recycles_inflight_count():
    plugin = Plugin()
    plugin.config = {}
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    plugin._inflight_counts["k1"] = 2
    plugin._inflight_oldest_ts["k1"] = 123.0

    await plugin.on_response({}, {"key_alias": "k1"})
    assert plugin._inflight_counts["k1"] == 1

    await plugin.on_response({}, {"key_alias": "k1"})
    assert "k1" not in plugin._inflight_counts
    assert "k1" not in plugin._inflight_oldest_ts
