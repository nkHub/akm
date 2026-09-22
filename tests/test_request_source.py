"""审计日志来源标签推导：akm.request_source.extract_source 单元测试。"""

import json

import pytest

from akm.request_source import UNKNOWN_SOURCE, extract_source


def _headers(payload: dict) -> str:
    return json.dumps(payload)


@pytest.mark.parametrize(
    "payload,expected",
    [
        # x-akm-source 内部标记优先于 User-Agent
        ({"x-akm-source": "chat", "user-agent": "curl/8.0"}, "Chat"),
        ({"x-akm-source": "FLOW", "user-agent": "curl/8.0"}, "Flow"),
        ({"x-akm-source": "task"}, "Task"),
        # 未知内部标记不生效，回落到 UA 推断
        ({"x-akm-source": "custom", "user-agent": "curl/8.0"}, "curl"),
        # UA 关键词
        ({"user-agent": "OpenCode/1.2.3"}, "OpenCode"),
        ({"user-agent": "claude-cli/1.0.0 (external)"}, "Claude"),
        ({"user-agent": "Claude Code/2.0"}, "Claude"),
        ({"user-agent": "codex_cli_rs/0.5"}, "Codex"),
        ({"user-agent": "Cursor/0.42"}, "Cursor"),
        ({"user-agent": "curl/8.4.0"}, "curl"),
        ({"user-agent": "python-httpx/0.27"}, "Python"),
        # 兜底：UA 斜杠前的产品名（小写）
        ({"user-agent": "DeepSeek-Harness/1.0"}, "deepseek-harness"),
        # 无法识别
        ({}, UNKNOWN_SOURCE),
        ({"user-agent": ""}, UNKNOWN_SOURCE),
        ({"user-agent": "/1.0"}, UNKNOWN_SOURCE),
    ],
)
def test_extract_source_labels(payload, expected):
    assert extract_source(_headers(payload)) == expected


def test_extract_source_accepts_dict_and_bad_input():
    assert extract_source({"x-akm-source": "chat"}) == "Chat"
    assert extract_source("") == UNKNOWN_SOURCE
    assert extract_source("not-json") == UNKNOWN_SOURCE
    assert extract_source(None) == UNKNOWN_SOURCE
    assert extract_source(["x-akm-source"]) == UNKNOWN_SOURCE
