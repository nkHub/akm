"""系统内部错误日志 — 将内部异常详情写入 ~/.akm/error.log，避免泄露给客户端。"""

import json
import os
import traceback
import threading
from datetime import datetime


ERROR_LOG_DIR = os.path.expanduser("~/.akm")
os.makedirs(ERROR_LOG_DIR, exist_ok=True)
ERROR_LOG_PATH = os.path.join(ERROR_LOG_DIR, "error.log")

# 写入锁，避免多线程并发写入时日志行交错
_write_lock = threading.Lock()


def write_error_log(
    source: str,
    error: str,
    traceback_str: str | None = None,
    request_path: str = "",
    extra: dict | None = None,
) -> None:
    """将内部错误详情以 JSON 行写入 ~/.akm/error.log。

    参数:
        source: 错误来源标识，如 "server.global_exception"、"proxy.forward"
        error: 错误信息的简短描述（通常为 str(exc)）
        traceback_str: 完整的调用栈字符串（来自 traceback.format_exc()）
        request_path: 触发错误的请求路径（如果有）
        extra: 额外的上下文信息（如 key_alias、model 等）
    """
    payload = {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "source": source,
        "error": error,
        "traceback": traceback_str or "",
        "request_path": request_path,
        "extra": extra or {},
    }
    try:
        with _write_lock:
            with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        # 如果连日志都写不了，不要因为日志写入失败而引发二次异常
        pass
