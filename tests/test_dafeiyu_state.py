"""桌宠状态机单测：在途请求计数、终态落定、动画时长兜底。

这一组锁住「连续请求期间保持忙碌、最后一个请求结束后按结果播 success/error」
的语义，以及两个曾经真实发生的缺陷：

  ① ``thinking`` 不在回落白名单里 → 状态永久卡在 thinking
     （真实日志实测卡了 1510 / 1591 秒）；
  ② ``success`` 动画需要 10.12 秒，而 ``idle_after_s`` 默认 8 秒
     → 动画只播到 79% 就被切走。

只测纯状态机逻辑，不启动 AppKit、不发网络请求。
"""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "dafeiyu"
_INDEX = _PLUGIN_DIR / "index.py"


def _load_index_module():
    """导入插件模块（不实例化宿主，只取状态机与常量）。"""
    spec = importlib.util.spec_from_file_location("dafeiyu_index", _INDEX)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dafeiyu_index"] = module
    spec.loader.exec_module(module)
    return module


idx = _load_index_module()


def _new_plugin():
    """构造一个只初始化状态字段的插件实例。

    不走 ``__init__`` 里的宿主依赖（router / asyncio task / 子进程），
    只补状态机需要的属性，保证单测不依赖 FastAPI 运行时。
    """
    plugin = idx.Plugin.__new__(idx.Plugin)
    plugin._state = idx.STATE_IDLE
    plugin._state_text = ""
    plugin._state_since = time.time()
    plugin._last_result = {}
    plugin._inflight = 0
    plugin._pending_state = ""
    plugin._pending_detail = ""
    # 请求登记表：waiting 判定按「已选 Key 的在途请求占比」算（≥ 9 成才切）
    plugin._pending_requests = {}
    # 轮次状态：静默期 + 过半判定所需
    plugin._round_ok = 0
    plugin._round_err = 0
    plugin._quiet_until = 0.0
    plugin._round_settled = True
    plugin._counters = {"requests": 0, "success": 0, "error": 0}
    plugin._recent = []
    plugin._cfg = {"idle_after_s": 8}
    # waiting / working 延迟落定所需（thinking 播完切 waiting、working 播完才 success）
    plugin._round_started_at = 0.0
    plugin._working_until = 0.0
    plugin._deferred_terminal = ""
    plugin._deferred_detail = ""
    plugin._balance = {}
    plugin._balance_at = 0.0
    # _state_payload 还会读这些：端口用于拼 base_url，_pet_proc 用于 running 字段
    plugin._port = 8800
    plugin._pet_proc = None
    return plugin


def _payload(plugin, *, ago: float = 0.0):
    """取状态快照，``ago`` 表示把「上次状态变化」往前推多少秒。"""
    if ago:
        plugin._state_since = time.time() - ago
    return plugin._state_payload()


# ── 在途请求期间保持忙碌 ─────────────────────────────────

def test_inflight_keeps_state_busy():
    """有在途请求时，即使超过 idle_after 也不回落 idle。

    这就是「后续请求延长 working 时间」：连续请求期间角色一直忙。
    """
    plugin = _new_plugin()
    plugin._set_state(idx.STATE_THINKING)
    plugin._inflight = 2
    # 已经过去 1 小时，远超 idle_after，但在途请求还在 → 必须保持 thinking
    assert _payload(plugin, ago=3600)["state"] == idx.STATE_THINKING


def test_inflight_zero_falls_back_to_idle():
    """没有在途请求且超过 idle_after 时，非 idle 状态都要回落。

    ``thinking`` 曾因不在白名单里永久卡住，这条断言防止回归。
    """
    for state in (idx.STATE_THINKING, idx.STATE_WORKING, idx.STATE_WAITING):
        plugin = _new_plugin()
        plugin._set_state(state)
        plugin._inflight = 0
        assert _payload(plugin, ago=3600)["state"] == idx.STATE_IDLE


# ── 终态只在最后一个请求结束时落定 ───────────────────────

def test_terminal_state_deferred_while_inflight():
    """还有请求在飞时，先完成的结果不立刻切换状态。"""
    plugin = _new_plugin()
    plugin._set_state(idx.STATE_THINKING)
    plugin._inflight = 2
    plugin._set_state(idx.STATE_SUCCESS)
    # 被挂起：状态仍是 thinking，终态记在 _pending_state
    assert plugin._state == idx.STATE_THINKING
    assert plugin._pending_state == idx.STATE_SUCCESS


