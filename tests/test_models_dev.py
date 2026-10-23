"""models.dev 目录数据与取价三段式的单元测试。

不联网：直接构造假 payload 走 :func:`akm.models_dev.filter_payload` 与入库流程。
"""

from __future__ import annotations

import json

import pytest

from akm import models_dev
from akm.cost_estimate import PriceResolver, match_price, parse_pricing
from akm.db import get_connection


def _model(
    *,
    input_price=1.0,
    cache_read=0.1,
    output=2.0,
    family="chat",
    output_modalities=("text",),
    max_output=8192,
    cost=True,
):
    """构造一条 models.dev 模型记录。"""
    item = {
        "id": "x",
        "name": "X",
        "family": family,
        "modalities": {"input": ["text"], "output": list(output_modalities)},
        "limit": {"context": 128000, "output": max_output},
        "reasoning": False,
        "tool_call": True,
    }
    if cost:
        item["cost"] = {
            "input": input_price,
            "cache_read": cache_read,
            "output": output,
        }
    else:
        item["cost"] = None
    return item


def _payload():
    """构造覆盖「正常 / 全 0 价 / 全 null / 非对话能力」的假数据。"""
    return {
        "alpha": {
            "id": "alpha",
            "name": "Alpha",
            "api": "https://alpha.example/v1",
            "doc": "https://alpha.example/docs",
            "npm": "@ai-sdk/openai-compatible",
            "models": {
                "a-chat": _model(),
                "a-embed": _model(
                    input_price=0.02,
                    cache_read=0.0,
                    output=0.0,
                    family="text-embedding",
                    max_output=1536,
                ),
                "a-image": _model(
                    input_price=5.0,
                    cache_read=1.25,
                    output=30.0,
                    family="gpt-image",
                    output_modalities=("image",),
                ),
            },
        },
        # 全 null 价 → 整条供应商剔除
        "beta": {
            "id": "beta",
            "name": "Beta",
            "models": {"b-chat": _model(cost=False)},
        },
        # 全 0 价 → 保留（免费 / 订阅渠道）
        "gamma": {
            "id": "gamma",
            "name": "Gamma",
            "models": {"a-chat": _model(input_price=0.0, cache_read=0.0, output=0.0)},
        },
        # 官方兜底候选
        "openai": {
            "id": "openai",
            "name": "OpenAI",
            "npm": "@ai-sdk/openai",
            "models": {"gpt-fallback": _model(input_price=3.0, cache_read=0.3, output=6.0)},
        },
    }


@pytest.fixture()
def synced(monkeypatch):
    """把假 payload 灌进隔离数据库，返回清理函数所需的句柄。"""
    payload = _payload()
    monkeypatch.setattr(
        models_dev, "fetch_remote", lambda etag="": (json.loads(json.dumps(payload)), "etag-1")
    )
    stats = models_dev.sync_from_remote(force=True)
    models_dev.invalidate_cache()
    yield stats
    # 清库，避免影响其它用例
    conn = get_connection()
    try:
        conn.execute("DELETE FROM models_dev_models")
        conn.execute("DELETE FROM models_dev_providers")
        conn.execute("DELETE FROM models_dev_meta")
        conn.commit()
    finally:
        conn.close()
    models_dev.invalidate_cache()


def test_filter_payload_keeps_zero_price_and_drops_null():
    """只剔 null：全 0 价供应商保留，全 null 供应商整条剔除。"""
    providers, models, dropped = models_dev.filter_payload(_payload())
    ids = {p["id"] for p in providers}
    assert "alpha" in ids
    assert "gamma" in ids, "全 0 价的供应商必须保留（免费/订阅渠道）"
    assert "beta" not in ids, "所有模型 cost 全为 null 的供应商应剔除"
    assert "beta" in dropped
    # alpha 3 条模型都有 cost（含 output=0 的 embedding），全部保留
    assert len(models) == 3 + 1 + 1


def test_sync_writes_rows_and_meta(synced):
    """同步成功时写入供应商/模型/meta 三类数据。"""
    assert synced["ok"] is True
    assert synced["skipped"] is False
    assert synced["provider_count"] == 3
    assert models_dev.meta_get("etag") == "etag-1"
    assert models_dev.meta_get("provider_count") == "3"
    assert models_dev.provider_exists("alpha") is True
    assert models_dev.provider_exists("beta") is False


def test_sync_skips_when_not_modified(monkeypatch, synced):
    """远端返回 304 时不动数据，只回报状态。"""
    monkeypatch.setattr(models_dev, "fetch_remote", lambda etag="": (None, etag or "etag-1"))
    stats = models_dev.sync_from_remote()
    assert stats["skipped"] is True
    assert stats["reason"] == "not_modified"
    assert stats["provider_count"] == 3


