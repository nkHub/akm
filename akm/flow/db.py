"""Flow 数据存储：workflows / runs 表与 CRUD 辅助。

沿用 AKM 的 SQLite 连接方式（~/.akm/akm.db），在 init_db 之外独立建表，
保证与定时任务等其它功能互不干扰。工作流定义与运行快照以 JSON 列存储。
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from akm.db import get_connection


# 建表（flow 表独立于 keys/audit_logs/scheduled_tasks，互不依赖）
FLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_workflows (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT DEFAULT '',
    version       INTEGER NOT NULL DEFAULT 1,
    nodes_json    TEXT NOT NULL DEFAULT '[]',
    edges_json    TEXT NOT NULL DEFAULT '[]',
    variables_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flow_runs (
    id            TEXT PRIMARY KEY,
    workflow_id   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    input_json    TEXT NOT NULL DEFAULT '{}',
    data_json     TEXT NOT NULL DEFAULT '{}',
    started_at    TEXT DEFAULT '',
    finished_at   TEXT DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_flow_runs_workflow ON flow_runs(workflow_id);
CREATE INDEX IF NOT EXISTS idx_flow_runs_status ON flow_runs(status);
"""


def now_iso() -> str:
    """返回当前 UTC ISO 时间字符串（与 flow 原实现一致）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def init_flow_db() -> None:
    """确保 flow 相关表存在（幂等，启动时调用）。"""
    conn = get_connection()
    try:
        conn.executescript(FLOW_SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ── workflow CRUD ─────────────────────────────────────────────

def insert_workflow(wf: dict) -> None:
    """插入或替换一条工作流定义。"""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO flow_workflows
               (id, name, description, version, nodes_json, edges_json,
                variables_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                wf["id"],
                wf.get("name", ""),
                wf.get("description", ""),
                int(wf.get("version", 1) or 1),
                json.dumps(wf.get("nodes") or [], ensure_ascii=False),
                json.dumps(wf.get("edges") or [], ensure_ascii=False),
                json.dumps(wf.get("variables") or {}, ensure_ascii=False),
                wf.get("createdAt") or now_iso(),
                wf.get("updatedAt") or now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _workflow_row_to_dict(row: sqlite3.Row) -> dict:
    """把 flow_workflows 行还原为工作流对象。"""
    item = dict(row)
    return {
        "id": item["id"],
        "name": item["name"],
        "description": item["description"] or "",
        "version": int(item["version"] or 1),
        "nodes": json.loads(item["nodes_json"] or "[]"),
        "edges": json.loads(item["edges_json"] or "[]"),
        "variables": json.loads(item["variables_json"] or "{}"),
        "createdAt": item["created_at"],
        "updatedAt": item["updated_at"],
    }


def get_workflow(wf_id: str) -> dict | None:
    """按 id 读取工作流定义，不存在返回 None。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM flow_workflows WHERE id = ?", (wf_id,)
        ).fetchone()
        return _workflow_row_to_dict(row) if row else None
    finally:
        conn.close()


def list_workflows() -> list[dict]:
    """列出全部工作流（按更新时间倒序）。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM flow_workflows ORDER BY updated_at DESC"
        ).fetchall()
        return [_workflow_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def update_workflow(wf_id: str, fields: dict) -> dict | None:
    """按 id 更新工作流的指定字段（name/description/version/nodes/edges/variables），
    返回更新后的定义；不存在返回 None。"""
    # 字段名 → 表列名映射
    col_map = {
        "name": "name",
        "description": "description",
        "version": "version",
        "nodes": "nodes_json",
        "edges": "edges_json",
        "variables": "variables_json",
    }
    valid = [(k, col_map[k]) for k in fields if k in col_map]
    if not valid:
        return get_workflow(wf_id)
    conn = get_connection()
    try:
        set_clauses = ", ".join(f"{col} = ?" for _, col in valid)
        args: list = []
        for key, col in valid:
            value = fields[key]
            if col.endswith("_json"):
                value = json.dumps(value or ([] if col != "variables_json" else {}), ensure_ascii=False)
            args.append(value)
        args.append(now_iso())
        args.append(wf_id)
        conn.execute(
            f"UPDATE flow_workflows SET {set_clauses}, updated_at = ? WHERE id = ?",
            args,
        )
        conn.commit()
        return get_workflow(wf_id)
    finally:
        conn.close()


def delete_workflow(wf_id: str) -> bool:
    """删除工作流，返回是否真的删除；同时删除其运行记录。"""
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM flow_workflows WHERE id = ?", (wf_id,))
        conn.execute("DELETE FROM flow_runs WHERE workflow_id = ?", (wf_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── run CRUD ──────────────────────────────────────────────────

def insert_run(run: dict) -> None:
    """插入或替换一条运行记录（data_json 为完整运行对象）。"""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO flow_runs
               (id, workflow_id, status, input_json, data_json,
                started_at, finished_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run["id"],
                run.get("workflowId", ""),
                run.get("status", "pending"),
                json.dumps(run.get("input") or {}, ensure_ascii=False),
                json.dumps(run, ensure_ascii=False, default=str),
                run.get("startedAt") or "",
                run.get("finishedAt") or "",
                run.get("createdAt") or now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _run_row_to_dict(row: sqlite3.Row) -> dict:
    """把 flow_runs 行还原为完整运行对象（优先 data_json 快照）。"""
    try:
        return json.loads(row["data_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def get_run(run_id: str) -> dict | None:
    """按 id 读取完整运行对象，不存在返回 None。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM flow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return _run_row_to_dict(row) if row else None
    finally:
        conn.close()


def list_runs(workflow_id: str | None = None, limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    """分页列出运行记录（含完整快照）；返回 (列表, 总数)。"""
    conn = get_connection()
    try:
        where = ""
        args: list = []
        if workflow_id:
            where = "WHERE workflow_id = ?"
            args.append(workflow_id)
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM flow_runs {where}", args
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM flow_runs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            args + [int(limit), int(offset)],
        ).fetchall()
        return [_run_row_to_dict(r) for r in rows], int(total)
    finally:
        conn.close()


def update_run(run_id: str, run: dict) -> None:
    """整对象更新运行快照（status/started/finished/data_json）。"""
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE flow_runs
               SET status = ?, data_json = ?, started_at = ?, finished_at = ?
               WHERE id = ?""",
            (
                run.get("status", ""),
                json.dumps(run, ensure_ascii=False, default=str),
                run.get("startedAt") or "",
                run.get("finishedAt") or "",
                run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def create_run_id() -> str:
    """生成运行 id（flow 风格：run_xxx）。"""
    return f"run_{uuid.uuid4().hex[:12]}"


def create_workflow_id() -> str:
    """生成工作流 id（flow 风格：wf_xxx）。"""
    return f"wf_{uuid.uuid4().hex[:12]}"


def create_node_id() -> str:
    """生成节点 id（flow 风格：node_xxx）。"""
    return f"node_{uuid.uuid4().hex[:8]}"


def create_edge_id() -> str:
    """生成边 id（flow 风格：edge_xxx）。"""
    return f"edge_{uuid.uuid4().hex[:8]}"
