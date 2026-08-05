"""交互循环：`akm agent` 的 REPL 主逻辑。

负责：
- 通过本地代理服务 ``/v1/agent`` 发起 Agent 请求（流式 SSE / 一次性 JSON）
- 维护当前会话的 messages，每轮结束后写回会话文件（服务端无状态）
- 处理内建斜杠命令（/model /workspace /sessions /resume /clear /quit 等）
- 渲染 SSE 事件到终端

请求与渲染解耦：``AgentClient`` 只负责 HTTP，``render`` 只负责输出。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, AsyncGenerator, Callable

# 显式导入 readline，为系统 input() 启用行编辑（左右键移动光标 / 删除 / 历史）。
# 非交互式进程（console script、python -m）默认不会自动导入 readline，
# 若缺失则 input() 退化为无编辑读取，左右键无法移动光标。
try:  # 非 Unix 平台（如 Windows）没有 readline 模块，静默跳过
    import readline  # noqa: F401
except ImportError:  # pragma: no cover
    pass

import httpx

from akm.agent_cli.render import (
    LiveStreamPanel,
    dim,
    error,
    event_text,
    header,
    help_text,
    ok,
    reason_delta_text,
    render_markdown,
    warn,
)
from akm.agent_cli.sessions import SessionStore
from akm.agent_cli.sse import SSEConsumer


class AgentClient:
    """本地代理服务 /v1/agent 的客户端封装。

    只负责组装请求与解析响应；工作区/会话等状态由调用方（REPL）维护。
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 600.0,
        token: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        """关闭底层 httpx client。"""
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        """构造请求头；配置了 agent_api_token 时带上鉴权。"""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _body(
        self,
        messages: list[dict],
        *,
        model: str,
        instructions: str,
        api_path: str,
        workspace_root: str,
        stream: bool,
        tools: list[dict] | None = None,
    ) -> dict[str, Any]:
        """组装 /v1/agent 请求体。"""
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "api_path": api_path,
            "instructions": instructions,
            "workspace_root": workspace_root,
        }
        if tools is not None:
            body["tools"] = tools
        return body

    async def stream(
        self,
        messages: list[dict],
        *,
        model: str,
        instructions: str,
        api_path: str,
        workspace_root: str,
        tools: list[dict] | None = None,
    ) -> AsyncGenerator[tuple[str, dict], None]:
        """流式调用 /v1/agent，产出 (event, data) 二元组。"""
        body = self._body(
            messages,
            model=model,
            instructions=instructions,
            api_path=api_path,
            workspace_root=workspace_root,
            stream=True,
            tools=tools,
        )
        async with self._client.stream(
            "POST",
            f"{self.base_url}/v1/agent",
            json=body,
            headers=self._headers(),
        ) as response:
            if response.status_code >= 400:
                text = (await response.aread()).decode("utf-8", errors="replace")
                yield "error", {"error": f"HTTP {response.status_code}: {text}"}
                return
            async for event, data in SSEConsumer().consume(response):
                yield event, data

    async def run(
        self,
        messages: list[dict],
        *,
        model: str,
        instructions: str,
        api_path: str,
        workspace_root: str,
        tools: list[dict] | None = None,
    ) -> dict[str, Any]:
        """一次性（非流式）调用 /v1/agent，返回完整 JSON 结果。"""
        body = self._body(
            messages,
            model=model,
            instructions=instructions,
            api_path=api_path,
            workspace_root=workspace_root,
            stream=False,
            tools=tools,
        )
        response = await self._client.post(
            f"{self.base_url}/v1/agent",
            json=body,
            headers=self._headers(),
        )
        try:
            data = response.json()
        except json.JSONDecodeError:
            data = {"ok": False, "error": f"HTTP {response.status_code}: 非 JSON 响应"}
        if response.status_code >= 400 and isinstance(data, dict):
            data.setdefault("ok", False)
            data.setdefault("error", f"HTTP {response.status_code}")
        return data if isinstance(data, dict) else {"ok": False, "error": str(data)}