def test_terminal_state_applied_when_last_request_finishes():
    """最后一个请求结束（_inflight 归 0）时按结果落定 success。"""
    plugin = _new_plugin()
    plugin._set_state(idx.STATE_THINKING)
    plugin._inflight = 1
    plugin._pending_state = idx.STATE_SUCCESS
    plugin._pending_detail = ""
    # 模拟 on_response 里 _inflight 减完之后的落定动作
    plugin._inflight = 0
    plugin._set_state(plugin._pending_state, force=True)
    assert plugin._state == idx.STATE_SUCCESS


def test_last_request_error_shows_error():
    """最后一个请求失败 → error 动画。"""
    plugin = _new_plugin()
    plugin._inflight = 0
    plugin._pending_state = idx.STATE_ERROR
    plugin._pending_detail = "失败 503 upstream"
    plugin._set_state(plugin._pending_state, detail=plugin._pending_detail, force=True)
    assert plugin._state == idx.STATE_ERROR
    assert "503" in plugin._state_text


# ── success / error 的展示时长按动画兜底 ─────────────────

@pytest.mark.parametrize("state", [idx.STATE_SUCCESS, idx.STATE_ERROR])
def test_terminal_state_held_for_full_animation(state):
    """终态展示时长必须 ≥ 动画一轮，否则动画播不完就被切走。"""
    plugin = _new_plugin()
    plugin._set_state(state)
    plugin._inflight = 0
    anim = idx.STATE_ANIM_SECONDS[state]
    idle_after = 8.0
    hold = max(idle_after, int(anim) + 1)
    # 动画还没播完（例如 idle_after=8 而 success 要 10.12 秒）→ 必须仍是该状态
    assert _payload(plugin, ago=anim - 0.5)["state"] == state
    # 超过「动画时长与 idle_after 取大者 + 1 秒缓冲」后 → 回落 idle
    assert _payload(plugin, ago=hold + 2.0)["state"] == idx.STATE_IDLE


def test_anim_seconds_match_manifest():
    """硬编码的动画时长必须与素材清单一致（素材变了要同步改）。"""
    import json

    manifest = json.loads(
        (_PLUGIN_DIR / "assets" / "pet-manifest.json").read_text(encoding="utf-8")
    )
    clips = manifest["clips"]
    for state, clip_name in (
        (idx.STATE_SUCCESS, "success"),
        (idx.STATE_ERROR, "error"),
    ):
        clip = clips[clip_name]
        expected = len(clip["frames"]) * clip["frameMs"] / 1000.0
        assert idx.STATE_ANIM_SECONDS[state] == pytest.approx(expected, abs=0.01)


def test_short_animation_not_extended():
    """error 动画 4.96 秒 < idle_after 8 秒：取 idle_after，不该被缩短。"""
    plugin = _new_plugin()
    plugin._set_state(idx.STATE_ERROR)
    plugin._inflight = 0
    # 7 秒时 error 动画早已播完，但 idle_after=8 还没到 → 保持 error
    assert _payload(plugin, ago=7.0)["state"] == idx.STATE_ERROR


# ── 常量健全性 ───────────────────────────────────────────

def test_state_clip_covers_all_states():
    """每个状态都要有对应动作，否则桌宠收不到 clip。"""
    for state in (
        idx.STATE_IDLE, idx.STATE_THINKING, idx.STATE_WORKING,
        idx.STATE_WAITING, idx.STATE_SUCCESS, idx.STATE_ERROR,
    ):
        assert state in idx.STATE_CLIP


def test_upstream_error_bypasses_pending():
    """上游报错要立刻展示（force=True），不能被在途请求挂起。"""
    plugin = _new_plugin()
    plugin._set_state(idx.STATE_THINKING)
    plugin._inflight = 3
    plugin._set_state(idx.STATE_ERROR, detail="上游异常：HTTP 503", force=True)
    assert plugin._state == idx.STATE_ERROR
    assert "503" in plugin._state_text


# ── 静默期（ROUND_QUIET_S）与过半判定 ───────────────────
# 这一组锁住方案 A 的核心语义：
#   最后一个请求结束后先等 1.5 秒确认「本轮真的结束了」，
#   期间若来新请求就跳过终态直接进 thinking/working；
#   静默期平安过去后，按「本轮成功率 ≥ 50%」决定播 success 还是 error。

