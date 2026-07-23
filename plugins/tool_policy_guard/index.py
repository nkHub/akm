"""工具声明与工具调用续接的本地策略插件。"""

from __future__ import annotations

import fnmatch
import json
import re

from akm.plugins import PluginBase


class Plugin(PluginBase):
    """在请求离开 AKM 前检查工具名和已存在的工具调用参数。"""

    def _items(self, raw) -> list[str]:
        """兼容配置表单中的逗号与换行列表。"""
        return [item.strip() for item in str(raw or "").replace("\r", "\n").replace(",", "\n").split("\n") if item.strip()]

    def _settings(self) -> dict:
        """解析可热更新的策略配置，并忽略非法正则。"""
        cfg = self.config or {}
        patterns = []
        for raw in str(cfg.get("deny_argument_patterns", "") or "").replace("\r", "\n").split("\n"):
            value = raw.strip()
            if not value:
                continue
            try:
                patterns.append(re.compile(value))
            except re.error as exc:
                self.logger.warning("[tool_policy_guard] 忽略非法参数正则 %s: %s", value, exc)
        return {
            "enabled": cfg.get("enabled", True) is True,
            "mode": str(cfg.get("mode", "block") or "block").lower(),
            "allow": self._items(cfg.get("allow_tool_names", "")),
            "deny": self._items(cfg.get("deny_tool_names", "")),
            "patterns": patterns,
            "message": str(cfg.get("block_message", "工具调用不符合当前安全策略，已被拒绝。") or "工具调用不符合当前安全策略，已被拒绝。"),
        }

    def _tool_names(self, request: dict) -> list[str]:
        """提取 Chat/Responses 格式中声明的工具名。"""
        names = []
        for tool in request.get("tools", []) if isinstance(request.get("tools"), list) else []:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = str(function.get("name", "") or "")
            if name:
                names.append(name)
        return names

    def _tool_calls(self, request: dict) -> list[tuple[str, str]]:
        """提取客户端续接请求中已出现的 function/tool_use 名称和参数文本。"""
        calls = []
        for message in request.get("messages", []) if isinstance(request.get("messages"), list) else []:
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls", []) if isinstance(message.get("tool_calls"), list) else []:
                function = call.get("function", {}) if isinstance(call, dict) else {}
                if isinstance(function, dict):
                    calls.append((str(function.get("name", "") or ""), str(function.get("arguments", "") or "")))
        for item in request.get("input", []) if isinstance(request.get("input"), list) else []:
            if isinstance(item, dict) and item.get("type") == "function_call":
                calls.append((str(item.get("name", "") or ""), str(item.get("arguments", "") or "")))
        return calls

    def _violation(self, request: dict, cfg: dict) -> str:
        """返回第一条命中原因，空字符串表示请求符合当前策略。"""
        names = [*self._tool_names(request), *(name for name, _ in self._tool_calls(request) if name)]
        for name in names:
            if cfg["allow"] and not any(fnmatch.fnmatchcase(name, pattern) for pattern in cfg["allow"]):
                return f"工具 {name} 不在白名单内"
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in cfg["deny"]):
                return f"工具 {name} 命中黑名单"
        for name, arguments in self._tool_calls(request):
            for pattern in cfg["patterns"]:
                if pattern.search(arguments):
                    return f"工具 {name or '?'} 的参数命中规则 {pattern.pattern}"
        return ""

    async def on_request(self, ctx) -> dict | None:
        """命中 block 时通过 ctx.set_block 阻断请求。"""
        request = ctx.request
        if not isinstance(request, dict):
            return None
        cfg = self._settings()
        if not cfg["enabled"]:
            return None
        reason = self._violation(request, cfg)
        if not reason:
            return None
        if cfg["mode"] == "warn":
            self.logger.warning("[tool_policy_guard] %s", reason)
            return None
        self.logger.warning("[tool_policy_guard] 已阻断: %s", reason)
        return ctx.set_block(
            status_code=400,
            error=cfg["message"],
            security_action="tool_policy_block",
            security_reason=reason,
            body=json.dumps({"error": cfg["message"], "reason": reason}, ensure_ascii=False),
        )