class Repl:
    """Agent 交互式会话。

    Args:
        store: 会话持久化层。
        client: 服务端客户端。
        session: 初始会话 dict（含 name / messages / model / workspace_root 等）。
        print_fn: 输出函数（默认 print，便于测试注入）。
        input_fn: 输入函数（默认 input，便于测试注入）。
        color: 是否启用 ANSI 颜色。
    """

    def __init__(
        self,
        store: SessionStore,
        client: AgentClient,
        session: dict[str, Any],
        *,
        print_fn: Callable[[str], None] = print,
        input_fn: Callable[[str], str] = input,
        color: bool = True,
        show_reasoning: bool = False,
        enable_live: bool = False,
    ):
        self.store = store
        self.client = client
        self.session = session
        self.print_fn = print_fn
        self.input_fn = input_fn
        self.color = color
        # 是否展示模型的思考过程（reasoning_delta）。
        # 默认折叠：只显示「思考中…」占位，避免 token 流刷屏；
        # 与 opencode 等工具一致，需详细思考时用 --show-reasoning 开启。
        self.show_reasoning = show_reasoning
        # 流式输出是否用 rich Live 三区实时渲染（思考 / 工具 / 正文逐字）。
        # 仅交互终端开启；测试 / 无终端场景保持逐行打印。
        self.enable_live = enable_live

    # ── 渲染辅助 ──

    def _p(self, text: str = "") -> None:
        """打印一行输出。"""
        self.print_fn(text)

    def _header(self, text: str) -> None:
        self._p(header(text, self.color))

    def _ok(self, text: str) -> None:
        self._p(ok(text, self.color))

    def _warn(self, text: str) -> None:
        self._p(warn(text, self.color))

    def _error(self, text: str) -> None:
        self._p(error(text, self.color))

    def _dim(self, text: str) -> None:
        self._p(dim(text, self.color))

    # ── 会话状态辅助 ──

    def _messages(self) -> list[dict]:
        """当前会话消息。"""
        return self.session.setdefault("messages", [])

    def _model(self) -> str:
        return str(self.session.get("model") or "")

    def _workspace(self) -> str:
        return str(self.session.get("workspace_root") or "")

    def _save(self) -> None:
        """写回会话文件。"""
        try:
            self.store.save(self.session)
        except (OSError, ValueError) as exc:
            self._warn(f"会话保存失败: {exc}")

    # ── 斜杠命令 ──

    def _handle_command(self, line: str) -> bool | None:
        """处理斜杠命令，返回 False 表示退出，True 表示继续，None 表示继续且不发送。

        返回语义：
        - False — 退出 REPL
        - True  — 继续下一轮（命令已执行）
        - None  — 命令失败，继续下一轮但提示
        """
        return execute_slash_command(
            self.store,
            self.session,
            line,
            self._emit,
        )

    def _emit(self, kind: str, text: str) -> None:
        """按输出类型分发到对应渲染通道（供 execute_slash_command 回调）。"""
        if kind == "header":
            self._header(text)
        elif kind == "ok":
            self._ok(text)
        elif kind == "warn":
            self._warn(text)
        elif kind == "error":
            self._error(text)
        elif kind == "dim":
            self._dim(text)
        else:
            self._p(text)

    # ── 单轮请求 ──

    def _ask(self) -> str | None:
        """读取一行用户输入。返回 None 表示 EOF（Ctrl+D）。"""
        prompt = "akm> "
        try:
            line = self.input_fn(prompt)
        except EOFError:
            return None
        return line.strip()

    async def _run_stream_round(self, user_text: str, *, enable_live: bool = False) -> None:
        """流式执行一轮 Agent 请求并把结果写回会话。

        Args:
            user_text: 用户输入文本。
            enable_live: 是否用 rich Live 三区实时渲染（思考 / 工具 / 正文逐字）。
                关闭时沿用逐行打印（测试 / 无终端场景）。
        """
        messages = self._messages()
        messages.append({"role": "user", "content": user_text})

        final_message: dict | None = None
        failed = False
        # Live 面板：思考区（灰字）/ 工具区（青色短行）/ 正文区（markdown 逐字渲染）。
        # 仅 enable_live 时真正刷新屏幕；否则纯累积供下方统一渲染。
        panel = LiveStreamPanel(
            enable=enable_live,
            color=self.color,
            show_reasoning=self.show_reasoning,
        )
        try:
            with panel:
                async for event, data in self.client.stream(
                    messages,
                    model=self._model(),
                    instructions=str(self.session.get("instructions") or ""),
                    api_path=str(self.session.get("api_path") or "chat/completions"),
                    workspace_root=self._workspace(),
                ):
                    if event == "reasoning_delta":
                        content = str(data.get("content") or "")
                        if content:
                            # 思考区逐字追加；不开启时仅累积（面板折叠）
                            panel.add_reasoning(content)
                            if self.show_reasoning:
                                self._p(reason_delta_text(content, self.color))
                    elif event == "model_delta":
                        content = str(data.get("content") or "")
                        if content:
                            # 正文区逐字追加，markdown 由面板实时重渲染
                            panel.add_body(content)
                    elif event == "tool_call":
                        panel.add_tool(
                            str(data.get("name") or ""),
                            data.get("arguments"),
                        )
                    elif event == "tool_result":
                        panel.add_tool_result(
                            str(data.get("name") or ""),
                            str(data.get("result") or ""),
                        )
                    elif event == "tool_retry":
                        self._p(event_text("tool_retry", data, self.color))
                    elif event == "context_warning":
                        self._p(event_text("context_warning", data, self.color))
                    elif event == "final":
                        final_message = data.get("final_message") or {}
                        new_messages = data.get("messages")
                        if isinstance(new_messages, list) and new_messages:
                            self.session["messages"] = new_messages
                    elif event == "error":
                        self._error(f"✗ {data.get('error')}")
                        failed = True
                # 结束时把最终正文交给面板（Live 退出后用于统一渲染）
                content = str((final_message or {}).get("content") or "")
                panel.finish(content)
        except (httpx.HTTPError, asyncio.CancelledError) as exc:
            self._error(f"请求失败: {exc}")
            failed = True

        if failed or final_message is None:
            # 本轮未成功完成：把刚追加的 user 消息回滚，避免污染会话
            messages = self._messages()
            if messages and messages[-1].get("role") == "user":
                messages.pop()
            # 流被中断（既无 final 也无 error，例如上游连接断开）时给出提示
            if not failed:
                self._warn("响应中断：服务端未返回完成事件，本轮未计入会话。")
        elif not enable_live:
            # 非 Live 模式（测试 / 无终端）：Live 从未真正上屏，
            # 这里统一打印完整三区静态文本（思考 / 工具 / 正文）。
            content = panel.rendered_final()
            if content:
                self._p(f"\n{content}")
        # Live 模式：transient=False 的最后一帧已把三区留在屏幕，无需重复打印
        self._save()

    async def _run_once_round(self, user_text: str) -> None:
        """非流式执行一轮 Agent 请求。"""
        messages = self._messages()
        messages.append({"role": "user", "content": user_text})

        self._dim("— Agent 处理中…")
        result = await self.client.run(
            messages,
            model=self._model(),
            instructions=str(self.session.get("instructions") or ""),
            api_path=str(self.session.get("api_path") or "chat/completions"),
            workspace_root=self._workspace(),
        )
        if result.get("ok"):
            new_messages = result.get("messages")
            if isinstance(new_messages, list) and new_messages:
                self.session["messages"] = new_messages
            final_message = result.get("final_message") or {}
            content = str(final_message.get("content") or "")
            if content:
                self._p(f"\n{render_markdown(content, self.color)}")
            usage = result.get("usage") or {}
            if usage:
                self._dim(f"（turns: {result.get('turns')}, tokens: {usage.get('total_tokens')}）")
        else:
            self._error(f"✗ {result.get('error')}")
            messages = self._messages()
            if messages and messages[-1].get("role") == "user":
                messages.pop()
        self._save()

    async def run_async(self, stream: bool = True) -> None:
        """主循环：读取输入 → 命令或发送。"""
        self._header(f"AKM Agent 会话「{self.session.get('name')}」")
        self._dim(f"工作区: {self._workspace() or os.getcwd()}")
        if self._messages():
            self._dim(f"已载入 {len(self._messages())} 条历史消息，输入 /help 查看命令")

        while True:
            line = self._ask()
            if line is None:  # EOF (Ctrl+D)
                self._p()
                self._dim("再见！")
                return
            if not line:
                continue
            if line.startswith("/"):
                action = self._handle_command(line)
                if action is False:
                    return
                continue
            if line.lower() in ("exit", "quit"):
                self._dim("再见！")
                return

            if stream:
                await self._run_stream_round(line, enable_live=self.enable_live)
            else:
                await self._run_once_round(line)


