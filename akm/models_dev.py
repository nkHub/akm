"""models.dev 目录数据：拉取、过滤、入库、进程内索引与取价。

数据来源：https://models.dev/api.json（注意不是 /json，/json 会 302 跳首页）。

设计要点（与 docs/superpowers/plans/2026-10-23-models-dev-pricing-sync.md 对应）：

1. **只剔 null**：`cost` 存在即为有效（`0/0/0/0` 这类全 0 价保留，代表免费/订阅渠道）；
   `cost` 为 null 或缺失的模型才跳过；某供应商下所有模型都被跳过 → 整条供应商剔除。
2. **入库而非 JSON 文件**：221 个供应商 / 8000+ 模型展平成文本有几百 KB，塞进
   config.json 会让每个请求都重新解析；改存 SQLite，统计走 SQL JOIN 聚合（实测
   2.6 万行 0.02 秒），明细场景走进程内索引（0.02 秒）。
3. **取价三段式**：本供应商命中 → 官方兜底（开关）→ 按 0。不做跨供应商的盲目猜价。
4. **协议能力不参与**：models.dev 没有协议能力字段，`npm` 只作为新建供应商时的
   初始勾选建议（`npm_protocol_hints`），绝不回写已有配置。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

import httpx

from akm.config import resolve_http_proxy_url
from akm.db import get_connection
from akm.error_log import write_error_log

# 真实数据地址：https://models.dev/json 会 302 到首页，必须用 /api.json
MODELS_DEV_API_URL = "https://models.dev/api.json"

# 拉取超时（秒）：整包约 5.9 MB，弱网下留足余量
FETCH_TIMEOUT_SEC = 60.0

# 官方兜底候选顺序（可在 config.json 的 models_dev_prefer_providers 覆盖）
DEFAULT_PREFER_PROVIDERS = (
    "openai,anthropic,deepseek,google,xai,moonshotai,zai,minimax,alibaba"
)


# ── 名称归一 ────────────────────────────────────────────────

# 模型名尾部的日期/版本后缀，用于宽松匹配（如 gpt-5.6-luna-2026-07-09）
_DATE_SUFFIX_RE = re.compile(r"[-_](?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4}|\d{3,4})$")

# 非对话能力的家族关键词（embedding / 重排）
_NON_CHAT_FAMILY_RE = re.compile(r"embed|bge|rerank", re.IGNORECASE)

# 没有 models.dev 元数据时，靠名字判定的排除词（宁可多留，不可误杀）
_NON_CHAT_NAME_RE = re.compile(
    r"embed|bge|rerank|image|dall-e|flux|stable-diffusion|whisper|tts",
    re.IGNORECASE,
)


def normalize_model_id(model: str) -> str:
    """把模型名归一成可比较形式：小写、去空白、去日期后缀。"""
    text = str(model or "").strip().lower()
    if not text:
        return ""
    return _DATE_SUFFIX_RE.sub("", text)


def candidate_model_ids(model: str) -> list[str]:
    """列出模型名的候选写法，按「精确 → 去 vendor 前缀 → 去日期后缀」排序。

    中转站经常把 ``deepseek/deepseek-v4.1-flash`` 与 ``deepseek-v4.1-flash``
    混用，所以匹配时要把带前缀与不带前缀都试一遍；但**只在同一个供应商内部**
    尝试，绝不跨供应商借用价格。
    """
    raw = str(model or "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    result: list[str] = []

    def _push(value: str) -> None:
        """按顺序收集去重后的候选名。"""
        if value and value not in seen:
            seen.add(value)
            result.append(value)

    _push(raw)
    bare = raw.split("/")[-1].strip()
    _push(bare)
    _push(_DATE_SUFFIX_RE.sub("", bare).strip())
    _push(_DATE_SUFFIX_RE.sub("", raw).strip())
    return result


# ── 拉取与过滤 ──────────────────────────────────────────────


def fetch_remote(etag: str = "") -> tuple[dict | None, str]:
    """拉取 models.dev 全量数据；支持 ETag 条件请求。

    返回 ``(payload, etag)``：命中 304 时 ``payload`` 为 ``None``（调用方保留旧数据）。
    任何网络/解析失败都返回 ``(None, "")`` 并写错误日志，不抛异常——同步失败
    不能影响应用启动与转发主链路。
    """
    headers = {"Accept": "application/json", "User-Agent": "akm-models-dev-sync"}
    if etag:
        headers["If-None-Match"] = etag
    proxy = resolve_http_proxy_url()
    try:
        # 统一在这里注入出站代理：显式传 proxy 会覆盖环境变量，未配置时走直连
        client_kwargs: dict = {"timeout": FETCH_TIMEOUT_SEC, "follow_redirects": True}
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(MODELS_DEV_API_URL, headers=headers)
    except Exception as exc:  # 网络异常一律降级
        write_error_log("models_dev.fetch", f"拉取 models.dev 失败: {exc}")
        return None, ""

    if resp.status_code == 304:
        return None, etag
    if resp.status_code != 200:
        write_error_log(
            "models_dev.fetch",
            f"拉取 models.dev 失败: HTTP {resp.status_code} {resp.text[:200]}",
        )
        return None, ""

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        write_error_log("models_dev.fetch", f"models.dev 响应不是合法 JSON: {exc}")
        return None, ""
    if not isinstance(payload, dict):
        write_error_log("models_dev.fetch", "models.dev 响应结构异常（顶层不是对象）")
        return None, ""
    return payload, str(resp.headers.get("etag") or "")


def _has_cost(model: dict) -> bool:
    """判断模型是否有价格数据（``0`` 算有，``null``/缺失才算没有）。"""
    cost = model.get("cost")
    if not isinstance(cost, dict):
        return False
    return cost.get("input") is not None


def _float_or_zero(value) -> float:
    """把价格字段安全转成浮点数（None → 0）。"""
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _int_or_zero(value) -> int:
    """把上下文长度等字段安全转成整数（None → 0）。"""
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def normalize_api_base(api: str) -> str:
    """把 models.dev 的 ``api`` 规范成「根地址」：去掉尾部斜杠与 ``/v1`` 后缀。

    为什么要去：models.dev 里 221 个供应商有 **177 个**的 ``api`` 带 ``/v1``
    （如 ``https://openrouter.ai/api/v1``、``https://api.302.ai/v1``）。
    而 AKM 的 :meth:`Agent.resolve_url` 自己会按需补 ``/v1``：

    - 带 ``/v1`` → ``{base}/{api_path}``
    - 不带 → ``{base}/v1/{api_path}``

    所以运行时两种写法都能跑通，但把 ``/v1`` 原样塞进 Key 编辑页的 base_url 输入框
    会让用户困惑（「我该不该自己再加一个 /v1？」），也让同一批供应商的地址格式不统一。
    入库时统一去掉，语义变成「这个供应商的 API 根地址」。

    注意：只剥**末尾**的 ``/v1``，不动路径中间的（如
    ``https://opencode.ai/inference/openai/v1`` → ``https://opencode.ai/inference/openai``）。
    """
    value = str(api or "").strip()
    if not value:
        return ""
    value = value.rstrip("/")
    if value.endswith("/v1"):
        value = value[: -len("/v1")]
    return value.rstrip("/")


def filter_payload(payload: dict) -> tuple[list[dict], list[dict], list[str]]:
    """按口径过滤全量数据，返回 ``(provider_rows, model_rows, dropped_provider_ids)``。

    过滤规则：``cost`` 为 dict 且 ``cost.input`` 不为 ``None`` 的模型保留（``0`` 保留）；
    若某供应商下没有任何符合条件的模型，整条供应商丢弃。
    """
    providers: list[dict] = []
    models: list[dict] = []
    dropped: list[str] = []

    for provider_id, provider in payload.items():
        if not isinstance(provider, dict):
            continue
        raw_models = provider.get("models")
        if not isinstance(raw_models, dict):
            dropped.append(str(provider_id))
            continue

        kept: list[dict] = []
        for model_id, model in raw_models.items():
            if not isinstance(model, dict) or not _has_cost(model):
                continue
            cost = model.get("cost") or {}
            limit = model.get("limit") if isinstance(model.get("limit"), dict) else {}
            kept.append(
                {
                    "provider_id": str(provider_id),
                    "model_id": str(model_id),
                    "input": _float_or_zero(cost.get("input")),
                    "cache_read": _float_or_zero(cost.get("cache_read")),
                    "cache_write": _float_or_zero(cost.get("cache_write")),
                    "output": _float_or_zero(cost.get("output")),
                    "context": _int_or_zero(limit.get("context")),
                    "max_output": _int_or_zero(limit.get("output")),
                    "modalities": json.dumps(
                        model.get("modalities") or {}, ensure_ascii=False
                    ),
                    "family": str(model.get("family") or ""),
                    "release_date": str(model.get("release_date") or ""),
                    "reasoning": 1 if model.get("reasoning") else 0,
                    "tool_call": 1 if model.get("tool_call") else 0,
                    "canonical_model_id": str(model.get("canonical_model_id") or ""),
                }
            )

        if not kept:
            dropped.append(str(provider_id))
            continue

        providers.append(
            {
                "id": str(provider_id),
                "name": str(provider.get("name") or provider_id),
                "api": normalize_api_base(str(provider.get("api") or "")),
                "doc": str(provider.get("doc") or ""),
                "npm": str(provider.get("npm") or ""),
                "model_count": len(kept),
            }
        )
        models.extend(kept)

    return providers, models, dropped


# ── 入库 ────────────────────────────────────────────────────


def meta_get(key: str, default: str = "") -> str:
    """读取一条同步元信息。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM models_dev_meta WHERE key = ?", (str(key),)
        ).fetchone()
        return str(row["value"]) if row and row["value"] is not None else default
    finally:
        conn.close()


