"""Agent 工作区文件工具与安全边界测试。"""

import asyncio
import json
from pathlib import Path

import pytest

from akm.agent_runtime.tools import (
    build_workspace_tools,
    _safe_resolve_workspace_path,
    _seatbelt_available,
    _seatbelt_profile,
    _workspace_root,
    reset_request_workspace_root,
    set_request_workspace_root,
)

# 供测试复用的工作区配置
WORKSPACE = "/tmp/akm-test-workspace"


def _workspace_cfg(**overrides):
    """构造工作区相关配置 dict，覆盖默认值。"""
    cfg = {
        "agent_workspace_root": WORKSPACE,
        "agent_write_tools_enabled": True,
        "agent_run_shell_enabled": True,
        # 既有用例大多数是「直接改没读过的文件」，这里默认关掉先读守卫；
        # 守卫本身由 test_edit_requires_read_when_guard_enabled 专门覆盖。
        "agent_write_requires_read": False,
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


def test_request_workspace_root_override(monkeypatch):
    """请求级 workspace_root 只能选择全局工作区内的子目录。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config", lambda: {"agent_workspace_root": "/global/ws"}
    )
    assert str(_workspace_root()) == str(Path("/global/ws").resolve())

    token = set_request_workspace_root("/global/ws/project")
    try:
        assert str(_workspace_root()) == str(Path("/global/ws/project").resolve())
    finally:
        reset_request_workspace_root(token)

    assert str(_workspace_root()) == str(Path("/global/ws").resolve())


def test_request_workspace_root_rejects_outside_global_root(monkeypatch):
    """请求级工作区不得把文件工具的沙箱范围扩大到全局根目录之外。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config", lambda: {"agent_workspace_root": "/global/ws"}
    )
    token = set_request_workspace_root("/other/ws")
    try:
        with pytest.raises(ValueError, match="必须位于"):
            _workspace_root()
    finally:
        reset_request_workspace_root(token)


def test_request_workspace_root_empty_keeps_global(monkeypatch):
    """空字符串的请求级覆盖不应生效，仍使用全局配置。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config", lambda: {"agent_workspace_root": "/global/ws"}
    )
    token = set_request_workspace_root("")
    try:
        assert str(_workspace_root()) == str(Path("/global/ws").resolve())
    finally:
        reset_request_workspace_root(token)


# ── 读工具 ──


@pytest.mark.asyncio
async def test_read_file_within_workspace(_workspace, monkeypatch):
    """读取工作区内文件应返回带绝对行号的纯文本窗口与版本 tag。"""
    f = _workspace / "hello.txt"
    f.write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = await handlers["akm_read_file"](path="hello.txt")

    # 成功路径返回纯文本（不是 JSON）：[path#TAG] 头 + 1-based 绝对行号 + footer
    assert out.startswith("[hello.txt#")
    assert "1: 第一行" in out
    assert "3: 第三行" in out
    assert "已读到文件结尾，共 3 行" in out


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
async def test_grep_skips_symlink_outside_workspace(_workspace, monkeypatch, tmp_path):
    """grep 必须逐项校验软链接真实路径，不能读取工作区外的文件内容。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret marker", encoding="utf-8")
    (_workspace / "outside-link.txt").symlink_to(outside)
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_grep"](pattern="secret marker"))

    assert out["total"] == 0


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


# ── 结构化编辑（行号模式）──


@pytest.mark.asyncio
async def test_edit_file_line_mode_replaces_range(_workspace, monkeypatch):
    """行号模式应按行区间整体替换并返回行数变化。"""
    f = _workspace / "code.py"
    f.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_edit_file"](path="code.py", start_line=2, end_line=3, new_content="A\nB"))

    assert out["ok"] is True
    assert out["old_lines"] == 2
    assert out["new_lines"] == 2
    assert f.read_text(encoding="utf-8") == "line1\nA\nB\nline4\n"


