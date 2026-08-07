"""akm agent CLI 子包测试：会话持久化、SSE 消费、渲染、交互循环与命令入口。"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from akm.agent_cli.repl import AgentClient, Repl, build_session
from akm.agent_cli.sessions import SessionStore
from akm.agent_cli.sse import SSEConsumer


def _setup_tmp_env(monkeypatch):
    """复用与 test_cli 相同的隔离环境，确保会话目录落在 tmpdir 下。"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setenv("HOME", tmpdir)
    monkeypatch.setattr("akm.config.CONFIG_DIR", tmpdir)
    monkeypatch.setattr("akm.config.CONFIG_PATH", str(Path(tmpdir) / "config.json"))
    return tmpdir


# ── SessionStore ──

def test_session_store_roundtrip(tmp_path):
    """会话能完整写盘并读回，消息不丢失。"""
    store = SessionStore(tmp_path)
    session = {
        "name": "s1",
        "model": "gpt-4o",
        "workspace_root": "/tmp/ws",
        "messages": [{"role": "user", "content": "你好"}],
    }
    store.save(session)
    loaded = store.load("s1")
    assert loaded is not None
    assert loaded["name"] == "s1"
    assert loaded["model"] == "gpt-4o"
    assert loaded["workspace_root"] == "/tmp/ws"
    assert loaded["messages"] == [{"role": "user", "content": "你好"}]
    assert loaded["created_at"]  # save 自动补全
    assert loaded["updated_at"]


def test_session_store_list_sorted_by_updated(tmp_path):
    """list() 按更新时间倒序，并给出消息数。"""
    store = SessionStore(tmp_path)
    store.save({"name": "old", "messages": []})
    store.save({"name": "new", "messages": [{"role": "user", "content": "x"}]})
    sessions = store.list()
    assert [s["name"] for s in sessions] == ["new", "old"]
    assert sessions[0]["message_count"] == 1


def test_session_store_delete_and_missing(tmp_path):
    """删除存在/不存在的会话返回正确布尔值。"""
    store = SessionStore(tmp_path)
    store.save({"name": "a", "messages": []})
    assert store.delete("a") is True
    assert store.load("a") is None
    assert store.delete("a") is False


def test_session_store_invalid_name(tmp_path):
    """会话名不允许路径穿越。"""
    store = SessionStore(tmp_path)
    with pytest.raises(ValueError):
        store._path("../evil")
    with pytest.raises(ValueError):
        store._path("a/b")


def test_session_store_next_name_unique(tmp_path):
    """next_name 生成的名字不与已有会话冲突。"""
    store = SessionStore(tmp_path)
    first = store.next_name()
    store.save({"name": first, "messages": []})
    second = store.next_name()
    assert second != first
    assert store.load(second) is None


# ── SSEConsumer ──

def _sse_event(event: str, data: dict) -> str:
    """按服务端格式构造一段 SSE 文本。"""
    payload = json.dumps({"event": event, "data": data})
    return f"data: {payload}\n\n"


def test_sse_consumer_single_block():
    """单个完整事件块能解析出 (event, data)。"""
    c = SSEConsumer()
    events = c.feed(_sse_event("final", {"ok": True}))
    assert len(events) == 1
    assert events[0]["event"] == "final"
    assert events[0]["data"] == {"ok": True}


def test_sse_consumer_split_chunks():
    """事件被切成任意小块的字节流也能完整重组。"""
    text = _sse_event("model_delta", {"content": "hello"}) + _sse_event("final", {"ok": 1})
    c = SSEConsumer()
    all_events = []
    # 每次只喂 1 个字符，模拟任意边界切分
    for ch in text:
        all_events.extend(c.feed(ch))
    all_events.extend(c.finish())
    assert [e["event"] for e in all_events] == ["model_delta", "final"]


def test_sse_consumer_ignores_non_data_lines():
    """event:/id: 等非 data 行不影响解析。"""
    block = "event: custom\nid: 3\ndata: {\"event\":\"final\",\"data\":{}}\n\n"
    c = SSEConsumer()
    events = c.feed(block) + c.finish()
    assert len(events) == 1
    assert events[0]["event"] == "final"


def test_sse_consumer_finish_remaining():
    """流末尾没有空行分隔符时，finish() 补出最后一个事件。"""
    c = SSEConsumer()
    c.feed('data: {"event":"final","data":{"ok":true}}')
    events = c.finish()
    assert len(events) == 1
    assert events[0]["event"] == "final"


# ── build_session ──

def test_build_session_new_with_auto_name(tmp_path):
    """新会话自动生成 name，参数正确填充。"""
    store = SessionStore(tmp_path)
    session = build_session(store, model="deepseek", workspace_root="/w")
    assert session["name"]
    assert session["model"] == "deepseek"
    assert session["workspace_root"] == "/w"
    assert session["messages"] == []