def meta_all() -> dict:
    """一次性读出全部同步元信息（供设置页展示状态）。"""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT key, value FROM models_dev_meta").fetchall()
        return {str(r["key"]): str(r["value"] or "") for r in rows}
    finally:
        conn.close()


def sync_from_remote(*, force: bool = False) -> dict:
    """从 models.dev 拉取并整包替换本地目录数据。

    单事务内 ``DELETE`` + 批量 ``INSERT``（8000+ 行约 20 毫秒），失败自动回滚，
    保证「同步失败不影响旧数据」。返回统计信息供 CLI / API 展示。
    """
    current_etag = "" if force else meta_get("etag")
    payload, etag = fetch_remote(current_etag)

    if payload is None:
        # 304 或失败：两种情况都不动数据，用 meta 里的旧值回报状态
        stats = {
            "ok": etag != "" or bool(meta_get("fetched_at")),
            "skipped": True,
            "reason": "not_modified" if etag else "fetch_failed",
            "provider_count": int(meta_get("provider_count", "0") or 0),
            "model_count": int(meta_get("model_count", "0") or 0),
            "fetched_at": meta_get("fetched_at"),
        }
        return stats

    providers, models, dropped = filter_payload(payload)
    fetched_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection()
    try:
        with conn:  # 事务：全部成功才提交，异常自动回滚
            conn.execute("DELETE FROM models_dev_models")
            conn.execute("DELETE FROM models_dev_providers")
            conn.executemany(
                """INSERT OR REPLACE INTO models_dev_providers
                   (id, name, api, doc, npm, model_count, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        p["id"],
                        p["name"],
                        p["api"],
                        p["doc"],
                        p["npm"],
                        p["model_count"],
                        fetched_at,
                    )
                    for p in providers
                ],
            )
            conn.executemany(
                """INSERT OR REPLACE INTO models_dev_models
                   (provider_id, model_id, input, cache_read, cache_write, output,
                    context, max_output, modalities, family, release_date,
                    reasoning, tool_call, canonical_model_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        m["provider_id"],
                        m["model_id"],
                        m["input"],
                        m["cache_read"],
                        m["cache_write"],
                        m["output"],
                        m["context"],
                        m["max_output"],
                        m["modalities"],
                        m["family"],
                        m["release_date"],
                        m["reasoning"],
                        m["tool_call"],
                        m["canonical_model_id"],
                    )
                    for m in models
                ],
            )
            conn.executemany(
                "INSERT OR REPLACE INTO models_dev_meta (key, value) VALUES (?, ?)",
                [
                    ("etag", etag),
                    ("fetched_at", fetched_at),
                    ("provider_count", str(len(providers))),
                    ("model_count", str(len(models))),
                    (
                        "dropped_providers",
                        json.dumps(dropped, ensure_ascii=False),
                    ),
                ],
            )
    finally:
        conn.close()

    invalidate_cache()
    return {
        "ok": True,
        "skipped": False,
        "provider_count": len(providers),
        "model_count": len(models),
        "dropped_providers": dropped,
        "fetched_at": fetched_at,
    }


