"""终端输出渲染：把 Agent SSE 事件转成可读的终端文本。

渲染策略：
- ``render_markdown`` 使用 rich.markdown 渲染最终正文，支持粗体 / 列表 / 代码语法高亮，
  是复刻 Claude Code / opencode 显示效果的核心；
- 工具调用、状态提示等短行使用轻量 ANSI 裸码（不依赖任何第三方库），颜色开关由调用方控制；
- 所有函数返回**字符串**（含 ANSI 码），是否真正上色由 ``color`` 开关决定，
  这样可以把输出交给任何 print 通道（终端 / 测试注入的 print_fn）。

提供两层 API：
- ``event_text(event, data)`` — 单事件 → 完整文本（含换行）
- ``reason_delta_text / model_delta_text`` — 增量正文（由调用方实时打印）
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from io import StringIO
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

# 轻量 ANSI 颜色码（与 rich 默认配色一致）。
# 注意：rich 渲染 markdown 时自带完整颜色，这里只用于工具行/提示行。
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_RED = "\x1b[31m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_CYAN = "\x1b[36m"
_GRAY = "\x1b[90m"
_RESET = "\x1b[0m"


def _color(text: str, code: str, enabled: bool) -> str:
    """按开关给文本加颜色；关闭时原样返回。"""
    if not enabled or not text:
        return text
    return f"{code}{text}{_RESET}"


def _truncate(text: str, limit: int = 400) -> str:
    """截断工具输出过长部分，避免刷屏。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...（已截断，共 {len(text)} 字符）"


def _terminal_lines() -> int:
    """返回终端行数（失败时回退 24 行）。"""
    try:
        return max(10, shutil.get_terminal_size().lines)
    except Exception:
        return 24


def _terminal_columns() -> int:
    """返回终端列数（失败时回退 100 列）。"""
    try:
        return max(40, shutil.get_terminal_size().columns)
    except Exception:
        return 100


def render_markdown(content: str, color: bool = True) -> str:
    """用 rich 渲染 markdown 正文为带 ANSI 的字符串。

    支持粗体 / 斜体 / 列表 / 标题 / 行内代码 / 代码块语法高亮。
    无颜色时（color=False）rich 仍保留段落结构与列表标记，但去掉着色，
    便于测试断言子串而不受 ANSI 码干扰。
    """
    if not content:
        return ""
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system=("truecolor" if color else None),
    )
    console.print(Markdown(content))
    text = buf.getvalue()
    # rich 会按终端宽度自动换行并在行尾填充空格，这里裁剪干净
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip("\n")


def render_message(content: str) -> Markdown:
    """返回一个 rich.markdown.Markdown 对象（供 Textual Markdown 组件直接渲染）。

    与 ``render_markdown`` 的区别：这里是对象而非字符串，交由 Textual 的
    Markdown 组件在终端宽度内实时排版渲染（支持滚动 / 宽高自适应）。
    """
    return Markdown(content or "")


def event_text(event: str, data: dict[str, Any], color: bool = True) -> str:
    """把单个 Agent SSE 事件格式化为一行或多行终端文本。"""
    if event == "tool_call":
        name = str(data.get("name") or "")
        args = data.get("arguments")
        try:
            args_text = json.dumps(args, ensure_ascii=False) if args else "{}"
        except (TypeError, ValueError):
            args_text = str(args)
        return _color(f"⚙ {name}({args_text})", _CYAN, color)

    if event == "tool_result":
        name = str(data.get("name") or "")
        result = str(data.get("result") or "")
        return _color(f"← {name} 结果: {_truncate(result)}", _DIM, color)

    if event == "tool_retry":
        error = str(data.get("error") or "")
        count = data.get("retry_count")
        max_retries = data.get("max_retries")
        return _color(
            f"⟳ 工具失败将重试（{count}/{max_retries}）: {error}",
            _YELLOW,
            color,
        )

    if event == "context_warning":
        estimated = data.get("estimated_tokens")
        max_tokens = data.get("max_tokens")
        ratio = data.get("ratio")
        return _color(
            f"⚠ 上下文占用 {estimated}/{max_tokens}（{ratio:.1%}），建议 /compact 压缩",
            _YELLOW,
            color,
        )

    if event == "turn_start":
        return _color(f"—— 第 {data.get('turn')} 轮 ——", _GRAY, color)

    if event == "final":
        final_message = data.get("final_message") or {}
        content = str(final_message.get("content") or "")
        usage = data.get("usage") or {}
        turns = data.get("turns")
        line = render_markdown(content, color) if content else ""
        if usage or turns:
            detail = f"（{turns} 轮，tokens: {usage.get('total_tokens', '?')}）"
            line = f"{line}\n{_color(detail, _GRAY, color)}"
        return line

    if event == "error":
        return _color(f"✗ {data.get('error')}", _RED, color)

    # reasoning_delta / model_delta 由调用方实时打印，这里返回空
    return ""


