"""Codex CLI 编码 agent 执行器（类比 pi_runner.py，执行器名 codex-cli）。

调用本机 ``codex exec`` 非交互执行编码任务，结果取 ``-o`` 输出的最后消息。
默认复用 codex 自身配置（本机 ``~/.codex/config.toml`` 可直接把 base_url 指向
本机 AKM 代理，让 Key 管理、限流、审计复用 AKM 链路）；也可通过
``agent_flow.codex_use_akm_proxy: true`` 注入 ``OPENAI_BASE_URL`` /
``OPENAI_API_KEY`` 强制走 AKM 代理。

与 pi 不同：codex 启动失败或超时一律直接让节点 failed，不做 mock 兜底
（避免「跑成功了但实际没有产出文件」的假成功）。默认超时 1 小时
（FLOW_CODEX_TIMEOUT_MS / agent_flow.codex_timeout_ms 可覆盖）。
"""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
from typing import Any


def _codex_timeout_ms() -> int:
    """解析 codex 编码节点超时（毫秒）。

    优先级：环境变量 FLOW_CODEX_TIMEOUT_MS > config.json 的
    ``agent_flow.codex_timeout_ms`` > 默认 1 小时（复杂任务可能需要长时间
    编码/测试）。便于各机器自行调大/调小而不改代码。
    """
    env = os.environ.get("FLOW_CODEX_TIMEOUT_MS")
    if env:
        try:
            return max(1000, int(env))
        except ValueError:
            pass
    try:
        from akm.config import load_config
        configured = (load_config().get("agent_flow") or {}).get("codex_timeout_ms")
        if configured:
            return max(1000, int(configured))
    except (ValueError, TypeError):
        pass
    return 3_600_000


def _resolve_codex_binary() -> str | None:
    """定位 codex CLI 绝对路径。

    优先读取配置显式指定的路径（config.json 的 ``agent_flow.codex_path``，支持
    ``~`` 展开）；未配置时依次 ``shutil.which`` 走 PATH、扫描常见安装目录兜底。
    找不到返回 None，由调用方报明确错误。
    """
    try:
        from akm.config import load_config
        configured = str(((load_config().get("agent_flow") or {}).get("codex_path") or "")).strip()
        if configured:
            expanded = os.path.abspath(os.path.expanduser(configured))
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                return expanded
    except Exception:  # noqa: BLE001
        pass
    found = shutil.which("codex")
    if found:
        return found
    candidates = (
        "/usr/local/bin/codex",
        "/opt/homebrew/bin/codex",
        "/opt/local/bin/codex",
        os.path.expanduser("~/.local/bin/codex"),
        "/usr/bin/codex",
    )
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def build_prompt(system_prompt: str, user_prompt: str) -> str:
    """组装发送给 codex 的提示词（System 块 + 用户任务 + 收尾要求）。"""
    parts = [
        f"## System\n{system_prompt}" if system_prompt else "",
        user_prompt or "",
        "",
        "完成后用简洁中文总结你做了什么、改了哪些文件、如何验证。",
    ]
    return "\n\n".join(p for p in parts if p)


def resolve_codex_model_ref(model: dict | None) -> str | None:
    """把 Flow/gateway 模型 id → codex ``-m`` 参数。

    配置/环境变量强制指定模型：config.json 的 ``agent_flow.codex_model`` 优先，
    环境变量 FLOW_CODEX_MODEL 兜底（与 pi 的 FLOW_PI_MODEL 同构）；均未配置时
    用节点模型 id（剥离 ``gateway:`` 前缀）。返回 None 时不传 ``-m``，
    由 codex 使用自身配置的默认模型。
    """
    from akm.config import load_config
    configured_model = (load_config().get("agent_flow") or {}).get("codex_model") or ""
    forced = os.environ.get("FLOW_CODEX_MODEL", "").strip() or str(configured_model).strip()
    if forced:
        return forced
    wanted = (model.get("model") or model.get("id") or "") if model else ""
    wanted = re.sub(r"^gateway:", "", wanted).strip()
    return wanted or None


def _akm_proxy_env() -> dict:
    """若显式启用 ``agent_flow.codex_use_akm_proxy: true``，注入指向本机 AKM
    代理的环境变量：OPENAI_BASE_URL 指向 AKM 的 /v1，OPENAI_API_KEY 填占位即可
    （AKM 按 model 选 Key，转发时用所选 Key 替换 Authorization）。

    默认不注入（信任用户 codex 自身配置，如本机 ``~/.codex/config.toml`` 已把
    base_url 指向 AKM）；codex_proxy_base_url / codex_api_key 可显式覆盖默认值。
    关闭/未配置代理时返回空，codex 使用本地登录/自己的配置。
    """
    from akm.config import load_config
    cfg = load_config()
    flow_cfg = cfg.get("agent_flow") or {}
    if flow_cfg.get("codex_use_akm_proxy") is not True:
        return {}
    port = cfg.get("server_port", 8800) or 8800
    base = str(flow_cfg.get("codex_proxy_base_url") or "").strip() or f"http://127.0.0.1:{port}/v1"
    key = str(flow_cfg.get("codex_api_key") or "").strip() or "akm-local"
    return {"OPENAI_BASE_URL": base, "OPENAI_API_KEY": key}


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
    """从 codex ``--json`` 事件行里提取可展示文本，用于实时 token 推送。

    兼容 message_delta（增量）与 message_created/item_created（整条消息）；
    解析失败返回空串（该行可能是会话/工具事件，不展示）。
    """
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(event, dict):
        return ""
    etype = event.get("type")
    if etype == "message_delta":
        delta = event.get("delta") or {}
        return str(delta.get("text") or "")
    if etype in ("message_created", "item_created"):
        item = event.get("item") or {}
        text = item.get("text") if isinstance(item, dict) else None
        if isinstance(text, str):
            return text
        # 有些版本 item.content 为文本块数组（[{type:"output_text",text:...}]）
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, list):
            return "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and str(part.get("type") or "").endswith("text")
            )
    return ""


