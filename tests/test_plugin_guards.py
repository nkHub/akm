"""配额、模型降级和数据过滤插件的聚焦回归测试。"""

import json
import logging
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from unittest.mock import AsyncMock, MagicMock

from akm.plugins.plugin_manager import PluginManager
from akm.proxy import forward_request
from plugins.data_filter_guard.index import Plugin as DataFilterGuard
from plugins.fallback_router.index import Plugin as FallbackRouter
from plugins.usage_quota_guard.index import Plugin as UsageQuotaGuard
from plugins.webhook_notifier.index import Plugin as WebhookNotifier
from plugins.prompt_profiles.index import Plugin as PromptProfiles
from plugins.tool_policy_guard.index import Plugin as ToolPolicyGuard
from plugins.response_schema_guard.index import Plugin as ResponseSchemaGuard
from plugins.provider_health_probe.index import Plugin as ProviderHealthProbe


class FakeResponse:
    """最小 HTTP 响应替身，覆盖 forward_request 的非流式读取路径。"""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self._body = body.encode("utf-8")

    async def aread(self):
        return self._body

    async def aclose(self):
        return None


def _make_client(responses, sent_bodies):
    """构造记录请求 JSON 的 httpx client 替身。"""
    client = MagicMock()

    def build_request(method, url, json=None, headers=None, timeout=None, **kwargs):
        sent_bodies.append(json)
        return httpx.Request(method, url, json=json, headers=headers)

    client.build_request.side_effect = build_request
    client.send = AsyncMock(side_effect=responses)
    return client


@pytest.mark.asyncio
async def test_data_filter_legacy_enabled_config_migrates_to_runtime_state(monkeypatch, tmp_path):
    """旧配置只有内部 enabled 时，插件也必须进入实际 Hook 候选列表。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config_path = tmp_path / ".akm" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({
        "plugin_configs": {"data_filter_guard": {"enabled": True}},
    }), "utf-8")

    manager = PluginManager()
    await manager.load_all(FastAPI())

    assert manager.plugins["data_filter_guard"].enabled is True
    assert manager.plugins["webhook_notifier"].enabled is False
    assert manager.plugins["prompt_profiles"].enabled is False
    assert manager.plugins["tool_policy_guard"].enabled is False
    assert manager.plugins["response_schema_guard"].enabled is False
    assert manager.plugins["provider_health_probe"].enabled is False
    saved = json.loads(config_path.read_text("utf-8"))
    assert saved["plugin_states"]["data_filter_guard"] is True


@pytest.mark.asyncio
async def test_error_handler_is_enabled_by_default_without_overriding_saved_state(monkeypatch, tmp_path):
    """错误处理插件首次加载默认开启，已有显式禁用状态必须继续生效。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config_path = tmp_path / ".akm" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", "utf-8")

    manager = PluginManager()
    await manager.load_all(FastAPI())

    assert manager.plugins["error_handler"].enabled is True
    saved = json.loads(config_path.read_text("utf-8"))
    assert saved["plugin_states"]["error_handler"] is True

    config_path.write_text(json.dumps({
        "plugin_states": {"error_handler": False},
    }), "utf-8")
    manager = PluginManager()
    await manager.load_all(FastAPI())

    assert manager.plugins["error_handler"].enabled is False
    saved = json.loads(config_path.read_text("utf-8"))
    assert saved["plugin_states"]["error_handler"] is False


