"""/v1/agent 流式 SSE 事件消费。

服务端 ``run_stream`` 把 Agent 事件编码为一行一条 ``data: {json}\\n\\n``。
本模块把 httpx 流式响应解析为 ``(event, data)`` 二元组序列，供交互循环使用。

事件类型（与 akm/agent_runtime/loop.py run_stream 文档一致）::

    - reasoning_delta — LLM 思考片段（灰字渲染）
    - model_delta     — 可见正文片段（实时输出）
    - context_warning — 上下文占用接近上限
    - turn_start      — 新一轮开始（检测到工具调用）
    - tool_call       — 单个工具调用
    - tool_result     — 单个工具执行结果
    - tool_retry      — 工具失败触发的自愈重试
    - final           — Agent 完成（含 final_message / messages / usage / turns）
    - error           — 错误结束（含 error / turns / usage）
"""

from __future__ import annotations

import json
from typing import AsyncGenerator


class SSEConsumer:
    """增量解析 SSE 字节流。

    httpx 的 ``aiter_bytes`` 可能把一条完整事件切到任意字节边界，因此不能
    按行解析，需要自己维护缓冲。每个事件以空行（\\n\\n）分隔，这里逐帧
    喂入文本并产出完整事件。
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> list[dict]:
        """喂入一段文本，返回解析出的完整事件 dict（含 event/data）。"""
        self._buffer += text
        events: list[dict] = []
        while True:
            # 事件分隔符：空行。兼容 \\r\\n 与 \\n 两种换行。
            sep = self._buffer.find("\n\n")
            if sep < 0:
                # 兼容 \\r\\r\\n\\n 的极端情况
                sep = self._buffer.find("\r\r\n\n")
            if sep < 0:
                break
            block = self._buffer[:sep]
            self._buffer = self._buffer[sep + 2:]
            event = self._parse_block(block)
            if event is not None:
                events.append(event)
        return events

    def finish(self) -> list[dict]:
        """流结束时把剩余缓冲作为最后一个事件解析。"""
        if not self._buffer.strip():
            return []
        event = self._parse_block(self._buffer)
        self._buffer = ""
        return [event] if event is not None else []

    @staticmethod
    def _parse_block(block: str) -> dict | None:
        """解析单个 SSE 事件块（可能含多行 data:，或非 data 行）。"""
        data_lines: list[str] = []
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
            # event:/id:/retry: 等行忽略，Agent 事件信息都在 data JSON 里
        if not data_lines:
            return None
        try:
            payload = json.loads("".join(data_lines))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    async def consume(
        self,
        response,
    ) -> AsyncGenerator[tuple[str, dict], None]:
        """消费一个 httpx 流式响应，产出 (event, data) 二元组。

        Args:
            response: 任意提供 ``aiter_bytes()``（逐块产出字节）与
                ``aclose()`` 方法的对象，通常是
                ``httpx.AsyncClient.stream`` 上下文里的响应。

        Yields:
            ``(event_name, data_dict)``，data 为事件负载。
        """
        try:
            async for chunk in response.aiter_bytes():
                for event in self.feed(chunk.decode("utf-8", errors="replace")):
                    name = str(event.get("event") or "")
                    data = event.get("data")
                    if isinstance(data, dict) and name:
                        yield name, data
            for event in self.finish():
                name = str(event.get("event") or "")
                data = event.get("data")
                if isinstance(data, dict) and name:
                    yield name, data
        finally:
            # 确保连接关闭，避免 Ctrl+C 后残留半开连接
            await response.aclose()
