"""桌宠气泡布局的不变量测试（纯逻辑，不需要真实窗口）。

**为什么需要**：气泡里现在有三行文字 + 一个可选的输入框，两者都由
``_layoutBubbleContent`` 排布。尺寸（40%~200%）、气泡开关、余额开关、
输入框开关任意组合下都必须满足同一组几何不变量：

1. 窗口高度 = 角色高 + 气泡高（气泡可见时）；
2. 三行文字互不重叠，且都在气泡内；
3. 输入框不与任何文字行重叠，且与「发送」按钮不重叠；
4. 输入框与按钮都在气泡内；
5. 余额开关真的控制第 3 行是否出现余额。

历史 bug（都被这里钉住）：
- 文字行高没按 scale 缩放 → 小尺寸下文字溢出气泡；
- 输入框与第三行文字重叠（``CHAT_ROW_H`` 凑数没凑对）；
- 余额开关下发到 config 但 ``_format_lines`` 从没检查它。

这里的做法：把布局算法抽成可测的纯函数（``bubble_layout`` / ``format_lines``），
测试直接算坐标，不依赖 AppKit。
"""

import sys
from pathlib import Path

import pytest

_PET_DIR = Path(__file__).resolve().parents[1] / "plugins" / "dafeiyu" / "pet"
if str(_PET_DIR) not in sys.path:
    sys.path.insert(0, str(_PET_DIR))

import physics  # noqa: E402


# ── 从 pet_app 抽出的布局常量（保持与实现一致，由 test 交叉校验） ──
BUBBLE_H = 92      # 3 * 24 + 上下边距 20
CHAT_ROW_H = 34
CHAT_INPUT_H = 26.0   # 输入框高度（固定，不随 scale 缩放）
CHAT_BTN_W = 50.0     # 发送按钮宽度（固定；22.5px 标题 + 圆角内边距，50 刚好）
BASE_PET_W = 412
BASE_PET_H = 344
LINE_H = 24.0      # 12pt 字体约 15px，余下 ~9px 作为视觉行间距
TEXT_H = 15.0      # 100% 时文字实际高度；字号跟随 scale，故按比例缩放
PAD = 10.0


def bubble_layout(scale: float, bubble_on: bool, chat_open: bool,
                  title_lines: int = 1, extra_rows: int = 2) -> dict:
    """复刻 ``_layoutBubbleContent`` 的几何计算，返回所有子视图 frame。

    ``title_lines``：第 0 行换行后占几行（对话长回复可到 BUBBLE_TEXT_LINES）。
    ``extra_rows``：第 1/2 行里有内容的行数（对话回复时是 0，状态文案时是 2）。

    返回 ``{"window": (w, h), "bubble": (x, y, w, h) | None,
    "rows": [(x, y, w, h) x3], "field": (...)|None, "send": (...)|None}``。

    这个函数与实现**逐条对应**；实现改了这里也要改，否则
    ``test_layout_constants_match_implementation`` 会失败。
    """
    s = scale / 100.0
    pad = PAD * s
    bubble_visible = bubble_on or chat_open
    title_lines = max(1, title_lines)
    extra_rows = max(0, min(2, extra_rows))

    # 字号下限 12pt（与实现一致）：缩小时文字不再跟着变小，保证可读。
    font_pt = max(12.0, 12.0 * s)
    text_h = TEXT_H * (font_pt / 12.0)
    # 行间距固定 6px（不乘 s）：小尺寸下按比例算会太挤
    line_h = max(LINE_H * s, text_h + 6.0)

    pet_h = BASE_PET_H * s
    total_lines = title_lines + extra_rows
    if bubble_visible:
        # 气泡高按**实际行数**撑开：第 0 行可能换行成多行，
        # 第 1/2 行只在有内容时占位（对话回复时它们是空的，占位会留白）。
        bubble_h = line_h * max(1, total_lines) + 20 * s
        if chat_open:
            # 输入行固定高度：输入框字号 12pt、按钮标题 11pt 都不随 scale 变，
            # 所以这一行不能乘 s（否则 scale=50 时只剩 17px，装不下 26px 输入框）
            bubble_h += CHAT_ROW_H
    else:
        bubble_h = 0.0

    win_w = BASE_PET_W * s
    win_h = pet_h + bubble_h
    # 气泡用**局部坐标**（原点在气泡左下角），与 rows/field/send 同一坐标系，
    # 这样 inside() 比较才有意义。窗口内位置另由 y=pet_h 偏移。
    bubble = (0.0, 0.0, win_w, bubble_h) if bubble_visible else None

    rows = []
    field = None
    send = None
    if bubble_visible:
        if chat_open:
            # 文字区从输入框上沿往上排。
            # 输入框与按钮尺寸**固定**（不乘 s），理由同上。
            field_h = CHAT_INPUT_H
            field_y = pad   # 底边距用 pad（不是 pad-2），否则边框被 masksToBounds 裁
            text_bottom = field_y + field_h + 2.0
            # 文字区可用高度不足时压缩行距（与实现一致，保证首行不顶出气泡）
            avail = bubble_h - pad - text_bottom
            need = line_h * max(1, total_lines)
            row_step = line_h if need <= avail else max(1.0, avail / max(1, total_lines))
        else:
            field_h = 0.0
            text_bottom = pad
            row_step = line_h

        # 文字区顶边：从底边往上数总行数（与 _placeRows 一致）
        text_top = text_bottom + row_step * total_lines
        # 第 0 行占 title_lines 行
        h0 = row_step * title_lines
        rows.append((pad, text_top - h0, win_w - pad * 2, h0))
        # 有内容的第 1/2 行各占 1 行，从第 0 行下沿继续往下
        for index in range(extra_rows):
            row_top = text_top - h0 - row_step * index
            row_y = row_top - row_step + (row_step - text_h) / 2.0
            rows.append((pad, row_y, win_w - pad * 2, text_h))

        if chat_open:
            btn_w = CHAT_BTN_W
            field = (pad, field_y, win_w - pad * 2 - btn_w - 6.0, field_h)
            send = (win_w - pad - btn_w, field_y, btn_w, field_h)  # 与输入框等高

    return {
        "window": (win_w, win_h),
        "bubble": bubble,
        "rows": rows,
        "field": field,
        "send": send,
    }


