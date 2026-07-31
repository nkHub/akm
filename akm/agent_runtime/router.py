"""Agent API 路由。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse


router = APIRouter()


@router.post("/v1/agent")
@router.post("/agent")
async def agent(request: Request):
    """接收多轮对话请求，并由 Agent Loop 编排工具调用。"""
    body = await request.json()
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return JSONResponse(status_code=400, content={"detail": "缺少 messages 参数"})

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
