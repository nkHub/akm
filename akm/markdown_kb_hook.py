"""markdown_kb 客户端 Hook 适配逻辑。

这个模块的定位不是直接绑定某一个客户端，而是把 `Codex` / `Claude Code`
都会用到的公共动作先沉淀出来：

1. 在 `UserPromptSubmit` 阶段识别最后一行学习关键词；
2. 命中后去掉关键词，把净化后的 prompt 继续交还给客户端；
3. 把本轮学习状态写入本地 pending 文件；
4. 在 `Stop` / `PreCompact` 阶段读取 pending，向 AKM `/api/markdown-kb/learn`
   发起真正的学习请求；
5. 把成功、忽略、失败等结果再写回 pending 状态，供后续补偿或排查使用。

这样做的好处是：

1. 客户端差异只剩下“如何把事件字段喂给这个脚本”；
2. 真正复杂的关键词剥离、幂等键复用、失败补偿逻辑只维护一份；
3. 当前先用 CLI 暴露，后续即使要改成单独脚本入口，也可以直接复用这里的核心函数。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import httpx

from akm.config import load_config

DEFAULT_LEARN_KEYWORDS = ("AKM入库",)


def _utc_now_iso() -> str:
    """统一输出 UTC 时间字符串，方便 pending 文件和服务端记录互相对照。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_source(source: str) -> str:
    """把客户端来源收敛到服务端 `/learn` 当前允许的枚举值。"""
    normalized = str(source or "").strip().lower()
    if normalized not in {"codex", "claude_code"}:
        raise ValueError("source 仅支持 codex 或 claude_code")
    return normalized


def _normalize_workspace_root(workspace_root: str) -> str:
    """规整工作目录，避免尾部斜杠导致同一目录写出两份 pending。"""
    return str(workspace_root or "").strip().rstrip("/\\")


def _normalize_keywords(keywords: list[str] | tuple[str, ...] | None) -> list[str]:
    """规整学习关键词列表，未传时回落到默认关键词。"""
    normalized = []
    for item in keywords or []:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return normalized or list(DEFAULT_LEARN_KEYWORDS)


def _default_pending_path() -> Path:
    """返回默认 pending 文件路径。

    这里和 `markdown_kb` 插件继续共用 `~/.akm/markdown_kb/` 根目录，
    这样服务端生成的 learn 结果与客户端待处理状态天然落在同一棵目录树下，
    后续排查时不需要在多个地方来回找文件。
    """
    return (Path.home() / ".akm" / "markdown_kb" / "learn_pending.json").resolve()


def _ensure_parent_dir(path: Path) -> None:
    """确保 pending 文件父目录存在。"""
    path.parent.mkdir(parents=True, exist_ok=True)


def _load_pending_records(pending_path: Path) -> dict[str, dict]:
    """读取本地 pending 状态文件。

    这里刻意继续采用轻量 JSON 文件，而不是再上数据库，原因有三个：

    1. Hook 侧只需要很小的一份“会话 -> pending 状态”映射；
    2. 客户端脚本可能被频繁调用，JSON 文件比引入额外存储依赖更稳妥；
    3. 当前目标是先跑通学习触发链路，不把客户端适配脚本做成新的基础设施。
    """
    if not pending_path.exists():
        return {}
    try:
        data = json.loads(pending_path.read_text("utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict] = {}
    for key, value in data.items():
        normalized_key = str(key or "").strip()
        if normalized_key and isinstance(value, dict):
            result[normalized_key] = dict(value)
    return result


def _save_pending_records(pending_path: Path, records: dict[str, dict]) -> None:
    """保存本地 pending 状态文件。"""
    _ensure_parent_dir(pending_path)
    normalized: dict[str, dict] = {}
    for key, value in (records or {}).items():
        normalized_key = str(key or "").strip()
        if normalized_key and isinstance(value, dict):
            normalized[normalized_key] = value
    pending_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), "utf-8")


def _pending_record_key(source: str, session_id: str) -> str:
    """生成 pending 记录键。

    当前第一版按“客户端来源 + session”维度维护一条待处理记录。
    这样做的前提是：同一客户端会话在同一时刻通常只会存在一个待落地的学习请求。
    如果未来某个客户端出现“同一 session 并发多轮都能挂 pending”的场景，
    再把这里升级为 `session + turn` 多记录模式会更合适。
    """
    return f"{source}:{session_id}"