# ── 进程内索引 ──────────────────────────────────────────────

# 索引结构：
#   strict:  {provider_id: {model_id: (input, cache_read, output)}}
#   metas:   {model_id: {family, output_modalities, max_output, output_price, context, ...}}
#   providers: {provider_id: {name, api, doc, npm}}
# 以 fetched_at 作为版本号，同步后自动失效重建。
_INDEX_CACHE: dict = {"version": None, "checked_at": 0.0}


def invalidate_cache() -> None:
    """让进程内索引失效（同步完成后调用）。"""
    _INDEX_CACHE["version"] = None
    _INDEX_CACHE["checked_at"] = 0.0
    for key in ("strict", "metas", "providers"):
        _INDEX_CACHE.pop(key, None)


# 索引新鲜度校验间隔（秒）。目录数据只在同步时变化，但同步可能来自另一个
# 进程（CLI `akm pricing`），所以仍需要定期核对一次；间隔太短会把业务库的
# 高频写入误当成目录变化。
_INDEX_RECHECK_SEC = 10.0


def _load_index() -> dict:
    """按需构建并缓存索引。

    **不要用 akm.db / -wal 的 mtime 判断失效**：审计日志每写一条都会改动 WAL 文件，
    那样会让索引在每次取价时都重建（实测 74 ms/次）。开启费用统计后日志页每行取一次价，
    一页就要重建十几次，页面直接卡死、事件循环被打到十几秒延迟。
    正确做法：索引版本只认 ``models_dev_meta.fetched_at``（目录数据自己的版本号），
    并用 ``_INDEX_RECHECK_SEC`` 做节流，避免每行取价都开一次数据库连接。
    """

    now = time.time()
    if "strict" in _INDEX_CACHE and (now - float(_INDEX_CACHE.get("checked_at") or 0.0)) < _INDEX_RECHECK_SEC:
        return _INDEX_CACHE

    version = meta_get("fetched_at")
    if _INDEX_CACHE.get("version") == version and "strict" in _INDEX_CACHE:
        _INDEX_CACHE["checked_at"] = now
        return _INDEX_CACHE

    strict: dict[str, dict[str, tuple[float, float, float]]] = {}
    metas: dict[str, dict] = {}
    providers: dict[str, dict] = {}

    conn = get_connection()
    try:
        for row in conn.execute(
            "SELECT id, name, api, doc, npm FROM models_dev_providers"
        ):
            providers[str(row["id"])] = {
                "name": str(row["name"] or ""),
                "api": str(row["api"] or ""),
                "doc": str(row["doc"] or ""),
                "npm": str(row["npm"] or ""),
            }
        for row in conn.execute(
            """SELECT provider_id, model_id, input, cache_read, output,
                      context, max_output, modalities, family, release_date,
                      reasoning, tool_call, canonical_model_id
               FROM models_dev_models"""
        ):
            pid = str(row["provider_id"])
            mid = str(row["model_id"])
            strict.setdefault(pid, {})[mid] = (
                float(row["input"] or 0.0),
                float(row["cache_read"] or 0.0),
                float(row["output"] or 0.0),
            )
            # 元数据按模型名归一存一份（同一模型在多个供应商下元数据基本一致，
            # 后面的写入覆盖前面的即可，这里只用于能力判定与 /v1/models 展示）
            try:
                modalities = json.loads(row["modalities"] or "{}")
            except (json.JSONDecodeError, ValueError):
                modalities = {}
            metas.setdefault(mid, {})
            metas[mid].update(
                {
                    "context": int(row["context"] or 0),
                    "max_output": int(row["max_output"] or 0),
                    "output_modalities": list(
                        (modalities or {}).get("output") or []
                    ),
                    "input_modalities": list((modalities or {}).get("input") or []),
                    "family": str(row["family"] or ""),
                    "release_date": str(row["release_date"] or ""),
                    "reasoning": bool(row["reasoning"]),
                    "tool_call": bool(row["tool_call"]),
                    "canonical_model_id": str(row["canonical_model_id"] or ""),
                    "output_price": float(row["output"] or 0.0),
                }
            )
    finally:
        conn.close()

    _INDEX_CACHE.update(
        {
            "version": version,
            "checked_at": time.time(),
            "strict": strict,
            "metas": metas,
            "providers": providers,
        }
    )
    return _INDEX_CACHE


