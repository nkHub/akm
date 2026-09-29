"""pytest 全局夹具：测试数据库/密钥隔离。

在收集测试模块之前把 AKM_DB_DIR 指向本会话专属的临时目录：
- 避免测试往生产库 ~/.akm/akm.db 写入数据（审计日志 / key 变更等）；
- 避免测试读写/生成真实 ~/.akm/secret.key（密钥改为本地文件方案后同理隔离）。
"""

import os
import tempfile

import pytest

# 必须在任何 akm.* 模块被 import 之前设置，保证 db.DB_DIR 在模块加载时
# 就读取到隔离路径（db.DB_DIR 是模块级常量，导入时求值）。
_TEST_DB_DIR = tempfile.mkdtemp(prefix="akm-test-db-")
os.environ["AKM_DB_DIR"] = _TEST_DB_DIR


def pytest_sessionstart(session):
    """会话开始前为隔离库创建表结构，保证测试开箱可查。"""
    from akm.db import get_connection, init_db

    conn = get_connection()
    init_db(conn)
    conn.close()


def pytest_sessionfinish(session, exitstatus):
    """会话结束后清理隔离目录（含 akm.db 及其 -wal/-shm）。"""
    import shutil

    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def akm_isolate(monkeypatch, tmp_path):
    """自动隔离：每条测试用例使用临时密钥目录，避免读写真实 ~/.akm/secret.key。"""
    import akm.crypto as crypto

    # 密钥文件路径重定向到每用例临时目录，避免读写 ~/.akm/secret.key
    monkeypatch.setattr(crypto, "SECRET_DIR", str(tmp_path))
    # 清除进程级加密器缓存，保证每例用隔离后的路径重新加载
    monkeypatch.setattr(crypto, "_cipher", None)
    monkeypatch.setattr(crypto, "_last_source", "")
    monkeypatch.setattr(crypto, "_last_notes", [])
    # 强制文件后端 + 清掉可能存在的环境覆盖，避免测试读到开发机上的真实密钥文件或 macOS 钥匙串条目
    monkeypatch.setenv("AKM_SECRET_BACKEND", "file")
    monkeypatch.delenv("AKM_SECRET_KEY", raising=False)
    monkeypatch.delenv("AKM_SECRET_FILE", raising=False)
    # 告警去重集合逐例重置，保证告警断言可重复
    import akm.secret_store as secret_store

    monkeypatch.setattr(secret_store, "_warned", set())

    # 安全护栏：默认禁止测试触碰真实 macOS 钥匙串（避免弹系统授权框 / 读到真实密钥）。
    # 需要钥匙串行为的用例请使用 fake_keychain 夹具覆盖。
    def _forbid_real_keychain():
        raise RuntimeError("测试禁止访问真实钥匙串，请使用 fake_keychain 夹具")

    monkeypatch.setattr(secret_store, "_get_bridge", _forbid_real_keychain)


@pytest.fixture
def fake_keychain(monkeypatch):
    """把钥匙串后端替换为内存实现，返回该假 bridge（``.items`` 为 account→value）。"""
    import akm.secret_store as secret_store

    class _FakeBridge:
        def __init__(self):
            self.items: dict[str, bytes] = {}

        def read(self, account):
            return self.items.get(account)

        def write(self, account, value):
            self.items[account] = value

        def delete(self, account):
            return self.items.pop(account, None) is not None

        def accounts(self):
            return list(self.items)

    bridge = _FakeBridge()
    monkeypatch.setattr(secret_store, "_get_bridge", lambda: bridge)
    monkeypatch.setattr(secret_store, "keychain_supported", lambda: True)
    return bridge