def _build_dedupe_key(
    *,
    source: str,
    session_id: str,
    turn_id: str,
    sanitized_prompt: str,
    explicit_dedupe_key: str = "",
) -> str:
    """生成或复用 dedupe_key。

    规则说明：

    1. 如果调用方已经显式给出 dedupe_key，则直接复用；
    2. `Codex` 优先使用 `session_id + turn_id`，和设计稿建议保持一致；
    3. `Claude Code` 当前没有稳定 turn_id，因此退回到 `session_id + user_prompt hash`；
    4. 关键点不是 hash 细节本身，而是 `UserPromptSubmit / Stop / PreCompact`
       三个阶段必须产出完全相同的 dedupe_key。
    """
    normalized_explicit = str(explicit_dedupe_key or "").strip()
    if normalized_explicit:
        return normalized_explicit
    normalized_turn_id = str(turn_id or "").strip()
    if source == "codex" and normalized_turn_id:
        return f"codex:{session_id}:{normalized_turn_id}"
    prompt_hash = hashlib.sha1(str(sanitized_prompt or "").encode("utf-8")).hexdigest()[:16]
    return f"{source}:{session_id}:{prompt_hash}"


def split_prompt_by_learn_keyword(prompt: str, keywords: list[str] | tuple[str, ...] | None = None) -> dict:
    """检查 prompt 最后一行是否为学习关键词，并返回净化结果。

    这里严格按设计稿执行：

    1. 只检查最后一行；
    2. 最后一行只允许做首尾空白裁剪后的精确匹配；
    3. 只要最后一行不是纯关键词，就视为未触发；
    4. 命中后只删除最后一行关键词，不擅自改写前面的正文内容。
    """
    original_prompt = str(prompt or "")
    normalized_keywords = _normalize_keywords(list(keywords or []))
    lines = original_prompt.splitlines()
    if not lines:
        return {
            "triggered": False,
            "learn_keyword": "",
            "sanitized_prompt": original_prompt,
        }
    last_line = lines[-1].strip()
    for keyword in normalized_keywords:
        if last_line != keyword:
            continue
        sanitized_prompt = "\n".join(lines[:-1]).rstrip()
        return {
            "triggered": True,
            "learn_keyword": keyword,
            "sanitized_prompt": sanitized_prompt,
        }
    return {
        "triggered": False,
        "learn_keyword": "",
        "sanitized_prompt": original_prompt,
    }


def _build_title_hint_from_prompt(prompt: str) -> str:
    """根据净化后的用户问题生成一个简短标题提示。"""
    first_line = ""
    for line in str(prompt or "").splitlines():
        compact = line.strip()
        if compact:
            first_line = compact
            break
    if not first_line:
        return ""
    return first_line[:80]


def register_prompt_submit(
    *,
    source: str,
    session_id: str,
    workspace_root: str,
    prompt: str,
    turn_id: str = "",
    keywords: list[str] | tuple[str, ...] | None = None,
    dedupe_key: str = "",
    pending_path: Path | None = None,
) -> dict:
    """处理 `UserPromptSubmit` 阶段。

    返回值里最重要的是两项：

    1. `sanitized_prompt`：客户端应继续转发给模型的净化正文；
    2. `triggered`：是否真的命中了学习关键词。
    """
    normalized_source = _normalize_source(source)
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        raise ValueError("session_id 不能为空")
    normalized_workspace_root = _normalize_workspace_root(workspace_root)
    split_result = split_prompt_by_learn_keyword(prompt, keywords)
    if not split_result["triggered"]:
        return {
            "ok": True,
            "triggered": False,
            "pending_written": False,
            "sanitized_prompt": split_result["sanitized_prompt"],
            "learn_keyword": "",
            "dedupe_key": "",
        }

    normalized_turn_id = str(turn_id or "").strip()
    sanitized_prompt = str(split_result["sanitized_prompt"] or "")
    final_dedupe_key = _build_dedupe_key(
        source=normalized_source,
        session_id=normalized_session_id,
        turn_id=normalized_turn_id,
        sanitized_prompt=sanitized_prompt,
        explicit_dedupe_key=dedupe_key,
    )
    record_key = _pending_record_key(normalized_source, normalized_session_id)
    path = pending_path or _default_pending_path()
    records = _load_pending_records(path)
    now = _utc_now_iso()
    previous = records.get(record_key) if isinstance(records.get(record_key), dict) else {}
    records[record_key] = {
        "source": normalized_source,
        "session_id": normalized_session_id,
        "turn_id": normalized_turn_id,
        "workspace_root": normalized_workspace_root,
        "user_prompt": sanitized_prompt,
        "title_hint": _build_title_hint_from_prompt(sanitized_prompt),
        "learn_keyword": split_result["learn_keyword"],
        "dedupe_key": final_dedupe_key,
        "status": "pending",
        "created_at": str(previous.get("created_at") or now),
        "updated_at": now,
        "processed_at": "",
        "attempt_count": 0,
        "last_error": "",
        "last_trigger_phase": "",
        "last_response": {},
    }
    _save_pending_records(path, records)
    return {
        "ok": True,
        "triggered": True,
        "pending_written": True,
        "sanitized_prompt": sanitized_prompt,
        "learn_keyword": split_result["learn_keyword"],
        "dedupe_key": final_dedupe_key,
        "pending_key": record_key,
    }


