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
def _monkey_forward(monkeypatch, tmp_path):
    # 隔离真实 ~/.akm/config.json：相对 projectPath 一律基于临时 git 仓库解析，
    # 避免读到用户真实 agent_workspace_root（~/Desktop），并保证 worktree 模式可用
    import subprocess

    import akm.config as cfg_mod

    git_dir = tmp_path / "workspace"
    git_dir.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=git_dir, check=False)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=git_dir,
        check=False,
    )
    monkeypatch.setattr(
        cfg_mod,
        "load_config",
        lambda: {
            "agent_workspace_root": str(git_dir),
            "log_request_body": False,
            "log_response_body": False,
            "flow_human_auto_approve": True,
        },
    )
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
    # 编码节点（pi-agent）不真实调用本机 pi CLI（测试环境 Node 版本可能不支持），
    # 统一 mock 返回一段文本，仅验证引擎调度与产物链
    async def _fake_pi_agent(opts):
        (opts.get("on_log") or (lambda m, lvl="info": None))("pi-agent（mock）", "info")
        return {
            "text": "# Pi Agent（mock）\n\n已完成编码任务。\n\n## 结论\npass",
            "tokensIn": 50,
            "tokensOut": 30,
            "fileDiffs": None,
        }

    monkeypatch.setattr(fe.pi_runner, "run_pi_agent", _fake_pi_agent)
    return fake


# ── 纯函数 ─────────────────────────────────────────────────────

def test_resolve_pi_binary(monkeypatch):
    """pi CLI 定位：which 优先，找不到再扫常见安装目录，全未命中返回 None。"""
    import os
    import shutil as sh

    from akm.flow.pi_runner import _resolve_pi_binary

    # 1) which 命中时直接返回（打包 app 也能拿到绝对路径，不依赖 PATH）
    monkeypatch.setattr(sh, "which", lambda name: "/custom/pi")
    assert _resolve_pi_binary() == "/custom/pi"

    # 2) which 未命中时，兜底扫描常见安装目录（模拟 /usr/local/bin/pi 存在）
    monkeypatch.setattr(sh, "which", lambda name: None)
    monkeypatch.setattr(os.path, "isfile", lambda p: p == "/usr/local/bin/pi")
    monkeypatch.setattr(os, "access", lambda p, mode: True)
    assert _resolve_pi_binary() == "/usr/local/bin/pi"

    # 3) 全部未命中返回 None（由 _is_start_failure 判定后回退 mock）
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    assert _resolve_pi_binary() is None


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
async def test_engine_start_variables_override(_monkey_forward):
    """启动时传入的 variables 应覆盖工作流默认值，且不修改工作流定义。"""
    wf = hotfix_workflow()
    wf["variables"]["projectPath"] = "."
    wf["variables"]["language"] = "HTML"
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(
        wf,
        "修复某个 bug",
        variables={"projectPath": "/tmp/proj-x", "language": "TS"},
    )
    # 本次运行的 input 与快照变量均为合并结果（运行参数优先）
    assert run["input"]["variables"]["projectPath"] == "/tmp/proj-x"
    assert run["input"]["variables"]["language"] == "TS"
    assert run["workflowSnapshot"]["variables"]["projectPath"] == "/tmp/proj-x"
    # 工作流定义本身不被修改
    assert wf["variables"]["projectPath"] == "."
    assert wf["variables"]["language"] == "HTML"
    # 后台任务跑完，避免游离任务
    await engine._tasks[run["id"]]
    assert engine.get_run(run["id"])["status"] == "succeeded"


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


# ── 二期：human 审批 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_engine_human_auto_approve_by_default(_monkey_forward):
    """默认 flow_human_auto_approve=true 时 human 节点自动放行，工作流可跑通。"""
    wf = hotfix_workflow()
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(wf, "修复 bug")
    await engine._tasks[run["id"]]
    final = engine.get_run(run["id"])
    assert final["status"] == "succeeded"
    human_node = next(n for n in wf["nodes"] if n["type"] == "human")
    assert final["nodeRuns"][human_node["id"]]["status"] == "succeeded"
    assert "output" in final["artifacts"]


