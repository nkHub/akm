"""Session Scanner：解析客户端本地 JSONL 会话文件，生成知识并更新记忆。

支持 Codex 和 Claude Code 两种客户端的会话格式，统一归一化后复用核心链路。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_logger = logging.getLogger("markdown_kb.session_scanner")

# 扫描记录持久化文件名
SCANNED_SESSIONS_FILE = "scanned_sessions.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ────────────────────────────── 文件发现 ──────────────────────────────


def list_codex_sessions(since_hours: float) -> list[Path]:
    """列出 ~/.codex/sessions/ 下 mtime 在 since_hours 内的 JSONL 文件。"""
    codex_sessions_dir = Path.home() / ".codex" / "sessions"
    if not codex_sessions_dir.is_dir():
        return []

    cutoff = datetime.now().timestamp() - since_hours * 3600
    files: list[Path] = []
    for root, _, filenames in os.walk(str(codex_sessions_dir)):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            fpath = Path(root) / name
            try:
                if fpath.stat().st_mtime >= cutoff:
                    files.append(fpath)
            except OSError:
                continue
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def list_claude_sessions(since_hours: float) -> list[Path]:
    """列出 ~/.claude/projects/*/ 下 mtime 在 since_hours 内的 JSONL 文件。"""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []

    cutoff = datetime.now().timestamp() - since_hours * 3600
    files: list[Path] = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for fpath in project_dir.glob("*.jsonl"):
            try:
                if fpath.stat().st_mtime >= cutoff:
                    files.append(fpath)
            except OSError:
                continue
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


# ────────────────────────────── Session 解析 ──────────────────────────────


def parse_codex_session(filepath: Path) -> dict | None:
    """解析 Codex 会话 JSONL，返回归一化结构。"""
    try:
        lines = filepath.read_text("utf-8").strip().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        _logger.warning("无法读取 Codex 会话文件 %s: %s", filepath, exc)
        return None

    if not lines:
        return None

    session_id = ""
    cwd = ""
    turns: list[dict] = []

    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = str(obj.get("type") or "")
        payload = obj.get("payload") or {}

        if event_type == "session_meta":
            session_id = str(payload.get("session_id") or payload.get("id") or "") or session_id
            cwd = str(payload.get("cwd") or "") or cwd
            continue

        if event_type == "response_item" and isinstance(payload, dict):
            msg_type = str(payload.get("type") or "")
            if msg_type != "message":
                continue
            role = str(payload.get("role") or "")
            if role not in ("user", "assistant"):
                continue

            content = payload.get("content") or []
            text_parts: list[str] = []
            for item in content if isinstance(content, list) else []:
                if isinstance(item, dict):
                    t = str(item.get("text") or "").strip()
                    if t:
                        text_parts.append(t)
            if not text_parts:
                continue
            turns.append({"role": role, "text": "\n".join(text_parts)})

    if not session_id:
        return None

    return {
        "session_id": session_id,
        "cwd": cwd,
        "source": "codex",
        "turns": turns,
    }


def parse_claude_session(filepath: Path) -> dict | None:
    """解析 Claude Code 会话 JSONL，返回归一化结构。"""
    try:
        lines = filepath.read_text("utf-8").strip().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        _logger.warning("无法读取 Claude 会话文件 %s: %s", filepath, exc)
        return None

    if not lines:
        return None

    session_id = ""
    cwd = ""
    turns: list[dict] = []

    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = str(obj.get("type") or "")
        if event_type == "user":
            session_id = str(obj.get("sessionId") or "") or session_id
            cwd = str(obj.get("cwd") or "") or cwd
            message = obj.get("message") or {}
            if isinstance(message, dict) and str(message.get("role") or "") == "user":
                content = message.get("content")
                text = _extract_claude_content(content)
                if text:
                    is_meta = obj.get("isMeta")
                    if not is_meta:
                        turns.append({"role": "user", "text": text})
            continue

        if event_type == "assistant":
            session_id = str(obj.get("sessionId") or "") or session_id
            cwd = str(obj.get("cwd") or "") or cwd
            is_api_error = obj.get("isApiErrorMessage")
            if is_api_error:
                continue
            message = obj.get("message") or {}
            if isinstance(message, dict) and str(message.get("role") or "") == "assistant":
                content = message.get("content")
                text = _extract_claude_content(content)
                if text:
                    turns.append({"role": "assistant", "text": text})
            continue

        if event_type == "mode" and not session_id:
            session_id = str(obj.get("sessionId") or "")

    if not session_id:
        return None

    return {
        "session_id": session_id,
        "cwd": cwd,
        "source": "claude_code",
        "turns": turns,
    }


def _extract_claude_content(content: Any) -> str:
    """从 Claude 的 content 字段提取纯文本（兼容字符串和数组两种格式）。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                t = str(item.get("text") or "").strip()
                if t:
                    parts.append(t)
            elif isinstance(item, str):
                t = item.strip()
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


def parse_session_file(filepath: Path) -> dict | None:
    """自动检测会话文件格式并解析。

    通过读取首行 JSON 中的 type 字段判断格式：
    - Codex 首行通常是 session_meta 或 rollout 类型的元信息
    - Claude Code 首行通常是 mode 或 user
    """
    try:
        first_line = filepath.read_text("utf-8").splitlines()[0].strip()
        obj = json.loads(first_line)
        first_type = str(obj.get("type") or "")
        # Codex 会话首行通常是 session_meta 或 rollout 标记
        if first_type in ("session_meta", "rollout"):
            return parse_codex_session(filepath)
        # Claude Code 首行通常是 mode
        if first_type in ("mode", "user", "assistant", "permission-mode"):
            return parse_claude_session(filepath)
        # 无法确定格式，尝试两种解析器
        result = parse_codex_session(filepath)
        if result and result.get("turns"):
            return result
        return parse_claude_session(filepath)
    except (OSError, json.JSONDecodeError, IndexError):
        return None


# ────────────────────────────── 扫描记录管理 ──────────────────────────────


def load_scanned_records(data_root: Path) -> dict[str, dict]:
    """加载已处理 session 记录。"""
    path = data_root / SCANNED_SESSIONS_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_scanned_records(data_root: Path, records: dict[str, dict]) -> None:
    """持久化已处理 session 记录。"""
    path = data_root / SCANNED_SESSIONS_FILE
    try:
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), "utf-8")
    except OSError as exc:
        _logger.warning("无法写入扫描记录 %s: %s", path, exc)


def mark_scanned_learned(
    records: dict[str, dict],
    dedupe_key: str,
    doc_count: int,
    now: str,
) -> None:
    """两阶段写入之第一阶段：learn 成功后立即标记。"""
    records[dedupe_key] = {
        "scanned_at": now,
        "learned": True,
        "memory_updated": False,
        "doc_count": doc_count,
        "boosted_chunks": 0,
    }


def mark_scanned_memory(
    records: dict[str, dict],
    dedupe_key: str,
    boosted_chunks: int,
    now: str,
) -> None:
    """两阶段写入之第二阶段：memory 完成后续写。"""
    entry = records.get(dedupe_key) or {}
    entry["memory_updated"] = True
    entry["boosted_chunks"] = boosted_chunks
    entry["scanned_at"] = entry.get("scanned_at") or now
    records[dedupe_key] = entry


def needs_memory_update(record: dict) -> bool:
    """检查已知记录是否需要补做 memory 更新。"""
    return bool(record.get("learned")) and not bool(record.get("memory_updated"))