@pytest.mark.asyncio
async def test_plugin_hot_toggle_calls_lifecycle(monkeypatch, tmp_path):
    """启用/禁用应热调用 on_load/on_unload，无需重启。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config_path = tmp_path / ".akm" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", "utf-8")

    manager = PluginManager()
    await manager.load_all(FastAPI())
    name = "webhook_notifier"
    plugin = manager.plugins[name]
    assert plugin.enabled is False

    loads = {"n": 0}
    unloads = {"n": 0}
    orig_load = plugin.on_load
    orig_unload = plugin.on_unload

    async def _load():
        loads["n"] += 1
        return await orig_load()

    async def _unload():
        unloads["n"] += 1
        return await orig_unload()

    plugin.on_load = _load  # type: ignore[method-assign]
    plugin.on_unload = _unload  # type: ignore[method-assign]

    en = await manager.toggle_plugin(name, True, hot=True)
    assert en["ok"] is True and en["hot"] is True
    assert plugin.enabled is True
    assert loads["n"] == 1
    saved = json.loads(config_path.read_text("utf-8"))
    assert saved["plugin_states"][name] is True

    dis = await manager.toggle_plugin(name, False, hot=True)
    assert dis["ok"] is True and dis["hot"] is True
    assert plugin.enabled is False
    assert unloads["n"] == 1
    saved = json.loads(config_path.read_text("utf-8"))
    assert saved["plugin_states"][name] is False

    # cold 路径只写配置
    cold = await manager.toggle_plugin(name, True, hot=False)
    assert cold["ok"] is True and cold["hot"] is False
    assert plugin.enabled is True
    assert loads["n"] == 1  # 未再 on_load


@pytest.mark.asyncio
async def test_data_filter_private_reverse_map_is_not_forwarded_upstream(monkeypatch):
    """反向映射仅供本地响应恢复使用，绝不能作为供应商请求参数发送。"""
    key = {
        "alias": "guard-key",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "https://api.openai.com",
    }
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value=key))
    sent_bodies = []
    client = _make_client([FakeResponse(200, '{"choices":[{"message":{"content":"ok"}}]}')], sent_bodies)

    class GuardManager:
        def get_converter(self, *_args):
            return None

        async def run_hook(self, hook, **kwargs):
            if hook == "on_request":
                changed = dict(kwargs["request"])
                changed["__akm_reverse_map__"] = {"<AKM-SEC:x@1/>": "secret"}
                return {"request": changed}
            return kwargs

    result = await forward_request(
        {"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
        client,
        plugin_manager=GuardManager(),
    )

    assert result["status_code"] == 200
    assert "__akm_reverse_map__" not in sent_bodies[0]


@pytest.mark.asyncio
async def test_usage_quota_guard_skips_key_after_request_or_token_limit():
    """配额应先限制请求数，并在响应 usage 可解析时累计 Token。"""
    plugin = UsageQuotaGuard()
    plugin.config = {
        "enabled": True,
        "window_seconds": 3600,
        "max_requests_per_key": 1,
        "max_tokens_per_key": 100,
    }
    await plugin.on_load()
    key = {"alias": "quota-key"}

    assert await plugin.on_key_selected("gpt-4", key, {}) is None
    skipped = await plugin.on_key_selected("gpt-4", key, {})
    assert skipped["__akm_action__"] == "skip_key"
    assert "请求数" in skipped["error"]

    token_plugin = UsageQuotaGuard()
    token_plugin.config = {
        "enabled": True,
        "window_seconds": 3600,
        "max_tokens_per_key": 5,
    }
    await token_plugin.on_load()
    assert await token_plugin.on_key_selected("gpt-4", key, {}) is None
    await token_plugin.on_response(
        {},
        {
            "ok": True,
            "key_alias": "quota-key",
            "model": "gpt-4",
            "response_body": '{"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}',
        },
    )
    skipped = await token_plugin.on_key_selected("gpt-4", key, {})
    assert skipped["__akm_action__"] == "skip_key"
    assert "Token" in skipped["error"]


@pytest.mark.asyncio
async def test_fallback_router_changes_model_and_proxy_reselects_key(monkeypatch):
    """目标模型降级后，proxy 必须清除旧模型候选并重新发起 Key 选择。"""
    source_key = {
        "alias": "source-key",
        "provider": "openai",
        "api_key": "sk-source",
        "base_url": "https://api.openai.com",
    }
    fallback_key = {**source_key, "alias": "fallback-key"}
    picked_models = []

    async def pick_key(model, _excluded):
        picked_models.append(model)
        return source_key if model == "gpt-primary" else fallback_key

    monkeypatch.setattr("akm.proxy.pick_key_async", pick_key)
    sent_bodies = []
    client = _make_client(
        [
            FakeResponse(503, '{"error":{"message":"unavailable"}}'),
            FakeResponse(200, '{"choices":[{"message":{"content":"ok"}}]}'),
        ],
        sent_bodies,
    )
    router = FallbackRouter()
    router.logger = logging.getLogger("test.fallback_router")
    router.config = {
        "enabled": True,
        "rules": "gpt-primary=>gpt-fallback",
        "status_codes": "503",
        "error_types": "",
        "max_fallbacks": 1,
    }

    class RouterManager:
        def get_converter(self, *_args):
            return None

        async def run_hook(self, hook, **kwargs):
            if hook == "on_upstream_error":
                return await router.on_upstream_error(**kwargs)
            return kwargs

    result = await forward_request(
        {"model": "gpt-primary", "messages": [{"role": "user", "content": "hello"}]},
        client,
        plugin_manager=RouterManager(),
    )

    assert result["status_code"] == 200
    assert picked_models == ["gpt-primary", "gpt-fallback"]
    assert sent_bodies[1]["model"] == "gpt-fallback"
    assert "__akm_fallback_history__" not in sent_bodies[1]


@pytest.mark.asyncio
async def test_data_filter_stream_guard_blocks_complete_payload_before_output():
    """流式保护在有界完整缓冲后应返回协议兼容的安全 SSE 内容。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "enable_stream_response_guard": True,
        "response_guard_mode": "block",
        "response_block_patterns": "(?i)rm\\s+-rf\\s+/",
    }
    await plugin.on_load()

    assert plugin.stream_guard_requires_buffering() is True
    protected, changed, _reason, action = plugin.protect_stream_payload(
        "chat/completions", "data: dangerous rm -rf / command\n\n"
    )
    assert changed is True
    assert action == "blocked"
    assert "[DONE]" in protected


