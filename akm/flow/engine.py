"""工作流执行引擎（移植自 flow 项目的 engine.ts）。

一期实现：
- 拓扑分层（Kahn 算法，检测环）
- 并行调度（每轮就绪节点并发执行，失败不中断本批）
- 条件边（pass/fail/子串匹配）与 loop 重入边（maxNodeVisits 预算）
- LLM 节点执行（直接复用 akm.proxy.forward_request）
- 事件总线（run 级订阅者队列，供 SSE 推送）
- 持久化（每节点结束写盘）

一期降级策略：pi-agent 节点按 llm 执行并记录提示日志；human 节点自动放行并记录提示日志。
二期接入 pi CLI 编码、人工审批与 worktree 沙箱。
"""

import asyncio
import json
import logging
import re
from collections import deque
from typing import Any

from akm.flow import db as flow_db
from akm.flow import models as flow_models
from akm.agent_runtime.loop import _extract_text_content

logger = logging.getLogger("akm.flow")

# 节点类型（NodeType）
NODE_TYPES = ("intake", "plan", "code", "review", "test", "fix", "human", "router", "merge", "output")

# 节点执行器（NodeExecutor）
CODING_EXECUTORS = ("pi-agent",)


# ── 纯函数（移植自 shared/index.ts）───────────────────────────

def structural_edges(workflow: dict) -> list[dict]:
    """结构边：排除 loop=true 的运行时重入边，保证结构图是 DAG。"""
    return [e for e in workflow.get("edges") or [] if not e.get("loop")]


def max_node_visits(workflow: dict) -> int:
    """loop 重入预算：读 variables.maxNodeVisits，clamp [1,20]，默认 3。"""
    raw = (workflow.get("variables") or {}).get("maxNodeVisits")
    if raw is None:
        return 3
    try:
        n = int(float(str(raw)))
    except (TypeError, ValueError):
        return 3
    return min(max(n, 1), 20)


def topological_layers(workflow: dict) -> list[list[str]]:
    """Kahn 拓扑分层；存在环抛 ValueError。忽略两端不存在的边。"""
    nodes = workflow.get("nodes") or []
    edges = structural_edges(workflow)
    node_ids = {n["id"] for n in nodes}
    indeg = {n["id"]: 0 for n in nodes}
    succ: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if isinstance(s, str) and isinstance(t, str) and s in node_ids and t in node_ids:
            indeg[t] += 1
            succ[s].append(t)
    layers: list[list[str]] = []
    visited: set[str] = set()
    queue = deque(n["id"] for n in nodes if indeg[n["id"]] == 0)
    while queue:
        layer = []
        for _ in range(len(queue)):
            nid = queue.popleft()
            if nid in visited:
                continue
            visited.add(nid)
            layer.append(nid)
            for t in succ.get(nid, []):
                indeg[t] -= 1
                if indeg[t] == 0:
                    queue.append(t)
        if layer:
            layers.append(layer)
    if len(visited) != len(node_ids):
        raise ValueError("Workflow contains a cycle")
    return layers


def render_template(template: str, ctx: dict) -> str:
    """渲染 {{path.to.key}} 模板；缺失返回空串，非字符串 JSON 序列化。"""
    if not template:
        return ""

    def _lookup(path: str) -> Any:
        parts = path.split(".")
        value: Any = ctx
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value

    def _repl(match: re.Match) -> str:
        value = _lookup(match.group(1))
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", _repl, template)


def output_to_text(output: Any) -> str:
    """把节点输出规整为纯文本。"""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        text = output.get("text")
        if isinstance(text, str):
            return text
    return json.dumps(output, ensure_ascii=False)


