"""Agent Runtime 的服务生命周期集成。"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI

from akm.agent_runtime.loop import AgentLoop, ToolRegistry
from akm.agent_runtime.tools import build_builtin_tools


async def initialize_agent_runtime(
    app: FastAPI,
    http_client: Any,
    plugin_manager: Any,
    audit_submitter: Callable[[dict], Awaitable[Any]],
    logger: logging.Logger,
) -> AgentLoop:
    """初始化 Agent Loop 并注册仅供 Agent API 使用的内置工具。"""
    agent_loop = AgentLoop(
        http_client,
        plugin_manager=plugin_manager,
        audit_submitter=audit_submitter,
    )
    tool_registry = ToolRegistry.instance()
    for tool in build_builtin_tools(app):
        tool_registry.register(tool)
    app.state.agent_loop = agent_loop
    logger.info("[Server] Agent Loop 已初始化，审计日志已就绪，已注册 %d 个内置调试工具", len(tool_registry))
    return agent_loop