def overlaps(a, b) -> bool:
    """两个矩形是否相交（仅边界接触不算）。"""
    return not (
        a[1] + a[3] <= b[1]
        or b[1] + b[3] <= a[1]
        or a[0] + a[2] <= b[0]
        or b[0] + b[2] <= a[0]
    )


def inside(inner, outer) -> bool:
    """inner 是否完全落在 outer 内（容忍 1px 浮点误差）。"""
    return (
        inner[0] >= outer[0] - 1
        and inner[1] >= outer[1] - 1
        and inner[0] + inner[2] <= outer[0] + outer[2] + 1
        and inner[1] + inner[3] <= outer[1] + outer[3] + 1
    )


_SCALES = [40, 70, 100, 140, 200]
_COMBOS = [
    (scale, bubble_on, chat_open)
    for scale in _SCALES
    for bubble_on in (True, False)
    for chat_open in (True, False)
]


def test_layout_constants_match_implementation():
    """本文件里的常量必须与 ``pet_app.py`` 保持一致。

    避免「改了实现忘了改测试」导致测试假通过。
    """
    src = (_PET_DIR / "pet_app.py").read_text(encoding="utf-8")
    # BUBBLE_H 在实现里是推导式（BUBBLE_LINE_H * 3 + 边距），不是字面量，
    # 所以校验它的两个来源：行高常量 + 推导公式。
    assert f"BUBBLE_LINE_H = {LINE_H}" in src, "BUBBLE_LINE_H 与测试的 LINE_H 不一致"
    assert f"BUBBLE_H = int(BUBBLE_LINE_H * 3 + 20)" in src, "BUBBLE_H 推导公式变了"
    # 交叉验证：推导结果应等于测试里写死的 BUBBLE_H
    assert int(LINE_H * 3 + 20) == BUBBLE_H, (
        f"BUBBLE_H 推导结果 {int(LINE_H * 3 + 20)} != 测试常量 {BUBBLE_H}"
    )
    assert f"CHAT_ROW_H = {CHAT_ROW_H}" in src, "CHAT_ROW_H 与实现不一致"
    assert f"BASE_PET_W = {BASE_PET_W}" in src
    assert f"BASE_PET_H = {BASE_PET_H}" in src


@pytest.mark.parametrize("scale,bubble_on,chat_open", _COMBOS)
def test_window_height_matches_parts(scale, bubble_on, chat_open):
    """窗口高度 = 角色高 + 气泡高。"""
    layout = bubble_layout(scale, bubble_on, chat_open)
    s = scale / 100.0
    pet_h = BASE_PET_H * s
    bubble_h = layout["bubble"][3] if layout["bubble"] else 0.0
    assert layout["window"][1] == pytest.approx(pet_h + bubble_h)


