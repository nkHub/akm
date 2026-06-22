"""Agent 和 Adapter 单元测试"""

import pytest
from akm.agent import (
    Agent, AGENT_REGISTRY, get_agent, get_agent_profile,
    register_agent, unregister_agent, list_agents, load_custom_agents,
)


# ───────────────────────────────────────────────
# Agent 基本属性
# ───────────────────────────────────────────────

def test_agent_defaults():
    agent = Agent(name="test", default_base_url="https://test.api.com")
    assert agent.name == "test"
    assert agent.default_base_url == "https://test.api.com"
    assert agent.default_auth_header == "Bearer {api_key}"
    assert agent.supports_chat is True
    assert agent.supports_responses is False
    assert agent.supports_messages is False
    assert agent.messages_use_anthropic_path is False


# ───────────────────────────────────────────────
# resolve_url
# ───────────────────────────────────────────────

def test_resolve_url_with_key_base_url():
    agent = Agent(name="openai", default_base_url="https://api.openai.com")
    key = {"base_url": "https://custom.api.com/v1"}
    url = agent.resolve_url(key, "chat/completions")
    assert url == "https://custom.api.com/v1/chat/completions"


def test_resolve_url_with_key_base_url_no_v1():
    agent = Agent(name="openai", default_base_url="https://api.openai.com")
    key = {"base_url": "https://custom.api.com"}
    url = agent.resolve_url(key, "chat/completions")
    assert url == "https://custom.api.com/v1/chat/completions"


def test_resolve_url_with_empty_key_base_url():
    """key 的 base_url 为空字符串时使用 agent 默认值"""
    agent = Agent(name="openai", default_base_url="https://api.openai.com")
    key = {"base_url": ""}
    url = agent.resolve_url(key, "chat/completions")
    assert url == "https://api.openai.com/v1/chat/completions"


def test_resolve_url_with_none_key_base_url():
    """key 没有 base_url 字段时使用 agent 默认值"""
    agent = Agent(name="deepseek", default_base_url="https://api.deepseek.com")
    key = {}
    url = agent.resolve_url(key, "chat/completions")
    assert url == "https://api.deepseek.com/v1/chat/completions"


def test_resolve_url_default_already_has_v1():
    agent = Agent(name="openai", default_base_url="https://api.openai.com/v1")
    key = {}
    url = agent.resolve_url(key, "chat/completions")
    assert url == "https://api.openai.com/v1/chat/completions"


def test_resolve_url_responses_path():
    agent = Agent(name="openai", default_base_url="https://api.openai.com")
    key = {}
    url = agent.resolve_url(key, "responses")
    assert url == "https://api.openai.com/v1/responses"


def test_resolve_url_messages_with_anthropic_path_switch():
    """开启开关后，messages 路径自动补到 /anthropic/v1/messages。"""
    agent = Agent(
        name="vendor",
        default_base_url="https://vendor.example.com",
        supports_messages=True,
        messages_use_anthropic_path=True,
    )
    key = {}
    url = agent.resolve_url(key, "messages")
    assert url == "https://vendor.example.com/anthropic/v1/messages"


def test_resolve_url_messages_without_anthropic_path_switch():
    """未开启开关时，messages 仍走普通 /v1/messages。"""
    agent = Agent(
        name="vendor",
        default_base_url="https://vendor.example.com",
        supports_messages=True,
    )
    key = {}
    url = agent.resolve_url(key, "messages")
    assert url == "https://vendor.example.com/v1/messages"


# ───────────────────────────────────────────────
# build_headers
# ───────────────────────────────────────────────

def test_build_headers_default():
    agent = Agent(name="openai", default_base_url="https://api.openai.com")
    key = {"api_key": "sk-test123"}
    headers = agent.build_headers(key)
    assert headers["Authorization"] == "Bearer sk-test123"
    assert headers["Content-Type"] == "application/json"


def test_build_headers_uses_native_user_agent_when_enabled(monkeypatch):
    """开启 use_native_user_agent 后，应优先透传原始 User-Agent。"""
    monkeypatch.setattr("akm.agent.config_get", lambda key, default=None: True if key == "use_native_user_agent" else default)
    agent = Agent(name="openai", default_base_url="https://api.openai.com")
    key = {"api_key": "sk-test123"}
    headers = agent.build_headers(key, original_user_agent="Claude-Code/1.2.3")
    assert headers["User-Agent"] == "Claude-Code/1.2.3"


def test_build_headers_falls_back_to_akm_user_agent_without_native_value(monkeypatch):
    """即使开启 use_native_user_agent，没有原始值时也应回退到 akm 版本标识。"""
    monkeypatch.setattr("akm.agent.config_get", lambda key, default=None: True if key == "use_native_user_agent" else default)
    agent = Agent(name="openai", default_base_url="https://api.openai.com")
    key = {"api_key": "sk-test123"}
    headers = agent.build_headers(key, original_user_agent="")
    assert headers["User-Agent"].startswith("akm/")


