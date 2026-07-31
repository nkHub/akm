"""供 Agent Loop 使用的 AKM 内置只读调试工具。"""

from typing import Any

from fastapi import FastAPI

from akm.agent_runtime.loop import ToolDef
from akm.audit import list_logs_async
from akm.key_pool import key_model_list, list_keys


def build_builtin_tools(app: FastAPI) -> list[ToolDef]:
    """创建与当前服务实例绑定的只读调试工具。

    工具刻意只暴露排障所需的运行元数据。密钥明文、审计请求体、响应体和
    请求头都不会进入模型上下文，避免 Agent 调用意外扩大敏感数据暴露面。
    """

    def get_status() -> dict[str, Any]:
        """返回健康监护、审计队列和已加载插件的摘要状态。"""
        monitor = getattr(app.state, "health_monitor", None)
        audit_queue = getattr(app.state, "audit_log_queue", None)
        plugin_manager = getattr(app.state, "plugin_manager", None)
        plugins = getattr(plugin_manager, "plugins", {})
        return {
            "health": monitor.detail_payload() if monitor is not None else {},
            "audit_queue": {
                "size": audit_queue.qsize() if audit_queue is not None else 0,
                "maxsize": getattr(audit_queue, "maxsize", 0),
                "dropped_count": getattr(audit_queue, "dropped_count", 0),
                "failure_count": getattr(audit_queue, "failure_count", 0),
                "worker_alive": audit_queue.worker_alive() if audit_queue is not None else False,
            },
            "plugins": [
                {
                    "name": name,
                    "enabled": plugin.enabled,
                    "runtime_ready": plugin.runtime_ready,
                }
                for name, plugin in plugins.items()
            ],
        }

    def get_keys() -> list[dict[str, Any]]:
        """返回 Key 的非敏感连接与模型配置，绝不返回 API Key。"""
        return [
            {
                "alias": key.get("alias", ""),
                "provider": key.get("provider", ""),
                "models": key_model_list(key),
                "priority": key.get("priority", 0),
                "status": key.get("status", ""),
            }
            for key in list_keys()
        ]

    async def get_logs(
        limit: int = 20,
        status: str = "all",
        days: int = 1,
        key_alias: str = "",
    ) -> list[dict[str, Any]]:
        """返回近期审计元数据，不包含任意请求或响应内容。"""
        limit = max(1, min(int(limit), 50))
        days = max(0, min(int(days), 30))
        if status not in {"all", "success", "failed"}:
            raise ValueError("status 只能是 all、success 或 failed")
        logs = await list_logs_async(
            limit=limit,
            status=status,
            days=days,
            key_alias=str(key_alias or ""),
        )
        return [
            {
                "id": log.get("id"),
                "timestamp": log.get("timestamp", ""),
                "provider": log.get("provider", ""),
                "key_alias": log.get("key_alias", ""),
                "model": log.get("model", ""),
                "status_code": log.get("status_code", 0),
                "latency_ms": log.get("latency_ms", 0),
                "prompt_tokens": log.get("prompt_tokens", 0),
                "completion_tokens": log.get("completion_tokens", 0),
                "error": log.get("error", ""),
            }
            for log in logs
        ]

    empty_object = {"type": "object", "properties": {}}
    return [
        ToolDef("akm_get_status", "读取 AKM 服务健康、审计队列和插件运行状态", empty_object, get_status),
        ToolDef("akm_list_keys", "列出 AKM 中已配置 Key 的非敏感状态与模型信息，不返回密钥", empty_object, get_keys),
        ToolDef(
            "akm_list_logs",
            "查询近期 AKM 审计日志摘要，不返回请求体、响应体或请求头",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回条数，1 到 50，默认 20"},
                    "status": {"type": "string", "enum": ["all", "success", "failed"], "description": "状态筛选，默认 all"},
                    "days": {"type": "integer", "description": "最近自然日范围，0 表示不限制，默认 1"},
                    "key_alias": {"type": "string", "description": "按 Key 别名筛选，可选"},
                },
            },
            get_logs,
        ),
    ]
