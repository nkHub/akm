"""git worktree 沙箱（移植自 flow 项目的 worktree.ts）。

为编码节点提供两种隔离模式：
- ``run``：整个 run 共享一个 worktree（WORKTREES_ROOT/{runId}/main）
- ``per-coding``：每个编码节点独立 worktree（{run_id}/nodes/{node_id}）

仅在 workflow.variables.useWorktree 为真时启用；否则编码节点直接使用
projectPath。分支命名 flow/{runId}/main 与 flow/{runId}/{nodeId}。
"""

import os
import re
import shutil
import subprocess

# worktree 根目录默认值（AKM 复用 ~/.akm/flow_worktrees）；
# 可通过 config.json 的 agent_flow.worktrees_root 自定义
WORKTREES_ROOT = os.path.join(os.path.expanduser("~/.akm"), "flow_worktrees")


def _worktrees_root() -> str:
    """解析 worktree 根目录：优先 config.json 的 agent_flow.worktrees_root，否则默认 ~/.akm/flow_worktrees。"""
    try:
        from akm.config import load_config

        configured = (load_config().get("agent_flow") or {}).get("worktrees_root")
        if configured:
            return os.path.abspath(os.path.expanduser(str(configured)))
    except Exception:  # noqa: BLE001
        pass
    return WORKTREES_ROOT


def is_truthy_var(value) -> bool:
    """1/true/yes/on 视为真。"""
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def parse_worktree_mode(raw: str | None) -> str:
    """解析 worktree 模式（run / per-coding）。"""
    s = (raw or "run").strip().lower()
    if s in ("per-coding", "per_coding", "node", "per-node"):
        return "per-coding"
    return "run"


def workflow_wants_worktree(variables: dict) -> bool:
    """variables.useWorktree 为真则启用 worktree。"""
    return is_truthy_var(variables.get("useWorktree"))


def workflow_keep_worktree(variables: dict) -> bool:
    """variables.keepWorktree 为真则保留 worktree 不清理。"""
    return is_truthy_var(variables.get("keepWorktree"))


