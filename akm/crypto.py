"""API Key 加密存储层：Fernet 兼容的对称加密 + 主密钥托管编排。

本模块分两部分：

1. **加解密**（``_MiniFernet`` / ``_KeyRing``）：令牌格式与 ``cryptography.fernet`` 完全互通，
   AES-128-CBC + PKCS7 填充 + Encrypt-then-MAC（HMAC-SHA256）。这部分的实现与格式**保持稳定**，
   不做兼容性改动。
2. **主密钥托管**（``_read_or_create_keys`` / ``rotate`` / ``migrate_backend`` / ``custody_status``）：
   决定密钥"从哪读、写到哪、权限对不对、怎么轮换"，具体后端原语在 ``akm.secret_store``。

主密钥读取顺序：

1. ``AKM_SECRET_KEY``：内联密钥（CI/测试用，只读，不落盘）；
2. ``secret_backend=keychain``（macOS 钥匙串，见 ``akm.secret_store``）：读钥匙串，
   没有条目则从文件读取并迁移进钥匙串（明文文件保留，需显式 ``akm secret purge-file`` 清理）；
3. 文件后端：``AKM_SECRET_FILE`` 或 ``<SECRET_DIR>/secret.key``（含历史密钥
   ``secret.key.previous.*``）；
4. 遗留路径：数据目录（默认 ``~/.akm``）下的 ``secret.key``——存在时**复制**到主路径完成迁移，
   **保留**原文件，保证可回滚；
5. 都不存在：生成新的 32 字节随机主密钥并写盘（0600）；写盘失败则报错，绝不"每次启动换一把"。

权限：密钥文件 0600、密钥目录 0700，创建与读取时都会加固（读取自愈，失败只告警不阻断）。
详见 ``docs/design/key-custody.md``。
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import sys
import time

import tinyaes

from akm import secret_store
from akm.secret_store import SecretStoreError

logger = logging.getLogger(__name__)

# ── 路径与后端 ──────────────────────────────────────────────


def _platform_default_secret_dir() -> str:
    """平台默认的主密钥目录（macOS 用 Application Support，避免与数据目录混放）"""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/AKM")
    return os.path.expanduser("~/.config/akm")


DEFAULT_SECRET_DIR = _platform_default_secret_dir()
"""平台默认主密钥目录（常量，不随环境变量变化）"""

SECRET_DIR = os.environ.get("AKM_SECRET_DIR") or DEFAULT_SECRET_DIR
"""当前主密钥目录；可被 ``AKM_SECRET_DIR`` 覆盖，测试可直接 monkeypatch"""

_cipher = None  # _KeyRing | None：进程级缓存
_last_source = ""   # 最近一次密钥来源（供 akm secret status 展示）
_last_notes: list[str] = []   # 最近一次加载发生的迁移/回退动作


def _get_secret_path() -> str:
    """主密钥文件路径（``AKM_SECRET_FILE`` 可显式指定单个文件）"""
    explicit = (os.environ.get("AKM_SECRET_FILE") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.join(SECRET_DIR, secret_store.SECRET_FILE_NAME)


def _get_legacy_secret_path() -> str:
    """遗留主密钥路径：AKM 数据目录下的 ``secret.key``（老版本存放位置）"""
    try:
        from akm.db import DB_DIR

        home = os.path.expanduser(str(DB_DIR))
    except Exception:  # noqa: BLE001
        home = os.path.expanduser("~/.akm")
    return os.path.join(home, secret_store.SECRET_FILE_NAME)


class InvalidToken(Exception):
    """Fernet 令牌校验失败（与 cryptography.fernet.InvalidToken 行为对齐）"""


# ── 加解密 ──────────────────────────────────────────────────

def _pkcs7_pad(data: bytes) -> bytes:
    """PKCS7 填充到 16 字节块边界"""
    pad_len = 16 - (len(data) % 16)
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes) -> bytes:
    """去除 PKCS7 填充；填充非法时抛 InvalidToken"""
    if not data:
        raise InvalidToken
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16 or data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise InvalidToken
    return data[:-pad_len]


class _MiniFernet:
    """Fernet 兼容加解密器（基于 tinyaes 的 AES-128-CBC，替代 cryptography.fernet.Fernet）

    令牌格式与 cryptography.fernet 完全互通：
        urlsafe_base64( 0x80 || 8字节时间戳 || 16字节IV || AES-128-CBC 密文 || 32字节HMAC-SHA256 )

    要点：
    - cryptography.fernet 的 MAC 是完整 SHA-256 digest（32 字节），不是旧规范中的 16 字节截断，
      所以这里签名也取完整 32 字节，保证与存量加密数据无缝互通。
    - 写出的令牌 cryptography.fernet 可直接解密（升级回退场景）；
      现存主密钥加密的存量数据也可无缝读取。
    - tinyaes 只提供无填充的 CBC 块加密，PKCS7 填充与 HMAC 认证在此完成。
    """

    def __init__(self, key):
        if isinstance(key, str):
            key = key.encode("utf-8")
        raw = base64.urlsafe_b64decode(key + b"=" * (-len(key) % 4))
        if len(raw) != 32:
            raise ValueError("Fernet 密钥必须是 32 字节")
        self._raw = raw
        self._signing_key = raw[:16]
        self._encryption_key = raw[16:]

    @classmethod
    def generate_key(cls) -> bytes:
        """生成与 Fernet.generate_key() 等价的 urlsafe base64 密钥"""
        return secret_store.generate_key()

    def encrypt(self, data: bytes) -> bytes:
        """加密并返回 urlsafe base64 令牌（bytes），格式与 Fernet 相同"""
        iv = secrets.token_bytes(16)
        ts = int(time.time()).to_bytes(8, "big")
        padded = _pkcs7_pad(data)
        # tinyaes 的 CBC 是原位操作：直接修改传入的 bytearray 并返回 None，完成后读回即可
        buf = bytearray(padded)
        tinyaes.AES(self._encryption_key, iv).CBC_encrypt_buffer_inplace_raw(buf)
        ct = bytes(buf)
        mac = hmac.new(self._signing_key, b"\x80" + ts + iv + ct, hashlib.sha256).digest()[:32]
        return base64.urlsafe_b64encode(b"\x80" + ts + iv + ct + mac)

    def decrypt(self, token: bytes) -> bytes:
        """解密 Fernet 令牌；令牌非法（含被篡改）抛 InvalidToken"""
        try:
            raw = base64.urlsafe_b64decode(token + b"=" * (-len(token) % 4))
        except Exception:
            raise InvalidToken
        if len(raw) < 73 or raw[0] != 0x80:
            raise InvalidToken
        iv, ct, mac = raw[9:25], raw[25:-32], raw[-32:]
        calc = hmac.new(self._signing_key, raw[:-32], hashlib.sha256).digest()[:32]
        if not hmac.compare_digest(calc, mac):
            raise InvalidToken
        # 密文长度必为 16 的倍数（PKCS7 填充保证），可直接原位解密
        buf = bytearray(ct)
        tinyaes.AES(self._encryption_key, iv).CBC_decrypt_buffer_inplace_raw(buf)
        plain = bytes(buf)
        return _pkcs7_unpad(plain)


class _KeyRing:
    """主密钥环：当前密钥负责加密，当前与历史密钥都可解密。

    历史密钥来自主密钥轮换（``akm secret rotate``），只用于解开轮换前的存量密文。
    """

    def __init__(self, keys: list[bytes]):
        if not keys:
            raise ValueError("密钥环至少需要一个主密钥")
        self._ciphers = [_MiniFernet(key) for key in keys]

    @property
    def key_count(self) -> int:
        return len(self._ciphers)

    def encrypt(self, data: bytes) -> bytes:
        """始终用当前（最新）主密钥加密"""
        return self._ciphers[0].encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        """依次尝试当前与历史主密钥；全部失败抛 InvalidToken"""
        for cipher in self._ciphers:
            try:
                return cipher.decrypt(token)
            except InvalidToken:
                continue
        raise InvalidToken


# ── 主密钥加载与托管 ────────────────────────────────────────

def _try_keychain_fallback(notes: list[str]) -> "secret_store.KeyMaterial | None":
    """文件后端下没有明文密钥文件时，尝试用钥匙串里的同一把密钥兜底。

    生成新主密钥是本模块**唯一不可逆**的动作（会让已存 ``api_key`` 永久无法解密），
    因此在生成之前先找一遍钥匙串：用户手工删掉/误清文件、迁移到钥匙串后又把配置改回
    ``file`` 时，仍能照常启动而不是悄悄换一把新密钥。
    """
    if not secret_store.keychain_supported():
        return None
    try:
        material = secret_store.keychain_read()
    except Exception as exc:  # noqa: BLE001 — 兜底查询失败不应影响正常流程
        logger.warning("[crypto] 明文密钥文件缺失，读取钥匙串兜底失败: %s", exc)
        notes.append(f"未找到明文密钥文件，钥匙串兜底读取失败：{exc}")
        return None
    if material and material.keys:
        logger.warning("[crypto] 明文密钥文件缺失，已改用钥匙串中的主密钥")
        notes.append("未找到明文密钥文件，已改用钥匙串中的同一把主密钥（未重新落盘）")
        return material
    return None


def _read_or_create_keys() -> tuple[list[bytes], str, list[str]]:
    """按既定顺序读取主密钥材料，必要时迁移或生成。返回 ``(密钥列表, 来源, 动作说明)``。"""
    inline = (os.environ.get("AKM_SECRET_KEY") or "").strip()
    if inline:
        return [secret_store.validate_key(inline)], "env:AKM_SECRET_KEY", []

    primary = _get_secret_path()
    legacy = _get_legacy_secret_path()
    if os.path.realpath(primary) == os.path.realpath(legacy):
        legacy = None
    backend = secret_store.effective_backend()
    notes: list[str] = []

    # 1) 钥匙串后端：优先读钥匙串，缺条目时从文件迁移进去
    if backend == secret_store.BACKEND_KEYCHAIN and secret_store.keychain_supported():
        try:
            material = secret_store.keychain_read()
        except SecretStoreError as exc:
            logger.warning("[crypto] 读取钥匙串失败，回退文件后端: %s", exc)
            notes.append(f"钥匙串读取失败，已回退文件后端：{exc}")
        else:
            if material:
                notes.extend(_file_inconsistency_notes(material.keys))
                return material.keys, material.source, notes
            file_material = secret_store.read_file_keys(primary, legacy)
            if file_material:
                notes.extend(file_material.notes)
                try:
                    secret_store.keychain_write(file_material.current, file_material.previous)
                except SecretStoreError as exc:
                    logger.warning("[crypto] 迁移主密钥到钥匙串失败: %s", exc)
                    notes.append(f"迁移到钥匙串失败，继续使用文件：{exc}")
                else:
                    notes.append(
                        "已把主密钥迁移到钥匙串；明文文件仍保留，"
                        "确认无误后可执行 `akm secret purge-file` 清理"
                    )
                return file_material.keys, "keychain(迁移)", notes

    # 2) 文件后端
    material = secret_store.read_file_keys(primary, legacy)
    if material:
        return material.keys, material.source, notes + material.notes

    # 2.5) 安全网：明文文件缺失时先找钥匙串，避免"文件被误删 → 生成新密钥 → 数据永久不可解"
    fallback = _try_keychain_fallback(notes)
    if fallback is not None:
        return fallback.keys, fallback.source, notes

    # 3) 生成新密钥（只在确认"哪里都没有密钥"之后）
    new_key = secret_store.generate_key()
    if backend == secret_store.BACKEND_KEYCHAIN and secret_store.keychain_supported():
        try:
            secret_store.keychain_write(new_key, [])
        except SecretStoreError as exc:
            logger.warning("[crypto] 写入钥匙串失败，改为写入文件: %s", exc)
            notes.append(f"写入钥匙串失败，已改为写入文件：{exc}")
        else:
            notes.append(f"已在钥匙串生成新的主密钥（{secret_store.KEYCHAIN_SERVICE}）")
            return [new_key], "generated(keychain)", notes
    try:
        written = secret_store.write_file_keys(primary, new_key, [])
    except SecretStoreError as exc:
        # 关键安全语义：无法持久化时直接报错，避免每次启动都换一把密钥
        raise SecretStoreError(f"无法保存新生成的主密钥，已中止：{exc}") from exc
    notes.append(f"已生成新的主密钥：{written[0]}")
    return [new_key], f"generated({written[0]})", notes


def _load_cipher() -> "_KeyRing":
    """加载主密钥环（进程级缓存，命中后不再读盘）"""
    global _cipher
    if _cipher is not None:
        return _cipher

    keys, source, notes = _read_or_create_keys()
    for note in notes:
        logger.info("[crypto] %s", note)
    _remember(source, notes)
    _cipher = _KeyRing(keys)
    return _cipher


def reset_cipher_cache() -> None:
    """清空进程级密钥环缓存（轮换/迁移后调用）"""
    global _cipher
    _cipher = None


def _remember(source: str, notes: list[str]) -> None:
    """记录最近一次密钥来源与动作说明（供 ``akm secret status`` 展示）"""
    global _last_source, _last_notes
    _last_source, _last_notes = source, list(notes)


def _encrypt(plain: str) -> str:
    """加密明文，返回 base64 编码的密文字符串"""
    return _load_cipher().encrypt(plain.encode()).decode()


def _decrypt(cipher_text: str) -> str:
    """解密 base64 编码的密文，返回明文"""
    # api_key 为空（用户尚未填写）时直接返回空串，避免对空令牌执行解密
    if not cipher_text:
        return ""
    return _load_cipher().decrypt(cipher_text.encode()).decode()


# ── 轮换 / 迁移 / 状态 ──────────────────────────────────────

def _file_copies_exist() -> bool:
    """主路径下是否已有密钥文件（含历史密钥文件）"""
    path = _get_secret_path()
    return os.path.exists(path) or bool(secret_store.previous_files(path))


def _apply_keys(current: bytes, previous: list[bytes]) -> list[str]:
    """把当前/历史密钥写入生效的存放位置，返回落地目标列表。

    - 钥匙串后端且可用：写钥匙串；
    - 文件后端，或钥匙串场景下仍保留着明文文件：同步写文件（保持两者一致）；
    - 两者都写不了：抛错，不做"看起来成功了"的假动作。
    """
    targets: list[str] = []
    backend = secret_store.effective_backend()
    keychain_ok = False
    if backend == secret_store.BACKEND_KEYCHAIN and secret_store.keychain_supported():
        try:
            secret_store.keychain_write(current, previous)
        except SecretStoreError as exc:
            logger.warning("[crypto] 写入钥匙串失败: %s", exc)
        else:
            keychain_ok = True
            targets.append(f"keychain:{secret_store.KEYCHAIN_SERVICE}")

    if backend == secret_store.BACKEND_FILE or _file_copies_exist() or not keychain_ok:
        written = secret_store.write_file_keys(_get_secret_path(), current, previous)
        targets.append(written[0])

    if not targets:
        raise SecretStoreError("没有可写入的主密钥存放位置（钥匙串与文件后端均失败）")
    reset_cipher_cache()
    return targets


def rotate(keep_previous: bool = True) -> dict:
    """轮换主密钥：生成新的当前密钥，旧密钥降级为历史密钥（仅用于解密）。

    只负责换密钥与落盘；数据库里的存量密文由 ``akm.key_pool.reencrypt_all_keys`` 重新加密。
    """
    keys, source, _ = _read_or_create_keys()
    old_current = keys[0]
    new_key = secret_store.generate_key()
    previous = [old_current] if keep_previous else []
    targets = _apply_keys(new_key, previous)
    _remember(f"rotated({', '.join(targets)})", [f"已轮换主密钥并写入 {target}" for target in targets])
    return {
        "previous_source": source,
        "targets": targets,
        "previous_kept": bool(previous),
    }


def migrate_backend(target: str, purge_file: bool = False) -> dict:
    """把主密钥迁移到指定后端（``file`` / ``keychain``）。

    - 迁移是**复制语义**：默认保留原有明文文件，只有 ``purge_file=True`` 且校验通过才删除；
    - 目标为钥匙串时先写入，再按需清理文件；校验方式是读回钥匙串并与内存中的当前密钥比对。
    """
    target = (target or "").strip().lower()
    if target not in secret_store.SUPPORTED_BACKENDS:
        raise SecretStoreError(f"不支持的密钥后端: {target}")
    if target == secret_store.BACKEND_KEYCHAIN and not secret_store.keychain_supported():
        raise SecretStoreError("当前平台不支持 macOS 钥匙串后端")

    keys, source, _ = _read_or_create_keys()
    current, previous = keys[0], keys[1:]
    actions: list[str] = []

    if target == secret_store.BACKEND_KEYCHAIN:
        secret_store.keychain_write(current, previous)
        actions.append(f"已写入钥匙串（{secret_store.KEYCHAIN_SERVICE}/{secret_store.KEYCHAIN_ACCOUNT}）")
        read_back = secret_store.keychain_read()
        if not read_back or read_back.current != current:
            raise SecretStoreError("钥匙串回读校验失败，已保留文件，不做清理")
        actions.append("钥匙串回读校验通过")
        if purge_file:
            removed = _purge_verified_file_keys(keys)
            actions.append(
                f"已删除明文密钥文件 {len(removed)} 个" if removed else "没有需要删除的明文密钥文件"
            )
        else:
            actions.append("明文文件仍保留（如需清理请加 --purge-file）")
    else:
        targets = _apply_keys(current, previous)
        actions.append(f"已写入文件后端：{targets[-1]}")

    reset_cipher_cache()
    _remember(f"migrated({target})", actions)
    return {"source": source, "target": target, "actions": actions}


def _purge_verified_file_keys(verified_keys: list[bytes]) -> list[str]:
    """在确认磁盘上每个密钥文件都属于当前密钥环之后，才删除它们。

    安全语义：**内容对不上就不删**。否则可能出现"钥匙串里是 A、文件里是 B"时
    把 B 删掉，而 B 才是解密现有数据所用密钥的情况。
    """
    entries = secret_store.file_key_entries(_get_secret_path())
    mismatched = [path for path, key in entries if key is None or key not in verified_keys]
    if mismatched:
        raise SecretStoreError(
            "拒绝清理：以下明文密钥文件的内容与当前主密钥环不一致，"
            "删除会导致对应数据永久无法解密：" + "、".join(mismatched)
        )
    return secret_store.delete_file_keys(_get_secret_path())


def _file_inconsistency_notes(keys: list[bytes]) -> list[str]:
    """检测"钥匙串里的密钥"与"磁盘上的明文密钥文件"是否一致，不一致时给出现场提示。"""
    entries = secret_store.file_key_entries(_get_secret_path())
    mismatched = [path for path, key in entries if key is None or key not in keys]
    if not mismatched:
        return []
    logger.warning("[crypto] 明文密钥文件与当前生效的主密钥不一致: %s", mismatched)
    return [
        "注意：磁盘上的明文密钥文件与当前生效的主密钥不一致（"
        + "、".join(mismatched)
        + "）。已按当前来源为准；请先确认哪一份才是解密现有数据所用的密钥，"
        "处理清楚之前不要执行 akm secret purge-file"
    ]


def purge_file_keys() -> dict:
    """删除明文密钥文件（仅在非文件来源已持有同一把密钥时允许）。"""
    keys, source, _ = _read_or_create_keys()
    current = keys[0]

    if secret_store.effective_backend() != secret_store.BACKEND_KEYCHAIN:
        raise SecretStoreError(
            "拒绝清理：当前 secret_backend 仍为 file，明文文件正是配置内的密钥来源。"
            "请先执行 `akm secret migrate --to keychain`（会同时写入配置），"
            "确认应用能正常启动后再清理"
        )

    verified_by = ""
    if secret_store.keychain_supported():
        try:
            material = secret_store.keychain_read()
        except Exception as exc:  # noqa: BLE001 — 校验失败一律走"拒绝清理"，不能让异常逃逸成误删
            logger.warning("[crypto] 清理前读取钥匙串失败: %s", exc)
            material = None
        if material and material.current == current:
            verified_by = "keychain"
    if not verified_by:
        raise SecretStoreError(
            "拒绝清理：未能在钥匙串中校验到同一把主密钥。"
            "请先执行 `akm secret migrate --to keychain`，确认应用正常后再清理"
        )
    removed = _purge_verified_file_keys(keys)
    reset_cipher_cache()
    return {"verified_by": verified_by, "removed": removed}


def custody_status(probe: bool = False) -> dict:
    """主密钥托管状态（供 ``akm secret status`` 与排障使用）。"""
    primary = _get_secret_path()
    legacy = _get_legacy_secret_path()
    if os.path.realpath(primary) == os.path.realpath(legacy):
        legacy = None
    configured = secret_store.effective_backend()
    status = {
        "configured_backend": configured,
        "default_dir": DEFAULT_SECRET_DIR,
        "env_inline": bool((os.environ.get("AKM_SECRET_KEY") or "").strip()),
        "env_file": (os.environ.get("AKM_SECRET_FILE") or "").strip(),
        "last_source": _last_source,
        "last_notes": list(_last_notes),
        "loaded_keys": _cipher.key_count if _cipher is not None else 0,
        "files": secret_store.file_status(primary, legacy),
        "keychain": secret_store.keychain_status(),
    }
    if probe:
        ok, message = secret_store.keychain_probe()
        status["keychain_probe"] = {"ok": ok, "message": message}
    return status
