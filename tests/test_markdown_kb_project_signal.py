"""markdown_kb 项目工作区信号抽取防御测试。

背景（0.1.4 修复）：此前抽取对“消息文本里任何 `Working directory: xxx` 字样”
都命中，导致对话正文（被客户端整段重发）里的路径示例/转述被误认为工作区信号，
自动创建了指向不存在目录的垃圾项目并触发无意义的 LLM context 生成。

本文件覆盖四类防御：
1. 信号只在会话开头少量消息的完整 `<env>` 块 / system 文本里识别；
2. 抽取出的路径做清洗，夹带对话残片的畸形取值直接不命中；
3. 自动建项目前校验目录真实存在；不存在时既不建也不注入；
4. 自动刷新任务在没有 KB 材料时不产生 LLM 调用。
"""
import asyncio
import json
import pathlib
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, "plugins/markdown_kb")
import index as _mk_index  # noqa: E402


# ─────────────────────────── 工具 ───────────────────────────

class _FakeLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def _build_plugin(config: dict | None = None):
    tmp = pathlib.Path(tempfile.mkdtemp())
    pathlib.Path.home = lambda: tmp
    plugin = _mk_index.Plugin.__new__(_mk_index.Plugin)
    plugin.name = "markdown_kb"
    plugin.config = config or {}
    plugin.logger = _FakeLogger()
    plugin._akm_base_url = lambda: "http://127.0.0.1:9"
    plugin._ensure_runtime_ready()
    plugin._validate_state_files()
    return plugin, tmp


def _workspace_dir(base: pathlib.Path, name: str = "proj-a") -> pathlib.Path:
    workspace = base / name
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _ctx(request: dict) -> SimpleNamespace:
    return SimpleNamespace(request=request)


def _chat_request(workspace: str, *, env_at: int = 0, junk_env: bool = False) -> dict:
    """构造 chat 协议请求：env 块放在第 env_at 条消息（0 起），前面插 filler。"""
    line = (
        "Working directory: " + ("/fake/path`/with-junk）" if junk_env else workspace)
    )
    env_block = f"<env>\n  {line}\n  Platform: darwin\n</env>"
    messages = []
    for i in range(env_at):
        messages.append({"role": "assistant", "content": f"第 {i} 轮回复。"})
    messages.append({"role": "user", "content": "介绍本项目。\n" + env_block})
    return {"model": "x", "messages": messages}


# ─────────────────────────── 抽取与注入防御 ───────────────────────────

@pytest.mark.asyncio
async def test_opencode_env_in_early_message_still_injects():
    """回归：会话开头完整 `<env>` 块的 opencode 形态仍然命中并注入。"""
    plugin, tmp = _build_plugin({"inject_project_context": True})
    workspace = str(_workspace_dir(tmp, "proj-a"))
    request = {
        "model": "x",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are opencode. 系统指引……\n"
                    f"<env>\n  Working directory: {workspace}\n"
                    "  Workspace root folder: " + workspace + "\n</env>"
                ),
            },
            {"role": "user", "content": "你好"},
        ],
    }
    out = await plugin.on_request(_ctx(request))
    # chat 注入 = 在 messages 头部插入一条 system
    assert out is request
    injected = str(request["messages"][0].get("content") or "")
    assert "## 项目上下文" in injected
    assert workspace in injected


@pytest.mark.asyncio
async def test_late_conversation_working_directory_text_is_not_signal():
    """对话中段消息出现裸 `Working directory:` 字样：不命中、不注入、不建项目。"""
    plugin, tmp = _build_plugin({"inject_project_context": True})
    workspace = str(_workspace_dir(tmp, "proj-a"))
    # env 块出现在第 6 条消息（超出前 3 条扫描窗口）
    request = _chat_request(workspace, env_at=5)
    out = await plugin.on_request(_ctx(request))
    assert out is request
    assert not str(out.get("instructions") or "").strip()


@pytest.mark.asyncio
async def test_junk_env_value_rejected_by_sanitizer():
    """`<env>` 块内取值夹带对话残片（反引号/括号/引号）→ 清洗为空 → 不命中。"""
    plugin, _tmp = _build_plugin({"inject_project_context": True})
    assert plugin._sanitize_path_value("/ok/path") == "/ok/path"
    assert plugin._sanitize_path_value("relative/path") == ""
    assert plugin._sanitize_path_value("/Applications/AI Key Manager.app/Contents/Resources/…</env>）===") == ""
    assert plugin._sanitize_path_value("/path/to/project`") == ""
    assert plugin._sanitize_path_value("/path\" 在 <env> 中为多行…") == ""
    assert plugin._sanitize_path_value("/Users/nk/My Project with spaces") == "/Users/nk/My Project with spaces"

    request = _chat_request(str("/fake/ws"), junk_env=True)
    out = await plugin.on_request(_ctx(request))
    assert out is request
    assert not str(out.get("instructions") or "").strip()


