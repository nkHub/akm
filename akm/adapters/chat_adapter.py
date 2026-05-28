"""
Chat 格式适配器

源格式为 Chat Completions API，提供到 Responses 和 Messages 的双向转换。
当前暂无使用场景，所有方法留空，等有供应商仅支持 Responses 或 Messages 时补充。
"""

import json
from typing import AsyncIterator
from akm.adapter import BaseAdapter


class ChatAdapter(BaseAdapter):
    """Chat 格式适配器：源格式为 Chat Completions API

    发送方向：convert_request()     — Chat → Responses/Messages
    接收方向：convert_sse_stream()  — Responses SSE/Messages SSE → Chat SSE
    非流式：  convert_response()   — Responses JSON/Messages JSON → Chat JSON
    """

    def convert_request(self, body: dict) -> dict:
        """Chat 请求转换（当前无需求，透传）"""
        return body

    async def convert_sse_stream(
        self, upstream_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[str]:
        """SSE 转换（当前无需求，透传）"""
        async for chunk in upstream_stream:
            if isinstance(chunk, bytes):
                yield chunk.decode("utf-8", errors="replace")
            else:
                yield chunk

    def convert_response(self, body: str) -> str:
        """非流式响应转换（当前无需求，透传）"""
        return body