def reason_delta_text(content: str, color: bool = True) -> str:
    """思考增量文本（灰字）。"""
    return _color(content, _GRAY, color)


def model_delta_text(content: str, color: bool = True) -> str:
    """正文增量文本（默认不额外着色，保持流式平滑）。"""
    return content


def header(text: str, color: bool = True) -> str:
    """会话头 / 分节标题。"""
    return _color(text, _BOLD, color)


def dim(text: str, color: bool = True) -> str:
    """弱化文本（提示、元信息）。"""
    return _color(text, _GRAY, color)


def warn(text: str, color: bool = True) -> str:
    """警告文本。"""
    return _color(text, _YELLOW, color)


def error(text: str, color: bool = True) -> str:
    """错误文本。"""
    return _color(text, _RED, color)


def ok(text: str, color: bool = True) -> str:
    """成功文本。"""
    return _color(text, _GREEN, color)


def help_text() -> str:
    """内建斜杠命令帮助。"""
    return "\n".join([
        "可用命令:",
        "  /model <名称>       切换模型（留空查看当前）",
        "  /workspace <路径>   切换工作区（留空显示当前）",
        "  /instructions <文本>  临时覆盖系统指令（仅本会话）",
        "  /compact [提示]     请求压缩上下文（服务端 akm_compact_context）",
        "  /sessions           列出历史会话",
        "  /resume <名称>      从历史会话继续对话",
        "  /clear              清空当前会话消息",
        "  /quit / exit        退出",
        "  /help               显示本帮助",
        "",
        "提示: 输入普通文本即发送给 Agent；输入 'exit' 或按 Ctrl+C 退出。",
    ])


