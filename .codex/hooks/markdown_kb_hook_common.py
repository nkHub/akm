"""Codex / Claude Code Hook wrapper 共用工具。

这些脚本的职责只做一件事：把客户端通过 stdin 传进来的事件 JSON，
尽量稳妥地映射到我们仓库内 `akm markdown-kb-hook` 这组 CLI 参数上。

设计选择：

1. 尽量宽松读取多个候选字段名，降低不同客户端或版本之间事件字段差异的影响；
2. wrapper 自己不实现学习逻辑，避免和 `akm.markdown_kb_hook` 形成双份状态机；
3. 对外始终返回“尽量不阻塞主会话”的结果，尤其是 `Stop / PreCompact`；
4. 对 `UserPromptSubmit`，当前只能做到“识别并登记 pending，再用 additionalContext 提醒模型忽略触发词”；
   原生 Hook 目前不支持真正改写 prompt 后继续，因此这里不会伪装成已经剥离成功。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
_LAST_HOOK_RAW = ""
_LAST_HOOK_PARSE_ERROR = ""


def read_hook_payload() -> dict:
    """从 stdin 读取客户端事件 JSON。"""
    global _LAST_HOOK_PARSE_ERROR, _LAST_HOOK_RAW
    raw = sys.stdin.read()
    _LAST_HOOK_RAW = raw
    _LAST_HOOK_PARSE_ERROR = ""
    if not str(raw or "").strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _LAST_HOOK_PARSE_ERROR = str(exc)
        return {}
    return data if isinstance(data, dict) else {}


def _first_non_empty(*values: Any) -> str:
    """返回第一项非空字符串。"""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _nested(data: dict, *path: str) -> Any:
    """安全读取嵌套字段。"""
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _search_nested_scalar(data: Any, aliases: tuple[str, ...], max_depth: int = 3) -> str:
    """在有限深度内按别名递归查找第一个标量值。"""
    if max_depth < 0:
        return ""
    if isinstance(data, dict):
        for alias in aliases:
            if alias in data:
                matched = _first_non_empty(data.get(alias))
                if matched:
                    return matched
        for value in data.values():
            matched = _search_nested_scalar(value, aliases, max_depth=max_depth - 1)
            if matched:
                return matched
        return ""
    if isinstance(data, list):
        for item in data:
            matched = _search_nested_scalar(item, aliases, max_depth=max_depth - 1)
            if matched:
                return matched
    return ""


def _extract_text_from_part(value: Any) -> str:
    """从消息片段里尽量提取纯文本。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        texts = []
        for item in value:
            compact = _extract_text_from_part(item)
            if compact:
                texts.append(compact)
        return "\n".join(texts).strip()
    if not isinstance(value, dict):
        return ""
    direct = _first_non_empty(
        value.get("text"),
        value.get("input_text"),
        value.get("output_text"),
        value.get("message"),
        value.get("value"),
    )
    if direct:
        return direct
    return _first_non_empty(
        _extract_text_from_part(value.get("content")),
        _extract_text_from_part(value.get("parts")),
    )


def _message_role(item: dict) -> str:
    """统一消息角色提取。"""
    return _first_non_empty(
        item.get("role"),
        item.get("speaker"),
        item.get("author_role"),
        item.get("authorRole"),
        item.get("author"),
    ).lower()


def _message_text(item: Any) -> str:
    """统一消息正文提取。"""
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    return _first_non_empty(
        item.get("text"),
        item.get("message"),
        item.get("prompt"),
        _extract_text_from_part(item.get("content")),
        _extract_text_from_part(item.get("parts")),
    )


def _conversation_candidates(payload: dict) -> list[list]:
    """收集多个常见位置里的消息数组候选。"""
    candidates: list[Any] = [
        payload.get("messages"),
        payload.get("conversation"),
        payload.get("conversation_excerpt"),
        payload.get("conversationExcerpt"),
        payload.get("transcript"),
        payload.get("input"),
        payload.get("items"),
        payload.get("output"),
        payload.get("outputs"),
        _nested(payload, "conversation", "messages"),
        _nested(payload, "input", "messages"),
        _nested(payload, "request", "messages"),
        _nested(payload, "event", "messages"),
        _nested(payload, "data", "messages"),
    ]
    normalized: list[list] = []
    for candidate in candidates:
        if isinstance(candidate, list):
            normalized.append(candidate)
    return normalized


def _detect_message_text(payload: dict, preferred_roles: tuple[str, ...]) -> str:
    """从常见消息数组里提取最后一条匹配角色的正文。"""
    role_set = {str(item or "").strip().lower() for item in preferred_roles}
    for candidate in _conversation_candidates(payload):
        for item in reversed(candidate):
            if not isinstance(item, dict):
                continue
            role = _message_role(item)
            if role_set and role and role not in role_set:
                continue
            text = _message_text(item)
            if text:
                return text
    return ""


def detect_workspace_root(payload: dict) -> str:
    """从多个可能字段里提取工作目录。"""
    return _first_non_empty(
        payload.get("workspace_root"),
        payload.get("workspaceRoot"),
        payload.get("cwd"),
        payload.get("working_directory"),
        payload.get("workingDirectory"),
        payload.get("project_root"),
        payload.get("projectRoot"),
        payload.get("repo_root"),
        payload.get("repoRoot"),
        _nested(payload, "workspace", "root"),
        _nested(payload, "project", "root"),
        _nested(payload, "repo", "root"),
        _nested(payload, "session", "cwd"),
        _nested(payload, "environment", "cwd"),
        _search_nested_scalar(
            payload,
            (
                "workspace_root",
                "workspaceRoot",
                "cwd",
                "working_directory",
                "workingDirectory",
                "project_root",
                "projectRoot",
                "repo_root",
                "repoRoot",
            ),
        ),
    )


