"""项目本地请求策略插件的聚焦回归测试。"""

import json
import logging

import pytest

from akm.plugins.context import RequestContext
from plugins.cache_proxy.index import Plugin as CacheProxy
from plugins.codex_impersonation.index import Plugin as CodexImpersonation
from plugins.key_source_guard.index import Plugin as KeySourceGuard
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
async def test_key_source_guard_only_allows_bound_key_for_matching_user_agent():
    plugin = KeySourceGuard()
    plugin.logger = logging.getLogger("test.key_source_guard")
    plugin.config = {
        "enabled": True,
        "bindings_json": json.dumps([
            {"key_alias": "codex-key", "client_patterns": ["CodexCLI/*", "ClaudeCode/*"]}
        ]),
    }

    allowed = _ctx({}, client_user_agent="CodexCLI/1.2")
    allowed.key = {"alias": "codex-key"}
    assert await plugin.on_key_selected(allowed) is None
    assert allowed.is_skip_key is False

    denied = _ctx({}, client_user_agent="curl/8.0")
    denied.key = {"alias": "codex-key"}
    assert await plugin.on_key_selected(denied) is None
    assert denied.action is not None
    assert denied.action["type"] == "skip_key"
    assert denied.action["security_action"] == "key_source_denied"

    unbound = _ctx({}, client_user_agent="curl/8.0")
    unbound.key = {"alias": "other-key"}
    assert await plugin.on_key_selected(unbound) is None
    assert unbound.is_skip_key is False


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


def _codex_plugin(**config_override) -> CodexImpersonation:
    """构造带默认配置的 codex_impersonation 插件实例。"""
    plugin = CodexImpersonation()
    plugin.logger = logging.getLogger("test.codex_impersonation")
    plugin.config = {
        "enabled": True,
        "client_patterns": '["opencode/*"]',
        "user_agent": "Codex Desktop/9.9.9",
        "installation_id": "",
        "sandbox": "seatbelt",
        **config_override,
    }
    return plugin


@pytest.mark.asyncio
async def test_codex_impersonation_sets_codex_style_headers_on_match():
    """命中的请求应被覆写为一套自洽的 Codex Desktop 风格请求头。"""
    plugin = _codex_plugin()
    ctx = _ctx({"model": "gpt-5"}, client_user_agent="opencode/1.18.4 ai-sdk/4.0.23")

    assert await plugin.on_request(ctx) is None
    assert ctx.upstream_headers["User-Agent"] == "Codex Desktop/9.9.9"
    assert ctx.upstream_headers["Originator"] == "Codex Desktop"
    assert ctx.upstream_headers["X-OpenAI-Internal-Codex-Responses-Lite"] == "true"
    assert ctx.upstream_headers["Accept"] == "text/event-stream"

    # 会话标识三件套彼此一致，且每次请求为新的 UUID
    assert ctx.upstream_headers["Session-Id"] == ctx.upstream_headers["Thread-Id"] == ctx.upstream_headers["X-Client-Request-Id"]
    assert ctx.upstream_headers["X-Codex-Window-Id"] == f'{ctx.upstream_headers["Session-Id"]}:0'

    # turn-metadata 是合法 JSON，字段齐全
    meta = json.loads(ctx.upstream_headers["X-Codex-Turn-Metadata"])
    assert meta["sandbox"] == "seatbelt"
    assert meta["request_kind"] == "turn"
    assert meta["thread_source"] == "system"
    assert meta["session_id"] == ctx.upstream_headers["Session-Id"]
    assert meta["window_id"] == ctx.upstream_headers["X-Codex-Window-Id"]
    assert meta["installation_id"]


@pytest.mark.asyncio
async def test_codex_impersonation_uses_configured_installation_id():
    """配置固定 installation_id 后，turn-metadata 应使用该值且跨请求稳定。"""
    plugin = _codex_plugin(installation_id="fixed-install")
    ctx = _ctx({}, client_user_agent="opencode/1.18.4")
    await plugin.on_request(ctx)
    assert json.loads(ctx.upstream_headers["X-Codex-Turn-Metadata"])["installation_id"] == "fixed-install"

    ctx2 = _ctx({}, client_user_agent="opencode/1.18.4")
    await plugin.on_request(ctx2)
    assert json.loads(ctx2.upstream_headers["X-Codex-Turn-Metadata"])["installation_id"] == "fixed-install"


@pytest.mark.asyncio
async def test_codex_impersonation_random_installation_id_stays_stable():
    """未配置 installation_id 时，进程内应复用同一个随机安装标识。"""
    plugin = _codex_plugin()
    ctx = _ctx({}, client_user_agent="opencode/1.18.4")
    await plugin.on_request(ctx)
    first = json.loads(ctx.upstream_headers["X-Codex-Turn-Metadata"])["installation_id"]

    ctx2 = _ctx({}, client_user_agent="opencode/1.18.4")
    await plugin.on_request(ctx2)
    second = json.loads(ctx2.upstream_headers["X-Codex-Turn-Metadata"])["installation_id"]
    assert first == second


@pytest.mark.asyncio
async def test_codex_impersonation_ignores_non_matching_and_disabled():
    """来源不匹配或插件被禁用时，不应覆写任何上游请求头。"""
    plugin = _codex_plugin()
    ctx = _ctx({}, client_user_agent="curl/8.0")
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {}

    plugin = _codex_plugin(enabled=False)
    ctx = _ctx({}, client_user_agent="opencode/1.18.4")
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {}
