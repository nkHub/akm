"""预算闸门插件。

在进程内按全局 / 模型 / 用户维度累计「估算费用」，超过预算后在
on_request 阶段直接 block，避免继续打上游。

费用口径复用核心 ``akm.cost_estimate``（与首页费用统计同源），
只能基于上游响应中可解析的 usage 做估算，**不能替代供应商账单**。
服务重启后内存计数清零。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter

from akm.cost_estimate import estimate_row_cost, parse_pricing
from akm.plugins import PluginBase


router = APIRouter()
_plugin_instance: "Plugin | None" = None


def _plugin() -> "Plugin":
    """取得由 PluginManager 注入上下文后的唯一插件实例。"""
    if _plugin_instance is None or not _plugin_instance.enabled:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="budget_gate 插件未启用")
    return _plugin_instance


@router.get("/status")
async def status():
    """返回当前预算周期内的累计估算与配置快照（不含请求正文）。"""
    return _plugin().status()


@router.post("/reset")
async def reset():
    """清空进程内费用桶；仅影响本插件统计，不影响审计日志。"""
    return _plugin().reset()


class Plugin(PluginBase):
    """请求前检查预算，响应后按 usage 累计估算费用。"""

    router = router

    async def on_load(self):
        """初始化进程内费用桶，并登记全局实例供路由复用。"""
        global _plugin_instance
        _plugin_instance = self
        # (scope_key, period_id) -> {"spent": float, "requests": int, "tokens": int}
        self._buckets: dict[tuple[str, str], dict[str, float | int]] = {}
        # 软告警去重：同一 (scope, period) 每个周期最多告警一次
        self._soft_warned: set[tuple[str, str]] = set()

    async def on_unload(self):
        """卸载时摘掉路由实例引用。"""
        global _plugin_instance
        if _plugin_instance is self:
            _plugin_instance = None

    def _settings(self) -> dict:
        """热读配置；非法枚举回落到安全默认值。"""
        cfg = self.config or {}
        scope = str(cfg.get("scope", "global") or "global").strip().lower()
        if scope not in ("global", "model", "user"):
            scope = "global"
        period = str(cfg.get("period", "calendar_day") or "calendar_day").strip().lower()
        if period not in ("calendar_day", "rolling_window"):
            period = "calendar_day"
        try:
            budget = float(cfg.get("budget_usd", 5) or 0)
        except (TypeError, ValueError):
            budget = 0.0
        try:
            soft = float(cfg.get("soft_warn_ratio", 0.8) or 0)
        except (TypeError, ValueError):
            soft = 0.0
        status_code = int(cfg.get("block_status_code", 429) or 429)
        status_code = min(599, max(400, status_code))
        return {
            "enabled": cfg.get("enabled", True) is True,
            "scope": scope,
            "period": period,
            "window_seconds": max(60, int(cfg.get("window_seconds", 86400) or 86400)),
            "budget_usd": max(0.0, budget),
            "soft_warn_ratio": min(1.0, max(0.0, soft)),
            "use_core_pricing": cfg.get("use_core_pricing", True) is True,
            "custom_pricing_table": str(cfg.get("custom_pricing_table", "") or ""),
            "block_message": str(
                cfg.get("block_message", "本地预算已用尽，请求已被预算闸门拦截。")
                or "本地预算已用尽，请求已被预算闸门拦截。"
            ),
            "block_status_code": status_code,
        }

    def _period_id(self, period: str, window_seconds: int) -> str:
        """生成当前预算周期标识，用于分桶与重置。"""
        if period == "rolling_window":
            return f"rw:{int(time.time() // window_seconds)}"
        # 自然日使用本地时区日期，方便与「每日预算」心智对齐
        return f"day:{datetime.now().astimezone().date().isoformat()}"

    def _scope_key(self, request: dict, scope: str) -> str:
        """按配置维度构造预算键。"""
        if scope == "model":
            return f"model:{request.get('model', '') or ''}"
        if scope == "user":
            user = request.get("user")
            if user is None or user == "":
                return "user:anonymous"
            return f"user:{user}"
        return "global"

    def _bucket(self, scope_key: str, period_id: str) -> dict[str, float | int]:
        """取得当前周期桶；同 scope 仅保留当前 period，避免内存无限增长。"""
        rebuilt: dict[tuple[str, str], dict[str, float | int]] = {}
        for key, value in self._buckets.items():
            # 其它 scope 全留；当前 scope 只留本 period
            if key[0] != scope_key or key[1] == period_id:
                rebuilt[key] = value
        self._buckets = rebuilt
        return self._buckets.setdefault(
            (scope_key, period_id),
            {"spent": 0.0, "requests": 0, "tokens": 0},
        )

    def _pricing_rules(self, cfg: dict) -> list[tuple[str, float, float, float]]:
        """解析单价规则：优先核心配置，否则插件自定义表。"""
        raw = ""
        if cfg["use_core_pricing"]:
            try:
                from akm.config import load_config

                raw = str(load_config().get("cost_pricing_table") or "")
            except Exception as exc:
                self.logger.warning("[budget_gate] 读取核心单价表失败: %s", exc)
        if not raw.strip():
            raw = cfg["custom_pricing_table"]
        return parse_pricing(raw)

    def _extract_usage_metrics(self, response_body: str) -> dict[str, int] | None:
        """从非流式 JSON 或 SSE 审计捕获中提取统一 token 字段。

        口径对齐审计侧：prompt / completion / cached / cache_creation。
        取所有候选 usage 中 total_tokens 最大的一条，避免中间 chunk 误用。
        """
        best: dict[str, int] | None = None
        best_total = -1

        def consider(usage: Any) -> None:
            nonlocal best, best_total
            if not isinstance(usage, dict):
                return
            has_signal = any(
                key in usage
                for key in (
                    "total_tokens",
                    "prompt_tokens",
                    "completion_tokens",
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            )
            if not has_signal:
                return
            try:
                completion = int(
                    usage.get("completion_tokens", 0)
                    or usage.get("output_tokens", 0)
                    or 0
                )
                cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
                cached = int(usage.get("cached_tokens", 0) or 0)
                if not cached:
                    cached = int(usage.get("cache_read_input_tokens", 0) or 0)
                if not cached:
                    for detail_key in ("prompt_tokens_details", "input_tokens_details"):
                        details = usage.get(detail_key)
                        if isinstance(details, dict):
                            cached = int(details.get("cached_tokens", 0) or 0)
                            if cached:
                                break
                prompt = int(usage.get("prompt_tokens", 0) or 0)
                if not prompt:
                    prompt = int(usage.get("input_tokens", 0) or 0)
                    if "cache_read_input_tokens" in usage and cached > 0:
                        prompt += cached
                total = int(usage.get("total_tokens", 0) or 0)
                if total <= 0:
                    total = prompt + completion
            except (TypeError, ValueError):
                return
            if total < best_total:
                return
            best_total = total
            best = {
                "prompt_tokens": max(0, prompt),
                "completion_tokens": max(0, completion),
                "cached_tokens": max(0, cached),
                "cache_creation_tokens": max(0, cache_creation),
                "total_tokens": max(0, total),
            }

        text = str(response_body or "")
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            consider(payload.get("usage"))
            response_obj = payload.get("response")
            if isinstance(response_obj, dict):
                consider(response_obj.get("usage"))
            message_obj = payload.get("message")
            if isinstance(message_obj, dict):
                consider(message_obj.get("usage"))

        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            consider(chunk.get("usage"))
            response_obj = chunk.get("response")
            if isinstance(response_obj, dict):
                consider(response_obj.get("usage"))
            message_obj = chunk.get("message")
            if isinstance(message_obj, dict):
                consider(message_obj.get("usage"))

        return best

    def _block(self, ctx, cfg: dict, scope_key: str, spent: float, budget: float) -> dict:
        """构造超预算阻断结构。"""
        reason = f"{scope_key} spent=${spent:.4f} budget=${budget:.4f}"
        body = json.dumps(
            {
                "error": {
                    "message": cfg["block_message"],
                    "type": "budget_exceeded",
                    "code": "budget_gate",
                    "param": reason,
                    "spent_usd": round(spent, 6),
                    "budget_usd": budget,
                }
            },
            ensure_ascii=False,
        )
        return ctx.set_block(
            status_code=cfg["block_status_code"],
            error=cfg["block_message"],
            body=body,
            security_action="budget_exceeded",
            security_reason=reason,
        )

    async def on_request(self, ctx) -> None:
        """预算已用尽则阻断；接近上限时软告警一次。"""
        request = ctx.request
        if not isinstance(request, dict):
            return
        cfg = self._settings()
        if not cfg["enabled"]:
            return

        scope_key = self._scope_key(request, cfg["scope"])
        period_id = self._period_id(cfg["period"], cfg["window_seconds"])
        bucket = self._bucket(scope_key, period_id)
        spent = float(bucket.get("spent", 0.0) or 0.0)
        budget = float(cfg["budget_usd"] or 0.0)

        # 把当前周期信息记入 bag，供 on_response 与审计对齐同一分桶
        ctx.bag_set("budget_gate.scope_key", scope_key)
        ctx.bag_set("budget_gate.period_id", period_id)

        if budget > 0 and spent >= budget:
            self.logger.warning(
                "[budget_gate] 超预算阻断: %s spent=%.6f budget=%.6f",
                scope_key,
                spent,
                budget,
            )
            self._block(ctx, cfg, scope_key, spent, budget)
            return

        ratio = cfg["soft_warn_ratio"]
        if budget > 0 and ratio > 0 and spent >= budget * ratio:
            warn_key = (scope_key, period_id)
            if warn_key not in self._soft_warned:
                self._soft_warned.add(warn_key)
                self.logger.warning(
                    "[budget_gate] 预算软告警: %s spent=%.6f / budget=%.6f (%.0f%%)",
                    scope_key,
                    spent,
                    budget,
                    (spent / budget) * 100 if budget else 0,
                )

    async def on_response(self, ctx) -> None:
        """成功响应后按 usage 与单价表累计估算费用。"""
        response = ctx.response
        cfg = self._settings()
        if not cfg["enabled"] or not isinstance(response, dict) or not response.get("ok"):
            return

        metrics = self._extract_usage_metrics(str(response.get("response_body", "") or ""))
        if not metrics:
            return

        model = str(response.get("model") or "") or str(
            (ctx.request or {}).get("model", "") if isinstance(ctx.request, dict) else ""
        )
        rules = self._pricing_rules(cfg)
        cost, _currency = estimate_row_cost(
            model=model,
            prompt_tokens=metrics["prompt_tokens"],
            completion_tokens=metrics["completion_tokens"],
            cached_tokens=metrics["cached_tokens"],
            cache_creation_tokens=metrics["cache_creation_tokens"],
            rules=rules,
        )
        if cost <= 0:
            return

        scope_key = str(ctx.bag_get("budget_gate.scope_key") or "")
        period_id = str(ctx.bag_get("budget_gate.period_id") or "")
        if not scope_key or not period_id:
            # 兼容：bag 缺失时按响应字段重建（例如插件中途热启用）
            request = ctx.request if isinstance(ctx.request, dict) else {}
            if not request.get("model") and model:
                request = {**request, "model": model}
            scope_key = self._scope_key(request, cfg["scope"])
            period_id = self._period_id(cfg["period"], cfg["window_seconds"])

        bucket = self._bucket(scope_key, period_id)
        bucket["spent"] = float(bucket.get("spent", 0.0) or 0.0) + float(cost)
        bucket["requests"] = int(bucket.get("requests", 0) or 0) + 1
        bucket["tokens"] = int(bucket.get("tokens", 0) or 0) + int(metrics["total_tokens"])
        ctx.bag_set("budget_gate.last_cost_usd", round(float(cost), 8))

    def status(self) -> dict:
        """管理/排障用状态快照。"""
        cfg = self._settings()
        period_id = self._period_id(cfg["period"], cfg["window_seconds"])
        items = []
        for (scope_key, pid), bucket in sorted(self._buckets.items()):
            if pid != period_id:
                continue
            spent = float(bucket.get("spent", 0.0) or 0.0)
            budget = float(cfg["budget_usd"] or 0.0)
            items.append(
                {
                    "scope_key": scope_key,
                    "period_id": pid,
                    "spent_usd": round(spent, 6),
                    "budget_usd": budget,
                    "remaining_usd": round(max(0.0, budget - spent), 6) if budget > 0 else None,
                    "requests": int(bucket.get("requests", 0) or 0),
                    "tokens": int(bucket.get("tokens", 0) or 0),
                    "exhausted": bool(budget > 0 and spent >= budget),
                }
            )
        return {
            "ok": True,
            "enabled": cfg["enabled"],
            "scope": cfg["scope"],
            "period": cfg["period"],
            "period_id": period_id,
            "budget_usd": cfg["budget_usd"],
            "buckets": items,
        }

    def reset(self) -> dict:
        """清空全部费用桶与软告警标记。"""
        count = len(self._buckets)
        self._buckets.clear()
        self._soft_warned.clear()
        return {"ok": True, "cleared_buckets": count}