def test_build_headers_special_request_uses_default_akm_user_agent(monkeypatch):
    """特殊请求在未透传原始 User-Agent 时，也应统一使用 akm/<version> 标识。"""
    monkeypatch.setattr("akm.agent.config_get", lambda key, default=None: False if key == "use_native_user_agent" else default)
    agent = Agent(name="openai", default_base_url="https://api.openai.com")
    key = {"api_key": "sk-test123"}
    headers = agent.build_headers(key, api_path="embeddings")
    assert headers["User-Agent"].startswith("akm/")


def test_build_headers_special_request_still_prefers_native_user_agent_when_enabled(monkeypatch):
    """特殊请求在开启透传开关后，仍应优先使用客户端原始 User-Agent。"""
    monkeypatch.setattr("akm.agent.config_get", lambda key, default=None: True if key == "use_native_user_agent" else default)
    agent = Agent(name="openai", default_base_url="https://api.openai.com")
    key = {"api_key": "sk-test123"}
    headers = agent.build_headers(key, api_path="images/generations", original_user_agent="OpenCode/9.9.9")
    assert headers["User-Agent"] == "OpenCode/9.9.9"


def test_build_headers_custom_auth():
    """Key 有自定义 auth_header 时使用 key 的模板"""
    agent = Agent(name="openai", default_base_url="https://api.openai.com")
    key = {"api_key": "sk-test123", "auth_header": "{api_key}"}
    headers = agent.build_headers(key)
    assert headers["Authorization"] == "sk-test123"


def test_build_headers_agent_custom_default():
    """Agent 默认 auth_header 非 Bearer 格式"""
    agent = Agent(
        name="custom",
        default_base_url="https://custom.api.com",
        default_auth_header="Api-Key {api_key}",
    )
    key = {"api_key": "secret"}
    headers = agent.build_headers(key)
    assert headers["Authorization"] == "Api-Key secret"


def test_build_headers_messages_with_anthropic_path_switch():
    """开启开关后，messages 请求使用 Anthropic 风格请求头。"""
    agent = Agent(
        name="vendor",
        default_base_url="https://vendor.example.com",
        supports_messages=True,
        messages_use_anthropic_path=True,
    )
    key = {"api_key": "secret"}
    headers = agent.build_headers(key, "messages")
    assert headers["x-api-key"] == "secret"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["Content-Type"] == "application/json"


# ───────────────────────────────────────────────
# needs_conversion
# ───────────────────────────────────────────────

def test_needs_conversion_deepseek_responses():
    """DeepSeek 不支持 responses → 需转为 chat/completions"""
    agent = AGENT_REGISTRY["deepseek"]
    assert agent.needs_conversion("responses") == "chat/completions"


def test_needs_conversion_openai_responses():
    """OpenAI 原生支持 responses → 不需要转换"""
    agent = AGENT_REGISTRY["openai"]
    assert agent.needs_conversion("responses") is None


def test_needs_conversion_deepseek_chat():
    """DeepSeek 支持 chat → 不需要转换"""
    agent = AGENT_REGISTRY["deepseek"]
    assert agent.needs_conversion("chat/completions") is None


def test_needs_conversion_anthropic_chat():
    """Anthropic 不支持 chat → 需转为 messages"""
    agent = AGENT_REGISTRY["anthropic"]
    assert agent.needs_conversion("chat/completions") == "messages"


def test_needs_conversion_anthropic_messages():
    """Anthropic 原生支持 messages → 不需要转换"""
    agent = AGENT_REGISTRY["anthropic"]
    assert agent.needs_conversion("messages") is None


def test_needs_conversion_anthropic_responses():
    """Anthropic 不支持 responses 且不支持 chat → 需转为 messages"""
    agent = AGENT_REGISTRY["anthropic"]
    assert agent.needs_conversion("responses") == "messages"


def test_needs_conversion_unknown_path():
    """未知路径不需要转换"""
    agent = AGENT_REGISTRY["deepseek"]
    assert agent.needs_conversion("embeddings") is None


# ───────────────────────────────────────────────
# AGENT_REGISTRY
# ───────────────────────────────────────────────

def test_registry_openai():
    agent = AGENT_REGISTRY["openai"]
    assert agent.name == "openai"
    assert agent.default_base_url == "https://api.openai.com"
    assert agent.supports_responses is True
    assert agent.supports_chat is True
    assert agent.inject_max_completion_tokens is True
    assert agent.inject_reasoning_effort is True


