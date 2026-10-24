"""桌宠物理模块单测。

重点验证三件事：
1. **与原版常量一致**：重力/弹性/摩擦/阈值等数值直接来自 dsh-pet 的 physics.ts，
   这里钉住，避免后续「顺手调一下」把两处手感调歪。
2. **曲线性质正确**：Q 弹曲线压缩到底、回弹过冲、收敛到 1；软钳速单调且不超上限。
3. **边界行为正确**：反弹方向、地面摩擦、多屏空洞当墙、锁定屏。

不依赖 AppKit，纯函数测试。
"""

import math
import sys
from pathlib import Path

import pytest

# 桌宠物理模块不在 akm 包内，按文件路径导入
_PET_DIR = Path(__file__).resolve().parents[1] / "plugins" / "dafeiyu" / "pet"
if str(_PET_DIR) not in sys.path:
    sys.path.insert(0, str(_PET_DIR))

import physics  # noqa: E402


# ── 常量与原版一致 ─────────────────────────────────────────

def test_constants_match_upstream():
    """这些数值与 dsh-pet/src/shared/physics.ts 逐条对应。"""
    assert physics.SPRING_K == 200.0
    assert physics.SPRING_C == 30.0
    assert physics.GRAVITY == 1400.0
    assert physics.RESTITUTION == 0.78
    assert physics.GROUND_FRICTION == 2.5
    assert physics.MAX_THROW_SPEED == 3600.0
    assert physics.THROW_SPEED_ANCHOR == 1200.0
    assert physics.REST_VY == 40.0
    assert physics.REST_VX == 15.0
    assert physics.MAX_STEP_DT == 0.05
    assert physics.SQ_SQUASH == 0.55
    assert physics.SQ_DURATION_MS == 220.0
    assert physics.SQ_SOFT_SPEED == 300.0
    assert physics.SQ_HARD_SPEED == 1500.0
    assert physics.SQ_MAX_SQUASH == 0.55


# ── 弹簧 ───────────────────────────────────────────────────

def test_spring_converges_to_target():
    """弹簧最终收敛到目标位置（不震荡发散）。"""
    v, x, target = 0.0, 0.0, 100.0
    dt = 1 / 60
    for _ in range(600):
        v = physics.spring_step(v, x, target, dt)
        x += v * dt
    assert abs(x - target) < 1.0


def test_spring_pulls_back_when_past_target():
    """超过目标点时弹簧往回拉（速度为负）。"""
    v = physics.spring_step(0.0, 150.0, 100.0, 1 / 60)
    assert v < 0


def test_spring_is_undershot():
    """欠阻尼：一步之后位置还没到目标（是「跟手」而不是「瞬移」）。"""
    v = 0.0
    x = 0.0
    dt = 1 / 60
    v = physics.spring_step(v, x, 100.0, dt)
    x += v * dt
    assert 0 < x < 100


# ── 软钳速 ─────────────────────────────────────────────────

def test_soft_clamp_never_exceeds_cap():
    """任意输入都不超过软上限（极端输入会渐近到上限，用 <= 判定）。"""
    assert physics.soft_clamp_speed(0) == 0
    for speed in (1, 100, 1200, 10000, 1e9):
        assert physics.soft_clamp_speed(speed) <= physics.MAX_THROW_SPEED


