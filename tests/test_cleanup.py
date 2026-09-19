"""本地数据目录自动维护测试：更新包缓存清理、文本日志轮转与开关默认值。

隔离方式：把 ``akm.db.DB_DIR`` 指向临时目录，``akm.cleanup.akm_home()``
与数据库路径同源，因此整个维护流程都不会触碰真实 ``~/.akm``。
"""

import os
import time

import pytest

import akm
import akm.config as cfg
import akm.db as db
import akm.cleanup as cleanup


@pytest.fixture
def akm_tmp_home(tmp_path, monkeypatch):
    """把 AKM 数据目录隔离到临时路径。"""
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    return tmp_path


def _write(path, content=b"x", mtime=None):
    """写入文件，并可选地把 mtime 设为过去某个时间点。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_app(dir_path, name, size=2048, mtime=None):
    """构造一份 .app 备份目录（含一个占位文件以便统计体积）。"""
    (dir_path / name / "Contents").mkdir(parents=True, exist_ok=True)
    binary = dir_path / name / "Contents" / "binary"
    binary.write_bytes(b"0" * size)
    if mtime is not None:
        os.utime(dir_path / name, (mtime, mtime))
    return dir_path / name


def test_cleanup_update_cache_keeps_newest_zip_and_rollback_backup(akm_tmp_home):
    """只保留最新 zip，以及「最新备份 + 当前版本备份」两个回滚点。"""
    now = time.time()
    updates = akm_tmp_home / "updates"
    _write(updates / "AI Key Manager-0.0.1-1.zip", mtime=now - 3000)
    _write(updates / "AI Key Manager-0.0.2-2.zip", mtime=now - 2000)
    newest_zip = _write(updates / "AI Key Manager-0.0.3-3.zip", mtime=now - 1000)

    backups = updates / "backups"
    stale = _write_app(backups, "AI Key Manager-0.0.1.app", mtime=now - 3000)
    newest = _write_app(backups, "AI Key Manager-0.0.2.app", mtime=now - 2000)
    current = _write_app(backups, f"AI Key Manager-{akm.__version__}.app", mtime=now - 4000)
    # 历史上被误放进 backups/ 的 zip 也要一并清掉
    stray_zip = _write(backups / "AI Key Manager-0.0.1-9.zip", mtime=now - 4000)

    result = cleanup.cleanup_update_cache()

    assert not (updates / "AI Key Manager-0.0.1-1.zip").exists()
    assert not (updates / "AI Key Manager-0.0.2-2.zip").exists()
    assert newest_zip.exists()
    assert not stray_zip.exists()
    assert not stale.exists()
    assert newest.exists()
    assert current.exists()
    assert result["freed_bytes"] > 0
    assert str(stale) in result["deleted"]
    assert str(stray_zip) in result["deleted"]


def test_cleanup_update_cache_skips_recent_files(akm_tmp_home):
    """宽限期内的文件视为正在使用，即使不是最新也不删除。"""
    now = time.time()
    updates = akm_tmp_home / "updates"
    old = _write(updates / "AI Key Manager-0.0.1-1.zip", mtime=now - 3000)
    fresh = _write(updates / "AI Key Manager-0.0.2-2.zip", mtime=now - 60)
    newest = _write(updates / "AI Key Manager-0.0.3-3.zip", mtime=now - 30)

    cleanup.cleanup_update_cache()

    assert not old.exists()
    assert fresh.exists()
    assert newest.exists()


def test_cleanup_update_cache_missing_dir_is_noop(akm_tmp_home):
    """目录不存在时安全返回，不抛异常。"""
    result = cleanup.cleanup_update_cache()
    assert result == {"deleted": [], "kept": [], "freed_bytes": 0}


def test_rotate_text_logs_keeps_one_generation(akm_tmp_home):
    """超过阈值的日志转存为 .1，且只保留一代。"""
    log = _write(akm_tmp_home / "error.log", b"a" * 4096)
    rotated = _write(akm_tmp_home / "error.log.1", b"b" * 1024)

    result = cleanup.rotate_text_logs(1024)

    assert not log.exists()
    assert (akm_tmp_home / "error.log.1").read_bytes() == b"a" * 4096
    assert rotated.exists()
    assert result["dropped_bytes"] == 1024
    assert result["freed_bytes"] == 1024
    assert str(log) in result["rotated"]


def test_rotate_text_logs_ignores_small_files_and_subdirs(akm_tmp_home):
    """小于阈值的日志、非 .log 文件与子目录内日志都不处理。"""
    small = _write(akm_tmp_home / "keys.log", b"a" * 10)
    other = _write(akm_tmp_home / "notes.txt", b"a" * 4096)
    nested = _write(akm_tmp_home / "markdown_kb" / "hook_debug.jsonl", b"a" * 4096)
    sub_log = _write(akm_tmp_home / "plugins" / "custom.log", b"a" * 4096)

    result = cleanup.rotate_text_logs(1024)

    assert small.exists()
    assert other.exists()
    assert nested.exists()
    assert sub_log.exists()
    assert result["rotated"] == []


def test_run_auto_maintenance_defaults_turn_on_update_cache_only(akm_tmp_home, monkeypatch):
    """默认配置：更新包清理执行，文本日志轮转不执行。"""
    now = time.time()
    updates = akm_tmp_home / "updates"
    _write(updates / "AI Key Manager-0.0.1-1.zip", mtime=now - 3000)
    _write(updates / "AI Key Manager-0.0.2-2.zip", mtime=now - 2000)
    big_log = _write(akm_tmp_home / "error.log", b"a" * 4096)

    monkeypatch.setattr(cfg, "get", lambda key, default=None: cfg.DEFAULTS.get(key, default))

    result = cleanup.run_auto_maintenance()

    assert result["update_cache"] is not None
    assert result["text_logs"] is None
    assert big_log.exists()


def test_run_auto_maintenance_respects_switches(akm_tmp_home, monkeypatch):
    """开关显式关闭 A、开启 B 时，只轮转日志、不动更新包。"""
    now = time.time()
    updates = akm_tmp_home / "updates"
    stale_zip = _write(updates / "AI Key Manager-0.0.1-1.zip", mtime=now - 3000)
    _write(updates / "AI Key Manager-0.0.2-2.zip", mtime=now - 2000)

    def fake_get(key, default=None):
        if key == "update_cache_cleanup":
            return False
        if key == "text_log_rotation":
            return True
        if key == "log_file_max_mb":
            return 1
        return cfg.DEFAULTS.get(key, default)

    monkeypatch.setattr(cfg, "get", fake_get)

    result = cleanup.run_auto_maintenance()

    assert result["update_cache"] is None
    assert stale_zip.exists()
    assert result["text_logs"] is not None


def test_config_defaults_and_normalization(tmp_path, monkeypatch):
    """新增配置项默认值正确，且对非法数值/非布尔值做归一化。"""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr(cfg, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(cfg_dir / "config.json"))

    merged = cfg.load_config()
    assert merged["update_cache_cleanup"] is True
    assert merged["text_log_rotation"] is False
    assert merged["log_file_max_mb"] == 5

    # 真实布尔值：A 关闭、B 开启，阈值 0 被收敛到下限 1
    cfg.save_config({"update_cache_cleanup": False, "text_log_rotation": True, "log_file_max_mb": 0})
    after = cfg.load_config()
    assert after["update_cache_cleanup"] is False
    assert after["text_log_rotation"] is True
    assert after["log_file_max_mb"] == 1

    # 非布尔值不生效：与 auto_update/agent_enabled 保持同一套严格归一化约定，
    # 只认真正的 JSON 布尔（避免字符串 "false"/"0" 被误当成开关）
    cfg.save_config({"update_cache_cleanup": "false", "text_log_rotation": "true"})
    strict = cfg.load_config()
    assert strict["update_cache_cleanup"] is True
    assert strict["text_log_rotation"] is False
