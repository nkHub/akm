import asyncio
import tempfile
import time

import pytest

from akm.db import get_connection, init_db
from akm.audit import AuditLogQueue, write_log, list_logs, clean_logs


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
