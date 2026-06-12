import pytest
from unittest.mock import AsyncMock

from akm.plugins.model_matcher.index import Plugin


@pytest.mark.asyncio
async def test_model_matcher_applies_explicit_alias():
    plugin = Plugin()
    plugin.config = {"aliases": "gpt-4=gpt-4.1"}
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = await plugin.on_request(req)
    assert out is req
    assert req["model"] == "gpt-4.1"


@pytest.mark.asyncio
async def test_model_matcher_keeps_request_when_no_aliases_configured():
    plugin = Plugin()
    plugin.config = {"aliases": ""}
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = await plugin.on_request(req)
    assert out is None
    assert req["model"] == "gpt-4"


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


@pytest.mark.asyncio
async def test_model_matcher_smart_bypass_picks_best_scored_candidate(monkeypatch):
    plugin = Plugin()
    plugin.config = {
        "enable_inflight_bypass": True,
        "enable_smart_bypass": True,
        "max_inflight_per_key": 1,
        "slow_inflight_threshold_sec": 10,
        "smart_bypass_candidate_pool": 3,
        "smart_bypass_min_improve": 0.01,
        "smart_bypass_error_cooldown_sec": 30,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    # 当前 key 拥塞且历史较差
    plugin._inflight_counts["k1"] = 2
    plugin._health_stats["k1"] = {"ema_latency_ms": 500, "ema_error": 0.5, "last_error_ts": 0}
    # 候选 k2 一般，k3 更优
    plugin._health_stats["k2"] = {"ema_latency_ms": 200, "ema_error": 0.2, "last_error_ts": 0}
    plugin._health_stats["k3"] = {"ema_latency_ms": 80, "ema_error": 0.0, "last_error_ts": 0}

    seq = [
        {"alias": "k2", "provider": "openai"},
        {"alias": "k3", "provider": "openai"},
        None,
    ]

    async def _pick(model, exclude_aliases=None):
        return seq.pop(0)

    monkeypatch.setattr("akm.plugins.model_matcher.index.pick_key_async", _pick)

    out = await plugin.on_key_selected(
        model="gpt-5",
        key={"alias": "k1", "provider": "openai"},
        request={"model": "gpt-5"},
    )
    assert out["alias"] == "k3"


@pytest.mark.asyncio
async def test_model_matcher_smart_bypass_keeps_current_when_improve_not_enough(monkeypatch):
    plugin = Plugin()
    plugin.config = {
        "enable_inflight_bypass": True,
        "enable_smart_bypass": True,
        "max_inflight_per_key": 1,
        "slow_inflight_threshold_sec": 10,
        "smart_bypass_candidate_pool": 2,
        "smart_bypass_min_improve": 2.0,
        "smart_bypass_error_cooldown_sec": 30,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    plugin._inflight_counts["k1"] = 1
    plugin._health_stats["k1"] = {"ema_latency_ms": 200, "ema_error": 0.1, "last_error_ts": 0}
    plugin._health_stats["k2"] = {"ema_latency_ms": 180, "ema_error": 0.1, "last_error_ts": 0}

    seq = [
        {"alias": "k2", "provider": "openai"},
        None,
    ]

    async def _pick(model, exclude_aliases=None):
        return seq.pop(0)

    monkeypatch.setattr("akm.plugins.model_matcher.index.pick_key_async", _pick)

    out = await plugin.on_key_selected(
        model="gpt-5",
        key={"alias": "k1", "provider": "openai"},
        request={"model": "gpt-5"},
    )
    assert out["alias"] == "k1"
