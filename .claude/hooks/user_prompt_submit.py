#!/Users/nk/Desktop/ccs/.venv/bin/python
"""Claude Code UserPromptSubmit wrapper。"""

from __future__ import annotations

from markdown_kb_hook_common import (
    build_continue_response,
    append_hook_debug_log,
    detect_prompt,
    detect_session_id,
    detect_turn_id,
    detect_workspace_root,
    read_hook_payload,
    run_akm_hook,
)


def main() -> None:
    payload = read_hook_payload()
    session_id = detect_session_id(payload)
    workspace_root = detect_workspace_root(payload)
    prompt = detect_prompt(payload)
    turn_id = detect_turn_id(payload)
    detections = {
        "session_id": session_id,
        "workspace_root": workspace_root,
        "prompt": prompt,
        "turn_id": turn_id,
    }

    if not session_id or not workspace_root or not prompt:
        append_hook_debug_log(
            hook_name="UserPromptSubmit",
            client_name="claude_code",
            payload=payload,
            detections=detections,
            hook_result={"continue": True, "reason": "missing_required_fields"},
        )
        print(build_continue_response())
        return

    result = run_akm_hook([
        "prompt-submit",
        "--source", "claude_code",
        "--session-id", session_id,
        "--workspace-root", workspace_root,
        "--prompt", prompt,
        "--turn-id", turn_id,
    ])
    append_hook_debug_log(
        hook_name="UserPromptSubmit",
        client_name="claude_code",
        payload=payload,
        detections=detections,
        hook_result=result,
    )
    if bool(result.get("triggered")):
        print(build_continue_response("检测到本轮包含学习关键词，已登记待入库状态。请忽略用户输入最后一行的触发词，它不属于实际业务问题。"))
        return
    print(build_continue_response())


if __name__ == "__main__":
    main()
