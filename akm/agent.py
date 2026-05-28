"""上游 AI 供应商代理（Agent），封装 URL 拼接、认证头构建、协议转换判断

内置 Agent 在 BUILTIN_AGENTS 中定义，不可删除。
自定义 Agent 通过 register_agent() 添加，持久化到 ~/.akm/config.json。
"""

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Agent:
    """上游 AI 供应商代理

    每个供应商对应一个 Agent 实例，统一管理：
    - 默认 base_url 和认证头模板
    - URL 拼接逻辑（支持 key 级 base_url 覆盖）
    - 协议转换判断（Responses / Messages / Chat 互转）
    """

    name: str
    default_base_url: str
    default_auth_header: str = "Bearer {api_key}"

    # 协议能力标记
    supports_responses: bool = False
    supports_chat: bool = True
    supports_messages: bool = False

    def resolve_url(self, key: dict, api_path: str) -> str:
        """根据 Key 配置解析最终上游 URL

        优先级: key.base_url > agent.default_base_url
        """
        raw = key.get("base_url") or ""
        base = raw.rstrip("/") if raw else self.default_base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/{api_path}"
        return f"{base}/v1/{api_path}"

    def build_headers(self, key: dict) -> dict:
        """构建请求头（含 Authorization）

        auth_header 模板中的 {api_key} 会被替换为解密后的 Key
        """
        template = key.get("auth_header") or self.default_auth_header
        return {
            "Authorization": template.format(api_key=key["api_key"]),
            "Content-Type": "application/json",
        }

    def needs_conversion(self, api_path: str) -> Optional[str]:
        """判断是否需要协议转换，返回目标 api_path 或 None

        例：deepseek 不支持 /v1/responses，需转为 /v1/chat/completions
        """
        if api_path == "responses" and not self.supports_responses:
            if self.supports_chat:
                return "chat/completions"
        if api_path == "messages" and not self.supports_messages:
            if self.supports_chat:
                return "chat/completions"
        if api_path == "chat/completions" and not self.supports_chat:
            if self.supports_messages:
                return "messages"
        return None

    # ── 适配器（懒加载，按源格式命名）──

    @property
    def responses_adapter(self):
        """Responses 格式适配器（用于将 Responses 请求转为 Chat 格式，DeepSeek 等不原生支持 Responses 的供应商）"""
        if not hasattr(self, "_responses_adapter"):
            from akm.adapters.responses_adapter import ResponsesAdapter
            self._responses_adapter = ResponsesAdapter()
        return self._responses_adapter

    @property
    def messages_adapter(self):
        """Messages 格式适配器（用于将 Messages 请求转为 Chat 格式，Anthropic → Chat 兼容供应商）"""
        if not hasattr(self, "_messages_adapter"):
            from akm.adapters.messages_adapter import MessagesAdapter
            self._messages_adapter = MessagesAdapter()
        return self._messages_adapter

    @property
    def chat_adapter(self):
        """Chat 格式适配器（预留，当前无使用场景）"""
        if not hasattr(self, "_chat_adapter"):
            from akm.adapters.chat_adapter import ChatAdapter
            self._chat_adapter = ChatAdapter()
        return self._chat_adapter


# ── 内置供应商（不可删除）──
BUILTIN_AGENTS: dict[str, Agent] = {
    "openai": Agent(
        name="openai",
        default_base_url="https://api.openai.com",
        supports_responses=True,
        supports_chat=True,
    ),
    "deepseek": Agent(
        name="deepseek",
        default_base_url="https://api.deepseek.com",
        supports_responses=False,
        supports_chat=True,
    ),
    "anthropic": Agent(
        name="anthropic",
        default_base_url="https://api.anthropic.com",
        supports_messages=True,
        supports_chat=False,
    ),
}

# ── 全局注册表 = 内置 + 自定义 ──
AGENT_REGISTRY: dict[str, Agent] = dict(BUILTIN_AGENTS)
_CUSTOM_AGENTS_DIRTY = False  # 延迟写入标记


def get_agent(provider: str) -> Agent:
    """根据 provider 名获取 Agent，未知供应商返回 openai 兜底"""
    return AGENT_REGISTRY.get(provider, AGENT_REGISTRY["openai"])


def list_agents() -> list[dict]:
    """列出所有 Agent（内置 + 自定义），返回含 is_custom 标记的列表"""
    result = []
    for name, agent in AGENT_REGISTRY.items():
        data = _agent_to_dict(agent)
        data["name"] = name
        data["is_custom"] = name not in BUILTIN_AGENTS
        result.append(data)
    return result


def register_agent(
    name: str,
    default_base_url: str,
    default_auth_header: str = "Bearer {api_key}",
    supports_responses: bool = False,
    supports_chat: bool = True,
    supports_messages: bool = False,
) -> None:
    """注册自定义 Agent，持久化到 config.json"""
    global _CUSTOM_AGENTS_DIRTY
    if name in BUILTIN_AGENTS:
        raise ValueError(f"不能覆盖内置供应商: {name}")
    AGENT_REGISTRY[name] = Agent(
        name=name,
        default_base_url=default_base_url,
        default_auth_header=default_auth_header,
        supports_responses=supports_responses,
        supports_chat=supports_chat,
        supports_messages=supports_messages,
    )
    _CUSTOM_AGENTS_DIRTY = True
    _save_custom_agents()


def unregister_agent(name: str) -> None:
    """删除自定义 Agent"""
    if name in BUILTIN_AGENTS:
        raise ValueError(f"不能删除内置供应商: {name}")
    AGENT_REGISTRY.pop(name, None)
    _save_custom_agents()


# ── 持久化 ──
CONFIG_DIR = os.path.expanduser("~/.akm")
CUSTOM_AGENTS_KEY = "custom_agents"


def _agent_to_dict(agent: Agent) -> dict:
    """Agent 转为可序列化的 dict"""
    return {
        "default_base_url": agent.default_base_url,
        "default_auth_header": agent.default_auth_header,
        "supports_responses": agent.supports_responses,
        "supports_chat": agent.supports_chat,
        "supports_messages": agent.supports_messages,
    }


def _agent_from_dict(name: str, data: dict) -> Agent:
    """dict 转为 Agent"""
    return Agent(
        name=name,
        default_base_url=data.get("default_base_url", ""),
        default_auth_header=data.get("default_auth_header", "Bearer {api_key}"),
        supports_responses=data.get("supports_responses", False),
        supports_chat=data.get("supports_chat", True),
        supports_messages=data.get("supports_messages", False),
    )


def _get_config_path() -> str:
    return os.path.join(CONFIG_DIR, "config.json")


def _save_custom_agents() -> None:
    """将自定义 Agent 写入 config.json"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    config = {}
    config_path = _get_config_path()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    custom = {}
    for name, agent in AGENT_REGISTRY.items():
        if name not in BUILTIN_AGENTS:
            custom[name] = _agent_to_dict(agent)

    config[CUSTOM_AGENTS_KEY] = custom
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def load_custom_agents() -> None:
    """启动时从 config.json 加载自定义 Agent 到注册表"""
    config_path = _get_config_path()
    if not os.path.exists(config_path):
        return
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    custom = config.get(CUSTOM_AGENTS_KEY, {})
    for name, data in custom.items():
        if name not in BUILTIN_AGENTS:
            AGENT_REGISTRY[name] = _agent_from_dict(name, data)
