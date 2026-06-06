import pytest
import re

from akm.plugins.request_redactor.index import Plugin


def _logger_stub():
    """构造测试用 logger stub，避免真实日志干扰断言输出。"""
    return type(
        "_L",
        (),
        {
            "info": lambda *args, **kwargs: None,
            "warning": lambda *args, **kwargs: None,
        },
    )()


@pytest.mark.asyncio
async def test_request_redactor_replaces_builtin_sensitive_patterns_with_stable_placeholders():
    """默认内置规则应把常见秘钥与邮箱替换为带类别和哈希的稳定占位符。"""
    plugin = Plugin()
    plugin.config = {"enabled": True}
    plugin.logger = _logger_stub()
    await plugin.on_load()

    request = {
        "model": "gpt-4.1",
        "messages": [
            {
                "role": "user",
                "content": "我的 openai key 是 sk-abcdefghijklmnopqrstuvwxyz123456，邮箱是 a@test.com",
            }
        ],
    }

    out = await plugin.on_request(request)
    assert out is not None
    text = out["messages"][0]["content"]
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in text
    assert "a@test.com" not in text
    assert re.search(r"__AKM_OPENAI_KEY_[0-9a-f]{12}__", text)
    assert re.search(r"__AKM_EMAIL_[0-9a-f]{12}__", text)


@pytest.mark.asyncio
async def test_request_redactor_masks_sensitive_fields_and_nested_tool_payloads():
    """敏感字段名命中时，应整字段替换为稳定占位符，嵌套字符串仍按路径规则继续脱敏。"""
    plugin = Plugin()
    plugin.config = {"enabled": True}
    plugin.logger = _logger_stub()
    await plugin.on_load()

    request = {
        "model": "gpt-4.1",
        "api_key": "sk-raw-secret-should-not-pass",
        "messages": [
            {
                "role": "user",
                "content": "请记住 github token: ghp_abcdefghijklmnopqrstuvwxyz123456",
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "write",
                    "arguments": {
                        "authorization": "Bearer top-secret-token-value",
                        "note": "联系我：13800138000",
                    },
                },
            }
        ],
    }

    out = await plugin.on_request(request)
    assert out is not None
    assert re.fullmatch(r"__AKM_API_KEY_[0-9a-f]{12}__", out["api_key"])
    assert out["messages"][0]["content"].find("ghp_") == -1
    assert re.search(r"__AKM_GITHUB_TOKEN_[0-9a-f]{12}__", out["messages"][0]["content"])
    assert re.fullmatch(r"__AKM_AUTHORIZATION_[0-9a-f]{12}__", out["tools"][0]["function"]["arguments"]["authorization"])
    assert re.search(r"__AKM_CHINA_PHONE_[0-9a-f]{12}__", out["tools"][0]["function"]["arguments"]["note"])


@pytest.mark.asyncio
async def test_request_redactor_respects_request_text_path_whitelist():
    """路径白名单应避免无关字符串字段被误改。"""
    plugin = Plugin()
    plugin.config = {
        "enabled": True,
        "request_text_paths": "messages[].content",
    }
    plugin.logger = _logger_stub()
    await plugin.on_load()

    request = {
        "model": "gpt-4.1",
        "instructions": "联系邮箱 a@test.com",
        "messages": [{"role": "user", "content": "邮箱 a@test.com"}],
    }

    out = await plugin.on_request(request)
    assert out is not None
    assert re.fullmatch(r"邮箱 __AKM_EMAIL_[0-9a-f]{12}__", out["messages"][0]["content"])
    assert out["instructions"] == "联系邮箱 a@test.com"


@pytest.mark.asyncio
async def test_request_redactor_uses_same_placeholder_for_same_plaintext():
    """同一段明文在同一次请求内多次出现时，应得到完全一致的稳定占位符。"""
    plugin = Plugin()
    plugin.config = {"enabled": True}
    plugin.logger = _logger_stub()
    await plugin.on_load()

    request = {
        "model": "gpt-4.1",
        "messages": [
            {
                "role": "user",
                "content": "邮箱 a@test.com，重复一遍 a@test.com",
            }
        ],
    }

    out = await plugin.on_request(request)
    assert out is not None
    text = out["messages"][0]["content"]
    placeholders = re.findall(r"__AKM_EMAIL_[0-9a-f]{12}__", text)
    assert len(placeholders) == 2
    assert placeholders[0] == placeholders[1]


