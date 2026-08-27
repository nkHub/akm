"""端到端测试：验证 CLI 操作 + 服务请求的完整流程"""

import os
import subprocess
import sys
import time
import pytest
import requests


def _test_env(home: str) -> dict[str, str]:
    """为子进程提供独立配置目录，避免修改 pytest 进程的全局环境。

    必须移除 AKM_DB_DIR：conftest 会把它设为会话级共享临时库，若保留，
    子进程将写入/读取同一共享库，导致测试之间（如 serve 产生日志、log list
    断言无日志）互相污染；移除后可回退到 HOME/.akm 实现每用例隔离。
    """
    env = {**os.environ, "HOME": home}
    env.pop("AKM_DB_DIR", None)
    return env


def _run_cli(home: str, *args: str, **kwargs):
    """通过当前测试解释器调用 CLI，避免 PATH 指向其他安装版本。"""
    return subprocess.run(
        [sys.executable, "-m", "akm.cli", *args],
        env=_test_env(home),
        **kwargs,
    )


def _wait_for_health(proc: subprocess.Popen, port: int) -> None:
    """轮询健康接口，既等待正常启动，也能在服务提前退出时给出错误输出。"""
    deadline = time.monotonic() + 10
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            response = requests.get(url, timeout=0.5)
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        if proc.poll() is not None:
            _, stderr = proc.communicate()
            pytest.fail(f"服务启动失败: {stderr.decode(errors='replace')}")
        time.sleep(0.1)
    proc.terminate()
    _, stderr = proc.communicate(timeout=5)
    pytest.fail(f"服务未在 10 秒内启动: {stderr.decode(errors='replace')}")


def _stop_server(proc: subprocess.Popen) -> None:
    """确保测试失败时也能回收服务进程。"""
    if proc.poll() is None:
        proc.terminate()
    proc.wait(timeout=5)


def test_cli_key_add_list_remove(tmp_path):
    """测试 key 的增删查完整流程"""
    home = str(tmp_path)
    # 添加 key
    r = _run_cli(
        home, "key", "add", "e2e-test", "openai", "--models", "gpt-4",
        input="sk-test123\n",
        text=True,
        capture_output=True,
    )
    assert "添加成功" in r.stdout, r.stderr
    # 列出
    r = _run_cli(home, "key", "list", capture_output=True, text=True)
    assert "e2e-test" in r.stdout
    assert "openai" in r.stdout
    # 删除
    r = _run_cli(home, "key", "remove", "e2e-test", capture_output=True, text=True)
    assert "已删除" in r.stdout
    # 确认已删
    r = _run_cli(home, "key", "list", capture_output=True, text=True)
    assert "暂无" in r.stdout


def test_cli_key_priority_and_status(tmp_path):
    """测试优先级设置和启用/禁用"""
    home = str(tmp_path)
    r = _run_cli(
        home, "key", "add", "prio", "openai",
        input="sk-prio\n",
        text=True,
        capture_output=True,
    )
    assert "添加成功" in r.stdout
    # 设置优先级
    _run_cli(home, "key", "set-priority", "prio", "99", capture_output=True, text=True)
    r = _run_cli(home, "key", "list", capture_output=True, text=True)
    assert "优先级=99" in r.stdout
    # 禁用
    _run_cli(home, "key", "disable", "prio", capture_output=True, text=True)
    r = _run_cli(home, "key", "list", capture_output=True, text=True)
    assert "状态=disabled" in r.stdout
    # 启用
    _run_cli(home, "key", "enable", "prio", capture_output=True, text=True)
    r = _run_cli(home, "key", "list", capture_output=True, text=True)
    assert "状态=active" in r.stdout


def test_server_startup_and_health(tmp_path):
    """测试服务启动和健康检查"""
    home = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "akm.cli", "serve", "--port", "18800"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_test_env(home),
    )
    try:
        _wait_for_health(proc, 18800)
        resp = requests.get("http://127.0.0.1:18800/health", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        _stop_server(proc)


def test_proxy_request_no_keys(tmp_path):
    """无 key 时代理请求返回 503"""
    home = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "akm.cli", "serve", "--port", "18801"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_test_env(home),
    )
    try:
        _wait_for_health(proc, 18801)
        resp = requests.post(
            "http://127.0.0.1:18801/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
            timeout=5,
        )
        assert resp.status_code == 503
    finally:
        _stop_server(proc)


def test_log_list(tmp_path):
    """测试日志查看"""
    home = str(tmp_path)
    # 无日志时
    r = _run_cli(home, "log", "list", capture_output=True, text=True)
    assert "暂无日志" in r.stdout
