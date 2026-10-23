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
#:
#: 只有 markdown 正文做不到的形态才进协议：段落 / 标题 / 代码块 / 表格 / 列表
#: 由正文自己表达，因此**不在**白名单里（``render_schema.json`` 也不暴露，
#: 前端 ``AGUIRenderer`` 同样没有它们的渲染分支）。
BLOCK_TYPES: frozenset[str] = frozenset({
    "stats", "cards", "alert", "comparison", "timeline", "separator",
    "chart", "progress", "kanban", "map", "flow",
})

#: 各项硬上限。取值原则：够用即可，宁可截断也不要让单条消息膨胀。
MAX_TEXT_CHARS = 4000        # 单个文本类字段
MAX_ROW_COUNT = 100          # 对比表行
MAX_COLUMN_COUNT = 12        # 对比表列 / 看板列
MAX_ITEM_COUNT = 24          # stats / cards / timeline / progress 条目
MAX_CHART_POINTS = 120       # 图表数据点
MAX_SERIES = 8               # 图表系列数
MAX_MAP_POINTS = 60          # 地图数据点
MAX_FLOW_NODES = 12          # 流程图节点数
MAX_FLOW_EDGES = 24          # 流程图连线数
MAX_FLOW_GROUPS = 4          # 流程图分区（泳道）数上限
MAX_SUB_ITEMS = 8            # cards / kanban 的 tags 子项
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




def _scalar_or_text(value: Any) -> str | int | float:
    """标量单元格：只允许 str/number/bool，其余转字符串（对比表、图表数据用）。"""
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


