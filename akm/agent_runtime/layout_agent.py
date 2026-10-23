"""布局 Agent：把一段已完成的回答转成结构化 AG-UI 布局。

定位：**主 Agent 之后的一次独立、可失败、可跳过的小模型调用**。
它不参与对话，不产出用户可见文字，只产出一份受约束的 JSON（``AGUILayout``）。

约定（与「兼容约定」条目一致，实现不得偏离）：
- 生成的布局只是**增强层**：调用方必须保留原始 markdown 正文，失败一律回落到正文；
- 失败/超时/JSON 非法/校验不过 → 返回 ``None``，绝不抛异常打断回答收尾；
- 正文太短（不值得排版）时**直接不调用**，省一次 token（见 ``MIN_CONTENT_CHARS``）；
- 复用 ``akm.proxy.forward_request``，因此 Key 选择 / 协议转换 / 重试 / 审计与其他
  请求完全一致，审计 source 记为 ``layout``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from akm.agent_runtime.layout_schema import LAYOUT_VERSION, validate_layout

logger = logging.getLogger(__name__)

#: 正文短于此长度就不调用布局模型：一两句话的回答排版只会更啰嗦。
MIN_CONTENT_CHARS = 160

#: 布局请求的输出上限。不显式设置时上游默认值往往只有 512 左右，12 个 block 的
#: JSON 会在中途被截断（解析直接失败），因此这里给足额度。
_LAYOUT_MAX_TOKENS = 2000

#: 协议说明（写进系统提示）。与 `layout_schema.py` 的白名单严格对齐，改一处必须改两处。
_AGUI_SPEC = """\
你必须输出一个 JSON 对象（不要 markdown 围栏、不要解释文字），结构：

{
  "version": "1.0",
  "intent": "explanation | data_summary | comparison | step_by_step | error_analysis | recommendation | status_report | code_review",
  "title": "可选，一句话概括（<= 40 字）",
  "blocks": [ ... ]
}

blocks 里每个元素必须是下面 11 种之一（字段名严格按此，不要自创）：
- {"type":"stats","items":[{"label":"...","value":"...","delta":{"value":"+12%","trend":"up|down|neutral"},"color":"default|green|red|blue|amber|violet"}],"columns":2|3|4}
- {"type":"cards","items":[{"title":"...","description":"...","badge":"...","badgeColor":"default|blue|green|red|amber|violet","meta":"...","tags":["..."]}],"columns":2|3|4}
- {"type":"alert","severity":"info|warning|error|success","title":"可选","message":"..."}
- {"type":"comparison","headers":["A","B"],"rows":[{"label":"维度","values":["x",true,1]}],"caption":"可选"}
- {"type":"timeline","events":[{"time":"09:00","title":"...","description":"...","status":"done|active|pending"}]}
- {"type":"flow","nodes":[{"id":"n0","label":"...","sublabel":"可选","kind":"step|start|end|decision|async"}],"edges":[{"from":"n0","to":"n1","label":"可选","kind":"main|branch|async|return"}],"direction":"LR|TB","groups":[{"label":"阶段名","color":"default|blue|green|amber|violet|red","nodes":["n0"]}],"legend":true,"caption":"可选"}
- {"type":"separator","label":"可选"}
- {"type":"chart","chartType":"bar|line|area|pie|donut","data":[{"x":"一月","y":12}],"xKey":"x","series":[{"key":"y","label":"销量"}],"title":"可选","caption":"可选","height":240,"stacked":false}
- {"type":"progress","items":[{"label":"...","value":72,"sublabel":"可选","color":"green|blue|amber|red|violet|default"}]}
- {"type":"kanban","columns":[{"id":"todo","title":"待办","color":"default|blue|amber|green|red|violet","cards":[{"id":"c1","title":"...","description":"...","tags":["..."],"priority":"high|medium|low","assignee":"可选"}]}]}
- {"type":"map","data":[{"name":"华东","value":120}],"unit":"万元","colorScheme":"blue|green|amber|violet"}

硬性规则：
1. **只允许以上 11 种 type**，出现任何其它 type 会被整份丢弃。
2. 最多 12 个 block（按信息重要性排序），宁可少而准。
3. 数据必须**完全来自给定正文**，不得编造数字、条目或结论。
4. 用户需要读的正文不要重复搬进 block：段落、标题、代码块、表格、列表都交给正文，
   block 只做 markdown 做不到的结构化提炼（指标 / 步骤 / 对比 / 告警 / 图表 / 流程）。
