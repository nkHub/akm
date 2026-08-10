"""定时任务系统：提供可持久化、可动态增删改查的后台任务调度能力。

任务分为两类：
- ``agent_call``：到点后调用 Agent Loop 执行一轮对话（messages 在 payload 中）。
- ``usage_query``：到点后对指定 alias 的 key 执行用量查询脚本并落库。

对外主要暴露：
- :func:`akm.tasks.scheduler.TaskScheduler`：后台调度器，随服务 lifespan 启动/停止。
- :func:`akm.tasks.router.router`：``/v1/tasks`` 的 CRUD 路由。
"""

from akm.tasks.scheduler import TaskScheduler

__all__ = ["TaskScheduler"]
