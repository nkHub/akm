"""Agent API 路由。"""

import base64
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse


router = APIRouter()


async def _append_file_messages(messages: list[dict], files: list) -> tuple[list[dict] | None, str]:
    """读取上传文件，并作为独立的 user 消息追加到对话末尾。

    图片（image/*）→ 转为 base64 data URL 的 image_url 内容块；
    其他文件 → 尝试以 UTF-8 读取为文本内容；解码失败则拒绝。

    Returns:
        (新 messages, "") 或 (None, 错误信息)
    """
    new_messages: list[dict] = list(messages)
    for f in files:
        filename = f.filename or "上传文件"
        content_type = f.content_type or ""
        data = await f.read()
        if content_type.startswith("image/"):
            b64 = base64.b64encode(data).decode("ascii")
            new_messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": f"用户上传了图片文件：{filename}"},
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
    body, error = await _parse_agent_body(request)
    if error is not None:
        return error

    messages = body["messages"]
    model = str(body.get("model", "") or "")
    tools = body.get("tools")
    instructions = str(body.get("instructions", "") or "")
    api_path = str(body.get("api_path", "chat/completions") or "chat/completions")
    stream = bool(body.get("stream", False))
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
    }
    if stream:
        return StreamingResponse(
            agent_loop.run_stream(messages, **options),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    result = await agent_loop.run(messages, **options)
    return JSONResponse(content=result.to_dict())