def model_meta(model_id: str) -> dict | None:
    """返回某模型在 models.dev 的元数据（任取一份），没有则 None。"""
    index = _load_index()
    metas = index.get("metas") or {}
    if model_id in metas:
        return metas[model_id]
    for candidate in candidate_model_ids(model_id):
        if candidate in metas:
            return metas[candidate]
    return None


def list_providers() -> list[dict]:
    """列出全部入库供应商（按 model_count 降序、id 升序稳定排序）。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, name, api, doc, npm, model_count, synced_at
               FROM models_dev_providers
               ORDER BY model_count DESC, id ASC"""
        ).fetchall()
        return [
            {
                "id": str(r["id"]),
                "name": str(r["name"] or ""),
                "api": str(r["api"] or ""),
                "doc": str(r["doc"] or ""),
                "npm": str(r["npm"] or ""),
                "model_count": int(r["model_count"] or 0),
                "synced_at": str(r["synced_at"] or ""),
            }
            for r in rows
        ]
    finally:
        conn.close()


def provider_meta(provider_id: str) -> dict | None:
    """返回供应商元数据（name/api/doc/npm），未收录则 None。"""
    index = _load_index()
    found = (index.get("providers") or {}).get(str(provider_id or ""))
    return dict(found) if isinstance(found, dict) else None


