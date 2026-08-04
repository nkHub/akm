"""Agent 工作区文件工具与安全边界测试。"""

import asyncio
import json
from pathlib import Path

import pytest

from akm.agent_runtime.tools import (
    build_workspace_tools,
    _safe_resolve_workspace_path,
    _workspace_root,
)

# 供测试复用的工作区配置
WORKSPACE = "/tmp/akm-test-workspace"


def _workspace_cfg(**overrides):
    """构造工作区相关配置 dict，覆盖默认值。"""
    cfg = {
        "agent_workspace_root": WORKSPACE,
        "agent_write_tools_enabled": True,
        "agent_run_shell_enabled": True,
    }
    cfg.update(overrides)
    return cfg


def _handlers(monkeypatch, **cfg):
    """注册工作区工具并返回 {name: handler}，配置从 cfg 生成。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config", lambda: _workspace_cfg(**cfg)
    )
    return {t.name: t.handler for t in build_workspace_tools()}


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    """每个测试使用独立临时目录作为工作区，避免跨测试污染。"""
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr("akm.agent_runtime.tools._workspace_root", lambda: root.resolve())
    yield root


# ── 路径沙箱 ──


def test_safe_resolve_rejects_outside_absolute_path():
    """绝对路径越界应被拒绝。"""
    with pytest.raises(ValueError, match="超出工作区"):
        _safe_resolve_workspace_path("/etc/passwd")


def test_safe_resolve_rejects_traversal():
    """相对路径中的 .. 穿越应被拒绝。"""
    with pytest.raises(ValueError, match="超出工作区"):
        _safe_resolve_workspace_path("../../etc/passwd")


def test_safe_resolve_accepts_inside_path():
    """工作区内的相对/绝对路径应被正常解析。"""
    p = _safe_resolve_workspace_path("sub/file.txt", must_exist=False)
    assert str(p).endswith("/sub/file.txt")


def test_safe_resolve_rejects_symlink_escape(tmp_path):
    """软链接指向工作区外时应被拒绝（resolve 后越界）。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = tmp_path / "ws" / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="超出工作区"):
        _safe_resolve_workspace_path("link.txt")


