"""协议转换插件 — 合并 Responses / Messages / Chat 三种格式的双向转换

通过 plugin.json 的 converts 列表声明全部转换能力：
- {"from": "responses", "to": "chat"} — Response → Chat 请求 + Chat SSE → Responses SSE
- {"from": "messages",  "to": "chat"} — Messages  → Chat 请求 + Chat SSE → Messages SSE
- {"from": "chat",      "to": "responses"} — Chat → Responses 反向（预留）
- {"from": "chat",      "to": "messages"}  — Chat → Messages  反向（预留）
"""
import importlib.util
import sys
import contextvars
import time
from collections import OrderedDict
from copy import deepcopy
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
        """加载适配器模块（使用 importlib 从插件目录动态导入）

        按依赖顺序加载，并为每个子模块注册 akm.plugins.protocol_converter.{name} 别名，
        保证子模块之间的 from akm.plugins.protocol_converter.xxx import yyy 能透明解析。
        """
        plugin_dir = self._static_dir.parent  # views 的父目录即插件根目录

        def _load_module(name: str):
            py_path = plugin_dir / f"{name}.py"
            if not py_path.exists():
                return None
            spec = importlib.util.spec_from_file_location(
                f"akm_plugin_protocol_converter_{name}", str(py_path)
            )
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"akm_plugin_protocol_converter_{name}"] = module
            sys.modules[f"akm.plugins.protocol_converter.{name}"] = module
            spec.loader.exec_module(module)
            setattr(self, name, module)
            return module

        # 1. 无依赖层
        _load_module("_ir")
        _load_module("_messages_codec")
        _load_module("_warnings")
        # 2. 仅依赖无依赖层
        _load_module("_messages_stream")
        _load_module("_chat")
        # 3. 依赖上层
        _load_module("_responses")
        _load_module("_messages")

        # 默认适配器（兜底）
        self._responses_adapter = self._responses.ResponsesAdapter() if hasattr(self, "_responses") else None
        self._messages_adapter = self._messages.MessagesAdapter() if hasattr(self, "_messages") else None
        self._chat_adapter = self._chat.ChatAdapter() if hasattr(self, "_chat") else None
        self._source_format: str = ""  # 保留兼容

        # 请求级上下文，避免并发请求互相覆盖 source_format/adapter 状态
        self._ctx_source_format = contextvars.ContextVar("protocol_converter_source_format", default="chat")
        self._ctx_active_adapter = contextvars.ContextVar("protocol_converter_active_adapter", default=None)
        self._ctx_request_messages = contextvars.ContextVar("protocol_converter_request_messages", default=[])
        self._ctx_request_provider = contextvars.ContextVar("protocol_converter_request_provider", default="")
        self._response_sessions = OrderedDict()
        self._session_ttl_sec = 60 * 60 * 24
        self._max_sessions = 256

    def _trim_response_sessions(self):
        """清理过期和超量的 Responses 会话缓存，避免长期运行时无限增长。"""
        now = time.time()
        stale_ids = [
            response_id
            for response_id, entry in self._response_sessions.items()
            if now - float(entry.get("updated_at", 0) or 0) > self._session_ttl_sec
        ]
        for response_id in stale_ids:
            self._response_sessions.pop(response_id, None)
        while len(self._response_sessions) > self._max_sessions:
            self._response_sessions.popitem(last=False)

    def _restore_response_history(self, body: dict) -> dict:
        """按 previous_response_id 恢复 Chat 历史，再交给 ResponsesAdapter 转换。"""
        previous_response_id = body.get("previous_response_id")
        if not previous_response_id:
            return body
        self._trim_response_sessions()
        entry = self._response_sessions.get(str(previous_response_id))
        if not entry:
            return body
        restored = dict(body)
        current_input = restored.get("input", [])
        if current_input is None:
            current_items = []
        elif isinstance(current_input, list):
            current_items = list(current_input)
        else:
            current_items = [current_input]
        restored["input"] = deepcopy(entry.get("messages", [])) + current_items
        self._response_sessions.move_to_end(str(previous_response_id))
        return restored

    def _store_response_session(self, adapter, response_body: str = ""):
        """记录 response_id 对应的 Chat 历史，用于后续 previous_response_id 续接。"""
        response_id = getattr(adapter, "_session_response_id", "") or ""
        assistant_message = getattr(adapter, "_session_assistant_message", None)
        if not response_id or not isinstance(assistant_message, dict):
            return
        request_messages = self._ctx_request_messages.get() or []
        messages = deepcopy(request_messages)
        messages.append(deepcopy(assistant_message))
        self._response_sessions[str(response_id)] = {
            "updated_at": time.time(),
            "messages": messages,
        }
        self._response_sessions.move_to_end(str(response_id))
        self._trim_response_sessions()

    def _new_adapter(self, source_format: str):
        """按源格式创建请求级 adapter 实例（隔离可变状态）"""
        if source_format == "responses" and hasattr(self, "_responses"):
            return self._responses.ResponsesAdapter()
        if source_format == "messages" and hasattr(self, "_messages"):
            return self._messages.MessagesAdapter()
        if hasattr(self, "_chat"):
            return self._chat.ChatAdapter()
        return None

    def set_request_context(self, **kwargs):
        """设置一次请求的额外上下文，供 adapter 做供应商相关兼容判断。"""
        provider = kwargs.get("provider")
        if provider is not None:
            self._ctx_request_provider.set(str(provider or ""))

        adapter = self._ctx_active_adapter.get()
        if adapter is not None and provider is not None:
            setattr(adapter, "_request_provider", str(provider or ""))

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
            adapter = self._new_adapter("responses") or self._responses_adapter
            self._fallback_thinking_to_text = False
            self._tool_trace_events = []
            self._conversion_warnings = []
            self._ctx_source_format.set("responses")
            self._ctx_active_adapter.set(adapter)
            setattr(adapter, "_request_provider", self._ctx_request_provider.get() or "")
            restored_body = self._restore_response_history(body)
            converted = adapter.convert_request(restored_body)
            converted.pop("previous_response_id", None)
            self._ctx_request_messages.set(deepcopy(converted.get("messages", [])))
            return converted
        elif "messages" in body and isinstance(body.get("messages"), list):
            self._source_format = "messages"
            adapter = self._new_adapter("messages") or self._messages_adapter
            self._fallback_thinking_to_text = False
            self._tool_trace_events = []
            self._conversion_warnings = []
            self._ctx_source_format.set("messages")
            self._ctx_active_adapter.set(adapter)
            setattr(adapter, "_request_provider", self._ctx_request_provider.get() or "")
            self._ctx_request_messages.set([])
            return adapter.convert_request(body)
        else:
            self._source_format = "chat"
            adapter = self._new_adapter("chat") or self._chat_adapter
            self._fallback_thinking_to_text = False
            self._tool_trace_events = []
            self._conversion_warnings = []
            self._ctx_source_format.set("chat")
            self._ctx_active_adapter.set(adapter)
            setattr(adapter, "_request_provider", self._ctx_request_provider.get() or "")
            self._ctx_request_messages.set([])
            return adapter.convert_request(body)

    # ── 非流式响应转换 ──

    def convert_response(self, body: str) -> str:
        """非流式响应体转换"""
        source_format = self._ctx_source_format.get() or self._source_format
        adapter = self._ctx_active_adapter.get()
        if source_format == "responses":
            a = adapter or self._responses_adapter
            result = a.convert_response(body)
            self._conversion_warnings = getattr(a, "_conversion_warnings", [])
            self._store_response_session(a, result)
            return result
        elif source_format == "messages":
            a = adapter or self._messages_adapter
            self._conversion_warnings = getattr(a, "_conversion_warnings", [])
            return a.convert_response(body)
        a = adapter or self._chat_adapter
        self._conversion_warnings = getattr(a, "_conversion_warnings", [])
        return a.convert_response(body)

    # ── 流式 SSE 转换 ──

    async def convert_sse_stream(
        self, upstream_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[str]:
        """SSE 流转换 — 按 source_format 选择对应适配器"""
        source_format = self._ctx_source_format.get() or self._source_format
        adapter = self._ctx_active_adapter.get()
        if source_format == "responses":
            a = adapter or self._responses_adapter
            async for line in a.convert_sse_stream(upstream_stream):
                self._fallback_thinking_to_text = getattr(a, "_fallback_thinking_to_text", False)
                self._tool_trace_events = getattr(a, "_tool_trace_events", [])
                self._conversion_warnings = getattr(a, "_conversion_warnings", [])
                yield line
            self._store_response_session(a)
        elif source_format == "messages":
            a = adapter or self._messages_adapter
            async for line in a.convert_sse_stream(upstream_stream):
                self._fallback_thinking_to_text = getattr(a, "_fallback_thinking_to_text", False)
                self._tool_trace_events = getattr(a, "_tool_trace_events", [])
                self._conversion_warnings = getattr(a, "_conversion_warnings", [])
                yield line
        else:
            a = adapter or self._chat_adapter
            async for line in a.convert_sse_stream(upstream_stream):
                self._fallback_thinking_to_text = getattr(a, "_fallback_thinking_to_text", False)
                self._tool_trace_events = getattr(a, "_tool_trace_events", [])
                self._conversion_warnings = getattr(a, "_conversion_warnings", [])
                yield line
