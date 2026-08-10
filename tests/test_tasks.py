"""/v1/tasks 定时任务接口与后台调度器的测试。"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from akm import db
from akm.agent_runtime.loop import AgentResult
from akm.server import app


class _FakeAgentLoop:
    """记录传入 messages 与 options 的假 Agent Loop，不做真实调用。"""

    def __init__(self):
        self.calls = []

    async def run(self, messages, **options):
        self.calls.append({"messages": messages, "options": options})
        return AgentResult(
            ok=True,
            final_message={"role": "assistant", "content": "done"},
            messages=messages,
        )


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """把数据库隔离到临时目录，避免污染真实 ~/.akm/akm.db。"""
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    # 确保建表（含 scheduled_tasks）在隔离库上执行
    conn = db.get_connection()
    try:
        db.init_db(conn)
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _mount_agent_loop():
    """挂载假 Agent Loop，保证 agent_call 任务可执行。"""
    app.state.agent_loop = _FakeAgentLoop()
    yield
    app.state.agent_loop = None


@pytest.fixture(autouse=True)
def _mount_task_scheduler():
    """挂载一个不自动运行的假调度器实例，供 /run 端点使用。"""
    from akm.tasks.scheduler import TaskScheduler

    scheduler = TaskScheduler(app)
    app.state.task_scheduler = scheduler
    yield scheduler
    app.state.task_scheduler = None


def _make_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_create_task_and_get():
    """创建任务应返回 201 与完整记录，且可查询到。"""
    async with _make_client() as client:
        resp = await client.post(
            "/v1/tasks",
            json={
                "name": "每日总结",
                "task_type": "agent_call",
                "payload": {"messages": [{"role": "user", "content": "总结"}]},
                "interval_sec": 3600,
            },
        )
    assert resp.status_code == 201
    task = resp.json()["task"]
    assert task["name"] == "每日总结"
    assert task["task_type"] == "agent_call"
    assert task["enabled"] is True
    assert task["payload"]["messages"][0]["content"] == "总结"
    assert task["next_run_at"]  # 首次创建即安排当前时间

    async with _make_client() as client:
        detail = await client.get(f"/v1/tasks/{task['id']}")
    assert detail.status_code == 200
    assert detail.json()["task"]["id"] == task["id"]


@pytest.mark.asyncio
async def test_create_task_validation():
    """缺少 name / 非法 task_type 应返回 400。"""
    async with _make_client() as client:
        resp = await client.post("/v1/tasks", json={"task_type": "agent_call"})
    assert resp.status_code == 400
    assert "name" in resp.json()["detail"]

    async with _make_client() as client:
        resp = await client.post(
            "/v1/tasks", json={"name": "x", "task_type": "cron_bomb"}
        )
    assert resp.status_code == 400
    assert "task_type" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_tasks_with_filter():
    """列表接口应返回全部任务，并支持 task_type / enabled 过滤。"""
    async with _make_client() as client:
        await client.post(
            "/v1/tasks",
            json={"name": "a", "task_type": "agent_call", "enabled": True},
        )
        await client.post(
            "/v1/tasks",
            json={"name": "b", "task_type": "usage_query", "enabled": False},
        )

    async with _make_client() as client:
        resp = await client.get("/v1/tasks")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    async with _make_client() as client:
        resp = await client.get("/v1/tasks", params={"task_type": "usage_query"})
    assert resp.json()["total"] == 1
    assert resp.json()["tasks"][0]["name"] == "b"

    async with _make_client() as client:
        resp = await client.get("/v1/tasks", params={"enabled": "1"})
    assert resp.json()["total"] == 1
    assert resp.json()["tasks"][0]["name"] == "a"


@pytest.mark.asyncio
async def test_update_task():
    """PUT 应更新指定字段，非法 task_type 返回 400，不存在返回 404。"""
    async with _make_client() as client:
        created = await client.post(
            "/v1/tasks", json={"name": "old", "task_type": "agent_call"}
        )
        task_id = created.json()["task"]["id"]
        resp = await client.put(
            f"/v1/tasks/{task_id}", json={"name": "new", "interval_sec": 60}
        )
    assert resp.status_code == 200
    updated = resp.json()["task"]
    assert updated["name"] == "new"
    assert updated["interval_sec"] == 60

    async with _make_client() as client:
        resp = await client.put(
            f"/v1/tasks/{task_id}", json={"task_type": "nope"}
        )
    assert resp.status_code == 400

    async with _make_client() as client:
        resp = await client.put("/v1/tasks/missing", json={"name": "x"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_task():
    """DELETE 应删除任务，重复删除返回 404。"""
    async with _make_client() as client:
        created = await client.post(
            "/v1/tasks", json={"name": "del", "task_type": "usage_query"}
        )
        task_id = created.json()["task"]["id"]
        resp = await client.delete(f"/v1/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    async with _make_client() as client:
        resp = await client.delete(f"/v1/tasks/{task_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_run_agent_call_task():
    """POST /{id}/run 应绕过调度器立即执行 agent_call，并返回 ok。"""
    async with _make_client() as client:
        created = await client.post(
            "/v1/tasks",
            json={
                "name": "跑一次",
                "task_type": "agent_call",
                "payload": {"messages": [{"role": "user", "content": "你好"}]},
            },
        )
        task_id = created.json()["task"]["id"]
        resp = await client.post(f"/v1/tasks/{task_id}/run")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    loop = app.state.agent_loop
    assert len(loop.calls) == 1
    assert loop.calls[0]["messages"][0]["content"] == "你好"

    # 立即执行不应推进调度状态
    task = db.get_task(task_id)
    assert task["last_run_at"] == ""


@pytest.mark.asyncio
async def test_run_task_missing_scheduler():
    """调度器未就绪时 /run 应返回 503。"""
    app.state.task_scheduler = None
    async with _make_client() as client:
        created = await client.post(
            "/v1/tasks", json={"name": "x", "task_type": "agent_call"}
        )
        task_id = created.json()["task"]["id"]
        resp = await client.post(f"/v1/tasks/{task_id}/run")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_tasks_require_token_when_configured(monkeypatch):
    """配置 agent_api_token 后未携带令牌的请求应返回 401。"""
    from akm.agent_runtime import router as agent_router

    monkeypatch.setattr(
        agent_router, "load_config", lambda: {"agent_api_token": "secret-token"}
    )
    async with _make_client() as client:
        resp = await client.get("/v1/tasks")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_scheduler_runs_due_task(monkeypatch, tmp_path):
    """调度器 _check_due_tasks 应执行到期任务并推进状态。"""
    from akm.tasks.scheduler import TaskScheduler

    task_id = db.create_task(
        name="到期任务",
        task_type="agent_call",
        payload={"messages": [{"role": "user", "content": "到点了"}]},
        enabled=True,
    )["id"]

    scheduler = app.state.task_scheduler
    await scheduler._check_due_tasks()

    loop = app.state.agent_loop
    assert len(loop.calls) == 1
    assert loop.calls[0]["messages"][0]["content"] == "到点了"

    # 单次任务执行后应自动禁用且清空 next_run_at
    task = db.get_task(task_id)
    assert task["enabled"] is False
    assert task["next_run_at"] == ""
    assert task["last_run_at"]  # 记录执行时间
    assert task["payload"]["last_result"]["ok"] is True


@pytest.mark.asyncio
async def test_scheduler_advances_interval_task():
    """循环任务执行后应把 next_run_at 滚动到下一时间点，保持启用。"""
    from akm.tasks.scheduler import TaskScheduler

    task_id = db.create_task(
        name="循环任务",
        task_type="agent_call",
        payload={"messages": [{"role": "user", "content": "hi"}]},
        interval_sec=3600,
        enabled=True,
    )["id"]

    scheduler = app.state.task_scheduler
    await scheduler._check_due_tasks()

    task = db.get_task(task_id)
    assert task["enabled"] is True
    assert task["next_run_at"]  # 下一轮已排期
    assert task["next_run_at"] > task["last_run_at"]
