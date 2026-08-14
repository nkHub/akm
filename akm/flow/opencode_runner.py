"""Opencode CLI 编码 agent 执行器（类比 pi_runner/codex_runner，执行器名 opencode-cli）。

调用本机 ``opencode run`` 非交互执行编码任务，stdout 取 ``--format json`` 的
JSONL 事件流：``type=text`` 事件累积最终回答文本，``type=step_finish`` 事件
累计 token 用量。opencode 使用自身配置/登录（不注入 AKM 代理 env）。

与 codex 不同：opencode 无 ``-o`` 输出文件，结果完全依赖 stdout 解析；无
sandbox 参数，由 opencode 自身的权限模型（config/permissions）控制。
启动失败或超时一律直接让节点 failed，不做 mock 兜底。默认超时 1 小时
（FLOW_OPENCODE_TIMEOUT_MS / agent_flow.opencode_timeout_ms 可覆盖）。
"""

import asyncio
import json
import os
import re
import shutil
import signal
from typing import Any


def _opencode_timeout_ms() -> int:
    """解析 opencode 编码节点超时（毫秒）。

    优先级：环境变量 FLOW_OPENCODE_TIMEOUT_MS > config.json 的
    ``agent_flow.opencode_timeout_ms`` > 默认 1 小时（复杂任务可能需要长时间
    编码/测试）。便于各机器自行调大/调小而不改代码。
    """
    env = os.environ.get("FLOW_OPENCODE_TIMEOUT_MS")
    if env:
        try:
            return max(1000, int(env))
        except ValueError:
            pass
    try:
        from akm.config import load_config
        configured = (load_config().get("agent_flow") or {}).get("opencode_timeout_ms")
        if configured:
            return max(1000, int(configured))
    except (ValueError, TypeError):
        pass
    return 3_600_000


def _resolve_opencode_binary() -> str | None:
    """定位 opencode CLI 绝对路径。

    优先读取配置显式指定的路径（config.json 的 ``agent_flow.opencode_path``，
    支持 ``~`` 展开）；未配置时依次 ``shutil.which`` 走 PATH、扫描常见安装目录
    兜底。找不到返回 None，由调用方报明确错误。
    """
    try:
        from akm.config import load_config
        configured = str(((load_config().get("agent_flow") or {}).get("opencode_path") or "")).strip()
        if configured:
            expanded = os.path.abspath(os.path.expanduser(configured))
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                return expanded
    except Exception:  # noqa: BLE001
        pass
    found = shutil.which("opencode")
    if found:
        return found
    candidates = (
        "/usr/local/bin/opencode",
        "/opt/homebrew/bin/opencode",
        "/opt/local/bin/opencode",
        os.path.expanduser("~/.local/bin/opencode"),
        "/usr/bin/opencode",
    )
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def build_prompt(system_prompt: str, user_prompt: str) -> str:
    """组装发送给 opencode 的提示词（System 块 + 用户任务 + 收尾要求）。"""
    parts = [
        f"## System\n{system_prompt}" if system_prompt else "",
        user_prompt or "",
        "",
        "完成后用简洁中文总结你做了什么、改了哪些文件、如何验证。",
    ]
    return "\n\n".join(p for p in parts if p)


def resolve_opencode_model_ref(model: dict | None) -> str | None:
    """把 Flow/gateway 模型 id → opencode ``-m`` 参数。

    配置/环境变量强制指定模型：config.json 的 ``agent_flow.opencode_model``
    优先，环境变量 FLOW_OPENCODE_MODEL 兜底（与 pi/codex 同构）；均未配置时用
    节点模型 id（剥离 ``gateway:`` 前缀）。返回 None 时不传 ``-m``，
    由 opencode 使用自身配置的默认模型。
    """
    from akm.config import load_config
    configured_model = (load_config().get("agent_flow") or {}).get("opencode_model") or ""
    forced = os.environ.get("FLOW_OPENCODE_MODEL", "").strip() or str(configured_model).strip()
    if forced:
        return forced
    wanted = (model.get("model") or model.get("id") or "") if model else ""
    wanted = re.sub(r"^gateway:", "", wanted).strip()
    return wanted or None


