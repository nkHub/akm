"""附加提示词注入插件 — 在请求发送前注入自定义 prompt"""
from akm.plugins import PluginBase


class Plugin(PluginBase):
    """附加提示词注入插件

    在 on_request hook 中读取配置的 prompt_text，
    注入到请求体的 instructions（Responses 格式）或
    messages（Chat 格式）数组中。
    支持「加在前面」或「追加到末尾」两种位置。
    """

    async def on_load(self):
        """初始化时缓存配置"""
        self._cached_text = ""
        self._cached_position = "before"
        self._refresh_config()

    def _refresh_config(self):
        """从 self.config 读取最新配置（支持热更新）"""
        self._cached_text = (self.config or {}).get("prompt_text", "") or ""
        self._cached_position = (self.config or {}).get("position", "before") or "before"

    async def on_request(self, request) -> dict | None:
        """请求预处理：注入附加提示词

        1. 如果 body 中有 instructions 字段（Responses 格式），
           直接将 prompt_text 拼接到 instructions 上。
        2. 如果 body 中有 messages 数组（Chat 格式），
           插入一条 system 角色消息。
        3. 如果 prompt_text 为空，跳过不做任何处理。
        """
        self._refresh_config()

        text = self._cached_text
        if not text:
            return None

        position = self._cached_position

        # ── Responses 格式：注入到 instructions ──
        if "instructions" in request:
            original = request.get("instructions") or ""
            if position == "before":
                request["instructions"] = text + "\n\n" + original if original else text
            else:
                request["instructions"] = original + "\n\n" + text if original else text
            self.logger.info(
                f"[prompt_booster] 已注入提示词到 instructions "
                f"(位置: {position}, 长度: {len(text)} 字符)"
            )
            return request

        # ── Chat 格式：注入为 system 消息 ──
        if "messages" in request and isinstance(request["messages"], list):
            messages = request["messages"]
            if position == "before":
                # 插入到最前面
                messages.insert(0, {"role": "system", "content": text})
            else:
                # 如果最后一条是 system，追加到其 content
                if messages and messages[-1].get("role") == "system":
                    messages[-1]["content"] = (
                        (messages[-1].get("content") or "") + "\n\n" + text
                    )
                else:
                    messages.append({"role": "system", "content": text})
            self.logger.info(
                f"[prompt_booster] 已注入提示词到 messages "
                f"(位置: {position}, 长度: {len(text)} 字符)"
            )
            return request

        return None
