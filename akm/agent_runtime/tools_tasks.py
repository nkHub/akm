"""定时任务类 Agent 工具（akm_list_tasks / akm_create_task / akm_delete_task）。

从 ``akm/agent_runtime/tools.py`` 的 ``build_builtin_tools`` 里拆出：这三个工具
只依赖 ``akm.db`` 的任务表 CRUD，不读取 ``app.state``、也不依赖 tools.py 内的
任何 helper，因此可以独立成模块，避免继续膨胀那个 1800+ 行的装配函数。

**工具描述与参数 schema 与拆分前逐字一致**——它们是模型看到的契约，改动会
影响模型行为，因此拆分过程只搬位置、不改文案。
"""

from __future__ import annotations

from typing import Any

from akm.agent_runtime.loop import ToolDef

def list_tasks_tool(
    task_type: str = "",
    enabled: str = "",
) -> list[dict[str, Any]]:
    """列出已配置的定时任务（akm 后台任务系统，见 /v1/tasks）。

    可选的按任务类型（agent_call / usage_query）或启用状态过滤，
    返回任务 id、名称、类型、间隔、启用状态与执行时间，不含敏感信息。
    """
    from akm.db import list_tasks

    task_type = str(task_type or "").strip()
    enabled_only = str(enabled or "").strip() == "1"
    return list_tasks(
        task_type=task_type or None,
        enabled_only=enabled_only,
    )


def create_task_tool(
    name: str,
    task_type: str,
    payload: dict[str, Any] | None = None,
    interval_sec: int = 0,
    cron: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    """创建一条定时任务，返回创建后的完整任务记录。

    task_type 支持 agent_call（周期调用 Agent Loop 跑一轮对话，payload 需含
    messages）与 usage_query（对指定 alias 执行用量查询脚本，payload 需含
    alias）。interval_sec 为循环间隔秒数，0 表示单次执行后自动禁用；
    cron 为预留字段暂不解析。
    """
    from akm.db import create_task

    try:
        return create_task(
            name=str(name),
            task_type=str(task_type),
            payload=payload or {},
            interval_sec=int(interval_sec or 0),
            cron=str(cron or ""),
            enabled=bool(enabled),
        )
    except ValueError as exc:
        return {"error": str(exc)}


def delete_task_tool(task_id: str) -> dict[str, Any]:
    """删除一条定时任务，返回是否删除成功。"""
    from akm.db import delete_task

    task_id = str(task_id or "").strip()
    if not task_id:
        return {"error": "task_id 不能为空"}
    return {"deleted": delete_task(task_id)}


def build_task_tools() -> list[ToolDef]:
    """装配定时任务类工具的 ToolDef 列表。"""
    return [
        ToolDef(
            "akm_list_tasks",
            "列出已配置的定时任务（akm 后台任务系统）：返回任务 id、名称、类型（agent_call / usage_query）、间隔、启用状态与执行时间。可用 task_type 过滤类型、enabled=1 只看启用任务",
            {
                "type": "object",
                "properties": {
                    "task_type": {"type": "string", "description": "按任务类型过滤：agent_call 或 usage_query，留空不过滤"},
                    "enabled": {"type": "string", "description": "传 1 时只返回启用的任务，留空返回全部"},
                },
                "required": [],
            },
            list_tasks_tool,
        ),
        ToolDef(
            "akm_create_task",
            "创建一条定时任务（akm 后台任务系统）：agent_call 类型周期调用 Agent Loop 跑一轮对话（payload 需含 messages，可带 model/tools/instructions/max_turns）；usage_query 类型对指定 alias 执行用量查询脚本（payload 需含 alias）。interval_sec 为循环间隔秒数，0 表示单次执行后自动禁用；cron 为预留字段。返回创建后的完整任务记录",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "任务名称"},
                    "task_type": {"type": "string", "description": "任务类型：agent_call 或 usage_query"},
                    "payload": {"type": "object", "description": "任务参数：agent_call 需 messages（对话历史，可带 model/tools/instructions/max_turns）；usage_query 需 alias"},
                    "interval_sec": {"type": "integer", "description": "循环间隔秒数，0（默认）表示单次执行后自动禁用"},
                    "cron": {"type": "string", "description": "预留字段，暂不解析"},
                    "enabled": {"type": "boolean", "description": "是否立即启用，默认 true"},
                },
                "required": ["name", "task_type"],
            },
            create_task_tool,
        ),
        ToolDef(
            "akm_delete_task",
            "删除一条定时任务（akm 后台任务系统），按 akm_list_tasks 返回的 task_id 删除，返回是否删除成功",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "要删除的任务 id（来自 akm_list_tasks）"},
                },
                "required": ["task_id"],
            },
            delete_task_tool,
        ),
    ]