def _normalize_flow_block(block: dict) -> dict:
    """流程图：nodes 是方框，edges 是带方向的连线。

    校验口径（宁可少画一条边，也不要画错）：

    - nodes 少于 2 个视为空壳块，由 ``_is_empty_block`` 丢弃；
    - 节点 id 缺省时按顺序生成 ``n0``、``n1``…；重复 id 丢弃后来的；
    - edges 的 ``from`` / ``to`` 必须**精确命中**已有 id，否则丢这条边
      （不做模糊匹配——连到错误的框比不连更误导）；
    - 自环（from == to）丢弃；
    - edges 整段缺失或全被丢弃时，按 nodes 顺序自动串成一条主链：
      模型只给节点也能画出线性流程，不必为「一步接一步」重复写边。
    """
    nodes: list[dict] = []
    raw_nodes = block.get("nodes")
    if isinstance(raw_nodes, list):
        for index, node in enumerate(raw_nodes[:MAX_FLOW_NODES]):
            if not isinstance(node, dict):
                continue
            label = _text(node.get("label"), MAX_TITLE_CHARS)
            if not label:
                continue
            node_id = _text(node.get("id"), 64) or f"n{index}"
            if any(existing["id"] == node_id for existing in nodes):
                continue
            nodes.append({
                "id": node_id,
                "label": label,
                "sublabel": _opt_text(node.get("sublabel"), 120),
                "kind": _one_of(node.get("kind"), ("step", "start", "end", "decision", "async"), "step"),
            })

    ids = {node["id"] for node in nodes}
    edges: list[dict] = []
    raw_edges = block.get("edges")
    if isinstance(raw_edges, list):
        for edge in raw_edges[:MAX_FLOW_EDGES]:
            if not isinstance(edge, dict):
                continue
            source = _text(edge.get("from"), 64)
            target = _text(edge.get("to"), 64)
            if source not in ids or target not in ids or source == target:
                continue
            edges.append({
                "from": source,
                "to": target,
                "label": _opt_text(edge.get("label"), 120),
                "kind": _one_of(edge.get("kind"), ("main", "branch", "async", "return"), "main"),
            })

    if not edges and len(nodes) >= 2:
        edges = [
            {"from": nodes[i]["id"], "to": nodes[i + 1]["id"], "label": None, "kind": "main"}
            for i in range(len(nodes) - 1)
        ]

    # 分区/泳道：把若干节点圈进一个框（对应 archify demo 的 01 / 02 / EX / 04 泳道）。
    # 规则：只保留真实存在的 id；一个节点只归属第一个匹配的组（避免框套框）；
    # 空组丢弃；最多 4 组——再多图会花，反而看不清。
    groups: list[dict] = []
    assigned: set[str] = set()
    raw_groups = block.get("groups")
    if isinstance(raw_groups, list):
        # 多给几项余量：空组 / 全无效的分组不占「最多 4 组」的名额
        for group in raw_groups[: MAX_FLOW_GROUPS * 4]:
            if len(groups) >= MAX_FLOW_GROUPS:
                break
            if not isinstance(group, dict):
                continue
            label = _text(group.get("label"), 40)
            if not label:
                continue
            members: list[str] = []
            for candidate in group.get("nodes") or []:
                node_id = _text(candidate, 64)
                if node_id in ids and node_id not in assigned:
                    members.append(node_id)
                    assigned.add(node_id)
            if not members:
                continue
            groups.append({
                "label": label,
                "color": _one_of(
                    group.get("color"),
                    ("default", "blue", "green", "amber", "violet", "red"),
                    "default",
                ),
                "nodes": members,
            })

    return {
        "type": "flow",
        "direction": _one_of(block.get("direction"), ("LR", "TB"), "LR"),
        "nodes": nodes,
        "edges": edges,
        "groups": groups,
        "legend": bool(block.get("legend", False)),
        "caption": _opt_text(block.get("caption"), MAX_TITLE_CHARS),
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
    # xKey 必须在「按 series / xKey 裁剪数据键」**之前**确定：模型常只给 data 不给 xKey，
    # 若先裁剪，分类键（如 "x"）会被当成噪声删掉，之后就只能把数值键（"y"）误当分类轴，
    # 前端表现为「横轴标签缺失」或「data 里的 x 字段整段消失」。
    # 推断顺序：显式 xKey > 第一个数据点的第一个键 > "x"。
    raw_data = block.get("data")
    sample = raw_data[:MAX_CHART_POINTS] if isinstance(raw_data, list) else []
    x_key = _text(block.get("xKey"), 64) or next(
        (str(key) for point in sample if isinstance(point, dict) for key in point),
        "",
    ) or "x"
    points: list[dict] = []
    if isinstance(raw_data, list):
        allowed_keys = {entry["key"] for entry in series} | {x_key}
        for point in sample:
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
        "xKey": x_key,
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
#:
#: 与 ``render_schema.json`` 暴露给模型的集合严格一一对应（10 种结构化块 + flow）：
#: markdown 正文已经能做的 text / heading / code / table / list 不在协议里，
#: 前端也同步删掉了它们的渲染分支——两边不留「仅兼容历史」的第三档口径。
_NORMALIZERS = {
    "stats": _normalize_stats_block,
    "cards": _normalize_cards_block,
    "alert": _normalize_alert_block,
    "comparison": _normalize_comparison_block,
    "timeline": _normalize_timeline_block,
    "separator": _normalize_separator_block,
    "chart": _normalize_chart_block,
    "progress": _normalize_progress_block,
    "kanban": _normalize_kanban_block,
    "map": _normalize_map_block,
    "flow": _normalize_flow_block,
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
        # 空壳块（空告警、空指标/卡片、没有连线意义的流程）不值得占用版面，直接丢掉
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
    if block_type == "alert":
        payload = block.get("message") or ""
        # 用 strip 判定：纯空白的告警同样是空壳（否则前端会渲染出一个空提示框）
        return not str(payload).strip()
    if block_type in ("stats", "cards", "progress"):
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
    if block_type == "flow":
        # 单节点没有「流程」可言，与 0 节点一样视为空壳
        return len(block.get("nodes") or []) < 2
    # separator 无内容也算有效（就是一条分割线）
    return False
