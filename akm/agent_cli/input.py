"""agent REPL 输入层：prompt_toolkit 多行智能输入。

对比系统 ``input()``（单行、粘贴多行会逐行立即发送、无补全），本模块提供：

- **多行输入**：``Enter`` 提交，``Alt+Enter`` / ``Ctrl+J`` 插入换行；
- **粘贴安全**：粘贴的多行文本整体进入缓冲区，按 ``Enter`` 一次性提交，
  不会逐行立即发送（走 bracketed paste）；
- **斜杠 selector**：输入 ``/`` 即自动弹出命令菜单（方向键选择、``Enter`` 确定），
  命令名支持 fzf 风格模糊匹配（如 ``/mde`` → ``/model``），右侧显示命令说明；
  ``/resume`` 补全会话名、``/workspace`` 补全路径；
- **非 TTY 回退**：stdin 不是终端（管道 / 重定向 / 测试注入）时自动回退到
  系统 ``input()``，保持既有逐行行为。

prompt_toolkit 为可选依赖：未安装时同样回退 ``input()``。
"""

from __future__ import annotations

import glob
import os
import sys
from typing import Any, Callable

from akm.agent_cli.sessions import SessionStore

# 内建斜杠命令（与 repl.execute_slash_command 实际支持的命令保持一致）
SLASH_COMMANDS = [
    "/clear",
    "/exit",
    "/help",
    "/instructions",
    "/model",
    "/q",
    "/quit",
    "/resume",
    "/sessions",
    "/workspace",
]

# 斜杠命令右侧说明（补全菜单里展示，帮助用户理解每个命令用途）
SLASH_COMMAND_META = {
    "/clear": "清空当前会话消息",
    "/exit": "退出会话（同 /quit）",
    "/help": "查看全部命令",
    "/instructions": "覆盖系统指令",
    "/model": "切换模型",
    "/q": "退出会话（同 /quit）",
    "/quit": "退出会话",
    "/resume": "从历史会话继续",
    "/sessions": "列出历史会话",
    "/workspace": "切换工作区",
}


def _prompt_toolkit_available() -> bool:
    """prompt_toolkit 是否可用（可选依赖，缺失时静默回退 input）。"""
    try:
        import prompt_toolkit  # noqa: F401
        return True
    except ImportError:
        return False


def _fuzzy_match(pattern: str, word: str) -> bool:
    """fzf 风格子序列模糊匹配：pattern 的字符按顺序出现在 word 中即命中。

    例如 ``/mde`` 命中 ``/model``（/→m→d→e 依次出现）；空 pattern 视为命中全部。
    """
    it = iter(word)
    return all(ch in it for ch in pattern)


