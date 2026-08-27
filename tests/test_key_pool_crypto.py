"""key_pool 加解密层测试（加密逻辑见 akm/crypto.py）。

覆盖四类场景：
1. 与 cryptography.fernet 的参考向量互通（本文件向量由 cryptography 预生成，保证格式兼容）；
2. 自身加解密往返一致；
3. 令牌被篡改或结构非法时抛 InvalidToken；
4. secret.key 密钥文件复用：首次生成后可复读，且用固定密钥可加解密。
"""

import base64

import pytest

from akm import crypto
from akm.crypto import InvalidToken, _MiniFernet


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


def test_load_cipher_prefers_keychain(tmp_path, monkeypatch):
    """本地 secret.key 存在时直接使用，不重复生成"""
    key = _MiniFernet.generate_key()
    key_path = tmp_path / "secret.key"
    monkeypatch.setattr(crypto, "SECRET_DIR", str(tmp_path))
    monkeypatch.setattr(crypto, "_cipher", None)
    key_path.write_bytes(key)

    c1 = crypto._load_cipher()
    assert key_path.read_bytes() == key  # 不覆盖已有密钥
    assert key_path.read_bytes() != _MiniFernet.generate_key()  # 用原密钥，未重新生成

    monkeypatch.setattr(crypto, "_cipher", None)
    c2 = crypto._load_cipher()
    token = c2.encrypt(b"keep-me")
    assert c1.decrypt(token) == b"keep-me"


def test_load_cipher_migrates_fallback_file_to_keychain(tmp_path, monkeypatch):
    """本地 secret.key 存在时直接使用（无 Keychain 依赖）。"""
    file_key = _MiniFernet.generate_key()
    key_path = tmp_path / "secret.key"
    key_path.write_bytes(file_key)
    monkeypatch.setattr(crypto, "SECRET_DIR", str(tmp_path))
    monkeypatch.setattr(crypto, "_cipher", None)

    c1 = crypto._load_cipher()
    assert key_path.exists()             # 保留本地文件
    assert key_path.read_bytes() == file_key
    token = c1.encrypt(b"migrated")
    monkeypatch.setattr(crypto, "_cipher", None)
    c2 = crypto._load_cipher()
    assert c2.decrypt(token) == b"migrated"


def test_load_cipher_generates_and_saves_new_key(tmp_path, monkeypatch):
    """本地没有 secret.key 时：自动生成新密钥并写入文件"""
    monkeypatch.setattr(crypto, "SECRET_DIR", str(tmp_path))
    monkeypatch.setattr(crypto, "_cipher", None)

    c1 = crypto._load_cipher()
    key_path = tmp_path / "secret.key"
    assert key_path.exists()                          # 已生成并写盘
    assert len(key_path.read_bytes().strip()) == 44   # 32 字节 urlsafe base64
    token = c1.encrypt(b"fresh")
    assert c1.decrypt(token) == b"fresh"


def test_load_cipher_uses_memory_key_when_keychain_unavailable(tmp_path, monkeypatch):
    """本地 secret.key 写入正常场景：生成并写盘，加解密可用。"""
    monkeypatch.setattr(crypto, "SECRET_DIR", str(tmp_path))
    monkeypatch.setattr(crypto, "_cipher", None)

    c1 = crypto._load_cipher()
    assert (tmp_path / "secret.key").exists()
    token = c1.encrypt(b"ephemeral")
    assert c1.decrypt(token) == b"ephemeral"


def test_load_cipher_reuses_process_cache(tmp_path, monkeypatch):
    """进程内缓存命中时不再读取密钥文件"""
    calls = []
    key = _MiniFernet.generate_key()

    monkeypatch.setattr(crypto, "SECRET_DIR", str(tmp_path))
    monkeypatch.setattr(crypto, "_cipher", None)
    (tmp_path / "secret.key").write_bytes(key)

    def counting_load():
        calls.append(1)
        with open(tmp_path / "secret.key", "rb") as f:
            return f.read().strip()

    original_load = crypto._load_cipher
    monkeypatch.setattr(crypto, "_cipher", None)
    # 直接验证：第二次调用走后只读一次文件
    monkeypatch.setattr(crypto, "_get_secret_path", lambda: str(tmp_path / "secret.key"))
    import builtins as _b
    real_open = _b.open
    opened = []

    def spy_open(*a, **kw):
        if "secret.key" in str(a[0]):
            opened.append(a[0])
        return real_open(*a, **kw)

    monkeypatch.setattr("builtins.open", spy_open)
    crypto._load_cipher()
    crypto._load_cipher()
    assert len(opened) == 1  # 第二次命中缓存不再读文件


def test_encrypt_decrypt_helpers_roundtrip(tmp_path, monkeypatch):
    """_encrypt/_decrypt 上层包装往返一致（API key 落库/读取走此路径）"""
    key = _MiniFernet.generate_key()
    monkeypatch.setattr(crypto, "SECRET_DIR", str(tmp_path))
    monkeypatch.setattr(crypto, "_cipher", None)
    (tmp_path / "secret.key").write_bytes(key)

    blob = crypto._encrypt("sk-live-938275")
    assert blob != "sk-live-938275"
    monkeypatch.setattr(crypto, "_cipher", None)
    assert crypto._decrypt(blob) == "sk-live-938275"