def test_resolve_price_strict_then_official_fallback(synced):
    """三段式：本供应商命中 → 官方兜底 → None。"""
    price, source = models_dev.resolve_price("alpha", "a-chat")
    assert price == (1.0, 0.1, 2.0)
    assert source == "models.dev:alpha"

    # gamma 也有同名模型，价格 0，说明价格按「供应商维度」取而不是全局
    price, source = models_dev.resolve_price("gamma", "a-chat")
    assert price == (0.0, 0.0, 0.0)

    # 供应商不存在 → 官方兜底命中 openai
    found = models_dev.resolve_price(
        "unknown-provider", "gpt-fallback", prefer_providers="openai"
    )
    assert found is not None
    assert found[0] == (3.0, 0.3, 6.0)
    assert "fallback" in found[1]

    # 关掉兜底 → 直接没有价格
    assert (
        models_dev.resolve_price(
            "unknown-provider", "gpt-fallback", official_fallback=False
        )
        is None
    )


def test_resolve_price_name_ladder_within_same_provider(synced):
    """名称阶梯只在同一供应商内部生效（vendor 前缀/日期后缀）。"""
    # alpha 下没有 deepseek/a-chat，但去前缀后能命中同名模型
    price, _ = models_dev.resolve_price("alpha", "alpha/a-chat")
    assert price == (1.0, 0.1, 2.0)


def test_is_chat_capable(synced):
    """对话能力判定：图像 / embedding 排除，纯文本模型保留。"""
    assert models_dev.is_chat_capable("a-chat") is True
    assert models_dev.is_chat_capable("a-image") is False, "output 不含 text"
    assert models_dev.is_chat_capable("a-embed") is False, "family 命中 embedding"
    # 没有元数据时退回名字规则：未命中排除词的保留
    assert models_dev.is_chat_capable("totally-unknown-model") is True
    assert models_dev.is_chat_capable("some-embedding-model") is False


def test_npm_protocol_hints_conservative():
    """npm 只给保守的初始建议，`/anthropic` 永远关。"""
    assert models_dev.npm_protocol_hints("@ai-sdk/openai") == {
        "supports_chat": True,
        "supports_responses": True,
        "supports_messages": False,
        "messages_use_anthropic_path": False,
    }
    compat = models_dev.npm_protocol_hints("@ai-sdk/openai-compatible")
    assert compat["supports_chat"] is True and compat["supports_responses"] is False
    anth = models_dev.npm_protocol_hints("@ai-sdk/anthropic")
    assert anth["supports_messages"] is True and anth["supports_chat"] is False
    # 未知 npm 保守只给 Chat
    assert models_dev.npm_protocol_hints("")["supports_chat"] is True


def test_price_resolver_only_snapshot_mode():
    """手写单价表已移除：PriceResolver 只有 snapshot 一种模式。

    老配置里的 manual / hybrid 会被归一化成 snapshot —— 否则升级后取价会静默
    变成 0（老表不再参与计算），用户只会看到「费用全是 0」而不知道原因。
    """
    for legacy_mode in ("manual", "hybrid", "snapshot", "", "bogus"):
        resolver = PriceResolver(mode=legacy_mode)
        assert resolver.mode == "snapshot"


def test_price_resolver_uses_catalog_only(synced):
    """目录命中时用目录价；手写表参数已从构造器移除，不可能再干扰取价。"""
    resolver = PriceResolver(mode="snapshot", official_fallback=True)
    # alpha 目录价
    assert resolver.resolve("alpha", "a-chat") == (1.0, 0.1, 2.0)
    # 目录也命中不到 → 0（不回落任何手写 * 行）
    assert resolver.resolve("alpha", "no-such-model") == (0.0, 0.0, 0.0)


def test_price_resolver_rejects_legacy_pricing_table_arg():
    """构造器不再接受 pricing_table 位置参数（防止老调用点悄悄继续传表）。"""
    import pytest

    with pytest.raises(TypeError):
        PriceResolver("a=1/0.1/2", mode="snapshot")


