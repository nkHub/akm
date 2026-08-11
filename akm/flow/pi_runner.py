"""Pi 编码 agent 执行器（移植自 flow 项目的 pi-runner.ts）。

优先调用本机 ``pi`` CLI（与交互式 pi 使用同一认证/模型配置），失败时
按启动类错误回退到 mock 摘要。默认超时 600s（FLOW_PI_TIMEOUT_MS 可覆盖）。
"""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
from typing import Any

DEFAULT_TIMEOUT_MS = int(os.environ.get("FLOW_PI_TIMEOUT_MS") or os.environ.get("FLOW_AGENT_TIMEOUT_MS") or 600_000)


def build_prompt(system_prompt: str, user_prompt: str) -> str:
    """组装发送给 pi 的提示词（含 System 块与收尾要求）。"""
    parts = [
        f"## System\n{system_prompt}" if system_prompt else "",
        user_prompt or "",
        "",
        "完成后用简洁中文总结你做了什么、改了哪些文件、如何验证。",
    ]
    return "\n\n".join(p for p in parts if p)


def resolve_cwd(raw: str | None) -> str:
    """解析工作目录。"""
    if not raw or raw == ".":
        return os.getcwd()
    return os.path.abspath(os.path.expanduser(raw))


def _resolve_pi_binary() -> str | None:
    """定位 pi CLI 绝对路径。

    每台机器 pi 的安装位置不同，因此优先读取配置显式指定的路径
    （config.json 的 ``agent_flow.pi_path``，支持 ``~`` 展开）；未配置时再
    依次 ``shutil.which`` 走 PATH、扫描常见安装目录兜底。找不到返回 None，
    交由 ``_is_start_failure`` 判定后回退 mock。
    """
    try:
        from akm.config import load_config
        configured = str(((load_config().get("agent_flow") or {}).get("pi_path") or "")).strip()
        if configured:
            expanded = os.path.abspath(os.path.expanduser(configured))
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                return expanded
    except Exception:  # noqa: BLE001
        pass
    found = shutil.which("pi")
    if found:
        return found
    candidates = (
        "/usr/local/bin/pi",
        "/opt/homebrew/bin/pi",
        "/opt/local/bin/pi",
        os.path.expanduser("~/.local/bin/pi"),
        "/usr/bin/pi",
    )
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# pi-coding-agent 要求的 Node 最低版本（cli 脚本用了 v flag 正则等新语法）
_PI_MIN_NODE = (22, 19, 0)


def _node_version_ok(node_bin: str, min_ver: tuple = _PI_MIN_NODE) -> bool:
    """node 版本是否 >= min_ver（跑一次 node --version 解析）。"""
    try:
        out = subprocess.run([node_bin, "--version"], capture_output=True, text=True, timeout=10).stdout or ""
        m = re.search(r"v(\d+)\.(\d+)\.(\d+)", out)
        if not m:
            return False
        return tuple(int(x) for x in m.groups()) >= min_ver
    except Exception:
        return False


def _nvm_node_candidates() -> list[str]:
    """~/.nvm/versions/node/*/bin/node，按版本降序（仅收录 >= _PI_MIN_NODE）。"""
    root = os.path.join(os.path.expanduser("~"), ".nvm", "versions", "node")
    if not os.path.isdir(root):
        return []
    dirs: list[tuple[tuple, str]] = []
    for name in os.listdir(root):
        m = re.match(r"v(\d+)\.(\d+)\.(\d+)", name)
        if not m:
            continue
        ver = tuple(int(x) for x in m.groups())
        if ver < _PI_MIN_NODE:
            continue
        bin_path = os.path.join(root, name, "bin", "node")
        if os.path.isfile(bin_path) and os.access(bin_path, os.X_OK):
            dirs.append((ver, bin_path))
    dirs.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in dirs]


def _resolve_node_binary() -> str | None:
    """定位满足 pi 要求的 node 绝对路径（版本 >= 22.19.0）。

    pi 是 node 脚本，shebang 走 ``#!/usr/bin/env node``；GUI/launchctl 启动的
    打包 app 环境 PATH 常为空或不含 /usr/local/bin，且用户默认 node 可能是
    旧版本（v18 跑不了新版 pi）。这里显式解析：PATH 里的 node（版本满足）→
    nvm 各版本（>=22.19.0 取最高）→ 常见安装目录。都不满足返回 None。
    """
    found = shutil.which("node")
    if found and _node_version_ok(found):
        return found
    for cand in _nvm_node_candidates():
        return cand
    for candidate in (
        "/usr/local/bin/node",
        "/opt/homebrew/bin/node",
        "/opt/local/bin/node",
        os.path.expanduser("~/.local/bin/node"),
        "/usr/bin/node",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK) and _node_version_ok(candidate):
            return candidate
    return None


