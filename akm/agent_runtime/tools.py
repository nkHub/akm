"""供 Agent Loop 使用的 AKM 内置只读调试工具。"""

import asyncio
import base64
import datetime
import json
import logging
import mimetypes
import re
import shutil
import subprocess
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from akm.agent_runtime.loop import ToolDef
from akm.agent_runtime.tavily_mcp import tavily_search
from akm.audit import list_logs_async
from akm.config import load_config
from akm.key_pool import key_model_list, list_keys

logger = logging.getLogger(__name__)

# 文件读取工具单次返回的最大字节数，防止超大文件撑爆模型上下文
_WORKSPACE_READ_MAX_BYTES = 60000
# grep 工具单次返回的最大命中行数
_WORKSPACE_GREP_MAX_RESULTS = 100
# run_shell 工具默认超时（秒）
_WORKSPACE_SHELL_TIMEOUT_SEC = 60

# 请求级工作区覆盖：由 AgentLoop 在单次请求执行工具期间设置，优先于
# config.json 的全局 agent_workspace_root；空字符串表示不使用覆盖。
# 用 ContextVar 保证并发请求之间互不影响（每个请求独立的 asyncio 上下文）。
_request_workspace_root: ContextVar[str] = ContextVar("agent_request_workspace_root", default="")


def set_request_workspace_root(root: str):
    """设置本次请求执行工具期间的工作区覆盖，返回用于恢复的 token。

    Args:
        root: 覆盖的工作区根目录绝对路径；空字符串表示不覆盖（走全局配置）。
    """
    return _request_workspace_root.set(str(root or "").strip())


def reset_request_workspace_root(token) -> None:
    """恢复 set_request_workspace_root 之前的工作区上下文。"""
    _request_workspace_root.reset(token)


def _workspace_root() -> Path | None:
    """返回当前请求的 Agent 工作区沙箱根目录（已展开 ~、已 resolve），未配置时返回 None。

    工作区根目录优先取请求级覆盖（/v1/agent 请求的 workspace_root 字段，
    由 AgentLoop 执行工具前设置），否则回退 config.json 的全局
    agent_workspace_root。所有文件读写工具都只能访问该目录内的路径
    （防止路径穿越读写任意文件）；未配置时文件工具整体不可用。
    """
    raw = (
        _request_workspace_root.get().strip()
        or str(load_config().get("agent_workspace_root") or "").strip()
    )
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _safe_resolve_workspace_path(raw: str, *, must_exist: bool = True) -> Path:
    """把模型传入的路径解析为工作区内的绝对路径，越界或缺失时抛 ValueError。

    安全约定：
    - 绝对路径必须位于工作区根目录内，否则拒绝；
    - 相对路径按工作区根目录拼接后再校验，`..` 等穿越写法会被拦截；
    - 最终用 resolve() 规范化，防止软链接把目标引到工作区之外；
    - must_exist 为 True 时目标必须存在（读写场景），False 时允许不存在
      （新建文件场景，但仍要求其父目录位于工作区内）。

    Returns:
        规范化后的绝对路径（Path 对象）。

    Raises:
        ValueError: 未配置工作区、路径越界或（must_exist 时）目标不存在。
    """
    root = _workspace_root()
    if root is None:
        raise ValueError("未配置 agent_workspace_root，工作区文件工具不可用")
    raw = str(raw or "").strip()
    if not raw:
        raise ValueError("path 不能为空")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"路径 {raw} 超出工作区范围，禁止访问")
    if must_exist and not candidate.exists():
        raise ValueError(f"路径 {raw} 不存在")
    return candidate


async def _check_tool_enabled(flag: str, tool_name: str) -> None:
    """检查工具开关配置，未启用时抛出带提示的错误（由调用方捕获返回给模型）。"""
    if not load_config().get(flag):
        raise PermissionError(
            f"工具 {tool_name} 未启用：请在 config.json 中设置 {flag}=true"
        )


def _akm_api_base_url() -> str:
    """返回本机 AKM 服务地址，供工具以 HTTP 调用内部接口（如 markdown-kb）。

    与 akm/markdown_kb_hook.py 的约定保持一致：走 127.0.0.1 + server_port
    配置项，避免依赖外部可访问地址。
    """
    port = int(load_config().get("server_port", 8800) or 8800)
    return f"http://127.0.0.1:{port}"


def _image_default_model() -> str:
    """返回 config.json 中 image_supported_models 配置的首个模型。

    与 server.py 的 _default_image_generation_model 保持一致；为避免
    tools 与 server 互相 import 造成循环依赖，这里内联解析配置。
    """
    raw = str(load_config().get("image_supported_models") or "gpt-image-2").strip()
    models = [item.strip() for item in raw.split(",") if item.strip()]
    return (models or ["gpt-image-2"])[0]


def _image_request_timeout() -> float:
    """返回 config.json 中 image_request_timeout_sec 配置的图片请求超时秒数。"""
    try:
        timeout = float(load_config().get("image_request_timeout_sec", 300) or 300)
    except (TypeError, ValueError):
        timeout = 300.0
    return max(30.0, timeout)