def _akm_api_base_url() -> str:
    """根据当前配置构造本地 AKM API 基础地址。"""
    cfg = load_config()
    port = int(cfg.get("server_port", 8800) or 8800)
    return f"http://127.0.0.1:{port}"


def _normalize_conversation_excerpt(value: Any) -> list[dict]:
    """规整对话摘录，保持和服务端 `/learn` 的字段格式一致。"""
    normalized = []
    if not isinstance(value, list):
        return normalized
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip() or "unknown"
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        normalized.append({
            "role": role,
            "text": text,
        })
    return normalized


async def _post_learn_payload(payload: dict) -> tuple[int, dict]:
    """调用本地 AKM `/api/markdown-kb/learn` 接口。"""
    url = f"{_akm_api_base_url()}/api/markdown-kb/learn"
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(url, json=payload)
    try:
        data = response.json()
    except json.JSONDecodeError:
        data = {"detail": response.text[:500]}
    return response.status_code, data if isinstance(data, dict) else {"detail": data}


async def trigger_pending_learn(
    *,
    trigger_phase: str,
    source: str,
    session_id: str,
    workspace_root: str = "",
    turn_id: str = "",
    assistant_excerpt: str = "",
    conversation_excerpt: list[dict] | None = None,
    title_hint: str = "",
    pending_path: Path | None = None,
) -> dict:
    """处理 `Stop` / `PreCompact` 阶段的学习提交。

    这里的行为目标是“尽力而为，不阻塞主会话”：

    1. 没有 pending 时直接返回 `skipped`；
    2. 已经成功或忽略过的 pending，不再重复提交；
    3. 真正请求失败时保留 pending，并记录 `last_error` 供 `PreCompact` 补偿。
    """
    normalized_phase = str(trigger_phase or "").strip().lower()
    if normalized_phase not in {"stop", "pre_compact"}:
        raise ValueError("trigger_phase 仅支持 stop 或 pre_compact")
    normalized_source = _normalize_source(source)
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        raise ValueError("session_id 不能为空")
    normalized_workspace_root = _normalize_workspace_root(workspace_root)
    normalized_turn_id = str(turn_id or "").strip()
    path = pending_path or _default_pending_path()
    records = _load_pending_records(path)
    record_key = _pending_record_key(normalized_source, normalized_session_id)
    record = dict(records.get(record_key) or {})
    if not record:
        return {
            "ok": True,
            "submitted": False,
            "skipped": True,
            "reason": "no_pending",
            "trigger_phase": normalized_phase,
        }
    if record.get("status") in {"completed", "ignored"}:
        return {
            "ok": True,
            "submitted": False,
            "skipped": True,
            "reason": "already_processed",
            "trigger_phase": normalized_phase,
            "dedupe_key": str(record.get("dedupe_key") or ""),
        }
    if normalized_turn_id and str(record.get("turn_id") or "").strip() and normalized_turn_id != str(record.get("turn_id") or "").strip():
        return {
            "ok": True,
            "submitted": False,
            "skipped": True,
            "reason": "turn_id_mismatch",
            "trigger_phase": normalized_phase,
        }

    final_workspace_root = normalized_workspace_root or _normalize_workspace_root(record.get("workspace_root") or "")
    final_turn_id = normalized_turn_id or str(record.get("turn_id") or "").strip()
    final_title_hint = str(title_hint or "").strip() or str(record.get("title_hint") or "").strip()
    payload = {
        "source": normalized_source,
        "trigger_phase": normalized_phase,
        "session_id": normalized_session_id,
        "turn_id": final_turn_id,
        "workspace_root": final_workspace_root,
        "title_hint": final_title_hint,
        "user_prompt": str(record.get("user_prompt") or "").strip(),
        "assistant_excerpt": str(assistant_excerpt or "").strip(),
        "conversation_excerpt": _normalize_conversation_excerpt(conversation_excerpt),
        "learn_keyword": str(record.get("learn_keyword") or "").strip(),
        "dedupe_key": str(record.get("dedupe_key") or "").strip(),
    }

    now = _utc_now_iso()
    try:
        status_code, response_data = await _post_learn_payload(payload)
    except Exception as exc:
        record["attempt_count"] = int(record.get("attempt_count") or 0) + 1
        record["updated_at"] = now
        record["last_error"] = str(exc)
        record["last_trigger_phase"] = normalized_phase
        records[record_key] = record
        _save_pending_records(path, records)
        return {
            "ok": False,
            "submitted": False,
            "skipped": False,
            "reason": "request_failed",
            "trigger_phase": normalized_phase,
            "dedupe_key": payload["dedupe_key"],
            "error": str(exc),
        }

    if status_code != 200 or not bool(response_data.get("ok")):
        detail = str(response_data.get("detail") or response_data.get("error") or f"HTTP {status_code}")
        record["attempt_count"] = int(record.get("attempt_count") or 0) + 1
        record["updated_at"] = now
        record["last_error"] = detail
        record["last_trigger_phase"] = normalized_phase
        record["last_response"] = response_data
        records[record_key] = record
        _save_pending_records(path, records)
        return {
            "ok": False,
            "submitted": False,
            "skipped": False,
            "reason": "learn_api_error",
            "trigger_phase": normalized_phase,
            "dedupe_key": payload["dedupe_key"],
            "status_code": status_code,
            "response": response_data,
        }

    record["attempt_count"] = int(record.get("attempt_count") or 0) + 1
    record["updated_at"] = now
    record["processed_at"] = now
    record["last_error"] = ""
    record["last_trigger_phase"] = normalized_phase
    record["last_response"] = response_data
    record["status"] = "ignored" if bool(response_data.get("ignored")) else "completed"
    records[record_key] = record
    _save_pending_records(path, records)
    return {
        "ok": True,
        "submitted": True,
        "skipped": False,
        "reason": "",
        "trigger_phase": normalized_phase,
        "dedupe_key": payload["dedupe_key"],
        "response": response_data,
    }


