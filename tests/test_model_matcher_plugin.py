import pytest

from akm.plugins.model_matcher.index import Plugin


@pytest.mark.asyncio
async def test_model_matcher_sets_required_tool_choice_for_gpt_when_enabled():
    plugin = Plugin()
    plugin.config = {"force_tool_choice_required_for_gpt": True}
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}],
    }
    out = await plugin.on_request(req)
    assert out["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_model_matcher_does_not_override_explicit_tool_choice():
    plugin = Plugin()
    plugin.config = {"force_tool_choice_required_for_gpt": True}
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}],
        "tool_choice": "auto",
    }
    out = await plugin.on_request(req)
    assert out is None
    assert req["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_model_matcher_respects_disable_flag_for_tool_choice_policy():
    plugin = Plugin()
    plugin.config = {"force_tool_choice_required_for_gpt": False}
    plugin.logger = type("_L", (), {"info": lambda *args, **kwargs: None})()
    await plugin.on_load()

    req = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}],
    }
    out = await plugin.on_request(req)
    assert out is None
    assert "tool_choice" not in req