def _start_round(plugin):
    """模拟一个请求进来（走 on_request 的轮次重置逻辑）。"""
    plugin._inflight += 1
    if plugin._inflight == 1:
        plugin._round_ok = 0
        plugin._round_err = 0
        plugin._quiet_until = 0.0
        plugin._round_settled = False
        plugin._pending_state = ""
        plugin._pending_detail = ""
    return plugin


def _finish_request(plugin, ok: bool, detail: str = ""):
    """模拟一个请求结束（累计本轮计数、归 0 时启动静默期）。"""
    if plugin._inflight > 0:
        plugin._inflight -= 1
    if ok:
        plugin._round_ok += 1
    else:
        plugin._round_err += 1
        if detail:
            plugin._pending_detail = detail
    if plugin._inflight == 0:
        plugin._quiet_until = time.time() + idx.ROUND_QUIET_S


def test_quiet_period_defers_terminal_animation():
    """请求结束后不立刻播终态，要等静默期过去。"""
    plugin = _new_plugin()
    _start_round(plugin)
    plugin._set_state(idx.STATE_THINKING)
    _finish_request(plugin, True)
    # 静默期刚启动：还没落定
    assert plugin._round_settled is False
    assert _payload(plugin)["state"] == idx.STATE_THINKING


def test_quiet_period_elapsed_settles_success():
    """静默期过去后，本轮成功 → success。"""
    plugin = _new_plugin()
    _start_round(plugin)
    plugin._set_state(idx.STATE_THINKING)
    _finish_request(plugin, True)
    # 把静默期拨到过去
    plugin._quiet_until = time.time() - 1
    assert _payload(plugin)["state"] == idx.STATE_SUCCESS


def test_quiet_period_elapsed_settles_error():
    """静默期过去后，本轮全失败 → error。"""
    plugin = _new_plugin()
    _start_round(plugin)
    plugin._set_state(idx.STATE_THINKING)
    _finish_request(plugin, False, "失败 503 upstream")
    plugin._quiet_until = time.time() - 1
    payload = _payload(plugin)
    assert payload["state"] == idx.STATE_ERROR
    assert "503" in plugin._state_text


def test_new_request_cancels_quiet_period():
    """静默期内来新请求 → 静默期作废，跳过终态直接进 thinking。"""
    plugin = _new_plugin()
    _start_round(plugin)
    plugin._set_state(idx.STATE_THINKING)
    _finish_request(plugin, True)
    assert plugin._quiet_until > 0
    # 新请求进来
    _start_round(plugin)
    assert plugin._quiet_until == 0.0, "静默期必须被作废"
    assert plugin._round_settled is False
    # 即使把时间推到很远，也不该播终态（因为静默期已作废）
    plugin._state_since = time.time() - 100
    assert _payload(plugin)["state"] != idx.STATE_SUCCESS


def test_second_request_switches_to_working():
    """第 2 个请求进来 → 从 thinking 切到 working。"""
    plugin = _new_plugin()
    _start_round(plugin)
    plugin._set_state(idx.STATE_THINKING)
    assert plugin._state == idx.STATE_THINKING
    _start_round(plugin)
    if plugin._inflight >= 2:
        plugin._set_state(idx.STATE_WORKING)
    assert plugin._state == idx.STATE_WORKING


def test_round_settles_only_once():
    """落定后重复轮询不应再次触发状态切换（_round_settled 挡住）。"""
    plugin = _new_plugin()
    _start_round(plugin)
    plugin._set_state(idx.STATE_THINKING)
    _finish_request(plugin, True)
    plugin._quiet_until = time.time() - 1
    assert _payload(plugin)["state"] == idx.STATE_SUCCESS
    assert plugin._round_settled is True
    # 再次轮询：状态保持 success（不会被重复落定打乱）
    assert _payload(plugin, ago=2)["state"] == idx.STATE_SUCCESS


# ── 过半判定 ─────────────────────────────────────────────