def _kill_process_tree(proc, timeout_grace: float = 2.5) -> None:
    """终止进程组（start_new_session 创建），2.5s 后 SIGKILL 兜底。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    async def _hard_kill():
        await asyncio.sleep(timeout_grace)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    try:
        asyncio.get_event_loop().create_task(_hard_kill())
    except RuntimeError:
        pass


def _event_text_from_line(line: str) -> str:
    """从 opencode ``--format json`` 事件行里提取文本（``type=text`` 事件）。

    事件形如 ``{"type":"text","part":{"type":"text","text":"..."}}``；解析失败
    返回空串（该行可能是 step_start/tool 等事件，不展示）。
    """
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(event, dict) or event.get("type") != "text":
        return ""
    part = event.get("part") or {}
    if not isinstance(part, dict):
        return ""
    return str(part.get("text") or "")


def _tokens_from_line(line: str) -> tuple[int, int] | None:
    """从 opencode ``type=step_finish`` 事件提取 (input, output) token 数。

    事件形如 ``{"type":"step_finish","part":{"type":"step-finish","tokens":{...}}}``；
    非 step_finish 事件返回 None。每步一个 step_finish，由调用方累计求和。
    """
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(event, dict) or event.get("type") != "step_finish":
        return None
    part = event.get("part") or {}
    tokens = part.get("tokens") if isinstance(part, dict) else None
    if not isinstance(tokens, dict):
        return None
    try:
        return int(tokens.get("input") or 0), int(tokens.get("output") or 0)
    except (TypeError, ValueError):
        return None


async def _run_opencode_cli(opts: dict, timeout_ms: int, model_ref: str | None) -> dict:
    """通过 opencode CLI 非交互执行编码（``--format json`` 事件流取结果）。"""
    cwd = str(opts.get("cwd") or os.getcwd())
    message = build_prompt(opts.get("systemPrompt") or "", opts.get("userPrompt") or "")
    args = ["run", "--format", "json", "--dir", cwd]
    if model_ref:
        args += ["-m", model_ref]
    args.append(message)
    opts.get("on_log") and opts["on_log"](
        f"Opencode CLI: opencode {' '.join(a for a in args if a != message)} … (timeout {round(timeout_ms / 1000)}s)", "debug"
    )
    opencode_bin = _resolve_opencode_binary()
    if not opencode_bin:
        raise FileNotFoundError(
            "opencode 命令不存在（PATH 与常见安装目录均未找到）：请安装 Opencode CLI 或把其所在目录加入 PATH"
        )
    env = {**os.environ, "CI": "1", "NO_COLOR": "1"}
    opts.get("on_log") and opts["on_log"](f"Opencode 启动: {opencode_bin}", "debug")
    proc = await asyncio.create_subprocess_exec(
        opencode_bin,
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    settled = False

    async def _reader(stream, target: list[str], on_token=None, on_log=None):
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            target.append(text)
            if on_token:
                piece = _event_text_from_line(text)
                if piece:
                    on_token(piece)
            stripped = text.strip()
            if on_log and stripped:
                on_log(stripped[:500], "debug")

    reader_tasks = [
        asyncio.create_task(_reader(proc.stdout, stdout_chunks, on_token=opts.get("on_token"))),
        asyncio.create_task(_reader(proc.stderr, stderr_chunks, on_log=opts.get("on_log"))),
    ]
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError:
        opts.get("on_log") and opts["on_log"](
            f"Opencode CLI 超时，正在终止进程树 pid={proc.pid}（timeout {round(timeout_ms / 1000)}s）", "warn"
        )
        _kill_process_tree(proc)
        await proc.wait()
        settled = True
        partial = _extract_last_message("".join(stdout_chunks))
        if len(partial) > 80:
            return {
                "text": partial + f"\n\n---\n[Flow] Opencode CLI timed out after {timeout_ms}ms；以上为超时前的最后消息。",
                "tokensIn": 0,
                "tokensOut": 0,
                "mode": "cli",
            }
        raise TimeoutError(f"Opencode CLI timed out after {timeout_ms}ms")
    finally:
        if not settled:
            for t in reader_tasks:
                t.cancel()
    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    code = proc.returncode or 0
    text = _extract_last_message(stdout)
    if not text and stderr.strip():
        text = stderr.strip()
    if code != 0 and not text:
        raise RuntimeError(stderr.strip() or f"opencode exited with code {code}")
    if stderr.strip() and code != 0:
        opts.get("on_log") and opts["on_log"](stderr[:800], "warn")
    tokens_in, tokens_out = 0, 0
    for line in stdout.splitlines():
        parsed = _tokens_from_line(line)
        if parsed:
            tokens_in += parsed[0]
            tokens_out += parsed[1]
    return {
        "text": text.strip() or "(opencode cli empty)",
        "tokensIn": tokens_in or max(len(message) // 4, 1),
        "tokensOut": tokens_out or max(len(text) // 4, 1),
        "mode": "cli",
    }


def _extract_last_message(stdout: str) -> str:
    """从 ``--format json`` 的 stdout JSONL 里拼接所有 text 事件文本。"""
    texts: list[str] = []
    for line in stdout.splitlines():
        piece = _event_text_from_line(line)
        if piece:
            texts.append(piece)
    return "\n".join(texts)


async def run_opencode_agent(opts: dict) -> dict:
    """执行编码节点（opencode-cli 执行器）。

    opts: cwd / systemPrompt / userPrompt / model / timeoutMs / on_log / on_token。
    返回 {text, tokensIn, tokensOut, mode}。启动失败或超时直接抛错让节点
    failed，不做 mock 回退（避免「跑成功了但实际没有产出文件」的假成功）。
    """
    timeout_ms = opts.get("timeoutMs") or _opencode_timeout_ms()
    model_ref = resolve_opencode_model_ref(opts.get("model"))
    opts.get("on_log") and opts["on_log"](
        f"Opencode 模型: {model_ref or '(opencode 默认)'} · timeout={timeout_ms}ms", "info"
    )
    try:
        result = await _run_opencode_cli(opts, timeout_ms, model_ref)
        opts.get("on_log") and opts["on_log"](f"Opencode 完成（cli） model={model_ref or 'default'}", "info")
        return result
    except Exception as err:  # noqa: BLE001
        opts.get("on_log") and opts["on_log"](f"Opencode CLI 失败: {err}", "warn")
        raise
