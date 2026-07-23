"""费用估算 — 按模型单价表估算审计日志中的 Token 成本。

单价格式（仅此一种）：
  model=输入/输入缓存/输出
  单价单位：每 1M tokens；支持 # 注释与尾部 * 通配。

估算值不能替代供应商账单。
"""

from __future__ import annotations

DEFAULT_PRICING_TABLE = (
    "# OpenAI / GPT-5.x (USD per 1M tokens)\n"
    "gpt-5.4-mini=0.75/0.075/4.5\n"
    "gpt-5.4=2.5/0.25/15\n"
    "gpt-5.5=5/0.5/30\n"
    "gpt-5.6-luna=1/0.1/6\n"
    "gpt-5.6-terra=2.5/0.25/15\n"
    "gpt-5.6-sol=5/0.5/30\n"
    "# DeepSeek\n"
    "deepseek-v4-pro=0.435/0.003625/0.87\n"
    "# xAI（缓存按 0）\n"
    "grok-4.5=2/0/6\n"
    "# Moonshot Kimi\n"
    "kimi-k3=3/0.3/15\n"
    "# fallback\n"
    "*=0/0/0"
)


def parse_pricing(raw: str) -> list[tuple[str, float, float, float]]:
    """解析单价表为 (pattern, input, cache, output)。

    仅接受：model=输入/输入缓存/输出，费用统一以美元计价。
    """
    rules: list[tuple[str, float, float, float]] = []
    for line in str(raw or "").replace("\r", "\n").split("\n"):
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if "=" not in item:
            continue
        model, prices = item.split("=", 1)
        model = model.strip()
        prices = prices.strip()
        if not model:
            continue
        parts = [p.strip() for p in prices.split("/")]
        if len(parts) != 3:
            continue
        try:
            inp = float(parts[0])
            cache = float(parts[1])
            out = float(parts[2])
        except ValueError:
            continue
        rules.append((model, max(0.0, inp), max(0.0, cache), max(0.0, out)))
    return rules


def match_price(
    model: str,
    rules: list[tuple[str, float, float, float]],
) -> tuple[float, float, float]:
    """按书写顺序匹配单价；`*` 或 `prefix*` 通配。"""
    name = str(model or "")
    fallback = (1.0, 1.0, 3.0)
    for pattern, inp, cache, out in rules:
        if pattern == "*":
            fallback = (inp, cache, out)
            continue
        if pattern.endswith("*"):
            if name.startswith(pattern[:-1]):
                return inp, cache, out
        elif name == pattern:
            return inp, cache, out
    return fallback


def estimate_row_cost(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    cache_creation_tokens: int,
    rules: list[tuple[str, float, float, float]],
) -> tuple[float, str]:
    """估算单条请求费用。返回 (cost, currency)。"""
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    cached = max(0, int(cached_tokens or 0))
    creation = max(0, int(cache_creation_tokens or 0))
    if cached > prompt > 0:
        cached = prompt
    non_cached = max(0, prompt - cached) + creation
    if non_cached <= 0 and completion <= 0 and cached <= 0:
        return 0.0, "$"

    inp_price, cache_price, out_price = match_price(model, rules)
    cost = (
        (non_cached / 1_000_000.0) * inp_price
        + (cached / 1_000_000.0) * cache_price
        + (completion / 1_000_000.0) * out_price
    )
    return float(cost), "$"


def add_cost(costs: dict[str, float], currency: str, cost: float) -> None:
    costs["$"] = float(costs.get("$", 0.0) or 0.0) + float(cost)


def finalize_costs(costs: dict[str, float]) -> tuple[float, str, dict[str, float]]:
    """返回固定美元符号的汇总费用。"""
    total = round(float(costs.get("$", 0.0) or 0.0), 2)
    return total, "$", {"$": total} if costs else {}


def pricing_snapshot(pricing_table: str) -> dict:
    rules = parse_pricing(pricing_table)
    return {
        "rules": [
            {
                "pattern": p,
                "input_per_1m": inp,
                "cache_per_1m": cache,
                "output_per_1m": out,
            }
            for p, inp, cache, out in rules
        ],
    }