def test_build_session_resume(tmp_path):
    """resume 载入已有会话，缺失时报错。"""
    store = SessionStore(tmp_path)
    store.save({"name": "s1", "messages": [{"role": "user", "content": "hi"}]})
    session = build_session(store, resume="s1")
    assert session["messages"] == [{"role": "user", "content": "hi"}]
    with pytest.raises(ValueError):
        build_session(store, resume="missing")


# ── Repl 斜杠命令 ──

def _make_repl(tmp_path, session=None):
    store = SessionStore(tmp_path)
    client = AgentClient("http://127.0.0.1:8800")
    if session is None:
        session = build_session(store, name="t")
    return Repl(store, client, session)


def test_repl_command_help_prints_help():
    """/help 打印命令帮助且不退出。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        repl = _make_repl(Path(td))
        out = []
        repl.print_fn = out.append
        assert repl._handle_command("/help") is True
        assert any("可用命令" in line for line in out)


def test_repl_command_quit_returns_false(tmp_path):
    """/quit 返回 False 表示退出。"""
    repl = _make_repl(tmp_path)
    assert repl._handle_command("/quit") is False
    assert repl._handle_command("/exit") is False


def test_repl_command_unknown(tmp_path):
    """未知命令给出提示但不退出。"""
    repl = _make_repl(tmp_path)
    out = []
    repl.print_fn = out.append
    result = repl._handle_command("/foobar")
    assert result is None
    assert any("未知命令" in line for line in out)


def test_repl_command_model_switch(tmp_path):
    """/model 切换当前会话模型并写盘。"""
    repl = _make_repl(tmp_path)
    out = []
    repl.print_fn = out.append
    assert repl._handle_command("/model deepseek-chat") is True
    assert repl.session["model"] == "deepseek-chat"
    assert any("已切换" in line for line in out)
    # 写盘验证
    loaded = repl.store.load("t")
    assert loaded["model"] == "deepseek-chat"


def test_repl_command_workspace_switch(tmp_path):
    """/workspace 切换到存在的目录，不存在的目录拒绝。"""
    repl = _make_repl(tmp_path)
    out = []
    repl.print_fn = out.append
    workdir = tmp_path / "ws"
    workdir.mkdir()
    assert repl._handle_command(f"/workspace {workdir}") is True
    assert repl.session["workspace_root"] == str(workdir)
    assert repl._handle_command("/workspace /no/such/dir") is None
    assert any("目录不存在" in line for line in out)


def test_repl_command_clear(tmp_path):
    """/clear 清空消息。"""
    repl = _make_repl(tmp_path)
    repl.session["messages"] = [{"role": "user", "content": "x"}]
    assert repl._handle_command("/clear") is True
    assert repl.session["messages"] == []


def test_repl_command_sessions_and_resume(tmp_path):
    """/sessions 列出历史，/resume 载入已有会话。"""
    repl = _make_repl(tmp_path)
    repl.store.save({"name": "old", "messages": [{"role": "assistant", "content": "a"}]})
    out = []
    repl.print_fn = out.append
    assert repl._handle_command("/sessions") is True
    assert any("old" in line for line in out)

    assert repl._handle_command("/resume old") is True
    assert repl.session["name"] == "old"
    assert len(repl.session["messages"]) == 1
    assert repl._handle_command("/resume nope") is None


# ── 交互主循环（注入输入/输出） ──

def _fake_client(events, *, final_messages=None, error=None):
    """构造一个假的 AgentClient.stream，产出预置事件序列。"""

    class _Fake:
        def __init__(self):
            self.stream_calls = 0

        async def stream(self, messages, **kwargs):
            self.stream_calls += 1
            for ev, data in events:
                yield ev, data

        async def run(self, messages, **kwargs):
            return {"ok": True, "messages": final_messages, "final_message": {"content": "ok"}}

        async def aclose(self):
            return None

    return _Fake()


def test_repl_run_stream_saves_session(tmp_path):
    """流式一轮后，final 里的 messages 被写回并落盘。"""
    store = SessionStore(tmp_path)
    session = build_session(store, name="t")
    final_msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]
    events = [("final", {"final_message": {"content": "你好"}, "messages": final_msgs})]
    client = _fake_client(events, final_messages=final_msgs)
    repl = Repl(store, client, session, input_fn=lambda _: "hi", color=False)
    repl._ask = lambda: "hi"

    async def _run():
        await repl._run_stream_round("hi")

    asyncio.run(_run())
    assert repl.session["messages"] == final_msgs
    loaded = store.load("t")
    assert loaded["messages"] == final_msgs


def test_repl_run_stream_error_rolls_back_user_message(tmp_path):
    """流式请求 error 事件时不污染会话（回滚刚追加的 user 消息）。"""
    store = SessionStore(tmp_path)
    session = build_session(store, name="t")
    events = [("error", {"error": "boom"})]
    client = _fake_client(events)
    repl = Repl(store, client, session, input_fn=lambda _: "hi", color=False)

    async def _run():
        await repl._run_stream_round("hi")

    asyncio.run(_run())
    # user 消息被回滚，会话回到空
    assert repl.session["messages"] == []


def test_repl_run_stream_cancelled_error_propagates(tmp_path):
    """流式过程中 CancelledError（Ctrl+C）必须向上传播，不能当普通失败吞掉。

    旧行为会打印「请求失败」后回到 akm>，用户感知为 TUI 突然停住但仍卡在提示符。
    """
    store = SessionStore(tmp_path)
    session = build_session(store, name="t")

    class _CancelClient:
        async def stream(self, messages, **kwargs):
            yield "model_delta", {"content": "开始"}
            raise asyncio.CancelledError()

        async def aclose(self):
            return None

    repl = Repl(store, _CancelClient(), session, input_fn=lambda _: "hi", color=False)
    outputs: list[str] = []
    repl.print_fn = outputs.append

    async def _run():
        await repl._run_stream_round("hi")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())
    # 取消时回滚本轮 user 消息，会话不被污染
    assert repl.session["messages"] == []
    # 不得把取消伪装成「请求失败」
    assert not any("请求失败" in line for line in outputs)


def test_repl_run_async_cancelled_during_stream_exits(tmp_path):
    """整段 REPL 在流式过程中被 cancel 时，run_async 应中止而不是继续读下一行。"""
    store = SessionStore(tmp_path)
    session = build_session(store, name="t")

    class _SlowClient:
        async def stream(self, messages, **kwargs):
            yield "model_delta", {"content": "正在输出"}
            await asyncio.sleep(5)
            yield "final", {"final_message": {"content": "完"}, "messages": []}

        async def aclose(self):
            return None

    inputs = iter(["hello", "should-not-reach"])
    repl = Repl(
        store,
        _SlowClient(),
        session,
        input_fn=lambda _: next(inputs),
        color=False,
        enable_live=False,
    )

    async def _scenario():
        task = asyncio.create_task(repl.run_async(stream=True))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_scenario())
    # 第二轮输入不应被消费（取消后 REPL 已退出）
    assert next(inputs) == "should-not-reach"


def test_repl_run_stream_skips_empty_delta(tmp_path):
    """内容为空的 reasoning_delta / model_delta 不应触发打印（防刷屏）。"""
    store = SessionStore(tmp_path)
    session = build_session(store, name="t")
    final_msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]
    events = [
        ("reasoning_delta", {"content": ""}),
        ("model_delta", {"content": ""}),
        ("reasoning_delta", {"content": "思考…"}),
        ("model_delta", {"content": "正文"}),
        ("final", {"final_message": {"content": "你好"}, "messages": final_msgs}),
    ]
    client = _fake_client(events, final_messages=final_msgs)
    printed = []

    def _p(text):
        printed.append(text)

    repl = Repl(store, client, session, input_fn=lambda _: "hi", print_fn=_p, color=False)
    repl._ask = lambda: "hi"

    async def _run():
        await repl._run_stream_round("hi")

    asyncio.run(_run())
    # 默认折叠思考：空 delta 跳过，非空 reasoning_delta 也不打印；
    # model_delta 缓冲到 final 统一 markdown 渲染，正文以 final_message.content 为准
    assert "思考…" not in printed
    assert "正文" not in printed
    assert any("你好" in p for p in printed)


def test_repl_run_stream_show_reasoning_prints_delta(tmp_path):
    """开启 show_reasoning 时，非空的 reasoning_delta 内容会被打印。"""
    store = SessionStore(tmp_path)
    session = build_session(store, name="t")
    final_msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]
    events = [
        ("reasoning_delta", {"content": "思考过程A"}),
        ("model_delta", {"content": "正文"}),
        ("final", {"final_message": {"content": "你好"}, "messages": final_msgs}),
    ]
    client = _fake_client(events, final_messages=final_msgs)
    printed = []

    def _p(text):
        printed.append(text)

    repl = Repl(
        store,
        client,
        session,
        input_fn=lambda _: "hi",
        print_fn=_p,
        color=False,
        show_reasoning=True,
    )
    repl._ask = lambda: "hi"

    async def _run():
        await repl._run_stream_round("hi")

    asyncio.run(_run())
    assert printed.count("思考过程A") == 1
    # 正文缓冲到 final 统一渲染，以 final_message.content 为准
    assert "正文" not in printed
    assert any("你好" in p for p in printed)


def test_repl_run_async_exit_on_quit(tmp_path):
    """输入 /quit 后主循环退出。"""
    store = SessionStore(tmp_path)
    session = build_session(store, name="t")
    client = _fake_client([])
    repl = Repl(
        store,
        client,
        session,
        input_fn=lambda _: "/quit",
        print_fn=lambda _: None,
        color=False,
    )

    async def _run():
        await repl.run_async()

    asyncio.run(_run())


# ── AgentClient 请求组装 ──

def test_agent_client_body_builds_correct_request(monkeypatch, tmp_path):
    """AgentClient.stream 组装出正确的 /v1/agent 请求体。"""
    captured = {}

    class FakeResponse:
        status_code = 200

        def __init__(self):
            self.closed = False

        async def aiter_bytes(self):
            payload = json.dumps({"event": "final", "data": {"ok": True}})
            yield f"data: {payload}\n\n".encode()

        async def aclose(self):
            self.closed = True

    class FakeContext:
        def __init__(self, resp):
            self.resp = resp

        async def __aenter__(self):
            return self.resp

        async def __aexit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self):
            self._calls = []

        def stream(self, method, url, json=None, headers=None):
            captured.update(method=method, url=url, json=json, headers=headers)
            return FakeContext(FakeResponse())

        async def aclose(self):
            return None

    import httpx as _httpx

    client = AgentClient("http://127.0.0.1:8800", token="sekret")
    client._client = FakeClient()

    async def _run():
        events = []
        async for ev, data in client.stream(
            [{"role": "user", "content": "hi"}],
            model="deepseek",
            instructions="be nice",
            api_path="chat/completions",
            workspace_root="/ws",
        ):
            events.append((ev, data))
        return events

    events = asyncio.run(_run())
    assert captured["method"] == "POST"
    assert captured["url"] == "http://127.0.0.1:8800/v1/agent"
    assert captured["json"]["stream"] is True
    assert captured["json"]["model"] == "deepseek"
    assert captured["json"]["instructions"] == "be nice"
    assert captured["json"]["workspace_root"] == "/ws"
    assert captured["headers"]["Authorization"] == "Bearer sekret"
    assert events == [("final", {"ok": True})]


def test_agent_client_stream_http_error(monkeypatch, tmp_path):
    """HTTP >=400 时产出 error 事件。"""

    class FakeResponse:
        status_code = 500

        async def aread(self):
            return b"internal error"

        async def aclose(self):
            return None

    class FakeContext:
        def __init__(self, resp):
            self.resp = resp

        async def __aenter__(self):
            return self.resp

        async def __aexit__(self, *exc):
            return False

    class FakeClient:
        def stream(self, method, url, json=None, headers=None):
            return FakeContext(FakeResponse())

        async def aclose(self):
            return None

    client = AgentClient("http://127.0.0.1:8800")
    client._client = FakeClient()

    async def _run():
        events = []
        async for ev, data in client.stream(
            [{"role": "user", "content": "hi"}],
            model="",
            instructions="",
            api_path="chat/completions",
            workspace_root="",
        ):
            events.append((ev, data))
        return events

    events = asyncio.run(_run())
    assert events[0][0] == "error"
    assert "500" in events[0][1]["error"]


# ── CLI 命令入口 ──

def test_cli_agent_help():
    """akm agent 命令树可用：无子命令直接进会话，session 用于历史管理。"""
    from akm.cli import main

    result = CliRunner().invoke(main, ["agent", "--help"])
    assert result.exit_code == 0
    assert "session" in result.output
    assert "run" not in result.output


def test_cli_agent_session_list_empty(monkeypatch):
    """无会话时 list 输出提示。"""
    _setup_tmp_env(monkeypatch)
    from akm.cli import main

    result = CliRunner().invoke(main, ["agent", "session", "list"])
    assert result.exit_code == 0
    assert "暂无历史会话" in result.output


def test_cli_agent_session_show_rm(monkeypatch):
    """show / rm 对已有会话正常工作。"""
    tmpdir = _setup_tmp_env(monkeypatch)
    # 会话目录随 monkeypatch 后的 CONFIG_DIR 走，CLI 内部使用默认目录
    store = SessionStore()
    store.save({"name": "s1", "messages": [{"role": "user", "content": "hello world"}]})

    from akm.cli import main

    result = CliRunner().invoke(main, ["agent", "session", "show", "s1"])
    assert result.exit_code == 0
    assert "s1" in result.output
    assert "hello world" in result.output

    result = CliRunner().invoke(main, ["agent", "session", "rm", "s1"])
    assert result.exit_code == 0
    assert "已删除" in result.output
    assert store.load("s1") is None


def test_cli_agent_session_rm_missing(monkeypatch):
    """删除不存在的会话报错。"""
    _setup_tmp_env(monkeypatch)
    from akm.cli import main

    result = CliRunner().invoke(main, ["agent", "session", "rm", "nope"])
    assert result.exit_code != 0
    assert "会话不存在" in result.output


def test_cli_agent_run_requires_service(monkeypatch):
    """服务未运行时启动 agent 会话给出提示。"""
    _setup_tmp_env(monkeypatch)
    from akm.cli import main

    # monkeypatch _check_service 让它抛错，模拟服务未启动
    import akm.agent_cli.cli as agent_cli

    def _fail():
        raise click.ClickException("本地代理服务未运行。请先执行 `akm serve` 启动服务后再使用 agent。")

    import click

    monkeypatch.setattr(agent_cli, "_check_service", _fail)
    result = CliRunner().invoke(main, ["agent"])
    assert result.exit_code != 0
    assert "本地代理服务未运行" in result.output


def test_resolve_default_model():
    """默认模型固定为 deepseek-v4-flash，显式传入时原样返回。"""
    from akm.agent_cli.cli import _resolve_default_model

    assert _resolve_default_model("") == "deepseek-v4-flash"
    assert _resolve_default_model("gpt-5.6-terra") == "gpt-5.6-terra"


def test_resolve_default_workspace(tmp_path, monkeypatch):
    """未指定工作区时默认当前目录，显式传入时原样返回。"""
    from akm.agent_cli.cli import _resolve_default_workspace

    monkeypatch.chdir(tmp_path)
    assert _resolve_default_workspace("") == str(tmp_path)
    assert _resolve_default_workspace("  ") == str(tmp_path)
    assert _resolve_default_workspace("/custom/ws") == "/custom/ws"


# ── LiveStreamPanel 三区渲染 ──


def test_live_panel_accumulates_three_sections():
    """Live 关闭时按顺序累积思考 / 工具 / 正文段，不进入 Live。"""
    from akm.agent_cli.render import LiveStreamPanel

    panel = LiveStreamPanel(enable=False, color=False)
    with panel:
        panel.add_reasoning("思考A")
        panel.add_body("正文")
        panel.add_tool("akm_read_file", {"path": "x"})
        panel.add_tool_result("akm_read_file", "内容")
    panel.finish("**最终**")
    # 段序列按事件到达顺序组织：思考 / 正文 / 工具（结果被丢弃，仅调用行）
    assert [seg["type"] for seg in panel._segments] == [
        "thinking", "text", "tool",
    ]
    assert panel.final_body == "**最终**"
    assert "最终" in panel.rendered_markdown()
    # 工具段只累积调用行（add_tool_result 为 no-op，不显示结果）
    tool_lines = [seg["line"] for seg in panel._segments if seg["type"] == "tool"]
    assert len(tool_lines) == 1
    assert "akm_read_file" in tool_lines[0]


def test_live_panel_renders_markdown_in_body():
    """rendered_markdown 优先使用 final 正文，其次累积增量。"""
    from akm.agent_cli.render import LiveStreamPanel

    panel = LiveStreamPanel(enable=False, color=False)
    panel.add_body("**你好**")
    assert "你好" in panel.rendered_markdown()
    panel.finish("正文2")
    assert "正文2" in panel.rendered_markdown()


def test_live_panel_rendered_final_keeps_reasoning_and_tools():
    """rendered_final 按顺序保留思考 / 正文 / 工具完整内容。"""
    from akm.agent_cli.render import LiveStreamPanel

    panel = LiveStreamPanel(enable=False, color=False)
    with panel:
        panel.add_reasoning("思考A")
        panel.add_body("正文B")
        panel.add_tool("akm_read_file", {"path": "x"})
    panel.finish("最终C")
    final = panel.rendered_final()
    # 思考标题与内容、工具调用行、最终正文都在顺序输出中
    assert "思考" in final
    assert "思考A" in final
    assert "akm_read_file" in final
    assert "最终C" in final


def test_live_panel_streams_full_content_without_clip():
    """Live 模式不再裁剪到当屏：长内容完整保留，由终端自然滚动。"""
    from akm.agent_cli.render import LiveStreamPanel

    panel = LiveStreamPanel(enable=True, color=False)

    class _FakeLive:
        def update(self, *_): pass
        def start(self): pass
        def stop(self): pass

    panel._live = _FakeLive()
    panel._term_lines = 30
    panel._term_columns = 100
    # 超长正文应完整出现在渲染结果中（不因终端高度被截掉）
    panel._flush_buffers()
    panel._segments.append(
        {
            "type": "text",
            "content": "".join(f"第{i}行内容 一些填充文本\n" for i in range(200)),
        }
    )
    out = panel._render()
    s = out.plain if hasattr(out, "plain") else str(out)
    # 首尾内容都在：不裁剪
    assert "第0行" in s
    assert "第199行" in s
    # 行数应明显超过当屏高度
    assert len(s.splitlines()) > 30


def test_live_panel_color_render_parses_ansi_not_literal():
    """color=True 时 _render 必须用 Text.from_ansi，不能把 ESC 当字面量（否则花屏）。"""
    from akm.agent_cli.render import LiveStreamPanel

    class _FakeLive:
        def update(self, *_): pass
        def start(self): pass
        def stop(self): pass

    panel = LiveStreamPanel(enable=True, color=True)
    panel._live = _FakeLive()
    panel._term_lines = 24
    panel._term_columns = 80
    # 含代码 fence 的正文会触发 rich 语法高亮 ANSI
    panel.add_body("```jsx\nexport const App = () => <div>hi</div>\n```\n")
    out = panel._render()
    plain = out.plain if hasattr(out, "plain") else str(out)
    # plain 不应残留 ESC 转义序列（from_ansi 已解析）
    assert "\x1b" not in plain
    # 可见内容应包含代码关键字，而非裸 ANSI 数字碎片
    assert "export" in plain
    assert "App" in plain


def test_live_panel_long_unclosed_fence_streams_full():
    """长未闭合代码 fence 流式时，完整内容向下滚动显示，不裁成碎片。"""
    from akm.agent_cli.render import LiveStreamPanel

    class _FakeLive:
        def update(self, *_): pass
        def start(self): pass
        def stop(self): pass

    panel = LiveStreamPanel(enable=True, color=False)
    panel._live = _FakeLive()
    panel._term_lines = 20
    panel._term_columns = 80
    # 模拟 agent 写大量 React 代码、fence 尚未闭合
    body = "```jsx\n"
    for i in range(80):
        body += f"export const Item{i} = () => {{\n  return <div>{i}</div>\n}}\n"
    panel.add_body(body)
    out = panel._render()
    plain = out.plain if hasattr(out, "plain") else str(out)
    # 首尾组件都在：完整流式，不裁剪
    assert "Item0" in plain
    assert "Item79" in plain
    # 应能看到完整 export 语句
    code_lines = [ln for ln in plain.splitlines() if "export const" in ln]
    assert code_lines, "应能看到完整 export 语句"
    assert any("Item" in ln for ln in code_lines)


def test_live_panel_exit_cancels_deferred_redraw():
    """Live 退出时必须 cancel 节流 deferred task，且退出后不再重绘。"""
    from akm.agent_cli.render import LiveStreamPanel

    class _CountingLive:
        def __init__(self):
            self.updates = 0
            self.stopped = False

        def update(self, *_):
            self.updates += 1

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    async def _run():
        panel = LiveStreamPanel(enable=True, color=False, refresh_per_second=12)
        # 缩短节流窗口，便于测试
        panel._throttle_secs = 0.05
        fake = _CountingLive()
        with panel:
            panel._live = fake
            # 第一次立即重绘
            panel.add_body("a")
            # 节流窗口内再追加 → 创建 deferred task
            panel.add_body("b")
            assert len(panel._deferred_tasks) >= 1
            # 立刻退出：应 cancel deferred，不再追加 update
            updates_before_exit = fake.updates
        assert fake.stopped is True
        assert panel._live is None
        assert len(panel._deferred_tasks) == 0
        # 给 deferred 一点时间（若未 cancel 会 sleep 后 update）
        await asyncio.sleep(0.08)
        assert fake.updates == updates_before_exit

    asyncio.run(_run())


def test_repl_run_stream_with_enable_live_passes_events(tmp_path, monkeypatch):
    """enable_live=True 时事件仍进入三区面板（思考逐字 + 工具 + 正文）。"""
    from akm.agent_cli.render import LiveStreamPanel

    store = SessionStore(tmp_path)
    session = build_session(store, name="t")
    final_msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]
    events = [
        ("reasoning_delta", {"turn": 1, "content": "思考"}),
        ("model_delta", {"turn": 1, "content": "正"}),
        ("model_delta", {"turn": 1, "content": "文"}),
        ("tool_call", {"name": "akm_read_file", "arguments": {"path": "x"}}),
        ("tool_result", {"name": "akm_read_file", "result": "文件内容"}),
        ("final", {"final_message": {"content": "你好"}, "messages": final_msgs}),
    ]
    client = _fake_client(events, final_messages=final_msgs)

    captured: dict[str, LiveStreamPanel] = {}
    real_factory = LiveStreamPanel

    class _Spy(LiveStreamPanel):
        """捕获面板实例，enable_live 通过 Repl 传入。"""

        def __init__(self, *args, **kwargs):
            kwargs.pop("enable", None)
            super().__init__(*args, **kwargs, enable=False)
            captured["panel"] = self

    # monkeypatch 渲染模块内的 LiveStreamPanel，让 Repl 用它
    import akm.agent_cli.repl as repl_mod

    monkeypatch.setattr(repl_mod, "LiveStreamPanel", _Spy)
    repl = Repl(store, client, session, input_fn=lambda _: "hi", color=False, enable_live=True)
    repl._ask = lambda: "hi"

    async def _run():
        await repl._run_stream_round("hi", enable_live=True)

    asyncio.run(_run())

    panel = captured["panel"]
    # 段序列按事件顺序：思考 → 正文 → 工具（结果被丢弃，仅调用行）
    assert [seg["type"] for seg in panel._segments] == [
        "thinking", "text", "tool",
    ]
    # 思考逐字累积
    thinking = "".join(
        str(seg.get("content") or "")
        for seg in panel._segments
        if seg["type"] == "thinking"
    )
    assert thinking == "思考"
    # 正文增量逐字累积（final 后 text 段被最终正文覆盖）
    assert panel.body_text == "你好"
    # 工具段只累积调用行（add_tool_result 为 no-op，不显示结果）
    tool_lines = [seg["line"] for seg in panel._segments if seg["type"] == "tool"]
    assert len(tool_lines) == 1
    assert "akm_read_file" in tool_lines[0]
    # 会话消息按 final 写回并落盘
    assert repl.session["messages"] == final_msgs
    loaded = store.load("t")
    assert loaded["messages"] == final_msgs


# ── 输入层（多行输入 / tab 补全 / 非 TTY 回退） ──


class _Doc:
    """模拟 prompt_toolkit Document：只暴露 text_before_cursor。"""

    def __init__(self, text: str):
        self.text_before_cursor = text


def test_completer_slash_commands():
    """tab 补全斜杠命令名（/mo → /model）。"""
    from akm.agent_cli.input import AgentCompleter

    c = AgentCompleter()
    comps = list(c.get_completions(_Doc("/mo"), None))
    assert ("/model", -3) in comps
    # 不匹配的不出现
    assert all(t.startswith("/mo") for t, _ in comps)


def test_completer_fuzzy_matches_commands():
    """斜杠命令支持 fzf 风格模糊匹配（/mde → /model，/x 无命中）。"""
    from akm.agent_cli.input import AgentCompleter

    c = AgentCompleter()
    comps = list(c.get_completions(_Doc("/mde"), None))
    assert ("/model", -4) in comps
    # 模糊不命中的命令不出现
    assert all(_is_subsequence("/mde", t) for t, _ in comps)
    # 完全无关的输入无候选
    assert list(c.get_completions(_Doc("/zzz"), None)) == []


def test_completer_typing_slash_lists_all():
    """仅输入 / 时列出全部命令（selector 弹窗）。"""
    from akm.agent_cli.input import AgentCompleter, SLASH_COMMANDS

    c = AgentCompleter()
    comps = list(c.get_completions(_Doc("/"), None))
    assert {t for t, _ in comps} == set(SLASH_COMMANDS)


def test_completer_space_then_slash_lists_all():
    """空格接 /（输入 / 前有空格）同样弹出全部命令。"""
    from akm.agent_cli.input import AgentCompleter, SLASH_COMMANDS

    c = AgentCompleter()
    comps = list(c.get_completions(_Doc(" /"), None))
    assert {t for t, _ in comps} == set(SLASH_COMMANDS)


def test_completer_mid_text_slash():
    """文本中间输入空格+/ 也能唤起命令菜单（/mde → /model）。"""
    from akm.agent_cli.input import AgentCompleter

    c = AgentCompleter()
    comps = list(c.get_completions(_Doc("帮我翻译这句话 /mde"), None))
    assert ("/model", -4) in comps


def test_completer_mid_text_not_slash_no_candidates():
    """普通句子不弹菜单（无斜杠候选）。"""
    from akm.agent_cli.input import AgentCompleter

    c = AgentCompleter()
    assert list(c.get_completions(_Doc("帮我翻译这句话"), None)) == []


def test_adapter_adds_display_meta():
    """命令补全项附带说明（display_meta），selector 菜单右侧展示。"""
    from akm.agent_cli.input import _build_prompt_session, SLASH_COMMAND_META
    from akm.agent_cli.sessions import SessionStore

    session = _build_prompt_session(SessionStore(), [])
    assert session.completer is not None
    docs = list(session.completer.get_completions(_Doc("/clear"), None))
    assert docs and all(
        d.display_meta_text == SLASH_COMMAND_META.get(d.text) for d in docs
    )


def _is_subsequence(pattern: str, word: str) -> bool:
    """测试辅助：子序列匹配（与 input._fuzzy_match 同语义）。"""
    it = iter(word)
    return all(ch in it for ch in pattern)


def test_completer_resume_session_names(tmp_path):
    """/resume 补全会话名。"""
    from akm.agent_cli.input import AgentCompleter

    store = SessionStore(tmp_path)
    store.save({"name": "s1", "messages": []})
    store.save({"name": "s2", "messages": []})
    c = AgentCompleter(store=store)
    comps = list(c.get_completions(_Doc("/resume s1"), None))
    assert any(t == "s1" for t, _ in comps)


def test_completer_workspace_paths(tmp_path):
    """/workspace 补全已有目录。"""
    from akm.agent_cli.input import AgentCompleter

    sub = tmp_path / "mywork"
    sub.mkdir()
    prefix = str(tmp_path / "my")
    c = AgentCompleter()
    comps = list(c.get_completions(_Doc(f"/workspace {prefix}"), None))
    assert any(t.startswith(str(sub)) for t, _ in comps)


def test_completer_ignores_multiline():
    """多行输入不触发补全（避免对长代码内容误补全）。"""
    from akm.agent_cli.input import AgentCompleter

    c = AgentCompleter()
    assert list(c.get_completions(_Doc("/mo\n第二行"), None)) == []


def test_create_agent_input_force_plain_is_builtin_input():
    """force_plain=True 时强制回退到系统 input()。"""
    from akm.agent_cli.input import create_agent_input

    fn = create_agent_input(force_plain=True)
    assert fn is input


def test_create_agent_input_non_tty_falls_back(monkeypatch):
    """stdin 非 TTY 时回退到系统 input()，不启动 prompt_toolkit 会话。"""
    from akm.agent_cli.input import create_agent_input

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    fn = create_agent_input()
    assert fn is input


def test_repl_multiline_starts_with_slash_not_command(tmp_path):
    """多行输入首行以 / 开头也作为单条 user 消息发送，不误判为斜杠命令。"""
    store = SessionStore(tmp_path)
    session = build_session(store, name="t")

    class _C:
        def __init__(self):
            self.stream_args = None

        async def stream(self, messages, **kwargs):
            self.stream_args = messages
            yield "final", {"final_message": {"content": "ok"}, "messages": messages}

        async def aclose(self):
            return None

    client = _C()
    inputs = iter(["/usr/bin/foo\n第二行", "/quit"])
    repl = Repl(store, client, session, input_fn=lambda _: next(inputs), color=False)

    async def _run():
        await repl.run_async()

    asyncio.run(_run())
    # 多行内容整体作为一条 user 消息发送，未走命令分支
    assert client.stream_args is not None
    assert client.stream_args[-1] == {"role": "user", "content": "/usr/bin/foo\n第二行"}


def test_startup_banner_contains_cat_and_session_info():
    """启动横幅：圆角框内左边字符画小猫、右边会话初始信息。"""
    from akm.agent_cli.render import startup_banner

    s = startup_banner(
        version="0.1.23",
        name="t1",
        model="m1",
        workspace="/w",
        color=False,
    )
    assert "( o.o )" in s  # 小猫字符画
    assert "AKM Agent（v0.1.23）" in s
    assert "会话: 「t1」" in s
    assert "模型: m1" in s
    assert "工作区: /w" in s
    # 圆角框
    assert "╭" in s and "╰" in s
    # 无颜色时不带 ANSI 码
    assert "\x1b" not in s


def test_startup_banner_defaults():
    """无 name / model / workspace 时显示占位，不抛错。"""
    from akm.agent_cli.render import startup_banner

    s = startup_banner(version="0.1.23")
    assert "会话: （新会话）" in s
    assert "模型: (未设置)" in s or "模型: （未设置）" in s


# ── 菜单栏版本比较 ──


def test_version_greater():
    """菜单栏更新提示的版本大小判断：仅线上版本大于本地版本才提示更新。"""
    from akm.menubar import _version_greater

    # 线上更新 → 提示
    assert _version_greater("0.1.23", "0.1.22") is True
    # 本地更新 / 相同 → 不提示（本场景：本地 0.1.22 > 线上 0.1.21）
    assert _version_greater("0.1.21", "0.1.22") is False
    assert _version_greater("0.1.22", "0.1.22") is False
    # 跨段进位
    assert _version_greater("0.2.0", "0.1.99") is True
    # pre-release 语义：正式版 > 预发布版
    assert _version_greater("0.1.22-beta", "0.1.22") is False
    assert _version_greater("0.1.22", "0.1.22-beta") is True
    # 忽略前导 v
    assert _version_greater("v0.1.23", "0.1.22") is True
