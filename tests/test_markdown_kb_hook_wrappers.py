import json
import subprocess
import os
from pathlib import Path


def test_codex_user_prompt_submit_wrapper_runs_and_returns_continue_json():
    """Codex wrapper 至少应能读取 stdin JSON，并返回可继续执行的 Hook JSON。"""
    repo_root = Path("/Users/nk/Desktop/ccs")
    script = repo_root / ".codex" / "hooks" / "user_prompt_submit.py"
    payload = {
        "session_id": "sess_wrapper",
        "workspace_root": str(repo_root),
        "prompt": "请帮我总结这次排查\nAKM入库",
        "turn_id": "turn_wrapper",
    }

    result = subprocess.run(
        [str(repo_root / ".venv" / "bin" / "python"), str(script)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        cwd=str(repo_root),
    )

    assert result.returncode == 0
    response = json.loads(result.stdout.strip())
    assert response["continue"] is True


def test_codex_user_prompt_submit_wrapper_accepts_nested_payload_and_writes_debug_log(tmp_path: Path, monkeypatch):
    """Codex wrapper 应能从嵌套 payload 中识别字段，并写出调试日志。"""
    repo_root = Path("/Users/nk/Desktop/ccs")
    script = repo_root / ".codex" / "hooks" / "user_prompt_submit.py"
    debug_file = tmp_path / "hook_debug.jsonl"
    payload = {
        "session": {"id": "sess_nested"},
        "workspace": {"root": str(repo_root)},
        "input": {"prompt": "请帮我总结这次排查\nAKM入库"},
        "turn": {"id": "turn_nested"},
    }
    env = {**os.environ, "AKM_MARKDOWN_KB_HOOK_DEBUG_FILE": str(debug_file)}

    result = subprocess.run(
        [str(repo_root / ".venv" / "bin" / "python"), str(script)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        cwd=str(repo_root),
        env=env,
    )

    assert result.returncode == 0
    response = json.loads(result.stdout.strip())
    assert response["continue"] is True
    assert debug_file.exists()
    debug_lines = debug_file.read_text("utf-8").strip().splitlines()
    assert debug_lines
    record = json.loads(debug_lines[-1])
    assert record["client_name"] == "codex"
    assert record["detections"]["session_id"] == "sess_nested"
    assert record["detections"]["workspace_root"] == str(repo_root)
    assert record["detections"]["prompt"] == "请帮我总结这次排查\nAKM入库"


def test_claude_stop_wrapper_accepts_nested_messages_and_writes_debug_log(tmp_path: Path):
    """Claude wrapper 应能从常见消息数组里抽取 assistant 摘录并落日志。"""
    repo_root = Path("/Users/nk/Desktop/ccs")
    script = repo_root / ".claude" / "hooks" / "stop.py"
    debug_file = tmp_path / "hook_debug.jsonl"
    payload = {
        "session": {"id": "sess_stop"},
        "workspace": {"root": str(repo_root)},
        "messages": [
            {"role": "user", "content": "请帮我总结这次排查\nAKM入库"},
            {"role": "assistant", "content": "已经定位到重复绑定 submit 事件。"},
        ],
        "turn": {"id": "turn_stop"},
    }
    env = {**os.environ, "AKM_MARKDOWN_KB_HOOK_DEBUG_FILE": str(debug_file)}

    result = subprocess.run(
        [str(repo_root / ".venv" / "bin" / "python"), str(script)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        cwd=str(repo_root),
        env=env,
    )

    assert result.returncode == 0
    response = json.loads(result.stdout.strip())
    assert response["continue"] is True
    assert debug_file.exists()
    record = json.loads(debug_file.read_text("utf-8").strip().splitlines()[-1])
    assert record["client_name"] == "claude_code"
    assert record["detections"]["session_id"] == "sess_stop"
    assert record["detections"]["assistant_excerpt"] == "已经定位到重复绑定 submit 事件。"