def test_soft_clamp_is_monotonic():
    """单调可区分：甩得越猛，初速越大。"""
    values = [physics.soft_clamp_speed(s) for s in (100, 500, 1200, 2400)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


# ── 抛掷初速 ───────────────────────────────────────────────

def test_throw_velocity_up_is_negative_vy():
    """角度 90 = 往上抛，屏幕 y 向下，所以 vy 必须为负。"""
    vx, vy = physics.throw_velocity(90, 1.0)
    assert abs(vx) < 1e-6
    assert vy < 0


def test_throw_velocity_right_is_positive_vx():
    vx, vy = physics.throw_velocity(0, 1.0)
    assert vx > 0
    assert abs(vy) < 1e-6


def test_throw_velocity_power_scales():
    """power 越大初速越大，但都受软上限约束。"""
    v1 = physics.throw_velocity(0, 0.5)[0]
    v2 = physics.throw_velocity(0, 1.0)[0]
    v3 = physics.throw_velocity(0, 5.0)[0]
    assert v1 < v2 < v3
    assert v3 < physics.MAX_THROW_SPEED


# ── 抛掷边界与步进 ─────────────────────────────────────────

def test_throw_bounds_body_hugs_edge():
    """side_allow 让身体能贴到屏幕边缘（包围盒可略微越界）。"""
    b = physics.throw_bounds(1000, 800, 200, side_allow=30)
    assert b.minX == -30
    assert b.maxX == 1000 - 200 + 30
    # 高度按 16:9 由宽度推算
    assert b.maxY == 800 - 200 * 9 / 16


def test_throw_step_gravity_pulls_down():
    """自由落体：y 增大、vy 增大。"""
    b = physics.throw_bounds(1000, 800, 200)
    state = physics.ThrowState(x=400, y=0, vx=0, vy=0)
    result = physics.throw_step(state, 0.016, b)
    assert result.y > 0
    assert result.vy > 0
    assert not result.bounced


def test_throw_step_bounces_off_right_edge():
    """撞右边缘：夹回边界内、vx 反向且衰减。"""
    b = physics.throw_bounds(1000, 800, 200)
    state = physics.ThrowState(x=b.maxX - 1, y=100, vx=2000, vy=0)
    result = physics.throw_step(state, 0.016, b)
    assert result.x <= b.maxX
    assert result.vx < 0
    assert abs(result.vx) < 2000  # 有衰减
    assert result.bounced


def test_throw_step_bounces_off_left_edge():
    b = physics.throw_bounds(1000, 800, 200)
    state = physics.ThrowState(x=b.minX + 1, y=100, vx=-2000, vy=0)
    result = physics.throw_step(state, 0.016, b)
    assert result.x >= b.minX
    assert result.vx > 0
    assert result.bounced


def test_throw_step_ground_friction_slows_horizontal():
    """落地后水平速度被摩擦衰减。"""
    b = physics.throw_bounds(1000, 800, 200)
    state = physics.ThrowState(x=300, y=b.maxY, vx=1000, vy=0)
    result = physics.throw_step(state, 0.016, b)
    assert result.y == b.maxY
    assert abs(result.vx) < 1000


def test_throw_step_reaches_rest():
    """反复步进最终会静止（不会永远弹）。"""
    b = physics.throw_bounds(1000, 800, 200)
    state = physics.ThrowState(x=400, y=b.maxY, vx=800, vy=0)
    for _ in range(3000):
        result = physics.throw_step(state, 1 / 60, b)
        state = physics.ThrowState(x=result.x, y=result.y, vx=result.vx, vy=result.vy)
        if result.atRest:
            break
    assert result.atRest


def test_throw_step_ceiling_disabled_lets_pet_leave_top():
    """ceilingBounce=False：顶部不夹，宠物可以飞出屏幕上方再落回。"""
    b = physics.throw_bounds(1000, 800, 200)
    state = physics.ThrowState(x=400, y=10, vx=0, vy=-3000)
    result = physics.throw_step(state, 0.016, b)
    assert result.y < b.minY
    assert result.vy < 0


def test_throw_step_dt_is_clamped():
    """超大 dt 被夹到 MAX_STEP_DT，不会一步穿墙。"""
    b = physics.throw_bounds(1000, 800, 200)
    state = physics.ThrowState(x=500, y=100, vx=10000, vy=0)
    result = physics.throw_step(state, 5.0, b)
    # 若未夹 dt，位移会是 50000px，早飞出边界；夹住后仍在边界内
    assert b.minX <= result.x <= b.maxX


# ── 多屏 ───────────────────────────────────────────────────

def _two_screens():
    """左右并排两块屏，中间无缝。"""
    left = physics.Rect(0, 0, 1000, 800)
    right = physics.Rect(1000, 0, 1000, 800)
    return [left, right]


def test_throw_space_builds_bounds_per_screen():
    areas = _two_screens()
    space = physics.throw_space(areas, size=200)
    assert len(space.bounds) == 2
    assert space.bounds[0].maxX == 1000 - 200


def test_throw_step_region_crosses_screen_seam():
    """屏缝两侧都有屏 ⇒ 放行，不反弹（可以跨屏飞）。"""
    areas = _two_screens()
    space = physics.throw_space(areas, size=200)
    # 在左屏右缘、向右飞
    state = physics.ThrowState(x=space.bounds[0].maxX - 1, y=100, vx=3000, vy=0)
    result = physics.throw_step_region(state, 0.016, space)
    assert result.vx > 0  # 未反向
    assert not result.bounced


def test_throw_step_region_bounces_at_outer_edge():
    """最外侧没有屏 ⇒ 反弹。"""
    areas = _two_screens()
    space = physics.throw_space(areas, size=200)
    # 在右屏右缘、继续向右飞
    state = physics.ThrowState(x=space.bounds[1].maxX - 1, y=100, vx=3000, vy=0)
    result = physics.throw_step_region(state, 0.016, space)
    assert result.vx < 0
    assert result.bounced


def test_throw_step_region_lock_screen_keeps_pet_inside():
    """锁定屏时，屏缝当墙——甩不出去。"""
    areas = _two_screens()
    space = physics.throw_space(areas, size=200)
    state = physics.ThrowState(x=space.bounds[0].maxX - 1, y=100, vx=3000, vy=0)
    result = physics.throw_step_region(state, 0.016, space, lock_screen=0)
    assert result.vx < 0
    assert result.bounced


def test_throw_step_region_handles_empty_space():
    """没有任何屏时不做边界处理，不崩。"""
    space = physics.ThrowSpace(areas=[], panels=[], bounds=[], size=200)
    state = physics.ThrowState(x=0, y=0, vx=100, vy=100)
    result = physics.throw_step_region(state, 0.016, space)
    assert result.screen == -1


def test_throw_step_region_lock_screen_out_of_range_falls_back():
    """锁定索引越界（拔掉显示器）时退回不锁定，不崩。"""
    areas = _two_screens()
    space = physics.throw_space(areas, size=200)
    state = physics.ThrowState(x=space.bounds[0].maxX - 1, y=100, vx=3000, vy=0)
    result = physics.throw_step_region(state, 0.016, space, lock_screen=99)
    assert result is not None


# ── Q 弹挤压 ───────────────────────────────────────────────

def test_squash_scale_starts_at_one():
    assert physics.squash_scale(0.0) == pytest.approx(1.0)


def test_squash_scale_compresses_at_bottom():
    """下压段最低点压到 squash 值。"""
    assert physics.squash_scale(0.45, squash=0.55) == pytest.approx(0.55, abs=1e-6)


def test_squash_scale_overshoots_on_rebound():
    """回弹段有轻微过冲（> 1），这是 Q 弹的关键。"""
    peak = max(physics.squash_scale(u / 100, squash=0.55) for u in range(46, 101))
    assert peak > 1.0


def test_squash_scale_converges_to_one():
    assert physics.squash_scale(1.0) == pytest.approx(1.0, abs=1e-6)


def test_squash_scale_capped():
    """过冲不超过 1.12，避免拉伸过头。"""
    for u in range(0, 101):
        assert physics.squash_scale(u / 100) <= 1.12


def test_landing_squash_soft_impact_light_compression():
    """轻落地只压到 0.8（原版 `min(0.8, ...)` 的上限语义），不会压满。"""
    assert physics.landing_squash(0) == pytest.approx(0.8)
    assert physics.landing_squash(physics.SQ_SOFT_SPEED) == pytest.approx(0.8)


def test_landing_squash_hard_impact_max_compression():
    """重落地压到最大值。"""
    assert physics.landing_squash(physics.SQ_HARD_SPEED) == pytest.approx(physics.SQ_MAX_SQUASH)


def test_landing_squash_monotonic():
    values = [physics.landing_squash(s) for s in (300, 600, 900, 1200, 1500)]
    assert values == sorted(values, reverse=True)


# ── 松手速度 ───────────────────────────────────────────────

def test_release_velocity_empty_trail():
    assert physics.release_velocity([], 1000) == (0.0, 0.0)


def test_release_velocity_single_sample():
    trail = [physics.DragSample(t=1000, x=0, y=0)]
    assert physics.release_velocity(trail, 1000) == (0.0, 0.0)


def test_release_velocity_slow_drop_is_zero():
    """慢速放下 → (0,0)，即「温柔放下原地停住」。"""
    trail = [
        physics.DragSample(t=1000, x=0, y=0),
        physics.DragSample(t=1100, x=10, y=10),  # 100ms 走 14px ≈ 140px/s < 500
    ]
    assert physics.release_velocity(trail, 1100) == (0.0, 0.0)


def test_release_velocity_fast_flick_is_nonzero():
    """快速甩出 → 有速度，且方向正确。"""
    trail = [
        physics.DragSample(t=1000, x=0, y=0),
        physics.DragSample(t=1100, x=300, y=0),
    ]
    vx, vy = physics.release_velocity(trail, 1100)
    assert vx > 0
    assert abs(vy) < 1e-6


def test_release_velocity_stale_trail_is_zero():
    """采样太旧（松手前停顿了）→ 不算甩抛。"""
    trail = [
        physics.DragSample(t=1000, x=0, y=0),
        physics.DragSample(t=1100, x=500, y=0),
    ]
    assert physics.release_velocity(trail, 2000) == (0.0, 0.0)


def test_trim_trail_drops_old_samples():
    trail = [
        physics.DragSample(t=0, x=0, y=0),
        physics.DragSample(t=1000, x=1, y=1),
    ]
    kept = physics.trim_trail(trail, 1000)
    assert len(kept) == 1
    assert kept[0].t == 1000


# ── 屏幕漫游 ───────────────────────────────────────────────

def test_plan_move_within_single_screen():
    """单屏内正常规划出落点。"""
    target = physics.plan_move(
        cx=500, cy=400, W=1000, H=800,
        direction=1, min_dist=50, max_dist=150,
        margin=40, half_w=100, distance=100,
    )
    assert target == 600


def test_plan_move_rejects_out_of_bounds():
    """越界返回 None（调用方应换方向）。"""
    target = physics.plan_move(
        cx=900, cy=400, W=1000, H=800,
        direction=1, min_dist=50, max_dist=150,
        margin=40, half_w=100, distance=500,
    )
    assert target is None


def test_plan_move_multi_screen_allows_seam_walk():
    """多屏并集判定：骑缝行走允许（两块屏都命中）。"""
    areas = _two_screens()
    target = physics.plan_move(
        cx=990, cy=400, W=2000, H=800,
        direction=1, min_dist=10, max_dist=20,
        margin=5, half_w=50, distance=20,
        areas=areas,
    )
    assert target is not None


def test_plan_move_multi_screen_rejects_gap():
    """多屏之间的空洞走不进去。"""
    left = physics.Rect(0, 0, 1000, 800)
    # 右屏在 1200 之后，中间 200px 空洞
    right = physics.Rect(1200, 0, 800, 800)
    areas = [left, right]
    target = physics.plan_move(
        cx=900, cy=400, W=2000, H=800,
        direction=1, min_dist=100, max_dist=100,
        margin=5, half_w=50, distance=100,
        areas=areas,
    )
    assert target is None
