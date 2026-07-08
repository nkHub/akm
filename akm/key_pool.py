"""Key 池管理：加密存储、CRUD、状态管理、选择逻辑"""

import os
import time
import asyncio
import json
from cryptography.fernet import Fernet
from akm.db import get_connection
from akm.agent import AGENT_REGISTRY

# ── 用量查询默认脚本 ─────────────────────────────────────────
# 格式兼容 ccswitch：extractor 为 JS 函数字符串
# extractor 返回字段：isValid, remaining, unit

DEFAULT_USAGE_QUERY_SCRIPT = json.dumps({
    "request": {
        "url": "{{baseUrl}}/v1/usage",
        "method": "GET",
        "headers": {"Authorization": "Bearer {{apiKey}}"},
    },
    "extractor": (
        "function(response) {\n"
        "  const remaining = response?.remaining ?? response?.quota?.remaining ?? response?.balance;\n"
        "  const unit = response?.unit ?? response?.quota?.unit ?? \"USD\";\n"
        "  return {\n"
        "    isValid: response?.is_active ?? response?.isValid ?? true,\n"
        "    remaining,\n"
        "    unit,\n"
        "  };\n"
        "}"
    ),
}, ensure_ascii=False)

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


def _normalize_csv_models(models: str) -> str:
    """规范逗号分隔模型串，避免因空格或空项导致匹配不一致。"""
    if not models:
        return ""
    if models == "*":
        return "*"
    return ",".join(m.strip() for m in models.split(",") if m.strip())


def _normalize_provider_models(provider_models) -> str:
    """将提供商模型列表统一保存为 JSON 数组字符串。

    这里统一做两层处理：
    1. 去除空值与首尾空格，避免前端/上游脏数据落库。
    2. 去重但保留原始顺序，保证展示稳定且不重复。
    """
    if not provider_models:
        return ""
    if isinstance(provider_models, str):
        try:
            provider_models = json.loads(provider_models)
        except json.JSONDecodeError:
            provider_models = [provider_models]
    seen = set()
    normalized = []
    for item in provider_models:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)
    return json.dumps(normalized, ensure_ascii=False)


