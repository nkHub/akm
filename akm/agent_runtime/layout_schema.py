"""AG-UI 布局协议的校验与夹紧（服务端权威）。

协议来源：同级设计项目 `AI Chat Window Design` 的 `src/components/agui/types.ts`
（前端渲染器 `AGUIRenderer.tsx` 直接消费这份结构）。本模块是**唯一**的合法性
判定处：模型产出的 JSON 必须先过这里，才能下发给前端。

设计原则：
1. **白名单 + 逐字段清洗**：只保留认识的字段，脏字段一律丢弃，不把模型输出原样
   透传给前端（避免前端要防御任意结构）。
2. **夹紧而非报错**：数量/长度超限就截断（而不是整份丢弃）；但 `version` 不认识、
   `blocks` 不是数组、块 `type` 不在白名单 → 直接返回 None（整份放弃，前端回落
   markdown，见「兼容约定」条目）。
3. 纯函数、无 IO，便于单测。
"""

from __future__ import annotations

from typing import Any

#: 协议版本。前端 `AGUIVersion` 必须与此一致；不匹配即整份放弃（不做部分解析）。
LAYOUT_VERSION = "1.0"

#: 允许的块类型（顺序无关，与前端 `AGUIBlock` 联合类型一一对应）。
BLOCK_TYPES: frozenset[str] = frozenset({
    "text", "heading", "code", "table", "stats", "cards", "alert",
    "comparison", "timeline", "list", "separator", "chart", "progress",
    "kanban", "map",
})

#: 各项硬上限。取值原则：够用即可，宁可截断也不要让单条消息膨胀。
MAX_TEXT_CHARS = 4000        # 单个文本类字段
MAX_ROW_COUNT = 100          # 表格行 / 对比行
MAX_COLUMN_COUNT = 12        # 表格列 / 看板列
MAX_ITEM_COUNT = 24          # stats / cards / list / timeline / progress 条目
MAX_CHART_POINTS = 120       # 图表数据点
MAX_SERIES = 8               # 图表系列数
MAX_MAP_POINTS = 60          # 地图数据点
MAX_SUB_ITEMS = 8            # 列表子项
MAX_TITLE_CHARS = 200
MAX_INTENT_CHARS = 40


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """把任意值转成有界字符串（None → 空串，换行保留，超长截断）。"""
    if value is None:
        return ""
    raw = value if isinstance(value, str) else str(value)
    return raw[:limit]


def _opt_text(value: Any, limit: int = MAX_TEXT_CHARS) -> str | None:
    """可选的文本字段：空串归一为 None，便于前端判空。"""
    text = _text(value, limit)
    return text or None


def _one_of(value: Any, allowed: tuple[str, ...], default: str) -> str:
    """枚举收敛：不在允许集合内就回落到默认值。"""
    text = str(value or "").strip()
    return text if text in allowed else default


def _scalar_list(value: Any, limit: int) -> list[Any]:
    """标量数组（对比表的单元格）：只保留 str/number/bool，最多 limit 项。"""
    if not isinstance(value, list):
        return []
    out: list[Any] = []
    for item in value[:limit]:
        if isinstance(item, bool) or isinstance(item, (int, float)):
            out.append(item)
        elif isinstance(item, str):
            out.append(item[:MAX_TEXT_CHARS])
        else:
            out.append("")
    return out


def _normalize_text_block(block: dict) -> dict:
    return {
        "type": "text",
        "content": _text(block.get("content")),
        "size": _one_of(block.get("size"), ("sm", "base", "lg"), "base"),
        "muted": bool(block.get("muted", False)),
    }


def _normalize_heading_block(block: dict) -> dict:
    try:
        rank = int(block.get("rank", 2))
    except (TypeError, ValueError):
        rank = 2
    return {
        "type": "heading",
        "content": _text(block.get("content"), MAX_TITLE_CHARS),
        "rank": rank if rank in (1, 2, 3) else 2,
    }


def _normalize_code_block(block: dict) -> dict:
    highlight = block.get("highlight")
    lines: list[int] = []
    if isinstance(highlight, list):
        for item in highlight[:60]:
            try:
                lines.append(int(item))
            except (TypeError, ValueError):
                continue
    return {
        "type": "code",
        "code": _text(block.get("code")),
        "lang": _text(block.get("lang"), 32) or "text",
        "title": _opt_text(block.get("title"), MAX_TITLE_CHARS),
        "highlight": lines,
    }