def _parse_conversation_json(raw: str) -> list[dict]:
    """把 CLI 传入的 JSON 字符串解析成 conversation_excerpt。"""
    compact = str(raw or "").strip()
    if not compact:
        return []
    parsed = json.loads(compact)
    return _normalize_conversation_excerpt(parsed)


def _emit_json(payload: dict) -> None:
    """统一输出 JSON，便于客户端 Hook 或脚本直接消费。"""
    click.echo(json.dumps(payload, ensure_ascii=False))


@click.group(name="markdown-kb-hook")
def markdown_kb_hook() -> None:
    """markdown_kb 客户端 Hook 辅助命令。"""


@markdown_kb_hook.command("prompt-submit")
@click.option("--source", required=True, type=click.Choice(["codex", "claude_code"]))
@click.option("--session-id", required=True)
@click.option("--workspace-root", required=True)
@click.option("--prompt", required=True)
@click.option("--turn-id", default="")
@click.option("--keyword", "keywords", multiple=True)
@click.option("--dedupe-key", default="")
@click.option("--pending-file", type=click.Path(dir_okay=False, path_type=Path), default=None)
def prompt_submit_command(
    source: str,
    session_id: str,
    workspace_root: str,
    prompt: str,
    turn_id: str,
    keywords: tuple[str, ...],
    dedupe_key: str,
    pending_file: Path | None,
) -> None:
    """处理 `UserPromptSubmit` 事件。

    命令成功时始终输出 JSON，方便直接被客户端 Hook 读取。
    如果业务逻辑里出现可恢复错误，也尽量通过 `ok=false` 回传，而不是让脚本直接退出非零。
    """
    try:
        result = register_prompt_submit(
            source=source,
            session_id=session_id,
            workspace_root=workspace_root,
            prompt=prompt,
            turn_id=turn_id,
            keywords=list(keywords),
            dedupe_key=dedupe_key,
            pending_path=pending_file,
        )
    except Exception as exc:
        result = {
            "ok": False,
            "triggered": False,
            "pending_written": False,
            "sanitized_prompt": str(prompt or ""),
            "error": str(exc),
        }
    _emit_json(result)


