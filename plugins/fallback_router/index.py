"""失败后的模型降级路由插件。"""

from akm.plugins import PluginBase


class Plugin(PluginBase):
    """命中故障条件后改写 request.model，由 proxy 重新执行 Key 选择。"""

    def _items(self, raw) -> list[str]:
        """兼容管理台文本框中的逗号和换行配置。"""
        return [item.strip() for item in str(raw or "").replace("\r", "\n").replace(",", "\n").split("\n") if item.strip()]

    def _rules(self) -> dict[str, str]:
        """解析 source=>target 规则，忽略不完整或自环配置。"""
        result = {}
        for item in self._items((self.config or {}).get("rules", "")):
            if "=>" not in item:
                continue
            source, target = (part.strip() for part in item.split("=>", 1))
            if source and target and source != target:
                result[source] = target
        return result

    async def on_upstream_error(
        self,
        ctx,
        status_code: int = 0,
        error_type: str = "http",
        attempt: int = 0,
        key: dict | None = None,
    ) -> str | None:
        """在可配置的故障条件下把本次请求切到备用模型。"""
        cfg = self.config or {}
        request = ctx.request
        if cfg.get("enabled", True) is not True or not isinstance(request, dict):
            return None

        status_codes = {int(item) for item in self._items(cfg.get("status_codes", "")) if item.isdigit()}
        error_types = set(self._items(cfg.get("error_types", "")))
        if not ((status_code in status_codes) or (status_code == 0 and error_type in error_types)):
            return None

        model = str(request.get("model", "") or ctx.model or "")
        target = self._rules().get(model)
        history = ctx.bag_get("fallback_router.history")
        if not isinstance(history, list):
            history = []
        max_fallbacks = max(1, int(cfg.get("max_fallbacks", 1) or 1))
        if not target or len(history) >= max_fallbacks or target in history or target == model:
            return None

        request["model"] = target
        ctx.sync_model_from_request()
        ctx.bag_set("fallback_router.history", [*history, model])
        self.logger.warning(
            "[fallback_router] model=%s key=%s status=%s error_type=%s -> %s",
            model,
            (key or {}).get("alias", ""),
            status_code,
            error_type,
            target,
        )
        return "fallback"