def _history_path() -> str | None:
    """输入历史文件路径；配置目录不可写时返回 None（改用内存历史）。"""
    try:
        from akm import config as config_module

        path = os.path.join(config_module.CONFIG_DIR, "agent_history.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path
    except Exception:
        return None


def _complete_dir_paths(prefix: str) -> list[str]:
    """基于用户输入补全本地目录路径（用于 /workspace）。"""
    expanded = os.path.expanduser(prefix)
    if not expanded:
        return []
    matches = glob.glob(expanded + "*")
    result: list[str] = []
    for m in sorted(matches):
        result.append(m + os.sep if os.path.isdir(m) else m)
    return result


class AgentCompleter:
    """斜杠命令 / 会话名 / 工作区路径的 tab 补全候选生成器。

    实现 ``get_completions(document, complete_event)`` 契约，产出 ``(文本, 起始偏移)``
    二元组（而非 prompt_toolkit 的 Completion 对象），因此**测试无需安装**
    prompt_toolkit 即可覆盖补全逻辑；真实输入时由 ``_build_prompt_session``
    适配成 prompt_toolkit Completion。
    """

    def __init__(
        self,
        store: SessionStore | None = None,
        models: list[str] | None = None,
    ):
        self.store = store
        self.models = models or []

    def get_completions(self, document: Any, complete_event: Any):
        """按光标前文本产出补全候选（生成器，产出 (文本, 起始偏移) 二元组）。

        触发规则：以「光标前最后一个空白词」判断，因此``空格接 /``（如 ``hello /``
        或 `` /``）同样能唤起命令菜单；``/`` 之前可以有任意文本，``/resume`` /
        ``/workspace`` 等带参数命令继续补全参数。
        """
        text = document.text_before_cursor
        # 只处理当前行（多行输入时最后一行才可能触发补全）
        line = text.rsplit("\n", 1)[-1]
        head, _, word = line.rpartition(" ")
        # 参数补全优先（如 /workspace /var/... 中路径以 / 开头，不能被当成命令）
        if head.endswith("/resume"):
            # 会话名补全：/resume s → 已有会话名
            for name in self._session_names():
                if name.startswith(word):
                    yield name, -len(word)
        elif head.endswith("/workspace"):
            # 工作区路径补全：/workspace ~/Des → 匹配目录
            for path in _complete_dir_paths(word):
                yield path, -len(word)
        elif head.endswith("/model") and self.models:
            # 模型名补全：/model gpt → 候选模型
            for name in self.models:
                if name.startswith(word):
                    yield name, -len(word)
        elif word.startswith("/"):
            # 命令 selector：当前词以 / 开头即弹出全部命令，继续敲字做模糊过滤
            for name in SLASH_COMMANDS:
                if _fuzzy_match(word, name):
                    yield name, -len(word)

    def _session_names(self) -> list[str]:
        """读取历史会话名（失败时静默返回空）。"""
        if self.store is None:
            return []
        try:
            return [str(item["name"]) for item in self.store.list()]
        except Exception:
            return []


def _build_prompt_session(
    store: SessionStore | None,
    models: list[str],
):
    """构建支持多行 + 斜杠 selector 的 PromptSession（延迟导入 prompt_toolkit）。

    - ``Enter`` 提交当前输入；补全菜单打开时先接受选中补全再提交；
    - ``Alt+Enter`` / ``Ctrl+J`` 插入换行，用于书写多行问题；
    - 粘贴含换行文本时整体进入缓冲区，Enter 一次性提交（不会逐行发送）；
    - ``complete_while_typing``：输入 ``/`` 即自动弹出命令菜单（selector 手感），
      命令名模糊过滤，菜单右侧显示命令说明。
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completion as PTCompletion
    from prompt_toolkit.filters import has_completions
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()
    completer = AgentCompleter(store=store, models=models)

    @kb.add("enter", filter=~has_completions)
    def _accept(event) -> None:
        """Enter 提交当前输入（含多行粘贴内容，一次性发送）。"""
        event.current_buffer.validate_and_handle()

    @kb.add("enter", filter=has_completions)
    def _accept_completion(event) -> None:
        """补全菜单打开时，Enter 先接受选中的补全项再提交。"""
        buffer = event.current_buffer
        if buffer.complete_state is not None:
            completion = buffer.complete_state.current_completion
            buffer.apply_completion(completion)
            buffer.complete_state = None
        buffer.validate_and_handle()

    @kb.add("escape", "enter")
    @kb.add("c-j")
    def _newline(event) -> None:
        """Alt+Enter / Ctrl+J 插入换行，书写多行问题。"""
        event.current_buffer.insert_text("\n")

    class _Adapter:
        """把 AgentCompleter 的 (文本, 偏移) 二元组适配为 prompt_toolkit Completion。

        命令名候选额外附带说明（SLASH_COMMAND_META）显示在菜单右侧；
        会话名 / 路径候选无说明，保持简洁。
        """

        def get_completions(self, document, complete_event):
            for text, start in completer.get_completions(document, complete_event):
                yield PTCompletion(
                    text,
                    start_position=start,
                    display_meta=SLASH_COMMAND_META.get(text),
                )

    path = _history_path()
    history = FileHistory(path) if path else InMemoryHistory()
    return PromptSession(
        history=history,
        completer=_Adapter(),
        key_bindings=kb,
        multiline=True,
        # 输入 / 即触发补全（命令 selector）；非 / 开头的普通输入无候选，不会误弹
        complete_while_typing=True,
        enable_history_search=True,
    )


def create_agent_input(
    store: SessionStore | None = None,
    *,
    models: list[str] | None = None,
    force_plain: bool = False,
) -> Callable[[str], str]:
    """构建 REPL 输入函数，签名 ``(prompt: str) -> str``，与系统 ``input()`` 一致。

    Args:
        store: 会话仓库，用于 ``/resume`` 会话名补全。
        models: 候选模型名列表（预留，用于 ``/model`` 补全）。
        force_plain: 强制回退到系统 ``input()``（测试 / 明确关闭多行输入）。

    仅在 stdin 是终端且 prompt_toolkit 可用时启用多行智能输入，
    否则回退系统 ``input()``（管道 / 重定向 / 测试注入场景）。
    """
    if not force_plain and _prompt_toolkit_available() and sys.stdin.isatty():
        session = _build_prompt_session(store, models or [])
        return lambda prompt: str(session.prompt(prompt))
    return input