@pytest.mark.asyncio
async def test_edit_file_line_mode_single_line_without_end(_workspace, monkeypatch):
    """行号模式不传 end_line 时应只替换一行，且保留尾部换行风格。"""
    f = _workspace / "code.py"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_edit_file"](path="code.py", start_line=2, new_content="two"))

    assert out["ok"] is True
    assert out["old_lines"] == 1
    assert f.read_text(encoding="utf-8") == "line1\ntwo\nline3\n"


@pytest.mark.asyncio
async def test_edit_file_line_mode_anchor_mismatch_rejected(_workspace, monkeypatch):
    """行号模式锚点校验失败应拒绝编辑，防止行号漂移后改错位置。"""
    f = _workspace / "code.py"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_edit_file"](path="code.py", start_line=2, end_line=3, old_string="不存在的锚点", new_content="x"))

    assert "error" in out
    assert "锚点校验失败" in out["error"]


@pytest.mark.asyncio
async def test_edit_file_line_mode_out_of_range_rejected(_workspace, monkeypatch):
    """行号越界应被拒绝并给出文件实际行数。"""
    f = _workspace / "code.py"
    f.write_text("a\nb\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_edit_file"](path="code.py", start_line=5, new_content="x"))

    assert "error" in out
    assert "越界" in out["error"]


@pytest.mark.asyncio
async def test_edit_file_content_mode_still_works(_workspace, monkeypatch):
    """内容模式（旧参数）应保持原有替换行为。"""
    f = _workspace / "code.py"
    f.write_text("print('old')\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_edit_file"](path="code.py", old_string="old", new_string="new"))

    assert out["replaced"] == 1
    assert f.read_text(encoding="utf-8") == "print('new')\n"


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
async def test_delete_rejects_directory_without_recursive(_workspace, monkeypatch):
    """删除目录但未设置 recursive=true 应被拒绝（目录完好）。"""
    handlers = _handlers(monkeypatch)

    d = _workspace / "adir"
    d.mkdir()
    (d / "f1.txt").write_text("x", encoding="utf-8")

    out = json.loads(await handlers["akm_delete_file"](path="adir"))

    assert "error" in out
    assert "recursive=true" in out["error"]
    assert d.is_dir()  # 目录及其内容完好


@pytest.mark.asyncio
async def test_delete_directory_recursive(_workspace, monkeypatch):
    """recursive=true 时递归删除目录及其所有内容。"""
    handlers = _handlers(monkeypatch)

    d = _workspace / "adir"
    d.mkdir()
    (d / "sub").mkdir()
    (d / "f1.txt").write_text("x", encoding="utf-8")
    (d / "sub" / "f2.txt").write_text("y", encoding="utf-8")

    out = json.loads(await handlers["akm_delete_file"](path="adir", recursive=True))

    assert out["ok"] is True
    assert not d.exists()  # 目录连同子目录与文件全部删除


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
    """shell 工具直接执行命令字符串，且以工作区为 cwd。"""
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_run_shell"](command="pwd && echo ok"))

    assert out["exit_code"] == 0
    assert str(_workspace) in out["output"]
    assert "ok" in out["output"]


@pytest.mark.asyncio
async def test_run_shell_timeout(_workspace, monkeypatch):
    """shell 命令超时应被终止并返回错误。"""
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_run_shell"](command="sleep 5", timeout=1))

    assert "超时" in out["error"]


# ── shell 沙箱（seatbelt）──


def test_seatbelt_profile_denies_sensitive_paths(_workspace):
    """生成的 SBPL profile 应放行工作区读写并拒绝敏感路径。"""
    profile = _seatbelt_profile(_workspace)

    assert f'(allow file-write* (subpath "{_workspace}")' in profile
    assert f'(allow file-read* (subpath "{_workspace}")' in profile
    assert "(deny file-write*)" in profile
    assert "/.ssh" in profile
    assert "/.akm" in profile
    assert "/Library" in profile
    assert "/.zshrc" in profile
    assert "/.zsh_history" in profile
    assert "/.gitconfig" in profile
    assert "/.npmrc" in profile
    assert '"/etc"' in profile
    assert '"/private/etc"' in profile
    assert '"/var/log"' in profile
    assert '"/private/var/log"' in profile
    assert '"/tmp"' in profile
    assert '"/private/tmp"' in profile
    assert f'(deny file-read-metadata (subpath "{Path.home()}"))' in profile
    assert f'(allow file-read-metadata (subpath "{_workspace}")' in profile
    assert profile.index("(deny file-write*)") < profile.index(
        f'(allow file-write* (subpath "{_workspace}")'
    )
    assert profile.index('(deny file-read*') < profile.index(
        f'(allow file-read* (subpath "{_workspace}")'
    )