@pytest.mark.asyncio
async def test_data_filter_default_response_patterns_match_without_trailing_commas():
    """默认响应拦截正则不应因行尾逗号而失效。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    # 模拟历史配置：每行带尾逗号
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "response_guard_mode": "block",
        "response_block_patterns": (
            "(?i)rm\\s+-rf\\s+/,"
            "\n(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh),"
            "\n(?i)os\\.system\\s*\\("
        ),
    }
    await plugin.on_load()
    plugin._reload_config()

    body = json.dumps({"choices": [{"message": {"content": "please run rm -rf / now"}}]})
    resp = await plugin.on_response({}, {"stream": False, "api_path": "chat/completions", "response_body": body})
    assert resp["security_action"] == "block"
    assert "数据安全插件拦截" in resp["response_body"]


@pytest.mark.asyncio
async def test_data_filter_masks_anthropic_content_blocks_and_system():
    """messages[].content 应覆盖 content blocks，system 路径也应可扫描。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {
        "enabled": True,
        "keyword_rules": "SECRET_TOKEN",
        "request_text_paths": "messages[].content,input,instructions,system",
        "enable_code_secret_guard": True,
        "code_secret_guard_mode": "mask",
        "code_secret_paths": "messages[].content,input,instructions,system",
        "code_secret_rule_groups": "llm_keys",
    }
    await plugin.on_load()

    secret_key = "sk-proj-" + ("A" * 48)
    request = {
        "system": "never leak SECRET_TOKEN",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"key={secret_key} and SECRET_TOKEN"},
                ],
            }
        ],
    }
    out = await plugin.on_request(request)
    assert out is not None
    assert "SECRET_TOKEN" not in out["system"]
    text = out["messages"][0]["content"][0]["text"]
    assert secret_key not in text
    assert "SECRET_TOKEN" not in text
    assert "<AKM-SEC:" in text
    assert out["__akm_reverse_map__"]


