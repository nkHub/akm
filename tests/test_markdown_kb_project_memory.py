"""markdown_kb 项目级 context.md / memory.md 自动维护功能测试。

覆盖三层：
1. ProjectMemory 管理器：懒创建、事件记录、digest（LLM/降级）、快照、初始化回填；
2. 注入策略：首轮全量、后续轮次仅在记忆版本更新时刷新；
3. 回归：纯 auto_inject（RAG）路径行为不变；事件钩子从落盘路径被触发。

注意：项目记忆文件全部落在插件数据目录 projects/ 子目录，不写工作区目录、
不进 docs 清单/索引，因此与既有 RAG 检索完全隔离。
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
from project_memory import ProjectMemory, normalize_workspace  # noqa: E402


# ─────────────────────────── 工具 ───────────────────────────

class _FakeLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def _tmp_root():
    return pathlib.Path(tempfile.mkdtemp())


def _build_plugin(config: dict | None = None):
    """轻量构造插件实例：先把 Path.home 指到临时目录，避免写入真实 ~/.akm。

    注意：`_ensure_runtime_ready()` 会用 `_resolve_data_root()`（基于 home）重新
    计算数据根并覆盖调用方预设值，因此必须在调用前替换 Path.home。
    """
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


def _responses_request(workspace: str, *, with_history: bool = False) -> dict:
    env = (
        f"<environment_context>\n<workspace_root>{workspace}</workspace_root>\n"
        f"<cwd>{workspace}</cwd>\n</environment_context>"
    )
    input_items = []
    if with_history:
        input_items.append(
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好的，已处理。"}]}
        )
        input_items.append(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "继续。\n" + env}]}
        )
    else:
        input_items.append(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "请介绍本项目。\n" + env}]}
        )
    return {"model": "test-model", "input": input_items, "instructions": ""}


def _ctx(request: dict) -> SimpleNamespace:
    return SimpleNamespace(request=request)


async def _stub_digest_chat(manager: ProjectMemory, digest_markdown: str) -> None:
    async def fake_post(base_url: str, payload: dict) -> dict:
        content = json.dumps({"digest_markdown": digest_markdown}, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}

    manager._post_chat = fake_post  # type: ignore[method-assign]


# ─────────────────── ProjectMemory 管理器测试 ───────────────────

def test_public_workspace_events_ignored():
    pm = ProjectMemory(_tmp_root(), _FakeLogger())
    assert pm.record_event("", "added", "a.md") is None
    assert pm.project_id("") == ""
    assert pm.list_projects() == []


def test_record_event_creates_project_and_renders_memory():
    root = _tmp_root()
    workspace = str(_workspace_dir(root))
    pm = ProjectMemory(root, _FakeLogger())
    result = pm.record_event(workspace, "added", "notes.md")
    assert result is not None
    assert result["pending_events"] == 1
    assert result["revision"] == 1
    assert pm.has_project(workspace)

    data = pm.read_project(workspace)
    assert data is not None
    assert data["context_md"].startswith("# ")
    assert "notes.md" in data["memory_md"]
    assert "新增文档" in data["memory_md"]
    assert data["events"][0]["action"] == "added"
    # memory 内容变化即版本变化（事件与 digest 都会使版本自增）
    result2 = pm.record_event(workspace, "updated", "notes.md")
    assert result2["revision"] == 2


def test_ensure_skeleton_respects_manual_context_edit():
    root = _tmp_root()
    workspace = str(_workspace_dir(root))
    pm = ProjectMemory(root, _FakeLogger())
    created = pm.ensure_skeleton(workspace)
    assert created["created"] is True
    data = pm.read_project(workspace)
    assert data is not None
    # 人工编辑 context.md 后再次 ensure 不应覆盖
    pm._atomic_write_text(pm._context_path(workspace), "# 人工简介\n\n自己写的。\n")
    again = pm.ensure_skeleton(workspace)
    assert again["created"] is False
    assert pm.snapshot(workspace)["context_md"].startswith("# 人工简介")


def test_context_includes_project_readme_intro():
    root = _tmp_root()
    workspace_dir = _workspace_dir(root)
    (workspace_dir / "README.md").write_text("# 我的项目\n\n这是项目说明。\n" * 30, "utf-8")
    workspace = str(workspace_dir)
    pm = ProjectMemory(root, _FakeLogger())
    pm.ensure_skeleton(workspace)
    context_md = pm.snapshot(workspace)["context_md"]
    assert "我的项目" in context_md
    assert "这是项目说明" in context_md


@pytest.mark.asyncio
async def test_digest_with_chat_stub_writes_memory_and_resets_pending():
    root = _tmp_root()
    workspace = str(_workspace_dir(root))
    pm = ProjectMemory(root, _FakeLogger())
    pm.record_event(workspace, "learn", "20260801-title.learn.md")
    await _stub_digest_chat(pm, "- 沉淀了标题相关知识点\n- 整理了使用方法\n")
    result = await pm.run_digest(workspace, base_url="http://x", model="m", kb_material=[])
    assert result["ok"] is True
    assert result["consumed"] == 1
    assert result["degraded"] is False
    assert pm.pending_count(workspace) == 0
    memory_md = pm.snapshot(workspace)["memory_md"]
    assert "沉淀了标题相关知识点" in memory_md
    assert "## 近期要点" in memory_md


@pytest.mark.asyncio
async def test_digest_textual_fallback_when_no_model():
    root = _tmp_root()
    workspace = str(_workspace_dir(root))
    pm = ProjectMemory(root, _FakeLogger())
    pm.record_event(workspace, "added", "a.md")
    result = await pm.run_digest(workspace, base_url="http://x", model="", kb_material=[])
    assert result["degraded"] is True
    assert pm.pending_count(workspace) == 0
    memory_md = pm.snapshot(workspace)["memory_md"]
    assert "降级摘要" in memory_md


@pytest.mark.asyncio
async def test_digest_cooldown_gating():
    root = _tmp_root()
    workspace = str(_workspace_dir(root))
    pm = ProjectMemory(root, _FakeLogger())
    pm.record_event(workspace, "added", "a.md")
    await _stub_digest_chat(pm, "- 已整理")
    await pm.run_digest(workspace, base_url="http://x", model="m")
    # digest 后只有 1 条新事件且冷却未过 → 不触发
    pm.record_event(workspace, "updated", "a.md")
    assert pm.due_for_digest(workspace, cooldown_seconds=300, min_pending=3) is False
    # 事件达标 → 立即触发
    pm.record_event(workspace, "updated", "a.md")
    pm.record_event(workspace, "updated", "a.md")
    assert pm.due_for_digest(workspace, cooldown_seconds=300, min_pending=3) is True
    # 只有 1 条新事件但冷却早已过去 → 触发
    pm2_root = _tmp_root()
    workspace2 = str(_workspace_dir(pm2_root, "proj-b"))
    pm2 = ProjectMemory(pm2_root, _FakeLogger())
    pm2.record_event(workspace2, "added", "b.md")
    await _stub_digest_chat(pm2, "- 已整理")
    await pm2.run_digest(workspace2, base_url="http://x", model="m")
    pm2.record_event(workspace2, "updated", "b.md")
    meta_path = pm2._meta_path(workspace2)
    meta = json.loads(meta_path.read_text("utf-8"))
    meta["last_digest_at"] = "2000-01-01T00:00:00+00:00"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    assert pm2.due_for_digest(workspace2, cooldown_seconds=300, min_pending=3) is True


def test_init_projects_backfills_with_material():
    root = _tmp_root()
    workspace = str(_workspace_dir(root))
    pm = ProjectMemory(root, _FakeLogger())

    def material_fn(ws: str):
        assert ws == normalize_workspace(workspace)
        return [{"file_name": "arch.md", "updated_at": "2026-08-01T00:00:00+00:00", "excerpt": "架构说明"}]

    result = pm.init_projects([workspace], kb_material_fn=material_fn)
    assert result["ok"] is True
    assert result["created"] == 1
    data = pm.read_project(workspace)
    assert data is not None
    assert "arch.md" in data["context_md"]
    # 幂等：再跑一次不重复创建
    result2 = pm.init_projects([workspace], kb_material_fn=material_fn)
    assert result2["created"] == 0
    assert result2["skipped"] == 1


def test_snapshot_missing_project_returns_none():
    pm = ProjectMemory(_tmp_root(), _FakeLogger())
    assert pm.snapshot(str(_tmp_root() / "nope")) is None


# ─────────────────── 注入策略（首轮 / 刷新） ───────────────────

@pytest.mark.asyncio
async def test_first_turn_full_inject_then_refresh_on_version_bump():
    plugin, tmp = _build_plugin({"inject_project_context": True})
    workspace = str(_workspace_dir(tmp, "proj-a"))
    pm = plugin._get_project_memory()
    assert pm is not None

    # 第一轮（无历史）：应全量注入项目上下文 + 最近记忆
    request1 = _responses_request(workspace)
    out1 = await plugin.on_request(_ctx(request1))
    instructions1 = str(out1["instructions"] or "")
    assert "项目上下文" in instructions1
    assert "最近修改记忆" in instructions1
    assert "版本 v0" in instructions1
    assert "【项目记忆更新】" not in instructions1

    # 第二轮（有助手历史、版本未变）：不再注入
    request2 = _responses_request(workspace, with_history=True)
    out2 = await plugin.on_request(_ctx(request2))
    assert not str(out2["instructions"] or "").strip()

    # 记忆出现新的维护事件（版本自增）后，后续轮次触发轻量刷新
    pm.record_event(workspace, "learn", "20260801-new.learn.md")
    request3 = _responses_request(workspace, with_history=True)
    out3 = await plugin.on_request(_ctx(request3))
    instructions3 = str(out3["instructions"] or "")
    assert "【项目记忆更新】" in instructions3
    assert "版本 v1" in instructions3
    # 刷新注入不会把 memory.md 全文重复注入两次
    assert instructions3.count("【项目记忆更新】") == 1


@pytest.mark.asyncio
async def test_each_turn_injects_full_block_on_every_turn():
    # inject_project_context_each_turn 开启后：每个带工作区信号的请求都注入全量，
    # 第二轮（有助手历史、版本未变）也不再是“不注入”或“轻量刷新”，而是全量块。
    plugin, tmp = _build_plugin({"inject_project_context": True, "inject_project_context_each_turn": True})
    workspace = str(_workspace_dir(tmp, "proj-a"))

    out1 = await plugin.on_request(_ctx(_responses_request(workspace)))
    assert "## 项目上下文" in str(out1["instructions"] or "")
    assert "最近修改记忆" in str(out1["instructions"] or "")

    out2 = await plugin.on_request(_ctx(_responses_request(workspace, with_history=True)))
    instructions2 = str(out2["instructions"] or "")
    assert "## 项目上下文" in instructions2
    assert "最近修改记忆" in instructions2
    assert "【项目记忆更新】" not in instructions2

    # 开关开启但无工作区信号：仍透传不注入
    plain = {"model": "x", "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "你好"}]}]}
    out3 = await plugin.on_request(_ctx(plain))
    assert out3 is plain
    assert not str(out3.get("instructions") or "").strip()


@pytest.mark.asyncio
async def test_project_inject_only_when_workspace_signal_present():
    plugin, _tmp = _build_plugin({"inject_project_context": True})
    # 纯聊天（无 cwd / workspace_root）：透传不注入
    request = {"model": "x", "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "你好"}]}]}
    out = await plugin.on_request(_ctx(request))
    assert out is request
    assert not str(out.get("instructions") or "").strip()


@pytest.mark.asyncio
async def test_rag_only_path_unchanged_when_project_feature_off():
    plugin, _tmp = _build_plugin({"auto_inject": True})
    hits = [
        {
            "file_name": "kb.md",
            "title": "标题",
            "chunk_index": 0,
            "chunk_text": "这是命中的知识片段",
        }
    ]

    async def fake_retrieve(*args, **kwargs):
        return hits

    plugin._retrieve = fake_retrieve
    plugin._extract_user_question_for_responses = lambda req: "本项目是什么？"
    request = {
        "model": "x",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "本项目是什么？"}]}],
    }
    out = await plugin.on_request(_ctx(request))
    instructions = str(out["instructions"] or "")
    assert instructions.startswith("以下是与当前问题相关的参考资料。")
    assert "这是命中的知识片段" in instructions
    assert "项目上下文" not in instructions


@pytest.mark.asyncio
async def test_project_and_rag_combined_in_single_block():
    plugin, tmp = _build_plugin({"auto_inject": True, "inject_project_context": True})
    workspace = str(_workspace_dir(tmp, "proj-a"))
    hits = [{"file_name": "kb.md", "title": "标题", "chunk_index": 0, "chunk_text": "命中的片段内容"}]

    async def fake_retrieve(*args, **kwargs):
        return hits

    plugin._retrieve = fake_retrieve
    plugin._extract_user_question_for_responses = lambda req: "本项目是什么？"

    request = _responses_request(workspace)
    out = await plugin.on_request(_ctx(request))
    instructions = str(out["instructions"] or "")
    # 单段 instructions：既有项目上下文块，又含 RAG 参考资料，且不重复
    assert instructions.count("## 项目上下文") == 1
    assert instructions.count("参考资料：") == 1
    assert "命中的片段内容" in instructions


# ─────────────────── 事件钩子（落盘路径自动记录） ───────────────────

@pytest.mark.asyncio
async def test_save_payload_records_event_and_delete_records_event():
    plugin, tmp = _build_plugin({"auto_inject": False})
    workspace = str(_workspace_dir(tmp, "proj-a"))
    pm = plugin._get_project_memory()
    assert pm is not None

    saved = plugin._save_markdown_payload("proj-notes.md", "# 项目笔记\n\n内容。\n".encode("utf-8"), workspace)
    assert saved["ok"] is True
    assert pm.has_project(workspace)
    project = pm.read_project(workspace)
    assert project is not None
    assert project["events"][-1]["action"] == "added"
    assert project["events"][-1]["file_name"] == "proj-notes.md"
    assert "proj-notes.md" in project["memory_md"]

    # 后台 digest 任务会自动消费（无模型 → 降级文本摘要，不发网络请求）
    for _ in range(50):
        if pm.pending_count(workspace) == 0:
            break
        await asyncio.sleep(0.05)
    assert pm.pending_count(workspace) == 0

    # 删除 → deleted 事件
    plugin.delete_file(name="proj-notes.md", workspace_root=workspace, doc_id=saved["doc_id"])
    project2 = pm.read_project(workspace)
    assert project2 is not None
    assert project2["events"][-1]["action"] == "deleted"

    # 收尾：取消仍在冷却等待的后台 digest 任务，避免 pytest 循环关闭告警
    for task in list(plugin._project_digest_tasks.values()):
        task.cancel()
    await asyncio.gather(*list(plugin._project_digest_tasks.values()), return_exceptions=True)


@pytest.mark.asyncio
async def test_public_upload_does_not_create_project():
    plugin, _tmp = _build_plugin({"auto_inject": False})
    pm = plugin._get_project_memory()
    assert pm is not None
    saved = plugin._save_markdown_payload("public.md", "# 公共\n".encode("utf-8"), "")
    assert saved["ok"] is True
    assert pm.list_projects() == []
    # 该文件进入了 docs 目录（公共知识库），但 projects 下没有记忆目录
    assert (plugin._data_root / "docs").exists()


# ─────────────────── context.md：LLM 首次生成 / 增量更新 ───────────────────

async def _stub_context_chat(manager: ProjectMemory, text: str, *, capture=None, raise_error=False) -> None:
    """把 context 生成/修订的 chat 结果桩成纯文本（非 digest 的 JSON）。"""

    async def fake_post(base_url: str, payload: dict) -> dict:
        if capture is not None:
            capture["system"] = str(payload["messages"][0]["content"])
            capture["user"] = str(payload["messages"][1]["content"])
        if raise_error:
            raise RuntimeError("模拟 chat 服务不可用")
        return {"choices": [{"message": {"content": text}}]}

    manager._post_chat = fake_post  # type: ignore[method-assign]


def _make_project(manager: ProjectMemory, workspace: str) -> None:
    manager.ensure_skeleton(workspace, kb_material=[])


def _read_context(manager: ProjectMemory, workspace: str) -> str:
    return manager.snapshot(workspace)["context_md"]


@pytest.mark.asyncio
async def test_context_init_generates_and_marks_llm_source():
    pm = ProjectMemory(_tmp_root(), _FakeLogger())
    workspace = str(_workspace_dir(pathlib.Path(tempfile.mkdtemp())))
    _make_project(pm, workspace)
    assert pm.context_state(workspace)["source"] == "skeleton"

    captured = {}
    await _stub_context_chat(pm, "# 项目说明书\n\n- 技术栈：Python\n", capture=captured)
    result = await pm.run_context_init(workspace, base_url="http://x", model="m", kb_material=[])

    assert result["ok"] is True and result["written"] is True and result["degraded"] is False
    assert "# 项目说明书" in _read_context(pm, workspace)
    assert pm.context_state(workspace)["source"] == "llm"
    # 首次生成要求把真实材料放给模型：提示词应含仓库材料章节与工作区路径
    assert workspace in captured["user"]
    assert "## 仓库真实材料" in captured["user"]
    assert "现有 context.md 全文" not in captured["user"]  # init 不读旧文做修订

    # 幂等：已是 llm 维护，不再重写
    second = await pm.run_context_init(workspace, base_url="http://x", model="m")
    assert second["reason"] == "already_llm"


@pytest.mark.asyncio
async def test_context_init_never_overwrites_manual_context():
    pm = ProjectMemory(_tmp_root(), _FakeLogger())
    workspace = str(_workspace_dir(pathlib.Path(tempfile.mkdtemp())))
    _make_project(pm, workspace)
    context_path = pm.project_dir(workspace) / "context.md"
    manual_text = "# 我的自建说明\n\n（人工书写，无自动维护标记。）\n"
    context_path.write_text(manual_text, "utf-8")

    await _stub_context_chat(pm, "# 应该被拒绝的机器文本\n")
    result = await pm.run_context_init(workspace, base_url="http://x", model="m")
    assert result["reason"] == "manual_context"
    assert _read_context(pm, workspace) == manual_text


@pytest.mark.asyncio
async def test_context_init_no_model_keeps_skeleton():
    pm = ProjectMemory(_tmp_root(), _FakeLogger())
    workspace = str(_workspace_dir(pathlib.Path(tempfile.mkdtemp())))
    _make_project(pm, workspace)
    skeleton = _read_context(pm, workspace)

    result = await pm.run_context_init(workspace, base_url="http://x", model="", kb_material=[])
    assert result["degraded"] is True and result["reason"] == "no_model"
    assert pm.context_state(workspace)["source"] == "skeleton"
    assert _read_context(pm, workspace) == skeleton


@pytest.mark.asyncio
async def test_context_init_chat_failure_degrades_without_touching_file():
    pm = ProjectMemory(_tmp_root(), _FakeLogger())
    workspace = str(_workspace_dir(pathlib.Path(tempfile.mkdtemp())))
    _make_project(pm, workspace)
    skeleton = _read_context(pm, workspace)

    await _stub_context_chat(pm, "", raise_error=True)
    result = await pm.run_context_init(workspace, base_url="http://x", model="m")
    assert result["reason"] == "chat_failed" and result["degraded"] is True
    assert _read_context(pm, workspace) == skeleton


@pytest.mark.asyncio
async def test_context_update_minimal_revision_flow():
    pm = ProjectMemory(_tmp_root(), _FakeLogger())
    workspace = str(_workspace_dir(pathlib.Path(tempfile.mkdtemp())))
    _make_project(pm, workspace)
    captured = {}

    await _stub_context_chat(pm, "# 项目说明书\n\n- 命令：旧命令\n", capture=captured)
    init = await pm.run_context_init(workspace, base_url="http://x", model="m")
    assert init["written"] is True

    # 增量更新提示词应携带现有全文 + 最新材料
    await _stub_context_chat(pm, "# 项目说明书\n\n- 命令：新命令\n", capture=captured)
    updated = await pm.run_context_update(workspace, base_url="http://x", model="m", kb_material=[], force=True)
    assert updated["written"] is True
    assert "新命令" in _read_context(pm, workspace)
    assert "## 现有 context.md 全文" in captured["user"]

    # 刚刷新过：自动模式受 1 天冷却保护，直接跳过（不产生 LLM 调用）
    skipped = await pm.run_context_update(workspace, base_url="http://x", model="m")
    assert skipped["reason"] == "cooldown"

    # force 且模型输出与现状逐字一致：不落盘
    await _stub_context_chat(pm, "# 项目说明书\n\n- 命令：新命令\n")
    unchanged = await pm.run_context_update(workspace, base_url="http://x", model="m", force=True)
    assert unchanged["reason"] == "unchanged" and unchanged["written"] is False


@pytest.mark.asyncio
async def test_context_update_skips_when_not_llm_managed():
    pm = ProjectMemory(_tmp_root(), _FakeLogger())
    workspace = str(_workspace_dir(pathlib.Path(tempfile.mkdtemp())))
    _make_project(pm, workspace)

    result = await pm.run_context_update(workspace, base_url="http://x", model="m", force=False)
    assert result["reason"] == "not_llm"


@pytest.mark.asyncio
async def test_context_due_refresh_respects_cooldown_and_meta_age():
    pm = ProjectMemory(_tmp_root(), _FakeLogger())
    workspace = str(_workspace_dir(pathlib.Path(tempfile.mkdtemp())))
    _make_project(pm, workspace)
    await _stub_context_chat(pm, "# 说明\n")
    await pm.run_context_init(workspace, base_url="http://x", model="m")

    # 刚生成：未到冷却
    assert pm.context_due_for_refresh(workspace) is False

    # 模拟 2 天前刷新过 → 到期
    meta_path = pm.project_dir(workspace) / "meta.json"
    meta = json.loads(meta_path.read_text("utf-8"))
    meta["last_context_refresh_at"] = "2000-01-01T00:00:00+00:00"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    assert pm.context_due_for_refresh(workspace) is True

    # 骨架未 llm 化时不参与轮询
    pm2 = ProjectMemory(_tmp_root(), _FakeLogger())
    ws2 = str(_workspace_dir(pathlib.Path(tempfile.mkdtemp())))
    _make_project(pm2, ws2)
    assert pm2.context_due_for_refresh(ws2) is False


def test_repo_survey_lists_real_files_and_tree():
    pm = ProjectMemory(_tmp_root(), _FakeLogger())
    base = pathlib.Path(tempfile.mkdtemp())
    workspace = base / "survey-proj"
    (workspace / "src").mkdir(parents=True)
    (workspace / "README.md").write_text("# 测试工程\n\n本地描述。\n", "utf-8")
    (workspace / "package.json").write_text('{"scripts": {"test": "vitest run"}}', "utf-8")
    (workspace / "node_modules").mkdir()

    survey = pm._repo_survey(str(workspace))
    assert "README.md" in survey and "本地描述" in survey
    assert "package.json" in survey and "vitest run" in survey
    assert "src/" in survey
    # 噪音目录不进入材料（避免误导模型）
    assert "node_modules" not in survey


@pytest.mark.asyncio
async def test_plugin_maintain_project_context_route_backing():
    plugin, tmp = _build_plugin({"chat_model": "test-model", "inject_project_context": True})
    workspace = str(_workspace_dir(tmp, "proj-ctx"))
    pm = plugin._get_project_memory()
    assert pm is not None
    await _stub_context_chat(pm, "# 项目说明书（LLM）\n\n- 技术栈：Python\n")

    result = await plugin.maintain_project_context(workspace)
    assert result["ok"] is True
    assert result["context"]["written"] is True
    assert result["context"]["degraded"] is False
    project = pm.read_project(workspace)
    assert project is not None
    assert project["context_source"] == "llm"
    assert "LLM" in project["context_md"]

    # context 刷新不改记忆 revision（revision 只由事件/digest 驱动）
    meta_path = pm.project_dir(workspace) / "meta.json"
    meta = json.loads(meta_path.read_text("utf-8"))
    assert meta["revision"] == 0

    # 收尾：取消后台 context/消化任务，避免 pytest 循环关闭告警
    for attr in ("_project_context_tasks", "_project_digest_tasks"):
        for task in list(getattr(plugin, attr, {}).values()):
            task.cancel()
        await asyncio.gather(*list(getattr(plugin, attr, {}).values()), return_exceptions=True)