def parse_artifact_sections(text: str) -> list[dict]:
    """按 /^(#{1,3})\\s+(.+?)\\s*$/ 分节；首个非标题行归入「(前言)」。"""
    sections: list[dict] = []
    current: dict | None = None
    for line in (text or "").splitlines():
        m = re.match(r"^(#{1,3})\s+(.+?)\s*$", line)
        if m:
            current = {"title": m.group(2).strip(), "body": ""}
            sections.append(current)
        elif line.strip():
            if current is None:
                current = {"title": "(前言)", "body": ""}
                sections.append(current)
            current["body"] += ("" if not current["body"] else "\n") + line
        else:
            if current is not None:
                current["body"] += "\n"
    for s in sections:
        s["body"] = s["body"].strip()
    return sections


def parse_conclusion(text: str) -> str:
    """解析结论：pass/fail/unknown。优先匹配「## 结论」块。"""
    block = re.search(r"##\s*结论\s*\n\s*(pass|fail)", text or "", re.IGNORECASE)
    if block:
        return block.group(1).lower()
    has_fail = re.search(r"\bfail\b", text or "", re.IGNORECASE)
    has_pass = re.search(r"\bpass\b", text or "", re.IGNORECASE)
    if has_fail and not has_pass:
        return "fail"
    if has_pass and not has_fail:
        return "pass"
    return "unknown"


def is_fail_conclusion(text: str) -> bool:
    """review_fail 专用：结论为 fail 视为不通过。"""
    block = re.search(r"##\s*结论\s*\n\s*fail", text or "", re.IGNORECASE)
    if block:
        return True
    return bool(re.search(r"\bfail\b", text or "", re.IGNORECASE)) and not re.search(
        r"\bpass\b", text or "", re.IGNORECASE
    )


def extract_mentioned_files(text: str) -> list[str]:
    """提取文本中提到的文件路径（<120 字符，最多 40 个）。"""
    pattern = re.compile(r"\b[\w./-]+\.(?:ts|tsx|js|jsx|py|go|rs|java|rb|php|c|cpp|h|hpp|swift|kt|sql|md|json|yaml|yml|toml|html|css|scss)\b", re.IGNORECASE)
    files: list[str] = []
    seen: set[str] = set()
    for m in pattern.finditer(text or ""):
        path = m.group(0)
        if len(path) >= 120 or path in seen:
            continue
        seen.add(path)
        files.append(path)
        if len(files) >= 40:
            break
    return files


def structure_artifact(text: str, **extra) -> dict:
    """组装结构化产物。"""
    artifact: dict[str, Any] = {
        "text": text or "",
        "conclusion": parse_conclusion(text or ""),
        "sections": parse_artifact_sections(text or ""),
        "files": extract_mentioned_files(text or ""),
    }
    for key in ("fileDiffs", "modelId", "nodeType", "executor"):
        if extra.get(key) is not None:
            artifact[key] = extra[key]
    return artifact


# ── 引擎 ───────────────────────────────────────────────────────