@pytest.mark.asyncio
async def test_data_filter_blocks_anthropic_nonstream_response():
    """Anthropic Messages 非流式响应的 content[].text 必须参与安全扫描。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "response_guard_mode": "block",
        "response_block_patterns": "(?i)rm\\s+-rf\\s+/",
    }
    await plugin.on_load()

    body = json.dumps(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "run rm -rf / immediately"}],
            "model": "claude",
        }
    )
    resp = await plugin.on_response({}, {"stream": False, "api_path": "messages", "response_body": body})
    assert resp["security_action"] == "block"
    assert "msg_akm_security" in resp["response_body"] or "数据安全插件拦截" in resp["response_body"]


@pytest.mark.asyncio
async def test_data_filter_placeholder_collision_and_stream_restore():
    """占位符序号应避免碰撞覆盖；跨 chunk 未闭合占位符应能完整还原。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {"enabled": True, "keyword_rules": "ALPHA,BETA", "request_text_paths": ""}
    await plugin.on_load()
    plugin._reload_config()
    plugin._reset_reverse_map()

    p1 = plugin._make_placeholder("keyword", "ALPHA")
    p2 = plugin._make_placeholder("keyword", "BETA")
    assert p1 != p2
    assert plugin._reverse_map[p1] == "ALPHA"
    assert plugin._reverse_map[p2] == "BETA"

    state = plugin.reverse_stream_state()
    rmap = {p1: "ALPHA"}
    # 在闭合后缀前切开
    head, tail = p1[: len(p1) // 2], p1[len(p1) // 2 :]
    out1 = plugin.reverse_stream_chunk(f"before {head}", state, reverse_map=rmap)
    out2 = plugin.reverse_stream_chunk(f"{tail} after", state, reverse_map=rmap)
    assert "ALPHA" in (out1 + out2)
    assert "<AKM-SEC:" not in (out1 + out2)


@pytest.mark.asyncio
async def test_data_filter_stream_mask_overflow_degrades_to_block():
    """流式增量路径上 mask 应退化为 block，避免危险内容透传。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "enable_stream_response_guard": True,
        "response_guard_mode": "mask",
        "response_block_patterns": "(?i)rm\\s+-rf\\s+/",
    }
    await plugin.on_load()

    state = plugin.create_stream_guard_state()
    state, blocked, reason, action = plugin.inspect_stream_chunk(
        "chat/completions", "please rm -rf / now", state
    )
    assert blocked is True
    assert action == "blocked"
    assert "rm" in reason.lower() or reason


@pytest.mark.asyncio
async def test_data_filter_redact_only_does_not_attach_empty_reverse_map():
    """仅敏感字段 redact 且无文本替换时，不应附加空 reverse map。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {
        "enabled": True,
        "sensitive_fields": "password",
        "keyword_rules": "",
        "request_text_paths": "messages[].content",
    }
    await plugin.on_load()
    out = await plugin.on_request({"password": "secret", "messages": [{"content": "hi"}]})
    assert out["password"] == "[REDACTED]"
    assert "__akm_reverse_map__" not in out


@pytest.mark.asyncio
async def test_webhook_notifier_sends_failure_once_with_cooldown(monkeypatch):
    """相同上游失败应异步通知一次，冷却期内不重复创建发送任务。"""
    plugin = WebhookNotifier()
    plugin.logger = logging.getLogger("test.webhook_notifier")
    plugin.config = {
        "enabled": True,
        "webhook_url": "https://example.test/webhook",
        "payload_format": "generic",
        "notify_failures": True,
        "cooldown_seconds": 300,
    }
    await plugin.on_load()
    sent = []

    async def fake_send(url, payload, timeout):
        sent.append((url, payload, timeout))

    monkeypatch.setattr(plugin, "_send", fake_send)
    event = {
        "ok": False,
        "phase": "upstream",
        "status_code": 503,
        "key_alias": "primary",
        "provider": "openai",
        "model": "gpt-5",
        "api_path": "responses",
        "error": "upstream unavailable",
    }
    await plugin.on_response({}, event)
    await plugin.on_response({}, event)
    await asyncio.sleep(0)

    assert len(sent) == 1
    assert sent[0][0] == "https://example.test/webhook"
    assert sent[0][1]["event"] == "failure"
    assert sent[0][1]["details"]["status_code"] == 503

    plugin.app = SimpleNamespace(
        state=SimpleNamespace(health_monitor=SimpleNamespace(audit_queue_dropped=2))
    )
    await plugin.on_response({}, {"ok": True, "model": "gpt-5", "key_alias": "primary"})
    await asyncio.sleep(0)
    assert sent[1][1]["event"] == "audit_drop"
    assert sent[1][1]["details"]["audit_queue_dropped"] == 2


@pytest.mark.asyncio
async def test_prompt_profiles_match_protocol_model_and_client_without_leaking_context():
    """配置集应按条件叠加注入，并保持内部匹配上下文只在本地请求对象中存在。"""
    plugin = PromptProfiles()
    plugin.logger = logging.getLogger("test.prompt_profiles")
    plugin.config = {
        "enabled": True,
        "profiles_json": json.dumps([
            {
                "name": "responses-base",
                "models": ["gpt-*"],
                "api_paths": ["responses"],
                "position": "before",
                "prompt": "Always answer in Chinese.",
            },
            {
                "name": "codex-extra",
                "client_patterns": ["codex"],
                "api_paths": ["responses"],
                "position": "after",
                "prompt": "Return a focused diff.",
            },
            {
                "name": "chat-only",
                "api_paths": ["chat/completions"],
                "prompt": "must not match",
            },
        ]),
    }
    request = {
        "model": "gpt-5",
        "instructions": "Existing instruction.",
        "__akm_api_path__": "responses",
        "__akm_client_user_agent__": "Codex CLI/1.0",
    }

    changed = await plugin.on_request(request)

    assert changed is request
    assert request["instructions"] == (
        "Always answer in Chinese.\n\nExisting instruction.\n\nReturn a focused diff."
    )
    assert request["__akm_api_path__"] == "responses"


@pytest.mark.asyncio
async def test_prompt_profiles_uses_anthropic_system_instead_of_system_message():
    """Messages API 的 profile 必须写入顶层 system，避免产生非法 role=system 消息。"""
    plugin = PromptProfiles()
    plugin.logger = logging.getLogger("test.prompt_profiles.messages")
    plugin.config = {
        "profiles_json": json.dumps([{
            "name": "anthropic",
            "api_paths": ["messages"],
            "prompt": "Be concise.",
        }]),
    }
    request = {
        "model": "claude-3",
        "messages": [{"role": "user", "content": "hello"}],
        "__akm_api_path__": "messages",
    }

    await plugin.on_request(request)

    assert request["system"] == "Be concise."
    assert request["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_proxy_passes_prompt_profile_context_and_strips_it_before_upstream(monkeypatch):
    """代理应提供 API/客户端匹配上下文，同时确保这些内部字段不会出站。"""
    key = {
        "alias": "profile-key",
        "provider": "openai",
        "api_key": "sk-test",
        "base_url": "https://api.openai.com",
    }
    monkeypatch.setattr("akm.proxy.pick_key_async", AsyncMock(return_value=key))
    sent_bodies = []
    client = _make_client([FakeResponse(200, '{"choices":[{"message":{"content":"ok"}}]}')], sent_bodies)
    profile = PromptProfiles()
    profile.logger = logging.getLogger("test.prompt_profiles.proxy")
    profile.config = {
        "profiles_json": json.dumps([{
            "name": "codex-chat",
            "api_paths": ["chat/completions"],
            "client_patterns": ["codex"],
            "prompt": "Use focused patches.",
        }]),
    }

    class ProfileManager:
        def get_converter(self, *_args):
            return None

        async def run_hook(self, hook, **kwargs):
            if hook == "on_request":
                changed = await profile.on_request(kwargs["request"])
                return {"request": changed or kwargs["request"]}
            return kwargs

    result = await forward_request(
        {"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}]},
        client,
        plugin_manager=ProfileManager(),
        original_user_agent="Codex CLI/1.0",
    )

    assert result["status_code"] == 200
    assert sent_bodies[0]["messages"][0] == {"role": "system", "content": "Use focused patches."}
    assert not any(name.startswith("__akm_") for name in sent_bodies[0])


@pytest.mark.asyncio
async def test_tool_policy_guard_blocks_denied_tool_and_dangerous_continuation():
    """工具黑名单和客户端续接参数正则都应触发标准请求阻断结构。"""
    plugin = ToolPolicyGuard()
    plugin.logger = logging.getLogger("test.tool_policy_guard")
    plugin.config = {
        "enabled": True,
        "mode": "block",
        "deny_tool_names": "bash",
        "deny_argument_patterns": "(?i)rm\\s+-rf\\s+/",
    }
    blocked_name = await plugin.on_request({
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}],
    })
    assert blocked_name["__akm_action__"] == "block"
    assert "黑名单" in blocked_name["security_reason"]

    plugin.config["deny_tool_names"] = ""
    blocked_args = await plugin.on_request({
        "messages": [{"role": "assistant", "tool_calls": [{"function": {"name": "run", "arguments": "rm -rf /"}}]}],
    })
    assert blocked_args["__akm_action__"] == "block"
    assert "参数命中" in blocked_args["security_reason"]


@pytest.mark.asyncio
async def test_response_schema_guard_blocks_invalid_declared_json_schema():
    """声明 required/type 的 JSON Schema 时，非法模型输出必须被协议兼容错误替换。"""
    plugin = ResponseSchemaGuard()
    plugin.logger = logging.getLogger("test.response_schema_guard")
    plugin.config = {"enabled": True, "mode": "block", "block_message": "invalid structured output"}
    request = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "schema": {
                    "type": "object",
                    "required": ["answer"],
                    "properties": {"answer": {"type": "string"}},
                }
            },
        },
    }
    response = {
        "ok": True,
        "api_path": "chat/completions",
        "response_body": '{"choices":[{"message":{"content":"{\\"answer\\": 42}"}}]}',
    }

    guarded = await plugin.on_response(request, response)

    assert guarded["security_action"] == "schema_block"
    assert "$.answer 应为 string" == guarded["security_reason"]
    assert json.loads(guarded["response_body"])["error"]["message"] == "invalid structured output"


