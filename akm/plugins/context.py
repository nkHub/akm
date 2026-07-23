"""请求级上下文 — 单次转发生命周期内的共享状态容器。

设计目标：
1. 业务请求体（request）与插件本地状态（bag）分离，避免污染上游 payload；
2. 网关元数据（api_path / client_user_agent）不塞进 body；
3. 同一引用贯穿 on_request → on_key_selected → on_upstream_error → on_response / 流式还原；
4. 并发安全：每次 forward 新建实例，禁止挂在插件 self 上跨请求复用 bag。
"""

from __future__ import annotations

from typing import Any


class RequestContext:
    """单次请求生命周期上下文（引用传递，非 clone）。"""

    def __init__(
        self,
        request: dict | None = None,
        *,
        api_path: str = "",
        client_user_agent: str = "",
    ):
        """创建请求上下文。

        Args:
            request: 业务请求体字典；multipart 等传输字段可仍留在其中，
                     由转发层统一剥离 ``__akm_*`` 前缀键后再发往上游。
            api_path: 客户端入口路径，如 ``chat/completions``。
            client_user_agent: 原始 User-Agent，供策略插件匹配客户端。
        """
        self.request: dict = request if isinstance(request, dict) else {}
        self.response: dict | None = None
        self.api_path: str = str(api_path or "")
        self.client_user_agent: str = str(client_user_agent or "")
        self.model: str = str(self.request.get("model", "") or "")
        self.key: dict | None = None
        # 插件共享袋：约定键名 ``{plugin_name}.{field}``
        self.bag: dict[str, Any] = {}
        # 管道控制结构：block（阻断请求）/ skip_key（跳过当前 Key）
        self.action: dict | None = None

    # ── 请求体 ──────────────────────────────────────────────

    def set_request(self, request: dict) -> None:
        """替换业务请求体引用（插件返回新 dict 时由 run_hook 调用）。"""
        if not isinstance(request, dict):
            return
        self.request = request
        # model 可能被别名映射或 fallback 改写
        if "model" in request:
            self.model = str(request.get("model", "") or self.model)

    def sync_model_from_request(self) -> None:
        """从当前 request 同步 model 字段（in-place 改写后调用）。"""
        if isinstance(self.request, dict):
            self.model = str(self.request.get("model", "") or self.model)

    # ── bag 读写 ────────────────────────────────────────────

    def bag_get(self, key: str, default: Any = None) -> Any:
        """读取插件共享状态。"""
        return self.bag.get(key, default)

    def bag_set(self, key: str, value: Any) -> None:
        """写入插件共享状态。"""
        self.bag[key] = value

    def bag_pop(self, key: str, default: Any = None) -> Any:
        """弹出插件共享状态。"""
        return self.bag.pop(key, default)

    # ── 控制流 ──────────────────────────────────────────────

    def set_block(
        self,
        *,
        status_code: int = 400,
        error: str = "",
        body: str | None = None,
        security_action: str = "block",
        security_reason: str = "",
    ) -> dict:
        """标记 on_request 阻断；proxy 读取后直接返回客户端，不再访问上游。"""
        self.action = {
            "type": "block",
            "status_code": int(status_code or 400),
            "error": str(error or "请求命中安全策略，已被拦截"),
            "body": body,
            "security_action": str(security_action or "block"),
            "security_reason": str(security_reason or ""),
        }
        return self.action

    def set_skip_key(
        self,
        *,
        error: str = "",
        security_action: str = "quota",
    ) -> dict:
        """标记 on_key_selected 跳过当前 Key，让 proxy 继续选下一个。"""
        self.action = {
            "type": "skip_key",
            "error": str(error or "当前可用 Key 均已达到配额上限"),
            "security_action": str(security_action or "quota"),
        }
        return self.action

    def clear_action(self) -> None:
        """清除管道控制标记。"""
        self.action = None

    @property
    def is_block(self) -> bool:
        """是否处于阻断请求状态。"""
        return isinstance(self.action, dict) and self.action.get("type") == "block"

    @property
    def is_skip_key(self) -> bool:
        """是否处于跳过当前 Key 状态。"""
        return isinstance(self.action, dict) and self.action.get("type") == "skip_key"

    def forwardable_request(self) -> dict:
        """生成可发往上游的请求体：剥离所有 ``__akm_*`` 本地字段。"""
        return {
            field: value
            for field, value in (self.request or {}).items()
            if not str(field).startswith("__akm_")
        }