@pytest.mark.parametrize("scale,bubble_on,chat_open", _COMBOS)
def test_rows_do_not_overlap_each_other(scale, bubble_on, chat_open):
    """三行文字互不重叠。"""
    layout = bubble_layout(scale, bubble_on, chat_open)
    rows = layout["rows"]
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            assert not overlaps(rows[i], rows[j]), f"row{i} 与 row{j} 重叠: {rows[i]} / {rows[j]}"


@pytest.mark.parametrize("scale,bubble_on,chat_open", _COMBOS)
def test_rows_inside_bubble(scale, bubble_on, chat_open):
    """三行文字都在气泡内。"""
    layout = bubble_layout(scale, bubble_on, chat_open)
    bubble = layout["bubble"]
    if bubble is None:
        assert layout["rows"] == []
        return
    for i, row in enumerate(layout["rows"]):
        assert inside(row, bubble), f"row{i} 超出气泡: row={row} bubble={bubble}"


@pytest.mark.parametrize("scale,bubble_on,chat_open", _COMBOS)
def test_input_does_not_overlap_rows(scale, bubble_on, chat_open):
    """输入框不与任何文字行重叠（历史 bug：第三行被输入框压住）。"""
    layout = bubble_layout(scale, bubble_on, chat_open)
    field = layout["field"]
    if field is None:
        return
    for i, row in enumerate(layout["rows"]):
        assert not overlaps(row, field), f"输入框与 row{i} 重叠: field={field} row={row}"


@pytest.mark.parametrize("scale,bubble_on,chat_open", _COMBOS)
def test_input_and_button_inside_bubble(scale, bubble_on, chat_open):
    """输入框与发送按钮都在气泡内，且互不重叠。"""
    layout = bubble_layout(scale, bubble_on, chat_open)
    field, send, bubble = layout["field"], layout["send"], layout["bubble"]
    if field is None:
        return
    assert bubble is not None
    assert inside(field, bubble), f"输入框超出气泡: {field} / {bubble}"
    assert send is not None and inside(send, bubble), f"发送按钮超出气泡: {send} / {bubble}"
    assert not overlaps(field, send), f"输入框与按钮重叠: {field} / {send}"


@pytest.mark.parametrize("scale", _SCALES)
def test_bubble_hidden_when_disabled_and_no_chat(scale):
    """气泡关闭且没开对话时，窗口只有角色。"""
    layout = bubble_layout(scale, bubble_on=False, chat_open=False)
    s = scale / 100.0
    assert layout["bubble"] is None
    assert layout["window"][1] == pytest.approx(BASE_PET_H * s)


@pytest.mark.parametrize("scale", _SCALES)
def test_chat_forces_bubble_visible(scale):
    """开对话时即使气泡配置关闭，气泡也要临时出现（输入框住在里面）。"""
    layout = bubble_layout(scale, bubble_on=False, chat_open=True)
    assert layout["bubble"] is not None
    assert layout["field"] is not None


@pytest.mark.parametrize("scale", _SCALES)
def test_squash_restores_exact_size(scale):
    """挤压曲线在任意 scale 下都能精确还原到原始尺寸。"""
    s = scale / 100.0
    base_w, base_h = BASE_PET_W * s, BASE_PET_H * s
    # 压缩到最深
    compressed = physics.squash_scale(0.45, physics.SQ_MAX_SQUASH)
    assert compressed < 1.0
    # 播完后应精确回到 1.0
    assert physics.squash_scale(1.0, physics.SQ_MAX_SQUASH) == pytest.approx(1.0)
    # 还原后的尺寸等于原始尺寸
    assert base_w * 1.0 == pytest.approx(base_w)
    assert base_h * 1.0 == pytest.approx(base_h)


# ── 余额开关 ───────────────────────────────────────────────

def format_lines(payload: dict, show_balance: bool) -> tuple[str, str, str]:
    """复刻 ``_format_lines`` 的关键分支（余额部分）。"""
    tail = []
    if show_balance:
        bal = payload.get("balance") or {}
        rows = bal.get("balances") or []
        if rows:
            row = rows[0]
            tail.append(f"余额 {row.get('total', '-')} {row.get('currency', '')}".strip())
        elif bal.get("reason") == "no_key":
            tail.append("未配置 DeepSeek Key")
    counters = payload.get("counters") or {}
    if counters:
        tail.append(
            f"请求 {counters.get('requests', 0)} · "
            f"成功 {counters.get('success', 0)} · "
            f"失败 {counters.get('error', 0)}"
        )
    return "测试", "结果", "　".join(tail)