@pytest.mark.skipif(
    not _seatbelt_available(), reason="当前环境不提供 sandbox_init_with_parameters"
)
@pytest.mark.asyncio
async def test_run_shell_sandbox_rejects_sensitive_read(_workspace, monkeypatch):
    """开启 agent_run_shell_sandbox 后，shell 读取 /etc/passwd 应被沙箱拒绝。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools._seatbelt_api_ok", True, raising=False
    )
    handlers = _handlers(monkeypatch, agent_run_shell_sandbox=True)

    out = json.loads(await handlers["akm_run_shell"](command="cat /etc/passwd"))

    assert out["exit_code"] != 0
    assert "Operation not permitted" in out["output"]


@pytest.mark.skipif(
    not _seatbelt_available(), reason="当前环境不提供 sandbox_init_with_parameters"
)
@pytest.mark.asyncio
async def test_run_shell_sandbox_allows_workspace_write(_workspace, monkeypatch):
    """开启沙箱后，工作区内的写入仍应正常。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools._seatbelt_api_ok", True, raising=False
    )
    handlers = _handlers(monkeypatch, agent_run_shell_sandbox=True)

    out = json.loads(
        await handlers["akm_run_shell"](command="echo sandbox-ok >> sb.txt && cat sb.txt")
    )

    assert out["exit_code"] == 0
    assert "sandbox-ok" in out["output"]
    assert (_workspace / "sb.txt").exists()


@pytest.mark.asyncio
async def test_run_shell_strips_pythonhome_pollution(_workspace, monkeypatch):
    """PYTHONHOME/PYTHONPATH 被 AKM 进程污染时，shell 子进程应剥离后执行。

    py2app 打包的 app 内嵌 Python 运行时会设置 PYTHONHOME 指向 app 的
    Resources/lib/python3.12，若不剥离，shell 里外部 python3 会因找不到
    encodings 启动即崩。此处模拟污染后验证子进程里的 python3 仍可用。
    """
    monkeypatch.setenv("PYTHONHOME", "/nonexistent/pythonhome")
    monkeypatch.setenv("PYTHONPATH", "/fake/pythonpath")
    handlers = _handlers(monkeypatch)

    out = json.loads(
        await handlers["akm_run_shell"](
            command='python3 -c "import sys; print(\'py-ok\')"'
        )
    )

    assert out["exit_code"] == 0
    assert "py-ok" in out["output"]


# ── git 工具 ──


@pytest.mark.asyncio
async def test_run_git_requires_enabled_flag(_workspace, monkeypatch):
    """agent_git_enabled=false 时 git 工具不注册。"""
    handlers = _handlers(monkeypatch, agent_git_enabled=False)

    assert "akm_run_git" not in handlers


@pytest.mark.asyncio
async def test_run_git_status_in_workspace(_workspace, monkeypatch):
    """git 工具应以工作区为 cwd 执行 git 命令并返回输出与退出码。"""
    import subprocess as _sp

    _sp.run(["git", "init"], cwd=str(_workspace), capture_output=True, text=True)
    (_workspace / "a.txt").write_text("x", encoding="utf-8")
    handlers = _handlers(monkeypatch, agent_git_enabled=True)

    out = json.loads(await handlers["akm_run_git"](operation="status"))

    assert out["exit_code"] == 0
    assert "a.txt" in out["output"]


