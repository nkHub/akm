#!/usr/bin/env python3
"""最小可用的图片 MCP server。

基于 JSON-RPC over stdio 实现最基础的 MCP 方法：
- initialize
- tools/list
- tools/call

工具能力只暴露两项：
- generate_image
- edit_image

内部不重复实现图片逻辑，而是直接复用 akm.cli 中已经封装好的本地代理调用：
- _generate_image_via_local_service
- _edit_image_via_local_service

这样可以确保 CLI、MCP、未来脚本调用都走同一条图片请求链路。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
from typing import Any


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from akm.cli import _edit_image_via_local_service, _generate_image_via_local_service, _read_upload_file


SERVER_INFO = {
    "name": "akm-image",
    "version": "0.1.0",
}


TOOLS = [
    {
        "name": "generate_image",
        "description": "通过 AKM 本地代理生成图片，返回上游 JSON 结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "图片生成提示词"},
                "model": {"type": "string", "description": "图片模型，如 gpt-image-2"},
                "size": {"type": "string", "description": "图片尺寸，如 1024x1024"},
                "quality": {"type": "string", "description": "图片质量，如 low/medium/high"},
                "background": {"type": "string", "description": "背景模式，如 transparent"},
                "output_format": {"type": "string", "description": "输出格式，如 png/webp"},
                "n": {"type": "integer", "description": "生成张数"},
                "user": {"type": "string", "description": "透传给上游的 user 字段"},
                "timeout": {"type": "number", "description": "超时时间（秒）", "default": 120},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_image",
        "description": "通过 AKM 本地代理编辑图片，自动上传本地文件并返回上游 JSON 结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "待编辑图片的本地路径"},
                "prompt": {"type": "string", "description": "图片编辑提示词"},
                "mask": {"type": "string", "description": "可选的 mask 图片本地路径"},
                "model": {"type": "string", "description": "图片模型，如 gpt-image-2"},
                "size": {"type": "string", "description": "图片尺寸，如 1024x1024"},
                "quality": {"type": "string", "description": "图片质量，如 low/medium/high"},
                "background": {"type": "string", "description": "背景模式，如 transparent"},
                "output_format": {"type": "string", "description": "输出格式，如 png/webp"},
                "n": {"type": "integer", "description": "生成张数"},
                "user": {"type": "string", "description": "透传给上游的 user 字段"},
                "timeout": {"type": "number", "description": "超时时间（秒）", "default": 120},
            },
            "required": ["image_path", "prompt"],
            "additionalProperties": False,
        },
    },
]


def _success_response(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _read_message() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    return json.loads(line)


def _write_message(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _build_image_content_blocks(result: dict) -> list[dict]:
    """把图片接口返回值规范化为 MCP 原生 image 内容块。

    设计目标：
    1. 若上游返回 `b64_json`，直接转成 `type=image`；
    2. 若上游返回 `data:image/...;base64,...`，拆出 mimeType 与 base64 数据；
    3. 若无法识别为原生图片，则回退为 text，避免让调用方拿到空结果；
    4. 同时附一段简短文本元信息，便于纯文本型 MCP 客户端理解返回内容。
    """
    data_items = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data_items, list):
        return [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False),
            }
        ]

    content: list[dict] = []
    image_count = 0
    data_url_pattern = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", re.DOTALL)

    for item in data_items:
        if not isinstance(item, dict):
            continue

        b64_json = item.get("b64_json")
        if isinstance(b64_json, str) and b64_json.strip():
            content.append(
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "data": b64_json.strip(),
                }
            )
            image_count += 1
            continue

        url = item.get("url")
        if isinstance(url, str):
            match = data_url_pattern.match(url.strip())
            if match:
                mime_type, encoded_data = match.groups()
                content.append(
                    {
                        "type": "image",
                        "mimeType": mime_type,
                        "data": encoded_data.strip(),
                    }
                )
                image_count += 1
                continue

    if image_count:
        content.append(
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "created": result.get("created"),
                        "image_count": image_count,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        return content

    return [
        {
            "type": "text",
            "text": json.dumps(result, ensure_ascii=False),
        }
    ]


async def _handle_generate_image(arguments: dict) -> dict:
    payload = {"prompt": arguments["prompt"]}
    for key in ("model", "size", "quality", "background", "output_format", "n", "user"):
        value = arguments.get(key)
        if value is not None and value != "":
            payload[key] = value
    timeout = float(arguments.get("timeout") or 120.0)
    result = await _generate_image_via_local_service(payload, timeout=timeout)
    return {"content": _build_image_content_blocks(result)}


async def _handle_edit_image(arguments: dict) -> dict:
    form_data = {"prompt": arguments["prompt"]}
    for key in ("model", "size", "quality", "background", "output_format", "user"):
        value = arguments.get(key)
        if value is not None and value != "":
            form_data[key] = value
    if arguments.get("n") is not None:
        form_data["n"] = str(arguments["n"])

    file_specs = [("image", _read_upload_file(arguments["image_path"]))]
    if arguments.get("mask"):
        file_specs.append(("mask", _read_upload_file(arguments["mask"])))
    timeout = float(arguments.get("timeout") or 120.0)
    result = await _edit_image_via_local_service(form_data, file_specs, timeout=timeout)
    return {"content": _build_image_content_blocks(result)}


async def _dispatch(method: str, params: dict) -> dict:
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": SERVER_INFO,
            "capabilities": {
                "tools": {},
            },
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if name == "generate_image":
            return await _handle_generate_image(arguments)
        if name == "edit_image":
            return await _handle_edit_image(arguments)
        raise ValueError(f"未知工具: {name}")
    if method == "notifications/initialized":
        return {}
    raise ValueError(f"不支持的方法: {method}")


def main() -> int:
    while True:
        try:
            message = _read_message()
        except json.JSONDecodeError as exc:
            _write_message(_error_response(None, -32700, f"JSON 解析失败: {exc}"))
            continue
        if message is None:
            break

        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") or {}

        try:
            result = asyncio.run(_dispatch(method, params))
        except Exception as exc:
            if request_id is not None:
                _write_message(_error_response(request_id, -32000, str(exc)))
            continue

        if request_id is not None:
            _write_message(_success_response(request_id, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
