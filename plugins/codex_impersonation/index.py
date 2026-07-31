"""codex_impersonation — 将指定来源客户端的请求头模拟为 Codex Desktop 风格。

适用场景：某些上游网关（如 Codex 官方 / 需校验官方客户端身份的服务）除了校验
User-Agent 外，还依赖 Codex 专属业务头（originator / x-codex-turn-metadata /
x-openai-internal-codex-responses-lite 等）来判定请求是否来自官方客户端。
开启本插件后，命中来源匹配规则的请求在转发前会被覆写为一套完整的 Codex 风格头，
从而通过这些网关的身份校验。

用法：在管理台「插件」页启用并配置：
  - client_patterns：来源 UA glob 数组（默认 ["opencode/*"]，大小写不敏感）；
  - user_agent：发往上游的模拟 UA（默认 Codex Desktop 版本串）；
  - installation_id：x-codex-turn-metadata 中的固定安装标识，留空则由本插件进程内随机生成并复用；
  - sandbox：x-codex-turn-metadata 中的 sandbox 字段（默认 seatbelt）。

覆写机制：on_request 阶段通过 ctx.set_upstream_headers() 写入
RequestContext.upstream_headers，转发层在 build_headers 之后优先合并这些头
（优先级高于原生透传，低于 build_headers 对认证/传输头的保护）。
"""

import json
import time
import uuid
from fnmatch import fnmatchcase

from akm.plugins import PluginBase

# 默认模拟的 Codex Desktop User-Agent（取自真实 Codex Desktop 0.146.0 抓包样本）。
DEFAULT_CODEX_USER_AGENT = (
    "Codex Desktop/0.146.0-alpha.9.2 (Mac OS 14.1.2; arm64) unknown "
    "(Codex Desktop; 26.727.40816)"
)


class Plugin(PluginBase):
    """Codex Desktop 请求头模拟插件。"""

    def __init__(self) -> None:
        super().__init__()
        self._installation_id: str = ""

    # ── 配置解析 ────────────────────────────────────────────

    def _load_patterns(self) -> list[str]:
        """解析 client_patterns 配置（JSON 数组）；解析失败时返回空列表（即不匹配任何来源）。"""
        raw = self.config.get("client_patterns", '["opencode/*"]')
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except Exception:
            self.logger.warning("client_patterns 不是合法的 JSON 数组: %r", raw)
        return []

    def _matches(self, client_user_agent: str) -> bool:
        """按配置的 UA glob 列表对客户端 User-Agent 做大小写不敏感匹配。"""
        for pattern in self._load_patterns():
            if not pattern:
                continue
            if fnmatchcase((client_user_agent or "").lower(), pattern.lower()):
                return True
        return False

    def _effective_installation_id(self) -> str:
        """返回用于 x-codex-turn-metadata 的 installation_id。

        配置了固定值时直接使用；留空时进程内生成一次并复用，贴近真实 Codex
        安装标识在一次安装生命周期内保持稳定的行为。
        """
        configured = str(self.config.get("installation_id", "") or "").strip()
        if configured:
            return configured
        if not self._installation_id:
            self._installation_id = str(uuid.uuid4())
        return self._installation_id

    # ── Hook ───────────────────────────────────────────────

    async def on_request(self, ctx) -> dict | None:
        """来源匹配时，覆写上游请求头为 Codex Desktop 风格。"""
        if not self.config.get("enabled", True):
            return None
        if not self._matches(ctx.client_user_agent):
            return None

        # 本次请求的会话/线程标识：真实 Codex Desktop 抓包中 session-id / thread-id /
        # x-client-request-id 为同一线程会话下的同一 UUID，x-codex-window-id 为其加 :0 后缀，
        # 仅 x-codex-turn-metadata 中的 turn_id 随每次轮次变化。
        session_id = str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        window_id = f"{session_id}:0"

        turn_metadata = {
            "installation_id": self._effective_installation_id(),
            "session_id": session_id,
            "thread_id": session_id,
            "turn_id": turn_id,
            "window_id": window_id,
            "request_kind": "turn",
            "thread_source": "system",
            "sandbox": str(self.config.get("sandbox", "seatbelt") or "seatbelt"),
            "turn_started_at_unix_ms": int(time.time() * 1000),
        }

        ctx.set_upstream_headers(
            {
                "User-Agent": str(self.config.get("user_agent", "") or DEFAULT_CODEX_USER_AGENT),
                "Originator": "Codex Desktop",
                "Session-Id": session_id,
                "Thread-Id": session_id,
                "X-Client-Request-Id": session_id,
                "X-Codex-Beta-Features": "remote_compaction_v2",
                "X-Codex-Turn-Metadata": json.dumps(turn_metadata, ensure_ascii=False),
                "X-Codex-Window-Id": window_id,
                "X-OpenAI-Internal-Codex-Responses-Lite": "true",
                "Accept": "text/event-stream",
            }
        )
        return None
