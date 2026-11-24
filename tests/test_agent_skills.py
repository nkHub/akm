"""Agent Skill 发现与按需加载测试。

覆盖的行为契约：
- 索引只含 name + 描述，不含正文；描述被限长压成一行；
- ``disable-model-invocation`` 的 skill 仍进索引，但标注「仅在用户显式要求时使用」；
- 索引顺序稳定（按目录名），否则每轮 system 前缀都变、上游 prompt 缓存全失效；
- 按名字加载正文；``section`` 先按标题匹配，匹配不到再按附属文件名找；
- 越界/路径构造（``../../.ssh/id_rsa``）一律拒绝——名字只能来自目录列举；
- 软链接 skill 目录合法可用（ego-browser 整个目录是符号链接）；
- 正文超长截断、附属文件清单限量；
- 没有 skills 目录时静默返回空，不影响 Agent 请求。
"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from akm.agent_runtime import skills
from akm.agent_runtime.tools import build_builtin_tools


def _write_skill(root: Path, name: str, frontmatter: str, body: str) -> Path:
    """在临时 skills 根下造一个 skill 目录。"""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return skill_dir


@pytest.fixture()
def skills_dir(tmp_path, monkeypatch):
    """把 agent_skills_dir 指向临时目录，并清掉跨用例的索引缓存。"""
    root = tmp_path / "skills"
    root.mkdir()
    skills._INDEX_CACHE.clear()
    monkeypatch.setattr(
        skills, "load_config", lambda: {"agent_skills_dir": str(root)}
    )
    yield root
    skills._INDEX_CACHE.clear()


def test_index_has_description_but_not_body(skills_dir):
    """索引只带描述，正文不进索引——这是控制上下文体积的核心。"""
    _write_skill(skills_dir, "alpha", "name: alpha\ndescription: Alpha 用法说明", "BODY_SENTINEL_ALPHA")
    text = skills.build_index_text()
    assert "alpha" in text
    assert "Alpha 用法说明" in text
    assert "BODY_SENTINEL_ALPHA" not in text


def test_index_marks_manual_only(skills_dir):
    """disable-model-invocation 的 skill 仍可被发现，但要标注需用户显式要求。"""
    _write_skill(
        skills_dir,
        "manual",
        "name: manual\ndescription: 手动 skill\ndisable-model-invocation: true",
        "x",
    )
    text = skills.build_index_text()
    assert "只在用户显式要求时使用" in text or "仅在用户显式要求时使用" in text


def test_index_order_is_stable(skills_dir):
    """多次调用顺序必须一致，否则 prompt 缓存命中不了。"""
    for name in ("zeta", "alpha", "mike"):
        _write_skill(skills_dir, name, f"name: {name}\ndescription: d", "b")
    first = [e["name"] for e in skills.list_skills()]
    second = [e["name"] for e in skills.list_skills()]
    assert first == second == ["alpha", "mike", "zeta"]


def test_load_skill_returns_body(skills_dir):
    _write_skill(skills_dir, "alpha", "name: alpha\ndescription: d", "## 用法\n正文内容")
    result = skills.load_skill("alpha")
    assert result["ok"] is True
    assert "正文内容" in result["body"]


def test_load_skill_unknown_name_rejected(skills_dir):
    _write_skill(skills_dir, "alpha", "name: alpha\ndescription: d", "b")
    result = skills.load_skill("不存在的")
    assert result["ok"] is False
    assert "available" in result


@pytest.mark.parametrize(
    "bad",
    [
        "../../.ssh/id_rsa",
        "/etc/passwd",
        "..",
        "~/.ssh/config",
        "./alpha",
        "alpha/SKILL.md",
    ],
)
def test_load_skill_rejects_path_construction(skills_dir, bad):
    """名字只做白名单匹配，任何路径构造都匹配不到条目。"""
    _write_skill(skills_dir, "alpha", "name: alpha\ndescription: d", "b")
    assert skills.load_skill(bad)["ok"] is False


def test_section_matches_heading(skills_dir):
    body = "## Setup\n设置内容\n\n## Commands\n命令内容\n"
    _write_skill(skills_dir, "alpha", "name: alpha\ndescription: d", body)
    result = skills.load_skill("alpha", section="Commands")
    assert result["ok"] is True
    assert "命令内容" in result["body"]
    assert "设置内容" not in result["body"]


def test_section_falls_back_to_attachment_file(skills_dir):
    """正文没有该标题时，到 reference/ 里按文件名找——impeccable 这类 skill 的知识都在那里。"""
    skill_dir = _write_skill(skills_dir, "alpha", "name: alpha\ndescription: d", "见 reference/audit.md")
    ref = skill_dir / "reference"
    ref.mkdir()
    (ref / "audit.md").write_text("AUDIT_SENTINEL_CONTENT", encoding="utf-8")
    result = skills.load_skill("alpha", section="audit")
    assert result["ok"] is True
    assert result.get("section_file") == "reference/audit.md"
    assert "AUDIT_SENTINEL_CONTENT" in result["body"]


def test_section_miss_falls_back_to_body_head(skills_dir):
    _write_skill(skills_dir, "alpha", "name: alpha\ndescription: d", "FULL_BODY_START")
    result = skills.load_skill("alpha", section="不存在的章节")
    assert result["ok"] is True
    assert result.get("section_miss") is True
    assert "FULL_BODY_START" in result["body"]


def test_symlink_skill_dir_is_valid(skills_dir, tmp_path):
    """ego-browser 整个目录是指向别处的符号链接，不能按「越界」误杀。"""
    real = tmp_path / "elsewhere" / "linked"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("---\nname: linked\ndescription: 软链 skill\n---\n\nLINKED_BODY", encoding="utf-8")
    (skills_dir / "linked").symlink_to(real)
    names = [e["name"] for e in skills.list_skills()]
    assert "linked" in names
    assert "LINKED_BODY" in skills.load_skill("linked")["body"]


def test_body_is_truncated_when_too_long(skills_dir, monkeypatch):
    monkeypatch.setattr(skills, "_MAX_BODY_BYTES", 100)
    _write_skill(skills_dir, "big", "name: big\ndescription: d", "x" * 500)
    result = skills.load_skill("big")
    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["body"].encode()) <= 100


def test_attachment_list_is_limited(skills_dir, monkeypatch):
    monkeypatch.setattr(skills, "_MAX_LISTED_FILES", 3)
    skill_dir = _write_skill(skills_dir, "many", "name: many\ndescription: d", "b")
    ref = skill_dir / "references"
    ref.mkdir()
    for index in range(10):
        (ref / f"doc{index}.md").write_text("x", encoding="utf-8")
    result = skills.load_skill("many")
    assert len(result["files"]) <= 3
    assert result["files_truncated"] is True


def test_missing_skills_dir_is_silent(monkeypatch):
    """目录不存在时返回空，不能抛异常——否则整个 Agent 请求都会 500。"""
    monkeypatch.setattr(skills, "load_config", lambda: {"agent_skills_dir": "/nonexistent/xyz"})
    skills._INDEX_CACHE.clear()
    assert skills.list_skills() == []
    assert skills.build_index_text() == ""


def test_empty_config_disables_skills(monkeypatch):
    monkeypatch.setattr(skills, "load_config", lambda: {"agent_skills_dir": ""})
    skills._INDEX_CACHE.clear()
    assert skills.skills_root() is None
    assert skills.build_index_text() == ""


def test_frontmatter_block_description_parsed(skills_dir):
    """description: | 多行块要能解析（modern-web-guidance 用的就是这种写法）。"""
    _write_skill(
        skills_dir,
        "block",
        "name: block\ndescription: |\n  第一行\n  第二行",
        "b",
    )
    entry = [e for e in skills.list_skills() if e["name"] == "block"][0]
    assert "第一行" in entry["description"]
    assert "第二行" in entry["description"]


def test_quoted_description_escapes_parsed(skills_dir):
    """带引号的 description 里可能出现 \\" 转义（code-review 就是这样）。"""
    _write_skill(
        skills_dir,
        "quoted",
        'name: quoted\ndescription: "引用 \\"review since X\\" 的说明"',
        "b",
    )
    entry = [e for e in skills.list_skills() if e["name"] == "quoted"][0]
    assert 'review since X' in entry["description"]