def _read_image_file(path: str) -> tuple[str, bytes, str]:
    """读取本地图片文件，返回 (文件名, 字节内容, content_type)。

    先按传入的 image_path 直接读取；若该路径不存在，则提取文件名回退到
    agent_upload_dir（默认 ~/.akm/cache）目录下查找。这样即使模型把
    http_url 中的 /agent-uploads/ 前缀误当成本地路径（例如编造出
    /data/agent-uploads/xxx.png），只要文件名一致仍能命中真实落盘文件。
    回退查找仅取文件名（basename），不会拼接目录，防止路径穿越。
    """
    file_path = Path(str(path or "")).expanduser()
    if not file_path.is_file():
        raw = str(load_config().get("agent_upload_dir") or "~/.akm/cache").strip()
        upload_dir = Path(raw).expanduser()
        fallback = upload_dir / file_path.name
        if fallback.is_file():
            file_path = fallback
        else:
            raise FileNotFoundError(f"图片文件不存在: {file_path}")
    content = file_path.read_bytes()
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return file_path.name, content, content_type


def _decode_image_base64(raw: str) -> tuple[str, bytes, str]:
    """解析图片 base64 数据，返回 (文件名, 字节内容, content_type)。

    兼容两种形态：带 ``data:image/png;base64,xxx`` 前缀的 data URL
    （Agent 对话中 image_url 内容块的形态），或去掉前缀的裸 base64。
    无前缀时按 image/png 处理；文件名用随机名 + 按 content_type 推断的
    扩展名，避免直接信任外部文件名。
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("图片 base64 数据为空")
    content_type = "image/png"
    if text.startswith("data:"):
        header, sep, payload = text.partition(",")
        if not sep:
            raise ValueError("图片 data URL 格式非法，缺少逗号分隔的 base64 数据")
        mime = header[5:].split(";", 1)[0].strip()
        if mime and mime.lower().startswith("image/"):
            content_type = mime
        text = payload.strip()
    try:
        data = base64.b64decode(text, validate=True)
    except Exception:
        raise ValueError("图片 base64 解码失败，请检查数据是否完整")
    if not data:
        raise ValueError("图片 base64 数据为空")
    ext = mimetypes.guess_extension(content_type) or ".png"
    filename = f"upload-{uuid.uuid4().hex}{ext}"
    return filename, data, content_type


def _resolve_image(image_path: str = "", image_base64: str = "") -> tuple[str, bytes, str]:
    """按 image_path 或 image_base64 解析图片，返回 (文件名, 字节内容, content_type)。

    image_base64 非空时优先使用 base64 数据（适用于本地无文件的云端场景，
    AI 可直接把对话中的 data URL 传入）；否则回退读取 image_path 本地文件。
    两者都为空时抛出 ValueError。
    """
    if str(image_base64 or "").strip():
        return _decode_image_base64(image_base64)
    if str(image_path or "").strip():
        return _read_image_file(image_path)
    raise ValueError("image_path 与 image_base64 必须至少提供一个")


def _persist_image(data: bytes, content_type: str, source: str = "") -> tuple[str, str, str]:
    """把图片字节保存到 agent_upload_dir，返回 (文件名, 本地路径, HTTP URL)。

    content_type 为空时尝试从来源 URL 后缀推断；扩展名取 mimetypes 映射，
    无匹配时回退 .bin。HTTP URL 指向 /agent-uploads/{filename}，由服务端
    按 agent_upload_dir 提供访问，供前端或外部工具直接拉取。
    """
    raw = str(load_config().get("agent_upload_dir") or "~/.akm/cache").strip()
    upload_dir = Path(raw).expanduser()
    upload_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not content_type:
        content_type = mimetypes.guess_type(source.split("?", 1)[0])[0] or ""
    ext = mimetypes.guess_extension(content_type or "") or ".bin"
    if not ext.startswith("."):
        ext = f".{ext}"
    filename = f"{uuid.uuid4().hex}{ext}"
    local_path = str(upload_dir / filename)
    with open(local_path, "wb") as fh:
        fh.write(data)
    port = int(load_config().get("server_port", 8800) or 8800)
    http_url = f"http://127.0.0.1:{port}/agent-uploads/{filename}"
    return filename, local_path, http_url


async def _save_generated_image(item: dict[str, Any], client) -> None:
    """把生成/编辑结果的图片保存到上传目录，并填充 local_path/http_url。

    有 url 时通过连接池 client 下载；只有 b64_json 时直接解码。保存成功
    附加 local_path 与 http_url 字段，失败附加 save_error，均不影响主结果。
    """
    try:
        url = item.get("url")
        if url:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
            content_type = (resp.headers or {}).get("content-type", "")
        else:
            b64 = item.pop("b64_json", None)
            if not b64:
                item["save_error"] = "无可用图片数据"
                return
            data = base64.b64decode(b64)
            content_type = ""
        _, local_path, http_url = _persist_image(data, content_type, url or "")
        item["local_path"] = local_path
        item["http_url"] = http_url
    except Exception as exc:
        logger.warning("[AgentTool] 保存生成图片失败: %s", exc)
        item["save_error"] = str(exc)


async def _read_file_tool(
    path: str,
    offset: int = 0,
    limit: int = -1,
) -> str:
    """读取工作区内的文本文件，返回内容摘要（含长度限制与分页）。

    仅允许读取工作区根目录内的文件（见 _safe_resolve_workspace_path）。
    offset/limit 为行级分页：offset 从 0 开始，limit 为返回的最大行数，
    -1 表示读到文件结尾。单次返回字节数上限为 _WORKSPACE_READ_MAX_BYTES，
    超长时在结果中标记 truncated。
    """
    try:
        target = _safe_resolve_workspace_path(path, must_exist=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    if not target.is_file():
        return json.dumps({"error": f"{path} 不是文件"}, ensure_ascii=False)
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return json.dumps({"error": f"读取文件失败: {exc}"}, ensure_ascii=False)

    offset = max(0, int(offset or 0))
    limit = int(limit if limit is not None else -1)
    start = min(offset, len(lines))
    if limit >= 0:
        lines = lines[start:start + limit]
    else:
        lines = lines[start:]

    text = "".join(lines)
    truncated = False
    if len(text.encode("utf-8", errors="ignore")) > _WORKSPACE_READ_MAX_BYTES:
        text = text[:_WORKSPACE_READ_MAX_BYTES]
        truncated = True
    return json.dumps({
        "path": str(target),
        "start_line": start,
        "total_lines": len(lines),
        "truncated": truncated,
        "content": text,
    }, ensure_ascii=False)


async def _list_dir_tool(path: str = "") -> str:
    """列出工作区内目录的条目（名称、类型、大小），供模型感知工作区结构。"""
    raw_path = str(path or "").strip()
    if not raw_path:
        raw_path = "."
    try:
        target = _safe_resolve_workspace_path(raw_path, must_exist=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    if not target.is_dir():
        return json.dumps({"error": f"{path or '.'} 不是目录"}, ensure_ascii=False)
    try:
        entries = []
        for item in sorted(target.iterdir(), key=lambda p: p.name):
            if item.is_dir():
                kind = "dir"
                size = None
            elif item.is_file():
                kind = "file"
                try:
                    size = item.stat().st_size
                except OSError:
                    size = None
            else:
                kind = "other"
                size = None
            entries.append({"name": item.name, "type": kind, "size": size})
    except OSError as exc:
        return json.dumps({"error": f"读取目录失败: {exc}"}, ensure_ascii=False)
    return json.dumps({"path": str(target), "entries": entries}, ensure_ascii=False)


async def _glob_tool(pattern: str = "") -> str:
    """在工作区内按 glob 模式匹配文件路径（相对工作区根目录返回）。

    模式如 ``**/*.py``、``src/**/*.ts``。绝对模式或以 ``../`` 开头
    试图离开工作区的模式会被拒绝。
    """
    pattern = str(pattern or "").strip()
    if not pattern:
        return json.dumps({"error": "pattern 不能为空"}, ensure_ascii=False)
    if pattern.startswith("/") or pattern.startswith("../") or ".." in pattern.split("/"):
        return json.dumps({"error": "模式不允许离开工作区范围"}, ensure_ascii=False)
    root = _workspace_root()
    if root is None:
        return json.dumps({"error": "未配置 agent_workspace_root，工作区文件工具不可用"}, ensure_ascii=False)
    matches = []
    try:
        for p in root.glob(pattern):
            if p.is_file() or p.is_dir():
                matches.append(str(p.relative_to(root)))
    except (OSError, ValueError) as exc:
        return json.dumps({"error": f"匹配失败: {exc}"}, ensure_ascii=False)
    matches.sort()
    return json.dumps({"pattern": pattern, "matches": matches[:_WORKSPACE_GREP_MAX_RESULTS], "total": len(matches)}, ensure_ascii=False)


async def _grep_tool(
    pattern: str = "",
    path: str = "",
    case_sensitive: bool = False,
) -> str:
    """在工作区内按正则搜索文件内容，返回命中文件与行号。

    默认递归搜索整个工作区；path 指定时仅搜索该目录/文件（仍限工作区内）。
    结果条数上限为 _WORKSPACE_GREP_MAX_RESULTS。
    """
    pattern = str(pattern or "").strip()
    if not pattern:
        return json.dumps({"error": "pattern 不能为空"}, ensure_ascii=False)
    try:
        flags = 0 if case_sensitive else re.IGNORECASE
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return json.dumps({"error": f"正则表达式非法: {exc}"}, ensure_ascii=False)
    raw_path = str(path or "").strip()
    if not raw_path:
        raw_path = "."
    try:
        root = _safe_resolve_workspace_path(raw_path, must_exist=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    if not root.is_dir():
        return json.dumps({"error": f"{path or '.'} 不是目录"}, ensure_ascii=False)

    results = []
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            # 跳过隐藏目录与常见二进制/版本库目录，避免命中无意义内容
            rel = p.relative_to(root)
            parts = rel.parts
            if any(part.startswith(".") or part in ("node_modules", ".git", "__pycache__", "venv", ".venv", "build", "dist") for part in parts):
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                    for lineno, line in enumerate(fh, start=1):
                        if rx.search(line):
                            results.append({
                                "file": str(rel),
                                "line": lineno,
                                "content": line.rstrip("\n")[:_WORKSPACE_READ_MAX_BYTES],
                            })
                            break
            except OSError:
                continue
            if len(results) >= _WORKSPACE_GREP_MAX_RESULTS:
                break
    except OSError as exc:
        return json.dumps({"error": f"搜索失败: {exc}"}, ensure_ascii=False)
    return json.dumps({"pattern": pattern, "results": results, "total": len(results)}, ensure_ascii=False)


async def _file_info_tool(path: str) -> str:
    """返回工作区内文件或目录的元信息（类型、大小、修改时间）。"""
    try:
        target = _safe_resolve_workspace_path(path, must_exist=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    try:
        stat = target.stat()
        kind = "dir" if target.is_dir() else "file"
        return json.dumps({
            "path": str(target),
            "type": kind,
            "size": stat.st_size,
            "mtime": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }, ensure_ascii=False)
    except OSError as exc:
        return json.dumps({"error": f"读取文件信息失败: {exc}"}, ensure_ascii=False)


async def _write_file_tool(
    path: str,
    content: str,
    mode: str = "overwrite",
) -> str:
    """写文件：新建或覆盖工作区内的文本文件。仅 agent_write_tools_enabled=true 时可用。"""
    await _check_tool_enabled("agent_write_tools_enabled", "akm_write_file")
    mode = str(mode or "overwrite").strip()
    if mode not in ("overwrite", "append"):
        return json.dumps({"error": "mode 只能是 overwrite 或 append"}, ensure_ascii=False)
    try:
        target = _safe_resolve_workspace_path(path, must_exist=(mode == "append"))
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    if target.exists() and target.is_dir():
        return json.dumps({"error": f"{path} 是目录，不能写入"}, ensure_ascii=False)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a" if mode == "append" else "w", encoding="utf-8") as fh:
            fh.write(str(content or ""))
    except OSError as exc:
        return json.dumps({"error": f"写入文件失败: {exc}"}, ensure_ascii=False)
    return json.dumps({"ok": True, "path": str(target), "mode": mode, "bytes_written": len(str(content or "").encode("utf-8"))}, ensure_ascii=False)


async def _edit_file_tool(
    path: str,
    old_string: str,
    new_string: str = "",
    replace_all: bool = False,
) -> str:
    """编辑工作区内的文本文件：用新字符串替换旧字符串。仅 agent_write_tools_enabled=true 时可用。"""
    await _check_tool_enabled("agent_write_tools_enabled", "akm_edit_file")
    old_string = str(old_string or "")
    if not old_string:
        return json.dumps({"error": "old_string 不能为空"}, ensure_ascii=False)
    try:
        target = _safe_resolve_workspace_path(path, must_exist=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    if not target.is_file():
        return json.dumps({"error": f"{path} 不是文件"}, ensure_ascii=False)
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return json.dumps({"error": f"读取文件失败: {exc}"}, ensure_ascii=False)
    count = content.count(old_string)
    if count == 0:
        return json.dumps({"error": "old_string 未在文件中找到"}, ensure_ascii=False)
    if not replace_all:
        count = 1
    new_content = content.replace(old_string, new_string, count)
    try:
        target.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        return json.dumps({"error": f"写入文件失败: {exc}"}, ensure_ascii=False)
    return json.dumps({"ok": True, "path": str(target), "replaced": count}, ensure_ascii=False)


async def _make_dir_tool(path: str) -> str:
    """在工作区内创建目录（含父目录）。仅 agent_write_tools_enabled=true 时可用。"""
    await _check_tool_enabled("agent_write_tools_enabled", "akm_make_dir")
    try:
        target = _safe_resolve_workspace_path(path, must_exist=False)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return json.dumps({"error": f"创建目录失败: {exc}"}, ensure_ascii=False)
    return json.dumps({"ok": True, "path": str(target)}, ensure_ascii=False)


async def _delete_tool(path: str, recursive: bool = False) -> str:
    """删除工作区内的文件或（recursive=true 时）目录。仅 agent_write_tools_enabled=true 时可用。

    出于安全考虑，始终拒绝删除工作区根目录本身。
    """
    await _check_tool_enabled("agent_write_tools_enabled", "akm_delete_file")
    try:
        target = _safe_resolve_workspace_path(path, must_exist=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    root = _workspace_root()
    if target == root:
        return json.dumps({"error": "禁止删除工作区根目录"}, ensure_ascii=False)
    try:
        if target.is_dir():
            if not recursive:
                return json.dumps({"error": "删除目录需要 recursive=true"}, ensure_ascii=False)
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        return json.dumps({"error": f"删除失败: {exc}"}, ensure_ascii=False)
    return json.dumps({"ok": True, "path": str(target)}, ensure_ascii=False)


async def _run_shell_tool(command: str, timeout: int = 0) -> str:
    """在工作区根目录下执行 shell 命令，返回 stdout+stderr。

    仅 agent_run_shell_enabled=true 时可用。命令以工作区根目录为 cwd，
    用 subprocess 运行，默认超时 _WORKSPACE_SHELL_TIMEOUT_SEC 秒。
    出于安全考虑，调用方需自行确认命令内容与影响范围。
    """
    await _check_tool_enabled("agent_run_shell_enabled", "akm_run_shell")
    command = str(command or "").strip()
    if not command:
        return json.dumps({"error": "command 不能为空"}, ensure_ascii=False)
    root = _workspace_root()
    if root is None:
        return json.dumps({"error": "未配置 agent_workspace_root，工作区文件工具不可用"}, ensure_ascii=False)
    timeout = max(1, min(int(timeout or _WORKSPACE_SHELL_TIMEOUT_SEC), 300))
    loop = asyncio.get_running_loop()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return json.dumps({"error": f"命令执行超时（{timeout} 秒），已终止"}, ensure_ascii=False)
        text = output.decode("utf-8", errors="replace")
        truncated = False
        if len(text) > _WORKSPACE_READ_MAX_BYTES:
            text = text[:_WORKSPACE_READ_MAX_BYTES]
            truncated = True
        return json.dumps({
            "exit_code": proc.returncode,
            "truncated": truncated,
            "output": text,
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"执行命令失败: {exc}"}, ensure_ascii=False)


def build_builtin_tools(app: FastAPI) -> list[ToolDef]:
    """创建与当前服务实例绑定的只读调试工具。

    工具刻意只暴露排障所需的运行元数据。密钥明文、审计请求体、响应体和
    请求头都不会进入模型上下文，避免 Agent 调用意外扩大敏感数据暴露面。
    """

    def get_status() -> dict[str, Any]:
        """返回健康监护、审计队列和已加载插件的摘要状态。"""
        monitor = getattr(app.state, "health_monitor", None)
        audit_queue = getattr(app.state, "audit_log_queue", None)
        plugin_manager = getattr(app.state, "plugin_manager", None)
        plugins = getattr(plugin_manager, "plugins", {})
        return {
            "health": monitor.detail_payload() if monitor is not None else {},
            "audit_queue": {
                "size": audit_queue.qsize() if audit_queue is not None else 0,
                "maxsize": getattr(audit_queue, "maxsize", 0),
                "dropped_count": getattr(audit_queue, "dropped_count", 0),
                "failure_count": getattr(audit_queue, "failure_count", 0),
                "worker_alive": audit_queue.worker_alive() if audit_queue is not None else False,
            },
            "plugins": [
                {
                    "name": name,
                    "enabled": plugin.enabled,
                    "runtime_ready": plugin.runtime_ready,
                }
                for name, plugin in plugins.items()
            ],
        }

    def get_keys() -> list[dict[str, Any]]:
        """返回 Key 的非敏感连接与模型配置，绝不返回 API Key。"""
        return [
            {
                "alias": key.get("alias", ""),
                "provider": key.get("provider", ""),
                "models": key_model_list(key),
                "priority": key.get("priority", 0),
                "status": key.get("status", ""),
            }
            for key in list_keys()
        ]

    def get_time() -> dict[str, Any]:
        """返回服务器当前时间，含本地 ISO 时间、UTC 时间、UNIX 时间戳与时区。"""
        now = datetime.datetime.now().astimezone()
        return {
            "iso": now.isoformat(),
            "utc_iso": now.astimezone(datetime.timezone.utc).isoformat(),
            "unix": int(now.timestamp()),
            "timezone": str(now.tzinfo),
        }

    async def get_logs(
        limit: int = 20,
        status: str = "all",
        days: int = 1,
        key_alias: str = "",
    ) -> list[dict[str, Any]]:
        """返回近期审计元数据，不包含任意请求或响应内容。"""
        limit = max(1, min(int(limit), 50))
        days = max(0, min(int(days), 30))
        if status not in {"all", "success", "failed"}:
            raise ValueError("status 只能是 all、success 或 failed")
        logs = await list_logs_async(
            limit=limit,
            status=status,
            days=days,
            key_alias=str(key_alias or ""),
        )
        return [
            {
                "id": log.get("id"),
                "timestamp": log.get("timestamp", ""),
                "provider": log.get("provider", ""),
                "key_alias": log.get("key_alias", ""),
                "model": log.get("model", ""),
                "status_code": log.get("status_code", 0),
                "latency_ms": log.get("latency_ms", 0),
                "prompt_tokens": log.get("prompt_tokens", 0),
                "completion_tokens": log.get("completion_tokens", 0),
                "error": log.get("error", ""),
            }
            for log in logs
        ]

    async def tavily_search_tool(
        query: str,
        max_results: int = 5,
        search_depth: str = "basic",
    ) -> str:
        """通过 Tavily 远程 MCP 端点执行联网搜索，返回序列化结果。"""
        pool = getattr(app.state, "http_client", None)
        if pool is None or not getattr(pool, "is_route_pool", False):
            return json.dumps({"error": "HTTP 连接池未就绪"}, ensure_ascii=False)
        # 复用按路由隔离的连接池，遵循出站代理设置
        client = await pool.get_client(
            provider="tavily", key_alias="mcp", model="", api_path="mcp"
        )
        try:
            return await tavily_search(
                client,
                query=query,
                max_results=max_results,
                search_depth=search_depth,
            )
        except Exception as exc:
            logger.warning("[AgentTool] tavily_search 失败: %s", exc)
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    async def search_kb_tool(
        question: str,
        top_k: int = 5,
        embedding_model: str = "",
        reranker_model: str = "",
        workspace_root: str = "",
    ) -> str:
        """通过 markdown-kb 插件 HTTP 接口检索知识库，返回精选命中片段。

        以 HTTP 方式请求本机服务的 /api/markdown-kb/query 端点，与插件内部
        逻辑解耦。为控制模型上下文体积，每个命中只回传标题、文件名、相关度
        分数与截断后的正文摘要（前 500 字符），不会把完整 chunk 全量塞回。
        未指定 workspace_root 时检索全部文档（不受工作域过滤限制）。
        """
        question = str(question or "").strip()
        if not question:
            return json.dumps({"error": "question 不能为空"}, ensure_ascii=False)
        payload: dict[str, Any] = {"question": question}
        top_k = max(1, min(int(top_k or 5), 20))
        payload["top_k"] = top_k
        if str(embedding_model or "").strip():
            payload["embedding_model"] = str(embedding_model).strip()
        if str(reranker_model or "").strip():
            payload["reranker_model"] = str(reranker_model).strip()
        workspace_root = str(workspace_root or "").strip()
        if workspace_root:
            payload["workspace_root"] = workspace_root
        else:
            payload["ignore_workspace"] = True
        url = f"{_akm_api_base_url()}/api/markdown-kb/query"
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("[AgentTool] akm_search_kb 失败: %s", exc)
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        if not data.get("ok"):
            err = data.get("error") or data.get("message") or "知识库查询失败"
            return json.dumps({"error": err}, ensure_ascii=False)
        hits = data.get("hits") or []
        results = []
        for hit in hits:
            text = str(hit.get("chunk_text") or "").strip()
            results.append(
                {
                    "title": hit.get("title") or hit.get("file_name") or "",
                    "file_name": hit.get("file_name") or "",
                    "score": hit.get("score") or hit.get("vector_score") or 0,
                    "content": text[:500],
                }
            )
        return json.dumps({"results": results}, ensure_ascii=False)

    async def generate_image_tool(
        prompt: str,
        model: str = "",
        size: str = "",
        quality: str = "",
        n: int = 1,
    ) -> str:
        """调用上游图片生成接口生成图片，返回图片资源列表。

        复用连接池并走 forward_request，与 /v1/images/generations 端点共用
        同一套 key 选择与故障切换逻辑。模型未指定时取 image_supported_models
        首项，保证能匹配到可用 Key。为避免把体积巨大的 base64 数据塞进模型
        上下文，只回传 URL；生成成功后还会把图片下载保存到 agent_upload_dir
        （默认 ~/.akm/cache），并在结果中附带 local_path 与 http_url
        （http://127.0.0.1:{port}/agent-uploads/{filename}）指向该资源；
        保存失败时附带 save_error 说明原因。
        """
        pool = getattr(app.state, "http_client", None)
        if pool is None or not getattr(pool, "is_route_pool", False):
            return json.dumps({"error": "HTTP 连接池未就绪"}, ensure_ascii=False)
        cfg = load_config()
        if not str(model or "").strip():
            model = _image_default_model()
        body: dict[str, Any] = {"model": model, "prompt": prompt}
        if size:
            body["size"] = size
        if quality:
            body["quality"] = quality
        n = max(1, int(n or 1))
        if n > 1:
            body["n"] = n
        client = await pool.get_client(
            provider="", key_alias="", model=model, api_path="images/generations"
        )
        try:
            # 函数内导入避免 akm.proxy 与 agent_runtime 之间循环依赖
            from akm.proxy import forward_request

            result = await forward_request(
                body,
                client,
                api_path="images/generations",
                request_timeout=_image_request_timeout(),
            )
        except Exception as exc:
            logger.warning("[AgentTool] akm_generate_image 失败: %s", exc)
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        if result.get("error") or int(result.get("status_code", 0) or 0) >= 400:
            err = result.get("error") or f"HTTP {result.get('status_code')}"
            return json.dumps({"error": err}, ensure_ascii=False)
        try:
            data = json.loads(result.get("body") or "{}")
        except (TypeError, ValueError):
            return json.dumps({"error": "上游响应不是合法 JSON"}, ensure_ascii=False)
        images = []
        for index, item in enumerate(data.get("data") or []):
            entry: dict[str, Any] = {"index": index}
            url = item.get("url")
            if url:
                entry["url"] = url
            else:
                b64 = item.get("b64_json")
                entry["b64_json_hint"] = (
                    f"base64 数据，长度 {len(b64)}" if b64 else "无可用数据"
                )
                if b64:
                    entry["b64_json"] = b64
            await _save_generated_image(entry, client)
            images.append(entry)
        return json.dumps({"images": images}, ensure_ascii=False)

    async def edit_image_tool(
        prompt: str,
        image_path: str = "",
        image_base64: str = "",
        model: str = "",
        mask_path: str = "",
        mask_base64: str = "",
        size: str = "",
        quality: str = "",
        output_format: str = "",
        n: int = 1,
    ) -> str:
        """读取本地图片（或 base64 数据）并按提示词编辑，返回编辑后的图片资源列表。

        与 akm_generate_image 共用图片转发链路（forward_request + images/edits）。
        图片有两种来源：image_path 读取服务器本地文件，或 image_base64 直接传入
        base64 数据（兼容 data URL 前缀，适用于本地无文件的云端场景，模型可直接
        使用对话中 image_url 的 data URL）。handler 组装与 /v1/images/edits 一致的
        multipart 请求体。编辑结果同样会下载保存到 agent_upload_dir，并附带
        local_path 与 http_url 指向资源，保存失败时附带 save_error 说明原因。
        """
        pool = getattr(app.state, "http_client", None)
        if pool is None or not getattr(pool, "is_route_pool", False):
            return json.dumps({"error": "HTTP 连接池未就绪"}, ensure_ascii=False)
        if not str(model or "").strip():
            model = _image_default_model()
        try:
            image_file = _resolve_image(image_path, image_base64)
        except (OSError, ValueError) as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        fields: dict[str, str] = {"prompt": prompt, "model": model}
        if size:
            fields["size"] = size
        if quality:
            fields["quality"] = quality
        if output_format:
            fields["output_format"] = output_format
        n = max(1, int(n or 1))
        if n > 1:
            fields["n"] = str(n)
        files: dict[str, tuple[str, bytes, str]] = {"image": image_file}
        if str(mask_path or "").strip() or str(mask_base64 or "").strip():
            try:
                files["mask"] = _resolve_image(mask_path, mask_base64)
            except (OSError, ValueError) as exc:
                return json.dumps({"error": str(exc)}, ensure_ascii=False)
        body: dict[str, Any] = {
            "__akm_multipart__": True,
            "__akm_form_fields__": fields,
            "__akm_form_files__": files,
            "model": model,
        }
        client = await pool.get_client(
            provider="", key_alias="", model=model, api_path="images/edits"
        )
        try:
            # 函数内导入避免 akm.proxy 与 agent_runtime 之间循环依赖
            from akm.proxy import forward_request

            result = await forward_request(
                body,
                client,
                api_path="images/edits",
                request_timeout=_image_request_timeout(),
            )
        except Exception as exc:
            logger.warning("[AgentTool] akm_edit_image 失败: %s", exc)
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        if result.get("error") or int(result.get("status_code", 0) or 0) >= 400:
            err = result.get("error") or f"HTTP {result.get('status_code')}"
            return json.dumps({"error": err}, ensure_ascii=False)
        try:
            data = json.loads(result.get("body") or "{}")
        except (TypeError, ValueError):
            return json.dumps({"error": "上游响应不是合法 JSON"}, ensure_ascii=False)
        images = []
        for index, item in enumerate(data.get("data") or []):
            entry: dict[str, Any] = {"index": index}
            url = item.get("url")
            if url:
                entry["url"] = url
            else:
                b64 = item.get("b64_json")
                entry["b64_json_hint"] = (
                    f"base64 数据，长度 {len(b64)}" if b64 else "无可用数据"
                )
                if b64:
                    entry["b64_json"] = b64
            await _save_generated_image(entry, client)
            images.append(entry)
        return json.dumps({"images": images}, ensure_ascii=False)

    empty_object = {"type": "object", "properties": {}}
    return [
        ToolDef("akm_get_status", "读取 AKM 服务健康、审计队列和插件运行状态", empty_object, get_status),
        ToolDef("akm_list_keys", "列出 AKM 中已配置 Key 的非敏感状态与模型信息，不返回密钥", empty_object, get_keys),
        ToolDef("akm_get_time", "获取服务器当前时间，返回本地 ISO 时间、UTC 时间、UNIX 时间戳与时区", empty_object, get_time),
        ToolDef(
            "akm_list_logs",
            "查询近期 AKM 审计日志摘要，不返回请求体、响应体或请求头",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回条数，1 到 50，默认 20"},
                    "status": {"type": "string", "enum": ["all", "success", "failed"], "description": "状态筛选，默认 all"},
                    "days": {"type": "integer", "description": "最近自然日范围，0 表示不限制，默认 1"},
                    "key_alias": {"type": "string", "description": "按 Key 别名筛选，可选"},
                },
            },
            get_logs,
        ),
        ToolDef(
            "tavily_search",
            "通过 Tavily 实时联网搜索互联网信息，返回包含标题、链接和摘要的搜索结果。需要先在 config.json 中配置 tavily_api_key",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "max_results": {"type": "integer", "description": "返回结果数量，1 到 20，默认 5"},
                    "search_depth": {"type": "string", "enum": ["basic", "advanced"], "description": "搜索深度，默认 basic"},
                },
                "required": ["query"],
            },
            tavily_search_tool,
        ),
        ToolDef(
            "akm_search_kb",
            "通过 markdown-kb 知识库检索与问题最相关的文档片段，返回命中内容的标题、文件名、相关度分数与正文摘要。需要 markdown-kb 插件已启用且已学习文档",
            {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "检索问题"},
                    "top_k": {"type": "integer", "description": "返回命中条数，1 到 20，默认 5"},
                    "embedding_model": {"type": "string", "description": "向量模型，默认取插件配置"},
                    "reranker_model": {"type": "string", "description": "重排模型，默认取插件配置"},
                    "workspace_root": {"type": "string", "description": "工作目录绝对路径（可选；不传时检索全部文档）"},
                },
                "required": ["question"],
            },
            search_kb_tool,
        ),
        ToolDef(
            "akm_generate_image",
            "调用 AKM 配置的图片生成模型生成图片，返回图片资源列表。每项含 url，并附带保存到本地的 local_path 与可访问的 http_url（/agent-uploads/...），保存失败时含 save_error。需要配置 image_supported_models 对应的可用 API Key",
            {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "图片描述提示词"},
                    "model": {"type": "string", "description": "图片生成模型，默认取 image_supported_models 首项"},
                    "size": {"type": "string", "description": "图片尺寸，如 1024x1024，可选"},
                    "quality": {"type": "string", "description": "生成质量，如 standard 或 hd，可选"},
                    "n": {"type": "integer", "description": "生成张数，默认 1"},
                },
                "required": ["prompt"],
            },
            generate_image_tool,
        ),
        ToolDef(
            "akm_edit_image",
            "编辑图片（如重绘局部、扩展内容），返回编辑后的图片资源列表。每项含 url，并附带保存到本地的 local_path 与可访问的 http_url（/agent-uploads/...），保存失败时含 save_error。图片来源二选一：image_path 传服务器本地文件绝对路径；或 image_base64 传图片的 base64 数据（可直接使用对话中图片的 data:image/...;base64, 前缀数据，适合本地无文件的场景）。需要配置了对应模型的可用 API Key",
            {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "本地图片文件的绝对路径，与 image_base64 二选一"},
                    "image_base64": {"type": "string", "description": "图片 base64 数据（可带 data:image/...;base64, 前缀），与 image_path 二选一，优先级更高"},
                    "prompt": {"type": "string", "description": "编辑指令，描述期望的修改效果"},
                    "model": {"type": "string", "description": "图片编辑模型，默认取 image_supported_models 首项"},
                    "mask_path": {"type": "string", "description": "本地蒙版图片路径，用于限定重绘区域，可选"},
                    "mask_base64": {"type": "string", "description": "蒙版图片的 base64 数据（可带 data:... 前缀），与 mask_path 二选一，优先级更高"},
                    "size": {"type": "string", "description": "输出图片尺寸，如 1024x1024，可选"},
                    "quality": {"type": "string", "description": "生成质量，如 standard 或 hd，可选"},
                    "output_format": {"type": "string", "description": "输出格式，如 png 或 jpeg，可选"},
                    "n": {"type": "integer", "description": "生成张数，默认 1"},
                },
                "required": ["prompt"],
            },
            edit_image_tool,
        ),
    ]


def build_workspace_tools() -> list[ToolDef]:
    """创建工作区文件工具（读 + 写 + shell）。

    安全设计：
    - 读工具（read/list/glob/grep/info）始终注册，但目标必须位于
      agent_workspace_root 工作区内；未配置工作区时执行返回明确错误。
    - 写工具（write/edit/make_dir/delete）默认禁用，仅当 config.json
      设置 ``agent_write_tools_enabled=true`` 时才注册，模型不可见即不可调。
    - shell 工具默认禁用，仅当 ``agent_run_shell_enabled=true`` 时才注册，
      且以工作区根目录为 cwd 执行，独立开关避免写文件与执行命令同权。
    """
    tools: list[ToolDef] = [
        ToolDef(
            "akm_read_file",
            "读取工作区内文本文件的内容（带行级分页与长度限制）。仅能访问 agent_workspace_root 配置的工作区目录",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的文件路径（绝对路径或相对工作区根目录的路径）"},
                    "offset": {"type": "integer", "description": "起始行号（从 0 开始），默认 0"},
                    "limit": {"type": "integer", "description": "返回的最大行数，-1 表示读到结尾，默认 -1"},
                },
                "required": ["path"],
            },
            _read_file_tool,
        ),
        ToolDef(
            "akm_list_dir",
            "列出工作区内目录下的条目（名称、类型、大小），用于感知工作区结构",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的目录路径，留空表示工作区根目录"},
                },
            },
            _list_dir_tool,
        ),
        ToolDef(
            "akm_glob",
            "在工作区内按 glob 模式匹配文件或目录路径（相对工作区根目录返回），如 **/*.py",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 匹配模式，如 **/*.py"},
                },
                "required": ["pattern"],
            },
            _glob_tool,
        ),
        ToolDef(
            "akm_grep",
            "在工作区内按正则搜索文件内容，返回命中的文件、行号与行内容",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "要搜索的正则表达式"},
                    "path": {"type": "string", "description": "限定搜索的目录或文件（工作区内），留空递归搜索整个工作区"},
                    "case_sensitive": {"type": "boolean", "description": "是否区分大小写，默认 false"},
                },
                "required": ["pattern"],
            },
            _grep_tool,
        ),
        ToolDef(
            "akm_file_info",
            "返回工作区内文件或目录的元信息（类型、大小、修改时间）",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的文件或目录路径"},
                },
                "required": ["path"],
            },
            _file_info_tool,
        ),
    ]

    # 写工具与 shell 工具：默认禁用，仅在对应配置开关开启时注册
    if load_config().get("agent_write_tools_enabled"):
        tools.extend([
            ToolDef(
                "akm_write_file",
                "新建或覆盖工作区内的文本文件（mode=overwrite 覆盖 / append 追加）。仅 agent_write_tools_enabled=true 时可用",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "工作区内的文件路径（相对工作区根目录的路径）"},
                        "content": {"type": "string", "description": "要写入的文本内容"},
                        "mode": {"type": "string", "enum": ["overwrite", "append"], "description": "overwrite 覆盖（默认）或 append 追加"},
                    },
                    "required": ["path", "content"],
                },
                _write_file_tool,
            ),
            ToolDef(
                "akm_edit_file",
                "编辑工作区内的文本文件：将 old_string 替换为 new_string。仅 agent_write_tools_enabled=true 时可用",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "工作区内的文件路径"},
                        "old_string": {"type": "string", "description": "要被替换的原文片段"},
                        "new_string": {"type": "string", "description": "替换后的新文本，可留空表示删除"},
                        "replace_all": {"type": "boolean", "description": "是否替换所有匹配（默认只替换第一处）"},
                    },
                    "required": ["path", "old_string"],
                },
                _edit_file_tool,
            ),
            ToolDef(
                "akm_make_dir",
                "在工作区内创建目录（含所需父目录）。仅 agent_write_tools_enabled=true 时可用",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "工作区内的目录路径"},
                    },
                    "required": ["path"],
                },
                _make_dir_tool,
            ),
            ToolDef(
                "akm_delete_file",
                "删除工作区内的文件，或（recursive=true 时）目录。仅 agent_write_tools_enabled=true 时可用",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "工作区内的文件或目录路径"},
                        "recursive": {"type": "boolean", "description": "删除目录时需设为 true"},
                    },
                    "required": ["path"],
                },
                _delete_tool,
            ),
        ])
    if load_config().get("agent_run_shell_enabled"):
        tools.append(ToolDef(
            "akm_run_shell",
            "在工作区根目录下执行 shell 命令并返回 stdout+stderr。仅 agent_run_shell_enabled=true 时可用；命令有超时限制（默认 60 秒，最大 300 秒）",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "timeout": {"type": "integer", "description": "超时秒数，1-300，默认 60"},
                },
                "required": ["command"],
            },
            _run_shell_tool,
        ))
    return tools
