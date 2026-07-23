import os
import tempfile
import pytest
from akm.db import get_connection, init_db
from akm.key_pool import (
    _load_cipher,
    add_key,
    get_key,
    list_keys,
    remove_key,
    set_priority,
    set_status,
    mark_rate_limited,
    pick_key,
    clear_expired_rate_limits,
)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    """每个测试使用独立数据库和密钥目录"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool.SECRET_DIR", tmpdir)
    # 强制重新生成 cipher，确保使用新的 secret key
    monkeypatch.setattr("akm.key_pool._cipher", None)
    conn = get_connection()
    init_db(conn)
    yield conn
    conn.close()


def test_add_and_get_key(setup):
    add_key("test-key", "openai", "sk-abc123")
    key = get_key("test-key")
    assert key["alias"] == "test-key"
    assert key["provider"] == "openai"
    assert key["api_key"] == "sk-abc123"
    assert key["status"] == "active"
    assert key["priority"] == 0
    assert key["models"] == "*"
    assert key["provider_models"] == []
    assert key["usage_query_interval_m"] == 0


def test_add_duplicate_key_raises(setup):
    add_key("dup", "openai", "sk-xxx")
    with pytest.raises(ValueError, match="已存在"):
        add_key("dup", "openai", "sk-yyy")


def test_list_keys(setup):
    add_key("k1", "openai", "sk-a")
    add_key("k2", "deepseek", "sk-b", priority=5)
    keys = list_keys()
    assert len(keys) == 2
    assert keys[0]["alias"] == "k1"   # 按 priority 排序，k1=0 在前
    assert keys[1]["alias"] == "k2"


def test_list_keys_by_provider(setup):
    add_key("k1", "openai", "sk-a")
    add_key("k2", "deepseek", "sk-b")
    keys = list_keys(provider="deepseek")
    assert len(keys) == 1
    assert keys[0]["alias"] == "k2"


def test_remove_key(setup):
    add_key("rm", "openai", "sk-x")
    assert remove_key("rm") is True
    assert get_key("rm") is None


def test_remove_nonexistent_key(setup):
    assert remove_key("no-such") is False


def test_set_priority(setup):
    add_key("p", "openai", "sk-x")
    set_priority("p", 10)
    assert get_key("p")["priority"] == 10


def test_set_status(setup):
    add_key("s", "openai", "sk-x")
    set_status("s", "disabled")
    assert get_key("s")["status"] == "disabled"


def test_rate_limited_tracking(setup):
    """mark_rate_limited 记录冷却时间，pick_key 会跳过冷却中的 key"""
    add_key("rl", "openai", "sk-x")
    mark_rate_limited("rl")
    assert get_key("rl")["status"] == "rate_limited"
    # 冷却中不应被选中
    key = pick_key(model="gpt-4")
    assert key is None


def test_pick_key_by_model(setup):
    add_key("openai-k", "openai", "sk-a", models="gpt-4,gpt-3.5")
    add_key("deepseek-k", "deepseek", "sk-b", models="deepseek-chat")
    # 请求 gpt-4 应选中 openai key
    key = pick_key(model="gpt-4")
    assert key is not None
    assert key["alias"] == "openai-k"
    # 请求 deepseek-chat 应选中 deepseek key
    key = pick_key(model="deepseek-chat")
    assert key is not None
    assert key["alias"] == "deepseek-k"


def test_pick_key_skips_disabled(setup):
    add_key("a", "openai", "sk-a", priority=0, provider_models=["gpt-4"])
    add_key("b", "openai", "sk-b", priority=1, provider_models=["gpt-4"])
    set_status("a", "disabled")
    key = pick_key(model="gpt-4")
    assert key["alias"] == "b"


def test_pick_key_wildcard_models(setup):
    add_key("w", "openai", "sk-w", models="*", provider_models=["any-model"])
    key = pick_key(model="any-model")
    assert key is not None
    assert key["alias"] == "w"


def test_pick_key_wildcard_without_provider_models_no_longer_matches(setup):
    add_key("w", "openai", "sk-w", models="*")
    assert pick_key(model="any-model") is None


def test_pick_key_wildcard_uses_provider_models_when_present(setup):
    add_key(
        "w",
        "openai",
        "sk-w",
        models="*",
        provider_models=["gpt-4.1", "gpt-4.1-mini"],
    )
    key = pick_key(model="gpt-4.1")
    assert key is not None
    assert key["alias"] == "w"
    assert pick_key(model="gpt-5.9") is None


def test_pick_key_prefers_exact_match_before_wildcard_provider_models(setup):
    add_key("wild", "openai", "sk-w", models="*", provider_models=["gpt-4"])
    add_key("exact", "openai", "sk-e", models="gpt-4", priority=0)
    key = pick_key(model="gpt-4")
    assert key is not None
    assert key["alias"] == "exact"
