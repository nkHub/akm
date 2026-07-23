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

from akm.plugins.context import RequestContext
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


def _ctx(request: dict | None = None, **kwargs) -> RequestContext:
    """构造单次请求级上下文（直接持有 request 引用，不 clone）。"""
    return RequestContext(request if isinstance(request, dict) else {}, **kwargs)


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

        async def run_hook(self, hook, ctx=None, **kwargs):
            # reverse_map 走请求级 bag，不得写入 request 以免误发上游
            if hook == "on_request" and ctx is not None:
                ctx.bag_set("data_filter_guard.reverse_map", {"<AKM-SEC:x@1/>": "secret"})
                return ctx
            return ctx if ctx is not None else kwargs

    result = await forward_request(
        {"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
        client,
        plugin_manager=GuardManager(),
    )

    assert result["status_code"] == 200
    assert "__akm_reverse_map__" not in sent_bodies[0]
    # bag 中的 reverse_map 也不应出现在上游 JSON
    assert "data_filter_guard.reverse_map" not in json.dumps(sent_bodies[0])


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

    ctx1 = _ctx({"model": "gpt-4"})
    ctx1.key = key
    ctx1.model = "gpt-4"
    assert await plugin.on_key_selected(ctx1) is None
    ctx2 = _ctx({"model": "gpt-4"})
    ctx2.key = key
    ctx2.model = "gpt-4"
    skipped = await plugin.on_key_selected(ctx2)
    assert skipped["type"] == "skip_key"
    assert "请求数" in skipped["error"]

    token_plugin = UsageQuotaGuard()
    token_plugin.config = {
        "enabled": True,
        "window_seconds": 3600,
        "max_tokens_per_key": 5,
    }
    await token_plugin.on_load()
    ctx3 = _ctx({"model": "gpt-4"})
    ctx3.key = key
    ctx3.model = "gpt-4"
    assert await token_plugin.on_key_selected(ctx3) is None
    ctx_resp = _ctx({"model": "gpt-4"})
    ctx_resp.response = {
        "ok": True,
        "key_alias": "quota-key",
        "model": "gpt-4",
        "response_body": '{"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}',
    }
    await token_plugin.on_response(ctx_resp)
    ctx4 = _ctx({"model": "gpt-4"})
    ctx4.key = key
    ctx4.model = "gpt-4"
    skipped = await token_plugin.on_key_selected(ctx4)
    assert skipped["type"] == "skip_key"
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

        async def run_hook(self, hook, ctx=None, **kwargs):
            if hook == "on_upstream_error" and ctx is not None:
                return await router.on_upstream_error(
                    ctx,
                    status_code=int(kwargs.get("status_code", 0) or 0),
                    error_type=str(kwargs.get("error_type", "http") or "http"),
                    attempt=int(kwargs.get("attempt", 0) or 0),
                    key=kwargs.get("key"),
                )
            return ctx if ctx is not None else kwargs

    result = await forward_request(
        {"model": "gpt-primary", "messages": [{"role": "user", "content": "hello"}]},
        client,
        plugin_manager=RouterManager(),
    )

    assert result["status_code"] == 200
    assert picked_models == ["gpt-primary", "gpt-fallback"]
    assert sent_bodies[1]["model"] == "gpt-fallback"
    # fallback 历史只在 bag，不得出现在上游请求体
    assert "__akm_fallback_history__" not in sent_bodies[1]


@pytest.mark.asyncio
async def test_data_filter_stream_guard_blocks_on_incremental_scan():
    """流式保护按字段级滑动窗口增量扫描，命中 block 后应可生成协议兼容安全载荷。"""
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

    assert plugin.is_stream_guard_active() is True
    assert not hasattr(plugin, "stream_guard_requires_buffering")
    assert not hasattr(plugin, "protect_stream_payload")

    state = plugin.create_stream_guard_state()
    state, blocked, reason, action = plugin.inspect_stream_chunk(
        "chat/completions",
        "data: " + json.dumps({"choices": [{"delta": {"content": "dangerous rm -rf / command"}}]}) + "\n\n",
        state,
    )
    assert blocked is True
    assert action == "blocked"
    assert reason
    safe = plugin._build_safe_stream_payload("chat/completions")
    assert "[DONE]" in safe


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
    ctx = _ctx({})
    ctx.response = {"stream": False, "api_path": "chat/completions", "response_body": body}
    resp = await plugin.on_response(ctx)
    assert resp["security_action"] == "block"
    assert "数据安全插件拦截" in resp["response_body"]


@pytest.mark.asyncio
async def test_data_filter_masks_anthropic_content_blocks_and_system():
    """messages[].content 应覆盖 content blocks，system 路径也应可扫描；默认正则可逆脱敏 sk。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {
        "enabled": True,
        "keyword_rules": "SECRET_TOKEN",
        "request_text_paths": "messages[].content,input,instructions,system",
        # 不显式写 regex_rules → 使用 DEFAULT_REGEX_RULES（含 sk-proj）
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
    ctx = _ctx(request)
    out = await plugin.on_request(ctx)
    assert out is not None
    assert "SECRET_TOKEN" not in out["system"]
    text = out["messages"][0]["content"][0]["text"]
    assert secret_key not in text
    assert "SECRET_TOKEN" not in text
    assert "<AKM-SEC:" in text
    rmap = ctx.bag_get("data_filter_guard.reverse_map")
    assert rmap
    assert "__akm_reverse_map__" not in out
    restored, changed = plugin._reverse_replace(text, reverse_map=rmap)
    assert changed is True
    assert secret_key in restored


@pytest.mark.asyncio
async def test_data_filter_request_text_paths_cover_tool_call_arguments():
    """默认 request_text_paths 应覆盖 tool_calls 参数，用正则可逆脱敏。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    secret_key = "sk-proj-" + ("B" * 48)
    plugin.config = {
        "enabled": True,
        # 显式包含 tool_calls 路径；关键词路径故意只写 content，验证路径门控
        "request_text_paths": "messages[].tool_calls[].function.arguments",
        "keyword_rules": "SHOULD_NOT_TOUCH",
        # 不显式写 regex_rules → 使用 DEFAULT_REGEX_RULES
    }
    await plugin.on_load()

    request = {
        "messages": [
            {
                "role": "assistant",
                "content": "SHOULD_NOT_TOUCH stays if outside keyword path only",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "run",
                            "arguments": json.dumps({"token": secret_key, "note": "SHOULD_NOT_TOUCH"}),
                        },
                    }
                ],
            }
        ],
    }
    ctx = _ctx(request)
    out = await plugin.on_request(ctx)
    assert out is not None
    # content 不在 request_text_paths 内，关键词与正则均不应处理
    assert "SHOULD_NOT_TOUCH" in out["messages"][0]["content"]
    args = out["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert secret_key not in args
    # 关键词规则也只作用于 tool_calls 路径：arguments 内的 SHOULD_NOT_TOUCH 会被替换
    assert "SHOULD_NOT_TOUCH" not in args
    assert "<AKM-SEC:" in args
    rmap = ctx.bag_get("data_filter_guard.reverse_map")
    assert rmap
    assert "__akm_reverse_map__" not in out
    restored, changed = plugin._reverse_replace(args, reverse_map=rmap)
    assert changed is True
    assert secret_key in restored


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
    ctx = _ctx({})
    ctx.response = {"stream": False, "api_path": "messages", "response_body": body}
    resp = await plugin.on_response(ctx)
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
async def test_data_filter_stream_restore_partial_prefix_across_chunks():
    """前缀 ``<AKM-SEC:`` 被切到两个 chunk 时不得原样透传半截。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {"enabled": True}
    await plugin.on_load()

    placeholder = "<AKM-SEC:phone@1:abcdef/>"
    rmap = {placeholder: "13012341234"}
    state = plugin.reverse_stream_state()

    # 前缀正中间切开：`<AKM` + `-SEC:phone@1:abcdef/> after`
    out1 = plugin.reverse_stream_chunk("号码=<AKM", state, reverse_map=rmap)
    out2 = plugin.reverse_stream_chunk("-SEC:phone@1:abcdef/> after", state, reverse_map=rmap)
    joined = out1 + out2
    assert "13012341234" in joined
    assert "AKM-SEC" not in joined
    assert out1 == "号码="  # 半截前缀必须压在 pending 里，不能先 yield


@pytest.mark.asyncio
async def test_data_filter_stream_restore_sse_token_split_content():
    """SSE 按 token 拆开 delta.content 时，应在字段级截流拼回后换回。

    模拟真实上游：每个 chunk 一帧，content 分别为 ``<`` / ``AK`` / ``M-SEC:.../>``，
    raw 帧间夹 JSON 外壳，纯文本扫描无法拼出连续占位符。
    """
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {"enabled": True}
    await plugin.on_load()

    placeholder = "<AKM-SEC:phone@1:abcdef/>"
    phone = "13012341234"
    rmap = {placeholder: phone}
    state = plugin.reverse_stream_state()

    def _frame(content: str) -> str:
        payload = json.dumps(
            {"choices": [{"index": 0, "delta": {"content": content}}]},
            ensure_ascii=False,
        )
        return f"data: {payload}\n\n"

    # 单字符拆：短 content 以 < 开头 → 截流
    out1 = plugin.reverse_stream_chunk(_frame("<"), state, reverse_map=rmap)
    assert "AKM-SEC" not in out1
    assert phone not in out1
    # 本帧 content 应被置空或整帧可为空 content
    if "data:" in out1:
        assert '"content": ""' in out1 or '"content":""' in out1 or phone not in out1

    out2 = plugin.reverse_stream_chunk(_frame("AKM-SEC:phone@1:abcdef/>"), state, reverse_map=rmap)
    joined = out1 + out2
    assert phone in joined
    assert "AKM-SEC" not in joined
    assert placeholder not in joined

    # flush 不应再残留占位符
    tail = plugin.reverse_stream_flush(state, reverse_map=rmap)
    assert "AKM-SEC" not in tail


@pytest.mark.asyncio
async def test_data_filter_stream_restore_sse_short_no_lt_passthrough():
    """短 content 不以 < 开头/结尾时不得无故截流。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {"enabled": True}
    await plugin.on_load()

    placeholder = "<AKM-SEC:phone@1:abcdef/>"
    rmap = {placeholder: "13012341234"}
    state = plugin.reverse_stream_state()
    payload = json.dumps(
        {"choices": [{"delta": {"content": "你好"}}]},
        ensure_ascii=False,
    )
    out = plugin.reverse_stream_chunk(f"data: {payload}\n\n", state, reverse_map=rmap)
    assert "你好" in out
    assert "data:" in out


@pytest.mark.asyncio
async def test_data_filter_stream_restore_loose_close_without_slash():
    """模型回显省略斜杠（``>`` 而非 ``/>``）时，流式仍应按指纹换回。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {"enabled": True}
    await plugin.on_load()

    placeholder = "<AKM-SEC:phone@1:abcdef/>"
    rmap = {placeholder: "13012341234"}
    # 宽松形态：无 /，tag 被改写，仅指纹一致
    loose = "<AKM-SEC:rewritten@9:abcdef>"
    state = plugin.reverse_stream_state()
    out1 = plugin.reverse_stream_chunk(f"手机 {loose[:12]}", state, reverse_map=rmap)
    out2 = plugin.reverse_stream_chunk(f"{loose[12:]} 结束", state, reverse_map=rmap)
    joined = out1 + out2
    assert "13012341234" in joined
    assert "AKM-SEC" not in joined


@pytest.mark.asyncio
async def test_data_filter_stream_restore_false_positive_still_restores_closed_key():
    """未闭合噪声超限时，缓冲区内已闭合的完整占位符仍应被换回。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {"enabled": True}
    await plugin.on_load()

    placeholder = "<AKM-SEC:phone@1:abcdef/>"
    rmap = {placeholder: "13012341234"}
    # 完整 key 后跟超长未闭合假前缀，触发 max_pending 放行路径
    noise = "<AKM-SEC:" + ("x" * 300)
    state = plugin.reverse_stream_state()
    out = plugin.reverse_stream_chunk(f"值={placeholder} 然后{noise}", state, reverse_map=rmap)
    assert "13012341234" in out
    # 完整 key 不得残留；假前缀可按原文放出
    assert placeholder not in out


@pytest.mark.asyncio
async def test_data_filter_reverse_handles_json_escape_and_chinese_tag():
    """JSON 转义斜杠与中文标签占位符应能在响应侧完整还原。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {
        "enabled": True,
        "regex_rules": r"(?<!\d)(1[3-9]\d{9})(?!\d)#@手机号",
        "request_text_paths": "messages[].content",
    }
    await plugin.on_load()

    phone = "13012341234"
    ctx = _ctx({
        "messages": [{"role": "user", "content": f"我的手机号是：{phone}"}],
    })
    out = await plugin.on_request(ctx)
    assert out is not None
    content = out["messages"][0]["content"]
    assert phone not in content
    assert "<AKM-SEC:" in content
    # 中文标签不得塌缩成无辨识度的全下划线
    assert "<AKM-SEC:___@" not in content
    rmap = ctx.bag_get("data_filter_guard.reverse_map")
    assert isinstance(rmap, dict) and rmap
    assert "__akm_reverse_map__" not in out
    placeholder = next(iter(rmap))
    assert rmap[placeholder] == phone

    # JSON 常见转义：/ → \/
    escaped = placeholder.replace("/", r"\/")
    restored, changed = plugin._reverse_replace(
        f"你之前发的手机号是 {escaped}",
        reverse_map=rmap,
    )
    assert changed is True
    assert phone in restored
    assert "AKM-SEC" not in restored

    # 模型轻微改写 tag，仅保留指纹时仍应能还原
    import re as _re
    m = _re.search(r":([0-9a-fA-F]{6})/>", placeholder)
    assert m is not None
    loose = f"<AKM-SEC:rewritten@9:{m.group(1)}/>"
    restored2, changed2 = plugin._reverse_replace(f"号码={loose}", reverse_map=rmap)
    assert changed2 is True
    assert phone in restored2

    # 流式：JSON 转义闭合跨 chunk
    state = plugin.reverse_stream_state()
    mid = placeholder.replace("/>", r"\/")
    # mid 形如 <AKM-SEC:...\/  再补 >
    head, tail = mid, ">"
    out1 = plugin.reverse_stream_chunk(f"手机号是{head}", state, reverse_map=rmap)
    out2 = plugin.reverse_stream_chunk(tail, state, reverse_map=rmap)
    assert phone in (out1 + out2)
    assert "AKM-SEC" not in (out1 + out2)


@pytest.mark.asyncio
async def test_data_filter_reverse_handles_json_unicode_lt_escape():
    """上游把 ``<`` 编成 ``\\u003c`` 时，整段与流式都应能换回。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {"enabled": True}
    await plugin.on_load()

    placeholder = "<AKM-SEC:t8098e2@1:2273f4/>"
    phone = "13800138000"
    rmap = {placeholder: phone}

    # 常见 escapeHTML：``\\u003c`` + 字面 body + ``\\u003e``；或 ``/`` 也成 ``\\u002f``
    u_ph = placeholder.replace("<", "\\u003c").replace(">", "\\u003e")
    u_ph_slash = u_ph.replace("/", "\\u002f")
    for sample in (u_ph, u_ph_slash):
        restored, changed = plugin._reverse_replace(
            f'{{"content":"{sample}"}}',
            reverse_map=rmap,
        )
        assert changed is True
        assert phone in restored
        assert "AKM-SEC" not in restored
        assert "\\u003c" not in restored

    # 流式：完整 unicode 占位符在一个 chunk
    state = plugin.reverse_stream_state()
    out = plugin.reverse_stream_chunk(
        f'data: {{"delta":"{u_ph}"}}\n\n',
        state,
        reverse_map=rmap,
    )
    assert phone in out
    assert "AKM-SEC" not in out

    # 流式：``\\u003c`` 被切到两个 chunk（``\\u00`` + ``3cAKM-SEC:...``）
    state2 = plugin.reverse_stream_state()
    out_a = plugin.reverse_stream_chunk("前缀\\u00", state2, reverse_map=rmap)
    out_b = plugin.reverse_stream_chunk(
        f"3cAKM-SEC:t8098e2@1:2273f4/\\u003e 后缀",
        state2,
        reverse_map=rmap,
    )
    joined = out_a + out_b
    assert phone in joined
    assert "AKM-SEC" not in joined


@pytest.mark.asyncio
async def test_data_filter_stream_mask_degrades_to_block():
    """流式路径上 mask 应退化为 block（边下发边扫无法回写已透传 chunk）。"""
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
async def test_data_filter_stream_guard_field_level_sse():
    """流式安全扫描应按 content 字段匹配，不因中文邻接而跳过。"""
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

    # 1) SSE content 字段命中应拦截
    dangerous = json.dumps(
        {"choices": [{"index": 0, "delta": {"content": "please rm -rf / now"}}]},
        ensure_ascii=False,
    )
    state = plugin.create_stream_guard_state()
    state, blocked, reason, action = plugin.inspect_stream_chunk(
        "chat/completions", f"data: {dangerous}\n\n", state
    )
    assert blocked is True
    assert action == "blocked"
    assert reason

    # 2) 仅出现在 JSON 键名/外壳、content 为空时不应误拦
    shell_only = json.dumps(
        {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        ensure_ascii=False,
    )
    state2 = plugin.create_stream_guard_state()
    state2, blocked2, _reason2, action2 = plugin.inspect_stream_chunk(
        "chat/completions", f"data: {shell_only}\n\n", state2
    )
    assert blocked2 is False
    assert action2 == ""

    # 3) 中文叙述夹杂危险命令仍应拦截（不做中文邻接跳过）
    cjk_wrapped = json.dumps(
        {"choices": [{"index": 0, "delta": {"content": "执行rm -rf /命令"}}]},
        ensure_ascii=False,
    )
    state3 = plugin.create_stream_guard_state()
    state3, blocked3, _reason3, action3 = plugin.inspect_stream_chunk(
        "chat/completions", f"data: {cjk_wrapped}\n\n", state3
    )
    assert blocked3 is True
    assert action3 == "blocked"

    # 4) 跨 chunk 字段级滑动窗口：危险片段拆到两帧仍应命中
    state4 = plugin.create_stream_guard_state()
    chunk_a = json.dumps(
        {"choices": [{"index": 0, "delta": {"content": "please rm -r"}}]},
        ensure_ascii=False,
    )
    chunk_b = json.dumps(
        {"choices": [{"index": 0, "delta": {"content": "f / now"}}]},
        ensure_ascii=False,
    )
    state4, blocked4a, _r4a, action4a = plugin.inspect_stream_chunk(
        "chat/completions", f"data: {chunk_a}\n\n", state4
    )
    assert blocked4a is False
    assert action4a == ""
    state4, blocked4b, _r4b, action4b = plugin.inspect_stream_chunk(
        "chat/completions", f"data: {chunk_b}\n\n", state4
    )
    assert blocked4b is True
    assert action4b == "blocked"


@pytest.mark.asyncio
async def test_data_filter_sensitive_field_uses_reversible_placeholder():
    """敏感字段名命中后应替换为可逆占位符，并挂载 reverse_map。"""
    plugin = DataFilterGuard()
    plugin.logger = logging.getLogger("test.data_filter_guard")
    plugin.config = {
        "enabled": True,
        "sensitive_fields": "password",
        "keyword_rules": "",
        "regex_rules": "",  # 显式关闭默认正则，只测敏感字段
        "request_text_paths": "messages[].content",
    }
    await plugin.on_load()
    ctx = _ctx({"password": "secret", "messages": [{"content": "hi"}]})
    out = await plugin.on_request(ctx)
    assert out is not None
    assert out["password"].startswith("<AKM-SEC:")
    assert out["password"].endswith("/>")
    assert "__akm_reverse_map__" not in out
    rmap = ctx.bag_get("data_filter_guard.reverse_map")
    assert isinstance(rmap, dict) and rmap
    assert rmap[out["password"]] == "secret"
    restored, changed = plugin._reverse_replace(out["password"], reverse_map=rmap)
    assert changed is True
    assert restored == "secret"


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
    ctx_fail = _ctx({})
    ctx_fail.response = event
    await plugin.on_response(ctx_fail)
    await plugin.on_response(ctx_fail)
    await asyncio.sleep(0)

    assert len(sent) == 1
    assert sent[0][0] == "https://example.test/webhook"
    assert sent[0][1]["event"] == "failure"
    assert sent[0][1]["details"]["status_code"] == 503

    plugin.app = SimpleNamespace(
        state=SimpleNamespace(health_monitor=SimpleNamespace(audit_queue_dropped=2))
    )
    ctx_ok = _ctx({})
    ctx_ok.response = {"ok": True, "model": "gpt-5", "key_alias": "primary"}
    await plugin.on_response(ctx_ok)
    await asyncio.sleep(0)
    assert sent[1][1]["event"] == "audit_drop"
    assert sent[1][1]["details"]["audit_queue_dropped"] == 2


@pytest.mark.asyncio
async def test_prompt_profiles_match_protocol_model_and_client_without_leaking_context():
    """配置集应按条件叠加注入；api_path/client 走 RequestContext，不污染 request。"""
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
    }
    ctx = _ctx(request, api_path="responses", client_user_agent="Codex CLI/1.0")

    changed = await plugin.on_request(ctx)

    assert changed is request
    assert request["instructions"] == (
        "Always answer in Chinese.\n\nExisting instruction.\n\nReturn a focused diff."
    )
    assert "__akm_api_path__" not in request
    assert ctx.api_path == "responses"


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
    }
    ctx = _ctx(request, api_path="messages")

    await plugin.on_request(ctx)

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

        async def run_hook(self, hook, ctx=None, **kwargs):
            if hook == "on_request" and ctx is not None:
                await profile.on_request(ctx)
                return ctx
            return ctx if ctx is not None else kwargs

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
    blocked_name = await plugin.on_request(_ctx({
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}],
    }))
    assert blocked_name["type"] == "block"
    assert "黑名单" in blocked_name["security_reason"]

    plugin.config["deny_tool_names"] = ""
    blocked_args = await plugin.on_request(_ctx({
        "messages": [{"role": "assistant", "tool_calls": [{"function": {"name": "run", "arguments": "rm -rf /"}}]}],
    }))
    assert blocked_args["type"] == "block"
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
    ctx = _ctx(request)
    ctx.response = response

    guarded = await plugin.on_response(ctx)

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