class WorkflowEngine:
    """单实例 DAG 执行引擎。"""

    def __init__(self, app) -> None:
        self.app = app
        # run_id → set[asyncio.Queue]，事件总线订阅者（SSE）
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        # run_id → asyncio.Task，活跃执行任务（cancel 用）
        self._tasks: dict[str, asyncio.Task] = {}
        # run_id → dict，内存中的运行对象（避免频繁读库）
        self._runs: dict[str, dict] = {}

    # ── 事件总线 ──────────────────────────────────────────────

    def subscribe(self, run_id: str) -> asyncio.Queue:
        """订阅某 run 的事件流，返回队列（SSE 端点使用）。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(run_id, set()).add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        """退订事件流。"""
        sinks = self._subscribers.get(run_id)
        if sinks and queue in sinks:
            sinks.discard(queue)
            if not sinks:
                self._subscribers.pop(run_id, None)

    def _emit(self, run_id: str, event: dict) -> None:
        """向 run 的所有订阅者推送事件（非阻塞，满则丢弃防止拖垮引擎）。"""
        for queue in list(self._subscribers.get(run_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # ── 运行管理 ──────────────────────────────────────────────

    def _model_for(self, workflow: dict, node: dict, models: list[dict]) -> dict | None:
        """解析节点模型：精确匹配 → mock 占位 strength hint → 非 mock → [0]。"""
        return flow_models.find_model(models, (node.get("data") or {}).get("modelId", ""))

    def _resolve_executor(self, node: dict) -> str:
        """解析节点执行器（移植自 engine resolveExecutor）。"""
        data = node.get("data") or {}
        raw = data.get("executor")
        if raw in ("codex-cli", "opencode-cli"):
            return "pi-agent"
        if raw in ("llm", "pi-agent", "human", "none"):
            return raw
        ntype = node.get("type")
        if ntype == "human":
            return "human"
        if ntype == "merge":
            return "none"
        if ntype in ("code", "fix", "test"):
            return "pi-agent"
        return "llm"

    def _artifact_key(self, node: dict) -> str:
        """节点产物键（data.artifactKey 或 node.id）。"""
        return (node.get("data") or {}).get("artifactKey") or node.get("id", "")

    def _push_log(self, run: dict, node_id: str, message: str, level: str = "info") -> None:
        """追加节点日志。"""
        nr = run["nodeRuns"].get(node_id)
        if nr is None:
            return
        if len(nr.get("logs") or []) >= 200:
            return
        nr.setdefault("logs", []).append({"ts": flow_db.now_iso(), "level": level, "message": str(message)[:2000]})

    async def _llm_chat(self, model: dict, messages: list[dict]) -> dict:
        """调用 LLM（一期直接复用 AKM 的 forward_request；mock 模型生成假内容）。"""
        if model.get("provider") == "mock":
            return self._mock_chat(model, messages)
        body = {
            "model": model.get("model") or model.get("id"),
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 4096,
            "stream": False,
        }
        http_client = getattr(self.app.state, "http_client", None)
        plugin_manager = getattr(self.app.state, "plugin_manager", None)
        result = await _forward_request(body, http_client, plugin_manager=plugin_manager)
        if result is None:
            raise RuntimeError("LLM 转发不可用（http_client 未初始化）")
        status_code = result.get("status_code") or 0
        response_body = result.get("body") or ""
        if status_code < 200 or status_code >= 300:
            raise RuntimeError(f"LLM 请求失败（{status_code}）：{response_body[:500]}")
        text = _extract_text_content(response_body)
        # 估算 token
        tokens_in = len(json.dumps(messages, ensure_ascii=False)) // 4
        tokens_out = max(len(text) // 4, 1)
        return {"text": text, "tokensIn": tokens_in, "tokensOut": tokens_out}

    def _mock_chat(self, model: dict, messages: list[dict]) -> dict:
        """mock 模型假内容（用于无 key 时的流程演示）。"""
        strengths = model.get("strengths") or []
        last_user = ""
        for m in messages:
            if m.get("role") == "user":
                last_user = m.get("content") or ""
        if "review" in strengths:
            text = (
                "## 审查报告\n\n代码结构清晰，满足需求与方案。未发现阻塞性问题。\n\n"
                "## 结论\npass"
            )
        elif "reason" in strengths:
            text = f"## 方案\n\n1. 模块划分\n2. 接口定义\n3. 实施步骤\n\n基于输入：{last_user[:120]}"
        else:
            text = f"已按流程完成处理。\n\n输入摘要：{last_user[:120]}"
        tokens_in = len(json.dumps(messages, ensure_ascii=False)) // 4
        tokens_out = max(len(text) // 4, 1)
        return {"text": text, "tokensIn": tokens_in, "tokensOut": tokens_out}

    async def start(self, workflow: dict, prompt: str, project_id: str = "", requirement_id: str = "") -> dict:
        """启动一次运行：冻结工作流快照，创建 run 并后台执行。"""
        run_id = flow_db.create_run_id()
        run: dict = {
            "id": run_id,
            "workflowId": workflow["id"],
            "status": "running",
            "input": {"prompt": prompt, "files": []},
            "projectId": project_id or None,
            "requirementId": requirement_id or None,
            "nodeRuns": {},
            "artifacts": {},
            "startedAt": flow_db.now_iso(),
            "finishedAt": "",
            "totals": {"tokensIn": 0, "tokensOut": 0, "costUsd": 0},
            "workflowSnapshot": workflow,
            "pendingHumanNodeId": None,
            "fileDiffs": {},
            "createdAt": flow_db.now_iso(),
        }
        self._runs[run_id] = run
        flow_db.insert_run(run)
        self._emit(run_id, {"type": "run_start", "runId": run_id, "workflowId": workflow["id"]})
        self._tasks[run_id] = asyncio.create_task(self.execute(run, workflow))
        return run

    def get_run(self, run_id: str) -> dict | None:
        """取内存运行对象；无则读库。"""
        run = self._runs.get(run_id)
        if run is not None:
            return run
        return flow_db.get_run(run_id)

    async def cancel(self, run_id: str) -> dict | None:
        """取消运行。"""
        run = self._runs.get(run_id)
        if run is None or run.get("status") in ("succeeded", "failed", "cancelled"):
            return run
        run["status"] = "cancelled"
        run["finishedAt"] = flow_db.now_iso()
        for nr in run.get("nodeRuns", {}).values():
            if nr.get("status") in ("running", "waiting_human"):
                nr["status"] = "cancelled"
                nr["error"] = "cancelled by user"
            elif nr.get("status") == "pending":
                nr["status"] = "skipped"
        # 结束所有未激活节点
        for nid, nr in run.get("nodeRuns", {}).items():
            if nr.get("status") == "pending":
                nr["status"] = "skipped"
                self._emit(run_id, {"type": "node_end", "runId": run_id, "nodeId": nid, "status": "skipped"})
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        self._emit(run_id, {
            "type": "run_end",
            "runId": run_id,
            "status": "cancelled",
            "artifacts": run.get("artifacts", {}),
            "totals": run.get("totals"),
        })
        flow_db.update_run(run_id, run)
        return run

    # ── 调度 ──────────────────────────────────────────────────

    def _initial_progress(self, workflow: dict) -> dict:
        """初始化调度状态：结构入度为 0 的节点进入 activated。"""
        nodes = workflow.get("nodes") or []
        edges = structural_edges(workflow)
        node_ids = {n["id"] for n in nodes}
        indeg = {n["id"]: 0 for n in nodes}
        for e in edges:
            s, t = e.get("source"), e.get("target")
            if s in node_ids and t in node_ids:
                indeg[t] += 1
        return {
            "completed": set(),
            "skipped": set(),
            "activated": {n["id"] for n in nodes if indeg[n["id"]] == 0},
            "visitCount": {},
            "tokensIn": 0,
            "tokensOut": 0,
            "costUsd": 0,
        }

    def _pick_ready(self, workflow: dict, progress: dict, run: dict) -> list[dict]:
        """挑选就绪节点（activated 且未完成/跳过 且非等待/运行 且所有结构前驱完成）。"""
        nodes = workflow.get("nodes") or []
        edges = structural_edges(workflow)
        by_id = {n["id"]: n for n in nodes}
        pred: dict[str, list[str]] = {n["id"]: [] for n in nodes}
        for e in edges:
            s, t = e.get("source"), e.get("target")
            if isinstance(s, str) and isinstance(t, str) and s in by_id and t in by_id:
                pred[t].append(s)
        ready: list[dict] = []
        for n in nodes:
            nid = n["id"]
            nr = run.get("nodeRuns", {}).get(nid)
            if nid not in progress["activated"]:
                continue
            if nid in progress["completed"] or nid in progress["skipped"]:
                continue
            if nr is not None and nr.get("status") in ("waiting_human", "running"):
                continue
            if any(p not in progress["completed"] and p not in progress["skipped"] for p in pred.get(nid, [])):
                continue
            ready.append(n)
        return ready

    def _pick_downstream(self, workflow: dict, source_id: str, output: Any) -> list[str]:
        """挑选下游激活目标（条件边匹配）。"""
        edges = workflow.get("edges") or []
        text = output_to_text(output).lower()
        targets: list[str] = []
        conditioned: list[tuple[str, str]] = []
        for e in edges:
            if e.get("source") != source_id:
                continue
            cond = e.get("condition")
            if not cond:
                targets.append(e["target"])
            else:
                conditioned.append((e["target"], cond.lower()))
        for target, cond in conditioned:
            hit = False
            if cond == "pass":
                # 优先结论块
                block = re.search(r"##\s*结论\s*\n\s*pass", text, re.IGNORECASE)
                if block:
                    hit = True
                else:
                    hit = bool(re.search(r"\bpass\b", text, re.IGNORECASE)) and not re.search(
                        r"##\s*结论\s*\n\s*fail", text, re.IGNORECASE
                    )
            elif cond == "fail":
                block = re.search(r"##\s*结论\s*\n\s*fail", text, re.IGNORECASE)
                if block:
                    hit = True
                else:
                    hit = bool(re.search(r"\bfail\b", text, re.IGNORECASE))
            else:
                hit = cond in text
            if hit:
                targets.append(target)
        # 有条件边但全部未命中 → 只保留无条件边
        if conditioned and not targets:
            targets = [t for e in workflow.get("edges") or [] if e.get("source") == source_id and not e.get("condition") for t in [e["target"]]]
        return targets

    def _activate_downstream(self, workflow: dict, progress: dict, run: dict, source_id: str, output: Any) -> None:
        """激活下游；loop 边或已 completed 目标走重入预算。"""
        limit = max_node_visits(workflow)
        for target in self._pick_downstream(workflow, source_id, output):
            edge = next(
                (e for e in workflow.get("edges") or [] if e.get("source") == source_id and e.get("target") == target),
                None,
            )
            visits = progress["visitCount"].get(target, 0)
            if (edge is not None and edge.get("loop")) or target in progress["completed"]:
                if visits >= limit:
                    self._push_log(run, source_id, f"节点 {target} 已达 maxNodeVisits={limit}，忽略 loop 边", "warn")
                    continue
                progress["completed"].discard(target)
                progress["skipped"].discard(target)
                nr = run.get("nodeRuns", {}).get(target)
                if nr is not None:
                    nr["status"] = "pending"
                    nr.pop("error", None)
                    nr.pop("finishedAt", None)
                    nr.pop("startedAt", None)
                self._push_log(
                    run,
                    source_id,
                    f"loop → {target}（第 {visits + 1} 次访问预算）" if edge and edge.get("loop") else f"重新激活 {target}",
                )
            progress["activated"].add(target)

    # ── 节点执行 ──────────────────────────────────────────────

    async def _execute_node(self, workflow: dict, run: dict, node: dict, models: list[dict], progress: dict) -> None:
        """执行单个节点。"""
        node_id = node["id"]
        run_id = run["id"]
        data = node.get("data") or {}
        ntype = node.get("type")
        executor = self._resolve_executor(node)
        nr = run["nodeRuns"][node_id]

        # 模型解析（human/merge/router 节点可能无需模型，用 models[0] 兜底即可）
        model = self._model_for(workflow, node, models)
        if model is None:
            nr["status"] = "failed"
            nr["error"] = f"未找到可用模型（占位 modelId={data.get('modelId', '')}）"
            run["status"] = "failed"
            self._emit(run_id, {"type": "node_end", "runId": run_id, "nodeId": node_id, "status": "failed", "error": nr["error"]})
            return

        # 访问计数（loop 预算）
        visits = progress["visitCount"].get(node_id, 0) + 1
        progress["visitCount"][node_id] = visits
        visit_limit = max_node_visits(workflow)
        if visits > visit_limit:
            nr["status"] = "failed"
            nr["error"] = f"超过 maxNodeVisits={visit_limit}"
            run["status"] = "failed"
            self._emit(run_id, {"type": "node_end", "runId": run_id, "nodeId": node_id, "status": "failed", "error": nr["error"]})
            return

        nr["status"] = "running"
        nr["modelId"] = model.get("id") or model.get("model", "")
        nr["startedAt"] = flow_db.now_iso()
        self._emit(run_id, {"type": "node_start", "runId": run_id, "nodeId": node_id, "modelId": nr["modelId"]})

        try:
            if ntype == "human":
                # 一期降级：自动放行
                self._push_log(run, node_id, "人工审批节点自动放行（一期暂未开放审批，二期接入）", "warn")
                result_text = data.get("userPromptTemplate") or "approved"
                t_in = t_out = 0
                node_diffs: dict | None = None
            elif ntype == "merge":
                # 汇合节点：收集所有入边源产物
                sources = [
                    e.get("source")
                    for e in workflow.get("edges") or []
                    if e.get("target") == node_id and not e.get("loop")
                ]
                upstream = []
                for src in sources:
                    upstream.append(run["artifacts"].get(self._artifact_key_by_id(workflow, src)) or run["artifacts"].get(src))
                merged = {"merged": upstream}
                run["artifacts"][self._artifact_key(node)] = merged
                run["artifacts"][node_id] = merged
                result_text = json.dumps(merged, ensure_ascii=False)
                t_in = t_out = 0
                node_diffs = None
            elif ntype == "router":
                # 条件路由：拼接上游文本，出边 condition 分流
                sources = [
                    e.get("source")
                    for e in workflow.get("edges") or []
                    if e.get("target") == node_id and not e.get("loop")
                ]
                parts = []
                for src in sources:
                    parts.append(str(run["artifacts"].get(self._artifact_key_by_id(workflow, src)) or run["artifacts"].get(src) or ""))
                result_text = "\n\n".join(p for p in parts if p)
                run["artifacts"][self._artifact_key(node)] = result_text
                run["artifacts"][node_id] = result_text
                t_in = t_out = 0
                node_diffs = None
            else:
                # 渲染 prompt
                ctx = {
                    "input": run["input"],
                    "vars": workflow.get("variables") or {},
                    "artifacts": run["artifacts"],
                }
                system = data.get("systemPrompt") or "You are a helpful assistant."
                user_prompt = render_template(data.get("userPromptTemplate", ""), ctx)
                if executor == "pi-agent":
                    # 一期降级：按 llm 执行
                    self._push_log(run, node_id, "pi-agent 编码执行器二期接入，一期以 LLM 生成代替（不写文件）", "warn")
                if executor == "none":
                    result_text = user_prompt or ""
                    t_in = t_out = 0
                    node_diffs = None
                else:
                    chat_result = await self._llm_chat(
                        model,
                        [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}],
                    )
                    result_text = chat_result.get("text", "")
                    t_in = chat_result.get("tokensIn", 0)
                    t_out = chat_result.get("tokensOut", 0)
                    node_diffs = None

            # 成功收尾
            cost = flow_models.estimate_cost_usd(model, t_in, t_out)
            nr["tokensIn"] = t_in
            nr["tokensOut"] = t_out
            progress["tokensIn"] += t_in
            progress["tokensOut"] += t_out
            progress["costUsd"] += cost
            structured = structure_artifact(
                result_text,
                fileDiffs=node_diffs,
                modelId=model.get("id") or model.get("model"),
                nodeType=ntype,
                executor=executor,
            )
            nr["output"] = {
                "text": result_text,
                "modelId": model.get("id") or model.get("model"),
                "nodeType": ntype,
                "executor": executor,
                "structured": structured,
                "fileDiffs": node_diffs,
            }
            akey = self._artifact_key(node)
            run["artifacts"][akey] = result_text
            run["artifacts"][node_id] = result_text
            run["artifacts"][f"{akey}:meta"] = structured
            if node_diffs:
                run.setdefault("fileDiffs", {})[node_id] = node_diffs
            nr["status"] = "succeeded"
            nr["finishedAt"] = flow_db.now_iso()
            progress["completed"].add(node_id)
            self._emit(run_id, {
                "type": "node_end",
                "runId": run_id,
                "nodeId": node_id,
                "status": "succeeded",
                "output": nr["output"],
            })
            self._activate_downstream(workflow, progress, run, node_id, result_text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("flow 节点执行失败: %s", node_id)
            nr["status"] = "failed"
            nr["error"] = str(exc)[:2000]
            nr["finishedAt"] = flow_db.now_iso()
            run["status"] = "failed"
            self._emit(run_id, {
                "type": "node_end",
                "runId": run_id,
                "nodeId": node_id,
                "status": "failed",
                "error": nr["error"],
            })
        finally:
            # 每节点结束写盘
            run["totals"] = {
                "tokensIn": progress["tokensIn"],
                "tokensOut": progress["tokensOut"],
                "costUsd": round(progress["costUsd"], 6),
            }
            flow_db.update_run(run_id, run)

    def _artifact_key_by_id(self, workflow: dict, node_id: str) -> str:
        """按节点 id 查其 artifactKey。"""
        for n in workflow.get("nodes") or []:
            if n.get("id") == node_id:
                return self._artifact_key(n)
        return node_id

    async def execute(self, run: dict, workflow: dict) -> dict:
        """主调度循环：拓扑 → 并行执行 → 收尾。"""
        run_id = run["id"]
        try:
            topological_layers(workflow)  # 环检测
        except ValueError as exc:
            run["status"] = "failed"
            run["finishedAt"] = flow_db.now_iso()
            for n in workflow.get("nodes") or []:
                nr = run["nodeRuns"].get(n["id"])
                if nr is None:
                    run["nodeRuns"][n["id"]] = {
                        "nodeId": n["id"],
                        "status": "failed",
                        "modelId": (n.get("data") or {}).get("modelId", ""),
                        "logs": [],
                        "error": str(exc),
                    }
            self._emit(run_id, {
                "type": "run_end",
                "runId": run_id,
                "status": "failed",
                "artifacts": run.get("artifacts", {}),
                "totals": run.get("totals"),
            })
            flow_db.update_run(run_id, run)
            return run

        models = flow_models.resolve_model_catalog()
        # 初始化节点运行记录
        for n in workflow.get("nodes") or []:
            run["nodeRuns"][n["id"]] = {
                "nodeId": n["id"],
                "status": "pending",
                "modelId": (n.get("data") or {}).get("modelId", ""),
                "logs": [],
            }
        progress = self._initial_progress(workflow)

        try:
            while run.get("status") not in ("failed", "cancelled"):
                ready = self._pick_ready(workflow, progress, run)
                if not ready:
                    break
                # 每轮并行执行全部就绪节点
                await asyncio.gather(*[self._execute_node(workflow, run, n, models, progress) for n in ready])

            # 收尾：未完成的 pending 节点标记 skipped
            for n in workflow.get("nodes") or []:
                nr = run.get("nodeRuns", {}).get(n["id"])
                if nr is not None and nr.get("status") == "pending":
                    nr["status"] = "skipped"
                    self._emit(run_id, {
                        "type": "node_end",
                        "runId": run_id,
                        "nodeId": n["id"],
                        "status": "skipped",
                    })
            if run.get("status") not in ("failed", "cancelled"):
                run["status"] = "succeeded"
            run["totals"] = {
                "tokensIn": progress["tokensIn"],
                "tokensOut": progress["tokensOut"],
                "costUsd": round(progress["costUsd"], 6),
            }
            run["finishedAt"] = flow_db.now_iso()
            self._emit(run_id, {
                "type": "run_end",
                "runId": run_id,
                "status": run["status"],
                "artifacts": run.get("artifacts", {}),
                "totals": run.get("totals"),
            })
            flow_db.update_run(run_id, run)
        except asyncio.CancelledError:
            if run.get("status") == "cancelled":
                flow_db.update_run(run_id, run)
            raise
        finally:
            self._tasks.pop(run_id, None)
        return run


async def _forward_request(body: dict, http_client, plugin_manager=None) -> dict:
    """薄封装：复用 AKM 的 forward_request。"""
    from akm.proxy import forward_request

    return await forward_request(
        body,
        client=http_client,
        api_path="chat/completions",
        plugin_manager=plugin_manager,
    )