def _normalize_table_block(block: dict) -> dict:
    columns: list[dict] = []
    raw_columns = block.get("columns")
    if isinstance(raw_columns, list):
        for column in raw_columns[:MAX_COLUMN_COUNT]:
            if not isinstance(column, dict):
                continue
            key = _text(column.get("key"), 64)
            if not key:
                continue
            columns.append({
                "key": key,
                "label": _text(column.get("label"), MAX_TITLE_CHARS) or key,
                "align": _one_of(column.get("align"), ("left", "center", "right"), "left"),
                "mono": bool(column.get("mono", False)),
                "badge": bool(column.get("badge", False)),
            })
    rows: list[dict] = []
    raw_rows = block.get("rows")
    if isinstance(raw_rows, list) and columns:
        keys = [column["key"] for column in columns]
        for row in raw_rows[:MAX_ROW_COUNT]:
            if not isinstance(row, dict):
                continue
            rows.append({key: _scalar_or_text(row.get(key)) for key in keys})
    return {
        "type": "table",
        "columns": columns,
        "rows": rows,
        "caption": _opt_text(block.get("caption"), MAX_TITLE_CHARS),
    }


def _scalar_or_text(value: Any) -> str | int | float:
    """表格单元格：只允许标量，其余转字符串。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return value
    return _text(value, 500)


def _normalize_stats_block(block: dict) -> dict:
    items: list[dict] = []
    raw = block.get("items")
    if isinstance(raw, list):
        for item in raw[:MAX_ITEM_COUNT]:
            if not isinstance(item, dict):
                continue
            entry: dict[str, Any] = {
                "label": _text(item.get("label"), MAX_TITLE_CHARS),
                "value": _scalar_or_text(item.get("value")),
                "color": _one_of(
                    item.get("color"),
                    ("default", "green", "red", "blue", "amber", "violet"),
                    "default",
                ),
            }
            delta = item.get("delta")
            if isinstance(delta, dict) and delta.get("value") not in (None, ""):
                entry["delta"] = {
                    "value": _text(delta.get("value"), 32),
                    "trend": _one_of(delta.get("trend"), ("up", "down", "neutral"), "neutral"),
                }
            items.append(entry)
    columns = block.get("columns")
    try:
        column_count = int(columns) if columns is not None else 3
    except (TypeError, ValueError):
        column_count = 3
    return {
        "type": "stats",
        "items": items,
        "columns": column_count if column_count in (2, 3, 4) else 3,
    }


def _normalize_cards_block(block: dict) -> dict:
    items: list[dict] = []
    raw = block.get("items")
    if isinstance(raw, list):
        for item in raw[:MAX_ITEM_COUNT]:
            if not isinstance(item, dict):
                continue
            tags = item.get("tags")
            items.append({
                "title": _text(item.get("title"), MAX_TITLE_CHARS),
                "description": _opt_text(item.get("description"), 800),
                "badge": _opt_text(item.get("badge"), 40),
                "badgeColor": _one_of(
                    item.get("badgeColor"),
                    ("default", "blue", "green", "red", "amber", "violet"),
                    "default",
                ),
                "meta": _opt_text(item.get("meta"), 120),
                "tags": [_text(tag, 40) for tag in tags[:MAX_SUB_ITEMS]] if isinstance(tags, list) else [],
            })
    columns = block.get("columns")
    try:
        column_count = int(columns) if columns is not None else 3
    except (TypeError, ValueError):
        column_count = 3
    return {
        "type": "cards",
        "items": items,
        "columns": column_count if column_count in (2, 3, 4) else 3,
    }


def _normalize_alert_block(block: dict) -> dict:
    return {
        "type": "alert",
        "severity": _one_of(block.get("severity"), ("info", "warning", "error", "success"), "info"),
        "title": _opt_text(block.get("title"), MAX_TITLE_CHARS),
        "message": _text(block.get("message")),
    }


def _normalize_comparison_block(block: dict) -> dict:
    headers = block.get("headers")
    normalized_headers = (
        [_text(header, MAX_TITLE_CHARS) for header in headers[:MAX_COLUMN_COUNT]]
        if isinstance(headers, list) else []
    )
    rows: list[dict] = []
    raw = block.get("rows")
    if isinstance(raw, list):
        for row in raw[:MAX_ROW_COUNT]:
            if not isinstance(row, dict):
                continue
            rows.append({
                "label": _text(row.get("label"), MAX_TITLE_CHARS),
                "values": _scalar_list(row.get("values"), MAX_COLUMN_COUNT),
            })
    return {
        "type": "comparison",
        "headers": normalized_headers,
        "rows": rows,
        "caption": _opt_text(block.get("caption"), MAX_TITLE_CHARS),
    }


def _normalize_timeline_block(block: dict) -> dict:
    events: list[dict] = []
    raw = block.get("events")
    if isinstance(raw, list):
        for event in raw[:MAX_ITEM_COUNT]:
            if not isinstance(event, dict):
                continue
            events.append({
                "time": _text(event.get("time"), 64),
                "title": _text(event.get("title"), MAX_TITLE_CHARS),
                "description": _opt_text(event.get("description"), 800),
                "status": _one_of(event.get("status"), ("done", "active", "pending"), "pending"),
            })
    return {"type": "timeline", "events": events}


def _normalize_list_block(block: dict) -> dict:
    items: list[dict] = []
    raw = block.get("items")
    if isinstance(raw, list):
        for item in raw[:MAX_ITEM_COUNT]:
            if isinstance(item, str):
                items.append({"content": _text(item, 800), "done": False, "sub": []})
                continue
            if not isinstance(item, dict):
                continue
            sub = item.get("sub")
            items.append({
                "content": _text(item.get("content"), 800),
                "done": bool(item.get("done", False)),
                "sub": [_text(line, 400) for line in sub[:MAX_SUB_ITEMS]] if isinstance(sub, list) else [],
            })
    return {
        "type": "list",
        "style": _one_of(block.get("style"), ("bullet", "numbered", "check"), "bullet"),
        "items": items,
    }


def _normalize_separator_block(block: dict) -> dict:
    return {"type": "separator", "label": _opt_text(block.get("label"), MAX_TITLE_CHARS)}


def _normalize_chart_block(block: dict) -> dict:
    series: list[dict] = []
    raw_series = block.get("series")
    if isinstance(raw_series, list):
        for entry in raw_series[:MAX_SERIES]:
            if not isinstance(entry, dict):
                continue
            key = _text(entry.get("key"), 64)
            if not key:
                continue
            series.append({
                "key": key,
                "label": _text(entry.get("label"), MAX_TITLE_CHARS) or key,
                # 颜色交给前端主题决定，这里只在显式给了合法 hex 时透传
                "color": _hex_or_none(entry.get("color")),
            })
    points: list[dict] = []
    raw_data = block.get("data")
    if isinstance(raw_data, list):
        allowed_keys = {entry["key"] for entry in series} | {_text(block.get("xKey"), 64)}
        for point in raw_data[:MAX_CHART_POINTS]:
            if not isinstance(point, dict):
                continue
            points.append({
                key: _scalar_or_text(value)
                for key, value in point.items()
                if key in allowed_keys
            })
    try:
        height = int(block.get("height", 240))
    except (TypeError, ValueError):
        height = 240
    return {
        "type": "chart",
        "chartType": _one_of(block.get("chartType"), ("bar", "line", "area", "pie", "donut"), "bar"),
        "data": points,
        "xKey": _text(block.get("xKey"), 64) or (points[0] and list(points[0].keys())[0]) or "x",
        "series": series,
        "title": _opt_text(block.get("title"), MAX_TITLE_CHARS),
        "caption": _opt_text(block.get("caption"), MAX_TITLE_CHARS),
        "height": max(160, min(560, height)),
        "stacked": bool(block.get("stacked", False)),
    }


def _normalize_progress_block(block: dict) -> dict:
    items: list[dict] = []
    raw = block.get("items")
    if isinstance(raw, list):
        for item in raw[:MAX_ITEM_COUNT]:
            if not isinstance(item, dict):
                continue
            try:
                value = float(item.get("value", 0))
            except (TypeError, ValueError):
                value = 0.0
            items.append({
                "label": _text(item.get("label"), MAX_TITLE_CHARS),
                "value": max(0.0, min(100.0, value)),
                "sublabel": _opt_text(item.get("sublabel"), 120),
                "color": _one_of(
                    item.get("color"),
                    ("green", "blue", "amber", "red", "violet", "default"),
                    "default",
                ),
            })
    return {"type": "progress", "items": items}


def _normalize_kanban_block(block: dict) -> dict:
    columns: list[dict] = []
    raw = block.get("columns")
    if isinstance(raw, list):
        for index, column in enumerate(raw[:MAX_COLUMN_COUNT]):
            if not isinstance(column, dict):
                continue
            cards: list[dict] = []
            raw_cards = column.get("cards")
            if isinstance(raw_cards, list):
                for card in raw_cards[:MAX_ITEM_COUNT]:
                    if not isinstance(card, dict):
                        continue
                    tags = card.get("tags")
                    cards.append({
                        "id": _text(card.get("id"), 64) or f"c{index}-{len(cards)}",
                        "title": _text(card.get("title"), MAX_TITLE_CHARS),
                        "description": _opt_text(card.get("description"), 800),
                        "tags": [_text(tag, 40) for tag in tags[:MAX_SUB_ITEMS]] if isinstance(tags, list) else [],
                        "priority": _one_of(card.get("priority"), ("high", "medium", "low"), "medium"),
                        "assignee": _opt_text(card.get("assignee"), 80),
                    })
            columns.append({
                "id": _text(column.get("id"), 64) or f"col{index}",
                "title": _text(column.get("title"), MAX_TITLE_CHARS),
                "color": _one_of(
                    column.get("color"),
                    ("default", "blue", "amber", "green", "red", "violet"),
                    "default",
                ),
                "cards": cards,
            })
    return {"type": "kanban", "columns": columns}


def _normalize_map_block(block: dict) -> dict:
    points: list[dict] = []
    raw = block.get("data")
    if isinstance(raw, list):
        for point in raw[:MAX_MAP_POINTS]:
            if not isinstance(point, dict):
                continue
            try:
                value = float(point.get("value", 0))
            except (TypeError, ValueError):
                value = 0.0
            points.append({"name": _text(point.get("name"), 80), "value": value})
    return {
        "type": "map",
        "data": points,
        "unit": _opt_text(block.get("unit"), 32),
        "colorScheme": _one_of(block.get("colorScheme"), ("blue", "green", "amber", "violet"), "blue"),
    }


def _hex_or_none(value: Any) -> str | None:
    """只接受 #RGB / #RRGGBB，其余（含 CSS 函数式颜色）一律忽略，避免注入。"""
    text = str(value or "").strip()
    if len(text) in (4, 7) and text.startswith("#"):
        try:
            int(text[1:], 16)
            return text
        except ValueError:
            return None
    return None