5. flow 的 edges 可以整段省略：省略时按 nodes 顺序自动连成一条主链，只有分支或回边才逐个写 from/to。
   **edge 的 kind 必须按语义选**：`main` 主链推进、`branch` 分叉到**后面的**节点（判断的两个出口）、
   `async` 异步旁路、`return` **回到前面已出现过的节点**（打回重试、循环、反馈）。
   把"回到上一步"写成 `branch` 会让图出现环：虽然渲染器已能识别并降级处理（不会崩、也不会拍平布局），
   但 `return` 才会画成虚线回边，语义更准确。
6. 如果这篇回答不值得结构化（寒暄、一句话答案、纯闲聊、内容不足），
   输出 {"version":"1.0","intent":"explanation","blocks":[]} —— 空 blocks 表示跳过。
"""


def _strip_fences(text: str) -> str:
    """去掉模型可能加的 ```json ... ``` 围栏，只留 JSON 主体。"""
    raw = text.strip()
    fence = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```$", raw, re.S)
    if fence:
        return fence.group(1).strip()
    # 兜底：从第一个 { 到最后一个 }（模型常在前面加一句"好的，这是布局："）
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        return raw[start:end + 1]
    return raw


def _parse_layout_text(text: str, max_blocks: int) -> dict | None:
    """把模型输出文本解析成校验通过的布局；任一步失败返回 None。"""
    if not text or not text.strip():
        return None
    candidate = _strip_fences(text)
    try:
        payload: Any = json.loads(candidate)
    except json.JSONDecodeError as exc:
        logger.info("[layout] 模型输出不是合法 JSON: %s", exc)
        return None
    return validate_layout(payload, max_blocks=max_blocks)


async def _notify_attempt(
    on_attempt: Any,
    result: dict | None,
    body: dict,
    response_body: str,
    status_code: int,
    error: str = "",
) -> None:
    """把一次布局请求的结果回调给调用方（当前唯一用途是写审计日志）。

    布局模块本身不依赖审计实现：回调由 ``AgentLoop`` 注入，未注入时是空操作。
    回调异常一律吞掉 —— 审计是旁路，绝不能因为写日志失败影响布局或回答收尾。

    Args:
        on_attempt: 调用方注入的回调；None 表示不需要。
        result: ``forward_request`` 的返回；超时 / 异常时为 None。
        body: 本次发给上游的请求体。
        response_body: 上游响应体；未拿到响应时为空串。
        status_code: 上游 HTTP 状态码；未拿到响应时为 0。
        error: 超时 / 异常 / 非 2xx 的说明，正常为空串。
    """
    if on_attempt is None:
        return
    try:
        await on_attempt(result, body, response_body, status_code, error)
    except Exception:  # noqa: BLE001 - 旁路回调，任何异常都不能冒泡
        logger.warning("[layout] 审计回调失败", exc_info=True)


async def render_layout(
    *,
    content: str,
    model: str,
    http_client: Any,
    api_path: str = "chat/completions",
    plugin_manager: Any = None,
    timeout_sec: float = 25.0,
    max_retries: int = 1,
    max_blocks: int = 12,
    intent_hint: str = "",
    on_attempt: Any = None,
) -> dict | None:
    """让布局模型把 ``content`` 转成 AG-UI 布局。

    Args:
        content: 主 Agent 已完成的回答正文（markdown）。
        model: 布局模型 id（来自 ``agent_layout_model``）。
        http_client: 与主流程共用的 httpx client（走连接池）。
        api_path: 上游协议，跟随主请求（``chat/completions`` 等）。
        plugin_manager: 传给 ``forward_request`` 以复用插件钩子。
        timeout_sec: 单次调用超时；超时即放弃（不阻塞回答收尾）。
        max_retries: 校验失败后的重试次数（带上一次的错误反馈）。
        max_blocks: 允许的最大块数。
        intent_hint: 主 Agent 给出的意图提示（可为空）。
        on_attempt: 每次真实上游请求后的回调（result, body, response_body,
            status_code, error）；由 ``AgentLoop`` 注入用于写审计日志，未注入时为 None。

    Returns:
        通过校验的 ``AGUILayout`` dict；任何失败（正文过短、超时、上游报错、
        非法 JSON、校验不过）都返回 ``None``，调用方据此跳过布局。
    """
    text = str(content or "").strip()
    if len(text) < MIN_CONTENT_CHARS:
        logger.debug("[layout] 正文仅 %d 字符，跳过布局生成", len(text))
        return None
    if not str(model or "").strip():
        return None

    from akm.proxy import forward_request

    user_prompt = f"意图提示：{intent_hint or '（未提供，请自行判断）'}\n\n以下是需要结构化的回答正文：\n\n{text}"
    attempt = 0
    last_error = ""
    # 只对 OpenAI Chat 协议启用 JSON 强约束，避免把 Anthropic / Responses 之类的
    # 上游惹毛（它们不认识 response_format；真遇到 400 时下面的降级分支会去掉它）。
    json_mode = "chat/completions" in str(api_path or "")
    while attempt <= max(0, max_retries):
        messages: list[dict] = [
            {"role": "system", "content": _AGUI_SPEC},
            {"role": "user", "content": user_prompt},
        ]
        if last_error:
            # 重试时把上一次的失败原因回灌，避免模型重复同一个错误
            messages.append({
                "role": "user",
                "content": f"上一次输出未通过校验：{last_error}。请严格按协议重新输出 JSON。",
            })
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": 0.2,
        }
        # OpenAI Chat 协议下补两个强约束，缺任一个都会让布局命中率大幅下降：
        # - max_tokens：不设时上游默认值很小（实测约 512），12 个 block 的 JSON 会被
        #   拦腰截断，JSON.parse 直接失败；
        # - response_format：部分模型无视「只输出 JSON」的提示，会改写成 markdown
        #   正文，这是唯一可靠的强制手段。
        if json_mode:
            body["max_tokens"] = _LAYOUT_MAX_TOKENS
            body["response_format"] = {"type": "json_object"}
        try:
            result = await asyncio.wait_for(
                forward_request(
                    body,
                    http_client,
                    api_path=api_path,
                    plugin_manager=plugin_manager,
                ),
                timeout=max(1.0, float(timeout_sec)),
            )
        except asyncio.TimeoutError:
            await _notify_attempt(
                on_attempt, None, body, "", 0,
                error=f"布局模型调用超时（{timeout_sec}s）",
            )
            logger.warning("[layout] 布局模型调用超时（%.1fs），跳过布局", timeout_sec)
            return None
        except Exception as exc:  # noqa: BLE001 - 布局是增强层，任何异常都不能打断回答
            await _notify_attempt(
                on_attempt, None, body, "", 0, error=f"布局模型调用异常: {exc}"
            )
            logger.warning("[layout] 布局模型调用异常: %s", exc)
            return None

        status_code = int(result.get("status_code", 0) or 0)
        response_body = str(result.get("body", "") or "")
        # 每次真实请求都回调一次（含下面 400 降级的重试），由调用方写审计日志：
        # 布局是独立上游请求，会真实消耗 token，不记录则统计与审计都看不到。
        await _notify_attempt(
            on_attempt,
            result,
            body,
            response_body,
            status_code,
            error=(
                ""
                if 200 <= status_code < 300
                else str(result.get("error", "") or f"HTTP {status_code}")
            ),
        )
        # 上游不认 response_format（部分网关 / 非 OpenAI 协议会直接 400）：去掉该字段
        # 再试一次，只靠提示词约束仍有成功率，不让一个可选参数把整条链路废掉。
        if status_code == 400 and json_mode:
            logger.info("[layout] 上游拒绝 response_format，去掉该字段后重试")
            json_mode = False
            continue
        if status_code < 200 or status_code >= 300:
            logger.warning("[layout] 布局模型返回 HTTP %s，跳过布局", status_code)
            return None

        # 复用主流程的文本提取（兼容 Chat / Responses / Messages 三种上游格式）
        from akm.agent_runtime.loop import _extract_text_content
        text_out = _extract_text_content(response_body)
        layout = _parse_layout_text(text_out, max_blocks)
        if layout is not None:
            return layout

        last_error = "输出不是符合协议的 JSON（或 blocks 为空/含未定义 type）"
        attempt += 1
        if attempt <= max_retries:
            logger.info("[layout] 第 %d 次校验失败，准备重试", attempt)

    logger.warning("[layout] 重试 %d 次后仍未通过校验，跳过布局", max(0, max_retries))
    return None