@pytest.mark.asyncio
async def test_provider_health_probe_returns_sanitized_snapshot(monkeypatch):
    """批量探测应复用完整 Key 做请求，但对外结果不得泄露密钥或 URL。"""
    plugin = ProviderHealthProbe()
    plugin.logger = logging.getLogger("test.provider_health_probe")
    plugin.config = {"max_concurrency": 2, "allow_protocol_fallback": True}
    await plugin.on_load()

    summary = {"alias": "probe-key", "provider": "openai", "status": "active"}
    full_key = {**summary, "api_key": "sk-secret", "base_url": "https://secret.example/v1"}
    monkeypatch.setattr("plugins.provider_health_probe.index.list_keys", lambda: [summary])
    monkeypatch.setattr("plugins.provider_health_probe.index.get_key", lambda alias: full_key if alias == "probe-key" else None)

    async def fake_test(key, allow_fallback=False):
        assert key["api_key"] == "sk-secret"
        assert allow_fallback is True
        return {"ok": True, "status_code": 200, "latency_ms": 25, "model": "gpt-5", "api_path": "responses", "url": "https://secret.example/v1/responses"}

    monkeypatch.setattr("plugins.provider_health_probe.index.test_key_connectivity", fake_test)
    result = await plugin.probe()

    assert result["checked"] == 1
    snapshot = result["results"][0]
    assert snapshot["ok"] is True
    assert snapshot["model"] == "gpt-5"
    assert "api_key" not in snapshot
    assert "url" not in snapshot
    assert plugin.status()["healthy"] == 1


@pytest.mark.asyncio
async def test_provider_health_probe_registers_status_api_when_enabled(monkeypatch, tmp_path):
    """启用后应由 PluginManager 注册健康状态路由，而非只保留插件内部方法。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config_path = tmp_path / ".akm" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({
        "plugin_states": {"provider_health_probe": True},
    }), "utf-8")
    app = FastAPI()
    manager = PluginManager()
    await manager.load_all(app)
    plugin = manager.plugins["provider_health_probe"]
    plugin._results = {
        "probe-key": {"alias": "probe-key", "ok": True, "provider": "openai"},
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/provider-health/status")

    assert response.status_code == 200
    assert response.json()["healthy"] == 1
