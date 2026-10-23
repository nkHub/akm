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
        # Agent 运行期的三类旁路记录：布局小模型 / 上下文摘要 / 本地工具调用
        ({"x-akm-source": "layout", "user-agent": "agent/1.0"}, "Layout"),
        ({"x-akm-source": "summary", "user-agent": "agent/1.0"}, "Summary"),
        ({"x-akm-source": "tool", "user-agent": "agent/1.0"}, "Tool"),
        # 工具调用记录按触发来源细分（对话 / 定时任务）
        ({"x-akm-source": "chat-tool", "user-agent": "agent/1.0"}, "Chat Tool"),
        ({"x-akm-source": "task-tool", "user-agent": "agent/1.0"}, "Task Tool"),
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


def test_build_trace_headers_keeps_akm_source_markers():
    """`_build_trace_headers` 的白名单必须包含 x-akm-source / x-akm-plugin。

    为什么必须锁这条：`extract_source()` 只读落库的 `request_headers`，
    而该列由白名单过滤生成。白名单漏掉这两个键时，插件与 Agent 旁路请求
    即使正确带了标记也读不到 —— 表现就是 markdown_kb 的向量化/摘要请求
    一直被归成「Python」，全库零条 plugin 记录（实测踩过）。
    """
    from starlette.requests import Request

    from akm.server import _build_trace_headers

    def make_request(headers: dict) -> Request:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/embeddings",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "query_string": b"",
        }
        return Request(scope)

    # 插件请求：两个标记都要落库，来源应为「AKM Plugins」
    _, payload = _build_trace_headers(
        make_request(
            {
                "user-agent": "python-httpx/0.28.1",
                "host": "127.0.0.1:8800",
                "x-akm-source": "plugin",
                "x-akm-plugin": "markdown_kb",
            }
        )
    )
    assert '"x-akm-source": "plugin"' in payload
    assert '"x-akm-plugin": "markdown_kb"' in payload
    assert extract_source(payload) == "AKM Plugins"

    # Agent 工具审计：来源应为「Chat Tool」
    _, payload2 = _build_trace_headers(
        make_request({"user-agent": "python-httpx/0.28.1", "x-akm-source": "chat-tool"})
    )
    assert extract_source(payload2) == "Chat Tool"

    # 真正的外部 Python 客户端（没有内部标记）仍应归为「Python」
    _, payload3 = _build_trace_headers(
        make_request({"user-agent": "python-httpx/0.28.1", "host": "127.0.0.1:8800"})
    )
    assert extract_source(payload3) == "Python"