@pytest.mark.asyncio
async def test_engine_human_waits_and_resume_approve(_monkey_forward, monkeypatch):
    """flow_human_auto_approve=false 时 human 节点挂起，resume approve 后续跑成功。"""
    import akm.flow.engine as fe

    monkeypatch.setattr(
        "akm.config.load_config",
        lambda: {"flow_human_auto_approve": False},
    )
    wf = hotfix_workflow()
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(wf, "修复 bug")
    human_node = next(n for n in wf["nodes"] if n["type"] == "human")
    # 等 human 节点进入 waiting_human
    for _ in range(50):
        cur = engine.get_run(run["id"])
        if cur["status"] == "waiting_human":
            break
        await asyncio.sleep(0.05)
    cur = engine.get_run(run["id"])
    assert cur["status"] == "waiting_human"
    assert cur.get("pendingHumanNodeId") == human_node["id"]
    assert cur["nodeRuns"][human_node["id"]]["status"] == "waiting_human"
    # approve 后应继续跑完
    await engine.resume(run["id"], {"action": "approve", "note": "同意"})
    await engine._tasks[run["id"]]
    final = engine.get_run(run["id"])
    assert final["status"] == "succeeded"
    assert final["nodeRuns"][human_node["id"]]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_engine_human_resume_reject_fails_run(_monkey_forward, monkeypatch):
    """resume reject 时 run 置 failed。"""
    import akm.flow.engine as fe

    monkeypatch.setattr(
        "akm.config.load_config",
        lambda: {"flow_human_auto_approve": False},
    )
    wf = hotfix_workflow()
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(wf, "修复 bug")
    human_node = next(n for n in wf["nodes"] if n["type"] == "human")
    for _ in range(50):
        cur = engine.get_run(run["id"])
        if cur["status"] == "waiting_human":
            break
        await asyncio.sleep(0.05)
    await engine.resume(run["id"], {"action": "reject", "note": "不同意"})
    await engine._tasks[run["id"]]
    final = engine.get_run(run["id"])
    assert final["status"] == "failed"
    assert final["nodeRuns"][human_node["id"]]["status"] == "failed"


@pytest.mark.asyncio
async def test_engine_human_resume_wrong_state(_monkey_forward, monkeypatch):
    """非 waiting_human 状态 resume 应抛错。"""
    import akm.flow.engine as fe

    monkeypatch.setattr(
        "akm.config.load_config",
        lambda: {"flow_human_auto_approve": False},
    )
    wf = hotfix_workflow()
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(wf, "修复 bug")
    human_node = next(n for n in wf["nodes"] if n["type"] == "human")
    for _ in range(50):
        cur = engine.get_run(run["id"])
        if cur["status"] == "waiting_human":
            break
        await asyncio.sleep(0.05)
    # 先 approve 一次让 run 离开 waiting_human
    await engine.resume(run["id"], {"action": "approve"})
    await engine._tasks[run["id"]]
    # 此时再 resume 应报错（run 已终态）
    with pytest.raises(ValueError):
        await engine.resume(run["id"], {"action": "approve"})


# ── 二期：retry ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_engine_retry_on_error(_monkey_forward, monkeypatch):
    """retry.on=error 时首次失败后重试成功。"""
    wf = hotfix_workflow()
    # 给 intake 节点配 retry：首次失败、重试成功
    intake = next(n for n in wf["nodes"] if n["type"] == "intake")
    intake["data"]["retry"] = {"max": 1, "on": "error"}
    calls = {"n": 0}

    async def flaky(body, client, api_path="chat/completions", plugin_manager=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("上游超时")
        import json

        return {
            "status_code": 200,
            "body": json.dumps({"choices": [{"message": {"role": "assistant", "content": "已完成。\n\n## 结论\npass"}}]}),
            "key_alias": "t",
            "provider": "p",
            "model": "m",
        }

    import akm.flow.engine as fe

    monkeypatch.setattr(fe, "_forward_request", flaky)
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(wf, "重试测试")
    await engine._tasks[run["id"]]
    final = engine.get_run(run["id"])
    assert final["status"] == "succeeded"
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_engine_retry_on_review_fail(_monkey_forward, monkeypatch):
    """retry.on=review_fail 时结论 fail 会重试同节点。"""
    wf = hotfix_workflow()
    intake = next(n for n in wf["nodes"] if n["type"] == "intake")
    intake["data"]["retry"] = {"max": 1, "on": "review_fail"}
    calls = {"n": 0}

    async def flaky(body, client, api_path="chat/completions", plugin_manager=None):
        calls["n"] += 1
        import json

        if calls["n"] <= 1:
            text = "有严重问题。\n\n## 结论\nfail"
        else:
            text = "已修复。\n\n## 结论\npass"
        return {
            "status_code": 200,
            "body": json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}),
            "key_alias": "t",
            "provider": "p",
            "model": "m",
        }

    import akm.flow.engine as fe

    monkeypatch.setattr(fe, "_forward_request", flaky)
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(wf, "审查重试")
    await engine._tasks[run["id"]]
    final = engine.get_run(run["id"])
    assert final["status"] == "succeeded"
    assert calls["n"] >= 2


# ── 二期：pi-agent 节点 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_engine_pi_coding_node_runs_pi_agent(_monkey_forward):
    """pi-agent 编码节点走 run_pi_agent 执行（fixture 已 mock），产物链完整。"""
    wf = hotfix_workflow()
    app = _fake_app()
    engine = WorkflowEngine(app)
    run = await engine.start(wf, "编码测试")
    await engine._tasks[run["id"]]
    final = engine.get_run(run["id"])
    assert final["status"] == "succeeded"
    code_node = next(n for n in wf["nodes"] if n["type"] == "code")
    assert final["nodeRuns"][code_node["id"]]["status"] == "succeeded"
    assert "output" in final["artifacts"]
