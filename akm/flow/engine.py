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
import os
import re
from collections import deque
from typing import Any

from akm.flow import db as flow_db
from akm.flow import models as flow_models
from akm.flow import pi_runner
from akm.flow import workspace_diff
from akm.flow import worktree as worktree_mod
from akm.flow.path_lock import acquire_path_lock, release_path_locks_for_run
from akm.agent_runtime.loop import _extract_text_content

logger = logging.getLogger("akm.flow")

# 节点类型（NodeType）
NODE_TYPES = ("intake", "plan", "code", "review", "test", "fix", "human", "router", "merge", "output")

# 节点执行器（NodeExecutor）
CODING_EXECUTORS = ("pi-agent",)

# LLM 节点对上游瞬时 5xx 的自动重试（总尝试次数 = _LLM_RETRY_MAX + 1，指数退避）
_LLM_RETRY_MAX = 2
_LLM_RETRY_BASE_DELAY = 1.0


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
        # run_id → asyncio.Future[dict]，human 审批等待槽位（每 run 单槽）
        self._human_waits: dict[str, asyncio.Future] = {}

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
        """调用 LLM（一期直接复用 AKM 的 forward_request；mock 模型生成假内容）。

        每次真实 LLM 调用都会写入审计日志（来源标记为 flow），便于与
        /v1/chat/completions（无来源标记）和 /v1/agent（chat/task）区分。

        对上游瞬时 5xx（网关时段性故障）做自动重试（指数退避），减少工作流
        因单次瞬时故障整次失败；4xx（请求本身问题）不重试，直接失败。
        """
        if model.get("provider") == "mock":
            return self._mock_chat(model, messages)
        # 运行参数从 config.json 的 agent_flow 读取（重试次数/退避/请求参数），
        # 缺省用模块级默认值，便于按机器/场景调整而不改代码
        from akm.config import load_config
        _flow_cfg = load_config().get("agent_flow") or {}
        try:
            llm_retry_max = int(_flow_cfg.get("llm_retry_max") or _LLM_RETRY_MAX)
        except (TypeError, ValueError):
            llm_retry_max = _LLM_RETRY_MAX
        try:
            llm_retry_base_delay = float(_flow_cfg.get("llm_retry_base_delay") or _LLM_RETRY_BASE_DELAY)
        except (TypeError, ValueError):
            llm_retry_base_delay = _LLM_RETRY_BASE_DELAY
        try:
            temperature = float(_flow_cfg.get("llm_temperature") or 0.3)
        except (TypeError, ValueError):
            temperature = 0.3
        try:
            max_tokens = int(_flow_cfg.get("llm_max_tokens") or 4096)
        except (TypeError, ValueError):
            max_tokens = 4096
        body = {
            "model": model.get("model") or model.get("id"),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        http_client = getattr(self.app.state, "http_client", None)
        plugin_manager = getattr(self.app.state, "plugin_manager", None)
        for attempt in range(llm_retry_max + 1):
            result = await _forward_request(body, http_client, plugin_manager=plugin_manager)
            if result is None:
                raise RuntimeError("LLM 转发不可用（http_client 未初始化）")
            status_code = result.get("status_code") or 0
            response_body = result.get("body") or ""
            if 200 <= status_code < 300:
                text = _extract_text_content(response_body)
                # 估算 token
                tokens_in = len(json.dumps(messages, ensure_ascii=False)) // 4
                tokens_out = max(len(text) // 4, 1)
                await self._submit_audit(
                    body, result, status_code, response_body,
                    prompt_tokens=tokens_in, completion_tokens=tokens_out,
                )
                return {"text": text, "tokensIn": tokens_in, "tokensOut": tokens_out}
            # 仅对 5xx 做自动重试（上游瞬时故障）；其余状态（4xx 等）直接失败
            if status_code >= 500 and attempt < llm_retry_max:
                retry_delay = llm_retry_base_delay * (2 ** attempt)
                logger.warning(
                    "[Flow] LLM 请求失败（%s），%.1fs 后重试（第 %d 次，共 %d 次）：%s",
                    status_code, retry_delay, attempt + 1, llm_retry_max + 1,
                    (response_body or "")[:200],
                )
                await asyncio.sleep(retry_delay)
                continue
            error_msg = f"LLM 请求失败（{status_code}）：{response_body[:500]}"
            await self._submit_audit(body, result, status_code, response_body, error=error_msg)
            raise RuntimeError(error_msg)
        # 兜底：循环内所有路径均已 return/raise，不应到达此处
        raise RuntimeError("LLM 请求失败（重试次数耗尽）")

    async def _submit_audit(
        self,
        body: dict,
        result: dict,
        status_code: int,
        response_body: str,
        error: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """把 flow 引擎的一次 LLM 调用写入审计日志，来源标记为 flow。

        复用 server 的审计提交链路（_submit_audit_log → AuditLogQueue 或
        直接写 DB），请求/响应体是否落库遵循 log_request_body / log_response_body
        配置，与转发请求的审计行为保持一致。
        """
        try:
            from akm.config import load_config
            from akm.server import _submit_audit_log

            cfg = load_config()
            save_request_body = bool(cfg.get("log_request_body", False))
            save_response_body = bool(cfg.get("log_response_body", False))
            resp_for_log = ""
            if save_response_body:
                resp_for_log = response_body
                if len(resp_for_log) > 64000:
                    resp_for_log = resp_for_log[:32000] + f"\n...(截断，共 {len(resp_for_log)} 字符)" + resp_for_log[-32000:]
            req_body_for_log = json.dumps(body, ensure_ascii=False) if save_request_body else ""
            await _submit_audit_log(self.app, {
                "provider": str(result.get("provider", "") or ""),
                "key_alias": str(result.get("key_alias", "") or ""),
                # model 优先取请求体里的实际模型；失败路径 result.get("model")
                # 可能是 key 的 models 匹配列表（逗号拼接），不应写入审计
                "model": str(body.get("model", "") or result.get("model", "") or ""),
                "request_body": req_body_for_log,
                "response_body": resp_for_log,
                "status_code": status_code,
                "latency_ms": int(result.get("latency_ms", 0) or 0),
                "error": error,
                "request_headers": json.dumps({"user-agent": "akm-flow/1.0", "x-akm-source": "flow"}),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            })
        except Exception:
            logger.warning("[Flow] 审计日志写入失败", exc_info=True)

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

    # ── human 审批 ────────────────────────────────────────────

    def _wait_for_human(self, run: dict, node_id: str, message: str = "") -> asyncio.Future:
        """挂起当前 run 等待人工审批；每 run 单槽位（已有等待则 reject 新的）。"""
        run_id = run["id"]
        # 自动放行开关：config.json 的 agent_flow.human_auto_approve（默认 true，
        # 兼容迁移期顶层 flow_human_auto_approve）时 human 节点不挂起，
        # 直接返回已批准的 future，保证模板工作流可无人工干预跑通。
        from akm.config import load_config
        _cfg = load_config()
        human_auto = (_cfg.get("agent_flow") or {}).get("human_auto_approve", _cfg.get("flow_human_auto_approve", True))
        if human_auto:
            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            fut.set_result({"action": "approve", "note": "auto", "nodeId": node_id})
            return fut
        existing = self._human_waits.get(run_id)
        if existing is not None and not existing.done():
            fut = asyncio.get_event_loop().create_future()
            fut.set_exception(RuntimeError("already waiting on another human node"))
            return fut
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._human_waits[run_id] = fut
        nr = run["nodeRuns"].get(node_id)
        if nr is not None:
            nr["status"] = "waiting_human"
        run["status"] = "waiting_human"
        run["pendingHumanNodeId"] = node_id
        self._push_log(run, node_id, message or "等待人工审批", "info")
        self._emit(run_id, {
            "type": "human_wait",
            "runId": run_id,
            "nodeId": node_id,
            "message": message or "等待人工审批",
        })
        flow_db.update_run(run_id, run)
        return fut

    async def resume(self, run_id: str, decision: dict, node_id: str | None = None) -> dict:
        """人工审批续跑（approve/reject）。进程内实时分支 + 重启后 durable 分支。"""
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("运行不存在")
        if run.get("status") != "waiting_human":
            raise ValueError("运行不在等待人工审批状态")
        pending_id = run.get("pendingHumanNodeId") or ""
        if node_id and node_id != pending_id:
            raise ValueError("nodeId 与等待审批的节点不一致")
        if not pending_id:
            raise ValueError("未记录等待审批的节点")

        action = (decision or {}).get("action") or ""
        if action not in ("approve", "reject"):
            raise ValueError("action 必须是 approve 或 reject")
        note = (decision.get("note") or "").strip()

        # 进程内实时分支：唤醒等待中的 future，由 execute 继续处理
        wait = self._human_waits.get(run_id)
        if wait is not None and not wait.done():
            wait.set_result({"action": action, "note": note, "nodeId": pending_id})
            return run

        # durable 分支（重启后）：直接应用决策并重建调度
        wf = run.get("workflowSnapshot")
        if not wf:
            raise ValueError("运行缺少工作流快照，无法续跑")
        self._apply_human_decision(run, pending_id, {"action": action, "note": note}, wf)
        if action == "reject":
            run["status"] = "failed"
            run["finishedAt"] = flow_db.now_iso()
            self._emit(run_id, {
                "type": "run_end",
                "runId": run_id,
                "status": "failed",
                "artifacts": run.get("artifacts", {}),
                "totals": run.get("totals"),
            })
            flow_db.update_run(run_id, run)
            return run
        # approve：重建进度，激活下游并重新执行
        run["status"] = "running"
        run["pendingHumanNodeId"] = None
        progress = self._reconstruct_progress(run, wf)
        progress["completed"].add(pending_id)
        nr = run["nodeRuns"].get(pending_id) or {}
        for target in self._pick_downstream(wf, pending_id, nr.get("output") or "approved"):
            progress["activated"].add(target)
        run["nodeRuns"][pending_id] = nr
        flow_db.update_run(run_id, run)
        self._tasks[run_id] = asyncio.create_task(self.execute(run, wf, progress=progress))
        return run

    def _apply_human_decision(self, run: dict, gate_node_id: str, decision: dict, workflow: dict) -> None:
        """应用人工决策到节点与产物（approve/reject 共用）。"""
        action = decision.get("action") or "approve"
        note = (decision.get("note") or "").strip() or ("approved" if action == "approve" else "人工驳回")
        nr = run["nodeRuns"].get(gate_node_id)
        if nr is None:
            return
        node = next((n for n in workflow.get("nodes") or [] if n.get("id") == gate_node_id), None)
        if action == "reject":
            nr["status"] = "failed"
            nr["error"] = note[:2000]
            nr["output"] = {"decision": "reject", "note": note}
            self._push_log(run, gate_node_id, note, "warn")
            self._emit(run["id"], {
                "type": "node_end", "runId": run["id"], "nodeId": gate_node_id,
                "status": "failed", "error": note, "output": nr["output"],
            })
        else:
            nr["status"] = "succeeded"
            nr["output"] = {"decision": "approve", "note": note}
            self._push_log(run, gate_node_id, note, "info")
            self._emit(run["id"], {
                "type": "node_end", "runId": run["id"], "nodeId": gate_node_id,
                "status": "succeeded", "output": nr["output"],
            })
        # 写审批结果到 artifacts（下游可引用）
        key = self._artifact_key(node) if node else gate_node_id
        run["artifacts"][key] = note
        run["artifacts"][gate_node_id] = note

    def _reconstruct_progress(self, run: dict, workflow: dict) -> dict:
        """重启后重建调度进度（completed/skipped/visitCount/token 累加）。"""
        progress = self._initial_progress(workflow)
        progress["completed"] = {
            nid for nid, nr in run.get("nodeRuns", {}).items() if nr.get("status") == "succeeded"
        }
        progress["skipped"] = {
            nid for nid, nr in run.get("nodeRuns", {}).items() if nr.get("status") == "skipped"
        }
        for nid, nr in run.get("nodeRuns", {}).items():
            if nr.get("status") == "succeeded":
                progress["visitCount"][nid] = max(progress["visitCount"].get(nid, 0), 1)
            progress["tokensIn"] += nr.get("tokensIn") or 0
            progress["tokensOut"] += nr.get("tokensOut") or 0
        totals = run.get("totals") or {}
        progress["costUsd"] = totals.get("costUsd") or 0
        # 保留 waiting_human 节点在 activated
        for nid, nr in run.get("nodeRuns", {}).items():
            if nr.get("status") == "waiting_human":
                progress["activated"].add(nid)
        return progress

    # ── pi-agent 编码执行 ─────────────────────────────────────

    async def _run_pi_coding_node(self, workflow: dict, run: dict, node: dict, models: list[dict]) -> dict:
        """执行 pi-agent 编码节点：解析 cwd（worktree 策略）→ 加锁 →
        快照 → 跑 pi → 差异对比。返回 {text, tokensIn, tokensOut, fileDiffs}。"""
        node_id = node["id"]
        run_id = run["id"]
        variables = workflow.get("variables") or {}
        project_path = variables.get("projectPath") or "."
        # projectPath 相对路径 → 基于工作区根（agent_workspace_root）解析为绝对路径；
        # 未配置工作区时回退到进程当前目录（保持开发态行为）
        if not os.path.isabs(project_path):
            from akm.config import load_config
            workspace_root = str(load_config().get("agent_workspace_root") or "").strip()
            base = os.path.abspath(os.path.expanduser(workspace_root)) if workspace_root else os.getcwd()
            project_path = os.path.abspath(os.path.join(base, project_path))
        system = (node.get("data") or {}).get("systemPrompt") or "You are a coding agent."
        ctx = {
            "input": run["input"],
            "vars": variables,
            "artifacts": run["artifacts"],
        }
        user_prompt = render_template((node.get("data") or {}).get("userPromptTemplate", ""), ctx)

        # 解析 worktree 策略
        existing_wt = run.get("worktrees")
        coding = worktree_mod.resolve_coding_cwd(run_id, node_id, project_path, variables, existing_wt)
        cwd = coding["cwd"]
        if coding["state"]:
            run["worktrees"] = coding["state"]
            flow_db.update_run(run_id, run)
        self._push_log(run, node_id, coding["note"], "info")

        # 项目路径互斥锁（同一目录并发编码节点串行化）
        release_lock = await acquire_path_lock(cwd, run_id, node_id)
        try:
            before = workspace_diff.snapshot_workspace(cwd)
            model = self._model_for(workflow, node, models)
            model_dict = model if model else None
            result = await pi_runner.run_pi_agent({
                "cwd": cwd,
                "systemPrompt": system,
                "userPrompt": user_prompt,
                "model": model_dict,
                "on_log": lambda msg, level="info": self._push_log(run, node_id, msg, level),
                "on_token": lambda text: self._emit(run_id, {
                    "type": "token", "runId": run_id, "nodeId": node_id, "text": text,
                }),
            })
            node_diffs = workspace_diff.diff_workspace(cwd, before)
        finally:
            release_lock()
            if coding["state"] and not worktree_mod.workflow_keep_worktree(variables):
                # 结束时不立即清理（run 级收尾统一清理），此处仅记录
                pass
        return {
            "text": result.get("text", ""),
            "tokensIn": result.get("tokensIn", 0),
            "tokensOut": result.get("tokensOut", 0),
            "fileDiffs": node_diffs,
        }

    def _maybe_cleanup_worktrees(self, run: dict, workflow: dict) -> None:
        """run 收尾时清理 worktree（保留 keepWorktree 或仍等待/运行中的情况）。"""
        state = run.get("worktrees")
        if not state:
            return
        variables = workflow.get("variables") or {}
        if worktree_mod.workflow_keep_worktree(variables):
            return
        if run.get("status") in ("waiting_human", "running"):
            return
        try:
            worktree_mod.cleanup_run_worktrees(state, run.get("id"))
        except Exception:  # noqa: BLE001
            logger.exception("flow worktree 清理失败: %s", run.get("id"))
        run.pop("worktrees", None)

    async def start(self, workflow: dict, prompt: str, project_id: str = "", requirement_id: str = "", variables: dict | None = None) -> dict:
        """启动一次运行：冻结工作流快照，创建 run 并后台执行。

        variables 为触发运行时的临时参数（如 projectPath / language），仅本次运行生效：
        覆盖合并进工作流默认 variables（不修改工作流定义），并写入快照与 run.input.variables。
        """
        # 运行时变量覆盖工作流默认变量，仅影响本次运行的快照
        merged_vars = {**(workflow.get("variables") or {}), **(variables or {})}
        snapshot = {**workflow, "variables": merged_vars}
        run_id = flow_db.create_run_id()
        run: dict = {
            "id": run_id,
            "workflowId": workflow["id"],
            "status": "running",
            "input": {"prompt": prompt, "files": [], "variables": merged_vars},
            "projectId": project_id or None,
            "requirementId": requirement_id or None,
            "nodeRuns": {},
            "artifacts": {},
            "startedAt": flow_db.now_iso(),
            "finishedAt": "",
            "totals": {"tokensIn": 0, "tokensOut": 0, "costUsd": 0},
            "workflowSnapshot": snapshot,
            "pendingHumanNodeId": None,
            "fileDiffs": {},
            "createdAt": flow_db.now_iso(),
        }
        self._runs[run_id] = run
        flow_db.insert_run(run)
        self._emit(run_id, {"type": "run_start", "runId": run_id, "workflowId": workflow["id"]})
        self._tasks[run_id] = asyncio.create_task(self.execute(run, snapshot))
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
        # 拒绝 human 等待，唤醒挂起节点
        wait = self._human_waits.get(run_id)
        if wait is not None and not wait.done():
            wait.set_exception(asyncio.CancelledError())
        # 释放 run 持有的项目路径锁
        release_path_locks_for_run(run_id)
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

        # 人工审批节点：挂起等待审批（不走常规 LLM/pi 执行路径）
        if ntype == "human":
            await self._execute_human_node(workflow, run, node, progress, model, visits)
            return

        nr["status"] = "running"
        nr["modelId"] = model.get("id") or model.get("model", "")
        nr["startedAt"] = flow_db.now_iso()
        self._emit(run_id, {"type": "node_start", "runId": run_id, "nodeId": node_id, "modelId": nr["modelId"]})

        # retry 配置：maxAttempts = retry.max + 1（仅 error / review_fail 生效）
        retry_cfg = data.get("retry") or {}
        retry_on = retry_cfg.get("on")
        if retry_on in ("error", "review_fail"):
            max_attempts = max(1, int(retry_cfg.get("max") or 0) + 1)
        else:
            max_attempts = 1

        result_text = ""
        t_in = t_out = 0
        node_diffs: list[dict] | None = None
        last_error = "node failed"

        try:
            for attempt in range(1, max_attempts + 1):
                if attempt > 1:
                    # 重试：重置状态，重发 node_start（保留 logs）
                    nr["status"] = "running"
                    nr.pop("error", None)
                    nr["startedAt"] = flow_db.now_iso()
                    self._push_log(run, node_id, f"── 第 {attempt} 次执行（retry）──", "info")
                    self._emit(run_id, {"type": "node_start", "runId": run_id, "nodeId": node_id, "modelId": nr["modelId"]})
                try:
                    if ntype == "merge":
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
                            # 编码节点：走 pi CLI + worktree 沙箱
                            coding = await self._run_pi_coding_node(workflow, run, node, models)
                            result_text = coding.get("text", "")
                            t_in = coding.get("tokensIn", 0)
                            t_out = coding.get("tokensOut", 0)
                            node_diffs = coding.get("fileDiffs")
                        elif executor == "none":
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
                    # review_fail 重试：执行成功但结论为 fail
                    if (
                        retry_on == "review_fail"
                        and is_fail_conclusion(result_text)
                        and attempt < max_attempts
                    ):
                        self._push_log(run, node_id, f"结论为 fail，{400 * attempt}ms 后同节点重试", "warn")
                        await asyncio.sleep(0.4 * attempt)
                        continue
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)[:2000]
                    if retry_on == "error" and attempt < max_attempts:
                        self._push_log(run, node_id, f"执行失败，{400 * attempt}ms 后重试：{last_error[:300]}", "warn")
                        await asyncio.sleep(0.4 * attempt)
                        continue
                    raise

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
            nr["error"] = str(exc)[:2000] or last_error
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

    async def _execute_human_node(
        self, workflow: dict, run: dict, node: dict, progress: dict, model: dict, visits: int
    ) -> None:
        """执行人工审批节点：挂起等待审批，resume 后应用决策并激活下游。"""
        node_id = node["id"]
        run_id = run["id"]
        data = node.get("data") or {}
        nr = run["nodeRuns"][node_id]
        nr["status"] = "running"
        nr["modelId"] = model.get("id") or model.get("model", "")
        nr["startedAt"] = flow_db.now_iso()
        self._emit(run_id, {"type": "node_start", "runId": run_id, "nodeId": node_id, "modelId": nr["modelId"]})
        try:
            fut = self._wait_for_human(run, node_id, data.get("userPromptTemplate") or "")
            decision = await fut
            self._apply_human_decision(run, node_id, decision, workflow)
            if decision.get("action") == "approve":
                progress["completed"].add(node_id)
                nr["finishedAt"] = flow_db.now_iso()
                self._activate_downstream(workflow, progress, run, node_id, "approved")
            else:
                nr["finishedAt"] = flow_db.now_iso()
                run["status"] = "failed"
                run["finishedAt"] = flow_db.now_iso()
                self._emit(run_id, {
                    "type": "run_end",
                    "runId": run_id,
                    "status": "failed",
                    "artifacts": run.get("artifacts", {}),
                    "totals": run.get("totals"),
                })
        except asyncio.CancelledError:
            # 等待中被取消（cancel 或覆盖等待）
            raise
        finally:
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

    async def execute(self, run: dict, workflow: dict, progress: dict | None = None) -> dict:
        """主调度循环：拓扑 → 并行执行 → 收尾。progress 可在 resume 时传入（seed）。"""
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
        # 初始化节点运行记录（resume 时已有记录则保留）
        for n in workflow.get("nodes") or []:
            run["nodeRuns"].setdefault(n["id"], {
                "nodeId": n["id"],
                "status": "pending",
                "modelId": (n.get("data") or {}).get("modelId", ""),
                "logs": [],
            })
        progress = progress or self._initial_progress(workflow)

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
            # 收尾清理 worktree（保留/等待/运行中除外）
            self._maybe_cleanup_worktrees(run, workflow)
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
            self._human_waits.pop(run_id, None)
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
