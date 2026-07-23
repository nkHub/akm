"""插件基类 — 提供上下文注入、生命周期、hook 方法"""
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter

if TYPE_CHECKING:
    from .context import RequestContext
    from .models import PluginMeta


class PluginBase:
    """插件基类，所有插件必须继承此类

    使用方式：
        from akm.plugins import PluginBase

        class Plugin(PluginBase):
            async def on_load(self):
                # 插件初始化逻辑
                pass

            async def on_request(self, ctx):
                # 改写 ctx.request，或 ctx.set_block(...) 阻断
                pass
    """

    name: str = ""               # 由 PluginManager 注入
    builtin: bool = False        # 由 PluginManager 注入
    enabled: bool = True         # 由 PluginManager 注入

    # — 上下文注入（由 PluginManager 调用） —
    app = None                   # FastAPI 实例
    db = None                    # 共享 SQLite 连接
    config: dict = {}            # ~/.akm/config.json 中该插件配置
    logger: logging.Logger = None

    # — 子类覆盖 —
    router = None                # APIRouter（可选）
    meta: "PluginMeta" = None    # 由 PluginManager 注入

    # — 静态资源路径 —
    _static_dir: Path = Path(".")

    # ── 生命周期 ──

    async def on_load(self):
        """插件加载回调（路由注册后调用），可在此建表、初始化资源"""
        pass

    async def on_unload(self):
        """插件卸载回调（应用关闭前调用），可在此清理资源"""
        pass

    # ── Hook 方法（子类按需重写；均接收请求级 RequestContext） ──

    async def on_request(self, ctx: "RequestContext"):
        """请求到达回调。

        - 直接改写 ``ctx.request``（in-place）或返回新的 request dict；
        - 跨阶段状态写入 ``ctx.bag``（约定键 ``{plugin}.{field}``）；
        - 需要阻断时调用 ``ctx.set_block(...)``。
        """
        pass

    async def on_key_selected(self, ctx: "RequestContext"):
        """Key 匹配后回调。

        - 读取 ``ctx.model`` / ``ctx.key`` / ``ctx.request``；
        - 返回替代 key dict，或调用 ``ctx.set_skip_key(...)`` 跳过当前 Key。
        """
        pass

    async def on_upstream_error(
        self,
        ctx: "RequestContext",
        status_code: int = 0,
        error_type: str = "http",
        attempt: int = 0,
        key: dict | None = None,
    ) -> str | None:
        """上游错误回调。返回 ``\"retry\"`` / ``\"switch\"`` / ``\"block\"`` / ``\"fallback\"`` / None"""
        pass

    async def on_response(self, ctx: "RequestContext"):
        """响应返回回调。

        - 读取 ``ctx.request`` / ``ctx.response`` / ``ctx.bag``；
        - 可返回改写后的 response dict（如脱敏还原、安全拦截）。
        """
        pass

    # ── 转换方法（converter 类插件重写） ──

    def convert_request(self, body: dict) -> dict:
        """请求体格式转换"""
        return body

    def convert_response(self, body: str) -> str:
        """非流式响应转换"""
        return body

    async def convert_sse_stream(self, upstream_stream):
        """流式 SSE 转换（异步生成器）"""
        async for chunk in upstream_stream:
            yield chunk
