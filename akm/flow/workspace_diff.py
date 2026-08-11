"""工作区文件快照与差异（移植自 flow 项目的 workspace-diff.ts）。

用于编码节点执行前后对比文件变化，生成 FileDiff 列表（含 unified 风格
patchPreview）。遍历有边界限制：跳过常见依赖/构建目录、只收录文本扩展名、
单文件 ≤120KB、最多扫描 800 个文件、最多 40 条 diff。
"""

import os
from typing import Any

# 跳过目录（依赖/构建产物）
SKIP_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".next",
    ".data",
    "coverage",
    ".turbo",
    ".cache",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
}

# 文本文件扩展名白名单
TEXT_EXT = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".md",
    ".css", ".scss", ".html", ".vue", ".svelte", ".py", ".go", ".rs",
    ".java", ".kt", ".swift", ".yml", ".yaml", ".toml", ".sh", ".sql",
    ".txt", ".env", ".gitignore",
}

# 快照/差异边界默认值；可通过 config.json 的 agent_flow 自定义
MAX_FILE_BYTES = 120_000
MAX_FILES_SCAN = 800
MAX_DIFFS = 40
MAX_CONTENT_CHARS = 24_000


def _flow_wsdiff_limits() -> dict:
    """解析工作区快照/差异边界：优先 config.json 的 agent_flow.wsdiff_*，否则默认值。

    返回 {max_file_bytes, max_files_scan, max_diffs, max_content_chars}。
    """
    try:
        from akm.config import load_config

        cfg = load_config().get("agent_flow") or {}
        return {
            "max_file_bytes": int(cfg.get("wsdiff_max_file_bytes") or MAX_FILE_BYTES),
            "max_files_scan": int(cfg.get("wsdiff_max_files_scan") or MAX_FILES_SCAN),
            "max_diffs": int(cfg.get("wsdiff_max_diffs") or MAX_DIFFS),
            "max_content_chars": int(cfg.get("wsdiff_max_content_chars") or MAX_CONTENT_CHARS),
        }
    except Exception:  # noqa: BLE001
        return {
            "max_file_bytes": MAX_FILE_BYTES,
            "max_files_scan": MAX_FILES_SCAN,
            "max_diffs": MAX_DIFFS,
            "max_content_chars": MAX_CONTENT_CHARS,
        }


def build_patch_preview(path: str, before: str | None, after: str | None, max_lines: int = 80) -> str:
    """由 before/after 文本生成简化 unified 风格预览（移植自 shared buildPatchPreview）。"""
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    lines: list[str] = [f"--- a/{path}", f"+++ b/{path}"]
    max_len = max(len(a), len(b))
    shown = 0
    for i in range(max_len):
        if shown >= max_lines:
            break
        left = a[i] if i < len(a) else None
        right = b[i] if i < len(b) else None
        if left == right:
            continue
        if left is not None and right is None:
            lines.append(f"-{left}")
            shown += 1
        elif left is None and right is not None:
            lines.append(f"+{right}")
            shown += 1
        elif left != right:
            lines.append(f"-{left}")
            lines.append(f"+{right}")
            shown += 2
    if shown == 0 and before != after:
        lines.append("@@ content changed @@")
    if shown >= max_lines:
        lines.append("…[truncated]")
    return "\n".join(lines)


def _walk_files(root: str, max_files_scan: int = MAX_FILES_SCAN) -> list[str]:
    """递归收集文本文件路径（有边界）。"""
    out: list[str] = []

    def _walk(dir_path: str) -> None:
        if len(out) >= max_files_scan:
            return
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: e.name)
        except OSError:
            return
        for ent in entries:
            if len(out) >= max_files_scan:
                return
            # 跳过隐藏文件（.env/.gitignore 例外）
            if ent.name.startswith(".") and ent.name not in (".env", ".gitignore"):
                if ent.is_dir():
                    continue
            full = os.path.join(dir_path, ent.name)
            if ent.is_dir():
                if ent.name in SKIP_DIRS:
                    continue
                _walk(full)
            elif ent.is_file():
                ext = os.path.splitext(ent.name)[1].lower()
                if ext and ext not in TEXT_EXT:
                    continue
                out.append(full)

    _walk(root)
    return out


def _read_text_limited(
    path: str,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_content_chars: int = MAX_CONTENT_CHARS,
) -> str | None:
    """读取文本文件，超限/二进制/异常返回 None。"""
    try:
        st = os.stat(path)
        if not os.path.isfile(path) or st.st_size > max_file_bytes:
            return None
        with open(path, "rb") as fh:
            buf = fh.read()
        if b"\x00" in buf:
            return None
        text = buf.decode("utf-8", errors="replace")
        if len(text) > max_content_chars:
            text = text[:max_content_chars] + f"\n…[truncated {len(text) - max_content_chars} chars]"
        return text
    except OSError:
        return None


def snapshot_workspace(project_path: str) -> dict[str, str | None]:
    """快照工作区文本文件：{相对路径: 内容或 None}。"""
    limits = _flow_wsdiff_limits()
    root = os.path.abspath(os.path.expanduser(project_path))
    snapshot: dict[str, str | None] = {}
    for full in _walk_files(root, limits["max_files_scan"]):
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        snapshot[rel] = _read_text_limited(
            full,
            max_file_bytes=limits["max_file_bytes"],
            max_content_chars=limits["max_content_chars"],
        )
    return snapshot


def diff_workspace(project_path: str, before: dict[str, str | None]) -> list[dict]:
    """对比执行前后差异，返回 FileDiff 列表。"""
    limits = _flow_wsdiff_limits()
    after = snapshot_workspace(project_path)
    keys = sorted(set(before.keys()) | set(after.keys()))
    diffs: list[dict] = []
    for rel in keys:
        if len(diffs) >= limits["max_diffs"]:
            break
        a = before.get(rel)
        b = after.get(rel)
        if a == b:
            continue
        # 两边都不可读（内容均为 None）但键一致——跳过噪音
        if a is None and b is None:
            continue
        if a is None and b is not None:
            status = "added"
        elif a is not None and b is None:
            status = "deleted"
        else:
            status = "modified"
        before_text = a if a is not None else None
        after_text = b if b is not None else None
        diffs.append({
            "path": rel,
            "status": status,
            "before": before_text,
            "after": after_text,
            "patchPreview": build_patch_preview(rel, before_text, after_text),
        })
    return diffs
