"""主密钥托管测试：权限加固、后端解析、迁移、钥匙串假实现与轮换。

约定：

- 真实 macOS 钥匙串一律不参与测试（conftest 默认禁止访问，需要时用 ``fake_keychain``）；
- 密钥目录由 conftest 的 autouse 夹具重定向到临时目录，断言失败也不会碰到生产数据。
"""

import os
import stat

import pytest

import akm.crypto as crypto
import akm.secret_store as secret_store
from akm.crypto import InvalidToken, SecretStoreError


def _isolate(tmp_path, monkeypatch, subdir="keydir"):
    """把主密钥目录指向 tmp 子目录并清缓存"""
    target = tmp_path / subdir
    monkeypatch.setattr(crypto, "SECRET_DIR", str(target))
    monkeypatch.setattr(crypto, "_cipher", None)
    return target


# ── P0：权限加固 ────────────────────────────────────────────

def test_new_key_file_is_0600_and_dir_is_0700(tmp_path, monkeypatch):
    """首次生成：密钥文件 0600、密钥目录 0700（不受 umask 影响）"""
    key_dir = _isolate(tmp_path, monkeypatch)

    crypto._load_cipher()

    path = key_dir / secret_store.SECRET_FILE_NAME
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700


def test_loose_permissions_are_healed_on_read(tmp_path, monkeypatch):
    """既有 0644 文件 / 0755 目录在读取时自愈为 0600 / 0700"""
    key_dir = tmp_path / "keydir"
    key_dir.mkdir()
    path = key_dir / secret_store.SECRET_FILE_NAME
    path.write_bytes(secret_store.generate_key())
    os.chmod(path, 0o644)
    os.chmod(key_dir, 0o755)

    monkeypatch.setattr(crypto, "SECRET_DIR", str(key_dir))
    monkeypatch.setattr(crypto, "_cipher", None)
    cipher = crypto._load_cipher()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700
    assert cipher.decrypt(cipher.encrypt(b"payload")) == b"payload"


