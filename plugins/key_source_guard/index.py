"""按客户端来源限制上游 Key 的项目本地插件。"""

from __future__ import annotations

import fnmatch
import json

from akm.plugins import PluginBase


class Plugin(PluginBase):
    """仅允许配置的客户端 User-Agent 使用指定的上游 Key。"""

    def _bindings(self) -> list[dict]:
        """解析并规整绑定规则，忽略不完整条目以避免意外限制其它 Key。"""
        raw = str((self.config or {}).get("bindings_json", "[]") or "[]")
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.logger.warning("[key_source_guard] bindings_json 不是合法 JSON: %s", exc)
            return []
        if not isinstance(items, list):
            self.logger.warning("[key_source_guard] bindings_json 顶层必须是数组")
            return []

        bindings = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key_alias = str(item.get("key_alias", "") or "").strip()
            patterns = item.get("client_patterns")
            if isinstance(patterns, str):
                patterns = [patterns]
            if not isinstance(patterns, list):
                continue
            normalized_patterns = [str(pattern).strip() for pattern in patterns if str(pattern).strip()]
            if key_alias and normalized_patterns:
                bindings.append({"key_alias": key_alias, "client_patterns": normalized_patterns})
        return bindings

    async def on_key_selected(self, ctx) -> None:
        """来源不符合当前 Key 的绑定规则时跳过它，让代理继续选择其它 Key。"""
        if (self.config or {}).get("enabled", True) is not True:
            return None
        key = ctx.key if isinstance(ctx.key, dict) else {}
        key_alias = str(key.get("alias", "") or "")
        if not key_alias:
            return None

        matching_rules = [item for item in self._bindings() if item["key_alias"] == key_alias]
        if not matching_rules:
            return None

        client = str(ctx.client_user_agent or "")
        allowed = any(
            fnmatch.fnmatchcase(client.lower(), pattern.lower())
            for rule in matching_rules
            for pattern in rule["client_patterns"]
        )
        if allowed:
            return None

        message = str(
            (self.config or {}).get("block_message", "当前客户端来源无权使用此 API Key。")
            or "当前客户端来源无权使用此 API Key。"
        )
        self.logger.warning("[key_source_guard] 拒绝 key=%s client=%s", key_alias, client or "<empty>")
        ctx.set_skip_key(error=message, security_action="key_source_denied")