def provider_exists(provider_id: str) -> bool:
    """判断供应商是否已入库。"""
    index = _load_index()
    return str(provider_id or "") in (index.get("providers") or {})


# ── 取价（三段式） ──────────────────────────────────────────


def common_prefix(a: str, b: str) -> int:
    """两个模型名的公共前缀长度（用于在同 family 候选里挑最贴近的一个）。"""
    n = 0
    for x, y in zip(str(a or ""), str(b or "")):
        if x != y:
            break
        n += 1
    return n


def _price_in_provider(
    strict: dict, provider_id: str, model: str
) -> tuple[float, float, float] | None:
    """在指定供应商内部按名称阶梯找价格。"""
    table = strict.get(str(provider_id or ""))
    if not table:
        return None
    for candidate in candidate_model_ids(model):
        price = table.get(candidate)
        if price is not None:
            return price
    return None


def resolve_price(
    provider: str,
    model: str,
    *,
    official_fallback: bool = True,
    prefer_providers: str | list[str] | None = None,
) -> tuple[tuple[float, float, float], str] | None:
    """按三段式取价，返回 ``((input, cache_read, output), source)`` 或 ``None``。

    - 第一段：key 的供应商存在且其名下有该模型 → 用该供应商的价（``0`` 也照用）
    - 第二段：供应商不匹配且开启官方兜底 → 按原厂顺序找同名模型
    - 第三段：**同 family 回退** —— 官方改了模型名（如官方叫 ``deepseek-flash``，
      第三方中转叫 ``deepseek-v4.1-flash``）时，按 ``family`` 在官方供应商里找同系列
      的模型，用它的价。没有这一步，所有走中转站的 v4.1 请求都会被算成 $0。
    - 第四段：都失败 → ``None``（调用方按 0 计）

    **为什么不取「跨供应商中位数/最低价」**：实测同一模型在 22 个第三方供应商里
    output 价从 0.12 到 2.016（差 17 倍），取任何统计量都是猜；而 family 对应到
    官方同系列（0.6）才是可解释的锚点。

    ``source`` 形如 ``"models.dev:deepseek"``，便于前端与排障展示来源。
    """
    index = _load_index()
    strict = index.get("strict") or {}

    # 第一段：本供应商
    price = _price_in_provider(strict, provider, model)
    if price is not None:
        return price, f"models.dev:{provider}"

    if not official_fallback:
        return None

    # 第二段：官方兜底
    if isinstance(prefer_providers, str):
        order = [p.strip() for p in prefer_providers.split(",") if p.strip()]
    elif prefer_providers:
        order = [str(p).strip() for p in prefer_providers if str(p).strip()]
    else:
        order = [p.strip() for p in DEFAULT_PREFER_PROVIDERS.split(",") if p.strip()]

    for candidate_provider in order:
        price = _price_in_provider(strict, candidate_provider, model)
        if price is not None:
            return price, f"models.dev:{candidate_provider}(fallback)"

    # 第三段：同 family 回退。
    # 场景：官方把 deepseek-v4.1-flash 在自家目录里叫 deepseek-flash，
    # 第三方中转沿用 deepseek-v4.1-flash —— 名字对不上，但 family 都是 deepseek-flash。
    # 只在官方供应商里找（不用第三方），保证价格锚点稳定可解释。
    family = ""
    meta = model_meta(model)
    if isinstance(meta, dict):
        family = str(meta.get("family") or "").strip()
    if family:
        # 同 family 可能有多个候选（如 deepseek-flash / deepseek-v4-flash /
        # deepseek-v4-flash-vision-exp），不能拿 dict 遍历到的第一个 ——
        # 那会挑中带 vision 后缀的实验模型。按「名字与请求模型的相似度」排序，
        # 优先选公共前缀最长的那个（deepseek-v4.1-flash → deepseek-flash 胜出）。
        best: tuple[int, tuple[float, float, float], str] | None = None
        for candidate_provider in order:
            table = strict.get(candidate_provider) or {}
            for candidate_model, price in table.items():
                if candidate_model == model:
                    continue
                candidate_meta = model_meta(candidate_model)
                if not isinstance(candidate_meta, dict):
                    continue
                if str(candidate_meta.get("family") or "").strip() != family:
                    continue
                # 只认非全零价：官方把某些渠道标 0（订阅/免费），拿 0 当价格会低估成本
                if not price or not any(float(v) > 0 for v in price):
                    continue
                # 排序规则（依次比较，前者胜出）：
                # 1) 稳定模型优先 —— 带 -exp / -preview / -vision 的是实验/特化分支，
                #    价格虽常与稳定版相同，但拿它当锚点会让人困惑「为什么标这个名」；
                # 2) 与请求名的公共前缀更长者优先（deepseek-v4.1-flash → deepseek-v4-flash）。
                unstable = any(tag in candidate_model.lower() for tag in ("-exp", "-preview", "-vision", "-beta"))
                score = (0 if unstable else 1, common_prefix(model, candidate_model))
                if best is None or score > best[0]:
                    best = (score, price, f"models.dev:{candidate_provider}/{candidate_model}(family)")
        if best is not None:
            return best[1], best[2]
    return None


