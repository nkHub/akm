"""模型目录解析（移植自 flow 项目的 models.ts）。

flow 原本通过 HTTP 拉取 AKM 网关的 /v1/models；并入 AKM 后改为直接
读取本机 key 池（key_pool.list_keys）构建可用模型目录，省去一层 HTTP。
始终附加 mock 三件套兜底，保证无 key 时也能跑通流程演示。
"""

import re
from typing import Any

from akm.key_pool import list_keys


# ── ModelConfig 结构 ──────────────────────────────────────────
# {id, name, provider, model, baseUrl?, strengths, costPer1k?}
# strengths: reason/code/review/fast/cheap/long_context


def strengths_for_model(model_id: str) -> list[str]:
    """按模型 id 启发式推断能力标签（移植自 flow strengthsForModel）。"""
    m = (model_id or "").lower()
    if re.search(r"opus|o3|o1|r1|reason|pro|ultra", m):
        return ["reason", "long_context", "review"]
    if re.search(r"coder|code|codex|deepseek|sonnet|gpt-4|claude", m):
        return ["code", "reason"]
    if re.search(r"mini|flash|haiku|small|fast|lite", m):
        return ["fast", "cheap"]
    return ["code", "fast"]


# mock 三件套（兜底演示用；provider=mock 时引擎直接生成假内容，不走 LLM）
DEFAULT_MODELS: list[dict] = [
    {
        "id": "mock-reasoner",
        "name": "Mock Reasoner",
        "provider": "mock",
        "model": "mock-reasoner",
        "strengths": ["reason", "long_context"],
        "costPer1k": None,
    },
    {
        "id": "mock-coder",
        "name": "Mock Coder",
        "provider": "mock",
        "model": "mock-coder",
        "strengths": ["code", "fast"],
        "costPer1k": None,
    },
    {
        "id": "mock-reviewer",
        "name": "Mock Reviewer",
        "provider": "mock",
        "model": "mock-reviewer",
        "strengths": ["review"],
        "costPer1k": None,
    },
]


def resolve_model_catalog() -> list[dict]:
    """从 AKM key 池构建可用模型目录，mock 三件套始终附加兜底。"""
    catalog: list[dict] = []
    seen: set[str] = set()
    try:
        keys = list_keys() or []
    except Exception:
        keys = []
    for key in keys:
        models = set(key.get("provider_models") or [])
        explicit = key.get("models") or []
        if isinstance(explicit, str):
            explicit = [explicit]
        models.update(m for m in explicit if m not in (None, "", "*"))
        for model in sorted(models):
            if model in seen:
                continue
            seen.add(model)
            catalog.append(
                {
                    "id": model,
                    "name": model,
                    "provider": "custom",
                    "model": model,
                    "baseUrl": "",
                    "strengths": strengths_for_model(model),
                    "costPer1k": None,
                }
            )
    # mock 兜底（放在目录末尾，find_model 的"第一个非 mock"逻辑会优先用真实模型）
    for m in DEFAULT_MODELS:
        if m["id"] not in seen:
            catalog.append(m)
    return catalog


def find_model(models: list[dict], model_id: str) -> dict | None:
    """按占位 modelId 解析真实模型（移植自 flow findModel）。

    优先级：精确匹配 id/model → 模板 mock 占位按 strength hint → 第一个非 mock → models[0]。
    """
    if not models:
        return None
    model_id = model_id or ""
    for m in models:
        if m.get("id") == model_id or m.get("model") == model_id:
            return m
    # mock 占位按 strength 提示匹配
    if model_id:
        if re.search(r"reason|opus", model_id):
            for m in models:
                if "reason" in m.get("strengths", []):
                    return m
        elif "review" in model_id:
            for m in models:
                if "review" in m.get("strengths", []):
                    return m
        elif re.search(r"coder|code", model_id):
            for m in models:
                if "code" in m.get("strengths", []):
                    return m
    for m in models:
        if m.get("provider") != "mock":
            return m
    return models[0]


def estimate_cost_usd(model: dict | None, tokens_in: int, tokens_out: int) -> float:
    """按模型单价估算费用（美元）；无 costPer1k 返回 0。"""
    if not model:
        return 0.0
    cost = model.get("costPer1k")
    if not cost:
        return 0.0
    return round((tokens_in / 1000.0) * cost.get("input", 0) + (tokens_out / 1000.0) * cost.get("output", 0), 6)