@pytest.mark.asyncio
async def test_request_redactor_masks_content_blocks_under_message_content_array():
    """当消息内容是 block 数组时，白名单路径也应继续命中内部 text 字段。"""
    plugin = Plugin()
    plugin.config = {"enabled": True}
    plugin.logger = _logger_stub()
    await plugin.on_load()

    request = {
        "model": "gpt-4.1",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "手机号 13800138000"},
                    {"type": "text", "text": "邮箱 a@test.com"},
                ],
            }
        ],
    }

    out = await plugin.on_request(request)
    assert out is not None
    text1 = out["messages"][0]["content"][0]["text"]
    text2 = out["messages"][0]["content"][1]["text"]
    assert "13800138000" not in text1
    assert "a@test.com" not in text2
    assert re.search(r"__AKM_CHINA_PHONE_[0-9a-f]{12}__", text1)
    assert re.search(r"__AKM_EMAIL_[0-9a-f]{12}__", text2)


@pytest.mark.asyncio
async def test_request_redactor_masks_non_first_message_content_entries():
    """白名单中的 messages[].content 应命中任意索引位，而不是只命中第 0 条消息。"""
    plugin = Plugin()
    plugin.config = {"enabled": True}
    plugin.logger = _logger_stub()
    await plugin.on_load()

    request = {
        "model": "gpt-4.1",
        "messages": [
            {"role": "system", "content": "系统消息"},
            {"role": "user", "content": '{ "api_key": "123123123" }'},
        ],
    }

    out = await plugin.on_request(request)
    assert out is not None
    text = out["messages"][1]["content"]
    assert '123123123' not in text
    assert re.search(r'__AKM_CREDENTIAL_VALUE_[0-9a-f]{12}__', text)


@pytest.mark.asyncio
async def test_request_redactor_masks_json_like_secret_assignments_inside_plain_text():
    """普通文本里的 JSON/配置片段也应把敏感字段值替换掉，而不是只识别真实结构化字段。"""
    plugin = Plugin()
    plugin.config = {"enabled": True}
    plugin.logger = _logger_stub()
    await plugin.on_load()

    request = {
        "model": "gpt-4.1",
        "messages": [
            {
                "role": "user",
                "content": '{ "api_key": "123123123" , "password": "abc123" }',
            }
        ],
    }

    out = await plugin.on_request(request)
    assert out is not None
    text = out["messages"][0]["content"]
    assert '"api_key"' in text
    assert '"password"' in text
    assert '123123123' not in text
    assert 'abc123' not in text
    assert re.search(r'__AKM_CREDENTIAL_VALUE_[0-9a-f]{12}__', text)


@pytest.mark.asyncio
async def test_request_redactor_does_not_mutate_json_schema_subtrees_even_with_sensitive_like_keys():
    """schema 子树中的字段定义对象不应因为 key 名像 appId/api_key 而被替换成占位符。"""
    plugin = Plugin()
    plugin.config = {
        "enabled": True,
        "sensitive_fields": "api_key,appId,token",
    }
    plugin.logger = _logger_stub()
    await plugin.on_load()

    request = {
        "model": "gpt-5.4",
        "text": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "appId": {"type": "string"},
                        "api_key": {"type": "string"},
                    },
                    "required": ["appId", "api_key"],
                },
            }
        },
    }

    out = await plugin.on_request(request)
    assert out is None or isinstance(out, dict)
    actual = out if out is not None else request
    assert actual["text"]["format"]["schema"]["properties"]["appId"] == {"type": "string"}
    assert actual["text"]["format"]["schema"]["properties"]["api_key"] == {"type": "string"}
