"""项目路径互斥锁（移植自 flow 项目的 path-lock.ts）。

作用：让并发编码节点不会同时写同一 projectPath（如 dual_model 的
codeA/codeB）。进程内异步互斥：同一 key 同时只有一个持有者，其余在
asyncio 队列中排队；持有者释放后自动移交下一个。cancel 时按 run_id
批量拒绝等待者并释放持有的锁。
"""

import asyncio
import os
from collections import deque
from typing import Callable

# key → (run_id, node_id)，当前持有者
_holders: dict[str, tuple[str, str]] = {}
# key → deque[(run_id, node_id, future)]，排队等待者
_queues: dict[str, deque[tuple[str, str, asyncio.Future]]] = {}


def normalize_project_path(raw: str | None) -> str:
    """解析为稳定的绝对路径 key（best-effort realpath，失败用绝对路径）。"""
    base = (raw or ".").strip() or "."
    abs_path = os.path.abspath(os.path.expanduser(base))
    try:
        return os.path.realpath(abs_path)
    except OSError:
        return abs_path


def _release_key(key: str, expected: tuple[str, str] | None) -> None:
    """释放 key 的持有权；expected 用于幂等校验（只有真正持有者才能释放）。"""
    current = _holders.get(key)
    if not current:
        return
    if expected is not None and current != expected:
        return
    _holders.pop(key, None)

    queue = _queues.get(key)
    if not queue:
        return
    next_run, next_node, fut = queue.popleft()
    if not queue:
        _queues.pop(key, None)
    # 移交持有权并唤醒下一个等待者
    holder = (next_run, next_node)
    _holders[key] = holder
    if not fut.done():
        fut.set_result(holder)


async def acquire_path_lock(project_path: str | None, run_id: str, node_id: str) -> Callable[[], None]:
    """获取 projectPath 的互斥锁。

    返回一个幂等的 release 函数；调用方应在 finally 中释放。
    等待期间若该 run 被取消，会抛 RuntimeError（调用方按节点失败处理）。
    """
    key = normalize_project_path(project_path)

    def _make_release(holder: tuple[str, str]) -> Callable[[], None]:
        released = False

        def _release() -> None:
            nonlocal released
            if released:
                return
            released = True
            _release_key(key, holder)

        return _release

    if key not in _holders:
        holder = (run_id, node_id)
        _holders[key] = holder
        return _make_release(holder)

    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _queues.setdefault(key, deque()).append((run_id, node_id, fut))
    holder = await fut
    return _make_release(holder)


def release_path_locks_for_run(run_id: str) -> None:
    """取消 run 持有的锁，并拒绝其仍在排队的等待者。"""
    for key in list(_holders):
        if _holders[key][0] == run_id:
            _release_key(key, _holders[key])
    for key in list(_queues):
        keep: deque[tuple[str, str, asyncio.Future]] = deque()
        for run, node, fut in _queues[key]:
            if run == run_id:
                if not fut.done():
                    fut.set_exception(RuntimeError("cancelled"))
            else:
                keep.append((run, node, fut))
        if keep:
            _queues[key] = keep
        else:
            _queues.pop(key, None)


def path_lock_debug() -> dict:
    """测试/调试快照。"""
    return {
        "holders": {k: v for k, v in _holders.items()},
        "queueSizes": {k: len(v) for k, v in _queues.items()},
    }