class LiveStreamPanel:
    """Agent 流式输出的顺序渲染面板（基于 rich Live，参考 chat 项目的段序列模型）。

    请求进行期间用 rich ``Live`` 维持一个常驻界面。内容**按事件到达顺序**
    组织为有序的段序列（segment），同类型段合并、不同类型段顺序叠加，
    而不是把思考 / 工具 / 正文各自攒成一整块——这样工具轮正文与最终轮正文
    各成独立段落，顺序展示，符合真实对话节奏：

    - ``thinking`` 段：模型的思考增量（灰字），逐字追加；
    - ``text`` 段：正文增量，每个 ``model_delta`` 到达即把累积文本交给
      rich ``Markdown`` 重渲染一次，视觉上逐字追加；
    - ``tool`` 段：工具调用 / 结果（青色 / 暗色短行）。

    用法::

        panel = LiveStreamPanel(color=True, show_reasoning=False)
        with panel:
            panel.add_reasoning("...")   # 思考增量（顺序追加）
            panel.add_body("...")        # 正文增量（顺序追加）
            panel.add_tool("akm_read_file", {"path": "x"})
            panel.add_tool_result("akm_read_file", "...")
            panel.finish("最终完整正文")   # 结束并清屏
            print(panel.rendered_markdown)  # 结束后拿最终渲染文本

    ``enable=False`` 时退化为纯累积（不进入 Live），便于测试与无终端场景：
    调用方仍可读 ``body_text`` / ``rendered_markdown`` / ``rendered_final``。
    """

    # 段类型常量（thinking=思考 / text=正文 / tool=工具调用）
    _SEG_THINKING = "thinking"
    _SEG_TEXT = "text"
    _SEG_TOOL = "tool"

    def __init__(
        self,
        *,
        enable: bool = True,
        color: bool = True,
        show_reasoning: bool = False,
        refresh_per_second: float = 12.0,
    ):
        self.enable = enable
        self.color = color
        self.show_reasoning = show_reasoning
        self._refresh_per_second = refresh_per_second
        # 有序段序列：每项 dict，thinking/text 段含 "content"，tool 段含 "line"
        self._segments: list[dict] = []
        # 当前缓冲的正文 / 思考增量与所属模式（未 flush 前不落段，40ms 节流语义）
        self._cur_text = ""
        self._cur_thinking = ""
        self._seg_mode: str | None = None
        self._final_body = ""
        self._live: Live | None = None
        # Live 重绘节流：高频增量（逐 token）合并到 40ms 内一次刷新，避免卡顿
        self._throttle_secs = 0.04
        self._last_live_at = 0.0
        self._pending_live = False
        # 终端高度缓存（列数 / 行数），用于 Live 裁剪到可视范围
        self._term_lines = _terminal_lines()
        self._term_columns = _terminal_columns()

    # ── 数据累积（顺序追加） ──

    def add_reasoning(self, content: str) -> None:
        """追加一段思考增量（空内容跳过），并即时刷新 Live 面板。"""
        if content:
            # 若此前在正文流，先把正文段封存，让思考另起一段（与 chat 一致）
            if self._seg_mode == self._SEG_TEXT:
                self._flush_buffers()
            self._seg_mode = self._SEG_THINKING
            self._cur_thinking += content
            self._live_update()

    def add_body(self, content: str) -> None:
        """追加一段正文增量（空内容跳过），并即时刷新 Live 面板。"""
        if content:
            # 若此前在思考流，先把思考段封存，让正文另起一段（与 chat 一致）
            if self._seg_mode == self._SEG_THINKING:
                self._flush_buffers()
            self._seg_mode = self._SEG_TEXT
            self._cur_text += content
            self._live_update()

    def add_tool(self, name: str, args: Any) -> None:
        """记录一次工具调用（调用时展示参数行），并即时刷新。"""
        # 工具调用是独立事件：先封存当前正文/思考缓冲，再追加 tool 段
        self._flush_buffers()
        self._seg_mode = None
        try:
            args_text = json.dumps(args, ensure_ascii=False) if args else "{}"
        except (TypeError, ValueError):
            args_text = str(args)
        self._segments.append({"type": self._SEG_TOOL, "line": f"⚙ {name}({args_text})"})
        self._live_update()

    def add_tool_result(self, name: str, result: str, limit: int = 200) -> None:
        """记录一次工具结果（用户要求不展示结果，故 no-op）。

        工具段只保留调用行 ``⚙ name(args)``；结果内容过长会刷屏且干扰阅读，
        因此这里直接丢弃，仅保留签名以兼容既有调用方。
        """

    def _flush_buffers(self) -> None:
        """把当前正文 / 思考缓冲并入段序列：同类型末段追加，否则新建一段。

        与 chat 项目 ``pushBuffers`` 语义一致，保证按发生顺序组织。
        """
        if self._cur_text:
            last = self._segments[-1] if self._segments else None
            if last and last.get("type") == self._SEG_TEXT:
                last["content"] = str(last.get("content") or "") + self._cur_text
            else:
                self._segments.append({"type": self._SEG_TEXT, "content": self._cur_text})
            self._cur_text = ""
        if self._cur_thinking:
            last = self._segments[-1] if self._segments else None
            if last and last.get("type") == self._SEG_THINKING:
                last["content"] = str(last.get("content") or "") + self._cur_thinking
            else:
                self._segments.append({"type": self._SEG_THINKING, "content": self._cur_thinking})
            self._cur_thinking = ""

    def finish(self, final_body: str) -> None:
        """结束流式：flush 缓冲、覆盖最后一段同类型正文 / 思考，并重绘最后一帧。

        final_body 是服务端最终轮完整正文，覆盖最后一段 text（若无则追加一段）；
        思考同理覆盖最后一段 thinking。Live 退出前保证最终内容落屏。
        """
        self._flush_buffers()
        self._final_body = str(final_body or "")
        if self._final_body:
            text_index = -1
            for i, seg in enumerate(self._segments):
                if seg.get("type") == self._SEG_TEXT:
                    text_index = i
            if text_index >= 0:
                self._segments[text_index]["content"] = self._final_body
            else:
                self._segments.append({"type": self._SEG_TEXT, "content": self._final_body})
        # 结束前强制重绘一次：把 _last_live_at 归零绕过节流窗口，清掉 pending
        self._pending_live = False
        self._last_live_at = 0.0
        self._live_update()

    # ── 只读结果 ──

    @property
    def body_text(self) -> str:
        """已累积的正文增量拼接结果（含缓冲中未 flush 的部分）。"""
        text = "".join(
            str(seg.get("content") or "")
            for seg in self._segments
            if seg.get("type") == self._SEG_TEXT
        )
        return text + self._cur_text

    @property
    def final_body(self) -> str:
        """finish() 设置的最终正文。"""
        return self._final_body

    def rendered_markdown(self) -> str:
        """把最终正文（优先 final，其次累积增量）渲染为 markdown 字符串。"""
        content = self._final_body or self.body_text
        return render_markdown(content, self.color)

    # ── Live 生命周期 ──

    def _live_update(self) -> None:
        """增量数据后触发 Live 重绘（未启用 Live 时静默）。

        高频增量（逐 token 到达）会用节流合并：40ms 内的多次 update 只重绘一次，
        避免整段 Markdown 被反复全量重渲染造成卡顿；节流结束后补一次收尾重绘。
        """
        if self._live is None:
            return
        live = self._live
        now = time.monotonic()
        self._pending_live = True
        if now - self._last_live_at >= self._throttle_secs:
            self._last_live_at = now
            self._pending_live = False
            live.update(self._render())
        else:
            # 节流窗口内：延迟到窗口结束再统一重绘
            async def _deferred() -> None:
                remaining = self._throttle_secs - (time.monotonic() - self._last_live_at)
                await asyncio.sleep(max(0.0, remaining))
                if self._pending_live:
                    self._pending_live = False
                    self._last_live_at = time.monotonic()
                    live.update(self._render())

            try:
                asyncio.get_running_loop().create_task(_deferred())
            except RuntimeError:
                # 无事件循环（同步测试环境）：立即重绘
                self._pending_live = False
                live.update(self._render())

    def __enter__(self) -> "LiveStreamPanel":
        if not self.enable:
            return self
        self._live = Live(
            console=Console(width=self._term_columns),
            refresh_per_second=self._refresh_per_second,
            screen=False,
            # 保留最后渲染帧：流式结束后内容留在屏幕上，随后由 rendered_final 落定
            transient=False,
        )
        self._live.start()
        self._live.update(self._render())
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live is not None:
            # 清掉节流 pending，避免延迟重绘任务访问已停止的 Live
            self._pending_live = False
            self._live.stop()
            self._live = None

    def _build_blocks(self) -> list[Any]:
        """组装顺序渲染的块列表（thinking / text / tool 按段序列顺序排列）。

        每段按类型独立渲染，段与段之间以空行分隔，保持事件发生顺序；
        工具行直接用轻量 ANSI 短行（青色 / 暗色），与 chat 的 tool 段一致。
        """
        # 渲染前先 flush 缓冲，保证 live 显示与最终文本一致
        self._flush_buffers()
        blocks: list[Any] = []
        for seg in self._segments:
            seg_type = seg.get("type")
            if seg_type == self._SEG_THINKING:
                content = str(seg.get("content") or "")
                # 思考区：正常色整段（不再置灰，便于阅读）
                blocks.append(
                    Panel(
                        Text(content),
                        title="思考",
                        title_align="left",
                    )
                )
            elif seg_type == self._SEG_TEXT:
                content = str(seg.get("content") or "")
                # 正文段：实时渲染 markdown（逐字追加效果）
                blocks.append(Panel(Markdown(content), title="正文", title_align="left"))
            elif seg_type == self._SEG_TOOL:
                # 工具段：青色 / 暗色短行
                line = str(seg.get("line") or "")
                blocks.append(Text(line, style="cyan" if "⚙" in line else "dim"))
        return blocks

    def _render(self):
        """拼装当前顺序界面（裁剪到终端可视高度，自动跟随最新内容）。"""
        blocks = self._build_blocks()
        if not blocks:
            return Text("…")
        # 段与段之间用空行分隔，保持顺序可读
        content = Group(*blocks)
        if not self.enable or self._live is None:
            return content
        # 裁剪到终端高度：rich Live 不会自动滚动，超出屏幕的旧内容丢弃，
        # 只保留底部可视行，让新增内容始终出现在屏幕内（等同“自动滚动到底部”）。
        buf = StringIO()
        Console(file=buf, width=self._term_columns, force_terminal=True,
                color_system="truecolor" if self.color else None).print(content)
        lines = [line.rstrip() for line in buf.getvalue().splitlines()]
        max_lines = max(5, self._term_lines - 3)
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return Text("\n".join(lines), no_wrap=True)

    def rendered_final(self) -> str:
        """流式结束后返回完整顺序渲染文本（思考 / 正文 / 工具按序），供落定打印。

        与 rendered_markdown 不同：思考段 / 工具段会一并按顺序保留，
        而非只输出最终正文。非 Live 模式（测试 / 管道）下用这份文本打印。
        """
        blocks = self._build_blocks()
        if not blocks:
            return ""
        buf = StringIO()
        console = Console(
            file=buf,
            force_terminal=True,
            color_system="truecolor" if self.color else None,
        )
        console.print(Group(*blocks))
        lines = [line.rstrip() for line in buf.getvalue().splitlines()]
        return "\n".join(lines).strip("\n")


__all__ = [
    "LiveStreamPanel",
    "event_text",
    "error",
    "header",
    "help_text",
    "dim",
    "model_delta_text",
    "ok",
    "reason_delta_text",
    "render_message",
    "render_markdown",
    "warn",
]
