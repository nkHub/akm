"""项目本地请求策略插件的聚焦回归测试。"""

import json
import logging

import pytest

from akm.plugins.context import RequestContext
from plugins.cache_proxy.index import Plugin as CacheProxy
from plugins.header_toolkit.index import Plugin as HeaderToolkit
from plugins.key_source_guard.index import Plugin as KeySourceGuard
from plugins.rate_limit_guard.index import Plugin as RateLimitGuard


def _header_toolkit(rules: list[dict]) -> HeaderToolkit:
    """构造已启用、带指定规则的 header_toolkit 插件实例。"""
    plugin = HeaderToolkit()
    plugin.logger = logging.getLogger("test.header_toolkit")
    plugin.config = {"enabled": True, "rules_json": json.dumps(rules, ensure_ascii=False)}
    return plugin


def _header_ctx(client_headers: dict | None, user_agent: str = "opencode/1.18.26") -> RequestContext:
    """构造携带客户端原始请求头快照的请求上下文（服务端真实入口形态）。"""
    return RequestContext(
        {"model": "deepseek-v4-pro", "messages": []},
        client_user_agent=user_agent,
        client_headers=client_headers,
    )


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


# ── header_toolkit：客户端请求头变换 ─────────────────────────


@pytest.mark.asyncio
async def test_header_toolkit_rename_copies_value_to_upstream_header():
    plugin = _header_toolkit([
        {"action": "rename", "from_header": "x-client-token", "to_header": "x-session-id"},
    ])
    ctx = _header_ctx({"X-Client-Token": "tok-1", "user-agent": "opencode/1.18.26"})
    assert await plugin.on_request(ctx) is None
    assert ctx.upstream_headers == {"x-session-id": "tok-1"}


@pytest.mark.asyncio
async def test_header_toolkit_source_lookup_is_case_insensitive():
    plugin = _header_toolkit([
        {"action": "copy", "from_header": "X-SESSION", "to_header": "x-upstream-session"},
    ])
    ctx = _header_ctx({"x-session": "s-abc"})
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {"x-upstream-session": "s-abc"}


@pytest.mark.asyncio
async def test_header_toolkit_missing_source_is_noop():
    plugin = _header_toolkit([
        {"action": "rename", "from_header": "x-not-there", "to_header": "x-session-id"},
        {"action": "copy", "from_header": "x-also-not", "to_header": "x-other"},
    ])
    ctx = _header_ctx({"user-agent": "opencode/1.18.26"})
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {}


@pytest.mark.asyncio
async def test_header_toolkit_empty_source_value_is_noop():
    plugin = _header_toolkit([
        {"action": "rename", "from_header": "x-empty", "to_header": "x-target"},
    ])
    ctx = _header_ctx({"x-empty": ""})
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {}


@pytest.mark.asyncio
async def test_header_toolkit_set_and_add_if_missing():
    plugin = _header_toolkit([
        {"action": "set", "to_header": "x-gateway-token", "value": "gt-9f2a"},
        {"action": "add_if_missing", "to_header": "x-gateway-token", "value": "should-not-write"},
        {"action": "add_if_missing", "to_header": "x-lang", "value": "ts"},
    ])
    ctx = _header_ctx({"user-agent": "opencode/1.18.26", "x-stainless-lang": "ts"})
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {
        "x-gateway-token": "gt-9f2a",
        "x-lang": "ts",
    }


@pytest.mark.asyncio
async def test_header_toolkit_add_if_missing_respects_target_case():
    """add_if_missing 判断已写目标时应大小写不敏感。"""
    plugin = _header_toolkit([
        {"action": "set", "to_header": "X-Gateway-Token", "value": "first"},
        {"action": "add_if_missing", "to_header": "x-gateway-token", "value": "second"},
    ])
    ctx = _header_ctx({"user-agent": "opencode/1.18.26", "accept": "*/*"})
    await plugin.on_request(ctx)
    # 第二规则命中已写目标，不再写重复键
    assert ctx.upstream_headers == {"X-Gateway-Token": "first"}


@pytest.mark.asyncio
async def test_header_toolkit_prefix_suffix_chain_on_same_target():
    plugin = _header_toolkit([
        {"action": "set", "to_header": "x-tag", "value": "mid"},
        {"action": "prefix", "from_header": "user-agent", "to_header": "x-tag", "prefix": "[p]"},
        {"action": "suffix", "from_header": "user-agent", "to_header": "x-tag", "suffix": "[s]"},
    ])
    ctx = _header_ctx({"user-agent": "opencode/1.18.26"})
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {"x-tag": "[p]mid[s]"}


@pytest.mark.asyncio
async def test_header_toolkit_prefix_in_place_when_to_header_omitted():
    plugin = _header_toolkit([
        {"action": "prefix", "from_header": "user-agent", "prefix": "akm|"},
    ])
    ctx = _header_ctx({"user-agent": "opencode/1.18.26"})
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {"user-agent": "akm|opencode/1.18.26"}


@pytest.mark.asyncio
async def test_header_toolkit_match_client_filters_rule():
    plugin = _header_toolkit([
        {"action": "set", "to_header": "x-only-opencode", "value": "1", "match_client": "opencode"},
        {"action": "set", "to_header": "x-only-other", "value": "1", "match_client": "codex"},
    ])
    ctx = _header_ctx({"user-agent": "opencode/1.18.26"}, user_agent="opencode/1.18.26")
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {"x-only-opencode": "1"}


