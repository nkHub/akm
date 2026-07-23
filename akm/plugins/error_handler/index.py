"""错误处理插件 — 接管 proxy.py 的上游错误响应策略

职责：
- 429 限流：标记 key 为 rate_limited，切换下一个 key
- 402/401/403：标记 key 为 disabled，切换下一个 key
- 5xx：指数退避重试（configurable max_retries_per_key）
- 连接/超时：指数退避重试后切换 key

返回规则：
- "retry"  → 同一个 key 再次重试
- "switch" → 换下一个 key
- "block"  → 先禁用/限流 key，再换下一个 key
- None     → 不做任何处理（正常响应）

配置项：
- max_retries_per_key: 单 key 最大重试次数（默认 3）
- max_key_tries: 所有 key 总尝试次数上限（默认 5）

接入方式：proxy.py 在遇到每个错误状态后调用 on_upstream_error hook，
将请求上下文和错误信息传入，根据返回值决定下一步动作。
"""
from akm.plugins import PluginBase


class Plugin(PluginBase):
    """错误处理插件

    可禁用——用户可安装第三方错误处理插件替代内置策略。
    """

    async def on_load(self):
        """初始化配置默认值"""
        self.max_retries = self.config.get("max_retries_per_key", 3)
        self.max_key_tries = self.config.get("max_key_tries", 5)

    async def on_upstream_error(
        self,
        ctx,
        status_code: int = 0,
        error_type: str = "http",
        attempt: int = 0,
        key: dict | None = None,
    ) -> str | None:
        """根据错误类型决定重试策略

        Args:
            ctx: 请求级上下文（可读 ctx.request / ctx.model）
            status_code: HTTP 状态码，连接错误时为 0
            error_type: 错误类型 — "http" / "connect" / "timeout" / "chunk"
            attempt: 当前 key 已重试次数（0-based）
            key: 当前使用的 key 信息字典

        Returns:
            "block"  — 禁用/限流该 key，然后切换下一个
            "switch" — 直接切换下一个 key
            "retry"  — 同一个 key 再次重试（需要 attempt < max_retries_per_key）
            None     — 不做处理
        """
        # 429 限流 → 标记限流，切换 key
        if status_code == 429:
            self.logger.warning(f"429 限流 (key: {key.get('alias') if key else '?'})，切换 key")
            return "block"

        # 402 欠费 / 401 403 认证失败 → 禁用 key，切换
        if status_code in (402, 401, 403):
            self.logger.warning(
                f"{status_code} 认证/付费失败 (key: {key.get('alias') if key else '?'})，禁用并切换 key"
            )
            return "block"

        # 5xx 服务端错误 → 指数退避重试，超过上限后切换
        if 500 <= status_code < 600:
            if attempt < self.max_retries:
                self.logger.info(f"5xx 错误 (attempt {attempt + 1}/{self.max_retries})，重试")
                return "retry"
            self.logger.warning(f"5xx 错误超过重试上限，切换 key")
            return "switch"

        # 连接错误 / 超时 → 指数退避重试，超过上限后切换
        if error_type in ("connect", "timeout", "chunk") and status_code == 0:
            if attempt < self.max_retries:
                self.logger.info(f"{error_type} 错误 (attempt {attempt + 1}/{self.max_retries})，重试")
                return "retry"
            self.logger.warning(f"{error_type} 错误超过重试上限，切换 key")
            return "switch"

        # 未知错误类型 → 切换 key
        self.logger.info(f"未知错误 (status={status_code}, type={error_type})，切换 key")
        return "switch"
