"""Agent API 路由。"""

import base64
import json
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

import akm
from akm.config import load_config

logger = logging.getLogger(__name__)

router = APIRouter()

# 附件在解码、Base64 编码和消息注入时会同时占用多份内存；总量上限既保护
# 服务进程，也避免大文本或大图片直接撑爆后续模型上下文。
_AGENT_UPLOAD_MAX_FILES = 8
_AGENT_UPLOAD_MAX_BYTES = 20 * 1024 * 1024


def _render_default_instructions(text: str, workspace_root: str = "") -> str:
    """替换默认系统指令中的占位符（{...}）为运行时实际路径。

    占位符仅当文本中出现时才替换，未出现的保持原样：
    - {AKM_SOURCE_DIR}          → akm 包源码根目录（akm/__init__.py 所在目录的父目录）
    - {CURRENT_WORKING_DIRECTORY} → 本次请求的工作区根目录（未传时取当前进程目录）
    - {USER_AGENTS_MD_PATH}     → 用户全局 AGENTS.md（~/.config/opencode/AGENTS.md，存在才替换）
    - {USER_AGENTS_SKILLS_DIR}  → 用户 skills 目录（~/.agents/skills，存在才替换）
    - {USER_PI_NPM_DIR}         → pi 依赖目录（~/.pi/node_modules，存在才替换）
    不存在的路径（如用户未配置 skills）替换为空字符串，避免注入无效路径。
    """
    if not text:
        return text

    # akm 包源码根目录：akm/__init__.py 的上一级（即项目根）
    source_dir = str(Path(akm.__file__).resolve().parent.parent)

    # 当前工作目录：优先请求级 workspace_root，否则进程当前目录
    cwd = str(workspace_root or "").strip() or os.getcwd()

    # 用户环境路径探测：仅替换存在的路径，避免注入误导性路径
    home = Path.home()
    candidates = {
        "{USER_AGENTS_MD_PATH}": home / ".config/opencode/AGENTS.md",
        "{USER_AGENTS_SKILLS_DIR}": home / ".agents/skills",
        "{USER_PI_NPM_DIR}": home / ".pi/node_modules",
    }

    out = text
    out = out.replace("{AKM_SOURCE_DIR}", source_dir)
    out = out.replace("{CURRENT_WORKING_DIRECTORY}", cwd)
    for placeholder, path in candidates.items():
        out = out.replace(placeholder, str(path) if path.exists() else "")
    return out


async def _check_agent_auth(request: Request) -> JSONResponse | None:
    """校验 /v1/agent 的鉴权 token 与危险工具启用条件。

    只读工具场景允许不配置 token；一旦启用写文件、shell 或 git 工具，
    必须配置 token。配置后要求请求携带 ``Authorization: Bearer <token>``
    或 ``X-Agent-Token`` 头，不匹配返回 401，避免危险工具无鉴权暴露。
    """
    config = load_config()
    token = str(config.get("agent_api_token") or "").strip()
    dangerous_tools_enabled = any(
        config.get(flag) is True
        for flag in (
            "agent_write_tools_enabled",
            "agent_run_shell_enabled",
            "agent_git_enabled",
        )
    )
    if dangerous_tools_enabled and not token:
        return JSONResponse(
            status_code=503,
            content={"detail": "启用 Agent 写入、shell 或 git 工具前必须配置 agent_api_token"},
        )
    if not token:
        return None
    auth = request.headers.get("authorization", "")
    provided = ""
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    if not provided:
        provided = str(request.headers.get("x-agent-token") or "").strip()
    if provided != token:
        return JSONResponse(status_code=401, content={"detail": "未授权：Agent token 缺失或不匹配"})
    return None


def _save_uploaded_image(data: bytes, content_type: str) -> str:
    """把上传的图片落盘到配置的保存目录，返回可被工具读取的绝对路径。

    上传的图片会先转成 base64 的 image_url 进入对话，同时落盘一份到
    config.json 的 agent_upload_dir 配置的目录（默认 ~/.akm/cache），供
    akm_edit_image 等按路径读取的工具使用。文件名使用随机 UUID 防止覆盖
    同名文件。
    """
    raw = str(load_config().get("agent_upload_dir") or "~/.akm/cache").strip()
    upload_dir = Path(raw).expanduser()
    os.makedirs(upload_dir, mode=0o700, exist_ok=True)
    ext = mimetypes.guess_extension(content_type or "") or ".bin"
    if not ext.startswith("."):
        ext = f".{ext}"
    path = upload_dir / f"{uuid.uuid4().hex}{ext}"
    with open(path, "wb") as fh:
        fh.write(data)
    return str(path)