# ── 能力判定与协议提示 ──────────────────────────────────────


def is_chat_capable(model_id: str, meta: dict | None = None) -> bool:
    """判断模型是否可用于对话（过滤掉图像生成 / embedding / 重排）。

    实测要点：embedding 模型的 ``modalities.output`` 也是 ``["text"]``，
    所以不能只看 output，必须叠加 family 与「向量维度被记进 max_output」这两条。
    """
    info = meta if isinstance(meta, dict) else model_meta(model_id)

    if info is None:
        # 没有元数据（中转站私有模型名）：靠名字排除，未命中则保留，宁可多留
        return not bool(_NON_CHAT_NAME_RE.search(str(model_id or "")))

    outputs = info.get("output_modalities") or []
    if "text" not in [str(x).lower() for x in outputs]:
        return False

    if _NON_CHAT_FAMILY_RE.search(str(info.get("family") or "")):
        return False

    # embedding 的向量维度常被当成 max_output，且没有输出价格
    if float(info.get("output_price") or 0.0) == 0.0 and int(info.get("max_output") or 0) <= 4096:
        return False

    return True


def npm_protocol_hints(npm: str) -> dict:
    """由 ``npm`` 字段推导协议能力的**初始建议**（仅用于新建自定义供应商）。

    注意：这只是猜测，不能当真源——akm 内置的 deepseek 实际支持 Responses 与
    Messages，但 models.dev 给它的 npm 是 ``@ai-sdk/openai-compatible``。
    ``/anthropic`` 永远返回 False，必须由用户手动开启。
    """
    value = str(npm or "").strip().lower()
    hints = {
        "supports_chat": True,
        "supports_responses": False,
        "supports_messages": False,
        "messages_use_anthropic_path": False,
    }
    if value == "@ai-sdk/openai":
        hints["supports_responses"] = True
    elif value == "@ai-sdk/anthropic":
        hints["supports_chat"] = False
        hints["supports_messages"] = True
    elif value == "@ai-sdk/google-vertex/anthropic":
        hints["supports_chat"] = False
        hints["supports_messages"] = True
        hints["messages_use_anthropic_path"] = True
    return hints


