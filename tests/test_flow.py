"""tests for akm/flow（/v1/flow 工作流引擎一期）。"""

import asyncio
from types import SimpleNamespace

import pytest

from akm.flow import db as flow_db
from akm.flow.engine import (
    WorkflowEngine,
    max_node_visits,
    parse_conclusion,
    render_template,
    structural_edges,
    topological_layers,
)
from akm.flow.templates import dual_model_workflow, hotfix_workflow, standard_dev_workflow


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """把 flow DB 隔离到临时目录，避免污染真实 ~/.akm/akm.db。"""
    import akm.db as core_db

    monkeypatch.setattr(core_db, "DB_DIR", str(tmp_path))
    conn = core_db.get_connection()
    core_db.init_db(conn)
    conn.close()
    flow_db.init_flow_db()
    yield


def _fake_app():
    """构造带 http_client/plugin_manager 的假 app（引擎测试用）。"""
    state = SimpleNamespace(http_client=object(), plugin_manager=None)
    app = SimpleNamespace(state=state)
    return app


class _FakeForward:
    """mock forward_request：记录调用，返回固定 Chat 文本。"""

    def __init__(self, text="你好，已完成处理。\n\n## 结论\npass"):
        self.text = text
        self.calls = []

    async def __call__(self, body, client, api_path="chat/completions", plugin_manager=None):
        self.calls.append(body)
        payload = {
            "choices": [{"message": {"role": "assistant", "content": self.text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        import json

        return {"status_code": 200, "body": json.dumps(payload), "key_alias": "t", "provider": "p", "model": "m"}


@pytest.fixture
def _monkey_forward(monkeypatch):
    fake = _FakeForward()
    import akm.flow.engine as fe

    monkeypatch.setattr(fe, "_forward_request", fake)
    # 让节点走真实 forward（而非 mock 生成），需注入非 mock 模型目录
    import akm.flow.models as fm

    monkeypatch.setattr(
        fm,
        "resolve_model_catalog",
        lambda: [{"id": "gpt-test", "name": "GPT Test", "provider": "custom", "model": "gpt-test", "strengths": ["code", "reason"], "costPer1k": None}],
    )
    return fake


# ── 纯函数 ─────────────────────────────────────────────────────

def test_structural_edges_excludes_loop():
    wf = standard_dev_workflow()
    edges = structural_edges(wf)
    assert all(not e.get("loop") for e in edges)
    assert len(edges) == 7  # 8 边中去掉 fix→review loop 边


def test_topological_layers_dag():
    for factory in (standard_dev_workflow, hotfix_workflow, dual_model_workflow):
        wf = factory()
        layers = topological_layers(wf)
        node_count = sum(len(l) for l in layers)
        assert node_count == len(wf["nodes"])


def test_topological_layers_detects_cycle():
    wf = standard_dev_workflow()
    nodes = wf["nodes"]
    # 给两条互相依赖的边制造环（非 loop 边）
    wf["edges"].append({"id": "e_cyc", "source": nodes[1]["id"], "target": nodes[0]["id"], "loop": False})
    with pytest.raises(ValueError):
        topological_layers(wf)


def test_max_node_visits_clamp():
    assert max_node_visits({"variables": {}}) == 3
    assert max_node_visits({"variables": {"maxNodeVisits": "5"}}) == 5
    assert max_node_visits({"variables": {"maxNodeVisits": "99"}}) == 20
    assert max_node_visits({"variables": {"maxNodeVisits": "0"}}) == 1
    assert max_node_visits({"variables": {"maxNodeVisits": "abc"}}) == 3


def test_render_template_vars_and_artifacts():
    ctx = {"vars": {"language": "Python"}, "artifacts": {"plan": "方案A"}, "input": {"prompt": "需求"}}
    out = render_template("语言: {{vars.language}}\n方案: {{artifacts.plan}}\n需求: {{input.prompt}}\n缺失: {{missing.key}}", ctx)
    assert "Python" in out
    assert "方案A" in out
    assert "需求" in out
    assert "缺失: " in out


def test_parse_conclusion():
    assert parse_conclusion("## 结论\npass") == "pass"
    assert parse_conclusion("## 结论\nfail") == "fail"
    assert parse_conclusion("没有结论") == "unknown"
    assert parse_conclusion("pass") == "pass"


# ── 引擎执行 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_engine_runs_linear_workflow(_monkey_forward):
    """hotfix 无分支无 loop，应全部节点 succeeded，run succeeded。"""
    wf = hotfix_workflow()
    app = _fake_app()
    engine = WorkflowEngine(app)
    # 使 mock/无 key 环境下也走真实 forward（_monkey_forward 已替换 _forward_request）
    run = await engine.start(wf, "修复某个 bug")
    assert run["status"] in ("running", "succeeded")
    await engine._tasks[run["id"]]
    final = engine.get_run(run["id"])
    assert final["status"] == "succeeded"
    nids = [n["id"] for n in wf["nodes"]]
    for nid in nids:
        nr = final["nodeRuns"][nid]
        assert nr["status"] == "succeeded", f"节点 {nid} 应为 succeeded，实为 {nr['status']}: {nr.get('error')}"
    # 产物链完整：intake → approval → code → test → output
    assert "output" in final["artifacts"]
    assert final["totals"]["tokensIn"] >= 0


@pytest.mark.asyncio
async def test_engine_condition_branch_pass(_monkey_forward):
    """review 结论 pass 时应走 test 分支，fix 保持 pending→skipped。"""
    wf = standard_dev_workflow()
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(wf, "实现登录功能")
    await engine._tasks[run["id"]]
    final = engine.get_run(run["id"])
    assert final["status"] == "succeeded"
    # review 节点在传入文本中带 pass 结论（_monkey_forward 默认文本含 ## 结论 pass）
    fix_node = next(n for n in wf["nodes"] if n["type"] == "fix")
    fix_nr = final["nodeRuns"][fix_node["id"]]
    # fix 未被激活（pass 分支不触发 fix），因未激活应保持 pending 且最终 skipped
    assert fix_nr["status"] in ("pending", "skipped")


@pytest.mark.asyncio
async def test_engine_condition_branch_fail(_monkey_forward):
    """review 结论 fail 时应走 fix 分支。"""
    wf = standard_dev_workflow()
    # 让 review 输出 fail 结论
    wf["nodes"][4]["data"]["modelId"] = "mock-reviewer"
    _monkey_forward.text = "代码存在问题。\n\n## 结论\nfail"
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(wf, "实现登录功能")
    await engine._tasks[run["id"]]
    final = engine.get_run(run["id"])
    fix_node = next(n for n in wf["nodes"] if n["type"] == "fix")
    fix_nr = final["nodeRuns"][fix_node["id"]]
    assert fix_nr["status"] in ("succeeded", "failed")


@pytest.mark.asyncio
async def test_engine_parallel_fan_in(_monkey_forward):
    """dual_model 的 judge 应等待 codeA/codeB 都完成后执行（fan-in）。"""
    wf = dual_model_workflow()
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(wf, "生成工具函数")
    await engine._tasks[run["id"]]
    final = engine.get_run(run["id"])
    assert final["status"] == "succeeded"
    judge_node = next(n for n in wf["nodes"] if n["type"] == "review")
    judge_nr = final["nodeRuns"][judge_node["id"]]
    assert judge_nr["status"] == "succeeded"
    assert "judge" in final["artifacts"]


@pytest.mark.asyncio
async def test_engine_node_failure_marks_run_failed(monkeypatch):
    """LLM 请求失败时节点 failed、run failed。"""
    wf = hotfix_workflow()
    import akm.flow.engine as fe

    async def _boom(body, client, api_path="chat/completions", plugin_manager=None):
        raise RuntimeError("模拟上游失败")

    monkeypatch.setattr(fe, "_forward_request", _boom)
    import akm.flow.models as fm

    monkeypatch.setattr(
        fm,
        "resolve_model_catalog",
        lambda: [{"id": "gpt-test", "name": "GPT Test", "provider": "custom", "model": "gpt-test", "strengths": ["code", "reason"], "costPer1k": None}],
    )
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(wf, "修复")
    await engine._tasks[run["id"]]
    final = engine.get_run(run["id"])
    assert final["status"] == "failed"
    first_node = wf["nodes"][0]
    assert final["nodeRuns"][first_node["id"]]["status"] == "failed"
    assert "模拟上游失败" in final["nodeRuns"][first_node["id"]]["error"]


# ── 存储层 ─────────────────────────────────────────────────────

def test_workflow_crud():
    wf = hotfix_workflow()
    flow_db.insert_workflow(wf)
    got = flow_db.get_workflow(wf["id"])
    assert got is not None
    assert len(got["nodes"]) == len(wf["nodes"])
    updated = flow_db.update_workflow(wf["id"], {"name": "改名"})
    assert updated["name"] == "改名"
    assert flow_db.list_workflows()[0]["id"] == wf["id"]
    assert flow_db.delete_workflow(wf["id"]) is True
    assert flow_db.get_workflow(wf["id"]) is None


def test_run_crud():
    run = {"id": "run_test", "workflowId": "wf_test", "status": "running", "input": {"prompt": "x"}, "nodeRuns": {}, "artifacts": {}, "startedAt": flow_db.now_iso()}
    flow_db.insert_run(run)
    got = flow_db.get_run("run_test")
    assert got["status"] == "running"
    got["status"] = "succeeded"
    flow_db.update_run("run_test", got)
    assert flow_db.get_run("run_test")["status"] == "succeeded"
    runs, total = flow_db.list_runs()
    assert total == 1
    assert len(runs) == 1


def test_templates_instantiate():
    from akm.flow.router import _instantiate

    for factory in (standard_dev_workflow, hotfix_workflow, dual_model_workflow):
        wf = factory()
        inst = _instantiate(wf)
        assert inst["id"] != wf["id"]
        assert len(inst["nodes"]) == len(wf["nodes"])
        assert len(inst["edges"]) == len(wf["edges"])
        # 边指向的节点 id 都已重映射
        node_ids = {n["id"] for n in inst["nodes"]}
        for e in inst["edges"]:
            assert e["source"] in node_ids
            assert e["target"] in node_ids
