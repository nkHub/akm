"""模型匹配插件 — 接管 key_pool.py 的核心匹配逻辑

职责：
1. on_request: 模型别名映射（将请求中的 model 字段按别名表替换）
2. on_key_selected: key 被匹配后二次检查/替换（如自定义路由策略）

配置项：
- aliases: 逗号分隔的模型别名映射，如 "gpt-4=gpt-4-turbo,claude-3=claude-3-opus"
"""
from akm.plugins import PluginBase
from akm.key_pool import pick_key_async
import time


class Plugin(PluginBase):
    """模型匹配插件

    required: true — 不可禁用，保证至少一个模型匹配插件始终生效。
    用户可安装第三方模型匹配插件替代内置逻辑，但不可完全禁用匹配。
    """

    async def on_load(self):
        """初始化时解析别名映射表"""
        self._aliases: dict[str, str] = {}
        # 记录每个 key 的并发请求数与最早开始时间，用于并发/慢 key 旁路
        self._inflight_counts: dict[str, int] = {}
        self._inflight_oldest_ts: dict[str, float] = {}
        # 记录每个 key 的健康度滑动统计，用于“更智能”的旁路打分。
        # 字段说明：
        # - ema_latency_ms: 指数滑动平均延迟（毫秒），越低越好
        # - ema_error: 指数滑动平均错误率（0~1），越低越好
        # - last_error_ts: 最近一次错误时间戳（用于短时冷却惩罚）
        self._health_stats: dict[str, dict] = {}
        self._parse_aliases()

    def _get_cfg(self) -> dict:
        """统一读取插件配置，便于后续策略函数复用。"""
        return self.config or {}

    def _update_health_stats(self, alias: str, response: dict):
        """根据 on_response 元信息更新 key 健康统计。

        设计说明：
        1. 仅在 response 含 key_alias 且可判定成功/失败时更新。
        2. 使用 EMA 平滑瞬时抖动，避免因为单次毛刺造成频繁切换。
        3. 错误事件会刷新 last_error_ts，供短时冷却惩罚使用。
        """
        if not alias or not isinstance(response, dict):
            return

        ok = bool(response.get("ok", False))
        latency_ms = response.get("latency_ms", 0)
        try:
            latency_ms = float(latency_ms)
        except Exception:
            latency_ms = 0.0

        stat = self._health_stats.get(alias) or {
            "ema_latency_ms": 0.0,
            "ema_error": 0.0,
            "last_error_ts": 0.0,
        }

        # EMA 系数：0.2 偏保守，既能跟踪变化又不过度敏感。
        alpha = 0.2
        prev_latency = float(stat.get("ema_latency_ms", 0.0) or 0.0)
        if latency_ms > 0:
            stat["ema_latency_ms"] = latency_ms if prev_latency <= 0 else (1 - alpha) * prev_latency + alpha * latency_ms

        err = 0.0 if ok else 1.0
        prev_err = float(stat.get("ema_error", 0.0) or 0.0)
        stat["ema_error"] = (1 - alpha) * prev_err + alpha * err
        if err > 0:
            stat["last_error_ts"] = time.time()

        self._health_stats[alias] = stat

    def _calc_key_score(self, alias: str, now_ts: float) -> float:
        """计算 key 的路由分数（越低越好）。

        分数组成（全部是加法惩罚项）：
        - 并发惩罚：in-flight 数越高，分数越高
        - 慢请求惩罚：最老请求时长越大，分数越高
        - 延迟惩罚：EMA 延迟按百毫秒归一化
        - 错误惩罚：EMA 错误率权重较高
        - 冷却惩罚：最近出现错误时，短窗口内附加固定惩罚
        """
        cfg = self._get_cfg()
        slow_threshold_sec = float(cfg.get("slow_inflight_threshold_sec", 8) or 8)
        max_inflight = max(1, int(cfg.get("max_inflight_per_key", 3) or 3))
        cooldown_sec = float(cfg.get("smart_bypass_error_cooldown_sec", 15) or 15)

        inflight = float(self._inflight_counts.get(alias, 0) or 0)
        oldest_ts = self._inflight_oldest_ts.get(alias)
        oldest_age = max(0.0, now_ts - float(oldest_ts)) if oldest_ts else 0.0

        stat = self._health_stats.get(alias) or {}
        ema_latency = float(stat.get("ema_latency_ms", 0.0) or 0.0)
        ema_error = float(stat.get("ema_error", 0.0) or 0.0)
        last_error_ts = float(stat.get("last_error_ts", 0.0) or 0.0)

        inflight_penalty = inflight / max_inflight
        slow_penalty = (oldest_age / slow_threshold_sec) if slow_threshold_sec > 0 else 0.0
        latency_penalty = ema_latency / 100.0
        error_penalty = ema_error * 5.0

        cooldown_penalty = 0.0
        if last_error_ts > 0 and (now_ts - last_error_ts) < cooldown_sec:
            cooldown_penalty = 1.0

        return inflight_penalty + slow_penalty + latency_penalty + error_penalty + cooldown_penalty

    async def _pick_better_alternate_key(self, model: str, current_alias: str) -> dict | None:
        """在候选 key 集合中选择比当前 key 更优的替代项。

        说明：
        - 只在插件内部完成决策，尽量不改 proxy 主逻辑。
        - 候选通过多次调用 pick_key_async 获取（逐步扩大 exclude）。
        - 若替代 key 改善幅度不足，则保持当前 key，避免无意义抖动。
        """
        cfg = self._get_cfg()
        candidate_pool = max(1, int(cfg.get("smart_bypass_candidate_pool", 4) or 4))
        min_improve = float(cfg.get("smart_bypass_min_improve", 0.15) or 0.15)

        now_ts = time.time()
        current_score = self._calc_key_score(current_alias, now_ts)
        exclude = [current_alias]
        best_key = None
        best_score = None

        for _ in range(candidate_pool):
            cand = await pick_key_async(model, exclude)
            if not isinstance(cand, dict):
                break
            alias = str(cand.get("alias", ""))
            if not alias:
                break
            exclude.append(alias)

            score = self._calc_key_score(alias, now_ts)
            if best_score is None or score < best_score:
                best_score = score
                best_key = cand

        if best_key is None or best_score is None:
            return None

        # 需要有明显改善才切换，防止频繁横跳。
        if (current_score - best_score) < min_improve:
            return None
        return best_key

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

        changed = False

        model = request.get("model", "")
        new_model = ""
        if self._aliases and model in self._aliases:
            new_model = self._aliases[model]
        if new_model:
            request["model"] = new_model
            changed = True
            self.logger.info(f"[model_matcher] 模型别名映射: {model} → {new_model}")

        # 工具调用策略（可配置）：
        # 对 GPT/Codex 模型且携带 tools 的请求，在未显式传 tool_choice 时默认强制 required。
        # 该策略从 protocol_converter 下沉到 matcher 层，减少协议层与模型策略耦合。
        cfg = self.config or {}
        force_required = cfg.get("force_tool_choice_required_for_gpt", False)
        if force_required:
            req_model = str(request.get("model", ""))
            tools = request.get("tools")
            if (
                isinstance(tools, list)
                and tools
                and "tool_choice" not in request
                and (req_model.startswith("gpt-") or "codex" in req_model)
                and self._is_tool_task_intent(request)
            ):
                request["tool_choice"] = "required"
                changed = True
                self.logger.info("[model_matcher] 自动设置 tool_choice=required (gpt/codex + tools)")

        return request if changed else None

    def _is_tool_task_intent(self, request: dict) -> bool:
        """判断请求是否属于“明确需要工具执行”的任务意图。

        设计目标：避免把普通闲聊（如“你好”）误判成必须调用工具，导致模型进入工具循环。
        仅在用户表达了“执行命令/改代码/读写文件/运行测试”等操作性意图时，才允许强制 required。
        """
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages:
            return False

        # 找最后一条 user 消息，尽量贴近用户当前意图
        last_user_text = ""
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            if m.get("role") != "user":
                continue
            content = m.get("content", "")
            if isinstance(content, str):
                last_user_text = content
            elif isinstance(content, list):
                parts = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        parts.append(c.get("text", ""))
                last_user_text = "\n".join(parts)
            break

        text = (last_user_text or "").strip().lower()
        if not text:
            return False

        # 明确排除常见闲聊/问候，防止误触发
        small_talk = (
            "你好", "hello", "hi", "hey", "在吗", "在么", "早上好", "晚上好",
            "谢谢", "thank you", "how are you", "你是谁",
        )
        if any(k in text for k in small_talk):
            return False

        # 操作性任务关键词：命令执行、代码修改、文件操作、测试构建、git 等
        tool_intent_keywords = (
            "run", "execute", "command", "bash", "shell", "terminal", "script",
            "test", "pytest", "build", "lint", "compile",
            "edit", "modify", "refactor", "fix", "patch", "apply",
            "file", "read", "write", "open", "search", "grep", "diff",
            "git", "commit", "branch", "log", "status",
            "运行", "执行", "命令", "终端", "脚本", "测试", "构建", "编译",
            "修改", "重构", "修复", "补丁", "文件", "读取", "写入", "搜索",
            "提交", "分支", "日志", "状态",
        )
        return any(k in text for k in tool_intent_keywords)

    async def on_key_selected(self, model: str, key: dict, request) -> dict | None:
        """Key 选择后回调：可在此实现自定义路由策略

        默认行为：返回 None（不修改选中的 key）
        可重写为：根据请求内容（如 model、用户标识）返回替换的 key
        """
        cfg = self.config or {}
        enable_bypass = bool(cfg.get("enable_inflight_bypass", False))
        enable_smart_bypass = bool(cfg.get("enable_smart_bypass", False))
        max_inflight = int(cfg.get("max_inflight_per_key", 3))
        slow_threshold_sec = float(cfg.get("slow_inflight_threshold_sec", 8))

        current_alias = str(key.get("alias", ""))
        now = time.time()
        current_count = int(self._inflight_counts.get(current_alias, 0))
        oldest_ts = self._inflight_oldest_ts.get(current_alias)
        oldest_age = (now - oldest_ts) if oldest_ts else 0.0

        should_bypass = (
            enable_bypass
            and current_alias
            and (
                (max_inflight > 0 and current_count >= max_inflight)
                or (slow_threshold_sec > 0 and oldest_age >= slow_threshold_sec)
            )
        )

        if should_bypass:
            # 智能旁路（默认关闭）：在多个候选中做健康打分，择优切换。
            # 关闭时维持原有行为，仅尝试下一个可用 key。
            if enable_smart_bypass:
                alt = await self._pick_better_alternate_key(model, current_alias)
            else:
                alt = await pick_key_async(model, [current_alias])
            if isinstance(alt, dict) and alt.get("alias") and alt.get("alias") != current_alias:
                if enable_smart_bypass:
                    now_ts = time.time()
                    src_score = self._calc_key_score(current_alias, now_ts)
                    dst_score = self._calc_key_score(str(alt.get("alias", "")), now_ts)
                    self.logger.info(
                        "[model_matcher] 智能旁路 key: %s(score=%.3f) -> %s(score=%.3f)",
                        current_alias,
                        src_score,
                        alt.get("alias"),
                        dst_score,
                    )
                self.logger.info(
                    "[model_matcher] 旁路拥塞 key: %s(count=%s, oldest=%.2fs) -> %s",
                    current_alias,
                    current_count,
                    oldest_age,
                    alt.get("alias"),
                )
                key = alt
                current_alias = str(key.get("alias", ""))

        # 将最终选择的 key 记为 in-flight；由 on_response 生命周期回收
        if current_alias:
            self._inflight_counts[current_alias] = int(self._inflight_counts.get(current_alias, 0)) + 1
            self._inflight_oldest_ts.setdefault(current_alias, now)

        return key

    async def on_response(self, request, response) -> None:
        """请求完成生命周期回调：回收 in-flight 计数，避免并发统计累积失真。"""
        if not isinstance(response, dict):
            return
        alias = str(response.get("key_alias", "") or "")
        if not alias:
            return

        # 先更新健康统计，再回收并发计数。
        self._update_health_stats(alias, response)

        count = int(self._inflight_counts.get(alias, 0))
        if count <= 1:
            self._inflight_counts.pop(alias, None)
            self._inflight_oldest_ts.pop(alias, None)
            return
        self._inflight_counts[alias] = count - 1
        if alias not in self._inflight_oldest_ts:
            self._inflight_oldest_ts[alias] = time.time()
