"""工具域拆分后的契约回归测试。

``akm/agent_runtime/tools.py`` 的 ``build_builtin_tools`` 原本是一个 1800+ 行的
巨型函数，内部用 34 个嵌套闭包定义全部工具。现已按功能域拆出：

- ``tools_tasks.py``：akm_list_tasks / akm_create_task / akm_delete_task
- ``tools_flow.py``：akm_flow_* 共 8 个

拆分最容易出的问题是**工具契约静默漂移**——工具名、描述、参数 schema 是模型
看到的契约，改错一个字都会影响模型行为，但普通单测未必覆盖。这里用快照比对
把契约钉死：只要拆分前后不一致就立刻失败。
"""

import pytest
from fastapi import FastAPI

from akm.agent_runtime.tools import build_builtin_tools
from akm.agent_runtime.tools_flow import build_flow_tools
from akm.agent_runtime.tools_tasks import build_task_tools

# ── 拆分后各模块负责的工具名，改动此表必须同步改拆分结构 ──────────────
TASK_TOOL_NAMES = ["akm_list_tasks", "akm_create_task", "akm_delete_task"]
FLOW_TOOL_NAMES = [
    "akm_flow_list",
    "akm_flow_get",
    "akm_flow_save",
    "akm_flow_delete",
    "akm_flow_run",
    "akm_flow_runs",
    "akm_flow_run_get",
    "akm_flow_cancel",
]


@pytest.fixture
def app():
    return FastAPI()


def test_task_module_exposes_expected_tools():
    """tasks 模块只暴露这三个工具，且名字顺序稳定。"""
    tools = build_task_tools()
    assert [t.name for t in tools] == TASK_TOOL_NAMES


def test_flow_module_exposes_expected_tools(app):
    """flow 模块暴露 8 个工具；build_flow_tools 需要 app 以取得 flow_engine。"""
    tools = build_flow_tools(app)
    assert [t.name for t in tools] == FLOW_TOOL_NAMES


def test_flow_handlers_with_app_are_prebound(app):
    """用到 app 的三个 handler 必须已被 partial 预绑定 app。

    ToolRegistry 用 ``handler(**arguments)`` 调用，arguments 来自模型；
    若 app 没被预绑定，模型就得提供它——既不该出现在 schema 里，也调用不通。
    """
    import functools

    tools = {t.name: t for t in build_flow_tools(app)}
    for name in ("akm_flow_run", "akm_flow_run_get", "akm_flow_cancel"):
        handler = tools[name].handler
        assert isinstance(handler, functools.partial), f"{name} 的 handler 未预绑定 app"
        assert "app" in handler.keywords, f"{name} 的 handler 未绑定 app"


def test_flow_tools_do_not_leak_app_into_schema(app):
    """app 是注入依赖，绝不能出现在模型可见的参数 schema 里。"""
    for t in build_flow_tools(app):
        props = (t.parameters or {}).get("properties") or {}
        assert "app" not in props, f"{t.name} 的 schema 泄漏了 app 参数"
        required = (t.parameters or {}).get("required") or []
        assert "app" not in required, f"{t.name} 把 app 标成了必填"


def test_merged_tool_list_has_no_duplicates(app):
    """各模块拼装后工具名必须唯一，重复注册会让模型看到两份同名工具。"""
    names = [t.name for t in build_builtin_tools(app)]
    assert len(names) == len(set(names)), f"存在重复工具名: {sorted(n for n in names if names.count(n) > 1)}"


def test_split_modules_are_wired_into_builder(app):
    """确认 build_builtin_tools 真的接入了两个新模块，而不是各自独立存在。"""
    names = {t.name for t in build_builtin_tools(app)}
    for n in TASK_TOOL_NAMES + FLOW_TOOL_NAMES:
        assert n in names, f"{n} 未被 build_builtin_tools 注册"


def test_task_tool_schema_contract():
    """钉住 tasks 工具的参数契约（模型可见，改动会影响调用成功率）。"""
    tools = {t.name: t for t in build_task_tools()}

    assert set(tools["akm_list_tasks"].parameters["properties"]) == {"task_type", "enabled"}
    assert tools["akm_list_tasks"].parameters["required"] == []

    assert set(tools["akm_create_task"].parameters["properties"]) == {
        "name",
        "task_type",
        "payload",
        "interval_sec",
        "cron",
        "enabled",
    }
    assert tools["akm_create_task"].parameters["required"] == ["name", "task_type"]

    assert set(tools["akm_delete_task"].parameters["properties"]) == {"task_id"}
    assert tools["akm_delete_task"].parameters["required"] == ["task_id"]


def test_flow_enabled_switch_still_gates_flow_tools(monkeypatch, app):
    """flow_enabled=false 时整组 flow 工具都不应注册（拆分前就有的行为）。"""
    from akm.agent_runtime import tools as tools_module

    real_load_config = tools_module.load_config
    monkeypatch.setattr(
        tools_module,
        "load_config",
        lambda: {**real_load_config(), "flow_enabled": False},
    )
    names = {t.name for t in build_builtin_tools(app)}
    for n in FLOW_TOOL_NAMES:
        assert n not in names, f"flow_enabled=false 时 {n} 仍被注册"
    # tasks 工具不受 flow_enabled 影响
    for n in TASK_TOOL_NAMES:
        assert n in names, f"flow_enabled=false 误伤了 {n}"