def test_harden_failure_only_warns_and_does_not_break_loading(tmp_path, monkeypatch):
    """chmod 失败（只读挂载/FAT 等）只告警，不阻断密钥加载"""
    key_dir = tmp_path / "keydir"
    key_dir.mkdir()
    path = key_dir / secret_store.SECRET_FILE_NAME
    key = secret_store.generate_key()
    path.write_bytes(key)
    os.chmod(path, 0o644)

    real_chmod = os.chmod

    def _boom(target, mode, *args, **kwargs):
        if os.path.realpath(target) == os.path.realpath(str(path)):
            raise OSError("read-only file system")
        return real_chmod(target, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", _boom)
    assert secret_store.harden_file(str(path)) is False

    monkeypatch.setattr(crypto, "SECRET_DIR", str(key_dir))
    monkeypatch.setattr(crypto, "_cipher", None)
    cipher = crypto._load_cipher()
    assert cipher.decrypt(cipher.encrypt(b"ok")) == b"ok"


def test_corrupt_key_file_is_never_silently_replaced(tmp_path, monkeypatch):
    """密钥文件损坏时必须报错，绝不生成新密钥覆盖（否则已存数据永久不可解）"""
    key_dir = _isolate(tmp_path, monkeypatch)
    key_dir.mkdir()
    path = key_dir / secret_store.SECRET_FILE_NAME
    path.write_bytes(b"definitely-not-a-32-byte-key")

    with pytest.raises(SecretStoreError):
        crypto._load_cipher()

    assert path.read_bytes() == b"definitely-not-a-32-byte-key"


def test_empty_key_file_is_rejected(tmp_path):
    """空密钥文件同样视为异常，不能当作"没有密钥"去生成新的"""
    path = tmp_path / secret_store.SECRET_FILE_NAME
    path.write_bytes(b"   \n")
    with pytest.raises(SecretStoreError):
        secret_store.read_file_keys(str(path), None)


# ── P1：后端解析、env 覆盖与遗留迁移 ────────────────────────

def test_inline_env_key_is_used_and_not_written_to_disk(tmp_path, monkeypatch):
    """AKM_SECRET_KEY 内联密钥：直接用，不落盘"""
    key = secret_store.generate_key()
    monkeypatch.setenv("AKM_SECRET_KEY", key.decode())
    key_dir = _isolate(tmp_path, monkeypatch)

    cipher = crypto._load_cipher()

    assert not key_dir.exists()
    assert cipher.decrypt(cipher.encrypt(b"inline")) == b"inline"
    assert crypto._last_source == "env:AKM_SECRET_KEY"


def test_env_file_override_wins_over_default_dir(tmp_path, monkeypatch):
    """AKM_SECRET_FILE 指向显式文件时优先于 SECRET_DIR"""
    explicit = tmp_path / "elsewhere" / "my.key"
    explicit.parent.mkdir()
    key = secret_store.generate_key()
    explicit.write_bytes(key)
    monkeypatch.setenv("AKM_SECRET_FILE", str(explicit))
    _isolate(tmp_path, monkeypatch)

    crypto._load_cipher()

    assert crypto._get_secret_path() == str(explicit)
    assert not (tmp_path / "keydir" / secret_store.SECRET_FILE_NAME).exists()


def test_legacy_key_is_migrated_to_new_path_and_legacy_is_kept(tmp_path, monkeypatch):
    """遗留路径存在密钥时迁移到主路径：复制语义，遗留文件必须保留"""
    legacy_dir = tmp_path / "data"
    legacy_dir.mkdir()
    legacy_path = legacy_dir / secret_store.SECRET_FILE_NAME
    legacy_key = secret_store.generate_key()
    legacy_path.write_bytes(legacy_key)

    key_dir = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(crypto, "_get_legacy_secret_path", lambda: str(legacy_path))

    cipher = crypto._load_cipher()

    assert (key_dir / secret_store.SECRET_FILE_NAME).read_bytes().strip() == legacy_key
    assert legacy_path.exists(), "迁移必须保留遗留文件，保证可回滚"
    assert crypto._last_source.startswith("legacy(")
    assert any("已从遗留路径迁移" in note for note in crypto._last_notes)
    assert cipher.decrypt(cipher.encrypt(b"migrated")) == b"migrated"


def test_legacy_migration_failure_falls_back_to_legacy_key(tmp_path, monkeypatch):
    """主路径不可写时继续使用遗留密钥，不阻断启动"""
    legacy_dir = tmp_path / "data"
    legacy_dir.mkdir()
    legacy_path = legacy_dir / secret_store.SECRET_FILE_NAME
    legacy_key = secret_store.generate_key()
    legacy_path.write_bytes(legacy_key)

    monkeypatch.setattr(crypto, "SECRET_DIR", str(tmp_path / "keydir"))
    monkeypatch.setattr(crypto, "_cipher", None)
    monkeypatch.setattr(crypto, "_get_legacy_secret_path", lambda: str(legacy_path))

    def _boom(path, key):
        raise SecretStoreError("disk full")

    monkeypatch.setattr(secret_store, "_write_secret_file", _boom)

    cipher = crypto._load_cipher()

    assert cipher.decrypt(cipher.encrypt(b"still-works")) == b"still-works"
    assert any("迁移失败" in note for note in crypto._last_notes)


def test_effective_backend_prefers_env_then_config(monkeypatch):
    """后端解析：AKM_SECRET_BACKEND > config.secret_backend > file"""
    import akm.config as config_module

    monkeypatch.setenv("AKM_SECRET_BACKEND", "keychain")
    assert secret_store.effective_backend() == "keychain"

    monkeypatch.setenv("AKM_SECRET_BACKEND", "")
    monkeypatch.setattr(config_module, "load_config", lambda: {"secret_backend": "keychain"})
    assert secret_store.effective_backend() == "keychain"

    monkeypatch.setattr(config_module, "load_config", lambda: {"secret_backend": "nonsense"})
    assert secret_store.effective_backend() == "file"

    def _boom():
        raise RuntimeError("config broken")

    monkeypatch.setattr(config_module, "load_config", _boom)
    assert secret_store.effective_backend() == "file"


# ── P2：钥匙串后端（假实现，不碰真实钥匙串）────────────────

def test_keychain_write_read_roundtrip_including_previous(fake_keychain):
    """钥匙串读写：当前密钥 + 历史密钥都能读回"""
    current = secret_store.generate_key()
    previous = secret_store.generate_key()

    secret_store.keychain_write(current, [previous])
    material = secret_store.keychain_read()

    assert material is not None
    assert material.current == current
    assert material.previous == [previous]


def test_keychain_read_returns_none_when_empty(fake_keychain):
    assert secret_store.keychain_read() is None


def test_keychain_write_prunes_stale_previous_entries(fake_keychain):
    """轮换两次后只剩最近一次的历史密钥，旧条目被清理"""
    first_previous = secret_store.generate_key()
    secret_store.keychain_write(secret_store.generate_key(), [first_previous])
    second_previous = secret_store.generate_key()
    secret_store.keychain_write(secret_store.generate_key(), [second_previous])

    material = secret_store.keychain_read()
    assert material.previous == [second_previous]
    previous_accounts = [a for a in fake_keychain.items
                         if a.startswith(secret_store.KEYCHAIN_PREVIOUS_PREFIX)]
    assert len(previous_accounts) == secret_store.MAX_PREVIOUS


def test_keychain_delete_removes_current_and_previous(fake_keychain):
    secret_store.keychain_write(secret_store.generate_key(), [secret_store.generate_key()])
    removed = secret_store.keychain_delete()
    assert secret_store.KEYCHAIN_ACCOUNT in removed
    assert secret_store.keychain_read() is None


def test_migrate_to_keychain_keeps_file_then_purge_removes_it(tmp_path, monkeypatch, fake_keychain):
    """迁移到钥匙串是复制语义；purge 需要钥匙串回读校验通过"""
    monkeypatch.setenv("AKM_SECRET_BACKEND", "keychain")
    key_dir = _isolate(tmp_path, monkeypatch)
    key_dir.mkdir()
    path = key_dir / secret_store.SECRET_FILE_NAME
    key = secret_store.generate_key()
    path.write_bytes(key)

    result = crypto.migrate_backend("keychain")

    assert fake_keychain.items[secret_store.KEYCHAIN_ACCOUNT] == key
    assert path.exists(), "默认必须保留明文文件"
    assert any("明文文件仍保留" in action for action in result["actions"])

    purged = crypto.purge_file_keys()
    assert str(path) in purged["removed"]
    assert not path.exists()
    assert purged["verified_by"] == "keychain"


def test_purge_refuses_when_keychain_has_no_matching_key(tmp_path, monkeypatch, fake_keychain):
    """钥匙串里没有同一把密钥时拒绝清理明文文件"""
    key_dir = _isolate(tmp_path, monkeypatch)
    key_dir.mkdir()
    path = key_dir / secret_store.SECRET_FILE_NAME
    path.write_bytes(secret_store.generate_key())

    with pytest.raises(SecretStoreError):
        crypto.purge_file_keys()

    assert path.exists()


def test_purge_refuses_when_file_key_differs_from_keychain(tmp_path, monkeypatch, fake_keychain):
    """钥匙串与明文文件里是两把不同密钥时拒绝清理（否则可能删掉真正在用的那一把）。

    这是真机验证时发现的真实缺陷场景：钥匙串里有残留/另一台机器的条目时，
    如果只校验"当前内存密钥在钥匙串里"就删文件，会把另一把密钥删掉。
    """
    monkeypatch.setenv("AKM_SECRET_BACKEND", "keychain")
    key_dir = _isolate(tmp_path, monkeypatch)
    key_dir.mkdir()
    path = key_dir / secret_store.SECRET_FILE_NAME
    path.write_bytes(secret_store.generate_key())
    fake_keychain.items[secret_store.KEYCHAIN_ACCOUNT] = secret_store.generate_key()

    with pytest.raises(SecretStoreError) as excinfo:
        crypto.purge_file_keys()

    assert "不一致" in str(excinfo.value)
    assert path.exists(), "内容对不上时必须保留文件"

    # 加载阶段同样要显式提示不一致，而不是静默以钥匙串为准
    crypto.reset_cipher_cache()
    crypto._load_cipher()
    assert any("不一致" in note for note in crypto._last_notes)


def test_migrate_to_keychain_with_purge_refuses_on_mismatch(tmp_path, monkeypatch, fake_keychain):
    """migrate --purge-file 走同一套校验：不一致时不能删文件"""
    monkeypatch.setenv("AKM_SECRET_BACKEND", "keychain")
    key_dir = _isolate(tmp_path, monkeypatch)
    key_dir.mkdir()
    path = key_dir / secret_store.SECRET_FILE_NAME
    path.write_bytes(secret_store.generate_key())
    fake_keychain.items[secret_store.KEYCHAIN_ACCOUNT] = secret_store.generate_key()

    with pytest.raises(SecretStoreError) as excinfo:
        crypto.migrate_backend("keychain", purge_file=True)

    assert "不一致" in str(excinfo.value)
    assert path.exists()


def test_migrate_to_keychain_with_purge_removes_matching_file(tmp_path, monkeypatch, fake_keychain):
    """密钥一致时 migrate --purge-file 正常删除明文文件"""
    monkeypatch.setenv("AKM_SECRET_BACKEND", "keychain")
    key_dir = _isolate(tmp_path, monkeypatch)
    key_dir.mkdir()
    path = key_dir / secret_store.SECRET_FILE_NAME
    key = secret_store.generate_key()
    path.write_bytes(key)

    result = crypto.migrate_backend("keychain", purge_file=True)

    assert fake_keychain.items[secret_store.KEYCHAIN_ACCOUNT] == key
    assert not path.exists()
    assert any("已删除明文密钥文件" in action for action in result["actions"])


def test_keychain_backend_reads_from_keychain_without_file(tmp_path, monkeypatch, fake_keychain):
    """backend=keychain 且钥匙串有条目时，密钥完全来自钥匙串"""
    key = secret_store.generate_key()
    fake_keychain.items[secret_store.KEYCHAIN_ACCOUNT] = key
    monkeypatch.setenv("AKM_SECRET_BACKEND", "keychain")
    key_dir = _isolate(tmp_path, monkeypatch)

    cipher = crypto._load_cipher()

    assert not key_dir.exists(), "钥匙串已提供密钥时不应再落盘明文"
    assert cipher.decrypt(cipher.encrypt(b"from-keychain")) == b"from-keychain"
    assert crypto._last_source.startswith("keychain")


def test_keychain_read_failure_falls_back_to_file(tmp_path, monkeypatch):
    """钥匙串读取异常时回退文件后端，不影响可用性"""
    monkeypatch.setenv("AKM_SECRET_BACKEND", "keychain")
    key_dir = _isolate(tmp_path, monkeypatch)
    key_dir.mkdir()
    (key_dir / secret_store.SECRET_FILE_NAME).write_bytes(secret_store.generate_key())

    def _broken():
        raise secret_store.KeychainError("系统拒绝访问")

    monkeypatch.setattr(secret_store, "_get_bridge", _broken)
    monkeypatch.setattr(secret_store, "keychain_supported", lambda: True)

    cipher = crypto._load_cipher()

    assert cipher.decrypt(cipher.encrypt(b"fallback")) == b"fallback"
    assert any("回退文件后端" in note for note in crypto._last_notes)


# ── P3：轮换 ────────────────────────────────────────────────

def test_rotate_keeps_previous_key_for_existing_ciphertext(tmp_path, monkeypatch):
    """轮换后：新密文用新密钥，旧密文仍可由历史密钥解开"""
    key_dir = _isolate(tmp_path, monkeypatch)
    old_cipher = crypto._load_cipher()
    old_blob = old_cipher.encrypt(b"before-rotate")
    old_key = (key_dir / secret_store.SECRET_FILE_NAME).read_bytes().strip()

    info = crypto.rotate()

    assert info["previous_kept"] is True
    new_key = (key_dir / secret_store.SECRET_FILE_NAME).read_bytes().strip()
    assert new_key != old_key
    assert len(secret_store.previous_files(crypto._get_secret_path())) == 1

    new_cipher = crypto._load_cipher()
    assert new_cipher.key_count == 2
    assert new_cipher.decrypt(old_blob) == b"before-rotate"
    new_blob = new_cipher.encrypt(b"after-rotate")
    assert new_cipher.decrypt(new_blob) == b"after-rotate"
    with pytest.raises(InvalidToken):
        old_cipher.decrypt(new_blob)   # 新密文必须用新密钥加密，旧密钥解不开


def test_rotate_without_previous_makes_old_ciphertext_unreadable(tmp_path, monkeypatch):
    """--drop-previous：旧密文不再可解（这是显式选择，不是默认行为）"""
    _isolate(tmp_path, monkeypatch)
    old_blob = crypto._load_cipher().encrypt(b"lost")

    crypto.rotate(keep_previous=False)

    with pytest.raises(InvalidToken):
        crypto._load_cipher().decrypt(old_blob)
    assert secret_store.previous_files(crypto._get_secret_path()) == []


def test_rotate_in_keychain_mode_keeps_both_copies_in_sync(tmp_path, monkeypatch, fake_keychain):
    """钥匙串模式下轮换：钥匙串与仍存在的明文文件保持同一把当前密钥"""
    monkeypatch.setenv("AKM_SECRET_BACKEND", "keychain")
    key_dir = _isolate(tmp_path, monkeypatch)
    key_dir.mkdir()
    path = key_dir / secret_store.SECRET_FILE_NAME
    path.write_bytes(secret_store.generate_key())
    fake_keychain.items[secret_store.KEYCHAIN_ACCOUNT] = path.read_bytes().strip()

    crypto.rotate()

    assert fake_keychain.items[secret_store.KEYCHAIN_ACCOUNT] == path.read_bytes().strip()
    assert crypto._load_cipher().key_count == 2


def test_apply_keys_refuses_when_no_target_is_writable(tmp_path, monkeypatch):
    """钥匙串与文件都写不进去时必须报错，而不是假装成功"""
    monkeypatch.setenv("AKM_SECRET_BACKEND", "keychain")
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "keychain_supported", lambda: False)

    def _boom(path, current, previous):
        raise SecretStoreError("read-only")

    monkeypatch.setattr(secret_store, "write_file_keys", _boom)

    with pytest.raises(SecretStoreError):
        crypto._apply_keys(secret_store.generate_key(), [])


def test_custody_status_reports_paths_and_permissions(tmp_path, monkeypatch):
    """status 输出包含后端、文件路径与权限，供 CLI 与排障使用"""
    key_dir = _isolate(tmp_path, monkeypatch)
    crypto._load_cipher()

    status = crypto.custody_status()

    assert status["configured_backend"] == "file"
    assert status["files"]["primary"]["path"] == str(key_dir / secret_store.SECRET_FILE_NAME)
    assert status["files"]["primary"]["mode"] == "0o600"
    assert status["files"]["primary_dir_mode"] == "0o700"
    assert status["loaded_keys"] == 1
