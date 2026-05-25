"""Key 池管理：加密存储、CRUD、状态管理、选择逻辑"""

import os
import time
import asyncio
from cryptography.fernet import Fernet
from akm.db import get_connection
from akm.models import DEFAULT_BASE_URLS

SECRET_DIR = os.path.expanduser("~/.akm")
RATE_LIMIT_COOLDOWN = 60  # 限流冷却秒数
_cipher: Fernet | None = None


def _get_secret_path() -> str:
    """密钥文件路径"""
    os.makedirs(SECRET_DIR, exist_ok=True)
    return os.path.join(SECRET_DIR, "secret.key")


def _load_cipher() -> Fernet:
    """加载 Fernet 加密器，首次使用时自动生成密钥"""
    global _cipher
    if _cipher is not None:
        return _cipher
    key_path = _get_secret_path()
    if not os.path.exists(key_path):
        key = Fernet.generate_key()
        with open(key_path, "wb") as f:
            f.write(key)
    with open(key_path, "rb") as f:
        key = f.read()
    _cipher = Fernet(key)
    return _cipher


def _encrypt(plain: str) -> str:
    """加密明文，返回 base64 编码的密文字符串"""
    return _load_cipher().encrypt(plain.encode()).decode()


def _decrypt(cipher_text: str) -> str:
    """解密 base64 编码的密文，返回明文"""
    return _load_cipher().decrypt(cipher_text.encode()).decode()


def add_key(
    alias: str,
    provider: str,
    api_key: str,
    base_url: str | None = None,
    models: str = "*",
    auth_header: str = "Bearer {api_key}",
    priority: int = 0,
) -> None:
    """添加一个新的 API key

    auth_header: 认证头模板，{api_key} 会被替换为实际 key
    """
    if base_url is None:
        base_url = DEFAULT_BASE_URLS.get(provider, "")
    enc_key = _encrypt(api_key)
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO keys (alias, provider, api_key, base_url, models, auth_header, priority)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (alias, provider, enc_key, base_url, models, auth_header, priority),
        )
        conn.commit()
    except Exception:
        raise ValueError(f"Key 别名 '{alias}' 已存在")
    finally:
        conn.close()


def get_key(alias: str) -> dict | None:
    """获取指定 key 的完整信息（解密后）"""
    conn = get_connection()
    row = conn.execute("SELECT * FROM keys WHERE alias = ?", (alias,)).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    d["api_key"] = _decrypt(d["api_key"])
    return d


def list_keys(provider: str | None = None) -> list[dict]:
    """列出所有 key，按 priority 升序排列，可筛选供应商"""
    conn = get_connection()
    if provider:
        rows = conn.execute(
            "SELECT * FROM keys WHERE provider = ? ORDER BY priority ASC",
            (provider,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM keys ORDER BY priority ASC"
        ).fetchall()
    conn.close()
    result = []
    for row in rows:
        d = dict(row)
        d["api_key"] = _decrypt(d["api_key"])
        result.append(d)
    return result


def remove_key(alias: str) -> bool:
    """删除指定 key，返回是否删除成功"""
    conn = get_connection()
    cursor = conn.execute("DELETE FROM keys WHERE alias = ?", (alias,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def set_priority(alias: str, priority: int) -> None:
    """设置 key 优先级"""
    conn = get_connection()
    conn.execute(
        "UPDATE keys SET priority = ? WHERE alias = ?", (priority, alias)
    )
    conn.commit()
    conn.close()


def set_base_url(alias: str, base_url: str) -> None:
    """修改 key 的 base_url"""
    conn = get_connection()
    conn.execute(
        "UPDATE keys SET base_url = ? WHERE alias = ?", (base_url, alias)
    )
    conn.commit()
    conn.close()


def set_api_key(alias: str, api_key: str) -> None:
    """修改指定 key 的 API key 值"""
    enc_key = _encrypt(api_key)
    conn = get_connection()
    conn.execute(
        "UPDATE keys SET api_key = ? WHERE alias = ?", (enc_key, alias)
    )
    conn.commit()
    conn.close()


def set_status(alias: str, status: str) -> None:
    """设置 key 状态：active / disabled / rate_limited"""
    conn = get_connection()
    conn.execute(
        "UPDATE keys SET status = ? WHERE alias = ?", (status, alias)
    )
    conn.commit()
    conn.close()


# 限流冷却记录：{alias: 冷却截止时间戳}
_rate_limit_timers: dict[str, float] = {}


def mark_rate_limited(alias: str) -> None:
    """标记 key 为限流状态，60 秒后自动恢复"""
    set_status(alias, "rate_limited")
    _rate_limit_timers[alias] = time.time() + RATE_LIMIT_COOLDOWN


def clear_expired_rate_limits() -> None:
    """恢复冷却到期的 key 为 active 状态

    同时检查内存计时器和数据库中 rate_limited 的 key，
    防止应用重启后内存计时器丢失导致 key 永久卡在 rate_limited。
    """
    now = time.time()

    # 1. 检查内存中的计时器
    expired = [
        alias for alias, deadline in _rate_limit_timers.items()
        if now >= deadline
    ]
    for alias in expired:
        set_status(alias, "active")
        del _rate_limit_timers[alias]

    # 2. 检查数据库中 rate_limited 但内存无记录的 key（重启后场景）
    conn = get_connection()
    rows = conn.execute(
        "SELECT alias FROM keys WHERE status = 'rate_limited'"
    ).fetchall()
    conn.close()
    for row in rows:
        alias = row["alias"]
        if alias not in _rate_limit_timers:
            # 内存中无记录，说明是旧数据或重启后残留，安全恢复为 active
            set_status(alias, "active")


def pick_key(model: str) -> dict | None:
    """根据 model 名选择优先级最高的可用 key

    匹配规则：
    1. key 的 models 字段包含该 model（精确匹配），或 models 为 '*'
    2. status 为 'active'
    3. 按 priority ASC 排序，取第一个
    """
    clear_expired_rate_limits()
    conn = get_connection()
    # 先选 models='*' 通配的 + models 包含指定 model 的 active key
    rows = conn.execute(
        """SELECT * FROM keys
           WHERE status = 'active'
             AND (models = '*' OR ',' || models || ',' LIKE '%,' || ? || ',%')
           ORDER BY priority ASC""",
        (model,),
    ).fetchall()
    conn.close()
    if not rows:
        return None
    d = dict(rows[0])
    d["api_key"] = _decrypt(d["api_key"])
    return d


async def pick_key_async(model: str) -> dict | None:
    """异步版本的 pick_key，在线程池中执行数据库查询"""
    return await asyncio.to_thread(pick_key, model)


def pick_wildcard_key() -> dict | None:
    """选择 models='*' 的通配符 active key（兜底用）"""
    clear_expired_rate_limits()
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM keys WHERE status = 'active' AND models = '*' ORDER BY priority ASC",
    ).fetchall()
    conn.close()
    if not rows:
        return None
    d = dict(rows[0])
    d["api_key"] = _decrypt(d["api_key"])
    return d


async def pick_wildcard_key_async() -> dict | None:
    """异步版本的 pick_wildcard_key"""
    return await asyncio.to_thread(pick_wildcard_key)
