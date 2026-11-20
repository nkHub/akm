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


def test_audit_key_latency_index_created(temp_db):
    """api_list_keys 的「近 10 次延迟」聚合依赖 idx_audit_key_latency。

    缺这条索引时该子查询会全表扫描 audit_logs，且每个 Key 各扫一遍；
    这条查询跑在 async 路由里，实测 100 万行时阻塞 131ms。
    """
    conn = get_connection()
    init_db(conn)
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_logs'"
        )
    }
    assert "idx_audit_key_latency" in names
    conn.close()


def test_audit_key_latency_index_is_used(temp_db):
    """确认查询计划真的走索引，而不是「索引存在但优化器不用」。"""
    conn = get_connection()
    init_db(conn)
    # 灌入足够多的行，让优化器倾向于走索引
    conn.executemany(
        "INSERT INTO audit_logs(key_alias, status_code, latency_ms) VALUES(?, ?, ?)",
        [("k1", 200, 100) for _ in range(2000)],
    )
    conn.commit()
    plan = [
        row[-1]
        for row in conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT latency_ms FROM audit_logs
               WHERE key_alias = 'k1' AND status_code = 200 AND latency_ms > 0
               ORDER BY id DESC LIMIT 10"""
        )
    ]
    joined = " | ".join(plan)
    assert "idx_audit_key_latency" in joined, f"未走索引: {joined}"
    # 必须没有对 audit_logs 的全表扫描
    assert "SCAN audit_logs" not in joined, f"仍存在全表扫描: {joined}"
    conn.close()
