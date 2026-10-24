"""大肥鱼桌宠插件（macOS）。

职责分三层，互相解耦：

1. **状态采集**：挂在 AKM 转发链路的 hook 上，把一次请求的生命周期映射成
   桌宠能理解的 6 个状态（idle / thinking / working / waiting / success / error），
   结果只写进进程内快照，不参与请求改写，也不会因为异常影响主链路。
2. **HTTP 接口**：给原生桌宠进程和插件设置页提供只读状态、余额与启停控制。
3. **桌宠进程管理**：按需 spawn 一个 pyobjc 子进程（``pet/pet_app.py``）创建
   无边框透明置顶窗口；进程通过轮询 ``/api/dafeiyu/state`` 取状态。

设计取舍（与 dsh-dafeiyu 原版的差异，均在 README 说明）：

- 原版状态来自 DSH 的会话事件（思考/查找/修改/执行/验证…）。AKM 是转发网关，
  没有会话事件模型，只有请求生命周期，因此状态粒度退化为「请求阶段」，
  不再区分 searching / editing / testing 这类工具语义。
- 原版同时支持 WebUI 页面内角色与原生窗口。这里只做原生窗口（macOS），
  不做页面内注入，避免为此修改宿主模板。
- 素材沿用上游 PC2005-cloud/dsh-pet（见 assets/ASSET_LICENSE.md），
  与插件代码授权分离。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter

from akm.plugins import PluginBase


# 角色缩放百分比，固定值（不开放设置项）。桌宠是常驻挂件，100% 会挡视线，
# 50% 是用户确认的默认观感；改这个值等于改默认大小。
PET_SCALE = 50
# ── 状态常量 ────────────────────────────────────────────────
# 取值与 dafeiyu 的 CompanionState 保持一致，便于素材动作一一对应。
STATE_IDLE = "idle"
STATE_THINKING = "thinking"
STATE_WORKING = "working"
STATE_WAITING = "waiting"
STATE_SUCCESS = "success"
STATE_ERROR = "error"

# 每个状态对应的素材动作组。素材目录名来自上游 pet-manifest.json 的 clips 键。
STATE_CLIP = {
    STATE_IDLE: "idle",
    STATE_THINKING: "thinking",
    STATE_WORKING: "working",
    STATE_WAITING: "waiting",
    STATE_SUCCESS: "success",
    STATE_ERROR: "error",
}

# 一次性动画（loop=False）播完一轮需要多久，用于「状态回落不能早于动画播完」。
# 数据来自 assets/pet-manifest.json：success 241 帧、error 118 帧，每帧 42ms。
# 硬编码而非读 manifest：index.py 与 pet/ 在不同进程/目录，且这两个值稳定，
# 读文件失败时反倒会让回落逻辑失去兜底。
STATE_ANIM_SECONDS = {
    STATE_SUCCESS: 241 * 0.042,   # ≈ 10.12 秒
    STATE_ERROR: 118 * 0.042,     # ≈ 4.96 秒
    # thinking / working / waiting 都是 241 帧循环动作，同样 10.12 秒一轮。
    # 用于「动画播完还没回 → 切下一个状态」的超时判定。
    STATE_THINKING: 241 * 0.042,  # ≈ 10.12 秒
    STATE_WORKING: 241 * 0.042,   # ≈ 10.12 秒
}

# 静默期：最后一个请求结束后，等这么多秒确认「这一轮真的结束了」，
# 再播 success/error 动画。期间若来了新请求，就跳过终态直接进 thinking/working。
#
# **为什么需要它**：success 动画要 10.12 秒，而高频请求可能 2 秒一个。
# 若「结束即播终态」，5 个请求会有 4 个被 10 秒动画挡住，角色一直在庆祝、
# 完全看不出还在处理新请求（实测模拟：每 2 秒一个请求，5 个里 4 个被挡住）。
# 用 1.5 秒静默期区分两种情况：
#   - 之后没请求了 → 播完整 success/error（用户能看到反馈）
#   - 之后马上又来 → 跳过终态，持续 thinking/working（用户看到在忙）
ROUND_QUIET_S = 1.5


# waiting 判定阈值：当前在途请求里「已选 Key、在等上游返回」的占比。
# 用户要求 9 成以上才切 waiting——少量请求慢不该影响整体显示。
WAITING_RATIO = 0.9
# 状态的中文名：管理台首页卡片直接展示这个，不暴露内部英文枚举值。
STATE_LABEL = {
    STATE_IDLE: "待机",
    STATE_THINKING: "思考中",
    STATE_WORKING: "转发中",
    STATE_WAITING: "等待响应",
    STATE_SUCCESS: "成功",
    STATE_ERROR: "出错",
}

# 状态文案：原版 status-copy.js 是按工具语义组织的，这里按请求阶段重写。
STATE_TEXT = {
    STATE_IDLE: ["我在这儿等新任务哦", "现在暂时没任务呢", "大肥鱼正在待命中~"],
    STATE_THINKING: ["有请求进来啦，正在准备呢", "正在把请求交出去哦~", "让我看看这次要发给谁呢"],
    STATE_WORKING: ["正在等上游回话呢", "上游正在处理哦~", "请求已经发出去了呢"],
    STATE_WAITING: ["上游有点慢，还在等呢", "这里要等一下下哦~", "正在耐心等待响应呢"],
    STATE_SUCCESS: ["这次请求搞定啦~", "转发顺利完成啦", "任务完成咯~"],
    STATE_ERROR: ["这次没成功呢…", "上游好像出问题了哦", "请求失败啦，看看日志吧"],
}

# 插件数据目录：桌宠窗口位置、日志等落在这里，不进插件目录（插件可能只读）。
_DATA_DIR = Path.home() / ".akm" / "dafeiyu"


class Plugin(PluginBase):
    """大肥鱼桌宠插件实例。

    单个实例在宿主进程内是单例，因此状态快照直接挂在实例属性上。
    并发安全：hook 只做赋值与追加，读接口取副本，不加锁也不会崩；
    计数类字段用整型自增，Python 侧 GIL 足以保证不丢步。
    """

    def __init__(self) -> None:
        super().__init__()
        self.router = APIRouter()
        # ── 状态快照 ──
        self._state: str = STATE_IDLE
        self._state_text: str = ""
        self._state_since: float = time.time()
        self._last_result: dict[str, Any] = {}
        self._inflight: int = 0
        # 本轮的请求登记表：key 是请求唯一 id，value 是状态 dict
        #   {"sent": bool, "started": float}
        # 「sent」由 on_key_selected 置 True（Key 已选、请求马上发往上游），
        # on_response 时移除。用它精确统计「当前有多少请求在等上游返回」，
        # 而不是靠 thinking 动画播了多久去猜。
        self._pending_requests: dict[str, dict] = {}
        # ── 轮次（一个「轮回」= 从第一个请求进来，到静默期结束）──
        # 终态不立刻落定，而是先记在这里，等静默期过了再播 success/error。
        self._pending_state: str = ""
        self._pending_detail: str = ""
        # 本轮成功/失败计数：用于「过半判定」（成功数 > 总数的一半 → success）
        self._round_ok: int = 0
        self._round_err: int = 0
        # 静默期截止时刻：_inflight 归 0 时设为 now + ROUND_QUIET_S。
        # waiting 相关：thinking 动画播完一轮仍未返回 → 切 waiting。
        # 记录本轮「thinking 开始」的时刻作为基准，而不是 _state_since——
        # 因为 _state_since 在切到 working 时会重置，用它会导致永远触发不到。
        self._round_started_at: float = 0.0
        # working 需要「完整播完一轮」才允许切 success 的场景：
        # waiting 期间请求成功返回时，先切 working 并记下「可落定时刻」，
        # 等 working 动画播完才由 _maybe_settle_round 落定 success。
        self._working_until: float = 0.0
        self._deferred_terminal: str = ""     # 待落定的终态（waiting→working 场景用）
        self._deferred_detail: str = ""
        # 期间若来了新请求，静默期作废、直接进 thinking/working（跳过终态动画）。
        self._quiet_until: float = 0.0
        # 本轮是否已落定终态（防止重复触发 / 静默期结束后又播一次）
        self._round_settled: bool = True
        self._counters: dict[str, int] = {
            "requests": 0,
            "success": 0,
            "error": 0,
        }
        self._recent: list[dict[str, Any]] = []
        # ── 余额缓存 ──
        self._balance: dict[str, Any] = {}
        self._balance_at: float = 0.0
        self._balance_task: asyncio.Task | None = None
        # ── 桌宠子进程 ──
        self._pet_proc: subprocess.Popen | None = None
        self._pet_started_at: float = 0.0
        self._pet_last_error: str = ""
        self._cfg: dict[str, Any] = {}
        # 服务端口：从 config 读取，供桌宠进程回调本机接口。
        self._port: int = 0
        self._register_routes()

    # ── 生命周期 ────────────────────────────────────────────

    async def on_load(self) -> bool | None:
        """加载时准备数据目录、读取配置、按需拉起桌宠进程。"""
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # 目录不可写时不应阻断插件加载
            self.logger.warning("[dafeiyu] 数据目录创建失败: %s", exc)

        self._reload_config()
        self._port = self._resolve_port()
        self._mount_assets()

        if self._cfg.get("enabled", True) and self._cfg.get("autostart_pet", True):
            # 不阻塞宿主启动：桌宠拉起放在后台任务里，失败只记录。
            asyncio.create_task(self._safe_start_pet())

        # **不再启动余额轮询任务**：AKM 后台 `UsageQueryWorker` 已按每个 Key
        # 的 `usage_query_interval_m` 定时查询并把结果写进 `keys.usage_data`。
        # 插件只需在 `/state` 里顺带读缓存（见 `_refresh_balance`），
        # 不需要自己的定时器——否则同一个 Key 会被查两次。
        if self._cfg.get("enabled", True):
            # 首帧就把余额读进来，避免桌宠刚起来时余额行是空的
            try:
                self._refresh_balance()
            except Exception as exc:
                self.logger.debug("[dafeiyu] 初始余额读取失败: %s", exc)

        # 把 Key 列表填进「关联 Key」下拉选项。
        # 必须在 _refresh_balance 之后：那时已经能算出「自动匹配到哪个 Key」，
        # 用它作为下拉的默认选中项（用户看到的就是实际生效的那个）。
        self._sync_balance_key_options()
        return True

    async def on_unload(self) -> None:
        """卸载时停掉余额轮询与桌宠子进程，避免留下孤儿窗口。"""
        if self._balance_task and not self._balance_task.done():
            self._balance_task.cancel()
            try:
                await self._balance_task
            except (asyncio.CancelledError, Exception):
                pass
        self._balance_task = None
        self.stop_pet()

    def on_config_changed(self, old_config: dict, new_config: dict) -> None:
        """配置保存后刷新缓存；影响窗口形态的项需要重启桌宠才生效。"""
        old_top = self._cfg.get("always_on_top")
        old_drag = self._cfg.get("allow_drag")
        self._cfg = dict(new_config or {})
        # 置顶 / 拖动 / 减少动态是启动参数，改动后重启子进程才能生效。
        # 角色大小已从设置项移除（固定 PET_SCALE=50），不再参与比较。
        changed_visual = (
            old_top != self._cfg.get("always_on_top")
            or old_drag != self._cfg.get("allow_drag")
            or old_config.get("reduce_motion") != self._cfg.get("reduce_motion")
        )
        if changed_visual and self._pet_proc and self._pet_proc.poll() is None:
            self.logger.info("[dafeiyu] 窗口配置变化，重启桌宠进程")
            self.stop_pet()
            asyncio.create_task(self._safe_start_pet())

    # ── 配置与端口 ──────────────────────────────────────────

    def _reload_config(self) -> None:
        """把 plugin.json settings 的默认值与用户配置合并成扁平字典。"""
        merged: dict[str, Any] = {}
        meta = self.meta
        for item in (getattr(meta, "settings", None) or []):
            key = getattr(item, "key", "")
            if key:
                merged[key] = getattr(item, "default", "")
        merged.update(self.config or {})
        # number 类型在配置里可能是字符串，这里统一转数值。
        # number 类型在配置里可能是字符串，这里统一转数值。
        # balance_interval_s 已移除（余额改为读 AKM 缓存，插件不再自己定时查）。
        for num_key in ("idle_after_s",):
            try:
                merged[num_key] = int(float(merged.get(num_key) or 0))
            except (TypeError, ValueError):
                merged[num_key] = 0
        self._cfg = merged

    def _mount_assets(self) -> None:
        """把素材目录挂到插件专属静态路径。

        宿主的 ``has_menu`` 静态挂载固定指向插件目录下的 ``views/``，而素材
        （59MB / 3462 帧）放在 ``assets/`` 与 ``pet/`` 同级，不适合塞进 views。
        这里由插件自己 mount 一份只读静态目录，供设置页预览首帧使用。
        原生桌宠进程直接读文件系统，不经过该挂载。

        幂等：热重载或重复 on_load 时同一个 app 不能重复 mount 同名路径，
        因此先用 ``self._assets_mounted`` 记状态。
        """
        if getattr(self, "_assets_mounted", False) or self.app is None:
            return
        assets_dir = Path(__file__).resolve().parent / "assets"
        if not assets_dir.exists():
            self.logger.warning("[dafeiyu] 素材目录不存在: %s", assets_dir)
            return
        try:
            from fastapi.staticfiles import StaticFiles

            self.app.mount(
                "/api/dafeiyu/assets-static",
                StaticFiles(directory=str(assets_dir)),
                name="dafeiyu_assets",
            )
            self._assets_mounted = True
        except Exception as exc:
            # 挂载失败只影响设置页预览，不影响桌宠与状态接口
            self.logger.warning("[dafeiyu] 素材静态挂载失败: %s", exc)

    def _resolve_port(self) -> int:
        """取宿主实际监听端口，供桌宠进程回调。

        优先读配置里的 server_port（AKM 的启动参数会写回配置），
        取不到时退回默认 8800。
        """
        try:
            from akm.config import get as config_get

            return int(config_get("server_port", 8800) or 8800)
        except Exception:
            return 8800

    def _state_payload(self) -> dict[str, Any]:
        """构造给桌宠进程轮询的状态快照（纯数据，不含内部对象）。"""
        idle_after = max(2, int(self._cfg.get("idle_after_s") or 8))
        now = time.time()
        # 每次轮询做两件由时间驱动的事（AKM 没有「全部请求结束」回调，
        # 只能靠桌宠每 0.5 秒拉一次 /state 顺路推进）：
        #   1. 静默期（或 working 播完）已过 → 落定 success/error
        #   2. 9 成以上在途请求在等上游 → 切 waiting
        #
        # **顺序不能反**：先落定终态，再判 waiting。否则刚落定的 success
        # 会被 waiting 判定立刻覆盖（落定时 _pending_requests 可能还没清空），
        # 用户就看到 success 一闪而过、直接变 waiting。
        self._maybe_settle_round(now)
        self._maybe_enter_waiting(now)
        # 状态回落规则（对外快照用，不改内部 _state）：
        #
        # 1. 有在途请求（_inflight > 0）→ **一律保持当前状态**。
        #    这就是「后续请求延长 working 时间」：连续请求期间角色一直忙，
        #    动画不被打断。
        # 2. 没有在途请求且距最后一次状态变化超过 idle_after → 回落 idle。
        #    **不再只对 success/error/waiting 生效**：之前 thinking 不在白名单里，
        #    导致 on_response 缺失时状态永久卡在 thinking（日志实测卡了
        #    1510 / 1591 秒）。现在所有非 idle 状态都能回落。
        # 3. success / error 的展示时长按**动画实际时长**兜底：
        #    这两个动作是 loop=False 的一次性动画，success 需要 241*42ms=10.12s，
        #    而 idle_after 默认 8 秒会让它只播到 79% 就被切走。
        state = self._state
        if self._inflight == 0 and state != STATE_IDLE:
            hold = idle_after
            if state in (STATE_SUCCESS, STATE_ERROR):
                # 至少留够动画播完一轮的时间（+1 秒缓冲）
                hold = max(idle_after, int(STATE_ANIM_SECONDS.get(state, 0)) + 1)
            if now - self._state_since > hold:
                state = STATE_IDLE
        return {
            "state": state,
            "clip": STATE_CLIP.get(state, "idle"),
            "text": self._state_text or self._pick_text(state),
            "since": self._state_since,
            "inflight": self._inflight,
            "counters": dict(self._counters),
            "last_result": dict(self._last_result),
            "recent": list(self._recent[:8]),
            "balance": self._balance_payload(),
            "config": {
                # 角色大小固定 50%（不提供设置项）：桌宠是常驻小挂件，
                # 100% 太大挡视线，50% 是用户选定的默认观感。
                "scale": PET_SCALE,
                "bubble": bool(self._cfg.get("bubble_enabled", True)),
                "bubble_balance": bool(self._cfg.get("bubble_show_balance", True)),
                "reduce_motion": bool(self._cfg.get("reduce_motion", False)),
                "sound": bool(self._cfg.get("sound_enabled", False)),
                "allow_drag": bool(self._cfg.get("allow_drag", True)),
                "always_on_top": bool(self._cfg.get("always_on_top", True)),
                "idle_after_s": idle_after,
                # 对话 / 碎碎念 / 多屏配置：桌宠进程据此决定功能是否可用
                "model": str(self._cfg.get("chat_model") or ""),
                "base_url": f"http://127.0.0.1:{self._port}",
                "chat_prompt": str(self._cfg.get("chat_prompt") or ""),
                "whisper": bool(self._cfg.get("whisper_enabled", True)),
                "whisper_interval_s": max(30, int(self._cfg.get("whisper_interval_s") or 180)),
                "whisper_prompt": str(self._cfg.get("whisper_prompt") or ""),
                "notify_unfocused": bool(self._cfg.get("notify_unfocused", True)),
                "lock_screen": bool(self._cfg.get("lock_screen", False)),
            },
            "server_time": now,
        }

    def _balance_payload(self) -> dict[str, Any]:
        """余额对外视图：区分「从未成功」与「刷新失败但保留旧值」。"""
        if not self._balance:
            return {"available": False, "reason": "not_queried", "balances": []}
        out = dict(self._balance)
        if self._balance_at:
            out["queried_at"] = datetime.fromtimestamp(self._balance_at).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            out["stale"] = bool(self._balance.get("stale"))
        return out

    def _pick_text(self, state: str) -> str:
        """按状态挑一句文案；用秒级时间做种子，避免每帧抖动。"""
        pool = STATE_TEXT.get(state) or STATE_TEXT[STATE_IDLE]
        return pool[int(time.time()) % len(pool)]

    def _set_state(self, state: str, *, detail: str = "", text: str = "",
                   force: bool = False) -> None:
        """切换状态并记录切换时间。

        **关键规则：在途请求期间不接受终态（success/error）**。

        多个请求并发时，先完成的那个会调 ``_set_state(SUCCESS)``，但此时
        ``_inflight`` 仍 > 0（还有别的请求在飞）。如果直接切到 success，
        桌宠会播成功动画——可用户明明还在等其余请求。

        所以：``_inflight > 0`` 时，终态只记到 ``_pending_state``，
        等最后一个请求结束（``_inflight`` 归 0）再统一落定。
        ``thinking``/``working`` 这类「进行中」状态不受此限制。

        相同状态重复设置不重置计时——连续请求都处于 thinking 时，
        计时不动，桌宠的循环动画就不会被打断。
        """
        if not force and self._inflight > 0 and state in (
            STATE_SUCCESS, STATE_ERROR, STATE_IDLE
        ):
            # 还有请求在飞：先挂起，等最后一个结束再定
            self._pending_state = state
            self._pending_detail = detail or self._pending_detail
            return
        if state != self._state:
            self._state = state
            self._state_since = time.time()
        if text:
            self._state_text = text
        elif detail:
            self._state_text = detail

    # ── Hook：把转发链路映射成状态 ──────────────────────────

    async def on_request(self, ctx) -> None:
        """请求到达：进入 thinking（准备选 Key / 转换格式）。"""
        if not self._cfg.get("enabled", True):
            return
        if not self._should_track(ctx):
            return
        self._inflight += 1
        self._counters["requests"] += 1
        model = str(getattr(ctx, "model", "") or "")
        path = str(getattr(ctx, "api_path", "") or "")
        detail = model or path or "新请求"
        # 登记本次请求：此时还没选 Key，sent=False（还不算「在等上游」）。
        # 用 id(ctx) 做 key 便于 on_response 时按同一对象移除。
        req_id = str(id(ctx))
        self._pending_requests[req_id] = {
            "sent": False,
            "started": time.time(),
        }
        try:
            ctx.bag_set("dafeiyu.req_id", req_id)
        except Exception:
            pass
        # **新一轮判定**：_inflight 从 0 → 1 说明上一轮已结束、这是新轮回的
        # 第一个请求。此时清零本轮计数、作废静默期，并标记「本轮未落定」。
        #
        # 静默期被新请求打断 → 直接进 thinking/working，跳过终态动画。
        # 这样高频请求时不会「一直在播 success」（success 动画 10.12 秒，
        # 若每个请求都播会被大量请求挡住）。
        if self._inflight == 1:
            self._round_ok = 0
            self._round_err = 0
            self._quiet_until = 0.0
            self._round_settled = False
            self._pending_state = ""
            self._pending_detail = ""
            # 本轮计时基准：thinking 播完一轮（10.12s）仍未返回就切 waiting
            self._round_started_at = time.time()
            self._working_until = 0.0
            self._deferred_terminal = ""
            self._deferred_detail = ""
        # **第一个请求 → thinking（准备选 Key / 转换格式）；
        # 后面还有请求进来 → 切成 working（转发中，表示并发在处理）**。
        #
        # 语义：thinking 是「我在准备」，working 是「已经有好几条在跑了」。
        # 注意 _set_state 对相同状态不重置计时，所以：
        #   - 第 1 个请求：idle → thinking（切一次，开始计时）
        #   - 第 2 个请求：thinking → working（切一次，开始计时）
        #   - 第 3+ 个请求：已是 working，计时不动，循环动画不被打断
        if self._inflight >= 2:
            self._set_state(STATE_WORKING, detail=f"转发中 {detail}（{self._inflight} 个）")
        else:
            self._set_state(STATE_THINKING, detail=f"正在准备 {detail}")
        # 把本次请求的开始时间放进 bag，on_response 用来算耗时。
        try:
            ctx.bag_set("dafeiyu.started_at", time.time())
            ctx.bag_set("dafeiyu.model", model)
        except Exception:
            pass


    async def on_key_selected(self, ctx, model: str = "", key: dict | None = None) -> None:
        """Key 已选、请求即将发往上游 → 标记该请求进入「等上游返回」。

        **为什么用这个 hook 而不是猜时间**：这是 AKM 里唯一能准确知道
        「请求已经交给上游了」的时点。`on_request` 太早（还在选 Key / 转换格式），
        `on_response` 太晚（已经回来了）。有了这个标记，`waiting` 判定就不再
        依赖「thinking 动画播了多久」这种猜测。
        """
        if not self._cfg.get("enabled", True):
            return
        if not self._should_track(ctx):
            return
        req_id = ""
        try:
            req_id = str(ctx.bag_get("dafeiyu.req_id", "") or "")
        except Exception:
            pass
        entry = self._pending_requests.get(req_id) if req_id else None
        if entry is not None:
            entry["sent"] = True
            entry["sent_at"] = time.time()
            return
        # 兜底：on_request 没登记上（例如 bag 不可用）时，按 id(ctx) 补一条
        self._pending_requests[str(id(ctx))] = {
            "sent": True,
            "started": time.time(),
            "sent_at": time.time(),
        }
    async def on_upstream_error(
        self,
        ctx,
        status_code: int = 0,
        error_type: str = "http",
        attempt: int = 0,
        key: dict | None = None,
    ) -> None:
        """上游报错：先切 error，让用户在重试期间就能看到异常。

        不返回动作字符串——重试/切换 Key 的决策仍由 error_handler、
        fallback_router 等策略插件负责，本插件只做展示。
        """
        if not self._cfg.get("enabled", True):
            return
        if not self._should_track(ctx):
            return
        detail = f"HTTP {status_code}" if status_code else str(error_type or "网络错误")
        if attempt:
            detail += f"（第 {attempt} 次尝试）"
        # 上游报错要**立刻**展示（重试期间用户就该看到异常），
        # 所以传 force=True 绕过「在途请求期间挂起终态」的保护。
        # 若后续重试成功，on_response 会把状态覆盖成 success。
        self._set_state(STATE_ERROR, detail=f"上游异常：{detail}", force=True)
        return None

    async def on_response(self, ctx) -> None:
        """响应返回：按 meta.ok 落到 success / error，并更新最近结果。"""
        if not self._cfg.get("enabled", True):
            return
        if not self._should_track(ctx):
            return
        if self._inflight > 0:
            self._inflight -= 1
        # 并发数降回 1：从 working 退回 thinking（还有一条在准备/转发中）。
        # 不退回的话，剩下那条请求明明只剩一个，动画却还显示「多个在跑」。
        # 只在确实是 working 时才退，避免把 thinking 打回 thinking（无意义切换）。
        if self._inflight == 1 and self._state == STATE_WORKING:
            self._set_state(STATE_THINKING, detail=self._state_text)

        # 从登记表移除本次请求（它已经拿到响应，不再算「在等上游」）。
        try:
            req_id = str(ctx.bag_get("dafeiyu.req_id", "") or "")
        except Exception:
            req_id = ""
        self._pending_requests.pop(req_id or str(id(ctx)), None)

        meta = getattr(ctx, "response", None) or {}
        ok = bool(meta.get("ok"))
        status_code = int(meta.get("status_code") or 0)
        phase = str(meta.get("phase") or "")
        model = ""
        started = 0.0
        try:
            model = str(ctx.bag_get("dafeiyu.model", "") or "")
            started = float(ctx.bag_get("dafeiyu.started_at", 0) or 0)
        except Exception:
            pass
        elapsed_ms = int((time.time() - started) * 1000) if started else 0

        # 流式请求的 on_response 在流结束时才触发，因此耗时口径为「整轮耗时」。
        self._counters["success" if ok else "error"] += 1
        self._last_result = {
            "ok": ok,
            "status_code": status_code,
            "phase": phase,
            "model": model,
            "elapsed_ms": elapsed_ms,
            "at": datetime.now().strftime("%H:%M:%S"),
        }
        # 最近事件列表：新在前，最多保留 8 条。
        self._recent.insert(
            0,
            {
                "at": self._last_result["at"],
                "ok": ok,
                "text": ("成功" if ok else "失败") + (f" {status_code}" if status_code else ""),
                "detail": f"{model} {elapsed_ms}ms".strip() if model else f"{elapsed_ms}ms",
            },
        )
        del self._recent[8:]

        # 累计本轮成功/失败数，用于「过半判定」。
        if ok:
            self._round_ok += 1
        else:
            self._round_err += 1
        # 记住最近一次失败详情，落定 error 时用作气泡文案
        if not ok:
            self._pending_detail = f"失败 {status_code or ''} {phase}".strip()

        # **waiting 期间的返回要特殊处理**：
        #   - 成功 → 先切 working（表示「上游回话了」），并等 working 动画完整
        #     播完一轮再落定 success。用户要求「等 working 的动画播放完，再 success」。
        #   - 失败 → 直接 error，不需要中间态（失败要尽快反馈）。
        # 注意：这只在「已经进入 waiting」时才走这条路；正常速度的请求不会经过它。
        now = time.time()
        was_waiting = self._state == STATE_WAITING
        # 最后一个请求结束 → 启动静默期，等 1.5 秒确认「这一轮真的结束了」。
        # 静默期内若有新请求进来，on_request 会作废它并直接进 thinking/working；
        # 静默期平安过去，才由 _maybe_settle_round() 播 success/error。
        if self._inflight == 0:
            self._quiet_until = now + ROUND_QUIET_S
            if was_waiting and ok:
                # waiting → working：等 working 播完一轮再落定 success。
                # 把静默期截止时刻推到「working 动画播完」之后，
                # _maybe_settle_round 到点后按过半判定落定（此时通常是 success）。
                self._working_until = now + STATE_ANIM_SECONDS.get(STATE_WORKING, 10.12)
                self._quiet_until = self._working_until
                self._deferred_terminal = STATE_SUCCESS
                self._deferred_detail = ""
                self._set_state(STATE_WORKING, detail="上游回话了，正在收尾", force=True)
            elif was_waiting and not ok:
                # waiting → error：失败直接落定，不等动画（尽快反馈异常）
                self._deferred_terminal = ""
                self._deferred_detail = ""
        if self._cfg.get("sound_enabled"):
            self._play_sound(ok)


    def _waiting_ratio(self) -> tuple[int, int, float]:
        """返回 ``(在等上游的请求数, 总在途请求数, 占比)``。

        「在等上游」= 已选 Key（on_key_selected 打过标记）但还没拿到响应。
        用请求登记表精确统计，不靠动画播了多久去猜。
        """
        total = len(self._pending_requests)
        waiting = sum(1 for e in self._pending_requests.values() if e.get("sent"))
        ratio = (waiting / total) if total > 0 else 0.0
        return waiting, total, ratio

    def _maybe_enter_waiting(self, now: float) -> None:
        """决定是否进入 waiting（方案 B：单请求按时间，多请求按占比）。

        **为什么不能只用占比**：单请求时 ``waiting/total`` 恒为 1.0，
        「9 成以上」的条件立刻满足——于是每个请求刚发出就切 waiting，
        thinking 只剩选 Key 那几毫秒，working 几乎看不到（实测日志里
        waiting 连续出现、thinking/working 极少）。

        所以分两种场景：

          - **并发 ≥ 2**：用占比。``sent 数 / 总数 ≥ 0.9`` 才切——
            这才符合「9 成请求在等上游」的本意。
          - **只有 1 个请求**：用时间。``thinking`` 动画播完一轮（10.12s）
            仍未返回才切——「这么久还没回」才是真的异常。

        **不影响其他逻辑**：只做状态切换，不改 `_inflight` / 轮次计数 /
        静默期。
        """
        # 已在播终态（success/error）时不切 waiting：那一轮的动画还没播完，
        # 切走会造成「success 一闪就没了」。等状态回落 idle 后才会再判。
        if self._state in (STATE_SUCCESS, STATE_ERROR):
            return
        waiting, total, ratio = self._waiting_ratio()
        if total == 0:
            return

        if total >= 2:
            # 多请求：按占比。已是 waiting 时若占比跌破阈值要退回去。
            should_wait = ratio >= WAITING_RATIO and waiting >= 1
            detail = f"上游有点慢（{waiting}/{total} 在等）"
        else:
            # 单请求：按时间。从「已发往上游」的时刻算起。
            entry = next(iter(self._pending_requests.values()), {})
            sent_at = float(entry.get("sent_at") or entry.get("started") or 0)
            limit = STATE_ANIM_SECONDS.get(STATE_THINKING, 10.12)
            should_wait = bool(sent_at) and (now - sent_at) >= limit
            detail = f"上游有点慢，已等 {int(now - sent_at)}s" if sent_at else ""

        if should_wait:
            if self._state != STATE_WAITING:
                self._set_state(STATE_WAITING, detail=detail)
        elif self._state == STATE_WAITING:
            # **waiting 必须能自己退出**：占比降下来（有请求完成或新请求
            # 还没发出）就该回到忙碌态，否则会一直卡在 waiting。
            self._set_state(
                STATE_WORKING if total >= 2 else STATE_THINKING,
                detail=f"{total} 个请求处理中",
            )

    def _maybe_settle_round(self, now: float) -> None:
        """静默期结束后落定本轮终态（由 ``_state_payload`` 每次轮询时调用）。

        **为什么要在这里判**：AKM 没有「请求全部结束」的回调，只能靠轮询驱动。
        桌宠每 0.5 秒拉一次 ``/state``，顺路做这件事成本为零。

        **过半判定**：本轮成功率 ≥ 50% → success，否则 error。
        平票（1 成 1 败）算成功——用户对「有成功」更敏感，
        也避免一次偶发失败就让整轮看起来是坏的。

        **waiting→working 的延迟落定**：若本轮是从 waiting 恢复的（请求拖了很久
        才成功），`_deferred_terminal` 会记着 success，且 `_quiet_until` 已被推到
        「working 动画播完」的时刻。所以这里到点后落定的就是 success——
        用户要求「等 working 的动画播放完，再 success」。

        只落定一次（``_round_settled`` 挡住重复触发），落定后清空计数，
        下一个请求进来时（``_inflight`` 0→1）会重置为新一轮。
        """
        if self._round_settled or self._inflight > 0:
            return
        if self._quiet_until <= 0 or now < self._quiet_until:
            return
        # 延迟落定的终态优先（waiting→working 场景）
        if self._deferred_terminal:
            state = self._deferred_terminal
            detail = self._deferred_detail
        else:
            total = self._round_ok + self._round_err
            if total <= 0:
                # 本轮没有任何结果（例如请求被过滤掉），不播终态
                self._quiet_until = 0.0
                self._round_settled = True
                self._working_until = 0.0
                return
            ok_ratio = self._round_ok / total
            if ok_ratio >= 0.5:
                state = STATE_SUCCESS
                detail = ""
            else:
                state = STATE_ERROR
                detail = self._pending_detail or f"失败 {self._round_err}/{total}"
        self._set_state(state, detail=detail, force=True)
        # 落定后清空本轮标记，等下一个请求开启新轮回。
        # **登记表也要清空**：否则残留的「已 sent」请求会让 waiting 判定
        # 在下一帧立刻把刚落定的 success/error 覆盖成 waiting
        # （表现为 success 一闪就没了）。
        self._quiet_until = 0.0
        self._round_settled = True
        self._round_ok = 0
        self._round_err = 0
        self._pending_detail = ""
        self._working_until = 0.0
        self._deferred_terminal = ""
        self._deferred_detail = ""
        self._pending_requests.clear()

    def _should_track(self, ctx) -> bool:
        """过滤掉 AKM 自身的内部子请求，避免 Agent 聊天把角色刷成永动机。"""
        if self._cfg.get("track_agent_requests", False):
            return True
        try:
            headers = getattr(ctx, "client_headers", None) or {}
            # 外部客户端一定会带 User-Agent；内核内部子请求不带客户端头。
            if headers:
                return True
            return bool(getattr(ctx, "client_user_agent", ""))
        except Exception:
            return True

    def _play_sound(self, ok: bool) -> None:
        """用 macOS 内置音效，不额外打包音频文件。"""
        if platform.system() != "Darwin":
            return
        name = "Glass.aiff" if ok else "Basso.aiff"
        path = f"/System/Library/Sounds/{name}"
        if not os.path.exists(path):
            return
        try:
            subprocess.Popen(
                ["afplay", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass

    # ── 余额：读取 AKM 已查好的结果，不再自己请求 ─────────────
    #
    # **为什么不再自己查**：AKM 本身就有完整的用量查询体系——
    # `key_pool._DEEPSEEK_USAGE_SCRIPT` 内置了 `{{baseUrl}}/user/balance`，
    # `usage_query.execute_query_script()` 负责执行，后台 `UsageQueryWorker`
    # 按每个 Key 的 `usage_query_interval_m` 定时刷新，结果存在
    # `keys.usage_data` 里。插件自己再写一套 httpx 请求 + 定时器，
    # 等于**同一个 Key 被查两次、两份数据、两处配置**，还可能不一致。
    #
    # 现在只做一件事：按「默认匹配 Key」的规则选出一个 Key，
    # 读它已经缓存好的 `usage_data`。零网络请求、零定时器。

    def _sync_balance_key_options(self) -> None:
        """把 Key 列表写进「关联 Key」下拉的 options，并让自动匹配项默认选中。

        **为什么能这么做**：``self.meta`` 与 ``PluginManager._plugin_metas[name]``
        是同一个对象，而宿主渲染配置弹窗时读的是 ``meta.settings[*].options``。
        所以插件在 ``on_load`` 里改它，前端下拉就能拿到真实的 Key 列表——
        宿主的 ``options_source`` 只硬编码支持 ``/v1/models``，没法动态列 Key。

        **默认选中**：宿主渲染 select 时用 ``option === 当前配置值`` 判断选中，
        而 value 与 label 是同一个字符串。所以这里直接把「自动匹配到的 alias」
        写进 ``setting.default``——用户没显式配过时，下拉打开就是实际生效的那个，
        不会出现「显示 A、实际用 B」。
        """
        meta = getattr(self, "meta", None)
        if meta is None:
            return
        try:
            from akm.key_pool import list_keys

            keys = list_keys()
        except Exception:
            return
        aliases = [str(k.get("alias") or "") for k in keys if str(k.get("alias") or "")]
        if not aliases:
            return

        # 算出「自动匹配」会选哪个 Key：临时清空配置再跑一次选择逻辑
        auto_alias = ""
        try:
            saved = self._cfg.get("balance_key_alias")
            self._cfg["balance_key_alias"] = ""
            auto_key = self._pick_balance_key()
            self._cfg["balance_key_alias"] = saved
            auto_alias = str((auto_key or {}).get("alias") or "")
        except Exception:
            auto_alias = ""

        for setting in (getattr(meta, "settings", None) or []):
            if getattr(setting, "key", "") != "balance_key_alias":
                continue
            setting.type = "select"
            setting.options = list(aliases)
            setting.allow_empty_option = False
            configured = str(self._cfg.get("balance_key_alias") or "").strip()
            if (not configured or configured not in aliases) and auto_alias:
                setting.default = auto_alias
            break

    def _pick_balance_key(self) -> dict | None:
        """挑选用于显示余额的 Key。

        **优先用配置里指定的 Key**（``balance_key_alias``）：填了别名就只认
        那一个，找不到就返回 None（气泡提示未配置），不会偷偷退回别的 Key——
        否则用户以为在看 A 的余额，实际显示的是 B 的。

        **留空时按默认匹配规则**：
          ① ``provider=deepseek`` 且 ``base_url`` 含 deepseek
             （官方或指向官方的网关）；
          ② 否则第一个 deepseek Key；
          ③ 否则任何 ``base_url`` 含 deepseek 的 Key（自建网关常见配置）。
        多个候选时取**优先级最高的那个**（``list_keys`` 已按 ``priority`` 升序），
        不做合计——用户确认按 A 方案。
        """
        try:
            from akm.key_pool import list_keys

            keys = list_keys()
        except Exception:
            return None

        # ① 配置指定的 Key 优先，且只认它
        want = str(self._cfg.get("balance_key_alias") or "").strip()
        if want:
            for k in keys:
                if str(k.get("alias") or "") == want:
                    return k
            return None

        deepseek = [k for k in keys if str(k.get("provider") or "") == "deepseek"]
        for k in deepseek:
            if "deepseek" in str(k.get("base_url") or "").lower():
                return k
        if deepseek:
            return deepseek[0]
        for k in keys:
            if "deepseek" in str(k.get("base_url") or "").lower():
                return k
        return None

    def _refresh_balance(self) -> dict[str, Any]:
        """从 AKM 的 Key 缓存里读余额（不发请求）。

        数据来源是 ``get_usage_query_config(alias, provider)`` 返回的
        ``data`` 字段——那是 AKM 后台按 Key 自己的查询间隔刷新的结果。

        几种状态要分清：
          - 没有 deepseek Key → ``no_key``，气泡隐藏余额行；
          - 有 Key 但从没查过 → ``not_queried``，提示「未查询」；
          - 查过但失败 → 有旧值就保留并标 ``stale``，没旧值给失败原因。
        """
        key = self._pick_balance_key()
        if not key:
            self._balance = {"available": False, "reason": "no_key", "balances": []}
            self._balance_at = time.time()
            return self._balance

        alias = str(key.get("alias") or "")
        provider = str(key.get("provider") or "")
        try:
            from akm.key_pool import get_usage_query_config

            cfg = get_usage_query_config(alias, provider) or {}
        except Exception as exc:
            self._mark_balance_failure("unavailable", str(exc))
            return self._balance

        data = cfg.get("data")
        error = str(cfg.get("error") or "")
        queried_at = str(cfg.get("queried_at") or "")

        parsed = self._parse_balance(data) if isinstance(data, dict) else None
        if parsed and parsed.get("balances"):
            parsed["stale"] = bool(error)      # 有旧值但最近一次失败 → 标 stale
            parsed["source"] = "akm_usage_cache"
            parsed["alias"] = alias
            if error:
                parsed["error"] = error
            self._balance = parsed
            self._balance_at = time.time()
            return self._balance

        # 没有可用数据：区分「从没查过」与「查过但失败」
        if error:
            self._mark_balance_failure("unavailable", error)
        else:
            self._balance = {
                "available": False,
                "reason": "not_queried",
                "alias": alias,
                "queried_at": queried_at,
                "balances": [],
            }
            self._balance_at = time.time()
        return self._balance

    @staticmethod
    def _parse_balance(data: Any) -> dict[str, Any] | None:
        """把 AKM 缓存的 ``usage_data`` 转成气泡需要的结构。

        **数据形状有三层，必须逐层剥**（实测 AKM 存的是第三种）：

          ① 查询记录包装：``{ok, status_code, latency_ms, queried_at,
             error, raw_response}`` —— 这是 ``keys.usage_data`` 的实际结构，
             真正的上游响应在 ``raw_response`` 里；
          ② 上游原始响应：``{is_available, balance_infos: [...]}``；
          ③ extractor 扁平结果：``{isValid, remaining, unit, planName}``
             （自定义脚本可能直接返回这个）。

        所以先剥 ① 的 ``raw_response``，再按 ②/③ 解析。
        金额保持字符串原样，不在插件侧做浮点换算。
        """
        if not isinstance(data, dict):
            return None

        # ① 剥掉查询记录包装：真正的上游响应在 raw_response 里
        inner = data.get("raw_response")
        if isinstance(inner, dict):
            # 记录里的 ok/error 也要一并带出来（查询失败时 raw_response 可能为空）
            if not inner and data.get("error"):
                return None
            data = inner
        # 形状 A：DeepSeek 官方原始结构（自定义查询脚本可能返回这个）
        infos = data.get("balance_infos")
        if isinstance(infos, list) and infos:
            rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in infos[:8]:
                if not isinstance(row, dict):
                    continue
                currency = str(row.get("currency") or "").upper()
                if not currency or currency in seen:
                    continue
                seen.add(currency)
                rows.append(
                    {
                        "currency": currency,
                        "total": str(row.get("total_balance") or "0"),
                        "granted": str(row.get("granted_balance") or "0"),
                        "topped_up": str(row.get("topped_up_balance") or "0"),
                    }
                )
            if rows:
                return {
                    "available": bool(data.get("is_available", True)),
                    "balances": rows,
                }
            return None

        # 形状 B：AKM extractor 的扁平结果
        remaining = data.get("remaining")
        if remaining is None or str(remaining).strip() == "":
            return None
        unit = str(data.get("unit") or "元")
        return {
            "available": bool(data.get("isValid", True)),
            "balances": [
                {
                    "currency": unit,
                    "total": str(remaining),
                    "granted": "0",
                    "topped_up": "0",
                }
            ],
        }

    def _mark_balance_failure(self, reason: str, detail: str) -> None:
        """余额不可用：有旧值就标 stale 保留，没旧值就记失败原因。"""
        if self._balance and self._balance.get("balances"):
            self._balance = {**self._balance, "stale": True, "error": detail}
        else:
            self._balance = {
                "available": False,
                "reason": reason,
                "error": detail,
                "balances": [],
            }
        self._balance_at = time.time()

    # ── 桌宠子进程 ──────────────────────────────────────────

    def _pet_script(self) -> Path:
        """桌宠入口脚本路径（插件目录下的 pet/pet_app.py）。"""
        return Path(__file__).resolve().parent / "pet" / "pet_app.py"

    def _pet_env(self) -> dict[str, str]:
        """构造子进程环境：把窗口参数与状态接口地址通过环境变量传入。"""
        env = dict(os.environ)
        env["AKM_DAFEIYU_STATE_URL"] = f"http://127.0.0.1:{self._port}/api/dafeiyu/state"
        env["AKM_DAFEIYU_ASSET_ROOT"] = str(Path(__file__).resolve().parent / "assets")
        env["AKM_DAFEIYU_DATA_DIR"] = str(_DATA_DIR)
        # 把宿主 pid 传下去：桌宠每 2 秒探测一次，宿主没了就自己退出。
        # 子进程用了 start_new_session=True 已脱离进程组，宿主被强杀/崩溃时
        # 不会收到 SIGTERM，只能靠子进程主动检测（否则会留下孤儿窗口）。
        env["AKM_DAFEIYU_PARENT_PID"] = str(os.getpid())
        return env

    def _pet_running(self) -> bool:
        """桌宠进程是否还活着。"""
        return bool(self._pet_proc and self._pet_proc.poll() is None)

    async def _safe_start_pet(self) -> None:
        """启动桌宠，失败只记录不抛出（不影响宿主启动）。"""
        try:
            self.start_pet()
        except Exception as exc:
            self._pet_last_error = str(exc)
            self.logger.warning("[dafeiyu] 桌宠启动失败: %s", exc)

    def start_pet(self) -> dict[str, Any]:
        """spawn 桌宠子进程。

        只支持 macOS：原生窗口依赖 AppKit，其余平台直接返回不支持，
        不静默失败——用户需要知道为什么没看到角色。
        """
        if platform.system() != "Darwin":
            self._pet_last_error = f"当前平台 {platform.system()} 不支持原生桌宠"
            return {"ok": False, "error": self._pet_last_error}
        if self._pet_running():
            return {"ok": True, "already_running": True, "pid": self._pet_proc.pid}

        script = self._pet_script()
        if not script.exists():
            self._pet_last_error = f"桌宠入口不存在: {script}"
            return {"ok": False, "error": self._pet_last_error}

        python = self._resolve_python()
        if not python:
            self._pet_last_error = "找不到带 pyobjc 的 Python 解释器"
            return {"ok": False, "error": self._pet_last_error}

        try:
            # stderr 不能接 PIPE 却不读：管道缓冲区（约 64KB）写满后子进程会
            # 在 write 上永久阻塞，主线程连同动画/拖拽一起冻结——这类「用一阵
            # 就卡死」正是它造成的。改为写日志文件，既留证据又不会阻塞。
            log_path = _DATA_DIR / "pet.log"
            # 简单轮转：超过 2MB 就把当前文件挪成 .1（只留一代），
            # 避免长期运行把日志堆成几百 MB。
            try:
                if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
                    rotated = _DATA_DIR / "pet.log.1"
                    if rotated.exists():
                        rotated.unlink()
                    log_path.rename(rotated)
            except Exception:
                pass
            log_file = open(log_path, "ab", buffering=0)
            # 每次启动写一行分隔，方便区分「哪一次运行」的日志
            try:
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                log_file.write(f"\n===== 桌宠启动 {stamp} =====\n".encode("utf-8"))
            except Exception:
                pass
            self._pet_proc = subprocess.Popen(
                [python, str(script)],
                env=self._pet_env(),
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                start_new_session=True,
            )
            # 父进程不再需要这个句柄，交给子进程持有
            log_file.close()
        except Exception as exc:
            self._pet_last_error = f"启动失败: {exc}"
            return {"ok": False, "error": self._pet_last_error}

        self._pet_started_at = time.time()
        self._pet_last_error = ""
        self.logger.info("[dafeiyu] 桌宠进程已启动 pid=%s", self._pet_proc.pid)
        return {"ok": True, "pid": self._pet_proc.pid}

    def stop_pet(self) -> dict[str, Any]:
        """结束桌宠子进程（先 terminate，超时再 kill）。"""
        proc = self._pet_proc
        self._pet_proc = None
        if not proc or proc.poll() is not None:
            return {"ok": True, "already_stopped": True}
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as exc:
            self._pet_last_error = f"停止失败: {exc}"
            return {"ok": False, "error": self._pet_last_error}
        self.logger.info("[dafeiyu] 桌宠进程已停止")
        return {"ok": True}

    def _resolve_python(self) -> str | None:
        """找一个能 ``import AppKit`` 的解释器来跑桌宠子进程。

        候选顺序（前者优先，逐个用真实 ``import AppKit`` 验证，不靠猜）：

        1. ``sys.executable`` 本身。开发时宿主从项目 ``.venv`` 启动、打包后宿主是
           App 内二进制，两种情况下「宿主自己用的解释器」都是依赖最一致的那个
           （App 内 `Contents/MacOS/python` 实测带 pyobjc）。
        2. ``sys.executable`` 同目录下的 ``python`` / ``python3``。打包 App 里
           ``sys.executable`` 可能指向主可执行文件（``AI Key Manager``），真正的
           解释器是同目录的 ``python``。
        3. 项目 ``.venv``（仅开发环境存在；插件装到 ``~/.akm/plugins/`` 后该相对
           路径不存在，会自动跳过）。
        4. 系统 / Homebrew Python。

        全部失败返回 ``None``，由调用方给出明确错误——不静默降级成「没有角色」。
        """
        candidates: list[str] = []

        def _add(path: str | None) -> None:
            if not path:
                return
            if path in candidates:
                return
            if os.path.exists(path):
                candidates.append(path)

        # 1. 宿主自己的解释器
        _add(sys.executable)
        # 2. 同目录下的 python / python3（打包 App 的实际解释器位置）
        exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if sys.executable else ""
        if exe_dir:
            for name in ("python", "python3"):
                _add(os.path.join(exe_dir, name))
        # 3. 项目 venv：从插件目录向上找 .venv（开发环境）
        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / ".venv" / "bin" / "python"
            if candidate.exists():
                _add(str(candidate))
                break
        # 4. 系统 / Homebrew
        for path in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
            _add(path)

        for python in candidates:
            try:
                probe = subprocess.run(
                    [python, "-c", "import AppKit"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            except Exception:
                continue
            if probe.returncode == 0:
                return python
        return None

    # ── 路由 ────────────────────────────────────────────────

    def _register_routes(self) -> None:
        """注册插件 API（前缀由 plugin.json 的 routes_prefix 提供）。"""
        router = self.router

        @router.get("/state")
        async def get_state():
            """桌宠进程轮询入口：返回完整状态快照。"""
            return self._state_payload()

        @router.get("/status")
        async def get_status():
            """插件页用的运行态：状态 + 桌宠进程 + 素材信息。"""
            return {
                "state": self._state_payload(),
                "pet": {
                    "running": self._pet_running(),
                    "pid": self._pet_proc.pid if self._pet_running() else None,
                    "started_at": self._pet_started_at or None,
                    "last_error": self._pet_last_error,
                    "supported": platform.system() == "Darwin",
                    "platform": platform.system(),
                },
                "assets": self._assets_summary(),
            }

        @router.post("/pet/start")
        async def pet_start():
            """手动启动桌宠。"""
            return self.start_pet()

        @router.post("/pet/stop")
        async def pet_stop():
            """手动停止桌宠。"""
            return self.stop_pet()

        @router.post("/pet/restart")
        async def pet_restart():
            """重启桌宠（改完窗口配置后调用）。"""
            self.stop_pet()
            await asyncio.sleep(0.3)
            return self.start_pet()

        @router.post("/balance/refresh")
        async def balance_refresh():
            """重新读一次余额缓存。

            **不再是「去上游查一次」**：数据源是 AKM 的 `keys.usage_data`，
            由 AKM 后台按 Key 自己的间隔刷新。这里只是重新读缓存——
            如果用户想立刻拿到最新值，应该去 Key 管理页点「查询用量」。
            """
            data = self._refresh_balance()
            return {"ok": True, "balance": self._balance_payload(), "raw": data}

        @router.get("/assets/manifest")
        async def assets_manifest():
            """素材清单：桌宠进程用它知道每个动作有多少帧。"""
            return self._load_manifest()

        @router.get("/log")
        async def get_log(lines: int = 100):
            """读取桌宠运行日志（默认最后 100 行）。

            加这个接口是为了让「卡住」类问题可自助排查：桌宠子进程的 stderr
            已重定向到 ``~/.akm/dafeiyu/pet.log``，这里按行尾返回，方便直接
            在浏览器/curl 里看，不用去翻文件。
            """
            return self._read_pet_log(lines)

        @router.post("/log/clear")
        async def clear_log():
            """清空桌宠日志（排查完一轮后重开记录）。"""
            path = _DATA_DIR / "pet.log"
            try:
                if path.exists():
                    path.write_text("", encoding="utf-8")
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    def _read_pet_log(self, lines: int = 100) -> dict[str, Any]:
        """读桌宠日志尾部若干行。

        ``lines`` 夹在 1..2000，避免一次读太多把响应撑大。
        文件不存在时返回空列表而不是报错——没启动过桌宠本来就没有日志。
        """
        path = _DATA_DIR / "pet.log"
        try:
            limit = max(1, min(2000, int(lines)))
        except (TypeError, ValueError):
            limit = 100
        try:
            if not path.exists():
                return {"ok": True, "lines": [], "path": str(path), "size": 0}
            # 文件可能较大，用尾部读取而不是整份读入
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                # 粗估：按平均每行 120 字节回退，最多读 512KB
                back = min(size, max(8192, limit * 120), 512 * 1024)
                fh.seek(size - back)
                raw = fh.read().decode("utf-8", errors="replace")
            tail = raw.splitlines()[-limit:]
            return {
                "ok": True,
                "lines": tail,
                "path": str(path),
                "size": size,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "lines": [], "path": str(path)}

    def _assets_summary(self) -> dict[str, Any]:
        """素材概况（体积、动作数），插件页展示用。"""
        manifest = self._load_manifest()
        clips = manifest.get("clips") or {}
        frames = sum(len((v or {}).get("frames") or []) for v in clips.values())
        root = Path(__file__).resolve().parent / "assets"
        return {
            "clips": len(clips),
            "frames": frames,
            "base_size": manifest.get("baseSize"),
            "frame_size": [
                manifest.get("maxFrameWidth"),
                manifest.get("maxFrameHeight"),
            ],
            "root_exists": root.exists(),
        }

    def _load_manifest(self) -> dict[str, Any]:
        """读取 pet-manifest.json（带进程内缓存）。"""
        cached = getattr(self, "_manifest_cache", None)
        if cached:
            return cached
        path = Path(__file__).resolve().parent / "assets" / "pet-manifest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning("[dafeiyu] 素材清单读取失败: %s", exc)
            data = {}
        self._manifest_cache = data if isinstance(data, dict) else {}
        return self._manifest_cache

    # ── 首页卡片 ────────────────────────────────────────────

    def dashboard_card(self) -> dict:
        """管理台首页卡片：当前状态 + 请求计数 + 余额。

        不展示「最近请求」列表：卡片里只放聚合指标更清爽，逐条记录看审计日志即可。
        状态值用中文（``STATE_LABEL``），不把 ``thinking`` 这类内部枚举直接摆给用户看。
        """
        state = self._state_payload()
        bal = state.get("balance") or {}
        raw_state = str(state.get("state") or STATE_IDLE)
        metrics = [
            {"label": "当前状态", "value": STATE_LABEL.get(raw_state, raw_state)},
            {"label": "请求", "value": self._counters.get("requests", 0)},
            {"label": "成功", "value": self._counters.get("success", 0)},
            {"label": "失败", "value": self._counters.get("error", 0)},
        ]
        if bal.get("balances"):
            first = bal["balances"][0]
            metrics.append(
                {
                    "label": f"余额({first.get('currency', '')})",
                    "value": first.get("total", "-"),
                }
            )
        return {
            "id": "dafeiyu",
            "icon": "sparkle",
            "title": "大肥鱼",
            "metrics": metrics,
            "actions": [{"label": "打开设置", "href": "/plugins/dafeiyu"}],
        }
