"""AG-UI 渲染能力的 schema 加载与工具定义生成。

设计口径（``render_schema.json`` 为唯一真源）：

- 工具名、工具描述、15 种块类型的字段都写在 ``render_schema.json`` 里；
  服务端据此生成 ``akm_render`` 的工具定义，前端 ``AGUIRenderer`` 按同一份清单渲染；
- 是否开启由配置 ``agent_render_enabled``（默认 false）决定，开启后才注册工具；
- 参数校验**不在本模块重复实现**：仍然交给 ``layout_schema.validate_layout``
  （15 种块白名单 + 逐字段归一化 + 夹紧 + 空壳块丢弃），避免出现两套真源。

为什么工具参数用「宽松 schema + 详细描述」而不是完整 JSON Schema：
模型对 ``oneOf`` 15 个分支的遵循度明显低于「一个数组 + 字段清单文字说明」，
而真正的约束由服务端校验兜底。所以这里只声明 ``blocks`` 是对象数组 + 上限。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: schema 文件与模块同目录，随包一起分发
_SCHEMA_PATH = Path(__file__).with_name("render_schema.json")

#: schema 读取失败时使用的兜底值（保证服务不会因为文件缺失而启动失败）
_FALLBACK_TOOL_NAME = "akm_render"
_FALLBACK_MAX_BLOCKS = 5


@lru_cache(maxsize=1)
def load_render_schema() -> dict[str, Any]:
    """读取并缓存 ``render_schema.json``；失败返回最小可用结构并记录告警。"""
    try:
        with _SCHEMA_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
            raise ValueError("render_schema.json 结构不符合预期")
        return data
    except Exception as exc:  # noqa: BLE001 - 配置类文件缺失不应阻断服务启动
        logger.warning("[render_schema] 读取失败，使用兜底定义: %s", exc)
        return {
            "version": "1.0",
            "tool": {"name": _FALLBACK_TOOL_NAME, "description": "渲染结构化卡片", "maxBlocks": _FALLBACK_MAX_BLOCKS},
            "blocks": [],
            "rules": [],
        }


def tool_name() -> str:
    """渲染工具名（前端据此识别工具段并原位替换成卡片）。"""
    tool = load_render_schema().get("tool") or {}
    return str(tool.get("name") or _FALLBACK_TOOL_NAME)


def max_blocks() -> int:
    """单次调用允许的最大块数（与 schema 里声明的保持一致）。"""
    tool = load_render_schema().get("tool") or {}
    try:
        return max(1, int(tool.get("maxBlocks") or _FALLBACK_MAX_BLOCKS))
    except (TypeError, ValueError):
        return _FALLBACK_MAX_BLOCKS


def build_tool_description() -> str:
    """把 schema 里的块清单渲染成工具描述：模型只看这一段就能照着填。"""
    schema = load_render_schema()
    tool = schema.get("tool") or {}
    lines: list[str] = [str(tool.get("description") or "渲染结构化卡片")]
    blocks = schema.get("blocks") or []
    if blocks:
        lines.append("")
        lines.append("blocks 里每个元素必须是下面 %d 种之一（字段名严格按此）：" % len(blocks))
        for block in blocks:
            block_type = str(block.get("type") or "")
            when = str(block.get("when") or "")
            fields = block.get("fields") or {}
            field_text = "，".join(f"{key}: {value}" for key, value in fields.items())
            lines.append(f"- {block_type}（{when}）：{field_text}")
    rules = schema.get("rules") or []
    if rules:
        lines.append("")
        lines.append("硬性规则：")
        for index, rule in enumerate(rules, start=1):
            lines.append(f"{index}. {rule}")
    return "\n".join(lines)


def build_tool_parameters() -> dict[str, Any]:
    """渲染工具的 OpenAI function 参数定义（宽松 schema，真正约束靠服务端校验）。"""
    return {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "卡片标题，一句话概括（可选，<= 40 字）",
            },
            "summary": {
                "type": "string",
                "description": "一句话结论（可选，几十字；先输出它能让用户立刻看到要点）",
            },
            "blocks": {
                "type": "array",
                "description": "要渲染的块数组，按展示顺序排列；每个元素是一个块对象，type 必须是允许的 15 种之一",
                "items": {"type": "object"},
                "maxItems": max_blocks(),
            },
        },
        "required": ["blocks"],
    }


def build_layout_payload(
    *,
    title: str = "",
    summary: str = "",
    blocks: Any = None,
    intent: str = "explanation",
) -> dict[str, Any]:
    """把工具入参组装成 ``validate_layout`` 需要的布局载荷。

    字段名与 ``layout_schema`` 完全一致（``version`` / ``intent`` / ``title`` / ``blocks``）；
    ``summary`` 不进 payload（白名单不认它），由 akm_render 工具单独回传前端。
    """
    payload: dict[str, Any] = {
        "version": str(load_render_schema().get("version") or "1.0"),
        "intent": intent,
        "blocks": blocks if isinstance(blocks, list) else [],
    }
    if title:
        payload["title"] = title
    # 注意：不把 summary 塞进 payload —— validate_layout 只认白名单字段，未知字段会被丢弃
    # 或触发结构告警。summary 由调用方（akm_render 工具）单独回传给前端。
    return payload