def _provider_models_list(raw) -> list[str]:
    """将数据库中的 provider_models 字段解析为字符串列表。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    result = []
    for item in data:
        name = str(item or "").strip()
        if name:
            result.append(name)
    return result


def key_model_list(key: dict) -> list[str]:
    """返回 key 当前应暴露给管理端/调用方的模型列表。

    规则：
    1. `models='*'` 时，返回已同步的 provider_models。
    2. `models` 为显式逗号分隔串时，返回切分后的模型列表。
    3. 其他异常/空值场景返回空数组，避免调用侧再写分支判断。
    """
    if not isinstance(key, dict):
        return []
    models = str(key.get("models") or "").strip()
    if not models:
        return []
    if models == "*":
        provider_models = key.get("provider_models")
        if isinstance(provider_models, list):
            return [str(item).strip() for item in provider_models if str(item).strip()]
        return _provider_models_list(provider_models)
    if models != "*":
        return [m.strip() for m in models.split(",") if m.strip()]
    return []


def _key_matches_model(row: dict, model: str) -> bool:
    """判断单个 key 是否命中当前模型。

    设计说明：
    1. 自定义 models 仍然保持精确匹配。
    2. 当 models='*' 时，不再无条件命中所有模型，而是使用
       provider_models 做“提供商可用模型集合”匹配。
    3. 若 provider_models 为空，则视为当前 key 没有可用模型清单，不命中。
    """
    normalized_model = str(model or "").strip()
    if not normalized_model:
        return False
    resolved_models = key_model_list(row)
    return normalized_model in set(resolved_models)


def _is_explicit_model_key(row: dict) -> bool:
    """判断当前 key 是否使用了显式模型配置，而不是 `*` 通配。"""
    return str(row.get("models") or "").strip() != "*"


def add_key(
    alias: str,
    provider: str,
    api_key: str,
    base_url: str | None = None,
    models: str = "*",
    provider_models=None,
    auth_header: str = "Bearer {api_key}",
    priority: int = 0,
) -> None:
    """添加一个新的 API key

    auth_header: 认证头模板，{api_key} 会被替换为实际 key
    """
    # 规范 models 字段：去除每个模型名前后空格，移除多余逗号
    models = _normalize_csv_models(models)
    if base_url is None:
        # 只查已知 provider 的默认地址，未知 provider 保留空字符串
        agent = AGENT_REGISTRY.get(provider)
        base_url = agent.default_base_url if agent else ""
    enc_key = _encrypt(api_key)
    normalized_provider_models = _normalize_provider_models(provider_models)
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO keys (alias, provider, api_key, base_url, models, provider_models, auth_header, priority)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (alias, provider, enc_key, base_url, models, normalized_provider_models, auth_header, priority),
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
    d["provider_models"] = _provider_models_list(d.get("provider_models"))
    d["model_list"] = key_model_list(d)
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
        d["provider_models"] = _provider_models_list(d.get("provider_models"))
        d["model_list"] = key_model_list(d)
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


def set_models(alias: str, models: str) -> None:
    """修改指定 key 的模型匹配配置。"""
    normalized = _normalize_csv_models(models)
    conn = get_connection()
    conn.execute(
        "UPDATE keys SET models = ? WHERE alias = ?", (normalized, alias)
    )
    conn.commit()
    conn.close()


def set_provider(alias: str, provider: str) -> None:
    """修改指定 key 的 provider。"""
    conn = get_connection()
    conn.execute(
        "UPDATE keys SET provider = ? WHERE alias = ?", (provider, alias)
    )
    conn.commit()
    conn.close()


def set_auth_header(alias: str, auth_header: str) -> None:
    """修改指定 key 的认证头模板。"""
    conn = get_connection()
    conn.execute(
        "UPDATE keys SET auth_header = ? WHERE alias = ?", (auth_header, alias)
    )
    conn.commit()
    conn.close()


def set_provider_models(alias: str, provider_models) -> None:
    """保存指定 key 同步得到的提供商模型列表。"""
    normalized = _normalize_provider_models(provider_models)
    conn = get_connection()
    conn.execute(
        "UPDATE keys SET provider_models = ? WHERE alias = ?", (normalized, alias)
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


def pick_key(model: str, exclude_aliases: list[str] | None = None) -> dict | None:
    """根据 model 名选择优先级最高的可用 key

    匹配规则：
    1. key 的 models 字段包含该 model（精确匹配），或 models 为 '*'
    2. status 为 'active'
    3. 按 priority ASC 排序，取第一个
    4. exclude_aliases 中的 alias 将被排除
    """
    clear_expired_rate_limits()
    model = model.strip()
    conn = get_connection()
    # 先筛出 active key，再按优先级在 Python 侧做匹配。
    # 这样可以让 models='*' 时结合 provider_models 精确判断是否命中。
    if exclude_aliases:
        placeholders = ",".join("?" * len(exclude_aliases))
        rows = conn.execute(
            f"""SELECT * FROM keys
               WHERE status = 'active'
                  AND alias NOT IN ({placeholders})
               ORDER BY priority ASC""",
            (*exclude_aliases,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM keys
               WHERE status = 'active'
               ORDER BY priority ASC""",
        ).fetchall()
    conn.close()
    # 先尝试显式模型 key，再回退到 `*` + provider_models 的兜底 key。
    # 这样即便优先级相同，也能保证“明确声明的模型绑定”优先于通配策略。
    for explicit_only in (True, False):
        for row in rows:
            d = dict(row)
            if _is_explicit_model_key(d) != explicit_only:
                continue
            if not _key_matches_model(d, model):
                continue
            d["api_key"] = _decrypt(d["api_key"])
            d["provider_models"] = _provider_models_list(d.get("provider_models"))
            return d
    return None