@pytest.mark.asyncio
async def test_run_git_rejects_unknown_operation(_workspace, monkeypatch):
    """Git 工具只支持服务端定义的结构化操作。"""
    handlers = _handlers(monkeypatch, agent_git_enabled=True)

    out = json.loads(await handlers["akm_run_git"](operation="clone"))

    assert "error" in out
    assert "不支持" in out["error"]


@pytest.mark.asyncio
async def test_run_git_rejects_paths_outside_workspace(_workspace, monkeypatch):
    """Git 路径参数不能通过绝对路径或 .. 逃离工作区。"""
    handlers = _handlers(monkeypatch, agent_git_enabled=True)

    out = json.loads(await handlers["akm_run_git"](operation="add", paths=["../outside.txt"]))

    assert "error" in out
    assert "工作区内的相对路径" in out["error"]


@pytest.mark.asyncio
async def test_run_git_commit_requires_message(_workspace, monkeypatch):
    """提交操作必须提供受限长度的提交说明。"""
    handlers = _handlers(monkeypatch, agent_git_enabled=True)

    out = json.loads(await handlers["akm_run_git"](operation="commit"))

    assert "error" in out
    assert "message" in out["error"]


@pytest.mark.asyncio
async def test_run_git_dir_switches_repo(_workspace, monkeypatch):
    """dir 参数切换到工作区内任意子目录仓库（等价 git -C）。"""
    import subprocess as _sp

    repo_a = _workspace / "repoA"
    repo_b = _workspace / "repoB"
    repo_a.mkdir()
    repo_b.mkdir()
    _sp.run(["git", "init", "-q"], cwd=str(repo_a), capture_output=True, text=True)
    _sp.run(["git", "init", "-q"], cwd=str(repo_b), capture_output=True, text=True)
    (repo_a / "only-a.txt").write_text("a", encoding="utf-8")
    (repo_b / "only-b.txt").write_text("b", encoding="utf-8")
    handlers = _handlers(monkeypatch, agent_git_enabled=True)

    out_a = json.loads(await handlers["akm_run_git"](operation="status", dir="repoA"))
    out_b = json.loads(await handlers["akm_run_git"](operation="status", dir=str(repo_b)))

    assert out_a["exit_code"] == 0
    assert "only-a.txt" in out_a["output"]
    assert "only-b.txt" not in out_a["output"]
    assert out_b["exit_code"] == 0
    assert "only-b.txt" in out_b["output"]


@pytest.mark.asyncio
async def test_run_git_rejects_dir_outside_workspace(_workspace, monkeypatch):
    """dir 越出工作区（绝对路径指向外部）必须拒绝。"""
    import tempfile

    outside = tempfile.mkdtemp()
    handlers = _handlers(monkeypatch, agent_git_enabled=True)

    out = json.loads(await handlers["akm_run_git"](operation="status", dir=outside))

    assert "error" in out
    assert "dir 必须位于工作区内" in out["error"]


@pytest.mark.asyncio
async def test_run_git_rejects_dir_not_exist(_workspace, monkeypatch):
    """dir 指向工作区内不存在的目录必须拒绝。"""
    handlers = _handlers(monkeypatch, agent_git_enabled=True)

    out = json.loads(await handlers["akm_run_git"](operation="status", dir="no/such/dir"))

    assert "error" in out
    assert "dir 不是存在的目录" in out["error"]


# ── 工具注册集合 ──