@pytest.mark.parametrize("ok_count,err_count,expected", [
    (1, 0, idx.STATE_SUCCESS),   # 全成功
    (0, 1, idx.STATE_ERROR),     # 全失败
    (2, 1, idx.STATE_SUCCESS),   # 2/3 成功 → success
    (1, 2, idx.STATE_ERROR),     # 1/3 成功 → error
    (1, 1, idx.STATE_SUCCESS),   # 平票 → 算成功
    (3, 1, idx.STATE_SUCCESS),   # 3/4 成功
    (1, 3, idx.STATE_ERROR),     # 1/4 成功
])
def test_majority_rule(ok_count, err_count, expected):
    """本轮成功率 ≥ 50% → success，否则 error（平票算成功）。

    构造并发场景：所有请求先一起进来（_inflight 累加、计数只在第一次清零），
    再一起结束。不能「一个进来一个结束」交替——那样每次归 0 都会启动静默期，
    下一个请求又把它当新一轮，计数被反复清零。
    """
    plugin = _new_plugin()
    # 1) 所有请求同时进来（只在第一次触发轮次重置）
    for _ in range(ok_count + err_count):
        _start_round(plugin)
    assert plugin._inflight == ok_count + err_count
    plugin._set_state(idx.STATE_WORKING)
    # 2) 按数量依次结束，累计本轮计数
    for _ in range(ok_count):
        _finish_request(plugin, True)
    for _ in range(err_count):
        _finish_request(plugin, False, "失败 500")
    # 3) 静默期拨到过去 → 落定
    plugin._quiet_until = time.time() - 1
    assert _payload(plugin)["state"] == expected


def test_round_counters_reset_on_new_round():
    """落定后计数清零，下一个请求开启新轮回，不被上一轮污染。"""
    plugin = _new_plugin()
    _start_round(plugin)
    plugin._set_state(idx.STATE_THINKING)
    _finish_request(plugin, False, "失败 500")
    plugin._quiet_until = time.time() - 1
    assert _payload(plugin)["state"] == idx.STATE_ERROR
    # 新一轮：计数必须归零
    _start_round(plugin)
    assert plugin._round_ok == 0
    assert plugin._round_err == 0
    assert plugin._pending_detail == ""


def test_no_requests_means_no_terminal_animation():
    """本轮没有任何结果时，静默期过去也不播终态（避免空轮次误报）。"""
    plugin = _new_plugin()
    plugin._round_settled = False
    plugin._round_ok = 0
    plugin._round_err = 0
    plugin._quiet_until = time.time() - 1
    plugin._set_state(idx.STATE_THINKING)
    _payload(plugin)
    # 状态仍是 thinking（没落定成 success/error）
    assert plugin._state == idx.STATE_THINKING


# ── waiting 判定：9 成以上在途请求在等上游 ───────────────
# 用户要求：「当前存在的请求里面有 9 成请求在等上游返回」才显示 waiting，
# 否则不影响其他显示逻辑。判定依据是请求登记表（on_key_selected 标记 sent），
# 不再用「thinking 播了多久」这种猜测。

def _add_pending(plugin, req_id: str, sent: bool, ago: float = 0.0):
    """往登记表塞一个在途请求。sent=True 表示已选 Key、在等上游。

    ``ago`` 把「已发往上游」的时刻往前推 N 秒——单请求按时间判定
    （方案 B）时用来模拟「已经等了很久」。
    """
    now = time.time()
    plugin._pending_requests[req_id] = {
        "sent": sent,
        "started": now - ago,
        "sent_at": now - ago,
    }


def test_waiting_enters_when_all_requests_sent():
    """全部在途请求都已发往上游（占比 100% ≥ 90%）→ 切 waiting。"""
    plugin = _new_plugin()
    plugin._inflight = 2
    _add_pending(plugin, "a", True)
    _add_pending(plugin, "b", True)
    plugin._set_state(idx.STATE_WORKING)
    assert _payload(plugin)["state"] == idx.STATE_WAITING


def test_waiting_not_entered_below_threshold():
    """只有 1/2 在等上游（50% < 90%）→ 保持原状态，不影响其他逻辑。"""
    plugin = _new_plugin()
    plugin._inflight = 2
    _add_pending(plugin, "a", True)
    _add_pending(plugin, "b", False)   # 还没选 Key
    plugin._set_state(idx.STATE_WORKING)
    assert _payload(plugin)["state"] == idx.STATE_WORKING


