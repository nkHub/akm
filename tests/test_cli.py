import tempfile
from pathlib import Path
import json
from json import JSONDecodeError

from click.testing import CliRunner

from akm.cli import main
from akm.config import load_config
from akm.db import get_connection, init_db
from akm.key_pool import add_key
from akm.audit import write_log


def _setup_tmp_env(monkeypatch):
    """为每条 CLI 测试隔离数据库、配置与密钥目录，避免互相污染。"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setenv("HOME", tmpdir)
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool.SECRET_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool._cipher", None)
    monkeypatch.setattr("akm.config.CONFIG_DIR", tmpdir)
    monkeypatch.setattr("akm.config.CONFIG_PATH", str(Path(tmpdir) / "config.json"))
    return tmpdir


def test_key_test_default_mode(monkeypatch):
    """默认模式继续走模型连通性测试，避免改变既有 CLI 语义。"""
    tmpdir = _setup_tmp_env(monkeypatch)

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
    tmpdir = _setup_tmp_env(monkeypatch)

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
    tmpdir = _setup_tmp_env(monkeypatch)

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


def test_config_get_and_set(monkeypatch):
    """config get/set 应保持默认类型，不把整数和布尔误写成字符串。"""
    _setup_tmp_env(monkeypatch)
    runner = CliRunner()

    set_result = runner.invoke(main, ["config", "set", "server_port", "9900"])
    assert set_result.exit_code == 0
    assert "配置已更新: server_port=9900" in set_result.output

    get_result = runner.invoke(main, ["config", "get", "server_port"])
    assert get_result.exit_code == 0
    assert get_result.output.strip() == "9900"
    assert load_config()["server_port"] == 9900


def test_config_set_bool(monkeypatch):
    """布尔配置应支持 true/false 文本输入。"""
    _setup_tmp_env(monkeypatch)

    result = CliRunner().invoke(main, ["config", "set", "auto_open_admin", "false"])

    assert result.exit_code == 0
    assert load_config()["auto_open_admin"] is False


def test_plugin_list_enable_disable(monkeypatch):
    """插件列表和启停命令应基于 plugin manager 正常工作。"""
    _setup_tmp_env(monkeypatch)
    runner = CliRunner()

    list_result = runner.invoke(main, ["plugin", "list"])
    assert list_result.exit_code == 0
    assert "[error_handler] 错误处理" in list_result.output
    assert "[model_matcher] 模型匹配" in list_result.output

    disable_result = runner.invoke(main, ["plugin", "disable", "error_handler"])
    assert disable_result.exit_code == 0
    assert "状态已保存，重启 akm 后生效" in disable_result.output

    enable_result = runner.invoke(main, ["plugin", "enable", "error_handler"])
    assert enable_result.exit_code == 0
    assert "状态已保存，重启 akm 后生效" in enable_result.output


def test_plugin_config_get_and_set(monkeypatch):
    """plugin config get/set 应支持按 schema 类型读写插件配置。"""
    _setup_tmp_env(monkeypatch)
    runner = CliRunner()

    get_result = runner.invoke(main, ["plugin", "config", "get", "error_handler", "max_retries_per_key"])
    assert get_result.exit_code == 0
    assert get_result.output.strip() == "3"

    set_result = runner.invoke(main, ["plugin", "config", "set", "error_handler", "max_retries_per_key", "7"])
    assert set_result.exit_code == 0
    assert "插件配置已更新: error_handler.max_retries_per_key=7" in set_result.output

    updated_result = runner.invoke(main, ["plugin", "config", "get", "error_handler", "max_retries_per_key"])
    assert updated_result.exit_code == 0
    assert updated_result.output.strip() == "7"


def test_status_summary(monkeypatch):
    """status 应输出服务、Key、日志和插件概览，便于快速自检。"""
    _setup_tmp_env(monkeypatch)
    conn = get_connection()
    init_db(conn)
    conn.close()
    add_key("share", "openai", "sk-test", base_url="https://example.com/v1", models="gpt-5.4")

    monkeypatch.setattr("akm.cli.count_logs", lambda: 12)
    monkeypatch.setattr("akm.cli._get_service_health", lambda base_url: (True, "运行中 (200)"))

    result = CliRunner().invoke(main, ["status"])

    assert result.exit_code == 0
    assert "AKM 版本" in result.output
    assert "服务状态      : 正常，运行中 (200)" in result.output
    assert "Key 概览       : 总数 1，启用 1，禁用 0" in result.output
    assert "审计日志      : 共 12 条" in result.output
    assert "插件概览      : 总数" in result.output


def test_key_show_and_edit(monkeypatch):
    """key show 应展示详情，key edit 应统一更新多个字段。"""
    _setup_tmp_env(monkeypatch)
    conn = get_connection()
    init_db(conn)
    conn.close()
    add_key("share", "openai", "sk-test-123456", base_url="https://example.com/v1", models="gpt-5.4")

    runner = CliRunner()
    show_result = runner.invoke(main, ["key", "show", "share"])
    assert show_result.exit_code == 0
    assert "别名           : share" in show_result.output
    assert "API Key        : sk-tes...3456" in show_result.output

    edit_result = runner.invoke(
        main,
        [
            "key", "edit", "share",
            "--models", "gpt-4o,gpt-4.1",
            "--priority", "2",
            "--status", "disabled",
        ],
    )
    assert edit_result.exit_code == 0
    assert "Key 'share' 已更新: models, priority, status" in edit_result.output

    updated = runner.invoke(main, ["key", "show", "share"])
    assert "模型配置       : gpt-4o,gpt-4.1" in updated.output
    assert "优先级         : 2" in updated.output
    assert "状态           : disabled" in updated.output


def test_log_stats(monkeypatch):
    """log stats 应输出总量、成功失败与 provider/model 聚合。"""
    _setup_tmp_env(monkeypatch)
    conn = get_connection()
    init_db(conn)
    conn.close()

    write_log({"provider": "openai", "key_alias": "k1", "model": "gpt-4o", "status_code": 200, "latency_ms": 100})
    write_log({"provider": "openai", "key_alias": "k1", "model": "gpt-4o", "status_code": 500, "latency_ms": 200, "error": "boom"})
    write_log({"provider": "deepseek", "key_alias": "k2", "model": "deepseek-chat", "status_code": 200, "latency_ms": 300})

    result = CliRunner().invoke(main, ["log", "stats"])

    assert result.exit_code == 0
    assert "总日志数       : 3" in result.output
    assert "成功请求       : 2" in result.output
    assert "失败请求       : 1" in result.output
    assert "供应商 Top5" in result.output
    assert "openai: 2" in result.output
    assert "gpt-4o: 2" in result.output


def test_doctor(monkeypatch):
    """doctor 应给出本地自检结果，并在服务未运行时至少返回 WARN。"""
    _setup_tmp_env(monkeypatch)
    conn = get_connection()
    init_db(conn)
    conn.close()

    monkeypatch.setattr("akm.cli._get_service_health", lambda base_url: (False, "未运行 (connection refused)"))

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "[OK] config" in result.output
    assert "[OK] database" in result.output
    assert "[WARN] service" in result.output


def test_key_health(monkeypatch):
    """key health 应批量输出巡检结果和最终汇总。"""
    _setup_tmp_env(monkeypatch)
    conn = get_connection()
    init_db(conn)
    conn.close()
    add_key("k1", "openai", "sk-one", base_url="https://example.com/v1", models="gpt-4o")
    add_key("k2", "openai", "sk-two", base_url="https://example.com/v1", models="gpt-4.1")

    async def fake_test_key_connectivity(key, allow_fallback=False):
        if key["alias"] == "k1":
            return {
                "ok": True,
                "url": "https://example.com/v1/responses",
                "model": key["models"],
                "api_path": "responses",
                "status_code": 200,
                "latency_ms": 18,
                "error": "",
                "response_body": "",
            }
        return {
            "ok": False,
            "url": "https://example.com/v1/responses",
            "model": key["models"],
            "api_path": "responses",
            "status_code": 500,
            "latency_ms": 0,
            "error": "boom",
            "response_body": "",
        }

    monkeypatch.setattr("akm.cli.test_key_connectivity", fake_test_key_connectivity)

    result = CliRunner().invoke(main, ["key", "health", "--provider", "openai"])

    assert result.exit_code == 0
    assert "[OK] k1 provider=openai latency=18ms" in result.output
    assert "[FAIL] k2 provider=openai status=500 error=boom" in result.output
    assert "巡检完成：成功 1，失败 1，总计 2" in result.output


def test_image_generate_outputs_raw_json(monkeypatch):
    """image generate 成功时应只输出 JSON，便于脚本或 MCP 直接解析。"""
    _setup_tmp_env(monkeypatch)

    async def fake_generate_image_via_local_service(payload, timeout=120.0):
        assert payload == {
            "prompt": "a cat astronaut",
            "model": "gpt-image-2",
            "size": "1024x1024",
            "quality": "high",
            "n": 2,
        }
        assert timeout == 45.0
        return {
            "created": 123,
            "data": [
                {"url": "https://example.com/cat-1.png"},
                {"url": "https://example.com/cat-2.png"},
            ],
        }

    monkeypatch.setattr("akm.cli._generate_image_via_local_service", fake_generate_image_via_local_service)

    result = CliRunner().invoke(
        main,
        [
            "image", "generate", "a cat astronaut",
            "--model", "gpt-image-2",
            "--size", "1024x1024",
            "--quality", "high",
            "--n", "2",
            "--timeout", "45",
        ],
    )

    assert result.exit_code == 0
    assert result.output.strip() == '{"created": 123, "data": [{"url": "https://example.com/cat-1.png"}, {"url": "https://example.com/cat-2.png"}]}'


def test_image_generate_uses_configured_default_timeout(monkeypatch):
    """未显式传 --timeout 时，图片生成应读取 image_request_timeout_sec。"""
    _setup_tmp_env(monkeypatch)
    monkeypatch.setattr("akm.cli.config_module.load_config", lambda: {"image_request_timeout_sec": 300, "server_port": 8800})

    async def fake_generate_image_via_local_service(payload, timeout=120.0):
        assert timeout == 300.0
        return {"created": 123, "data": [{"url": "https://example.com/cat-1.png"}]}

    monkeypatch.setattr("akm.cli._generate_image_via_local_service", fake_generate_image_via_local_service)

    result = CliRunner().invoke(main, ["image", "generate", "a cat astronaut"])

    assert result.exit_code == 0


def test_image_generate_surfaces_service_error(monkeypatch):
    """image generate 失败时应返回非 0，并保留可读错误，方便外部调用方判断。"""
    _setup_tmp_env(monkeypatch)

    async def fake_fail(payload, timeout=120.0):
        from click import ClickException
        raise ClickException("图片生成失败：服务未启动")

    monkeypatch.setattr("akm.cli._generate_image_via_local_service", fake_fail)

    result = CliRunner().invoke(main, ["image", "generate", "a cat astronaut"])

    assert result.exit_code != 0
    assert "图片生成失败：服务未启动" in result.output


def test_image_generate_includes_non_json_response_preview(monkeypatch):
    """image generate 遇到非 JSON 错误页时应回显摘要，方便直接定位上游异常。"""
    _setup_tmp_env(monkeypatch)

    class FakeResponse:
        status_code = 502
        headers = {
            "content-type": "text/html; charset=utf-8",
            "server": "mock-gateway",
            "content-length": "51",
        }
        text = "<html><body>Bad Gateway from upstream</body></html>"

        def json(self):
            raise JSONDecodeError("Expecting value", self.text, 0)

    class FakeAsyncClient:
        def __init__(self, timeout):
            assert timeout == 300.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            assert url.endswith("/v1/images/generations")
            return FakeResponse()

    monkeypatch.setattr("akm.cli.httpx.AsyncClient", FakeAsyncClient)

    result = CliRunner().invoke(main, ["image", "generate", "a cat astronaut"])

    assert result.exit_code != 0
    assert "HTTP 502" in result.output
    assert "content-type=text/html; charset=utf-8" in result.output
    assert "server=mock-gateway" in result.output
    assert "content-length=51" in result.output
    assert "Bad Gateway from upstream" in result.output


def test_image_edit_outputs_raw_json(monkeypatch):
    """image edit 成功时应透传 JSON，方便外部工具直接消费。"""
    _setup_tmp_env(monkeypatch)

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("cat.png").write_bytes(b"fake-image")
        Path("mask.png").write_bytes(b"fake-mask")

        async def fake_edit_image_via_local_service(form_data, file_specs, timeout=120.0):
            assert form_data == {
                "prompt": "remove background",
                "model": "gpt-image-2",
                "size": "1024x1024",
                "n": "1",
            }
            assert timeout == 30.0
            assert file_specs[0][0] == "image"
            assert file_specs[0][1][0] == "cat.png"
            assert file_specs[0][1][1] == b"fake-image"
            assert file_specs[1][0] == "mask"
            assert file_specs[1][1][0] == "mask.png"
            assert file_specs[1][1][1] == b"fake-mask"
            return {
                "created": 456,
                "data": [{"url": "https://example.com/edited-cat.png"}],
            }

        monkeypatch.setattr("akm.cli._edit_image_via_local_service", fake_edit_image_via_local_service)

        result = runner.invoke(
            main,
            [
                "image", "edit", "cat.png",
                "--prompt", "remove background",
                "--mask", "mask.png",
                "--model", "gpt-image-2",
                "--size", "1024x1024",
                "--n", "1",
                "--timeout", "30",
            ],
        )

    assert result.exit_code == 0
    assert json.loads(result.output.strip())["data"][0]["url"] == "https://example.com/edited-cat.png"


def test_image_edit_uses_configured_default_timeout(monkeypatch):
    """未显式传 --timeout 时，图片编辑应读取 image_request_timeout_sec。"""
    _setup_tmp_env(monkeypatch)
    monkeypatch.setattr("akm.cli.config_module.load_config", lambda: {"image_request_timeout_sec": 300, "server_port": 8800})

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("cat.png").write_bytes(b"fake-image")

        async def fake_edit_image_via_local_service(form_data, file_specs, timeout=120.0):
            assert timeout == 300.0
            return {"created": 456, "data": [{"url": "https://example.com/edited-cat.png"}]}

        monkeypatch.setattr("akm.cli._edit_image_via_local_service", fake_edit_image_via_local_service)

        result = runner.invoke(
            main,
            [
                "image", "edit", "cat.png",
                "--prompt", "remove background",
            ],
        )

    assert result.exit_code == 0


def test_image_edit_includes_non_json_response_preview(monkeypatch):
    """image edit 遇到非 JSON 错误页时也应保留响应摘要，避免只能看到笼统报错。"""
    _setup_tmp_env(monkeypatch)

    class FakeResponse:
        status_code = 502
        headers = {
            "content-type": "text/plain",
            "server": "mock-gateway",
            "content-length": "33",
        }
        text = "upstream image edit gateway error"

        def json(self):
            raise JSONDecodeError("Expecting value", self.text, 0)

    class FakeAsyncClient:
        def __init__(self, timeout):
            assert timeout == 300.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, data, files):
            assert url.endswith("/v1/images/edits")
            return FakeResponse()

    monkeypatch.setattr("akm.cli.httpx.AsyncClient", FakeAsyncClient)

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("cat.png").write_bytes(b"fake-image")
        result = runner.invoke(main, ["image", "edit", "cat.png", "--prompt", "remove background"])

    assert result.exit_code != 0
    assert "HTTP 502" in result.output
    assert "content-type=text/plain" in result.output
    assert "server=mock-gateway" in result.output
    assert "content-length=33" in result.output
    assert "upstream image edit gateway error" in result.output


def test_image_edit_reports_missing_file(monkeypatch):
    """image edit 遇到不存在的输入文件时应直接报错，避免发出空请求。"""
    _setup_tmp_env(monkeypatch)

    result = CliRunner().invoke(main, ["image", "edit", "missing.png", "--prompt", "remove background"])

    assert result.exit_code != 0
    assert "文件不存在: missing.png" in result.output
