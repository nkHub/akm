"""key_pool 加解密层（pycryptodome 的 Fernet 兼容实现）测试。

覆盖四类场景：
1. 与 cryptography.fernet 的参考向量互通（本文件向量由 cryptography 预生成，保证格式兼容）；
2. 自身加解密往返一致；
3. 令牌被篡改或结构非法时抛 InvalidToken；
4. secret.key 密钥文件复用：首次生成后可复读，且用固定密钥可加解密。
"""
import base64

import pytest

from akm import key_pool
from akm.key_pool import InvalidToken, _MiniFernet


# ── 参考向量：由 cryptography.fernet 预生成（密钥为 32 字节 'K'） ──
_REF_KEY = (
    "S0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0s="
)
_REF_CASES = [
    ("sk-test-123", "gAAAAABqj7ScFYx5Yc_2aK31SgMi4G-To7IvF6MDKdmuYWPcbAj4Ttvpilwui66JLyI-OgFjapey53Dt-EQKsDVAaJ3IOXMlGw=="),
    ("hello fernet compat", "gAAAAABqj7Sct3zjU_UxCu6x21rlqSO9K5OmZzLL6uMmrcfzI5jLPmiTzGw1fGRd7Y7ICu5zMUlUIRV-A4zsWLjpyj8UAWr16hp7yMputAeZEN031O_b0IU="),
    ("", "gAAAAABqj7ScBg7fHGqz448UUWwk564uHV3dJrOIpmmFG0KqIumA5-0CIFwVhPVahviY-L6KpOgd2W0oeJHZR-kIHSIytnFoXg=="),
]


def test_mini_fernet_decrypts_cryptography_reference_vectors():
    """用 cryptography.fernet 预生成的令牌验证解密互通（存量数据无缝读取）"""
    f = _MiniFernet(_REF_KEY)
    for plain, token in _REF_CASES:
        assert f.decrypt(token.encode()).decode() == plain


def test_mini_fernet_generate_key_matches_expected_format():
    """generate_key 产出 44 字符 urlsafe base64（解码 32 字节）"""
    key = _MiniFernet.generate_key()
    assert len(key) == 44
    assert len(base64.urlsafe_b64decode(key)) == 32


def test_mini_fernet_roundtrip():
    """加解密往返一致，且密文为 urlsafe base64 无 padding 字符串"""
    f = _MiniFernet(_REF_KEY)
    data = b"sk-opencode-test-2026"
    token = f.encrypt(data)
    assert isinstance(token, bytes)
    assert f.decrypt(token) == data
    # 明文含中文与特殊字符
    zh = "密钥が含まれる明文 sk-123".encode("utf-8")
    assert f.decrypt(f.encrypt(zh)) == zh


def test_mini_fernet_rejects_tampered_token():
    """篡改令牌（翻转最后一个 base64 字符）应抛 InvalidToken"""
    f = _MiniFernet(_REF_KEY)
    token = f.encrypt(b"sk-secret")
    tampered = token[:-1] + (b"A" if token[-1:] != b"A" else b"B")
    with pytest.raises(InvalidToken):
        f.decrypt(tampered)


def test_mini_fernet_rejects_invalid_token_structure():
    """错误版本号 / 过短令牌 / 非法 base64 均抛 InvalidToken"""
    f = _MiniFernet(_REF_KEY)
    with pytest.raises(InvalidToken):
        f.decrypt(b"AAAA")  # 非 base64 语义解码后过短
    with pytest.raises(InvalidToken):
        # 版本号非 0x80（篡改第一字节）
        token = bytearray(f.encrypt(b"x"))
        token[0] = 0x79
        f.decrypt(bytes(token))


def test_load_cipher_creates_and_reuses_secret_key(tmp_path, monkeypatch):
    """首次加载生成 ~/.akm/secret.key；后续同路径复读同一密钥可解密"""
    monkeypatch.setattr(key_pool, "SECRET_DIR", str(tmp_path))
    monkeypatch.setattr(key_pool, "_cipher", None)
    key_path = tmp_path / "secret.key"

    assert not key_path.exists()
    c1 = key_pool._load_cipher()
    assert key_path.exists()

    # 重置缓存后重新加载，应命中同一密钥文件
    monkeypatch.setattr(key_pool, "_cipher", None)
    c2 = key_pool._load_cipher()
    token = c2.encrypt(b"keep-me")
    assert c1.decrypt(token) == b"keep-me"


def test_encrypt_decrypt_helpers_roundtrip(tmp_path, monkeypatch):
    """_encrypt/_decrypt 上层包装往返一致（API key 落库/读取走此路径）"""
    monkeypatch.setattr(key_pool, "SECRET_DIR", str(tmp_path))
    monkeypatch.setattr(key_pool, "_cipher", None)
    blob = key_pool._encrypt("sk-live-938275")
    assert blob != "sk-live-938275"
    monkeypatch.setattr(key_pool, "_cipher", None)
    assert key_pool._decrypt(blob) == "sk-live-938275"