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