import asyncio
import json
from pathlib import Path

from click.testing import CliRunner

from akm.cli import main
from akm.markdown_kb_hook import (
    register_prompt_submit,
    split_prompt_by_learn_keyword,
    trigger_pending_learn,
)
from tests.test_cli import _setup_tmp_env


def test_split_prompt_by_learn_keyword_only_matches_last_line_exactly():
    """只允许最后一行精确命中关键词，避免把正文中的普通文本误识别为学习触发。"""
    matched = split_prompt_by_learn_keyword("请帮我总结这次排查\nAKM入库")
    assert matched["triggered"] is True
    assert matched["learn_keyword"] == "AKM入库"
    assert matched["sanitized_prompt"] == "请帮我总结这次排查"

    not_matched = split_prompt_by_learn_keyword("AKM入库\n请帮我总结这次排查")
    assert not_matched["triggered"] is False
    assert not_matched["sanitized_prompt"] == "AKM入库\n请帮我总结这次排查"

    mixed_line = split_prompt_by_learn_keyword("请帮我总结这次排查 AKM入库")
    assert mixed_line["triggered"] is False


def test_register_prompt_submit_writes_pending_and_reuses_codex_turn_id(tmp_path: Path):
    """Codex 命中关键词后应写入 pending，并按 session + turn 生成稳定 dedupe_key。"""
    pending_file = tmp_path / "learn_pending.json"

    result = register_prompt_submit(
        source="codex",
        session_id="sess_123",
        turn_id="turn_456",
        workspace_root="/Users/nk/Desktop/ccs/",
        prompt="请帮我总结这次退款页排查\nAKM入库",
        pending_path=pending_file,
    )

    assert result["ok"] is True
    assert result["triggered"] is True
    assert result["pending_written"] is True
    assert result["sanitized_prompt"] == "请帮我总结这次退款页排查"
    assert result["dedupe_key"] == "codex:sess_123:turn_456"

    data = json.loads(pending_file.read_text("utf-8"))
    pending = data["codex:sess_123"]
    assert pending["workspace_root"] == "/Users/nk/Desktop/ccs"
    assert pending["user_prompt"] == "请帮我总结这次退款页排查"
    assert pending["learn_keyword"] == "AKM入库"
    assert pending["dedupe_key"] == "codex:sess_123:turn_456"
    assert pending["status"] == "pending"


def test_trigger_pending_learn_stop_success_marks_record_completed(tmp_path: Path, monkeypatch):
    """Stop 成功调用 `/learn` 后，应把 pending 标记为 completed。"""
    pending_file = tmp_path / "learn_pending.json"
    register_prompt_submit(
        source="codex",
        session_id="sess_123",
        turn_id="turn_456",
        workspace_root="/Users/nk/Desktop/ccs",
        prompt="请帮我总结这次退款页排查\nAKM入库",
        pending_path=pending_file,
    )

    async def fake_post(payload: dict):
        assert payload["trigger_phase"] == "stop"
        assert payload["source"] == "codex"
        assert payload["session_id"] == "sess_123"
        assert payload["turn_id"] == "turn_456"
        assert payload["dedupe_key"] == "codex:sess_123:turn_456"
        assert payload["assistant_excerpt"] == "已经定位到重复绑定 submit 事件。"
        return 200, {
            "ok": True,
            "ignored": False,
            "status": "completed",
            "dedupe_key": "codex:sess_123:turn_456",
            "doc_id": "doc_1",
            "file_name": "2026-06-24-refund.learn.md",
        }

    monkeypatch.setattr("akm.markdown_kb_hook._post_learn_payload", fake_post)

    result = asyncio.run(
        trigger_pending_learn(
            trigger_phase="stop",
            source="codex",
            session_id="sess_123",
            workspace_root="/Users/nk/Desktop/ccs",
            turn_id="turn_456",
            assistant_excerpt="已经定位到重复绑定 submit 事件。",
            conversation_excerpt=[
                {"role": "user", "text": "请帮我总结这次退款页排查"},
                {"role": "assistant", "text": "已经定位到重复绑定 submit 事件。"},
            ],
            pending_path=pending_file,
        )
    )

    assert result["ok"] is True
    assert result["submitted"] is True
    assert result["skipped"] is False
    assert result["dedupe_key"] == "codex:sess_123:turn_456"

    saved = json.loads(pending_file.read_text("utf-8"))["codex:sess_123"]
    assert saved["status"] == "completed"
    assert saved["last_trigger_phase"] == "stop"
    assert saved["last_error"] == ""
    assert saved["attempt_count"] == 1