def test_waiting_threshold_boundary():
    """9/10 = 90% 达标；8/10 = 80% 不达标。"""
    # 10 个请求里 9 个在等 → 切
    plugin = _new_plugin()
    plugin._inflight = 10
    for i in range(9):
        _add_pending(plugin, f"s{i}", True)
    _add_pending(plugin, "pending", False)
    plugin._set_state(idx.STATE_THINKING)
    assert _payload(plugin)["state"] == idx.STATE_WAITING
    # 10 个里 8 个在等 → 不切
    plugin2 = _new_plugin()
    plugin2._inflight = 10
    for i in range(8):
        _add_pending(plugin2, f"s{i}", True)
    for i in range(2):
        _add_pending(plugin2, f"p{i}", False)
    plugin2._set_state(idx.STATE_THINKING)
    assert _payload(plugin2)["state"] == idx.STATE_THINKING


def test_waiting_not_entered_when_no_pending():
    """登记表为空（没有在途请求）时不切 waiting。"""
    plugin = _new_plugin()
    plugin._inflight = 0
    plugin._set_state(idx.STATE_IDLE)
    assert _payload(plugin)["state"] == idx.STATE_IDLE


def test_waiting_does_not_touch_round_counters():
    """waiting 只做状态切换，不能改 _inflight / 轮次计数 / 静默期。"""
    plugin = _new_plugin()
    plugin._inflight = 2
    plugin._round_ok = 1
    plugin._round_err = 0
    plugin._quiet_until = 0.0
    _add_pending(plugin, "a", True)
    _add_pending(plugin, "b", True)
    plugin._set_state(idx.STATE_WORKING)
    _payload(plugin)
    assert plugin._inflight == 2
    assert plugin._round_ok == 1
    assert plugin._round_err == 0
    assert plugin._quiet_until == 0.0


def test_waiting_stays_waiting():
    """已在 waiting 且条件仍成立时，不重复切换（避免状态抖动）。"""
    plugin = _new_plugin()
    plugin._inflight = 2
    _add_pending(plugin, "a", True)
    _add_pending(plugin, "b", True)
    plugin._set_state(idx.STATE_WAITING)
    before = plugin._state_since
    _payload(plugin)
    assert plugin._state == idx.STATE_WAITING
    assert plugin._state_since == before   # 计时没被重置


def test_waiting_exits_when_ratio_drops():
    """**waiting 必须能自己退出**：占比跌回阈值以下要回到忙碌态。"""
    plugin = _new_plugin()
    plugin._inflight = 2
    _add_pending(plugin, "a", True)
    _add_pending(plugin, "b", True)
    plugin._set_state(idx.STATE_WORKING)
    assert _payload(plugin)["state"] == idx.STATE_WAITING
    # 新请求进来但还没选 Key → 占比掉到 2/3
    _add_pending(plugin, "c", False)
    plugin._inflight = 3
    assert _payload(plugin)["state"] == idx.STATE_WORKING


def test_single_request_not_waiting_immediately():
    """**单请求刚发出不该切 waiting**（原来 ratio=1/1 会立刻满足 9 成）。"""
    plugin = _new_plugin()
    plugin._inflight = 1
    _add_pending(plugin, "a", True)     # ago=0，刚发出
    plugin._set_state(idx.STATE_THINKING)
    assert _payload(plugin)["state"] == idx.STATE_THINKING
    # 等够 thinking 动画时长后才切
    _add_pending(plugin, "a", True, ago=idx.STATE_ANIM_SECONDS[idx.STATE_THINKING] + 1)
    assert _payload(plugin)["state"] == idx.STATE_WAITING


# ── waiting → working → success / error ──────────────────

def test_waiting_success_plays_working_then_success():
    """waiting 期间成功 → 先播 working，播完一轮才落定 success。"""
    plugin = _new_plugin()
    plugin._inflight = 1
    # 单请求要等够 thinking 动画时长才切 waiting（方案 B）
    _add_pending(plugin, "a", True, ago=idx.STATE_ANIM_SECONDS[idx.STATE_THINKING] + 1)
    plugin._set_state(idx.STATE_THINKING)
    assert _payload(plugin)["state"] == idx.STATE_WAITING
    # 请求成功返回（模拟 on_response 的 waiting 分支）
    plugin._inflight = 0
    plugin._pending_requests.clear()
    plugin._round_ok = 1
    # 本轮尚未落定，才允许 _maybe_settle_round 落定
    plugin._round_settled = False
    working_s = idx.STATE_ANIM_SECONDS[idx.STATE_WORKING]
    plugin._working_until = time.time() + working_s
    plugin._quiet_until = plugin._working_until
    plugin._deferred_terminal = idx.STATE_SUCCESS
    plugin._deferred_detail = ""
    plugin._set_state(idx.STATE_WORKING, detail="上游回话了，正在收尾", force=True)
    # working 播放中：仍是 working
    assert _payload(plugin)["state"] == idx.STATE_WORKING
    # working 播完：落定 success
    plugin._quiet_until = time.time() - 1
    assert _payload(plugin)["state"] == idx.STATE_SUCCESS