async def _run_codex_cli(opts: dict, timeout_ms: int, model_ref: str | None) -> dict:
    """通过 codex CLI 非交互执行编码（``-o`` 输出最后消息，``--json`` 事件流）。"""
    cwd = str(opts.get("cwd") or os.getcwd())
    message = build_prompt(opts.get("systemPrompt") or "", opts.get("userPrompt") or "")
    last_msg_file = opts.get("lastMsgFile") or ""
    args = ["exec", "-s", "workspace-write", "--skip-git-repo-check", "-C", cwd, "--json"]
    if model_ref:
        args += ["-m", model_ref]
    if last_msg_file:
        args += ["-o", last_msg_file]
    args.append(message)
    opts.get("on_log") and opts["on_log"](
        f"Codex CLI: codex {' '.join(a for a in args if a != message)} … (timeout {round(timeout_ms / 1000)}s)", "debug"
    )
    codex_bin = _resolve_codex_binary()
    if not codex_bin:
        raise FileNotFoundError(
            "codex 命令不存在（PATH 与常见安装目录均未找到）：请安装 Codex CLI 或把其所在目录加入 PATH"
        )
    env = {**os.environ, **_akm_proxy_env(), "CI": "1", "NO_COLOR": "1"}
    opts.get("on_log") and opts["on_log"](f"Codex 启动: {codex_bin}（env={sorted(_akm_proxy_env().keys()) or 'none'}）", "debug")
    proc = await asyncio.create_subprocess_exec(
        codex_bin,
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
            f"Codex CLI 超时，正在终止进程树 pid={proc.pid}（timeout {round(timeout_ms / 1000)}s）", "warn"
        )
        _kill_process_tree(proc)
        await proc.wait()
        settled = True
        partial = _read_last_msg(last_msg_file) or ""
        if len(partial) > 80:
            return {
                "text": partial + f"\n\n---\n[Flow] Codex CLI timed out after {timeout_ms}ms；以上为超时前的最后消息。",
                "tokensIn": max(len(message) // 4, 1),
                "tokensOut": max(len(partial) // 4, 1),
                "mode": "cli",
            }
        raise TimeoutError(f"Codex CLI timed out after {timeout_ms}ms")
    finally:
        if not settled:
            for t in reader_tasks:
                t.cancel()
    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    code = proc.returncode or 0
    text = _read_last_msg(last_msg_file)
    if not text:
        # -o 未产出时（如较早失败）回退到 stdout JSONL 里提取的最后消息
        text = _extract_last_message(stdout)
    if not text and stderr.strip():
        text = stderr.strip()
    if code != 0 and not text:
        raise RuntimeError(stderr.strip() or f"codex exited with code {code}")
    if stderr.strip() and code != 0:
        opts.get("on_log") and opts["on_log"](stderr[:800], "warn")
    return {
        "text": text.strip() or "(codex cli empty)",
        "tokensIn": max(len(message) // 4, 1),
        "tokensOut": max(len(text) // 4, 1),
        "mode": "cli",
    }


def _read_last_msg(path: str) -> str:
    """读取 codex ``-o`` 写入的最后消息文件（可能尚不存在或为空）。"""
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read() or ""
    except (OSError, UnicodeDecodeError):
        return ""


def _extract_last_message(stdout: str) -> str:
    """从 ``--json`` 的 stdout JSONL 里兜底提取最后一条 assistant 文本。"""
    texts: list[str] = []
    for line in stdout.splitlines():
        piece = _event_text_from_line(line)
        if piece:
            texts.append(piece)
    return "\n".join(texts)


async def run_codex_agent(opts: dict) -> dict:
    """执行编码节点（codex-cli 执行器）。

    opts: cwd / systemPrompt / userPrompt / model / timeoutMs / on_log / on_token。
    返回 {text, tokensIn, tokensOut, mode}。启动失败或超时直接抛错让节点
    failed，不做 mock 回退（避免「跑成功了但实际没有产出文件」的假成功）。
    """
    timeout_ms = opts.get("timeoutMs") or _codex_timeout_ms()
    model_ref = resolve_codex_model_ref(opts.get("model"))
    # 结果文件放系统临时目录，运行结束由引擎/OS 清理
    import tempfile

    fd, last_msg_file = tempfile.mkstemp(prefix="akm-codex-lastmsg-", suffix=".txt")
    os.close(fd)
    opts.get("on_log") and opts["on_log"](
        f"Codex 模型: {model_ref or '(codex 默认)'} · timeout={timeout_ms}ms", "info"
    )
    try:
        result = await _run_codex_cli(
            {**opts, "lastMsgFile": last_msg_file},
            timeout_ms,
            model_ref,
        )
        opts.get("on_log") and opts["on_log"](f"Codex 完成（cli） model={model_ref or 'default'}", "info")
        return result
    except Exception as err:  # noqa: BLE001
        opts.get("on_log") and opts["on_log"](f"Codex CLI 失败: {err}", "warn")
        raise
    finally:
        try:
            os.remove(last_msg_file)
        except OSError:
            pass
