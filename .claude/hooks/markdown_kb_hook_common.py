"""Claude Hook 侧复用 Codex 目录下的公共实现。

这里不直接做符号链接，而是通过 importlib 显式加载同仓库里的共用文件，
避免不同平台或打包/归档工具对符号链接处理不一致时把 Hook 搞坏。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


_COMMON_PATH = Path(__file__).resolve().parents[2] / ".codex" / "hooks" / "markdown_kb_hook_common.py"
_SPEC = importlib.util.spec_from_file_location("codex_markdown_kb_hook_common", _COMMON_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载公共 Hook 模块: {_COMMON_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

read_hook_payload = _MODULE.read_hook_payload
detect_workspace_root = _MODULE.detect_workspace_root
detect_session_id = _MODULE.detect_session_id
detect_turn_id = _MODULE.detect_turn_id
detect_prompt = _MODULE.detect_prompt
detect_assistant_excerpt = _MODULE.detect_assistant_excerpt
detect_conversation_excerpt = _MODULE.detect_conversation_excerpt
run_akm_hook = _MODULE.run_akm_hook
build_continue_response = _MODULE.build_continue_response
append_hook_debug_log = _MODULE.append_hook_debug_log
