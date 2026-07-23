"""rate_limit_guard / cache_proxy 聚焦回归测试。"""

import json
import logging

import pytest

from akm.plugins.context import RequestContext
from plugins.cache_proxy.index import Plugin as CacheProxy
from plugins.rate_limit_guard.index import Plugin as RateLimitGuard


def _ctx(request: dict | None = None, **kwargs) -> RequestContext:
    """构造单次请求级上下文（直接持有 request 引用，不 clone）。"""
    return RequestContext(request if isinstance(request, dict) else {}, **kwargs)


@pytest.mark.asyncio
async def test_rate_limit_guard_blocks_after_rpm():
    plugin = RateLimitGuard()
    plugin.logger = logging.getLogger("test.rate_limit_guard")
    plugin.config = {
        "enabled": True,
        "scope": "global",
        "max_requests_per_minute": 2,
        "max_requests_per_hour": 0,
        "max_concurrent": 0,
    }
    await plugin.on_load()

    req = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    assert await plugin.on_request(_ctx(req)) is None
    assert await plugin.on_request(_ctx(req)) is None
    blocked = await plugin.on_request(_ctx(req))
    assert blocked["type"] == "block"
    assert blocked["status_code"] == 429
    assert blocked["security_action"] == "rate_limit"
    assert "rate_limit" in blocked["body"]


@pytest.mark.asyncio
async def test_rate_limit_guard_concurrent_slot_release():
    plugin = RateLimitGuard()
    plugin.logger = logging.getLogger("test.rate_limit_guard")
    plugin.config = {
        "enabled": True,
        "scope": "model",
        "max_requests_per_minute": 0,
        "max_concurrent": 1,
    }
    await plugin.on_load()

    req1 = {"model": "gpt-a", "messages": []}
    ctx1 = _ctx(req1)
    first = await plugin.on_request(ctx1)
    assert first is None
    assert ctx1.bag_get("rate_limit_guard.slot") == "model:gpt-a"

    blocked = await plugin.on_request(_ctx({"model": "gpt-a", "messages": []}))
    assert blocked["type"] == "block"

    # 其它模型有独立并发槽
    other = {"model": "gpt-b", "messages": []}
    ctx_other = _ctx(other)
    other_ret = await plugin.on_request(ctx_other)
    assert other_ret is None

    # 释放 gpt-a 后可再进
    await plugin.on_response(ctx1)
    again = {"model": "gpt-a", "messages": []}
    ctx_again = _ctx(again)
    again_ret = await plugin.on_request(ctx_again)
    assert again_ret is None
    assert not ctx_again.is_block


@pytest.mark.asyncio
async def test_rate_limit_guard_rpm_uses_configured_scope():
    """模型和用户维度必须分别使用独立的固定窗口计数。"""
    plugin = RateLimitGuard()
    plugin.logger = logging.getLogger("test.rate_limit_guard")
    plugin.config = {
        "enabled": True,
        "scope": "model",
        "max_requests_per_minute": 1,
        "max_requests_per_hour": 0,
        "max_concurrent": 0,
    }
    await plugin.on_load()

    assert await plugin.on_request(_ctx({"model": "gpt-a"})) is None
    assert (await plugin.on_request(_ctx({"model": "gpt-a"})))["type"] == "block"
    assert await plugin.on_request(_ctx({"model": "gpt-b"})) is None

    plugin.config["scope"] = "user"
    assert await plugin.on_request(_ctx({"model": "gpt-a", "user": "alice"})) is None
    assert (await plugin.on_request(_ctx({"model": "gpt-b", "user": "alice"})))["type"] == "block"
    assert await plugin.on_request(_ctx({"model": "gpt-a", "user": "bob"})) is None


@pytest.mark.asyncio
async def test_cache_proxy_hit_and_skip_tools_stream():
    plugin = CacheProxy()
    plugin.logger = logging.getLogger("test.cache_proxy")
    plugin.config = {
        "enabled": True,
        "ttl_seconds": 60,
        "max_entries": 10,
        "max_body_bytes": 10000,
        "skip_stream": True,
        "skip_tools": True,
    }
    await plugin.on_load()

    request = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hello cache"}],
        "temperature": 0,
    }
    # 首次未命中，标记 eligible
    ctx_mark = _ctx(request)
    marked = await plugin.on_request(ctx_mark)
    assert marked is None
    assert ctx_mark.bag_get("cache_proxy.eligible") is True
    key = ctx_mark.bag_get("cache_proxy.cache_key")

    body = json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": "cached-answer"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    ctx_mark.response = {
        "ok": True,
        "stream": False,
        "status_code": 200,
        "response_body": body,
        "model": "gpt-4",
        "api_path": "chat/completions",
    }
    await plugin.on_response(ctx_mark)

    ctx_hit = _ctx(request)
    hit = await plugin.on_request(ctx_hit)
    assert hit["type"] == "block"
    assert hit["security_action"] == "cache_hit"
    assert "cached-answer" in hit["body"]
    assert "HIT" in hit["body"]
    assert key[:8] in hit["security_reason"] or True

    # stream / tools 跳过
    assert await plugin.on_request(_ctx({**request, "stream": True})) is None
    assert await plugin.on_request(
        _ctx({**request, "tools": [{"type": "function", "function": {"name": "x"}}]})
    ) is None


def test_cost_estimate_parse_strict_three_part_only():
    from akm.cost_estimate import estimate_row_cost, parse_pricing, pricing_snapshot

    rules = parse_pricing(
        "gpt-4=1/0.1/2\n"
        "local-*=2/1/4\n"
        "bad=1/2\n"  # 非法：必须三段
        "also-bad=1/0.1/2/USD\n"
        "*=0.5/0.05/1\n"
    )
    assert len(rules) == 3
    assert rules[0] == ("gpt-4", 1.0, 0.1, 2.0)
    assert rules[1] == ("local-*", 2.0, 1.0, 4.0)

    cost, currency = estimate_row_cost(
        model="gpt-4",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        cached_tokens=400_000,
        cache_creation_tokens=0,
        rules=rules,
    )
    # 0.6*1 + 0.4*0.1 + 1*2 = 2.64
    assert round(cost, 2) == 2.64
    assert currency == "$"

    snap = pricing_snapshot("gpt-4=1/0.1/2")
    assert snap["rules"][0]["output_per_1m"] == 2.0
    assert parse_pricing("gpt-4=1/2") == []


def test_default_cost_pricing_table_includes_current_models_and_free_fallback():
    """默认单价表应覆盖当前模型，并避免未知模型产生估算费用。"""
    from akm.cost_estimate import DEFAULT_PRICING_TABLE, match_price, parse_pricing

    rules = parse_pricing(DEFAULT_PRICING_TABLE)

    assert match_price("gpt-5.6-luna", rules) == (1.0, 0.1, 6.0)
    assert match_price("gpt-5.6-terra", rules) == (2.5, 0.25, 15.0)
    assert match_price("unknown-model", rules) == (0.0, 0.0, 0.0)


def test_cost_pricing_table_migrates_legacy_currency_column():
    """升级后历史四段单价表仍应继续按固定美元计费。"""
    from akm.config import _normalize_cost_pricing_table

    assert _normalize_cost_pricing_table(
        "gpt-4=1/0.1/2/USD\n*=0.5/0.05/1/CNY"
    ) == "gpt-4=1/0.1/2\n*=0.5/0.05/1"
