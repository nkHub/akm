"""插件基类 — 提供上下文注入、生命周期、hook 方法"""
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, FastAPI

if TYPE_CHECKING:
    from .context import RequestContext
    from .models import PluginMeta


class NotifyFn(Protocol):
    """宿主提供的系统通知回调约定。"""

    def __call__(self, title: str, subtitle: str = "", message: str = "") -> None:
        """发送一条宿主系统通知。"""


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

    router: APIRouter | None = None  # 子类可覆盖的 APIRouter（可选）

    def __init__(self) -> None:
        """创建彼此隔离的插件运行上下文。

        插件经常在测试、第三方扩展或管理器加载失败后的恢复流程中被直接实例化。
        因此配置和注入上下文必须是实例属性，不能使用可变的类级默认值，避免多个
        插件实例共享配置或残留上一个实例的运行状态。
        """
        self.name: str = ""               # 由 PluginManager 注入
        self.builtin: bool = False         # 由 PluginManager 注入
        self.enabled: bool = True          # 由 PluginManager 注入
        self.app: FastAPI | None = None    # FastAPI 实例
        self.db: Any = None                # 默认不注入连接；插件自行管理短生命周期数据库连接
        self.config: dict = {}             # ~/.akm/config.json 中该插件配置
        self.logger: logging.Logger = logging.getLogger("akm.plugin")
        self.notify: NotifyFn | None = None
        self.meta: "PluginMeta | None" = None
        self._static_dir: Path = Path(".")
        # 只有 on_load 成功完成后才参与 Hook 管道，避免半初始化实例处理请求。
        self.runtime_ready: bool = False

    # ── 生命周期 ──

    async def on_load(self) -> bool | None:
        """插件加载回调（路由注册后调用），可在此建表、初始化资源。

        返回 ``False`` 表示配置或依赖不满足，管理器会保留插件为未就绪状态；
        其他返回值均视为初始化成功，兼容既有未返回值的插件实现。
        """
        pass

    async def on_unload(self):
        """插件卸载回调（应用关闭前调用），可在此清理资源"""
        pass

    


    def on_config_changed(self, old_config: dict, new_config: dict) -> None:
        """插件配置保存后的同步回调。

        管理器在持久化新配置并替换 ``self.config`` 后调用此方法。需要缓存配置值的
        插件可在这里刷新缓存；涉及异步资源重建的配置仍应要求用户重新启用插件。
        """
        pass

    # ── Hook 方法（子类按需重写；均接收请求级 RequestContext） ──

    async def on_request(self, ctx: "RequestContext") -> dict | None:
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

    async def on_response(self, ctx: "RequestContext") -> dict | None:
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
