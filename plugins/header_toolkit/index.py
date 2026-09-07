"""客户端请求头变换工具插件（header_toolkit）。

读取客户端原始请求头快照（``ctx.client_headers``，键已归一为小写），
按配置规则做「重命名 / 复制 / 固定值 / 补缺失 / 加前后缀」等变换，
再经 ``ctx.set_upstream_header(...)`` 写入上游请求头。

典型场景（贴近 opencode 等官方客户端直连本地代理）：
- 官方 SDK 会发送 ``x-opencode-session`` 等会话头，供同会话请求路由到同一
  供应商、利于 token 缓存；代理侧通常还需要补一组上游身份/会话头，让上游
  网关识别为原生客户端并保持会话亲和。
- 客户端头的键经内核统一转小写存储，源匹配不区分大小写；``to_header``
  写出的目标头大小写由规则指定，转发层按大小写不敏感合并，可原子覆盖
  既有的仅大小写不同同名头（不会出现重复头同时上送）。
"""

from __future__ import annotations

import json

from akm.plugins import PluginBase


class Plugin(PluginBase):
    """按声明顺序执行客户端头变换规则，并写入上游请求头。"""

    def _rules(self) -> list[dict]:
        """解析规则 JSON；非法配置只跳过本次变换，不影响代理请求。"""
        raw = str((self.config or {}).get("rules_json", "[]") or "[]")
        try:
            rules = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.logger.warning("[header_toolkit] rules_json 不是合法 JSON: %s", exc)
            return []
        if not isinstance(rules, list):
            self.logger.warning("[header_toolkit] rules_json 顶层必须是数组")
            return []
        return [rule for rule in rules if isinstance(rule, dict)]

    @staticmethod
    def _string(value) -> str:
        """规则里允许标量值（数字/布尔/None），统一字符串化。"""
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _source_names(rule: dict) -> list[str]:
        """把 from_header 解析为候选源头名列表（逗号分隔，按声明顺序）。

        每项 trim 后忽略空项；支持 ``"a, b, c"`` 形式。返回空列表表示无源。
        """
        raw = str(rule.get("from_header", "") or "").strip()
        return [part.strip() for part in raw.split(",") if part.strip()]

    def _source_value(self, rule: dict, ctx) -> str:
        """按顺序取第一个在客户端头快照中存在且非空的值。

        候选源头名大小写不敏感（快照键已小写归一）。全部缺失 / 值为空时返回空串，
        由调用方决定 no-op，不回退固定值。
        """
        for name in self._source_names(rule):
            value = ctx.client_headers.get(name.lower())
            if value is not None and value != "":
                return value
        return ""

    def _client_match(self, rule: dict, client: str) -> bool:
        """规则可选按客户端 User-Agent 过滤；未配置 match_client 时对所有客户端生效。"""
        pattern = str(rule.get("match_client", "") or "").strip()
        if not pattern:
            return True
        return pattern.lower() in client.lower()

    def _apply_rename(self, rule: dict, ctx) -> None:
        """rename: 把 from_header（可逗号分隔候选源）的值改名写为 to_header，原目标值被覆盖。

        全部候选源缺失（或值为空串）时整体 no-op（既不写新头也不动原目标头）。
        """
        target = str(rule.get("to_header", "") or "").strip()
        if not target:
            return
        src = self._source_value(rule, ctx)
        if not src:
            return
        if self._client_match(rule, ctx.client_user_agent):
            ctx.set_upstream_header(target, src)

    def _apply_copy(self, rule: dict, ctx) -> None:
        """copy: 把 from_header（可逗号分隔候选源）的值原样写到 to_header；全部源缺失则跳过。"""
        target = str(rule.get("to_header", "") or "").strip()
        if not target:
            return
        src = self._source_value(rule, ctx)
        if not src:
            return
        if self._client_match(rule, ctx.client_user_agent):
            ctx.set_upstream_header(target, src)

    def _apply_set(self, rule: dict, ctx) -> None:
        """set: 无条件把 to_header 设为固定值 value。"""
        value = self._string(rule.get("value"))
        if not value:
            return
        target = str(rule.get("to_header", "") or "").strip()
        if not target:
            return
        if self._client_match(rule, ctx.client_user_agent):
            ctx.set_upstream_header(target, value)

    def _apply_add_if_missing(self, rule: dict, ctx) -> None:
        """add_if_missing: 仅当 to_header 当前不在上游头集合时写入 value。"""
        value = self._string(rule.get("value"))
        if not value:
            return
        target = str(rule.get("to_header", "") or "").strip()
        if not target:
            return
        if not self._client_match(rule, ctx.client_user_agent):
            return
        if any(existing.lower() == target.lower() for existing in ctx.upstream_headers):
            return
        ctx.set_upstream_header(target, value)

    def _apply_prefix_suffix(self, rule: dict, ctx, prefix: bool) -> None:
        """prefix / suffix: 把 from_header（可逗号分隔候选源）的值加工后写到目标头。

        to_header 缺省时取第一个候选源名（对同一头原位加工）；否则写入独立新头。
        目标头在当前规则生效前已有其它规则写入的值时，以该累计值为基础加工
        （保持同一头多规则串行叠加的直观语义）；否则以候选源客户端头为基础。
        全部候选源缺失且无累计值时跳过。
        """
        names = self._source_names(rule)
        if not names:
            return
        target = str(rule.get("to_header", "") or "").strip() or names[0]
        if not target:
            return
        if not self._client_match(rule, ctx.client_user_agent):
            return
        existing = None
        for name in ctx.upstream_headers:
            if name.lower() == target.lower():
                existing = ctx.upstream_headers[name]
                break
        if existing is not None:
            base = existing
        else:
            base = self._source_value(rule, ctx)
            if not base:
                return
        token = self._string(rule.get("prefix") if prefix else rule.get("suffix"))
        if not token:
            return
        ctx.set_upstream_header(target, f"{token}{base}" if prefix else f"{base}{token}")

    async def on_request(self, ctx) -> None:
        """读取客户端原始请求头快照，按规则顺序执行变换并写入上游头。"""
        if (self.config or {}).get("enabled", True) is not True:
            return None
        client_headers = getattr(ctx, "client_headers", None)
        if not isinstance(client_headers, dict) or not client_headers:
            # 内部子请求（agent_runtime / flow）没有客户端头快照：no-op
            return None
        for rule in self._rules():
            action = str(rule.get("action", "") or "").strip()
            try:
                if action == "rename":
                    self._apply_rename(rule, ctx)
                elif action == "copy":
                    self._apply_copy(rule, ctx)
                elif action == "set":
                    self._apply_set(rule, ctx)
                elif action == "add_if_missing":
                    self._apply_add_if_missing(rule, ctx)
                elif action == "prefix":
                    self._apply_prefix_suffix(rule, ctx, prefix=True)
                elif action == "suffix":
                    self._apply_prefix_suffix(rule, ctx, prefix=False)
                else:
                    if action:
                        self.logger.warning(
                            "[header_toolkit] 未知 action=%r，已跳过该规则", action
                        )
            except Exception as exc:  # 单条规则异常不影响后续规则
                self.logger.warning("[header_toolkit] 规则执行异常 %s: %s", action, exc)
        return None
