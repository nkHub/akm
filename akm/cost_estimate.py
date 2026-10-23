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


def estimate_row_cost_detail(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    cache_creation_tokens: int,
    rules: list[tuple[str, float, float, float]] | None = None,
    provider: str = "",
    resolver: "PriceResolver | None" = None,
) -> tuple[float, str, dict]:
    """估算单条请求费用，并拆分输入未命中缓存 / 缓存命中 / 输出三部分明细。

    返回 (cost, currency, detail)，其中 detail 结构为：
        {
            "input_missed": {"tokens": int, "cost": float},   # 输入未命中缓存（含缓存写入）
            "input_cached": {"tokens": int, "cost": float},   # 输入缓存命中
            "output":      {"tokens": int, "cost": float},    # 输出
        }
    三部分 cost 之和与返回的总 cost 严格一致，供前端 tooltip 逐项展示。
    """
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    cached = max(0, int(cached_tokens or 0))
    creation = max(0, int(cache_creation_tokens or 0))
    if cached > prompt > 0:
        cached = prompt
    non_cached = max(0, prompt - cached) + creation
    zero_detail = {
        "input_missed": {"tokens": 0, "cost": 0.0},
        "input_cached": {"tokens": 0, "cost": 0.0},
        "output": {"tokens": 0, "cost": 0.0},
    }
    if non_cached <= 0 and completion <= 0 and cached <= 0:
        return 0.0, "$", zero_detail

    if resolver is not None:
        # 三段式取价：本供应商 → 官方兜底（开关）→ 按 0
        inp_price, cache_price, out_price = resolver.resolve(provider, model)
    else:
        inp_price, cache_price, out_price = match_price(model, list(rules or []))
    input_missed_cost = (non_cached / 1_000_000.0) * inp_price
    input_cached_cost = (cached / 1_000_000.0) * cache_price
    output_cost = (completion / 1_000_000.0) * out_price
    cost = input_missed_cost + input_cached_cost + output_cost
    return float(cost), "$", {
        "input_missed": {"tokens": non_cached, "cost": input_missed_cost},
        "input_cached": {"tokens": cached, "cost": input_cached_cost},
        "output": {"tokens": completion, "cost": output_cost},
    }


def estimate_row_cost(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    cache_creation_tokens: int,
    rules: list[tuple[str, float, float, float]] | None = None,
    provider: str = "",
    resolver: "PriceResolver | None" = None,
) -> tuple[float, str]:
    """估算单条请求费用。返回 (cost, currency)。"""
    cost, currency, _ = estimate_row_cost_detail(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        cache_creation_tokens=cache_creation_tokens,
        rules=rules,
        provider=provider,
        resolver=resolver,
    )
    return cost, currency


def add_cost(costs: dict[str, float], currency: str, cost: float) -> None:
    costs["$"] = float(costs.get("$", 0.0) or 0.0) + float(cost)


def finalize_costs(costs: dict[str, float], precision: int = 5) -> tuple[float, str, dict[str, float]]:
    """返回固定美元符号的汇总费用；precision 控制内部四舍五入小数位数。"""
    total = round(float(costs.get("$", 0.0) or 0.0), precision)
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


class PriceResolver:
    """费用取价器：统一从 models.dev 目录取价。

    **价格只有一个来源：models.dev 目录**（自动同步，见 :mod:`akm.models_dev`）。
    手写单价表已移除 —— 它需要人工维护、容易漏配（历史日志里 5814 次请求因缺条目被算成 $0），
    而目录数据覆盖 8000+ 模型且能自动更新。

    取价三段式：key 的供应商存在且名下有该模型 → 用它在该供应商下的价；
    不匹配时按原厂兜底（``models_dev_official_fallback`` 开关）；都失败按 0。
    """

    __slots__ = ("mode", "official_fallback", "prefer_providers")

    def __init__(
        self,
        *,
        mode: str = "snapshot",
        official_fallback: bool = True,
        prefer_providers: str = "",
    ) -> None:
        # 只保留 snapshot；老配置里的 manual / hybrid 一律归一化过来
        self.mode = "snapshot" if mode != "snapshot" else mode
        self.official_fallback = official_fallback is not False
        self.prefer_providers = str(prefer_providers or "").strip()

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> "PriceResolver":
        """按当前配置构造取价器。"""
        if not isinstance(cfg, dict):
            try:
                from akm.config import load_config

                cfg = load_config()
            except Exception:
                cfg = {}
        return cls(
            mode=str(cfg.get("cost_pricing_source") or "snapshot"),
            official_fallback=cfg.get("models_dev_official_fallback") is not False,
            prefer_providers=str(cfg.get("models_dev_prefer_providers") or ""),
        )

    def resolve(
        self, provider: str, model: str
    ) -> tuple[float, float, float]:
        """返回一条请求的 (输入, 缓存命中, 输出) 单价；未命中按 0。"""
        try:
            from akm import models_dev

            found = models_dev.resolve_price(
                provider,
                model,
                official_fallback=self.official_fallback,
                prefer_providers=self.prefer_providers or None,
            )
        except Exception:
            # 目录数据缺失/损坏时不能影响费用统计，按未命中处理
            return (0.0, 0.0, 0.0)
        return found[0] if found else (0.0, 0.0, 0.0)


def build_resolver(cfg: dict | None = None) -> PriceResolver:
    """便捷入口：按配置构造取价器（供各调用点统一使用）。"""
    return PriceResolver.from_config(cfg)