def test_workspace_tools_registration(monkeypatch):
    """写工具、shell 工具与 git 工具按开关注册；读工具始终注册。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {
            "agent_workspace_root": WORKSPACE,
            "agent_write_tools_enabled": False,
            "agent_run_shell_enabled": False,
            "agent_git_enabled": False,
        },
    )
    names = {t.name for t in build_workspace_tools()}
    assert {"akm_read_file", "akm_list_dir", "akm_glob", "akm_grep", "akm_file_info"} <= names
    assert "akm_write_file" not in names
    assert "akm_run_shell" not in names
    assert "akm_run_git" not in names

    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: {
            "agent_workspace_root": WORKSPACE,
            "agent_write_tools_enabled": True,
            "agent_run_shell_enabled": True,
            "agent_git_enabled": True,
        },
    )
    names = {t.name for t in build_workspace_tools()}
    assert "akm_write_file" in names
    assert "akm_edit_file" in names
    assert "akm_make_dir" in names
    assert "akm_delete_file" in names
    assert "akm_run_shell" in names
    assert "akm_run_git" in names


def test_shell_and_git_schema(monkeypatch):
    """shell 工具暴露 command 字符串；git 工具保持固定 operation，不暴露自由命令。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: _workspace_cfg(agent_git_enabled=True),
    )
    tools = {tool.name: tool for tool in build_workspace_tools()}

    shell_properties = tools["akm_run_shell"].parameters["properties"]
    git_properties = tools["akm_run_git"].parameters["properties"]
    assert "command" in shell_properties
    assert shell_properties["command"]["type"] == "string"
    assert "operation" in git_properties
    assert "command" not in git_properties
    assert "dir" in git_properties
    assert git_properties["dir"]["type"] == "string"


# ── 读写工具契约：行号 / tag / 截断 / 唯一匹配 / 敏感名单 ──


@pytest.mark.asyncio
async def test_read_file_footer_gives_next_offset(_workspace, monkeypatch):
    """分页读取时 footer 必须直接给出下一次要用的 offset（1-based）。"""
    (_workspace / "many.txt").write_text(
        "\n".join(f"row-{i}" for i in range(1, 11)) + "\n", encoding="utf-8"
    )
    handlers = _handlers(monkeypatch)

    out = await handlers["akm_read_file"](path="many.txt", offset=4, limit=3)

    assert "4: row-4" in out
    assert "6: row-6" in out
    assert "继续读取用 offset=7" in out


@pytest.mark.asyncio
async def test_read_file_rejects_binary(_workspace, monkeypatch):
    """二进制内容不能让模型看到乱码：直接给出可操作错误。"""
    (_workspace / "blob.bin").write_bytes(b"\x00\x01\x02PNG")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_read_file"](path="blob.bin"))

    assert "二进制" in out["error"]


@pytest.mark.asyncio
async def test_read_file_decodes_gb18030(_workspace, monkeypatch):
    """GBK/GB18030 文件应按回退编码正确解码，而不是静默乱码。"""
    (_workspace / "gbk.txt").write_bytes("中文内容\n".encode("gb18030"))
    handlers = _handlers(monkeypatch)

    out = await handlers["akm_read_file"](path="gbk.txt")

    assert "中文内容" in out
    assert "gb18030" in out


