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

        if self._cfg.get("enabled", True):
            self._balance_task = asyncio.create_task(self._balance_loop())
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
        for num_key in ("balance_interval_s", "idle_after_s"):
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
        # 没有在途请求且距上次状态变化超过阈值时，对外表现为待机，
        # 但内部仍保留 success / error 供气泡展示最近结果。
        state = self._state
        if state in (STATE_SUCCESS, STATE_ERROR, STATE_WAITING):
            if self._inflight == 0 and now - self._state_since > idle_after:
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

    def _set_state(self, state: str, *, detail: str = "", text: str = "") -> None:
        """切换状态并记录切换时间。相同状态重复设置不重置计时。"""
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
        self._set_state(STATE_THINKING, detail=f"正在准备 {detail}")
        # 把本次请求的开始时间放进 bag，on_response 用来算耗时。
        try:
            ctx.bag_set("dafeiyu.started_at", time.time())
            ctx.bag_set("dafeiyu.model", model)
        except Exception:
            pass

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
        self._set_state(STATE_ERROR, detail=f"上游异常：{detail}")
        return None

    async def on_response(self, ctx) -> None:
        """响应返回：按 meta.ok 落到 success / error，并更新最近结果。"""
        if not self._cfg.get("enabled", True):
            return
        if not self._should_track(ctx):
            return
        if self._inflight > 0:
            self._inflight -= 1

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

        if ok:
            self._set_state(STATE_SUCCESS, detail="")
        else:
            self._set_state(
                STATE_ERROR,
                detail=f"失败 {status_code or ''} {phase}".strip(),
            )
        if self._cfg.get("sound_enabled"):
            self._play_sound(ok)

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

    # ── 余额查询 ────────────────────────────────────────────

    async def _balance_loop(self) -> None:
        """按配置间隔刷新余额；首次延迟几秒，避免和宿主启动抢资源。"""
        await asyncio.sleep(5)
        while True:
            try:
                if self._cfg.get("enabled", True):
                    await self._refresh_balance()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.debug("[dafeiyu] 余额刷新异常: %s", exc)
            interval = max(15, int(self._cfg.get("balance_interval_s") or 60))
            await asyncio.sleep(interval)

    async def _refresh_balance(self) -> dict[str, Any]:
        """查询 DeepSeek 余额。

        复用 AKM 已有的 Key 存储：取 provider=deepseek 且 base_url 指向 DeepSeek
        官方（或支持 /user/balance 的自定义网关）的第一个 Key。
        查询失败时保留上次成功结果，只标记 stale，避免气泡闪成空。
        """
        key = self._pick_balance_key()
        if not key:
            # 没有可用 Key 不是错误，只是「无凭据」，气泡自动隐藏余额行。
            self._balance = {"available": False, "reason": "no_key", "balances": []}
            self._balance_at = time.time()
            return self._balance

        base_url = str(key.get("base_url") or "").strip().rstrip("/")
        api_key = str(key.get("api_key") or "")
        if not base_url or not api_key:
            self._balance = {"available": False, "reason": "no_key", "balances": []}
            self._balance_at = time.time()
            return self._balance

        url = f"{base_url}/user/balance"
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code == 401 or resp.status_code == 403:
                self._mark_balance_failure("auth", f"HTTP {resp.status_code}")
                return self._balance
            if resp.status_code >= 400:
                self._mark_balance_failure("unavailable", f"HTTP {resp.status_code}")
                return self._balance
            body = resp.json()
        except httpx.TimeoutException:
            self._mark_balance_failure("unavailable", "请求超时")
            return self._balance
        except Exception as exc:
            self._mark_balance_failure("unavailable", str(exc))
            return self._balance

        parsed = self._parse_balance(body)
        if not parsed:
            self._mark_balance_failure("invalid_response", "返回结构无法识别")
            return self._balance
        parsed["stale"] = False
        parsed["source"] = "api_key"
        self._balance = parsed
        self._balance_at = time.time()
        return self._balance

    def _pick_balance_key(self) -> dict | None:
        """挑选用于余额查询的 Key：优先 deepseek provider，其次 base_url 命中。"""
        try:
            from akm.key_pool import list_keys

            keys = list_keys()
        except Exception:
            return None
        deepseek = [k for k in keys if str(k.get("provider") or "") == "deepseek"]
        for k in deepseek:
            if "deepseek" in str(k.get("base_url") or "").lower():
                return k
        if deepseek:
            return deepseek[0]
        # 兜底：任何 base_url 含 deepseek 的 Key（自定义网关自建 Key 常这么配）
        for k in keys:
            if "deepseek" in str(k.get("base_url") or "").lower():
                return k
        return None

    @staticmethod
    def _parse_balance(body: Any) -> dict[str, Any] | None:
        """解析 DeepSeek /user/balance。

        官方结构：``{is_available, balance_infos:[{currency,total_balance,
        granted_balance,topped_up_balance}]}``。金额是字符串，保留原始精度，
        不在插件侧做浮点换算（前端/气泡直接显示字符串）。
        """
        if not isinstance(body, dict):
            return None
        infos = body.get("balance_infos")
        if not isinstance(infos, list) or not infos:
            return None
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
        if not rows:
            return None
        return {
            "available": bool(body.get("is_available", True)),
            "balances": rows,
        }

    def _mark_balance_failure(self, reason: str, detail: str) -> None:
        """余额查询失败：有旧值就标 stale 保留，没旧值就记失败原因。"""
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
            """手动刷新余额（最快 5 秒一次，避免打爆上游）。"""
            if self._balance_at and time.time() - self._balance_at < 5:
                return {"ok": False, "error": "刷新过于频繁，请稍后再试", "balance": self._balance_payload()}
            data = await self._refresh_balance()
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