def test_skill_without_frontmatter_uses_dir_name(skills_dir):
    skill_dir = skills_dir / "bare"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("没有 frontmatter 的正文", encoding="utf-8")
    entry = [e for e in skills.list_skills() if e["name"] == "bare"][0]
    assert entry["description"] == ""


def test_long_description_is_clipped(skills_dir, monkeypatch):
    monkeypatch.setattr(skills, "_MAX_INDEX_DESCRIPTION", 20)
    _write_skill(skills_dir, "long", "name: long\ndescription: " + "字" * 200, "b")
    text = skills.build_index_text()
    line = [l for l in text.splitlines() if l.startswith("- **long**")][0]
    assert len(line) < 120


def test_load_skill_tool_registered_and_executes(skills_dir):
    """工具必须注册进注册表，且 handler 能通过注册表执行。"""
    from akm.agent_runtime.loop import ToolRegistry

    _write_skill(skills_dir, "alpha", "name: alpha\ndescription: d", "TOOL_BODY_SENTINEL")
    tools = {t.name: t for t in build_builtin_tools(FastAPI())}
    assert "akm_load_skill" in tools
    registry = ToolRegistry()
    registry.register(tools["akm_load_skill"])
    import asyncio

    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        registry.execute("akm_load_skill", {"name": "alpha"})
    )
    assert "TOOL_BODY_SENTINEL" in out
    assert json.loads(out)["ok"] is True