@pytest.mark.asyncio
async def test_sensitive_paths_blocked(_workspace, monkeypatch):
    """.env / 私钥即使在沙箱内也必须被文件工具拒绝，模板文件放行。"""
    (_workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (_workspace / "id_rsa").write_text("PRIVATE\n", encoding="utf-8")
    (_workspace / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    env_out = json.loads(await handlers["akm_read_file"](path=".env"))
    key_out = json.loads(await handlers["akm_read_file"](path="id_rsa"))
    sample = await handlers["akm_read_file"](path=".env.example")

    assert "禁止" in env_out["error"]
    assert "禁止" in key_out["error"]
    assert "SECRET=" in sample


@pytest.mark.asyncio
async def test_edit_requires_read_when_guard_enabled(_workspace, monkeypatch):
    """开启先读守卫时，没读过就编辑必须被拒绝，读过之后才放行。"""
    (_workspace / "code.py").write_text("print('old')\n", encoding="utf-8")
    handlers = _handlers(monkeypatch, agent_write_requires_read=True)

    blocked = json.loads(
        await handlers["akm_edit_file"](path="code.py", old_string="old", new_string="new")
    )
    assert "必须先读取" in blocked["error"]

    await handlers["akm_read_file"](path="code.py")
    ok = json.loads(
        await handlers["akm_edit_file"](path="code.py", old_string="old", new_string="new")
    )
    assert ok["ok"] is True
    assert (_workspace / "code.py").read_text(encoding="utf-8") == "print('new')\n"


@pytest.mark.asyncio
async def test_edit_rejects_ambiguous_old_string(_workspace, monkeypatch):
    """old_string 多处命中时必须报错，不能静默改掉第一处。"""
    f = _workspace / "dup.py"
    f.write_text("x = 1\nx = 1\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(
        await handlers["akm_edit_file"](path="dup.py", old_string="x = 1", new_string="x = 2")
    )

    assert "2 次" in out["error"]
    assert f.read_text(encoding="utf-8") == "x = 1\nx = 1\n"

    out2 = json.loads(
        await handlers["akm_edit_file"](
            path="dup.py", old_string="x = 1", new_string="x = 2", replace_all=True
        )
    )
    assert out2["replaced"] == 2
    assert f.read_text(encoding="utf-8") == "x = 2\nx = 2\n"


@pytest.mark.asyncio
async def test_edit_tag_mismatch_rejected(_workspace, monkeypatch):
    """tag 与磁盘内容不一致时拒绝编辑，避免基于旧快照改写。"""
    f = _workspace / "tagged.py"
    f.write_text("a = 1\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(
        await handlers["akm_edit_file"](
            path="tagged.py", old_string="a = 1", new_string="a = 2", tag="0000"
        )
    )

    assert "tag 与文件当前内容不一致" in out["error"]
    assert f.read_text(encoding="utf-8") == "a = 1\n"


@pytest.mark.asyncio
async def test_edit_line_mode_reports_new_tag_and_diff(_workspace, monkeypatch):
    """编辑成功应回新 tag 与 diff，模型可据此确认改动结果。"""
    f = _workspace / "lines.py"
    f.write_text("one\ntwo\nthree\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(
        await handlers["akm_edit_file"](path="lines.py", start_line=2, new_content="TWO")
    )

    assert out["ok"] is True
    assert len(out["tag"]) == 4
    assert "-two" in out["diff"] and "+TWO" in out["diff"]
    assert f.read_text(encoding="utf-8") == "one\nTWO\nthree\n"


@pytest.mark.asyncio
async def test_write_overwrite_requires_read(_workspace, monkeypatch):
    """覆盖已存在文件在守卫开启时要求先读；新建文件不受限。"""
    (_workspace / "cfg.json").write_text('{"a": 1}', encoding="utf-8")
    handlers = _handlers(monkeypatch, agent_write_requires_read=True)

    blocked = json.loads(await handlers["akm_write_file"](path="cfg.json", content='{"a": 2}'))
    assert "必须先读取" in blocked["error"]

    created = json.loads(await handlers["akm_write_file"](path="new.json", content="{}"))
    assert created["ok"] is True and created["created"] is True

    await handlers["akm_read_file"](path="cfg.json")
    overwritten = json.loads(await handlers["akm_write_file"](path="cfg.json", content='{"a": 2}'))
    assert overwritten["ok"] is True
    assert "diff" in overwritten


@pytest.mark.asyncio
async def test_grep_returns_all_matches_per_file(_workspace, monkeypatch):
    """同一文件的多处命中必须全部返回（早先只返回第一条）。"""
    (_workspace / "multi.py").write_text("hit one\nnope\nhit two\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_grep"](pattern="hit"))

    lines = [r["line"] for r in out["results"] if r["file"] == "multi.py"]
    assert lines == [1, 3]
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_grep_include_and_output_modes(_workspace, monkeypatch):
    """include 过滤与 files_with_matches / count 两种输出模式。"""
    (_workspace / "a.py").write_text("needle\nneedle\n", encoding="utf-8")
    (_workspace / "b.txt").write_text("needle\n", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    only_py = json.loads(await handlers["akm_grep"](pattern="needle", include="*.py"))
    assert only_py["file_count"] == 1
    assert only_py["match_count"] == 2

    files_only = json.loads(
        await handlers["akm_grep"](pattern="needle", output_mode="files_with_matches")
    )
    assert set(files_only["matches"]) == {"a.py", "b.txt"}
    assert files_only["results"] == []

    counts = json.loads(await handlers["akm_grep"](pattern="needle", output_mode="count"))
    assert counts["counts"]["a.py"] == 2
    assert counts["counts"]["b.txt"] == 1


@pytest.mark.asyncio
async def test_grep_marks_truncation(_workspace, monkeypatch):
    """命中数达到 head_limit 时必须标记 truncated 并给 notice。"""
    (_workspace / "many.txt").write_text(
        "\n".join("hit" for _ in range(10)) + "\n", encoding="utf-8"
    )
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_grep"](pattern="hit", head_limit=3))

    assert len(out["results"]) == 3
    assert out["truncated"] is True
    assert "notice" in out


@pytest.mark.asyncio
async def test_glob_limit_marks_truncated(_workspace, monkeypatch):
    """glob 结果被 limit 截断时必须如实标记（早先只有扫描预算才标记）。"""
    for name in ("a.txt", "b.txt", "c.txt"):
        (_workspace / name).write_text("x", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_glob"](pattern="*.txt", limit=2))

    assert len(out["matches"]) == 2
    assert out["total"] == 3
    assert out["truncated"] is True


@pytest.mark.asyncio
async def test_list_dir_pagination(_workspace, monkeypatch):
    """list_dir 支持 offset/limit 并返回 next_offset。"""
    for i in range(5):
        (_workspace / f"f{i}.txt").write_text("x", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    first = json.loads(await handlers["akm_list_dir"](path="", offset=0, limit=2))
    assert len(first["entries"]) == 2
    assert first["truncated"] is True
    assert first["next_offset"] == 2

    rest = json.loads(await handlers["akm_list_dir"](path="", offset=4, limit=2))
    assert len(rest["entries"]) == 1
    assert rest["truncated"] is False


@pytest.mark.asyncio
async def test_move_and_copy_tools(_workspace, monkeypatch):
    """移动/复制工具应一步完成，不需要 read+write+delete。"""
    (_workspace / "src.txt").write_text("payload", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    copied = json.loads(await handlers["akm_copy_file"](path="src.txt", dest="dup.txt"))
    assert copied["ok"] is True
    assert (_workspace / "dup.txt").read_text(encoding="utf-8") == "payload"

    moved = json.loads(await handlers["akm_move_file"](path="dup.txt", dest="sub/renamed.txt"))
    assert moved["ok"] is True
    assert (_workspace / "sub" / "renamed.txt").read_text(encoding="utf-8") == "payload"
    assert not (_workspace / "dup.txt").exists()


@pytest.mark.asyncio
async def test_delete_reports_entries_removed(_workspace, monkeypatch):
    """递归删除应返回被删条目数，便于确认删了多少。"""
    d = _workspace / "tree"
    (d / "nested").mkdir(parents=True)
    (d / "a.txt").write_text("x", encoding="utf-8")
    (d / "nested" / "b.txt").write_text("y", encoding="utf-8")
    handlers = _handlers(monkeypatch)

    out = json.loads(await handlers["akm_delete_file"](path="tree", recursive=True))

    assert out["ok"] is True
    assert out["entries_removed"] >= 2
    assert not d.exists()


def test_workspace_tools_registration_includes_move_copy(monkeypatch):
    """写工具开关打开时，移动/复制工具也要注册，并带上结果预算。"""
    monkeypatch.setattr(
        "akm.agent_runtime.tools.load_config",
        lambda: _workspace_cfg(),
    )
    tools = {tool.name: tool for tool in build_workspace_tools()}

    assert {"akm_move_file", "akm_copy_file"} <= set(tools)
    # 文本型工具声明了结果预算；读取本身已有字节上限，所以不需要额外预算
    assert tools["akm_grep"].max_result_chars == 200_000
    assert tools["akm_read_file"].max_result_chars == 0


