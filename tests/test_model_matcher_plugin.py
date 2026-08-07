import pytest
from unittest.mock import AsyncMock

from akm.plugins.context import RequestContext
from akm.plugins.model_matcher.index import Plugin


def _ctx(request: dict | None = None, *, key: dict | None = None, **kwargs) -> RequestContext:
    """构造 model_matcher 测试用请求上下文。

    注意：直接持有传入的 request 引用（不 clone），与生产侧
    RequestContext 行为一致，便于断言 in-place 改写。
    """
    ctx = RequestContext(request if isinstance(request, dict) else {}, **kwargs)
    if key is not None:
        ctx.key = key
    return ctx


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
    ctx = _ctx(req)
    out = await plugin.on_request(ctx)
    assert out is req
    assert req["model"] == "gpt-4.1"
    assert ctx.model == "gpt-4.1"


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
    out = await plugin.on_request(_ctx(req))
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
    out = await plugin.on_request(_ctx(req))
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
    out = await plugin.on_request(_ctx(req))
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
    out = await plugin.on_request(_ctx(req))
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
    out = await plugin.on_request(_ctx(req))
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

    ctx = _ctx(
        {"model": "gpt-5"},
        key={"alias": "k1", "provider": "openai"},
    )
    out = await plugin.on_key_selected(ctx)
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

    ctx = _ctx(
        {"model": "gpt-5"},
        key={"alias": "k1", "provider": "openai"},
    )
    out = await plugin.on_key_selected(ctx)
    assert out["alias"] == "k1"
    # 无替代候选时不再递增 in-flight，避免慢请求把计数永久推高造成拥塞误判累积
    assert plugin._inflight_counts["k1"] == 1
    # 同时不登记 inflight_key，on_response 不会对此请求做回收配对
    assert ctx.bag_get("model_matcher.inflight_key") is None


@pytest.mark.asyncio
async def test_model_matcher_on_response_recycles_inflight_count():
    plugin = Plugin()
    plugin.config = {}
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    plugin._inflight_counts["k1"] = 2
    plugin._inflight_oldest_ts["k1"] = 123.0

    ctx = _ctx({})
    ctx.response = {"key_alias": "k1"}
    # 模拟 on_key_selected 已登记 inflight_key，on_response 配对成功时回收一次
    ctx.bag_set("model_matcher.inflight_key", "k1")
    await plugin.on_response(ctx)
    assert plugin._inflight_counts["k1"] == 1

    # bag 已被清空，第二次调用不会重复回收
    assert ctx.bag_get("model_matcher.inflight_key") is None
    plugin._inflight_counts["k1"] = 1
    await plugin.on_response(ctx)
    assert plugin._inflight_counts["k1"] == 1


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

    ctx = _ctx(
        {"model": "gpt-5"},
        key={"alias": "k1", "provider": "openai"},
    )
    out = await plugin.on_key_selected(ctx)
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

    ctx = _ctx(
        {"model": "gpt-5"},
        key={"alias": "k1", "provider": "openai"},
    )
    out = await plugin.on_key_selected(ctx)
    assert out["alias"] == "k1"


@pytest.mark.asyncio
async def test_model_matcher_bypass_excludes_proxy_tried_aliases(monkeypatch):
    """普通旁路必须排除 proxy 主循环已失败的 key，避免反复选中导致空转。"""
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

    # 记录旁路选 key 时的 exclude 参数
    seen_exclude = []

    async def _pick(model, exclude_aliases=None):
        seen_exclude.append(list(exclude_aliases or []))
        # 模拟：候选 k2 已被 proxy 试过失败，因此不返回 k2，只返回全新的 k3
        return {"alias": "k3", "provider": "openai"}

    monkeypatch.setattr("akm.plugins.model_matcher.index.pick_key_async", _pick)

    ctx = _ctx(
        {"model": "gpt-5"},
        key={"alias": "k1", "provider": "openai"},
    )
    # proxy 已失败集合透传进 ctx.bag
    ctx.bag_set("proxy.tried_aliases", ["k2"])
    out = await plugin.on_key_selected(ctx)
    assert out["alias"] == "k3"
    # 旁路选 key 时必须排除 k1（当前）与 k2（已失败）
    assert seen_exclude and "k1" in seen_exclude[0] and "k2" in seen_exclude[0]


@pytest.mark.asyncio
async def test_model_matcher_bypass_keeps_current_when_only_tried_candidates(monkeypatch):
    """当旁路候选全部已被 proxy 试过失败时，应保持当前 key 而不是无限空转。"""
    plugin = Plugin()
    plugin.config = {
        "enable_inflight_bypass": True,
        "max_inflight_per_key": 1,
        "slow_inflight_threshold_sec": 60,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    plugin._inflight_counts["k1"] = 1

    # 候选 k2 已在 tried_aliases，pick_key_async 因 exclude 命中返回 None
    monkeypatch.setattr(
        "akm.plugins.model_matcher.index.pick_key_async",
        AsyncMock(return_value=None),
    )

    ctx = _ctx(
        {"model": "gpt-5"},
        key={"alias": "k1", "provider": "openai"},
    )
    ctx.bag_set("proxy.tried_aliases", ["k2"])
    out = await plugin.on_key_selected(ctx)
    assert out["alias"] == "k1"
    # 无替代候选时不再递增 in-flight，避免慢请求把计数永久推高造成拥塞误判累积
    assert plugin._inflight_counts["k1"] == 1
    # 同时不登记 inflight_key，on_response 不会对此请求做回收配对
    assert ctx.bag_get("model_matcher.inflight_key") is None


@pytest.mark.asyncio
async def test_model_matcher_smart_bypass_excludes_proxy_tried_aliases(monkeypatch):
    """智能旁路同样必须排除 proxy 已失败的 key，候选打分不得包含它们。"""
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

    plugin._inflight_counts["k1"] = 2
    plugin._health_stats["k1"] = {"ema_latency_ms": 500, "ema_error": 0.5, "last_error_ts": 0}
    # k2 已失败（proxy tried），k3 才是合法候选
    plugin._health_stats["k2"] = {"ema_latency_ms": 50, "ema_error": 0.0, "last_error_ts": 0}
    plugin._health_stats["k3"] = {"ema_latency_ms": 80, "ema_error": 0.0, "last_error_ts": 0}

    seen_exclude = []

    async def _pick(model, exclude_aliases=None):
        seen_exclude.append(list(exclude_aliases or []))
        # 只返回 k3（k2 已在 exclude 中不会返回）
        return {"alias": "k3", "provider": "openai"}

    monkeypatch.setattr("akm.plugins.model_matcher.index.pick_key_async", _pick)

    ctx = _ctx(
        {"model": "gpt-5"},
        key={"alias": "k1", "provider": "openai"},
    )
    ctx.bag_set("proxy.tried_aliases", ["k2"])
    out = await plugin.on_key_selected(ctx)
    assert out["alias"] == "k3"
    # 每次候选打分都必须同时排除 k1（当前）与 k2（已失败）
    for exclude in seen_exclude:
        assert "k1" in exclude and "k2" in exclude