#: 块 type → 归一化函数
_NORMALIZERS = {
    "text": _normalize_text_block,
    "heading": _normalize_heading_block,
    "code": _normalize_code_block,
    "table": _normalize_table_block,
    "stats": _normalize_stats_block,
    "cards": _normalize_cards_block,
    "alert": _normalize_alert_block,
    "comparison": _normalize_comparison_block,
    "timeline": _normalize_timeline_block,
    "list": _normalize_list_block,
    "separator": _normalize_separator_block,
    "chart": _normalize_chart_block,
    "progress": _normalize_progress_block,
    "kanban": _normalize_kanban_block,
    "map": _normalize_map_block,
}


def validate_layout(raw: Any, *, max_blocks: int = 12) -> dict | None:
    """校验并清洗一份布局文档。

    Args:
        raw: 模型产出的 JSON（已 json.loads）。
        max_blocks: 允许的最大块数（来自配置 ``agent_layout_max_blocks``）。

    Returns:
        清洗后的 ``AGUILayout`` dict；任何**结构性**问题（版本不符、blocks 不是
        非空数组、存在不认识的块 type）返回 ``None``，调用方据此回落 markdown。
        单块内的越界字段只做夹紧，不导致整份失败。
    """
    if not isinstance(raw, dict):
        return None
    if str(raw.get("version") or "") != LAYOUT_VERSION:
        return None
    blocks_raw = raw.get("blocks")
    if not isinstance(blocks_raw, list) or not blocks_raw:
        return None

    blocks: list[dict] = []
    for block in blocks_raw:
        if len(blocks) >= max(1, max_blocks):
            break
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip()
        if block_type not in BLOCK_TYPES:
            # 不认识的块 → 整份放弃：半渲染的结构化内容比纯文本更容易误导用户
            return None
        normalized = _NORMALIZERS[block_type](block)
        # 空壳块（无内容的 text/heading、空表、空卡片）不值得占用版面，直接丢掉
        if _is_empty_block(normalized):
            continue
        blocks.append(normalized)

    if not blocks:
        return None

    return {
        "version": LAYOUT_VERSION,
        "intent": _text(raw.get("intent"), MAX_INTENT_CHARS) or "explanation",
        "title": _opt_text(raw.get("title"), MAX_TITLE_CHARS),
        "blocks": blocks,
    }


def _is_empty_block(block: dict) -> bool:
    """判断归一化后的块是否没有任何可展示内容（用于安静丢弃空壳块）。"""
    block_type = block.get("type")
    if block_type in ("text", "heading", "code", "alert"):
        payload = block.get("content") or block.get("code") or block.get("message") or ""
        # 用 strip 判定：纯空白的 text 块同样是空壳（否则前端会渲染出一个空段落）
        return not str(payload).strip()
    if block_type == "table":
        return not block.get("rows") or not block.get("columns")
    if block_type in ("stats", "cards", "list", "progress"):
        return not block.get("items")
    if block_type == "comparison":
        return not block.get("rows")
    if block_type == "timeline":
        return not block.get("events")
    if block_type == "chart":
        return not block.get("data") or not block.get("series")
    if block_type == "kanban":
        return not block.get("columns")
    if block_type == "map":
        return not block.get("data")
    # separator 无内容也算有效（就是一条分割线）
    return False