async def _append_file_messages(messages: list[dict], files: list) -> tuple[list[dict] | None, str]:
    """读取上传文件，并作为独立的 user 消息追加到对话末尾。

    图片（image/*）→ 转为 base64 data URL 的 image_url 内容块，并把图片
    落盘到临时目录，在文本提示中给出绝对路径，方便模型调用 akm_edit_image
    编辑该图片；其他文件 → 尝试以 UTF-8 读取为文本内容；解码失败则拒绝。

    Returns:
        (新 messages, "") 或 (None, 错误信息)
    """
    new_messages: list[dict] = list(messages)
    if len(files) > _AGENT_UPLOAD_MAX_FILES:
        return None, f"一次最多上传 {_AGENT_UPLOAD_MAX_FILES} 个文件"
    total_size = 0
    for f in files:
        filename = f.filename or "上传文件"
        content_type = f.content_type or ""
        # 单次只多读一个字节来判定超限，不能先完整读取攻击者声明的大文件。
        remaining = _AGENT_UPLOAD_MAX_BYTES - total_size
        data = await f.read(remaining + 1)
        if len(data) > remaining:
            return None, f"上传文件总大小不能超过 {_AGENT_UPLOAD_MAX_BYTES // (1024 * 1024)}MB"
        total_size += len(data)
        if content_type.startswith("image/"):
            b64 = base64.b64encode(data).decode("ascii")
            saved_path = ""
            try:
                saved_path = _save_uploaded_image(data, content_type)
            except OSError as exc:
                # 落盘失败不影响图片进入上下文，只是不提供编辑路径
                logger.warning("[Agent] 保存上传图片失败: %s", exc)
            text = f"用户上传了图片文件：{filename}"
            if saved_path:
                text += (
                    f"\n图片已保存至：{saved_path}，如需编辑该图片，"
                    f"可调用 akm_edit_image 工具并传入 image_path={saved_path}。"
                )
            new_messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64}"}},
                ],
            })
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None, f"不支持的文件类型: {filename}（{content_type or '未知类型'}）"
        new_messages.append({
            "role": "user",
            "content": f"用户上传了文件：{filename}\n\n{text}",
        })
    return new_messages, ""


async def _parse_agent_body(request: Request) -> tuple[dict[str, Any], JSONResponse | None]:
    """解析 /v1/agent 请求体，兼容纯 JSON 与 multipart/form-data 两种方式。

    multipart 方式：messages 为 JSON 字符串表单字段，files 为文件字段
    （支持多个）。文件读取后作为独立 user 消息追加到对话末尾。
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "multipart/form-data":
        try:
            body = await request.json()
        except Exception:
            return {}, JSONResponse(status_code=400, content={"detail": "请求体不是合法的 JSON"})
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not body["messages"]:
            return {}, JSONResponse(status_code=400, content={"detail": "缺少 messages 参数"})
        return body, None

    form = await request.form()
    messages_raw = form.get("messages")
    if not messages_raw:
        return {}, JSONResponse(status_code=400, content={"detail": "缺少 messages 参数"})
    try:
        messages = json.loads(str(messages_raw))
    except (TypeError, json.JSONDecodeError):
        return {}, JSONResponse(status_code=400, content={"detail": "messages 必须是合法的 JSON 字符串"})
    if not isinstance(messages, list) or not messages:
        return {}, JSONResponse(status_code=400, content={"detail": "缺少 messages 参数"})

    files = form.getlist("files")
    if files:
        new_messages, err = await _append_file_messages(messages, files)
        if err:
            return {}, JSONResponse(status_code=400, content={"detail": err})
        messages = new_messages

    tools = None
    tools_raw = form.get("tools")
    if tools_raw:
        try:
            tools = json.loads(str(tools_raw))
        except (TypeError, json.JSONDecodeError):
            return {}, JSONResponse(status_code=400, content={"detail": "tools 必须是合法的 JSON 字符串"})

    stream_raw = str(form.get("stream", "false") or "false").lower()
    body = {
        "model": str(form.get("model", "") or ""),
        "messages": messages,
        "tools": tools if isinstance(tools, list) else None,
        "instructions": str(form.get("instructions", "") or ""),
        "api_path": str(form.get("api_path", "chat/completions") or "chat/completions"),
        "stream": stream_raw in ("1", "true", "yes", "on"),
        "max_turns": form.get("max_turns"),
        "workspace_root": str(form.get("workspace_root", "") or ""),
    }
    return body, None


@router.post("/v1/agent")
@router.post("/agent")
async def agent(request: Request):
    """接收多轮对话请求，并由 Agent Loop 编排工具调用。

    支持两种请求方式：
    1. 纯 JSON：messages 等字段直接放在请求体中。
    2. multipart/form-data：messages 为 JSON 字符串表单字段，files 为
       文件字段（支持多个）；上传的文件会被读取并作为独立的 user 消息
        追加到对话末尾（图片转 image_url、其他文件转文本）。
    """
    auth_error = await _check_agent_auth(request)
    if auth_error is not None:
        return auth_error

    body, error = await _parse_agent_body(request)
    if error is not None:
        return error

    messages = body["messages"]
    model = str(body.get("model", "") or "")
    tools = body.get("tools")
    instructions = str(body.get("instructions", "") or "")
    if not instructions:
        # 客户端未提供指令时回填 config.json 的默认系统指令（agent_default_instructions）
        instructions = str(load_config().get("agent_default_instructions") or "").strip()
    api_path = str(body.get("api_path", "chat/completions") or "chat/completions")
    stream = bool(body.get("stream", False))
    workspace_root = str(body.get("workspace_root", "") or "")
    # 回填的默认指令包含 {AKM_SOURCE_DIR} 等占位符，注入前替换为运行时实际路径
    instructions = _render_default_instructions(instructions, workspace_root)
    try:
        max_turns = int(body.get("max_turns", 0) or 0)
    except (TypeError, ValueError):
        max_turns = 0

    agent_loop = getattr(request.app.state, "agent_loop", None)
    if agent_loop is None:
        return JSONResponse(status_code=503, content={"detail": "Agent Loop 尚未初始化"})

    options = {
        "model": model,
        "tools": tools if isinstance(tools, list) else None,
        "instructions": instructions,
        "max_turns": max_turns,
        "api_path": api_path,
        "workspace_root": workspace_root,
    }
    if stream:
        return StreamingResponse(
            agent_loop.run_stream(messages, **options),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    result = await agent_loop.run(messages, **options)
    return JSONResponse(content=result.to_dict())
