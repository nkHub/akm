"""审计日志「来源」标签推导。

统计页「按来源」分组与审计页「来源」列共用这里的同一套规则，
避免前后端各写一份推导逻辑后逐渐漂移（审计页只负责按标签着色）。

判定顺序：
1. 内部标记 `x-akm-source`（chat / flow / task，分别由 /v1/agent、Flow 引擎、
   定时任务写入）；
2. User-Agent 关键词（OpenCode / Claude / Codex / Cursor / curl / Python）；
3. 兜底取 UA 中斜杠前的产品名；
4. 无法识别时统一归入「其他」。
"""

from __future__ import annotations

import json

#: 无法识别来源时的兜底分组名
UNKNOWN_SOURCE = "其他"

#: x-akm-source 内部标记 → 展示名
_INTERNAL_SOURCES = {
    "chat": "Chat",
    "flow": "Flow",
    "task": "Task",
}

#: User-Agent 关键词 → 展示名（按顺序匹配，先命中先返回）
_UA_KEYWORDS = (
    ("opencode", "OpenCode"),
    ("claude-cli", "Claude"),
    ("claude code", "Claude"),
    ("codex", "Codex"),
    ("cursor", "Cursor"),
    ("curl", "curl"),
    ("python", "Python"),
)


def extract_source(request_headers) -> str:
    """从审计日志的 request_headers 中提取展示用来源标签。

    Args:
        request_headers: 审计日志落库的请求头，JSON 字符串或已解析的 dict。

    Returns:
        来源展示名；无法识别时返回 ``UNKNOWN_SOURCE``。
    """
    if isinstance(request_headers, dict):
        headers = request_headers
    else:
        try:
            headers = json.loads(request_headers or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            return UNKNOWN_SOURCE
    if not isinstance(headers, dict):
        return UNKNOWN_SOURCE

    internal = str(headers.get("x-akm-source") or "").strip().lower()
    if internal in _INTERNAL_SOURCES:
        return _INTERNAL_SOURCES[internal]

    ua = str(headers.get("user-agent") or "").strip().lower()
    if not ua:
        return UNKNOWN_SOURCE
    for keyword, label in _UA_KEYWORDS:
        if keyword in ua:
            return label
    # 兜底：直接取 UA 里斜杠前的产品名，保持与审计页一致（小写产品名）。
    product = ua.split("/")[0].strip()
    return product or UNKNOWN_SOURCE


__all__ = ["UNKNOWN_SOURCE", "extract_source"]
