import asyncio
import tempfile
import time

import pytest

from akm.db import get_connection, init_db
from akm.audit import AuditLogQueue, write_log, list_logs, count_logs, clean_logs, clean_log_bodies


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    conn = get_connection()
    init_db(conn)
    yield conn
    conn.close()


def test_write_and_list_log(setup):
    write_log({
        "provider": "openai",
        "key_alias": "my-key",
        "model": "gpt-4",
        "request_body": '{"model":"gpt-4"}',
        "response_body": '{"choices":[]}',
        "status_code": 200,
        "latency_ms": 350,
        "error": "",
    })
    logs = list_logs(limit=10)
    assert len(logs) == 1
    log = logs[0]
    assert log["provider"] == "openai"
    assert log["key_alias"] == "my-key"
    assert log["status_code"] == 200
    assert log["latency_ms"] == 350


def test_write_log_error(setup):
    write_log({
        "provider": "openai",
        "key_alias": "bad-key",
        "model": "gpt-4",
        "request_body": "{}",
        "response_body": "",
        "status_code": 0,
        "latency_ms": 0,
        "error": "Connection timeout",
    })
    logs = list_logs()
    assert logs[0]["error"] == "Connection timeout"


def test_list_logs_by_provider(setup):
    write_log({"provider": "openai", "key_alias": "a", "model": "g", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    write_log({"provider": "deepseek", "key_alias": "b", "model": "d", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    logs = list_logs(provider="deepseek", limit=10)
    assert len(logs) == 1
    assert logs[0]["key_alias"] == "b"


def test_clean_logs(setup):
    write_log({"provider": "o", "key_alias": "k", "model": "m", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    assert len(list_logs()) == 1
    # 清理未来日期的日志会删除所有内容
    count = clean_logs("2099-01-01")
    assert count == 1
    assert len(list_logs()) == 0


def test_clean_logs_partial(setup):
    write_log({"provider": "o", "key_alias": "k1", "model": "m", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    time.sleep(0.01)
    write_log({"provider": "o", "key_alias": "k2", "model": "m", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    # 清理很旧的数据不影响
    count = clean_logs("2000-01-01")
    assert count == 0
    assert len(list_logs()) == 2


def test_write_log_three_stage_trace_fields(setup):
    """三段式审计链路字段应完整写入并读回。"""
    write_log({
        "provider": "openai",
        "key_alias": "my-key",
        "model": "gpt-4",
        "request_body": '{"model":"gpt-4"}',
        "response_body": '{"choices":[]}',
        "status_code": 200,
        "latency_ms": 100,
        "error": "",
        "client_request_headers": '{"authorization":"Bearer ***","x-custom":"abc"}',
        "client_request_body": '{"model":"gpt-4","messages":[]}',
        "upstream_request_headers": '{"Authorization":"Bearer ***"}',
        "upstream_response_body": '{"type":"message","content":[]}',
    })
    logs = list_logs(limit=10)
    assert len(logs) == 1
    log = logs[0]
    assert log["client_request_headers"] == '{"authorization":"Bearer ***","x-custom":"abc"}'
    assert log["client_request_body"] == '{"model":"gpt-4","messages":[]}'
    assert log["upstream_request_headers"] == '{"Authorization":"Bearer ***"}'
    assert log["upstream_response_body"] == '{"type":"message","content":[]}'


def test_clean_log_bodies_clears_client_request_body(setup):
    """清理请求/响应体时清空大体积请求头快照，但保留来源头。"""
    write_log({
        "provider": "o", "key_alias": "k", "model": "m",
        "request_body": '{"a":1}', "response_body": '{"b":2}',
        "status_code": 200, "latency_ms": 0, "error": "",
        "client_request_body": '{"orig":true}',
        "upstream_response_body": '{"raw":true}',
        "request_headers": '{"user-agent":"x"}',
        "client_request_headers": '{"host":"x"}',
        "upstream_request_headers": '{"authorization":"Bearer ***"}',
    })
    count = clean_log_bodies()
    assert count == 1
    logs = list_logs()
    assert logs[0]["request_body"] == ""
    assert logs[0]["response_body"] == ""
    assert logs[0]["client_request_body"] == ""
    assert logs[0]["upstream_response_body"] == ""
    # request_headers 是轻量来源头，供审计列表显示来源和徽章，清理正文时必须保留。
    assert logs[0]["request_headers"] == '{"user-agent":"x"}'
    assert logs[0]["client_request_headers"] == ""
    assert logs[0]["upstream_request_headers"] == ""


def test_list_logs_hide_empty_matches_any_body_column(setup):
    """“仅对话”(hide_empty) 应命中任一请求/响应体列，而非只看转换后列。

    schema 演进后客户端原始请求体/上游响应体分别落在
    client_request_body / upstream_response_body，request_body / response_body
    仅在发生协议转换时落库；普通透传（无转换）的对话行只有新列有内容，
    因此过滤必须多列 OR，否则“仅对话”会漏掉无转换的普通对话。
    """
    write_log({
        "provider": "deepseek", "key_alias": "k1", "model": "deepseek-v4",
        "request_body": "", "response_body": "",
        "status_code": 200, "latency_ms": 500, "error": "",
        "client_request_body": '{"model":"deepseek-v4","messages":[]}',
        "upstream_response_body": '{"choices":[]}',
    })
    write_log({
        "provider": "openai", "key_alias": "k2", "model": "gpt-4",
        "request_body": '{"model":"gpt-4"}', "response_body": '{"choices":[]}',
        "status_code": 200, "latency_ms": 500, "error": "",
    })
    write_log({
        "provider": "openai", "key_alias": "k3", "model": "gpt-4",
        "request_body": "", "response_body": "",
        "status_code": 502, "latency_ms": 0, "error": "timeout",
    })

    shown = list_logs(limit=10, hide_empty=True)
    assert len(shown) == 2
    assert {r["key_alias"] for r in shown} == {"k1", "k2"}
    assert count_logs(hide_empty=True) == 2
    assert count_logs() == 3


def test_clean_log_bodies_noop_when_nothing_to_clean(setup):
    """所有 body 均为空时，clean_log_bodies 不应影响行数。"""
    write_log({
        "provider": "o", "key_alias": "k", "model": "m",
        "request_body": "", "response_body": "",
        "status_code": 200, "latency_ms": 0, "error": "",
    })
    assert clean_log_bodies() == 0


def test_list_logs_hide_est_filters_only_low_latency_metadata_rows(setup):
    write_log({
        "provider": "openai",
        "key_alias": "k1",
        "model": "gpt-5.4",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 3,
        "error": "",
        "request_headers": '{"x-akm-flags":"usage_estimated_light"}',
        "prompt_tokens": 24000,
        "completion_tokens": 100,
        "total_tokens": 24100,
    })
    write_log({
        "provider": "openai",
        "key_alias": "k1",
        "model": "gpt-5.4",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 3000,
        "error": "",
        "request_headers": '{"x-akm-flags":"usage_estimated_light"}',
        "prompt_tokens": 24000,
        "completion_tokens": 800,
        "total_tokens": 24800,
    })
    write_log({
        "provider": "openai",
        "key_alias": "k1",
        "model": "gpt-5.4",
        "request_body": "",
        "response_body": "",
        "status_code": 200,
        "latency_ms": 200,
        "error": "",
        "request_headers": '{}',
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    })

    hidden = list_logs(limit=10, hide_est=True)
    shown = list_logs(limit=10, hide_est=False)

    assert len(shown) == 3
    assert len(hidden) == 2
    assert count_logs(hide_est=True) == 2
    totals = sorted(row["total_tokens"] for row in hidden)
    assert totals == [120, 24800]


@pytest.mark.asyncio
async def test_audit_log_queue_drops_when_full(monkeypatch):
    """有界审计队列满载时应丢弃新增任务，而不是无限堆积后台任务。"""

    queue = AuditLogQueue(maxsize=1)

    gate = asyncio.Event()

    async def fake_write_log_async(data):
        await gate.wait()

    monkeypatch.setattr("akm.audit.write_log_async", fake_write_log_async)

    await queue.start()
    try:
        assert await queue.submit({"provider": "a"}) is True
        assert await queue.submit({"provider": "b"}) is False
        assert queue.dropped_count == 1
        assert queue.qsize() == 1
    finally:
        gate.set()
        await queue.stop()
