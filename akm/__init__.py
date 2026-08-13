"""AKM 包级元信息。"""

__version__ = "0.1.25"

# 供 server.py API 触发的待执行操作（由 menubar native timer 在主线程安全执行）
# 放在 akm/__init__.py 而非 menubar.py，避免 py2app 打包后 __main__ 与 akm.menubar 双加载
# 导致 _pending_action 不同步的问题。
_pending_action: str | None = None  # "restart"


def trigger_server_restart() -> bool:
    """供 server.py 调用：请求重启服务（由 menubar native timer 在主线程安全执行）。"""
    global _pending_action
    _pending_action = "restart"
    return True