def _pi_exec_command(pi_bin: str, node_bin: str | None) -> list[str]:
    """构造 pi 的执行命令。

    pi 常为 node 脚本（symlink → cli.js）；node 版本满足时用 ``node cli.js``
    直接执行，绕过 shebang 的 env node（打包 app 缺 PATH 时 env 找不到 node）。
    """
    cli_js = os.path.realpath(pi_bin)
    if node_bin and cli_js.endswith(".js") and os.path.isfile(cli_js):
        return [node_bin, cli_js]
    return [pi_bin]


def _load_pi_models_file() -> dict | None:
    """读 ~/.pi/agent/models.json（provider → models 映射），用于把网关模型
    id 映射为 pi 的 provider/model 形态。"""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".pi", "agent", "models.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def resolve_pi_model_ref(model: dict | None) -> dict:
    """把 Flow/gateway 模型 id → pi --model 模式（provider/id）。

    返回 {ref: str|None, reason}。裸网关 id 不传给 pi（会让 pi 猜错 provider），
    未映射时省略 --model 用 pi 默认。"""
    forced = os.environ.get("FLOW_PI_MODEL", "").strip()
    if forced:
        return {"ref": forced, "reason": "FLOW_PI_MODEL"}
    wanted = (model.get("model") or model.get("id") or "") if model else ""
    wanted = re.sub(r"^gateway:", "", wanted).strip()
    if not wanted:
        return {"ref": None, "reason": "pi-default (no model on node)"}
    if "/" in wanted:
        return {"ref": wanted, "reason": "explicit provider/model"}
    file = _load_pi_models_file()
    providers = (file or {}).get("providers") or {}
    for provider, cfg in (providers or {}).items():
        ids = [m.get("id") for m in (cfg.get("models") or []) if m.get("id")]
        if wanted in ids:
            return {"ref": f"{provider}/{wanted}", "reason": f"models.json · {provider}"}
    # 启发式：deepseek → ds、grok → grok、gpt- → oai
    if re.search(r"deepseek", wanted, re.IGNORECASE) and "ds" in providers:
        return {"ref": f"ds/{wanted}", "reason": "heuristic ds/*"}
    if re.search(r"grok", wanted, re.IGNORECASE) and "grok" in providers:
        return {"ref": f"grok/{wanted}", "reason": "heuristic grok/*"}
    if re.match(r"^gpt-", wanted, re.IGNORECASE) and "oai" in providers:
        return {"ref": f"oai/{wanted}", "reason": "heuristic oai/*"}
    return {
        "ref": None,
        "reason": f'unmapped gateway model "{wanted}" → 使用 pi 默认模型（可设 FLOW_PI_MODEL 或改用 provider/id）',
    }


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


def _is_start_failure(err: Exception) -> bool:
    """启动类错误（找不到命令/认证/模型配置）才允许回退 mock。"""
    msg = str(err)
    return bool(
        re.search(r"ENOENT|not found|No such file|command not found|Cannot find module|EACCES|spawn ", msg, re.IGNORECASE)
        or re.search(r"API key|auth|unauthorized|login|models?\.json|unknown model|invalid model", msg, re.IGNORECASE)
    )


def _is_timeout(err: Exception) -> bool:
    """是否超时。"""
    return "timed out after" in str(err)