def test_registry_deepseek():
    agent = AGENT_REGISTRY["deepseek"]
    assert agent.name == "deepseek"
    assert agent.default_base_url == "https://api.deepseek.com"
    assert agent.supports_responses is False
    assert agent.supports_chat is True
    assert agent.messages_use_anthropic_path is True
    assert agent.responses_force_thinking_enabled is True
    assert agent.responses_default_reasoning_effort == "high"


def test_registry_anthropic():
    agent = AGENT_REGISTRY["anthropic"]
    assert agent.name == "anthropic"
    assert agent.default_base_url == "https://api.anthropic.com"
    assert agent.supports_messages is True
    assert agent.supports_chat is False


def test_get_agent_known():
    agent = get_agent("deepseek")
    assert agent.name == "deepseek"


def test_get_agent_unknown():
    """未知 provider 返回 openai 兜底"""
    agent = get_agent("non-existent-provider")
    assert agent.name == "openai"


def test_get_agent_profile_unknown_is_conservative():
    """未知 provider 的协议画像应保守，避免误注入 OpenAI/DeepSeek 特有字段。"""
    agent = get_agent_profile("vendor-x")
    assert agent.inject_max_completion_tokens is False
    assert agent.inject_reasoning_effort is False
    assert agent.responses_force_thinking_enabled is False


# ───────────────────────────────────────────────
# register_agent / unregister_agent
# ───────────────────────────────────────────────

def test_register_custom_agent(monkeypatch, tmp_path):
    """注册自定义 Agent 并持久化"""
    monkeypatch.setattr("akm.agent.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("akm.agent._get_config_path", lambda: str(tmp_path / "config.json"))

    register_agent(
        name="dmxapi",
        default_base_url="https://www.dmxapi.cn/v1",
        default_auth_header="{api_key}",
        supports_chat=True,
        supports_responses=False,
    )
    assert "dmxapi" in AGENT_REGISTRY
    agent = AGENT_REGISTRY["dmxapi"]
    assert agent.default_base_url == "https://www.dmxapi.cn/v1"
    assert agent.default_auth_header == "{api_key}"

    # 持久化验证
    import json
    with open(str(tmp_path / "config.json")) as f:
        cfg = json.load(f)
    assert "dmxapi" in cfg.get("custom_agents", {})
    assert cfg["custom_agents"]["dmxapi"]["default_auth_header"] == "{api_key}"
    assert cfg["custom_agents"]["dmxapi"]["messages_use_anthropic_path"] is False

    # 清理：从注册表移除，避免影响后续测试
    del AGENT_REGISTRY["dmxapi"]


def test_register_builtin_raises():
    """不能注册覆盖内置 Agent"""
    with pytest.raises(ValueError, match="不能覆盖内置供应商"):
        register_agent("openai", "https://fake.openai.com")


def test_unregister_custom_agent(monkeypatch, tmp_path):
    """删除自定义 Agent"""
    monkeypatch.setattr("akm.agent.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("akm.agent._get_config_path", lambda: str(tmp_path / "config.json"))

    AGENT_REGISTRY["test_custom"] = Agent(name="test_custom", default_base_url="https://test.com")
    unregister_agent("test_custom")
    assert "test_custom" not in AGENT_REGISTRY


def test_unregister_builtin_raises():
    """不能删除内置 Agent"""
    with pytest.raises(ValueError, match="不能删除内置供应商"):
        unregister_agent("openai")


def test_list_agents_marks_custom():
    """list_agents 标记 is_custom"""
    AGENT_REGISTRY["x-custom"] = Agent(name="x-custom", default_base_url="https://x.com")
    agents = {a["name"]: a for a in list_agents()}
    assert agents["openai"]["is_custom"] is False
    assert agents["x-custom"]["is_custom"] is True
    del AGENT_REGISTRY["x-custom"]


def test_load_custom_agents(monkeypatch, tmp_path):
    """从 config.json 加载自定义 Agent"""
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("akm.agent.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("akm.agent._get_config_path", lambda: str(config_path))

    import json
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(config_path), "w") as f:
        json.dump({"custom_agents": {
            "myapi": {
                "default_base_url": "https://myapi.example.com",
                "default_auth_header": "X-Key {api_key}",
                "supports_chat": True,
                "supports_responses": False,
                "supports_messages": False,
                "messages_use_anthropic_path": True,
            }
        }}, f)

    load_custom_agents()
    assert "myapi" in AGENT_REGISTRY
    agent = AGENT_REGISTRY["myapi"]
    assert agent.default_base_url == "https://myapi.example.com"
    assert agent.default_auth_header == "X-Key {api_key}"
    assert agent.messages_use_anthropic_path is True

    del AGENT_REGISTRY["myapi"]
