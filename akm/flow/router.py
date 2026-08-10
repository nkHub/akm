"""/v1/flow 路由：工作流 CRUD、内置模板、运行管理、SSE 事件流。

鉴权复用 agent_runtime 的 _check_agent_auth（配置了 agent_api_token 时校验）。
"""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from akm.agent_runtime.router import _check_agent_auth
from akm.flow import db as flow_db
from akm.flow import templates as flow_templates
from akm.flow.engine import WorkflowEngine

router = APIRouter(prefix="/v1/flow", tags=["flow"])


def _engine(request: Request) -> WorkflowEngine:
    """获取单例引擎（server lifespan 中注入 app.state.flow_engine）。"""
    engine = getattr(request.app.state, "flow_engine", None)
    if engine is None:
        engine = WorkflowEngine(request.app)
        request.app.state.flow_engine = engine
    return engine


def _check(request: Request):
    """鉴权；返回 None 表示放行，否则返回 JSONResponse（已含错误）。"""
    return _check_agent_auth(request)


# ── 健康检查 / 模型目录 ───────────────────────────────────────

@router.get("/health")
async def health(request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    return {"ok": True, "engine": "flow"}


@router.get("/models")
async def list_models(request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    from akm.flow import models as flow_models

    return {"models": flow_models.resolve_model_catalog()}


# ── 工作流 CRUD ───────────────────────────────────────────────

@router.get("/workflows")
async def list_workflows(request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    return {"workflows": flow_db.list_workflows()}


@router.post("/workflows")
async def create_workflow(request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"detail": "name 不能为空"}, status_code=400)
    wf: dict = {
        "id": flow_db.create_workflow_id(),
        "name": name,
        "description": body.get("description", ""),
        "version": 1,
        "nodes": body.get("nodes") or [],
        "edges": body.get("edges") or [],
        "variables": body.get("variables") or {},
        "createdAt": flow_db.now_iso(),
        "updatedAt": flow_db.now_iso(),
    }
    flow_db.insert_workflow(wf)
    return JSONResponse({"workflow": flow_db.get_workflow(wf["id"])}, status_code=201)


@router.get("/workflows/{wf_id}")
async def get_workflow(wf_id: str, request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    wf = flow_db.get_workflow(wf_id)
    if wf is None:
        return JSONResponse({"detail": "工作流不存在"}, status_code=404)
    return {"workflow": wf}


@router.put("/workflows/{wf_id}")
async def update_workflow(wf_id: str, request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    body = await request.json()
    updated = flow_db.update_workflow(wf_id, body)
    if updated is None:
        return JSONResponse({"detail": "工作流不存在"}, status_code=404)
    return {"workflow": updated}


@router.delete("/workflows/{wf_id}")
async def delete_workflow(wf_id: str, request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    if not flow_db.delete_workflow(wf_id):
        return JSONResponse({"detail": "工作流不存在"}, status_code=404)
    return {"ok": True}


# ── 内置模板 ──────────────────────────────────────────────────

@router.get("/templates")
async def list_templates(request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    # 返回不含 id 的模板定义（实例化时重新生成 id）
    safe = []
    for t in flow_templates.TEMPLATES:
        copy = json.loads(json.dumps(t))
        copy.pop("id", None)
        copy.pop("createdAt", None)
        copy.pop("updatedAt", None)
        safe.append(copy)
    return {"templates": safe}


@router.post("/templates/{template_id}/instantiate")
async def instantiate_template(template_id: str, request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    # 按模板名或序号匹配
    names = [t["name"] for t in flow_templates.TEMPLATES]
    if template_id.isdigit():
        idx = int(template_id)
        if idx < 0 or idx >= len(flow_templates.TEMPLATES):
            return JSONResponse({"detail": "模板不存在"}, status_code=404)
        template = flow_templates.TEMPLATES[idx]
    else:
        template = next(
            (t for t in flow_templates.TEMPLATES if t["name"] == template_id or t.get("id") == template_id),
            None,
        )
        if template is None:
            return JSONResponse({"detail": "模板不存在"}, status_code=404)
    wf = _instantiate(template)
    flow_db.insert_workflow(wf)
    return JSONResponse({"workflow": flow_db.get_workflow(wf["id"])}, status_code=201)


def _instantiate(template: dict) -> dict:
    """实例化模板：重新生成所有 id（节点/边/工作流）。"""
    id_map: dict[str, str] = {}
    nodes = []
    for n in template.get("nodes") or []:
        old = n["id"]
        new = flow_db.create_node_id()
        id_map[old] = new
        node = json.loads(json.dumps(n))
        node["id"] = new
        nodes.append(node)
    edges = []
    for e in template.get("edges") or []:
        edge = json.loads(json.dumps(e))
        edge["id"] = flow_db.create_edge_id()
        edge["source"] = id_map.get(e["source"], e["source"])
        edge["target"] = id_map.get(e["target"], e["target"])
        edges.append(edge)
    t = flow_db.now_iso()
    return {
        "id": flow_db.create_workflow_id(),
        "name": template.get("name", ""),
        "description": template.get("description", ""),
        "version": 1,
        "nodes": nodes,
        "edges": edges,
        "variables": json.loads(json.dumps(template.get("variables") or {})),
        "createdAt": t,
        "updatedAt": t,
    }


# ── 运行管理 ──────────────────────────────────────────────────

@router.post("/workflows/{wf_id}/runs")
async def start_run(wf_id: str, request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    wf = flow_db.get_workflow(wf_id)
    if wf is None:
        return JSONResponse({"detail": "工作流不存在"}, status_code=404)
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"detail": "prompt 不能为空"}, status_code=400)
    engine = _engine(request)
    run = await engine.start(
        wf,
        prompt,
        project_id=body.get("projectId") or "",
        requirement_id=body.get("requirementId") or "",
    )
    return JSONResponse({"run": run}, status_code=201)


@router.get("/runs")
async def list_runs(request: Request, workflow_id: str | None = None, limit: int = 100, offset: int = 0):
    blocked = _check(request)
    if blocked:
        return blocked
    runs, total = flow_db.list_runs(workflow_id, limit=min(max(limit, 1), 500), offset=max(offset, 0))
    return {"runs": runs, "total": total}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    run = _engine(request).get_run(run_id)
    if run is None:
        return JSONResponse({"detail": "运行不存在"}, status_code=404)
    return {"run": run}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request):
    blocked = _check(request)
    if blocked:
        return blocked
    run = await _engine(request).cancel(run_id)
    if run is None:
        return JSONResponse({"detail": "运行不存在"}, status_code=404)
    return {"run": run}


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    """SSE 事件流：先推 snapshot，再转发引擎事件；终态补发 run_end。"""
    blocked = _check(request)
    if blocked:
        return blocked
    engine = _engine(request)
    run = engine.get_run(run_id)
    if run is None:
        return JSONResponse({"detail": "运行不存在"}, status_code=404)

    async def event_stream():
        queue = engine.subscribe(run_id)
        try:
            # 初始快照
            yield f"event: snapshot\ndata: {json.dumps({'type': 'snapshot', 'run': run}, ensure_ascii=False)}\n\n"
            # 若已终态，直接补发 run_end 并关闭
            if run.get("status") in ("succeeded", "failed", "cancelled"):
                yield f"event: run_end\ndata: {json.dumps({'type': 'run_end', 'runId': run_id, 'status': run['status'], 'artifacts': run.get('artifacts', {}), 'totals': run.get('totals')}, ensure_ascii=False)}\n\n"
                return
            # 等待人工审批时重发 human_wait
            if run.get("status") == "waiting_human":
                yield f"event: human_wait\ndata: {json.dumps({'type': 'human_wait', 'runId': run_id, 'nodeId': run.get('pendingHumanNodeId', ''), 'message': '等待人工审批'}, ensure_ascii=False)}\n\n"
            # 转发引擎事件
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # 心跳 + 轮询终态（支持外部进程终结）
                    yield "event: ping\ndata: {}\n\n"
                    current = engine.get_run(run_id)
                    if current is not None and current.get("status") in ("succeeded", "failed", "cancelled"):
                        yield f"event: run_end\ndata: {json.dumps({'type': 'run_end', 'runId': run_id, 'status': current['status'], 'artifacts': current.get('artifacts', {}), 'totals': current.get('totals')}, ensure_ascii=False)}\n\n"
                        return
                    continue
                if event.get("type") == "run_end":
                    yield f"event: run_end\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    return
                yield f"event: {event.get('type', 'event')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            engine.unsubscribe(run_id, queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
