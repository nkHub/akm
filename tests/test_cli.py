import tempfile

from click.testing import CliRunner

from akm.cli import main
from akm.db import get_connection, init_db
from akm.key_pool import add_key


def test_key_test_default_mode(monkeypatch):
    """默认模式继续走模型连通性测试，避免改变既有 CLI 语义。"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool.SECRET_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool._cipher", None)

    conn = get_connection()
    init_db(conn)
    conn.close()

    add_key("share", "openai", "sk-test", base_url="https://example.com/v1", models="gpt-5.4")

    async def fake_test_key_connectivity(key, allow_fallback=False):
        assert allow_fallback is False
        return {
            "ok": True,
            "url": "https://example.com/v1/responses",
            "model": "gpt-5.4",
            "api_path": "responses",
            "status_code": 200,
            "latency_ms": 12,
            "error": "",
            "response_body": "",
            "attempted_paths": ["responses"],
            "fallback_used": False,
        }

    monkeypatch.setattr("akm.cli.test_key_connectivity", fake_test_key_connectivity)

    result = CliRunner().invoke(main, ["key", "test", "share"])

    assert result.exit_code == 0
    assert "请求 URL : https://example.com/v1/responses" in result.output
    assert "测试接口 : responses" in result.output
    assert "请求模型 : gpt-5.4" in result.output
    assert "测试模式 : health" not in result.output


def test_key_test_health_mode(monkeypatch):
    """health 模式只请求 /health，适合快速验证共享网关是否在线。"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool.SECRET_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool._cipher", None)

    conn = get_connection()
    init_db(conn)
    conn.close()

    add_key("share", "openai", "sk-test", base_url="https://example.com/codex", models="gpt-5.4")

    async def fake_test_health_endpoint(key):
        assert key["base_url"] == "https://example.com/codex"
        assert key["api_key"] == "sk-test"
        assert key["auth_header"] == "Bearer {api_key}"
        return {
            "ok": True,
            "url": "https://example.com/codex/health",
            "status_code": 200,
            "latency_ms": 8,
            "error": "",
            "response_body": "ok",
        }

    monkeypatch.setattr("akm.cli._test_health_endpoint", fake_test_health_endpoint)

    result = CliRunner().invoke(main, ["key", "test", "share", "--health"])

    assert result.exit_code == 0
    assert "请求 URL : https://example.com/codex/health" in result.output
    assert "测试模式 : health" in result.output
    assert "请求模型" not in result.output


def test_key_test_prints_fallback_chain(monkeypatch):
    """显式启用 fallback 时，CLI 应展示尝试链路。"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool.SECRET_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool._cipher", None)

    conn = get_connection()
    init_db(conn)
    conn.close()

    add_key("share", "openai", "sk-test", base_url="https://example.com/codex", models="gpt-5.4")

    async def fake_test_key_connectivity(key, allow_fallback=False):
        assert allow_fallback is True
        return {
            "ok": True,
            "url": "https://example.com/v1/chat/completions",
            "model": "gpt-5.4",
            "api_path": "chat/completions",
            "status_code": 200,
            "latency_ms": 16,
            "error": "",
            "response_body": "",
            "attempted_paths": ["responses", "chat/completions"],
            "fallback_used": True,
        }

    monkeypatch.setattr("akm.cli.test_key_connectivity", fake_test_key_connectivity)

    result = CliRunner().invoke(main, ["key", "test", "share", "--fallback"])

    assert result.exit_code == 0
    assert "测试接口 : chat/completions" in result.output
    assert "回退链路 : responses -> chat/completions" in result.output
