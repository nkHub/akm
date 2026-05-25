"""端到端测试：验证 CLI 操作 + 服务请求的完整流程"""

import os
import tempfile
import subprocess
import time
import pytest
import requests


def _set_home(tmpdir):
    """设置隔离的 HOME 目录用于测试"""
    os.environ["HOME"] = tmpdir


@pytest.fixture(autouse=True)
def cleanup_home():
    """测试结束后恢复 HOME"""
    original = os.environ.get("HOME")
    yield
    if original:
        os.environ["HOME"] = original


def test_cli_key_add_list_remove():
    """测试 key 的增删查完整流程"""
    tmpdir = tempfile.mkdtemp()
    _set_home(tmpdir)
    # 添加 key
    r = subprocess.run(
        ["akm", "key", "add", "e2e-test", "openai", "--models", "gpt-4"],
        input="sk-test123\n",
        text=True,
        capture_output=True,
    )
    assert "添加成功" in r.stdout, r.stderr
    # 列出
    r = subprocess.run(["akm", "key", "list"], capture_output=True, text=True)
    assert "e2e-test" in r.stdout
    assert "openai" in r.stdout
    # 删除
    r = subprocess.run(["akm", "key", "remove", "e2e-test"], capture_output=True, text=True)
    assert "已删除" in r.stdout
    # 确认已删
    r = subprocess.run(["akm", "key", "list"], capture_output=True, text=True)
    assert "暂无" in r.stdout


def test_cli_key_priority_and_status():
    """测试优先级设置和启用/禁用"""
    tmpdir = tempfile.mkdtemp()
    _set_home(tmpdir)
    r = subprocess.run(
        ["akm", "key", "add", "prio", "openai"],
        input="sk-prio\n",
        text=True,
        capture_output=True,
    )
    assert "添加成功" in r.stdout
    # 设置优先级
    subprocess.run(["akm", "key", "set-priority", "prio", "99"], capture_output=True, text=True)
    r = subprocess.run(["akm", "key", "list"], capture_output=True, text=True)
    assert "优先级=99" in r.stdout
    # 禁用
    subprocess.run(["akm", "key", "disable", "prio"], capture_output=True, text=True)
    r = subprocess.run(["akm", "key", "list"], capture_output=True, text=True)
    assert "状态=disabled" in r.stdout
    # 启用
    subprocess.run(["akm", "key", "enable", "prio"], capture_output=True, text=True)
    r = subprocess.run(["akm", "key", "list"], capture_output=True, text=True)
    assert "状态=active" in r.stdout


def test_server_startup_and_health():
    """测试服务启动和健康检查"""
    tmpdir = tempfile.mkdtemp()
    _set_home(tmpdir)
    proc = subprocess.Popen(
        ["akm", "serve", "--port", "18800"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(2)  # 等待服务启动
    try:
        resp = requests.get("http://127.0.0.1:18800/health", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        proc.terminate()
        proc.wait()


def test_proxy_request_no_keys():
    """无 key 时代理请求返回 503"""
    tmpdir = tempfile.mkdtemp()
    _set_home(tmpdir)
    proc = subprocess.Popen(
        ["akm", "serve", "--port", "18801"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(2)
    try:
        resp = requests.post(
            "http://127.0.0.1:18801/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
            timeout=5,
        )
        assert resp.status_code == 503
    finally:
        proc.terminate()
        proc.wait()


def test_log_list():
    """测试日志查看"""
    tmpdir = tempfile.mkdtemp()
    _set_home(tmpdir)
    # 无日志时
    r = subprocess.run(["akm", "log", "list"], capture_output=True, text=True)
    assert "暂无日志" in r.stdout