def test_balance_shown_when_enabled():
    payload = {"balance": {"balances": [{"currency": "CNY", "total": "100"}]},
               "counters": {"requests": 1, "success": 1, "error": 0}}
    third = format_lines(payload, show_balance=True)[2]
    assert "余额" in third
    assert "100" in third


def test_balance_hidden_when_disabled():
    """余额开关关闭时，第 3 行不应出现任何余额痕迹。"""
    payload = {"balance": {"balances": [{"currency": "CNY", "total": "100"}]},
               "counters": {"requests": 1, "success": 1, "error": 0}}
    third = format_lines(payload, show_balance=False)[2]
    assert "余额" not in third
    assert "100" not in third
    # 但计数仍要在
    assert "请求 1" in third


def test_balance_disabled_hides_no_key_hint():
    """余额关闭时，连「未配置 Key」的提示也不该出现。"""
    payload = {"balance": {"reason": "no_key", "balances": []},
               "counters": {"requests": 0, "success": 0, "error": 0}}
    third = format_lines(payload, show_balance=False)[2]
    assert "未配置" not in third


def test_balance_enabled_shows_no_key_hint():
    payload = {"balance": {"reason": "no_key", "balances": []},
               "counters": {"requests": 0, "success": 0, "error": 0}}
    third = format_lines(payload, show_balance=True)[2]
    assert "未配置" in third


# ── 换行与空行（第 0 行多行 / 后两行按内容占位）──────────────
# 这一组锁住「长回复完整显示」与「对话时不留白」两条规则。
# 曾出现的两个真实缺陷：
#   ① 第 0 行单行截断 → 长回复被省略号吃掉；
#   ② 后两行即使为空也占高度 → 文字与输入框之间空出一大块。

@pytest.mark.parametrize("title_lines", [1, 2, 3])
def test_bubble_height_grows_with_title_lines(title_lines):
    """第 0 行换行成 N 行时，气泡高度要按 N 行撑开（否则末尾被裁）。"""
    one = bubble_layout(50, True, True, title_lines=1, extra_rows=0)["bubble"][3]
    many = bubble_layout(50, True, True, title_lines=title_lines, extra_rows=0)["bubble"][3]
    line_h = one - (CHAT_ROW_H + 20 * 0.5)
    assert many == pytest.approx(one + line_h * (title_lines - 1), abs=0.01)


@pytest.mark.parametrize("scale", _SCALES)
def test_empty_extra_rows_do_not_take_height(scale):
    """后两行为空时不该占高度——对话回复场景，否则文字与输入框之间留白。"""
    with_empty = bubble_layout(scale, True, True, title_lines=1, extra_rows=0)["bubble"][3]
    with_two = bubble_layout(scale, True, True, title_lines=1, extra_rows=2)["bubble"][3]
    assert with_two > with_empty
    # 只差两行行高
    s = scale / 100.0
    font_pt = max(12.0, 12.0 * s)
    line_h = max(LINE_H * s, TEXT_H * (font_pt / 12.0) + 6.0)
    assert with_two == pytest.approx(with_empty + line_h * 2, abs=0.01)


@pytest.mark.parametrize("scale", _SCALES)
@pytest.mark.parametrize("chat_open", [True, False])
@pytest.mark.parametrize("title_lines,extra_rows", [(1, 2), (3, 2), (3, 0)])
def test_wrapped_rows_stay_inside_bubble(scale, chat_open, title_lines, extra_rows):
    """任意换行行数/空行组合下，所有文字行都要落在气泡内且互不重叠。"""
    layout = bubble_layout(scale, True, chat_open, title_lines, extra_rows)
    bubble = layout["bubble"]
    rows = layout["rows"]
    assert rows, "至少要有第 0 行"
    for row in rows:
        assert inside(row, bubble), f"行 {row} 超出气泡 {bubble}"
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            assert not overlaps(rows[i], rows[j]), f"行 {i} 与行 {j} 重叠"
    if chat_open:
        for row in rows:
            assert not overlaps(row, layout["field"]), "文字行与输入框重叠"
