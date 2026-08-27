"""API Key 加密存储层：Fernet 兼容的对称加密。

此模块独立于 key 池管理（key_pool.py），只负责密钥的生命周期与加解密：
1. 主密钥（32 字节 urlsafe base64）存于本地文件 ~/.akm/secret.key（唯一真相源），
   首次使用时自动生成并写入该文件。
2. 令牌格式与 cryptography.fernet 完全互通。

底层使用 tinyaes 提供的 AES-128-CBC 块加密（无填充的原始 CBC），
PKCS7 填充与 HMAC-SHA256 认证在此模块完成。
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time

import tinyaes

logger = logging.getLogger(__name__)

# ── 密钥文件管理 ─────────────────────────────────────────────

SECRET_DIR = os.path.expanduser("~/.akm")
"""密钥文件所在目录（可被测试 monkeypatch 指向临时目录）"""

_cipher = None  # _MiniFernet | None：进程级缓存


def _get_secret_path() -> str:
    """密钥文件路径"""
    os.makedirs(SECRET_DIR, exist_ok=True)
    return os.path.join(SECRET_DIR, "secret.key")


class InvalidToken(Exception):
    """Fernet 令牌校验失败（与 cryptography.fernet.InvalidToken 行为对齐）"""


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
      现存 ~/.akm/secret.key 加密的存量数据也可无缝读取。
    - tinyaes 只提供无填充的 CBC 块加密，PKCS7 填充与 HMAC 认证在此完成。
    """

    def __init__(self, key):
        if isinstance(key, str):
            key = key.encode("utf-8")
        raw = base64.urlsafe_b64decode(key + b"=" * (-len(key) % 4))
        if len(raw) != 32:
            raise ValueError("Fernet 密钥必须是 32 字节")
        self._signing_key = raw[:16]
        self._encryption_key = raw[16:]

    @classmethod
    def generate_key(cls) -> bytes:
        """生成与 Fernet.generate_key() 等价的 urlsafe base64 密钥"""
        return base64.urlsafe_b64encode(os.urandom(32))

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


# ── 顶层便捷接口 ────────────────────────────────────────────

def _load_cipher() -> "_MiniFernet":
    """加载 Fernet 兼容加密器（主密钥存于本地 ~/.akm/secret.key）。

    加载顺序：
    1. 进程内缓存命中直接返回；
    2. 读本地 secret.key，存在则直接使用；
    3. 文件不存在时自动生成新密钥并写入，保证后续可用。
    """
    global _cipher
    if _cipher is not None:
        return _cipher

    key_path = _get_secret_path()
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            key = f.read().strip()
    else:
        key = _MiniFernet.generate_key()
        with open(key_path, "wb") as f:
            f.write(key)
        logger.info("[crypto] 已在 %s 生成新的主密钥", key_path)

    _cipher = _MiniFernet(key)
    return _cipher


def _encrypt(plain: str) -> str:
    """加密明文，返回 base64 编码的密文字符串"""
    return _load_cipher().encrypt(plain.encode()).decode()


def _decrypt(cipher_text: str) -> str:
    """解密 base64 编码的密文，返回明文"""
    # api_key 为空（用户尚未填写）时直接返回空串，避免对空令牌执行解密
    if not cipher_text:
        return ""
    return _load_cipher().decrypt(cipher_text.encode()).decode()