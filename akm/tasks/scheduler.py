"""后台任务调度器：周期性扫描到期任务并执行。

调度器随服务 lifespan 启动（见 ``akm/server.py``），与既有的用量查询
调度器（_UsageQueryScheduler）相互独立：本调度器只负责处理
``scheduled_tasks`` 表中的通用任务，旧调度器保持原样不动。

任务执行后按类型分派：
- ``agent_call``：调用 Agent Loop（``app.state.agent_loop.run``）。
- ``usage_query``：对指定 alias 的 key 执行用量查询脚本并落库。

循环任务按 ``interval_sec`` 滚动计算下一次执行时间；``interval_sec=0``
视为单次任务，执行后自动禁用。提供 ``cron`` 表达式时优先按 cron 计算
下一次执行时间（cron 优先于 interval_sec）。
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from akm.db import (
    get_task,
    list_tasks,
    update_task,
    delete_task,
)
from akm.config import load_config
from akm.key_pool import list_keys, key_model_list

logger = logging.getLogger("akm.tasks.scheduler")

# 调度器扫描间隔（秒）：最小 5 秒，避免空转过快
_DEFAULT_SCAN_INTERVAL_SEC = 5
_MIN_SCAN_INTERVAL_SEC = 5


def _default_agent_model() -> str:
    """为 agent_call 任务挑选一个默认模型。

    任务未显式指定 model 时使用：取第一个 active Key 的模型列表首项。
    避免把空模型传给 Agent Loop / 转发层——空模型无法选 Key，历史上还会
    误命中通配 Key 并发往不支持空模型的上游（如 opencode.ai 返回 401），
    进而触发故障切换把 Key 自动禁用。
    """
    for key in list_keys():
        if key.get("status") != "active":
            continue
        models = key_model_list(key)
        if models:
            return models[0]
    return ""


class TaskScheduler:
    """后台定时任务调度器。

    持有 FastAPI app 引用，任务执行时通过 ``app.state.agent_loop`` 调用
    Agent Loop；用量查询通过 ``app.state.http_client`` 复用连接池。
    """

    def __init__(self, app: Any) -> None:
        """初始化调度器，绑定 app 实例。"""
        self.app = app
        self._scan_interval = _DEFAULT_SCAN_INTERVAL_SEC
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """创建后台任务并启动调度循环。"""
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """取消后台任务并等待退出。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run(self) -> None:
        """后台主循环：周期扫描到期任务并执行。"""
        while True:
            try:
                # 从配置读取扫描间隔（可选配置项，默认 5 秒，最小 5 秒）
                configured = int(
                    load_config().get("task_check_interval_sec", _DEFAULT_SCAN_INTERVAL_SEC)
                    or _DEFAULT_SCAN_INTERVAL_SEC
                )
                self._scan_interval = max(_MIN_SCAN_INTERVAL_SEC, configured)
                await asyncio.sleep(self._scan_interval)
                await self._check_due_tasks()
            except asyncio.CancelledError:
                break
            except Exception:
                # 单轮扫描失败不影响后台循环存活
                logger.exception("[TaskScheduler] 扫描任务时发生异常")

    async def _check_due_tasks(self) -> None:
        """扫描启用中的任务，对到期任务逐个执行。"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for task in list_tasks(enabled_only=True):
            next_run = task.get("next_run_at", "")
            # 到期判断：next_run_at 为空或早于等于当前时间
            if next_run and next_run > now_str:
                continue
            try:
                await self._execute_task(task)
            except Exception:
                # 单个任务执行失败不影响其它任务与后台循环
                logger.exception("[TaskScheduler] 任务执行失败: %s", task.get("id"))
            finally:
                # 无论成败都推进下一次执行，避免失败任务卡住调度器
                await self._advance_task(task)

    async def _execute_task(self, task: dict) -> None:
        """按任务类型分派执行。"""
        task_type = task.get("task_type", "")
        payload = task.get("payload") or {}
        if task_type == "agent_call":
            await self._run_agent_call(task, payload)
        elif task_type == "usage_query":
            await self._run_usage_query(task, payload)
        else:
            logger.warning("[TaskScheduler] 未知任务类型: %s", task_type)

    async def _run_agent_call(self, task: dict, payload: dict) -> None:
        """执行 agent_call 任务：调用 Agent Loop 跑一轮对话。"""
        agent_loop = getattr(self.app.state, "agent_loop", None)
        if agent_loop is None:
            if not load_config().get("agent_enabled", True):
                logger.warning("[TaskScheduler] agent_call 任务 %s 跳过：agent_enabled=false 关闭了 Agent Loop", task.get("id"))
            else:
                logger.warning("[TaskScheduler] agent_loop 未就绪，任务 %s 跳过", task.get("id"))
            return
        messages = payload.get("messages") or []
        if not messages:
            logger.warning("[TaskScheduler] agent_call 任务 %s 缺少 messages", task.get("id"))
            return
        model = str(payload.get("model") or "").strip()
        if not model:
            # 任务未指定模型时回填默认模型，避免空模型误选通配 Key
            # （空模型向上游发送会触发 401 → 自动禁用 Key 的连锁问题）。
            model = _default_agent_model()
        result = await agent_loop.run(
            messages,
            model=model,
            tools=payload.get("tools"),
            instructions=payload.get("instructions", ""),
            max_turns=int(payload.get("max_turns", 0) or 0),
            api_path=payload.get("api_path", "chat/completions"),
            workspace_root=payload.get("workspace_root", ""),
            source="task",
        )
        # 记录执行结果摘要到任务 payload，便于事后追溯
        summary = {
            "ok": bool(getattr(result, "ok", False)),
            "final_message": str(getattr(result, "final_message", ""))[:500],
        }
        merged = dict(payload)
        merged["last_result"] = summary
        merged["last_result_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 仅更新 last_result 相关字段，避免覆盖用户对 payload 的其它自定义
        try:
            update_task(task["id"], payload=merged)
        except Exception:
            logger.exception("[TaskScheduler] 更新任务结果失败: %s", task.get("id"))

    async def _run_usage_query(self, task: dict, payload: dict) -> None:
        """执行 usage_query 任务：对指定 alias 的 key 做一次用量查询。"""
        alias = (payload.get("alias") or "").strip()
        if not alias:
            logger.warning("[TaskScheduler] usage_query 任务 %s 缺少 alias", task.get("id"))
            return
        # 复用既有用量查询调度器的查询逻辑：按 alias 找到 key 与脚本配置
        from akm.key_pool import list_keys, get_usage_query_config, update_usage_data
        from akm.usage_query import execute_query_script

        keys = {key.get("alias"): key for key in list_keys()}
        key = keys.get(alias)
        if key is None:
            logger.warning("[TaskScheduler] usage_query 任务 %s 找不到 key: %s", task.get("id"), alias)
            return
        config = get_usage_query_config(alias, key.get("provider", ""))
        if config is None:
            logger.warning("[TaskScheduler] usage_query 任务 %s 无查询配置: %s", task.get("id"), alias)
            return
        script_raw = config.get("script", "")
        if not script_raw:
            logger.warning("[TaskScheduler] usage_query 任务 %s 脚本为空: %s", task.get("id"), alias)
            return
        try:
            script_cfg = json.loads(script_raw)
        except json.JSONDecodeError:
            logger.warning("[TaskScheduler] usage_query 任务 %s 脚本解析失败: %s", task.get("id"), alias)
            return
        http_client = getattr(self.app.state, "http_client", None)
        result = await execute_query_script(key, script_cfg, http_client=http_client)
        update_usage_data(alias, result)

    async def _advance_task(self, task: dict) -> None:
        """推进任务执行状态：更新 last_run_at 与下一次执行时间。"""
        task_id = task["id"]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fields: dict[str, Any] = {"last_run_at": now_str}
        cron = (task.get("cron") or "").strip()
        if cron:
            # cron 任务：按表达式计算下一次执行时间（cron 优先于 interval_sec）
            from akm.db import cron_next_run

            try:
                fields["next_run_at"] = cron_next_run(cron)
            except Exception:
                # 表达式异常（理论上已被创建/更新时拦截）：回退到禁用该任务，避免卡死
                logger.exception("[TaskScheduler] cron 表达式异常，禁用任务 %s: %s", task_id, cron)
                fields["next_run_at"] = ""
                fields["enabled"] = False
        elif int(task.get("interval_sec", 0) or 0) > 0:
            # 循环任务：按间隔滚动排下一次
            from datetime import timedelta

            next_run = (datetime.now() + timedelta(seconds=int(task.get("interval_sec", 0)))).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            fields["next_run_at"] = next_run
        else:
            # 单次任务：执行后自动禁用
            fields["next_run_at"] = ""
            fields["enabled"] = False
        update_task(task_id, **fields)
