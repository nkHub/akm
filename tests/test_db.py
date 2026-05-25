import os
import tempfile
import pytest
from akm.db import get_db_path, get_connection, init_db


@pytest.fixture
def temp_db(monkeypatch):
    """使用临时目录隔离测试数据库"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    yield tmpdir


def test_get_db_path(temp_db):
    path = get_db_path()
    assert path.endswith("akm.db")
    assert path.startswith(temp_db)


def test_init_db_creates_tables(temp_db):
    conn = get_connection()
    init_db(conn)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert "keys" in tables
    assert "audit_logs" in tables
    conn.close()


def test_keys_table_schema(temp_db):
    conn = get_connection()
    init_db(conn)
    cursor = conn.execute("PRAGMA table_info(keys)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}
    assert columns["alias"] == "TEXT"
    assert columns["provider"] == "TEXT"
    assert columns["api_key"] == "TEXT"
    assert columns["base_url"] == "TEXT"
    assert columns["auth_header"] == "TEXT"
    assert columns["models"] == "TEXT"
    assert columns["priority"] == "INTEGER"
    assert columns["status"] == "TEXT"
    conn.close()


def test_audit_logs_table_schema(temp_db):
    conn = get_connection()
    init_db(conn)
    cursor = conn.execute("PRAGMA table_info(audit_logs)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}
    assert columns["timestamp"] == "TEXT"
    assert columns["provider"] == "TEXT"
    assert columns["key_alias"] == "TEXT"
    assert columns["model"] == "TEXT"
    assert columns["request_body"] == "TEXT"
    assert columns["response_body"] == "TEXT"
    assert columns["status_code"] == "INTEGER"
    assert columns["latency_ms"] == "INTEGER"
    assert columns["error"] == "TEXT"
    conn.close()