def run_git(cwd: str, args: list[str], timeout_ms: int = 60_000) -> dict:
    """执行 git 命令，返回 {code, stdout, stderr}。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
        )
        return {"code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired as exc:
        return {"code": 124, "stdout": exc.stdout or "", "stderr": "git timed out"}
    except FileNotFoundError:
        return {"code": 127, "stdout": "", "stderr": "git 命令不存在"}


def resolve_git_root(project_path: str) -> dict:
    """解析 git 仓库根目录；非仓库返回 {ok: False, error}。"""
    from akm.flow.path_lock import normalize_project_path

    abs_path = normalize_project_path(project_path)
    if not os.path.exists(abs_path):
        return {"ok": False, "error": f"目录不存在: {abs_path}"}
    result = run_git(abs_path, ["rev-parse", "--show-toplevel"])
    if result["code"] != 0:
        return {
            "ok": False,
            "error": result["stderr"].strip() or "projectPath 不是 git 仓库（worktree 需要 git）",
        }
    root = result["stdout"].strip()
    if not root:
        return {"ok": False, "error": "无法解析 git root"}
    return {"ok": True, "root": normalize_project_path(root)}


def _safe_segment(s: str) -> str:
    """把任意字符串规整为安全目录名（非 [a-zA-Z0-9._-] → -，截断 48 字符）。"""
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", s)[:48]


def plan_worktree_paths(run_id: str, node_id: str | None = None) -> dict:
    """规划 worktree 目录与分支。"""
    base = os.path.join(_worktrees_root(), _safe_segment(run_id))
    if node_id:
        return {
            "dir": os.path.join(base, "nodes", _safe_segment(node_id)),
            "branch": f"flow/{_safe_segment(run_id)}/{_safe_segment(node_id)}",
        }
    return {
        "dir": os.path.join(base, "main"),
        "branch": f"flow/{_safe_segment(run_id)}/main",
    }


def create_worktree(git_root: str, dest: str, branch: str) -> dict:
    """创建链接 worktree，返回 {path, branch, reused}。已存在则复用。"""
    if os.path.exists(dest):
        check = run_git(dest, ["rev-parse", "--is-inside-work-tree"])
        if check["code"] == 0 and check["stdout"].strip() == "true":
            return {"path": dest, "branch": branch, "reused": True}
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # 优先从 HEAD 新建分支；分支已存在则改用现有分支；最后 detached 兜底
    result = run_git(git_root, ["worktree", "add", "-b", branch, dest, "HEAD"])
    if result["code"] != 0:
        result = run_git(git_root, ["worktree", "add", dest, branch])
    if result["code"] != 0:
        result = run_git(git_root, ["worktree", "add", "--detach", dest, "HEAD"])
    if result["code"] != 0:
        err = (result["stderr"] or result["stdout"]).strip() or f"code {result['code']}"
        raise RuntimeError(f"git worktree add 失败: {err}")
    return {"path": dest, "branch": branch, "reused": False}


def remove_worktree(git_root: str, worktree_path: str) -> None:
    """删除单个 worktree（git remove 优先，rm -rf 兜底，再 prune）。"""
    abs_path = os.path.abspath(worktree_path)
    result = run_git(git_root, ["worktree", "remove", "--force", abs_path])
    if result["code"] != 0:
        result = run_git(git_root, ["worktree", "remove", "--force", worktree_path])
    shutil.rmtree(abs_path, ignore_errors=True)
    run_git(git_root, ["worktree", "prune"])


def cleanup_run_worktrees(state: dict | None, run_id: str | None = None) -> list[str]:
    """删除 run 的全部 worktree（main + nodes）并清掉整个 runId 目录。"""
    if not state and not run_id:
        return []
    removed: list[str] = []
    root = state.get("baseProjectPath") if state else None
    paths: list[str] = []
    if state:
        if state.get("runPath"):
            paths.append(state["runPath"])
        paths.extend((state.get("nodePaths") or {}).values())
    for p in paths:
        try:
            if root:
                remove_worktree(root, p)
                removed.append(p)
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            shutil.rmtree(p, ignore_errors=True)
            removed.append(p)
        except Exception:  # noqa: BLE001
            pass
    folder = None
    if run_id:
        folder = os.path.join(_worktrees_root(), _safe_segment(run_id))
    elif state and state.get("runPath"):
        folder = os.path.dirname(state["runPath"])
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
    return removed


def ensure_run_worktree(run_id: str, project_path: str, mode: str, existing: dict | None = None) -> dict:
    """确保 run 级共享 worktree 存在；返回/更新 state。"""
    if existing and existing.get("runPath") and existing.get("baseProjectPath"):
        if os.path.exists(existing["runPath"]):
            return existing
    git = resolve_git_root(project_path)
    if not git["ok"]:
        raise RuntimeError(git["error"])
    plan = plan_worktree_paths(run_id)
    created = create_worktree(git["root"], plan["dir"], plan["branch"])
    return {
        "baseProjectPath": git["root"],
        "mode": mode,
        "runPath": created["path"],
        "nodePaths": (existing or {}).get("nodePaths") or {},
        "branchPrefix": f"flow/{_safe_segment(run_id)}",
    }


def ensure_node_worktree(run_id: str, node_id: str, project_path: str, existing: dict | None = None):
    """确保单节点 worktree 存在（从 git root 独立 add）；返回 (state, path, reused)。"""
    state = existing or None
    base = state.get("baseProjectPath") if state else project_path
    git = resolve_git_root(base or project_path)
    if not git["ok"]:
        raise RuntimeError(git["error"])
    if state is None:
        state = {
            "baseProjectPath": git["root"],
            "mode": "per-coding",
            "nodePaths": {},
            "branchPrefix": f"flow/{_safe_segment(run_id)}",
        }
    prev = state.get("nodePaths", {}).get(node_id)
    if prev and os.path.exists(prev):
        return state, prev, True
    plan = plan_worktree_paths(run_id, node_id)
    created = create_worktree(git["root"], plan["dir"], plan["branch"])
    state = {
        **state,
        "baseProjectPath": git["root"],
        "mode": "per-coding",
        "nodePaths": {**state.get("nodePaths", {}), node_id: created["path"]},
    }
    return state, created["path"], created["reused"]


def resolve_coding_cwd(run_id: str, node_id: str, project_path: str, variables: dict, existing: dict | None = None) -> dict:
    """解析编码节点工作目录（按 worktree 策略）。

    返回 {cwd, state, isolated, note}；无 worktree 时 state 为 None。
    """
    if not workflow_wants_worktree(variables):
        from akm.flow.path_lock import normalize_project_path

        return {
            "cwd": normalize_project_path(project_path),
            "state": None,
            "isolated": False,
            "note": "直接使用 projectPath（无 worktree）",
        }
    mode = parse_worktree_mode(variables.get("worktreeMode"))
    if mode == "per-coding":
        state, node_path, reused = ensure_node_worktree(run_id, node_id, project_path, existing)
        return {
            "cwd": node_path,
            "state": state,
            "isolated": True,
            "note": f"复用节点 worktree: {node_path}" if reused else f"创建节点 worktree: {node_path}",
        }
    state = ensure_run_worktree(run_id, project_path, "run", existing)
    return {
        "cwd": state["runPath"],
        "state": state,
        "isolated": True,
        "note": f"run worktree: {state['runPath']}",
    }