def execute_slash_command(
    store: SessionStore,
    session: dict[str, Any],
    line: str,
    emit: Callable[[str, str], None],
) -> bool | None:
    """处理斜杠命令（滚动 REPL 共用）。

    Args:
        store: 会话持久化层。
        session: 当前会话 dict（会被命令就地修改）。
        line: 用户输入的一行（以 / 开头）。
        emit: 输出回调 ``emit(kind, text)``，kind 取值
              ``header / ok / warn / error / dim / plain``。

    返回语义（与 Repl._handle_command 一致）：
    - False — 退出 REPL
    - True  — 继续下一轮（命令已执行）
    - None  — 命令失败，继续下一轮但提示
    """
    cmd, _, arg = line.partition(" ")
    cmd = cmd.lower()
    arg = arg.strip()

    if cmd in ("/quit", "/exit", "/q"):
        return False

    if cmd == "/help":
        emit("plain", help_text())
        return True

    if cmd == "/model":
        if arg:
            session["model"] = arg
            emit("ok", f"模型已切换: {arg}")
        else:
            emit("dim", f"当前模型: {str(session.get('model') or '') or '(默认)'}")
        _save_session(store, session, emit)
        return True

    if cmd == "/workspace":
        if arg:
            resolved = os.path.abspath(os.path.expanduser(arg))
            if not os.path.isdir(resolved):
                emit("error", f"目录不存在: {resolved}")
                return None
            session["workspace_root"] = resolved
            emit("ok", f"工作区已切换: {resolved}")
        else:
            emit(
                "dim",
                f"当前工作区: {str(session.get('workspace_root') or '') or '(未设置，默认当前目录)'}",
            )
        _save_session(store, session, emit)
        return True

    if cmd == "/instructions":
        session["instructions"] = arg
        emit("ok", "会话指令已更新" if arg else "会话指令已清空")
        _save_session(store, session, emit)
        return True

    if cmd == "/clear":
        session["messages"] = []
        _save_session(store, session, emit)
        emit("ok", "已清空当前会话消息")
        return True

    if cmd == "/sessions":
        sessions = store.list()
        if not sessions:
            emit("dim", "暂无历史会话")
            return True
        emit("header", "历史会话:")
        for item in sessions:
            emit(
                "plain",
                f"  {item['name']}  ({item['message_count']} 条消息, "
                f"更新于 {item['updated_at']})",
            )
        return True

    if cmd == "/resume":
        if not arg:
            emit("error", "用法: /resume <会话名>")
            return None
        loaded = store.load(arg)
        if loaded is None:
            emit("error", f"会话不存在: {arg}")
            return None
        session.clear()
        session.update(loaded)
        emit(
            "ok",
            f"已恢复会话: {arg}（{len(session.setdefault('messages', []))} 条消息）",
        )
        return True

    # 未识别命令
    emit("error", f"未知命令: {line}")
    emit("plain", "输入 /help 查看可用命令")
    return None