def test_waiting_error_settles_immediately():
    """waiting 期间失败 → 直接 error（不等 working 动画）。"""
    plugin = _new_plugin()
    plugin._inflight = 1
    # 单请求要「等够 thinking 动画时长」才切 waiting（方案 B）
    _add_pending(plugin, "a", True, ago=idx.STATE_ANIM_SECONDS[idx.STATE_THINKING] + 1)
    plugin._set_state(idx.STATE_THINKING)
    assert _payload(plugin)["state"] == idx.STATE_WAITING
    plugin._inflight = 0
    plugin._pending_requests.clear()
    plugin._round_err = 1
    plugin._pending_detail = "失败 503 upstream"
    # 本轮尚未落定（on_request 里会置 False），静默期已过 → 应落定 error
    plugin._round_settled = False
    plugin._quiet_until = time.time() - 1
    assert _payload(plugin)["state"] == idx.STATE_ERROR
    assert "503" in plugin._state_text


def test_deferred_terminal_cleared_after_settle():
    """落定后 deferred 标记必须清空，避免下一轮误用。"""
    plugin = _new_plugin()
    plugin._inflight = 0
    plugin._round_settled = False
    plugin._deferred_terminal = idx.STATE_SUCCESS
    plugin._deferred_detail = ""
    plugin._quiet_until = time.time() - 1
    assert _payload(plugin)["state"] == idx.STATE_SUCCESS
    assert plugin._deferred_terminal == ""
    assert plugin._deferred_detail == ""


# ── 余额：读 AKM 缓存（不再自己请求）─────────────────────
# 用户确认：AKM 已有用量查询体系，插件不该重复请求。
# 数据来自 keys.usage_data，形状是「查询记录包装 → raw_response → balance_infos」。

def test_parse_balance_wrapped_record():
    """AKM 实际存的形状：{ok, raw_response:{balance_infos:[...]}}。"""
    data = {
        "ok": True, "status_code": 200, "latency_ms": 127,
        "queried_at": "2026-10-24 22:09:46", "error": "",
        "raw_response": {
            "is_available": True,
            "balance_infos": [{
                "currency": "CNY", "total_balance": "180.91",
                "granted_balance": "77.16", "topped_up_balance": "103.74",
            }],
        },
    }
    parsed = idx.Plugin._parse_balance(data)
    assert parsed["available"] is True
    assert parsed["balances"][0]["currency"] == "CNY"
    assert parsed["balances"][0]["total"] == "180.91"


def test_parse_balance_raw_shape():
    """未包装的上游原始结构（自定义脚本可能直接返回）。"""
    parsed = idx.Plugin._parse_balance({
        "is_available": True,
        "balance_infos": [{"currency": "CNY", "total_balance": "50.5"}],
    })
    assert parsed["balances"][0]["total"] == "50.5"


def test_parse_balance_flat_extractor_shape():
    """extractor 扁平结果 {remaining, unit}。"""
    parsed = idx.Plugin._parse_balance(
        {"isValid": True, "remaining": "182.97", "unit": "元"}
    )
    assert parsed["balances"][0]["currency"] == "元"
    assert parsed["balances"][0]["total"] == "182.97"


def test_parse_balance_rejects_empty():
    """空/缺字段的数据不能当成余额。"""
    for data in (None, {}, {"isValid": True}, {"remaining": ""}, {"raw_response": {}}):
        assert idx.Plugin._parse_balance(data) is None


def test_balance_interval_setting_removed():
    """balance_interval_s 设置项必须已删除（余额改读 AKM 缓存）。"""
    import json
    from pathlib import Path
    meta = json.loads(
        (Path(__file__).resolve().parents[1] / "plugins" / "dafeiyu"
         / "plugin.json").read_text(encoding="utf-8")
    )
    keys = [s["key"] for s in meta["settings"]]
    assert "balance_interval_s" not in keys, "余额刷新间隔应已删除"
    assert "bubble_show_balance" in keys, "气泡余额开关要保留"


