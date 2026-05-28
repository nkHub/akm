"""模型匹配插件 — 接管 key_pool.py 的核心匹配逻辑

职责：
1. on_request: 模型别名映射（将请求中的 model 字段按别名表替换）
2. on_key_selected: key 被匹配后二次检查/替换（如自定义路由策略）

配置项：
- aliases: 逗号分隔的模型别名映射，如 "gpt-4=gpt-4-turbo,claude-3=claude-3-opus"
"""
from akm.plugins import PluginBase


class Plugin(PluginBase):
    """模型匹配插件

    required: true — 不可禁用，保证至少一个模型匹配插件始终生效。
    用户可安装第三方模型匹配插件替代内置逻辑，但不可完全禁用匹配。
    """

    async def on_load(self):
        """初始化时解析别名映射表"""
        self._aliases: dict[str, str] = {}
        self._parse_aliases()

    def _parse_aliases(self):
        """从配置中解析模型别名映射"""
        self._aliases.clear()
        raw = (self.config or {}).get("aliases", "")
        if not raw or not isinstance(raw, str):
            return
        for pair in raw.split(","):
            pair = pair.strip()
            if "=" in pair:
                old, new = pair.split("=", 1)
                old = old.strip()
                new = new.strip()
                if old and new:
                    self._aliases[old] = new
        if self._aliases:
            self.logger.info(f"[model_matcher] 加载别名表: {self._aliases}")

    async def on_request(self, request) -> dict | None:
        """请求预处理：模型别名映射

        将请求 body 中的 model 字段按 aliases 配置替换。
        如用户配置 gpt-4=gpt-4-turbo，则请求 model=gpt-4 时自动改为 gpt-4-turbo。
        """
        # 重新解析别名（热更新，无需重启）
        self._parse_aliases()

        if not self._aliases:
            return None

        model = request.get("model", "")
        if model in self._aliases:
            new_model = self._aliases[model]
            request["model"] = new_model
            self.logger.info(f"[model_matcher] 模型别名映射: {model} → {new_model}")

        return request

    async def on_key_selected(self, model: str, key: dict, request) -> dict | None:
        """Key 选择后回调：可在此实现自定义路由策略

        默认行为：返回 None（不修改选中的 key）
        可重写为：根据请求内容（如 model、用户标识）返回替换的 key
        """
        return None