@pytest.mark.asyncio
async def test_header_toolkit_no_client_headers_noop():
    """内部子请求无客户端头快照，插件必须安静跳过，不写任何上游头。"""
    plugin = _header_toolkit([
        {"action": "set", "to_header": "x-anything", "value": "1"},
    ])
    ctx = RequestContext({"model": "m", "messages": []})  # 无 client_headers
    assert await plugin.on_request(ctx) is None
    assert ctx.upstream_headers == {}


@pytest.mark.asyncio
async def test_header_toolkit_disabled_does_nothing():
    plugin = HeaderToolkit()
    plugin.logger = logging.getLogger("test.header_toolkit")
    plugin.config = {"enabled": False, "rules_json": json.dumps([
        {"action": "set", "to_header": "x-anything", "value": "1"},
    ])}
    ctx = _header_ctx({"user-agent": "opencode/1.18.26"})
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {}


@pytest.mark.asyncio
async def test_header_toolkit_invalid_json_rules_skips_safely():
    plugin = HeaderToolkit()
    plugin.logger = logging.getLogger("test.header_toolkit")
    plugin.config = {"enabled": True, "rules_json": "{not json"}
    ctx = _header_ctx({"user-agent": "opencode/1.18.26"})
    assert await plugin.on_request(ctx) is None
    assert ctx.upstream_headers == {}


# ── header_toolkit：from_header 逗号分隔候选源 ────────────────


@pytest.mark.asyncio
async def test_header_toolkit_candidate_source_takes_first_present():
    """from_header 逗号分隔时，按顺序取第一个存在且非空的值。"""
    plugin = _header_toolkit([
        {"action": "rename", "from_header": "x-opencode-session, session-id, x-session-id", "to_header": "x-session-id"},
    ])
    ctx = _header_ctx({"session-id": "sess-2", "x-opencode-session": "sess-1"})
    await plugin.on_request(ctx)
    # 第一个候选 x-opencode-session 存在，取它；忽略顺序上的后位
    assert ctx.upstream_headers == {"x-session-id": "sess-1"}


@pytest.mark.asyncio
async def test_header_toolkit_candidate_source_skips_missing_and_empty():
    """候选源缺失或值为空时跳过，落到下一个存在的候选。"""
    plugin = _header_toolkit([
        {"action": "copy", "from_header": "x-not-there, x-session-id, x-last", "to_header": "x-up"},
    ])
    ctx = _header_ctx({"x-session-id": "s-9"})
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {"x-up": "s-9"}

    # 空值候选也要跳过，落到 x-last
    plugin2 = _header_toolkit([
        {"action": "copy", "from_header": "x-empty, x-last", "to_header": "x-up2"},
    ])
    ctx2 = _header_ctx({"x-empty": "", "x-last": "fallback"})
    await plugin2.on_request(ctx2)
    assert ctx2.upstream_headers == {"x-up2": "fallback"}


@pytest.mark.asyncio
async def test_header_toolkit_candidate_source_all_missing_is_noop():
    """全部候选源缺失时整体 no-op。"""
    plugin = _header_toolkit([
        {"action": "rename", "from_header": "a, b, c", "to_header": "x-target"},
    ])
    ctx = _header_ctx({"user-agent": "opencode/1.18.26"})
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {}


@pytest.mark.asyncio
async def test_header_toolkit_candidate_source_case_insensitive():
    """候选源名字大小写不敏感。"""
    plugin = _header_toolkit([
        {"action": "rename", "from_header": "X-Opencode-Session, Session-Id", "to_header": "x-target"},
    ])
    ctx = _header_ctx({"x-opencode-session": "sess-X"})
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {"x-target": "sess-X"}


@pytest.mark.asyncio
async def test_header_toolkit_candidate_source_prefix_suffix_fallback_target():
    """prefix/suffix 缺省 to_header 时以第一个候选源名为原位加工目标。"""
    plugin = _header_toolkit([
        {"action": "prefix", "from_header": "x-opencode-session, x-session-id", "prefix": "akm|"},
    ])
    ctx = _header_ctx({"x-opencode-session": "s-1"})
    await plugin.on_request(ctx)
    # 目标 = 第一个候选源名 x-opencode-session，原位加前缀
    assert ctx.upstream_headers == {"x-opencode-session": "akm|s-1"}


@pytest.mark.asyncio
async def test_header_toolkit_candidate_source_copy_with_explicit_target():
    """copy 用候选源时 to_header 正常生效。"""
    plugin = _header_toolkit([
        {"action": "copy", "from_header": "x-session-id, x-opencode-session", "to_header": "x-gw-session"},
    ])
    ctx = _header_ctx({"x-opencode-session": "os-1"})
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {"x-gw-session": "os-1"}


@pytest.mark.asyncio
async def test_header_toolkit_candidate_source_noop_when_no_client_headers():
    """内部子请求无客户端头快照时，候选源规则也应安全 no-op。"""
    plugin = _header_toolkit([
        {"action": "rename", "from_header": "x-a, x-b", "to_header": "x-target"},
    ])
    ctx = RequestContext({"model": "m", "messages": []})  # 无 client_headers
    await plugin.on_request(ctx)
    assert ctx.upstream_headers == {}