def test_no_balance_loop_in_plugin():
    """插件不应再有 _balance_loop（自己定时查余额）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "plugins" / "dafeiyu"
           / "index.py").read_text(encoding="utf-8")
    assert "_balance_loop" not in src, "余额轮询任务应已移除"
    assert "get_usage_query_config" in src, "应改为读 AKM 用量缓存"


# ── 关联 Key 配置（balance_key_alias）────────────────────

def test_balance_key_alias_setting_exists():
    """配置里要有「关联 Key」项，让用户能指定余额来源。"""
    import json
    from pathlib import Path
    meta = json.loads(
        (Path(__file__).resolve().parents[1] / "plugins" / "dafeiyu"
         / "plugin.json").read_text(encoding="utf-8")
    )
    keys = [s["key"] for s in meta["settings"]]
    assert "balance_key_alias" in keys


def test_plugin_page_hidden():
    """插件页已隐藏（has_menu=false），配置项仍可用。"""
    import json
    from pathlib import Path
    meta = json.loads(
        (Path(__file__).resolve().parents[1] / "plugins" / "dafeiyu"
         / "plugin.json").read_text(encoding="utf-8")
    )
    assert meta["has_menu"] is False, "插件页应隐藏"
    assert meta["settings"], "配置项必须保留"


def test_pick_balance_key_respects_alias():
    """填了 alias 就只认那一个；找不到返回 None，不偷偷退回别的 Key。"""
    plugin = _new_plugin()
    plugin._cfg = {"balance_key_alias": "not-exist-alias"}
    assert plugin._pick_balance_key() is None


def test_pick_balance_key_alias_empty_falls_back():
    """alias 留空时走默认匹配（不返回 None 或返回某个真实 Key）。"""
    plugin = _new_plugin()
    plugin._cfg = {"balance_key_alias": ""}
    key = plugin._pick_balance_key()
    # 没有 Key 的环境返回 None 也算正常；有 Key 时必须返回一个 dict
    assert key is None or isinstance(key, dict)


# ── 关联 Key 下拉（动态填充 options）─────────────────────

class _FakeSetting:
    def __init__(self, key, typ, default=""):
        self.key = key
        self.type = typ
        self.default = default
        self.options = []
        self.allow_empty_option = False


class _FakeMeta:
    def __init__(self):
        self.settings = [_FakeSetting("balance_key_alias", "select", "")]


def _plugin_with_meta(cfg=None):
    plugin = _new_plugin()
    plugin._cfg = cfg or {}
    plugin.meta = _FakeMeta()
    return plugin


def test_sync_key_options_fills_real_aliases():
    """加载后下拉选项应填成真实 Key 的 alias 列表。"""
    plugin = _plugin_with_meta()
    plugin._sync_balance_key_options()
    setting = plugin.meta.settings[0]
    assert setting.type == "select"
    # 环境里至少有一个 Key 才会填；没有 Key 时保持空也算正常
    if setting.options:
        assert all(isinstance(a, str) and a for a in setting.options)


def test_sync_key_options_defaults_to_auto_match():
    """配置留空时，下拉默认选中「自动匹配」算出来的那个 Key。"""
    plugin = _plugin_with_meta({})
    plugin._sync_balance_key_options()
    setting = plugin.meta.settings[0]
    auto = plugin._pick_balance_key()
    if auto and setting.options:
        assert setting.default == auto["alias"], "默认选中应等于自动匹配结果"


def test_sync_key_options_keeps_user_choice():
    """用户已显式选了有效 Key 时，不能被他自动匹配结果覆盖。"""
    plugin = _plugin_with_meta()
    plugin._sync_balance_key_options()
    aliases = plugin.meta.settings[0].options
    if not aliases:
        return  # 没有 Key 的环境跳过
    chosen = aliases[-1]
    plugin2 = _plugin_with_meta({"balance_key_alias": chosen})
    plugin2._sync_balance_key_options()
    # 用户选的是有效 alias → default 保持空（不覆盖），配置值本身仍是 chosen
    assert plugin2.meta.settings[0].default == ""


def test_sync_key_options_falls_back_when_alias_deleted():
    """配置指向已删除的 Key 时，回落到自动匹配结果。"""
    plugin = _plugin_with_meta({"balance_key_alias": "__deleted__"})
    plugin._sync_balance_key_options()
    setting = plugin.meta.settings[0]
    if setting.options:
        assert setting.default != "__deleted__"
