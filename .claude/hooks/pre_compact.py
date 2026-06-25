#!/Users/nk/Desktop/ccs/.venv/bin/python
"""Claude Code PreCompact wrapper。"""

from __future__ import annotations

import json

from markdown_kb_hook_common import (
    append_hook_debug_log,
    build_continue_response,
    detect_assistant_excerpt,
    detect_conversation_excerpt,
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
    if not session_id:
        append_hook_debug_log(
            hook_name="PreCompact",
            client_name="claude_code",
            payload=payload,
            detections={
                "session_id": session_id,
                "workspace_root": workspace_root,
                "turn_id": detect_turn_id(payload),
                "assistant_excerpt": detect_assistant_excerpt(payload),
                "conversation_excerpt": detect_conversation_excerpt(payload),
            },
            hook_result={"continue": True, "reason": "missing_required_fields"},
        )
        print(build_continue_response())
        return

    conversation = detect_conversation_excerpt(payload)
    result = run_akm_hook([
        "pre-compact",
        "--source", "claude_code",
        "--session-id", session_id,
        "--workspace-root", workspace_root,
        "--turn-id", detect_turn_id(payload),
        "--assistant-excerpt", detect_assistant_excerpt(payload),
        "--conversation-json", json.dumps(conversation, ensure_ascii=False),
    ])
    append_hook_debug_log(
        hook_name="PreCompact",
        client_name="claude_code",
        payload=payload,
        detections={
            "session_id": session_id,
            "workspace_root": workspace_root,
            "turn_id": detect_turn_id(payload),
            "assistant_excerpt": detect_assistant_excerpt(payload),
            "conversation_excerpt": conversation,
        },
        hook_result=result,
    )
    if not bool(result.get("ok")):
        print(build_continue_response("学习入库 PreCompact 钩子执行失败，但不影响当前会话继续 compact。"))
        return
    print(build_continue_response())


if __name__ == "__main__":
    main()
