"""协议转换适配器基类"""

from abc import ABC, abstractmethod
from typing import AsyncIterator


class BaseAdapter(ABC):
    """协议转换适配器基类

    子类需实现三种转换方法：
    - convert_request: 请求体格式转换
    - convert_response: 非流式响应体转换
    - convert_sse_stream: 流式 SSE 响应逐行转换
    """

    @abstractmethod
    def convert_request(self, body: dict) -> dict:
        """转换请求体，返回目标协议的请求 dict"""
        ...

    @abstractmethod
    def convert_response(self, body: str) -> str:
        """转换非流式响应体，返回目标协议的响应字符串"""
        ...

    @abstractmethod
    async def convert_sse_stream(
        self, upstream_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[str]:
        """将上游 SSE 字节流逐行转换为目标协议 SSE 文本流"""
        ...
        yield ""  # pragma: no cover — 占位，子类覆盖
