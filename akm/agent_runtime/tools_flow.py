"""工作流（flow）类 Agent 工具（akm_flow_* 共 8 个）。

从 ``akm/agent_runtime/tools.py`` 的 ``build_builtin_tools`` 里拆出。这些工具
只依赖 ``akm.flow.db`` 的 CRUD 与 ``app.state.flow_engine``，不依赖 tools.py
内的任何 helper。

与拆分前唯一的形式差异：原先这些处理函数是嵌套闭包，直接读取外层 ``app``；
现在是模块级函数，**只有真正用到 app 的函数**才追加显式 ``app`` 参数。
调用契约（工具名、描述、参数 schema、返回结构）逐字未变。

**注册仍受 ``flow_enabled`` 总开关控制**：关闭工作流引擎时整组工具不注册，
该判断保留在 ``build_builtin_tools`` 里，不在本模块。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from functools import partial

from akm.agent_runtime.loop import ToolDef

def list_flow_workflows_tool() -> list[dict[str, Any]]:
    """列出已配置的工作流（akm flow 工作流引擎），返回工作流的 id、名称、
    描述、节点数、边数与更新时间，不含完整定义。"""
    from akm.flow.db import list_workflows

    return [
        {
            "id": wf.get("id", ""),
            "name": wf.get("name", ""),
            "description": wf.get("description", ""),
            "version": wf.get("version", 1),
            "node_count": len(wf.get("nodes") or []),
            "edge_count": len(wf.get("edges") or []),
            "updated_at": wf.get("updatedAt", ""),
        }
        for wf in list_workflows()
    ]


def get_flow_workflow_tool(workflow_id: str) -> dict[str, Any]:
    """按 id 读取一条工作流完整定义（nodes / edges / variables）。"""
    from akm.flow.db import get_workflow

    wf_id = str(workflow_id or "").strip()
    if not wf_id:
        return {"error": "workflow_id 不能为空"}
    wf = get_workflow(wf_id)
    if wf is None:
        return {"error": f"工作流不存在: {wf_id}"}
    return {"workflow": wf}


def save_flow_workflow_tool(
    name: str,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    variables: dict[str, Any] | None = None,
    description: str = "",
    workflow_id: str = "",
) -> dict[str, Any]:
    """创建或更新一条工作流定义。

    workflow_id 留空则创建新工作流；传 id 则更新已有工作流（不存在返回错误）。
    nodes 为节点数组（含 type / data 等），edges 为连线数组（含 source / target
    / condition / loop），variables 为运行变量（如 projectPath / maxNodeVisits）。
    """
    from akm.flow import db as flow_db

    name = str(name or "").strip()
    if not name:
        return {"error": "name 不能为空"}
    wf_id = str(workflow_id or "").strip()
    if wf_id:
        existing = flow_db.get_workflow(wf_id)
        if existing is None:
            return {"error": f"工作流不存在: {wf_id}"}
        updated = flow_db.update_workflow(
            wf_id,
            {
                "name": name,
                "description": str(description or ""),
                "nodes": nodes or [],
                "edges": edges or [],
                "variables": variables or {},
            },
        )
        return {"ok": True, "workflow": updated}
    t = flow_db.now_iso()
    wf: dict[str, Any] = {
        "id": flow_db.create_workflow_id(),
        "name": name,
        "description": str(description or ""),
        "version": 1,
        "nodes": nodes or [],
        "edges": edges or [],
        "variables": variables or {},
        "createdAt": t,
        "updatedAt": t,
    }
    flow_db.insert_workflow(wf)
    return {"ok": True, "workflow": flow_db.get_workflow(wf["id"])}


def delete_flow_workflow_tool(workflow_id: str) -> dict[str, Any]:
    """删除一条工作流及其全部运行记录，返回是否删除成功。"""
    from akm.flow.db import delete_workflow

    wf_id = str(workflow_id or "").strip()
    if not wf_id:
        return {"error": "workflow_id 不能为空"}
    if not delete_workflow(wf_id):
        return {"error": f"工作流不存在: {wf_id}"}
    return {"deleted": True}


async def run_flow_workflow_tool(workflow_id: str, prompt: str, variables: dict | None = None, *, app: FastAPI) -> dict[str, Any]:
    """启动一次工作流运行并返回运行对象（含 run id 与初始状态）。

    运行在后台执行，可通过 akm_flow_runs 查询进度与结果。
    variables 为本次运行的临时变量（如 projectPath / language），覆盖工作流默认 variables。
    """
    from akm.flow.db import get_workflow

    wf_id = str(workflow_id or "").strip()
    prompt = str(prompt or "").strip()
    if not wf_id:
        return {"error": "workflow_id 不能为空"}
    if not prompt:
        return {"error": "prompt 不能为空"}
    wf = get_workflow(wf_id)
    if wf is None:
        return {"error": f"工作流不存在: {wf_id}"}
    engine = getattr(app.state, "flow_engine", None)
    if engine is None:
        return {"error": "flow 引擎未初始化"}
    try:
        run = await engine.start(wf, prompt, variables=variables or None)
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "ok": True,
        "run": {
            "id": run.get("id", ""),
            "workflowId": run.get("workflowId", ""),
            "status": run.get("status", ""),
            "input": run.get("input", {}),
        },
    }


def list_flow_runs_tool(
    workflow_id: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """列出工作流运行记录（按创建时间倒序），返回运行 id、状态、起止时间与
    token 用量摘要；可按 workflow_id 过滤，limit 最大 100。"""
    from akm.flow.db import list_runs

    runs, total = list_runs(
        str(workflow_id or "").strip() or None,
        limit=max(1, min(int(limit or 20), 100)),
        offset=max(int(offset or 0), 0),
    )
    return {
        "total": total,
        "runs": [
            {
                "id": r.get("id", ""),
                "workflowId": r.get("workflowId", ""),
                "status": r.get("status", ""),
                "input": r.get("input", {}),
                "totals": r.get("totals", {}),
                "startedAt": r.get("startedAt", ""),
                "finishedAt": r.get("finishedAt", ""),
            }
            for r in runs
        ],
    }


def get_flow_run_tool(run_id: str, *, app: FastAPI) -> dict[str, Any]:
    """按 id 查询一次工作流运行的节点级详情，用于定位卡住或失败的节点。

    返回各节点状态、错误信息、token 用量、文件改动数与最近日志（截断），
    以及运行级 prompt / variables / totals。
    """
    engine = getattr(app.state, "flow_engine", None)
    if engine is None:
        return {"error": "flow 引擎未初始化"}
    run_id = str(run_id or "").strip()
    if not run_id:
        return {"error": "run_id 不能为空"}
    run = engine.get_run(run_id)
    if run is None:
        return {"error": f"运行不存在: {run_id}"}
    nodes = (run.get("workflowSnapshot") or {}).get("nodes") or []
    by_id = {n.get("id", ""): n for n in nodes}
    node_summaries: list[dict[str, Any]] = []
    from akm.flow.engine import output_to_text

    for nid, nr in (run.get("nodeRuns") or {}).items():
        node = by_id.get(nid, {})
        data = node.get("data") or {}
        logs = nr.get("logs") or []
        # 节点输出摘要：正文截断，结构化产物保留 conclusion/sections/files
        output = nr.get("output") or {}
        structured = output.get("structured") or {}
        node_summaries.append(
            {
                "id": nid,
                "label": data.get("label") or node.get("type") or nid,
                "type": node.get("type", ""),
                "executor": data.get("executor", ""),
                "status": nr.get("status", ""),
                "error": str(nr.get("error") or "")[:500],
                "tokensIn": nr.get("tokensIn") or 0,
                "tokensOut": nr.get("tokensOut") or 0,
                "fileDiffs": len(nr.get("fileDiffs") or []),
                "outputText": str(output_to_text(output) or "")[:2000],
                "structured": {
                    "conclusion": structured.get("conclusion", ""),
                    "sections": [
                        {
                            "title": s.get("title", ""),
                            "body": str(s.get("body") or "")[:500],
                        }
                        for s in (structured.get("sections") or [])
                    ],
                    "files": (structured.get("files") or [])[:20],
                },
                "logs": [str(l.get("message") or "")[:300] for l in logs[-5:]],
            }
        )
    run_input = run.get("input") or {}
    return {
        "run": {
            "id": run_id,
            "workflowId": run.get("workflowId", ""),
            "status": run.get("status", ""),
            "pendingHumanNodeId": run.get("pendingHumanNodeId"),
            "prompt": str(run_input.get("prompt") or "")[:500],
            "variables": run_input.get("variables") or {},
            "totals": run.get("totals", {}),
            "startedAt": run.get("startedAt", ""),
            "finishedAt": run.get("finishedAt", ""),
            "nodes": node_summaries,
        }
    }


async def cancel_flow_run_tool(run_id: str, *, app: FastAPI) -> dict[str, Any]:
    """中止一次工作流运行（取消）。

    正在执行的节点会中断，待执行的节点跳过，运行整体置为 cancelled。
    已结束或已取消的运行返回其当前状态而不报错；不存在的运行返回 error。
    """
    engine = getattr(app.state, "flow_engine", None)
    if engine is None:
        return {"error": "flow 引擎未初始化"}
    run_id = str(run_id or "").strip()
    if not run_id:
        return {"error": "run_id 不能为空"}
    run = await engine.cancel(run_id)
    if run is None:
        return {"error": f"运行不存在: {run_id}"}
    return {
        "ok": True,
        "run": {
            "id": run.get("id", run_id),
            "workflowId": run.get("workflowId", ""),
            "status": run.get("status", "cancelled"),
            "finishedAt": run.get("finishedAt", ""),
        },
    }


def build_flow_tools(app: FastAPI) -> list[ToolDef]:
    """装配工作流类工具的 ToolDef 列表（调用方负责 flow_enabled 开关判断）。"""
    return [
        ToolDef(
            "akm_flow_list",
            "列出已配置的工作流（akm flow 工作流引擎）：返回工作流的 id、名称、描述、节点数与更新时间，不含完整定义",
            {
                "type": "object",
                "properties": {},
                "required": [],
            },
            list_flow_workflows_tool,
        ),
        ToolDef(
            "akm_flow_get",
            "按 id 读取一条工作流的完整定义（nodes / edges / variables），供检查流程结构或复制修改",
            {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "工作流 id（来自 akm_flow_list）"},
                },
                "required": ["workflow_id"],
            },
            get_flow_workflow_tool,
        ),
        ToolDef(
            "akm_flow_save",
            "创建或更新一条工作流定义：workflow_id 留空则新建，传 id 则更新已有工作流。nodes 为节点数组（含 type / label / data）、edges 为连线数组（含 source / target / condition / loop）、variables 为运行变量（如 projectPath / maxNodeVisits）",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "工作流名称"},
                    "nodes": {"type": "array", "description": "节点数组，每项含 type（intake/plan/code/review/test/fix/human/router/merge/output）与 data（label/modelId/systemPrompt/userPromptTemplate 等）"},
                    "edges": {"type": "array", "description": "连线数组，每项含 source / target（节点 id），可选 condition（pass/fail/子串）与 loop"},
                    "variables": {"type": "object", "description": "运行变量，如 projectPath / maxNodeVisits / useWorktree"},
                    "description": {"type": "string", "description": "工作流描述，可选"},
                    "workflow_id": {"type": "string", "description": "留空创建新工作流；传已有 id 则更新该工作流"},
                },
                "required": ["name"],
            },
            save_flow_workflow_tool,
        ),
        ToolDef(
            "akm_flow_delete",
            "删除一条工作流及其全部运行记录，返回是否删除成功",
            {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "工作流 id（来自 akm_flow_list）"},
                },
                "required": ["workflow_id"],
            },
            delete_flow_workflow_tool,
        ),
        ToolDef(
            "akm_flow_run",
            "启动一次工作流运行：传入工作流 id 与用户提示词 prompt，后台执行 DAG（LLM 节点 / 条件分支 / 循环重入 / 并行），返回运行 id 与初始状态；可用 akm_flow_runs 查询进度",
            {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "工作流 id（来自 akm_flow_list）"},
                    "prompt": {"type": "string", "description": "用户提示词，作为工作流输入注入 {{input.prompt}}"},
                    "variables": {"type": "object", "description": "本次运行的临时变量（如 projectPath / language），覆盖工作流默认 variables，仅本次生效，不修改工作流定义"},
                },
                "required": ["workflow_id", "prompt"],
            },
            partial(run_flow_workflow_tool, app=app),
        ),
        ToolDef(
            "akm_flow_runs",
            "列出工作流运行记录（按创建时间倒序）：返回运行 id、状态、输入摘要、token 用量与起止时间；可按 workflow_id 过滤",
            {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "description": "按工作流 id 过滤，留空返回全部"},
                    "limit": {"type": "integer", "description": "返回条数，默认 20，最大 100"},
                    "offset": {"type": "integer", "description": "分页偏移，默认 0"},
                },
                "required": [],
            },
            list_flow_runs_tool,
        ),
        ToolDef(
            "akm_flow_run_get",
            "按 id 查询一次工作流运行的节点级详情：返回各节点状态、错误、token 用量与最近日志，用于定位运行卡住或失败的节点",
            {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "运行 id（来自 akm_flow_runs / akm_flow_run）"},
                },
                "required": ["run_id"],
            },
            partial(get_flow_run_tool, app=app),
        ),
        ToolDef(
            "akm_flow_cancel",
            "中止一次工作流运行（取消）：正在执行的节点中断、待执行的节点跳过，运行置为 cancelled；对卡住或不想再等的运行使用",
            {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "运行 id（来自 akm_flow_runs / akm_flow_run）"},
                },
                "required": ["run_id"],
            },
            partial(cancel_flow_run_tool, app=app),
        ),
    ]