def test_workspace_root_returns_none_when_unconfigured(monkeypatch):
    """未配置 agent_workspace_root 时 _workspace_root 应返回 None。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config", lambda: {"agent_workspace_root": ""}
    )
    assert _workspace_root() is None


# ── 读工具 ──


@pytest.mark.asyncio
async def test_read_file_within_workspace(_workspace, monkeypatch):
    """读取工作区内文件应返回内容与行信息。"""
    f = _workspace / "hello.txt"
    f.write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_read_file"](path="hello.txt"))

    assert out["path"].endswith("/hello.txt")
    assert "第一行" in out["content"]
    assert out["start_line"] == 0


@pytest.mark.asyncio
async def test_read_file_outside_workspace_rejected(_workspace, monkeypatch):
    """读取工作区外文件应返回错误，而不是泄露内容。"""
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_read_file"](path="/etc/passwd"))

    assert "error" in out
    assert "超出工作区" in out["error"]


@pytest.mark.asyncio
async def test_read_file_missing_returns_error(_workspace, monkeypatch):
    """读取不存在的文件应返回错误。"""
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_read_file"](path="nope.txt"))

    assert "error" in out


@pytest.mark.asyncio
async def test_list_dir_shows_entries(_workspace, monkeypatch):
    """list_dir 应返回工作区内的目录条目。"""
    (_workspace / "a.txt").write_text("x", encoding="utf-8")
    (_workspace / "sub").mkdir()
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_list_dir"](path=""))

    names = {e["name"] for e in out["entries"]}
    assert "a.txt" in names
    assert "sub" in names


@pytest.mark.asyncio
async def test_glob_matches_within_workspace(_workspace, monkeypatch):
    """glob 应返回相对路径且不允许越界模式。"""
    (_workspace / "main.py").write_text("", encoding="utf-8")
    (_workspace / "src").mkdir()
    (_workspace / "src" / "lib.py").write_text("", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_glob"](pattern="**/*.py"))
    assert "main.py" in out["matches"]
    assert "src/lib.py" in out["matches"]

    bad = json.loads(await handlers["akm_glob"](pattern="../**"))
    assert "error" in bad


@pytest.mark.asyncio
async def test_grep_searches_content(_workspace, monkeypatch):
    """grep 应返回命中文件、行号与行内容。"""
    (_workspace / "code.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_grep"](pattern="def foo"))

    assert out["total"] == 1
    assert out["results"][0]["file"] == "code.py"
    assert out["results"][0]["line"] == 1


@pytest.mark.asyncio
async def test_file_info_returns_metadata(_workspace, monkeypatch):
    """file_info 应返回类型、大小与修改时间。"""
    f = _workspace / "meta.txt"
    f.write_text("hello", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_file_info"](path="meta.txt"))

    assert out["type"] == "file"
    assert out["size"] == 5


# ── 写工具开关 ──


@pytest.mark.asyncio
async def test_write_tool_requires_enabled_flag(_workspace, monkeypatch):
    """agent_write_tools_enabled=false 时写工具不注册（模型不可见即不可调）。"""
    handlers = _handlers(monkeypatch, agent_write_tools_enabled=False)

    assert "akm_write_file" not in handlers
    assert "akm_edit_file" not in handlers


@pytest.mark.asyncio
async def test_write_file_and_edit(_workspace, monkeypatch):
    """写文件并编辑：覆盖写入、精确替换。"""
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_write_file"](path="code.py", content="print('old')\n"))
    assert out["ok"] is True
    assert (_workspace / "code.py").read_text(encoding="utf-8") == "print('old')\n"

    out = json.loads(await handlers["akm_edit_file"](path="code.py", old_string="old", new_string="new"))
    assert out["replaced"] == 1
    assert (_workspace / "code.py").read_text(encoding="utf-8") == "print('new')\n"


@pytest.mark.asyncio
async def test_write_file_rejects_outside_workspace(_workspace, monkeypatch):
    """写文件到工作区外应被拒绝。"""
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_write_file"](path="/tmp/evil.txt", content="x"))

    assert "error" in out


@pytest.mark.asyncio
async def test_delete_rejects_workspace_root(_workspace, monkeypatch):
    """删除工作区根目录应被拒绝。"""
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_delete_file"](path=".", recursive=True))

    assert "error" in out
    assert "工作区根目录" in out["error"]


@pytest.mark.asyncio
async def test_make_dir_and_delete(_workspace, monkeypatch):
    """创建目录并删除文件。"""
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_make_dir"](path="a/b/c"))
    assert out["ok"] is True
    assert (_workspace / "a" / "b" / "c").is_dir()

    f = _workspace / "a" / "tmp.txt"
    f.write_text("x", encoding="utf-8")
    out = json.loads(await handlers["akm_delete_file"](path="a/tmp.txt"))
    assert out["ok"] is True
    assert not f.exists()


# ── shell 工具开关 ──


@pytest.mark.asyncio
async def test_run_shell_requires_enabled_flag(_workspace, monkeypatch):
    """agent_run_shell_enabled=false 时 shell 工具不注册。"""
    handlers = _handlers(monkeypatch, agent_run_shell_enabled=False)

    assert "akm_run_shell" not in handlers


@pytest.mark.asyncio
async def test_run_shell_executes_with_workspace_cwd(_workspace, monkeypatch):
    """shell 工具应以工作区为 cwd 执行并返回输出与退出码。"""
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_run_shell"](command="pwd && echo ok"))

    assert out["exit_code"] == 0
    assert str(_workspace) in out["output"]
    assert "ok" in out["output"]


@pytest.mark.asyncio
async def test_run_shell_timeout(_workspace, monkeypatch):
    """shell 工具超时应终止命令并返回错误。"""
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_run_shell"](command="sleep 5", timeout=1))

    assert "超时" in out["error"]


# ── 工具注册集合 ──


def test_workspace_tools_registration(monkeypatch):
    """写工具与 shell 工具按开关注册；读工具始终注册。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {
            "agent_workspace_root": WORKSPACE,
            "agent_write_tools_enabled": False,
            "agent_run_shell_enabled": False,
        },
    )
    names = {t.name for t in build_workspace_tools()}
    assert {"akm_read_file", "akm_list_dir", "akm_glob", "akm_grep", "akm_file_info"} <= names
    assert "akm_write_file" not in names
    assert "akm_run_shell" not in names

    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {
            "agent_workspace_root": WORKSPACE,
            "agent_write_tools_enabled": True,
            "agent_run_shell_enabled": True,
        },
    )
    names = {t.name for t in build_workspace_tools()}
    assert "akm_write_file" in names
    assert "akm_edit_file" in names
    assert "akm_make_dir" in names
    assert "akm_delete_file" in names
    assert "akm_run_shell" in names