def test_resolve_price_family_fallback_when_official_renamed(monkeypatch):
    """官方改名后，第三方中转的旧名仍能取到价（按 family 回退）。

    真实场景：`deepseek` 官方目录里叫 `deepseek-flash`，而 trae-cn 等中转站
    沿用 `deepseek-v4.1-flash`。名字对不上时，若不按 family 回退，所有走中转站的
    请求费用都会被算成 $0 —— 用户只看到「免费」，实际在花钱。

    前提：旧名**必须出现在目录里某处**（通常是第三方供应商），否则拿不到 family
    元数据，也就无从回退 —— 这是已知边界，见测试尾部断言。
    """
    payload = {
        "official": {
            "id": "official",
            "name": "Official",
            "models": {
                # 官方稳定名
                "ds-flash": _model(input_price=0.15, cache_read=0.003, output=0.6, family="ds-flash"),
                # 同 family 的实验分支（价格相同但名字带 -exp，不该被优先选中）
                "ds-flash-exp": _model(input_price=0.15, cache_read=0.003, output=0.6, family="ds-flash"),
            },
        },
        # 第三方中转：保留旧名 ds-v4.1-flash（family 相同），这就是 family 元数据的来源
        "relay": {
            "id": "relay",
            "name": "Relay",
            "models": {
                "ds-v4.1-flash": _model(
                    input_price=1.0, cache_read=0.1, output=2.0, family="ds-flash"
                )
            },
        },
    }
    monkeypatch.setattr(
        models_dev, "fetch_remote", lambda etag="": (json.loads(json.dumps(payload)), "etag-x")
    )
    models_dev.sync_from_remote(force=True)
    models_dev.invalidate_cache()
    try:
        # 请求 relay 名下的模型 → 第一段就命中 relay 自己的价（不走回退）
        assert models_dev.resolve_price("relay", "ds-v4.1-flash") == (
            (1.0, 0.1, 2.0),
            "models.dev:relay",
        )
        # 请求一个目录里没有的供应商 → 官方兜底按名字找不到 → family 回退到 official/ds-flash
        found = models_dev.resolve_price(
            "nobody", "ds-v4.1-flash", official_fallback=True,
            prefer_providers=["official"],
        )
        assert found is not None
        price, source = found
        assert price == (0.15, 0.003, 0.6)
        assert "(family)" in source
        # 不能挑中带 -exp 的实验分支
        assert "-exp" not in source
    finally:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM models_dev_models")
            conn.execute("DELETE FROM models_dev_providers")
            conn.execute("DELETE FROM models_dev_meta")
            conn.commit()
        finally:
            conn.close()
        models_dev.invalidate_cache()


def test_resolve_price_family_fallback_disabled_when_official_fallback_off(monkeypatch):
    """关闭官方兜底后，family 回退也不该生效（否则开关形同虚设）。"""
    payload = {
        "official": {
            "id": "official",
            "name": "Official",
            "models": {"ds-flash": _model(input_price=0.15, cache_read=0.003, output=0.6, family="ds-flash")},
        },
    }
    monkeypatch.setattr(
        models_dev, "fetch_remote", lambda etag="": (json.loads(json.dumps(payload)), "etag-y")
    )
    models_dev.sync_from_remote(force=True)
    models_dev.invalidate_cache()
    try:
        assert (
            models_dev.resolve_price("nobody", "ds-v4.1-flash", official_fallback=False)
            is None
        )
    finally:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM models_dev_models")
            conn.execute("DELETE FROM models_dev_providers")
            conn.execute("DELETE FROM models_dev_meta")
            conn.commit()
        finally:
            conn.close()
        models_dev.invalidate_cache()


def test_normalize_api_base_strips_trailing_v1():
    """models.dev 的 api 有 80% 带 /v1，入库时统一去掉，语义变成「根地址」。

    为什么必须去：AKM 的 `Agent.resolve_url` 自己会按需补 /v1 ——
    带 /v1 走 `{base}/{path}`，不带走 `{base}/v1/{path}`，两种写法拼出的最终
    URL 完全一致。但把 /v1 原样塞进 Key 编辑页的 base_url 输入框会让用户困惑。
    """
    cases = {
        "https://openrouter.ai/api/v1": "https://openrouter.ai/api",
        "https://api.302.ai/v1": "https://api.302.ai",
        "https://api.deepseek.com": "https://api.deepseek.com",
        # 只剥末尾的 /v1，不动路径中间的
        "https://opencode.ai/inference/openai/v1": "https://opencode.ai/inference/openai",
        # 尾部斜杠要先处理掉
        "https://api.openai.com/v1/": "https://api.openai.com",
        "": "",
        # 不能误伤 /v1beta 与大小写变体
        "https://example.com/v1beta": "https://example.com/v1beta",
        "https://example.com/V1": "https://example.com/V1",
    }
    for raw, expected in cases.items():
        assert models_dev.normalize_api_base(raw) == expected


def test_normalize_api_base_keeps_resolve_url_equivalent():
    """去掉 /v1 后，Agent.resolve_url 拼出的最终 URL 必须与不去时完全一致。"""
    from akm.agent import Agent

    with_v1 = Agent(name="t", default_base_url="https://openrouter.ai/api/v1")
    without_v1 = Agent(name="t", default_base_url="https://openrouter.ai/api")
    for path in ("chat/completions", "responses"):
        assert with_v1.resolve_url({}, path) == without_v1.resolve_url({}, path)