async def pick_key_async(model: str, exclude_aliases: list[str] | None = None) -> dict | None:
    """异步版本的 pick_key，在线程池中执行数据库查询"""
    return await asyncio.to_thread(pick_key, model, exclude_aliases)


def pick_wildcard_key(model: str = "", exclude_aliases: list[str] | None = None) -> dict | None:
    """选择 models='*' 的 active key（兜底用）。

    只有当前模型在 provider_models 内时才命中；未同步模型列表的 wildcard
    key 不再参与兜底匹配，避免旧兼容语义继续放大匹配范围。
    """
    clear_expired_rate_limits()
    conn = get_connection()
    if exclude_aliases:
        placeholders = ",".join("?" * len(exclude_aliases))
        rows = conn.execute(
            f"SELECT * FROM keys WHERE status = 'active' AND models = '*' AND alias NOT IN ({placeholders}) ORDER BY priority ASC",
            (*exclude_aliases,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM keys WHERE status = 'active' AND models = '*' ORDER BY priority ASC",
        ).fetchall()
    conn.close()
    normalized_model = str(model or "").strip()
    for row in rows:
        d = dict(row)
        provider_models = _provider_models_list(d.get("provider_models"))
        if not provider_models:
            continue
        if normalized_model and normalized_model not in provider_models:
            continue
        d["api_key"] = _decrypt(d["api_key"])
        d["provider_models"] = provider_models
        return d
    return None


async def pick_wildcard_key_async(model: str = "", exclude_aliases: list[str] | None = None) -> dict | None:
    """异步版本的 pick_wildcard_key"""
    return await asyncio.to_thread(pick_wildcard_key, model, exclude_aliases)


# ── 用量查询配置 ─────────────────────────────────────────────

def get_usage_query_config(alias: str) -> dict | None:
    """获取指定 key 的用量查询配置（脚本 + 间隔 + 最近结果）"""
    conn = get_connection()
    row = conn.execute(
        "SELECT usage_query_script, usage_query_interval_m, usage_queried_at, usage_data, usage_error, usage_query_endpoint FROM keys WHERE alias = ?",
        (alias,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    script_raw = row["usage_query_script"] or ""
    return {
        "alias": alias,
        "script": script_raw if script_raw else DEFAULT_USAGE_QUERY_SCRIPT,
        "script_is_default": not script_raw,
        "interval_m": int(row["usage_query_interval_m"] or 0),
        "queried_at": row["usage_queried_at"] or "",
        "data": _safe_json_parse(row["usage_data"]),
        "error": row["usage_error"] or "",
        "query_endpoint": row["usage_query_endpoint"] or "",
    }


def set_usage_query_config(alias: str, script: str | None = None, interval_m: int | None = None, query_endpoint: str | None = None) -> None:
    """设置 key 的用量查询配置（脚本 / 查询间隔(分钟) / 自定义端点）"""
    conn = get_connection()
    if script is not None:
        conn.execute("UPDATE keys SET usage_query_script = ? WHERE alias = ?", (script, alias))
    if interval_m is not None:
        conn.execute("UPDATE keys SET usage_query_interval_m = ? WHERE alias = ?", (int(interval_m or 0), alias))
    if query_endpoint is not None:
        conn.execute("UPDATE keys SET usage_query_endpoint = ? WHERE alias = ?", (query_endpoint, alias))
    conn.commit()
    conn.close()


def update_usage_data(alias: str, data: dict) -> None:
    """更新用量查询结果和错误信息"""
    conn = get_connection()
    conn.execute(
        "UPDATE keys SET usage_data = ?, usage_error = ?, usage_queried_at = datetime('now', 'localtime') WHERE alias = ?",
        (json.dumps(data, ensure_ascii=False), data.get("error", ""), alias),
    )
    conn.commit()
    conn.close()


def _safe_json_parse(raw: str | None) -> dict | None:
    """安全解析 JSON 字符串，失败返回 None"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