def _save_session(store: SessionStore, session: dict[str, Any], emit: Callable[[str, str], None]) -> None:
    """写回会话文件，失败时通过 emit 输出警告（滚动共用）。"""
    try:
        store.save(session)
    except (OSError, ValueError) as exc:
        emit("warn", f"会话保存失败: {exc}")


async def run_agent_repl(
    store: SessionStore,
    client: AgentClient,
    session: dict[str, Any],
    *,
    stream: bool = True,
    print_fn: Callable[[str], None] = print,
    input_fn: Callable[[str], str] = input,
    color: bool = True,
    show_reasoning: bool = False,
    enable_live: bool = False,
) -> None:
    """运行交互式 REPL 的异步入口。"""
    repl = Repl(
        store,
        client,
        session,
        print_fn=print_fn,
        input_fn=input_fn,
        color=color,
        show_reasoning=show_reasoning,
        enable_live=enable_live,
    )
    await repl.run_async(stream=stream)
    await client.aclose()


def build_session(
    store: SessionStore,
    *,
    name: str = "",
    resume: str = "",
    model: str = "",
    workspace_root: str = "",
    api_path: str = "chat/completions",
    instructions: str = "",
) -> dict[str, Any]:
    """构建一个会话 dict。

    优先 resume 已存在会话；否则新建（name 缺省自动生成）。
    """
    if resume:
        loaded = store.load(resume)
        if loaded is None:
            raise ValueError(f"会话不存在: {resume}")
        return loaded
    return {
        "name": name or store.next_name(),
        "created_at": "",
        "updated_at": "",
        "model": model,
        "workspace_root": workspace_root,
        "api_path": api_path,
        "instructions": instructions,
        "messages": [],
    }