async def run_sync_loop() -> None:
    """后台循环：首次启动立即同步，之后按 ``models_dev_sync_interval_hours`` 定期刷新。

    为什么需要这个：``models_dev_sync_enabled`` / ``models_dev_sync_interval_hours``
    两个配置项此前**只被读取、从未被任何任务使用**，全新安装的用户打开设置页
    看到的永远是「尚未同步」，必须手动点「立即同步」才有价格 —— 那是个死开关。

    设计取舍：
    - **首次启动不等同步完成**：用 ``asyncio.to_thread`` 丢到线程池，拉取 8000+ 模型
      约 2~3 秒，不能卡住服务启动（否则菜单栏图标会晚出现）；
    - 已有数据时仍会请求一次（带 ETag 条件请求），304 就直接跳过，几乎无开销；
    - 同步失败只写日志，绝不影响服务可用性（旧数据继续生效）。
    """
    import asyncio
    import logging

    from akm.config import load_config

    logger = logging.getLogger(__name__)
    first_round = True
    while True:
        try:
            cfg = load_config()
            if first_round or cfg.get("models_dev_sync_enabled") is not False:
                # 首轮无论开关都跑一次：新用户需要开箱有价格；
                # 之后才遵守 models_dev_sync_enabled（关掉表示不要定期刷新）。
                stats = await asyncio.to_thread(sync_from_remote, force=False)
                if stats.get("skipped"):
                    logger.debug("[models_dev] 目录已是最新：%s", stats.get("reason"))
                else:
                    logger.info(
                        "[models_dev] 目录已更新：%s 供应商 / %s 模型",
                        stats.get("provider_count"),
                        stats.get("model_count"),
                    )
            first_round = False
            try:
                hours = int(cfg.get("models_dev_sync_interval_hours") or 24)
            except (TypeError, ValueError):
                hours = 24
            await asyncio.sleep(max(1, min(24 * 30, hours)) * 3600)
        except asyncio.CancelledError:
            break
        except Exception:
            # 网络抖动、DNS 失败等都不能让循环退出：睡 10 分钟再试
            logger.warning("[models_dev] 目录同步失败，10 分钟后重试", exc_info=True)
            try:
                await asyncio.sleep(600)
            except asyncio.CancelledError:
                break