@pytest.mark.asyncio
async def test_signal_to_missing_directory_creates_no_project_and_no_inject():
    """信号指向不存在目录：自动建项目被拦截，既不注入也不产生垃圾项目。"""
    plugin, tmp = _build_plugin({"inject_project_context": True})
    ghost = str(tmp / "ghost-project")  # 目录从未创建
    request = {
        "model": "x",
        "messages": [
            {"role": "system", "content": f"<env>\n  Working directory: {ghost}\n</env>"},
            {"role": "user", "content": "你好"},
        ],
    }
    out = await plugin.on_request(_ctx(request))
    assert out is request
    assert not str(out.get("instructions") or "").strip()

    data_root = plugin._data_root
    projects_dir = data_root / "projects"
    existing = set(p.name for p in projects_dir.iterdir()) if projects_dir.exists() else set()
    assert not any(p.startswith("ghost") for p in existing)
    assert existing == set()  # 没有自动创建任何项目


@pytest.mark.asyncio
async def test_claude_system_primary_working_directory_still_injects():
    """回归：Claude system 里的 `Primary working directory` 仍命中注入。"""
    plugin, tmp = _build_plugin({"inject_project_context": True})
    workspace = str(_workspace_dir(tmp, "proj-claude"))
    request = {
        "model": "x",
        "max_tokens": 128,
        "system": f"你是 Claude。\nPrimary working directory: {workspace}",
        "messages": [{"role": "user", "content": "你好"}],
    }
    out = await plugin.on_request(_ctx(request))
    # messages 协议注入 = 写回 request["system"]
    assert out is request
    injected = str(request.get("system") or "")
    assert "## 项目上下文" in injected
    assert workspace in injected


@pytest.mark.asyncio
async def test_auto_refresh_skips_llm_when_no_kb_material():
    """自动刷新任务在 KB 材料为空时不产生 LLM 调用（不烧 token 生成占位文档）。"""
    plugin, tmp = _build_plugin({"inject_project_context": True, "chat_model": "test-model"})
    workspace = str(_workspace_dir(tmp, "proj-empty"))
    pm = plugin._get_project_memory()
    assert pm is not None
    pm.ensure_skeleton(workspace, [])
    assert pm.has_project(workspace)

    calls = []
    original_init = pm.run_context_init

    async def fake_init(ws, **kwargs):
        calls.append(ws)
        return {"ok": True, "written": True, "degraded": False}

    pm.run_context_init = fake_init  # type: ignore[method-assign]
    try:
        plugin._project_kb_material = lambda _ws: []  # type: ignore[method-assign]
        plugin._schedule_project_context_refresh(workspace)
        # 等待后台 runner 跑完（材料空 → 直接 return，不调 init）
        for _ in range(50):
            tasks = getattr(plugin, "_project_context_tasks", {})
            if not tasks:
                break
            await asyncio.sleep(0.01)
        assert calls == []
    finally:
        pm.run_context_init = original_init  # type: ignore[method-assign]
        for task in list(getattr(plugin, "_project_context_tasks", {}).values()):
            task.cancel()
        await asyncio.gather(*list(getattr(plugin, "_project_context_tasks", {}).values()), return_exceptions=True)


@pytest.mark.asyncio
async def test_auto_refresh_runs_llm_when_kb_material_present():
    """对照：KB 材料非空时自动刷新确实调用一次 LLM init。"""
    plugin, tmp = _build_plugin({"inject_project_context": True, "chat_model": "test-model"})
    workspace = str(_workspace_dir(tmp, "proj-real"))
    pm = plugin._get_project_memory()
    assert pm is not None
    pm.ensure_skeleton(workspace, [{"file_name": "seed.md", "excerpt": "种子材料", "updated_at": "2026-01-01T00:00:00+00:00"}])

    calls = []
    original_init = pm.run_context_init

    async def fake_init(ws, **kwargs):
        calls.append(ws)
        return {"ok": True, "written": True, "degraded": False}

    pm.run_context_init = fake_init  # type: ignore[method-assign]
    try:
        # 有真实 KB 材料时才允许自动刷新调用 LLM
        plugin._project_kb_material = lambda _ws: [  # type: ignore[method-assign]
            {"file_name": "seed.md", "excerpt": "种子材料", "updated_at": "2026-01-01T00:00:00+00:00"}
        ]
        plugin._schedule_project_context_refresh(workspace)
        for _ in range(100):
            tasks = getattr(plugin, "_project_context_tasks", {})
            if not tasks and calls:
                break
            await asyncio.sleep(0.01)
        assert calls == [workspace]
    finally:
        pm.run_context_init = original_init  # type: ignore[method-assign]
        for task in list(getattr(plugin, "_project_context_tasks", {}).values()):
            task.cancel()
        await asyncio.gather(*list(getattr(plugin, "_project_context_tasks", {}).values()), return_exceptions=True)


@pytest.mark.asyncio
async def test_real_debug_trace_like_text_does_not_trigger():
    """复现线上污染样例：正文里出现的 `.app` 路径描述与正则示例不再触发。"""
    plugin, _tmp = _build_plugin({"inject_project_context": True})
    body_text = (
        "我之前打印了 UA 判断逻辑。路径形如\n"
        "`Working directory: /Applications/AI Key Manager.app/Contents/Resources/`\n"
        "以及我的正则 `Working directory\\s*:\\s*(.+)` 取到行尾含多余内容/路径带空格截断？"
        "不会截断，.+ 贪婪到行尾。或 opencode env 实际标签是 "
        "`Working directory:`），都在 <env>...</env> 中为多行；正则匹配单行文本 OK）。"
    )
    request = {
        "model": "x",
        "messages": [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": body_text},
        ],
    }
    out = await plugin.on_request(_ctx(request))
    assert out is request
    assert not str(out.get("instructions") or "").strip()