async def _run_pi_cli(opts: dict, timeout_ms: int, model_ref: str | None) -> dict:
    """通过 pi CLI 执行编码（stdout 为结果文本）。"""
    cwd = resolve_cwd(opts.get("cwd"))
    message = build_prompt(opts.get("systemPrompt") or "", opts.get("userPrompt") or "")
    args = ["-p", "--no-session", "--mode", "text", "--offline"]
    if model_ref:
        args += ["--model", model_ref]
    args += ["--approve", message]
    opts.get("on_log") and opts["on_log"](
        f"Pi CLI: pi {' '.join(a for a in args if a != message)} … (timeout {round(timeout_ms / 1000)}s)", "debug"
    )
    env = {
        **os.environ,
        "CI": os.environ.get("CI", "1"),
        "NO_COLOR": os.environ.get("NO_COLOR", "1"),
    }
    pi_bin = _resolve_pi_binary()
    if not pi_bin:
        raise FileNotFoundError(
            "pi 命令不存在（PATH 与常见安装目录均未找到）：请安装 pi-coding-agent 或把其所在目录加入 PATH"
        )
    # pi 是 node 脚本：用满足版本要求的 node 绝对路径直接执行真实 cli.js，
    # 绕过 shebang 的 env node（打包 app 缺 PATH 时 env 找不到 node）；缺 node
    # 时报明确错误让节点 failed，而不是回退 mock 造成「跑成功了但没有文件」。
    cli_js = os.path.realpath(pi_bin)
    node_bin = _resolve_node_binary()
    if cli_js.endswith(".js") and os.path.isfile(cli_js) and not node_bin:
        raise FileNotFoundError(
            "pi 需要 Node.js ≥22.19.0，但未找到满足版本的 node（PATH 与常见安装目录均不含）："
            "请用 nvm 安装新版 Node.js（如 nvm install 22）或在 PATH 中提供新版 node"
        )
    exec_args = _pi_exec_command(pi_bin, node_bin)
    opts.get("on_log") and opts["on_log"](f"Pi 启动: {' '.join(exec_args)}（node={node_bin}）", "debug")
    proc = await asyncio.create_subprocess_exec(
        *exec_args,
        *args,
        cwd=cwd,
        env=env,
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
                on_token(text)
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
            f"Pi CLI 超时，正在终止进程树 pid={proc.pid}（timeout {round(timeout_ms / 1000)}s）", "warn"
        )
        _kill_process_tree(proc)
        await proc.wait()
        settled = True
        partial = "".join(stdout_chunks).strip()
        if len(partial) > 80:
            return {
                "text": partial + f"\n\n---\n[Flow] Pi CLI timed out after {timeout_ms}ms；以上为超时前的部分输出。",
                "tokensIn": max(len(message) // 4, 1),
                "tokensOut": max(len(partial) // 4, 1),
                "mode": "cli",
            }
        raise TimeoutError(f"Pi CLI timed out after {timeout_ms}ms")
    finally:
        if not settled:
            for t in reader_tasks:
                t.cancel()
    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    code = proc.returncode or 0
    err_hint = stderr.strip() or f"pi exited with code {code}"
    if code != 0 and not stdout.strip():
        raise RuntimeError(err_hint)
    if stderr.strip() and code != 0:
        opts.get("on_log") and opts["on_log"](stderr[:800], "warn")
    return {
        "text": stdout.strip() or stderr.strip() or "(pi cli empty)",
        "tokensIn": max(len(message) // 4, 1),
        "tokensOut": max(len(stdout) // 4, 1),
        "mode": "cli",
    }


async def _run_pi_mock(opts: dict) -> dict:
    """CLI 启动失败时返回模拟开发摘要。"""
    chunks = [
        "# Pi Agent（mock）\n\n",
        f"cwd: {resolve_cwd(opts.get('cwd'))}\n",
        f"model: {(opts.get('model') or {}).get('name') or (opts.get('model') or {}).get('model') or 'default'}\n\n",
        f"## 任务\n{(opts.get('userPrompt') or '')[:400]}\n\n",
        "## 结果\nSDK/CLI 不可用，返回模拟开发摘要。\n",
        "- 已解析上游产物\n",
        "- 跳过真实写文件\n",
        "- 提示: 配置 ~/.pi/agent/models.json 中的 provider/model，或设 FLOW_PI_MODEL=ds/deepseek-v4-pro\n",
    ]
    text = "".join(chunks)
    on_token = opts.get("on_token")
    if on_token:
        for c in chunks:
            on_token(c)
            await asyncio.sleep(0.02)
    return {
        "text": text,
        "tokensIn": max(len(opts.get("userPrompt") or "") // 4, 1),
        "tokensOut": max(len(text) // 4, 1),
        "mode": "mock",
    }


async def run_pi_agent(opts: dict) -> dict:
    """执行编码节点（默认 CLI → mock 回退）。

    opts: cwd / systemPrompt / userPrompt / model / timeoutMs / on_log / on_token。
    返回 {text, tokensIn, tokensOut, mode}。超时后不回退 mock（避免双开改同一仓库）。
    """
    timeout_ms = opts.get("timeoutMs") or DEFAULT_TIMEOUT_MS
    resolved = resolve_pi_model_ref(opts.get("model"))
    ref = resolved["ref"]
    opts.get("on_log") and opts["on_log"](
        f"Pi 模型解析: {ref or '(default)'} · {resolved['reason']} · timeout={timeout_ms}ms", "info"
    )
    try:
        result = await _run_pi_cli(opts, timeout_ms, ref)
        opts.get("on_log") and opts["on_log"](f"Pi 完成（cli） model={ref or 'default'}", "info")
        return result
    except Exception as err:  # noqa: BLE001
        msg = str(err)
        opts.get("on_log") and opts["on_log"](f"Pi CLI 失败: {msg}", "warn")
        if _is_timeout(err):
            opts.get("on_log") and opts["on_log"](
                "CLI 超时后不回退 mock（避免重复改同一仓库）。可提高 FLOW_PI_TIMEOUT_MS 后重试。", "warn"
            )
            raise
        if not _is_start_failure(err):
            opts.get("on_log") and opts["on_log"]("非启动类错误，跳过 mock 回退", "warn")
            raise
    opts.get("on_log") and opts["on_log"]("回退到 Pi mock 执行器", "warn")
    return await _run_pi_mock(opts)
