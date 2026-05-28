"""协议转换插件 — 合并 Responses / Messages / Chat 三种格式的双向转换

通过 plugin.json 的 converts 列表声明全部转换能力：
- {"from": "responses", "to": "chat"} — Response → Chat 请求 + Chat SSE → Responses SSE
- {"from": "messages",  "to": "chat"} — Messages  → Chat 请求 + Chat SSE → Messages SSE
- {"from": "chat",      "to": "responses"} — Chat → Responses 反向（预留）
- {"from": "chat",      "to": "messages"}  — Chat → Messages  反向（预留）
"""
import importlib.util
import sys
from pathlib import Path
from typing import AsyncIterator
from akm.plugins import PluginBase


class Plugin(PluginBase):
    """协议转换插件

    代理流程：
    1. proxy 通过 PluginManager.get_converter(src, dst) 获取本插件
    2. proxy 调用 convert_request(body) → 自动检测格式 → 转换请求
    3. 发送到上游，收到 Chat SSE
    4. proxy 调用 convert_sse_stream(stream) → 转回源格式 SSE

    source_format 在 convert_request 中设置，供后续 convert_sse_stream 使用。
    """

    async def on_load(self):
        """加载三个适配器模块（使用 importlib 从插件目录动态导入）"""
        plugin_dir = self._static_dir.parent  # views 的父目录即插件根目录
        for name in ("_responses", "_messages", "_chat"):
            py_path = plugin_dir / f"{name}.py"
            if py_path.exists():
                spec = importlib.util.spec_from_file_location(
                    f"akm_plugin_protocol_converter_{name}", str(py_path)
                )
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[f"akm_plugin_protocol_converter_{name}"] = module
                    spec.loader.exec_module(module)
                    setattr(self, name, module)

        # 实例化适配器
        self._responses_adapter = self._responses.ResponsesAdapter() if hasattr(self, "_responses") else None
        self._messages_adapter = self._messages.MessagesAdapter() if hasattr(self, "_messages") else None
        self._chat_adapter = self._chat.ChatAdapter() if hasattr(self, "_chat") else None
        self._source_format: str = ""  # convert_request 时设置

    # ── 请求转换 ──

    def convert_request(self, body: dict) -> dict:
        """请求体格式转换 — 自动检测源格式

        检测规则：
        - body 含 "input" 字段 → Responses 格式
        - body 含 "messages" 字段（顶层列表） → Messages 格式
        - 其他 → Chat 格式（透传）
        """
        if "input" in body:
            self._source_format = "responses"
            return self._responses_adapter.convert_request(body)
        elif "messages" in body and isinstance(body.get("messages"), list):
            self._source_format = "messages"
            return self._messages_adapter.convert_request(body)
        else:
            self._source_format = "chat"
            return self._chat_adapter.convert_request(body)

    # ── 非流式响应转换 ──

    def convert_response(self, body: str) -> str:
        """非流式响应体转换"""
        if self._source_format == "responses":
            return self._responses_adapter.convert_response(body)
        elif self._source_format == "messages":
            return self._messages_adapter.convert_response(body)
        return self._chat_adapter.convert_response(body)

    # ── 流式 SSE 转换 ──

    async def convert_sse_stream(
        self, upstream_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[str]:
        """SSE 流转换 — 按 source_format 选择对应适配器"""
        if self._source_format == "responses":
            async for line in self._responses_adapter.convert_sse_stream(upstream_stream):
                yield line
        elif self._source_format == "messages":
            async for line in self._messages_adapter.convert_sse_stream(upstream_stream):
                yield line
        else:
            async for line in self._chat_adapter.convert_sse_stream(upstream_stream):
                yield line