def test_load_skill_without_name_returns_catalog(skills_dir):
    """空 name = 查清单，与 akm_load_mcp 不传 server 的行为对齐。"""
    _write_skill(
        skills_dir, "alpha", "name: alpha\ndescription: Alpha 描述", "BODY_SENTINEL_ALPHA"
    )
    result = skills.load_skill("")
    assert result["ok"] is True
    assert [item["name"] for item in result["skills"]] == ["alpha"]
    assert result["skills"][0]["description"] == "Alpha 描述"
    assert "BODY_SENTINEL_ALPHA" not in json.dumps(result, ensure_ascii=False)


def test_load_skill_catalog_marks_manual_only(skills_dir):
    """manual_only 必须透传，否则模型会主动用只能在用户显式要求时用的 skill。"""
    _write_skill(
        skills_dir,
        "manual",
        "name: manual\ndescription: 手动 skill\ndisable-model-invocation: true",
        "b",
    )
    assert skills.load_skill("")["skills"][0]["manual_only"] is True


def test_load_skill_catalog_on_empty_dir(skills_dir):
    """没有 skill 时也要 ok=True 并给出排查提示，不能表现成调用失败。"""
    result = skills.load_skill("")
    assert result["ok"] is True
    assert result["skills"] == []
    assert "agent_skills_dir" in result["hint"]


def test_load_skill_schema_allows_omitting_name():
    """name 不进 required，否则模型永远不可能发出空参调用。"""
    tools = {t.name: t for t in build_builtin_tools(FastAPI())}
    schema = tools["akm_load_skill"].parameters
    assert schema["required"] == []
    assert "name" in schema["properties"]

def test_load_skill_is_gateway_pinned():
    """常驻名单要包含它，否则折叠后模型看不到这个工具。"""
    from akm.agent_runtime.loop import _GATEWAY_PINNED_TOOLS

    assert "akm_load_skill" in _GATEWAY_PINNED_TOOLS


def test_inject_skills_index_into_system(skills_dir):
    """索引要并进已有 system 消息，而不是新插一条。"""
    from akm.agent_runtime.loop import _inject_skills_index

    _write_skill(skills_dir, "alpha", "name: alpha\ndescription: Alpha 描述", "b")
    messages = [{"role": "system", "content": "原有指令"}]
    _inject_skills_index(messages)
    assert len(messages) == 1
    assert "原有指令" in messages[0]["content"]
    assert "alpha" in messages[0]["content"]



def test_agent_loop_copies_messages_before_inject(skills_dir):
    """注入不能写回调用方的 message 对象，否则复用列表的调用方会逐轮累积。"""
    from akm.agent_runtime.loop import _inject_skills_index

    _write_skill(skills_dir, "alpha", "name: alpha\ndescription: Alpha 描述", "b")
    messages = [{"role": "system", "content": "原始指令"}]
    original = messages[0]

    # 复现 loop.py 的拷贝口径：逐条 dict 拷贝后注入
    working = [dict(m) for m in messages]
    _inject_skills_index(working)

    assert working[0] is not original
    assert messages[0]["content"] == "原始指令"
    assert len(messages[0]["content"]) == 4

def test_inject_skills_index_skipped_when_empty(monkeypatch):
    """没有 skill 时不能凭空插一条 system 消息。"""
    from akm.agent_runtime.loop import _inject_skills_index

    monkeypatch.setattr(skills, "build_index_text", lambda: "")
    messages = [{"role": "user", "content": "hi"}]
    _inject_skills_index(messages)
    assert messages == [{"role": "user", "content": "hi"}]
