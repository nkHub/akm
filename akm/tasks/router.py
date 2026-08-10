"""定时任务 CRUD 与立即执行路由。

通过 ``/v1/tasks`` 提供对 ``scheduled_tasks`` 表的增删改查与手动触发，
供 chat 等外部客户端动态管理后台任务。

鉴权复用 ``/v1/agent`` 的 ``_check_agent_auth``：``agent_api_token``
未配置时本地直连免鉴权；配置后需携带 ``Authorization: Bearer`` 或
``X-Agent-Token`` 头。
"""

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from akm.db import (
    TASK_TYPES,
    create_task,
    get_task,
    list_tasks,
    update_task,
    delete_task,
    validate_cron_expr,
)

logger = logging.getLogger("akm.tasks.router")

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


async def _check_auth(request: Request) -> JSONResponse | None:
    """任务接口的可选鉴权（与 /v1/agent 一致）。"""
    from akm.agent_runtime.router import _check_agent_auth

    return await _check_agent_auth(request)


@router.get("")
async def list_task_route(request: Request, task_type: str = "", enabled: str = ""):
    """列出全部任务，可选按 task_type / enabled 过滤。"""
    auth_error = await _check_auth(request)
    if auth_error is not None:
        return auth_error
    try:
        items = list_tasks(
            task_type=task_type or None,
            enabled_only=True if enabled == "1" else False,
        )
    except Exception as exc:
        return JSONResponse(status_code=400, content={"detail": f"查询任务失败: {exc}"})
    return JSONResponse(content={"tasks": items, "total": len(items)})


@router.post("")
async def create_task_route(request: Request):
    """创建一条定时任务。"""
    auth_error = await _check_auth(request)
    if auth_error is not None:
        return auth_error
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "请求体必须是 JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "请求体必须是对象"})
    name = str(body.get("name") or "").strip()
    task_type = str(body.get("task_type") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"detail": "缺少必填字段 name"})
    if task_type not in TASK_TYPES:
        return JSONResponse(
            status_code=400,
            content={"detail": f"task_type 必须是 {'/'.join(TASK_TYPES)} 之一"},
        )
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    interval_sec = int(body.get("interval_sec", 0) or 0)
    cron = str(body.get("cron") or "")
    enabled = bool(body.get("enabled", True))
    if cron.strip() and not validate_cron_expr(cron):
        return JSONResponse(status_code=400, content={"detail": f"cron 表达式非法: {cron}"})
    try:
        task = create_task(
            name=name,
            task_type=task_type,
            payload=payload,
            interval_sec=interval_sec,
            cron=cron,
            enabled=enabled,
        )
    except Exception as exc:
        return JSONResponse(status_code=400, content={"detail": f"创建任务失败: {exc}"})
    return JSONResponse(status_code=201, content={"task": task})


@router.get("/{task_id}")
async def get_task_route(request: Request, task_id: str):
    """查询单条任务详情。"""
    auth_error = await _check_auth(request)
    if auth_error is not None:
        return auth_error
    task = get_task(task_id)
    if task is None:
        return JSONResponse(status_code=404, content={"detail": "任务不存在"})
    return JSONResponse(content={"task": task})


@router.put("/{task_id}")
async def update_task_route(request: Request, task_id: str):
    """更新任务字段（name / task_type / payload / interval_sec / cron / enabled）。"""
    auth_error = await _check_auth(request)
    if auth_error is not None:
        return auth_error
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "请求体必须是 JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "请求体必须是对象"})
    if get_task(task_id) is None:
        return JSONResponse(status_code=404, content={"detail": "任务不存在"})
    try:
        updated = update_task(task_id, **body)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=400, content={"detail": f"更新任务失败: {exc}"})
    return JSONResponse(content={"task": updated})


@router.delete("/{task_id}")
async def delete_task_route(request: Request, task_id: str):
    """删除一条定时任务。"""
    auth_error = await _check_auth(request)
    if auth_error is not None:
        return auth_error
    deleted = delete_task(task_id)
    if not deleted:
        return JSONResponse(status_code=404, content={"detail": "任务不存在"})
    return JSONResponse(content={"ok": True, "deleted": task_id})


@router.post("/{task_id}/run")
async def run_task_route(request: Request, task_id: str):
    """立即执行一次任务，不走调度器（不改变 last_run_at / next_run_at）。"""
    auth_error = await _check_auth(request)
    if auth_error is not None:
        return auth_error
    task = get_task(task_id)
    if task is None:
        return JSONResponse(status_code=404, content={"detail": "任务不存在"})
    scheduler = getattr(request.app.state, "task_scheduler", None)
    if scheduler is None:
        return JSONResponse(status_code=503, content={"detail": "任务调度器未就绪"})
    # 类型收窄：调度器实例在 lifespan 中已创建，此处仅做防御
    if not hasattr(scheduler, "_execute_task"):
        return JSONResponse(status_code=503, content={"detail": "任务调度器未就绪"})
    try:
        await scheduler._execute_task(task)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": f"执行失败: {exc}"})
    return JSONResponse(content={"ok": True, "id": task_id})
