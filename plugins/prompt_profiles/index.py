"""按模型、接口与客户端匹配的提示词配置集插件。"""

from __future__ import annotations

import fnmatch
import json

from akm.plugins import PluginBase


class Plugin(PluginBase):
    """将所有匹配 profile 的提示词按声明顺序注入请求。"""

    def _profiles(self) -> list[dict]:
        """解析配置 JSON；非法配置只跳过本次注入，不影响代理请求。"""
        raw = str((self.config or {}).get("profiles_json", "[]") or "[]")
        try:
            profiles = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.logger.warning("[prompt_profiles] profiles_json 不是合法 JSON: %s", exc)
            return []
        if not isinstance(profiles, list):
            self.logger.warning("[prompt_profiles] profiles_json 顶层必须是数组")
            return []
        return [item for item in profiles if isinstance(item, dict)]

    def _string_list(self, value) -> list[str]:
        """兼容数组和逗号/换行文本，便于手工编辑 JSON。"""
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in str(value or "").replace("\r", "\n").replace(",", "\n").split("\n") if item.strip()]

    def _matches(self, profile: dict, model: str, api_path: str, client: str) -> bool:
        """匹配模型 glob、API 路径精确项和客户端 UA 子串。空条件表示不限制。"""
        if profile.get("enabled", True) is not True:
            return False
        models = self._string_list(profile.get("models"))
        paths = self._string_list(profile.get("api_paths"))
        clients = self._string_list(profile.get("client_patterns"))
        if models and not any(fnmatch.fnmatchcase(model, pattern) for pattern in models):
            return False
        if paths and api_path not in paths:
            return False
        client_lower = client.lower()
        if clients and not any(pattern.lower() in client_lower for pattern in clients):
            return False
        return bool(str(profile.get("prompt", "") or "").strip())

    def _join(self, original: str, prompt: str, position: str) -> str:
        """用空行分隔提示词，保持已有 system/instructions 内容完整。"""
        if not original:
            return prompt
        return f"{prompt}\n\n{original}" if position == "before" else f"{original}\n\n{prompt}"

    def _inject_messages(self, request: dict, prompt: str, position: str):
        """向 Chat 请求插入 system 消息，并避免改写用户或工具消息。"""
        messages = request.get("messages")
        if not isinstance(messages, list):
            return False
        message = {"role": "system", "content": prompt}
        if position == "before":
            first_non_system = next((index for index, item in enumerate(messages) if not isinstance(item, dict) or item.get("role") != "system"), len(messages))
            messages.insert(first_non_system, message)
        else:
            messages.append(message)
        return True

    def _inject_anthropic_system(self, request: dict, prompt: str, position: str):
        """Anthropic Messages 使用顶层 system 字段，不能塞入 role=system 消息。"""
        current = request.get("system")
        if isinstance(current, str):
            request["system"] = self._join(current, prompt, position)
            return True
        if isinstance(current, list):
            block = {"type": "text", "text": prompt}
            if position == "before":
                current.insert(0, block)
            else:
                current.append(block)
            return True
        request["system"] = prompt
        return True

    def _inject(self, request: dict, api_path: str, prompt: str, position: str) -> bool:
        """按协议把 profile 注入到 Responses、Messages 或 Chat 的正确字段。"""
        if api_path == "responses":
            request["instructions"] = self._join(str(request.get("instructions", "") or ""), prompt, position)
            return True
        if api_path == "messages":
            return self._inject_anthropic_system(request, prompt, position)
        if isinstance(request.get("messages"), list):
            return self._inject_messages(request, prompt, position)
        if "instructions" in request:
            request["instructions"] = self._join(str(request.get("instructions", "") or ""), prompt, position)
            return True
        return False

    async def on_request(self, request) -> dict | None:
        """依次注入所有匹配 profile，并保持未匹配请求原样。"""
        if (self.config or {}).get("enabled", True) is not True or not isinstance(request, dict):
            return None
        model = str(request.get("model", "") or "")
        api_path = str(request.get("__akm_api_path__", "") or "")
        client = str(request.get("__akm_client_user_agent__", "") or "")
        matched_names = []
        for index, profile in enumerate(self._profiles()):
            if not self._matches(profile, model, api_path, client):
                continue
            prompt = str(profile.get("prompt", "") or "").strip()
            position = str(profile.get("position", "before") or "before").strip().lower()
            if position not in ("before", "after"):
                position = "before"
            if self._inject(request, api_path, prompt, position):
                matched_names.append(str(profile.get("name", f"profile-{index + 1}") or f"profile-{index + 1}"))
        if not matched_names:
            return None
        self.logger.info("[prompt_profiles] model=%s api=%s 注入配置集: %s", model, api_path, ", ".join(matched_names))
        return request