def test_trigger_pending_learn_precompact_retries_after_stop_failure(tmp_path: Path, monkeypatch):
    """Stop 失败后，PreCompact 应复用同一 dedupe_key 继续补偿提交。"""
    pending_file = tmp_path / "learn_pending.json"
    register_prompt_submit(
        source="claude_code",
        session_id="sess_789",
        workspace_root="/Users/nk/Desktop/ccs",
        prompt="请帮我沉淀这次排查\nAKM入库",
        pending_path=pending_file,
    )

    calls = []

    async def fake_post(payload: dict):
        calls.append(dict(payload))
        if payload["trigger_phase"] == "stop":
            return 502, {"detail": "upstream timeout"}
        return 200, {
            "ok": True,
            "ignored": False,
            "status": "completed",
            "dedupe_key": payload["dedupe_key"],
            "doc_id": "doc_2",
            "file_name": "2026-06-24-learn.learn.md",
        }

    monkeypatch.setattr("akm.markdown_kb_hook._post_learn_payload", fake_post)

    stop_result = asyncio.run(
        trigger_pending_learn(
            trigger_phase="stop",
            source="claude_code",
            session_id="sess_789",
            workspace_root="/Users/nk/Desktop/ccs",
            assistant_excerpt="先记录失败。",
            pending_path=pending_file,
        )
    )
    assert stop_result["ok"] is False
    assert stop_result["reason"] == "learn_api_error"

    precompact_result = asyncio.run(
        trigger_pending_learn(
            trigger_phase="pre_compact",
            source="claude_code",
            session_id="sess_789",
            workspace_root="/Users/nk/Desktop/ccs",
            assistant_excerpt="PreCompact 再试一次。",
            pending_path=pending_file,
        )
    )
    assert precompact_result["ok"] is True
    assert precompact_result["submitted"] is True

    assert len(calls) == 2
    assert calls[0]["dedupe_key"] == calls[1]["dedupe_key"]
    assert calls[0]["trigger_phase"] == "stop"
    assert calls[1]["trigger_phase"] == "pre_compact"

    saved = json.loads(pending_file.read_text("utf-8"))["claude_code:sess_789"]
    assert saved["status"] == "completed"
    assert saved["attempt_count"] == 2
    assert saved["last_trigger_phase"] == "pre_compact"


def test_markdown_kb_hook_cli_prompt_submit_returns_sanitized_prompt(tmp_path: Path, monkeypatch):
    """CLI 入口应输出机器可消费 JSON，方便直接接到客户端 Hook。"""
    _setup_tmp_env(monkeypatch)
    pending_file = tmp_path / "learn_pending.json"
    result = CliRunner().invoke(
        main,
        [
            "markdown-kb-hook",
            "prompt-submit",
            "--source", "codex",
            "--session-id", "sess_cli",
            "--workspace-root", "/Users/nk/Desktop/ccs",
            "--turn-id", "turn_cli",
            "--prompt", "请帮我总结这次会话\nAKM入库",
            "--pending-file", str(pending_file),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert payload["triggered"] is True
    assert payload["sanitized_prompt"] == "请帮我总结这次会话"
    assert payload["dedupe_key"] == "codex:sess_cli:turn_cli"
