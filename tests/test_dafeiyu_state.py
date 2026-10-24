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

def _add_pending(plugin, req_id: str, sent: bool):
    """往登记表塞一个在途请求。sent=True 表示已选 Key、在等上游。"""
    plugin._pending_requests[req_id] = {
        "sent": sent,
        "started": time.time(),
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
    """已在 waiting 时不重复切换（避免状态抖动）。"""
    plugin = _new_plugin()
    plugin._inflight = 1
    _add_pending(plugin, "a", True)
    plugin._set_state(idx.STATE_WAITING)
    before = plugin._state_since
    _payload(plugin)
    assert plugin._state == idx.STATE_WAITING
    assert plugin._state_since == before   # 计时没被重置


# ── waiting → working → success / error ──────────────────

def test_waiting_success_plays_working_then_success():
    """waiting 期间成功 → 先播 working，播完一轮才落定 success。"""
    plugin = _new_plugin()
    plugin._inflight = 1
    _add_pending(plugin, "a", True)
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
    _add_pending(plugin, "a", True)
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