def _run_trigger_command(
    *,
    trigger_phase: str,
    source: str,
    session_id: str,
    workspace_root: str,
    turn_id: str,
    assistant_excerpt: str,
    conversation_json: str,
    title_hint: str,
    pending_file: Path | None,
) -> dict:
    """同步包装异步触发逻辑，避免 CLI 命令里重复写一遍错误处理。"""
    conversation_excerpt = _parse_conversation_json(conversation_json)
    return asyncio.run(
        trigger_pending_learn(
            trigger_phase=trigger_phase,
            source=source,
            session_id=session_id,
            workspace_root=workspace_root,
            turn_id=turn_id,
            assistant_excerpt=assistant_excerpt,
            conversation_excerpt=conversation_excerpt,
            title_hint=title_hint,
            pending_path=pending_file,
        )
    )


@markdown_kb_hook.command("stop")
@click.option("--source", required=True, type=click.Choice(["codex", "claude_code"]))
@click.option("--session-id", required=True)
@click.option("--workspace-root", default="")
@click.option("--turn-id", default="")
@click.option("--assistant-excerpt", default="")
@click.option("--conversation-json", default="[]")
@click.option("--title-hint", default="")
@click.option("--pending-file", type=click.Path(dir_okay=False, path_type=Path), default=None)
def stop_command(
    source: str,
    session_id: str,
    workspace_root: str,
    turn_id: str,
    assistant_excerpt: str,
    conversation_json: str,
    title_hint: str,
    pending_file: Path | None,
) -> None:
    """处理 `Stop` 事件。"""
    try:
        result = _run_trigger_command(
            trigger_phase="stop",
            source=source,
            session_id=session_id,
            workspace_root=workspace_root,
            turn_id=turn_id,
            assistant_excerpt=assistant_excerpt,
            conversation_json=conversation_json,
            title_hint=title_hint,
            pending_file=pending_file,
        )
    except Exception as exc:
        result = {
            "ok": False,
            "submitted": False,
            "skipped": False,
            "reason": "unexpected_error",
            "trigger_phase": "stop",
            "error": str(exc),
        }
    _emit_json(result)


@markdown_kb_hook.command("pre-compact")
@click.option("--source", required=True, type=click.Choice(["codex", "claude_code"]))
@click.option("--session-id", required=True)
@click.option("--workspace-root", default="")
@click.option("--turn-id", default="")
@click.option("--assistant-excerpt", default="")
@click.option("--conversation-json", default="[]")
@click.option("--title-hint", default="")
@click.option("--pending-file", type=click.Path(dir_okay=False, path_type=Path), default=None)
def pre_compact_command(
    source: str,
    session_id: str,
    workspace_root: str,
    turn_id: str,
    assistant_excerpt: str,
    conversation_json: str,
    title_hint: str,
    pending_file: Path | None,
) -> None:
    """处理 `PreCompact` 事件。"""
    try:
        result = _run_trigger_command(
            trigger_phase="pre_compact",
            source=source,
            session_id=session_id,
            workspace_root=workspace_root,
            turn_id=turn_id,
            assistant_excerpt=assistant_excerpt,
            conversation_json=conversation_json,
            title_hint=title_hint,
            pending_file=pending_file,
        )
    except Exception as exc:
        result = {
            "ok": False,
            "submitted": False,
            "skipped": False,
            "reason": "unexpected_error",
            "trigger_phase": "pre_compact",
            "error": str(exc),
        }
    _emit_json(result)


if __name__ == "__main__":
    markdown_kb_hook()
