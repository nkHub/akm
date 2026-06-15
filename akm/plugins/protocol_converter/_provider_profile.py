"""协议转换层的供应商策略画像。

目标：
- 把和 provider 相关的协议兼容开关收口到一处
- 先覆盖当前已经出现的最小策略集合，避免在多个 adapter 中散落 `provider == ...`
- 后续若要扩展到更多供应商或更多能力，只需要在这里补配置
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderProfile:
    """协议转换层使用的供应商能力画像。"""

    name: str
    inject_max_completion_tokens: bool = False
    inject_reasoning_effort: bool = False
    map_metadata_user_id_to_user: bool = True
    responses_force_thinking_enabled: bool = False
    responses_default_reasoning_effort: str | None = None


DEFAULT_PROVIDER_PROFILE = ProviderProfile(name="default")

PROVIDER_PROFILES: dict[str, ProviderProfile] = {
    # OpenAI Chat / Responses 新族谱通常兼容这两项补齐。
    "openai": ProviderProfile(
        name="openai",
        inject_max_completion_tokens=True,
        inject_reasoning_effort=True,
        map_metadata_user_id_to_user=True,
    ),
    # 目前 deepseek / anthropic 在 Messages -> Chat 侧不额外补这些 OpenAI 语义字段。
    # DeepSeek 的 Responses -> Chat 兼容依赖 thinking=enabled + reasoning_effort 缺省值。
    "deepseek": ProviderProfile(
        name="deepseek",
        responses_force_thinking_enabled=True,
        responses_default_reasoning_effort="high",
    ),
    "anthropic": ProviderProfile(name="anthropic"),
}


def get_provider_profile(provider_name: str | None) -> ProviderProfile:
    """按 provider 名返回协议转换画像，未知供应商使用保守默认值。"""
    name = str(provider_name or "").strip().lower()
    if not name:
        return DEFAULT_PROVIDER_PROFILE
    return PROVIDER_PROFILES.get(name, DEFAULT_PROVIDER_PROFILE)