def detect_session_id(payload: dict) -> str:
    """从多个可能字段里提取 session id。"""
    return _first_non_empty(
        payload.get("session_id"),
        payload.get("sessionId"),
        payload.get("thread_id"),
        payload.get("threadId"),
        payload.get("conversation_id"),
        payload.get("conversationId"),
        payload.get("run_id"),
        payload.get("runId"),
        _nested(payload, "session", "id"),
        _nested(payload, "thread", "id"),
        _nested(payload, "conversation", "id"),
        _nested(payload, "run", "id"),
        _search_nested_scalar(
            payload,
            (
                "session_id",
                "sessionId",
                "thread_id",
                "threadId",
                "conversation_id",
                "conversationId",
                "run_id",
                "runId",
            ),
        ),
    )


def detect_turn_id(payload: dict) -> str:
    """从多个可能字段里提取 turn id。"""
    return _first_non_empty(
        payload.get("turn_id"),
        payload.get("turnId"),
        payload.get("response_id"),
        payload.get("responseId"),
        payload.get("message_id"),
        payload.get("messageId"),
        payload.get("request_id"),
        payload.get("requestId"),
        _nested(payload, "turn", "id"),
        _nested(payload, "response", "id"),
        _nested(payload, "message", "id"),
        _search_nested_scalar(
            payload,
            (
                "turn_id",
                "turnId",
                "response_id",
                "responseId",
                "message_id",
                "messageId",
                "request_id",
                "requestId",
            ),
        ),
    )


def detect_prompt(payload: dict) -> str:
    """从多个可能字段里提取用户 prompt。"""
    return _first_non_empty(
        payload.get("prompt"),
        payload.get("text"),
        payload.get("user_prompt"),
        payload.get("userPrompt"),
        _nested(payload, "input", "prompt"),
        _nested(payload, "input", "text"),
        _nested(payload, "user", "prompt"),
        _nested(payload, "request", "prompt"),
        _detect_message_text(payload, ("user", "human")),
    )


def detect_assistant_excerpt(payload: dict) -> str:
    """从多个可能字段里提取助手输出摘要。"""
    return _first_non_empty(
        payload.get("assistant_excerpt"),
        payload.get("assistantExcerpt"),
        payload.get("output_text"),
        payload.get("outputText"),
        payload.get("response"),
        payload.get("completion"),
        payload.get("last_assistant_message"),
        payload.get("lastAssistantMessage"),
        _nested(payload, "output", "text"),
        _nested(payload, "response", "text"),
        _detect_message_text(payload, ("assistant", "model")),
    )[:4000]


def detect_conversation_excerpt(payload: dict) -> list[dict]:
    """从多个可能字段里提取对话摘录。"""
    for candidate in _conversation_candidates(payload):
        normalized = []
        for item in candidate:
            if not isinstance(item, dict):
                continue
            role = _first_non_empty(_message_role(item), "unknown")
            text = _message_text(item)
            if text:
                normalized.append({"role": role, "text": text[:4000]})
        if normalized:
            return normalized
    return []


def run_akm_hook(args: list[str]) -> dict:
    """调用源码版 `python -m akm.markdown_kb_hook ...` 并解析返回 JSON。"""
    command = [
        str(VENV_PYTHON),
        "-m",
        "akm.markdown_kb_hook",
        *args,
    ]
    result = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = (result.stdout or "").strip()
    if stdout:
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": f"hook cli returned non-json stdout: {stdout[:500]}",
                "stderr": (result.stderr or "").strip()[:500],
                "exit_code": result.returncode,
            }
    if result.returncode != 0:
        return {
            "ok": False,
            "error": (result.stderr or f"hook cli exited with {result.returncode}").strip()[:500],
            "exit_code": result.returncode,
        }
    return {"ok": True}


def build_continue_response(additional_context: str = "") -> str:
    """构造“继续执行”的 Hook JSON 响应。"""
    payload: dict[str, Any] = {"continue": True}
    compact = str(additional_context or "").strip()
    if compact:
        payload["hookSpecificOutput"] = {
            "additionalContext": compact,
        }
    return json.dumps(payload, ensure_ascii=False)


def _debug_log_path() -> Path:
    """返回 Hook 本地调试日志路径。"""
    override = str(os.environ.get("AKM_MARKDOWN_KB_HOOK_DEBUG_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".akm" / "markdown_kb" / "hook_debug.jsonl").resolve()


def _truncate_debug_text(text: str, limit: int = 12000) -> str:
    """限制单条调试日志体积，避免无限增长。"""
    compact = str(text or "")
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "\n...[truncated]"


def _trim_debug_log_file(path: Path, max_bytes: int = 1_000_000, keep_lines: int = 200) -> None:
    """在日志过大时保留最近若干行，避免调试文件持续膨胀。"""
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        lines = path.read_text("utf-8").splitlines()
        path.write_text("\n".join(lines[-keep_lines:]) + ("\n" if lines else ""), "utf-8")
    except Exception:
        return


def append_hook_debug_log(
    *,
    hook_name: str,
    client_name: str,
    payload: dict,
    detections: dict[str, Any],
    hook_result: dict[str, Any] | None = None,
) -> None:
    """把 Hook 执行现场落到本地 jsonl，便于排查真实 payload。"""
    path = _debug_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _trim_debug_log_file(path)
        record = {
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "hook_name": str(hook_name or "").strip(),
            "client_name": str(client_name or "").strip(),
            "parse_error": _LAST_HOOK_PARSE_ERROR,
            "top_level_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
            "detections": detections,
            "hook_result": hook_result or {},
            "payload_preview": _truncate_debug_text(_LAST_HOOK_RAW or json.dumps(payload or {}, ensure_ascii=False)),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return
