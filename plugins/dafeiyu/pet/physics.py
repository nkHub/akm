"""桌宠物理：甩抛 / Q 弹挤压 / 阻尼弹簧 —— 纯函数，无 AppKit 依赖。

移植自 PC2005-cloud/dsh-pet 的 ``src/shared/physics.ts``（两端共用的纯函数物理）。
**只搬数学，不搬平台代码**：原版是 TS（浏览器 overlay + 桌面 sprite 共用），
这里是 Python（AKM 桌宠子进程用），函数语义、常量、边界条件逐条对齐，
因此同一套手感参数在两处表现一致。

为什么单独成文件而不是塞进 pet_app.py：
1. 纯函数可单测（``tests/test_dafeiyu_physics.py``），不需要起窗口；
2. 物理与渲染解耦——窗口层只负责「每帧问一次下一步在哪」。

坐标系约定（与原版一致）：
- 单位 px、秒；x 向右、y **向下**（屏幕坐标），因此「向上抛」= vy 为负；
- ``ThrowState`` 的 (x, y) 是**包围盒左上角**，不是中心；
- 高度按 16:9 由宽度推算（``h = size * 9 / 16``），与原版 sprite 比例一致。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ── 弹簧（拖拽跟手）────────────────────────────────────────
# 欠阻尼弹簧：K 越大越硬、C 越大越黏。原版参数直接照搬。
SPRING_K = 200.0
SPRING_C = 30.0

# ── 甩抛 ───────────────────────────────────────────────────
GRAVITY = 1400.0          # px/s²
RESTITUTION = 0.78        # 边缘/落地反弹系数
GROUND_FRICTION = 2.5     # 落地后水平速度衰减（每秒）
MAX_THROW_SPEED = 3600.0  # 软上限：初速永不超过它
THROW_SPEED_ANCHOR = 1200.0  # power=1 对应软钳速前的速度
REST_VY = 40.0            # 竖直速度低于此值视为贴地
REST_VX = 15.0            # 水平速度低于此值视为静止
MAX_STEP_DT = 0.05        # 单步 dt 上限，防止掉帧时穿墙

# 轨迹采样：只保留最近一段，用于估算松手速度
TRAIL_KEEP_MS = 200.0
RELEASE_WINDOW_MS = 150.0
RELEASE_STALE_MS = 150.0
MIN_SPAN_MS = 20.0
DEAD_ZONE_SPEED = 500.0   # 低于此速度视为「温柔放下」，不甩
PEAK_WEIGHT = 0.5

# ── Q 弹挤压 ───────────────────────────────────────────────
SQ_SQUASH = 0.55          # 最大压缩到 0.55（scaleY）
SQ_DURATION_MS = 220.0    # 一次挤压总时长
SQ_SOFT_SPEED = 300.0     # 落地速度低于此值不挤压
SQ_HARD_SPEED = 1500.0    # 达到此速度即最大挤压
SQ_MAX_SQUASH = 0.55


@dataclass
class PhysicsParams:
    """一套可调物理参数（对应原版 ``PhysicsParams``）。"""

    gravity: float = GRAVITY
    restitution: float = RESTITUTION
    groundFriction: float = GROUND_FRICTION
    # 顶部是否有边界。原版默认 False：宠物可以飞出屏幕顶部，靠重力落回。
    ceilingBounce: bool = False
    throwPower: float = 1.0


DEFAULT_PHYSICS = PhysicsParams()


@dataclass
class Rect:
    """矩形（屏幕工作区 / 面板），坐标同桌面坐标系。"""

    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def contains(self, px: float, py: float) -> bool:
        """点是否落在矩形内（左闭右开，与原版 ``rectAtPoint`` 一致）。"""
        return self.x <= px < self.right and self.y <= py < self.bottom


@dataclass
class ThrowBounds:
    """抛掷边界：包围盒左上角的允许范围（身体贴边才反弹）。"""

    minX: float
    minY: float
    maxX: float
    maxY: float


@dataclass
class ThrowState:
    """抛体状态：包围盒左上角 px + 速度 px/s。"""

    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0


@dataclass
class ThrowStepResult:
    """单步结果：新状态 + 是否碰撞 + 是否静止 + 所在屏索引。"""

    x: float
    y: float
    vx: float
    vy: float
    bounced: bool = False
    atRest: bool = False
    screen: int = -1


@dataclass
class DragSample:
    """一次拖拽采样（t = 毫秒时间戳，x/y = 指针绝对坐标）。"""

    t: float
    x: float
    y: float


# ── 弹簧 ───────────────────────────────────────────────────

def spring_step(v: float, x: float, target: float, dt: float, power: float = 1.0) -> float:
    """欠阻尼弹簧一步：返回新的速度。

    ``v' = v + ((target - x) * K - v * C) * power * dt``

    调用方用 ``x += v' * dt`` 推进位置。这是原版拖拽「过阻尼弹簧跟手」的
    同一个公式——窗口位置永远在追鼠标，而不是硬跟，所以有轻微滞后与回弹。
    """
    return v + ((target - x) * SPRING_K - v * SPRING_C) * power * dt


def soft_clamp_speed(speed: float) -> float:
    """软钳速：``cap * (1 - e^(-s/cap))``。

    任意速度下单调可区分，但渐近不超过 ``MAX_THROW_SPEED``——既保留「甩得越猛
    飞得越快」的直觉，又不会因为一次抖动把宠物甩到屏幕外。
    """
    if speed <= 0:
        return 0.0
    return MAX_THROW_SPEED * (1.0 - math.exp(-speed / MAX_THROW_SPEED))


def throw_velocity(angle_deg: float, power: float) -> tuple[float, float]:
    """抛掷指令 → 初速度 (vx, vy)。

    ``angle`` 单位度，0 = 正右，**逆时针为正**（屏幕 y 向下，所以 vy 取负号，
    「往上抛」写 90）。初速 = ``power * THROW_SPEED_ANCHOR`` 再过软钳速。
    """
    speed = soft_clamp_speed(power * THROW_SPEED_ANCHOR)
    rad = math.radians(angle_deg)
    return math.cos(rad) * speed, -math.sin(rad) * speed


# ── 甩抛边界 ───────────────────────────────────────────────

def throw_bounds(W: float, H: float, size: float, side_allow: float = 0.0) -> ThrowBounds:
    """由视口/屏幕尺寸算抛掷边界。

    ``side_allow`` = 素材左右透明边余量：身体贴边而不是整个画布贴边，
    所以宠物看起来能贴到屏幕边缘，但身体不会越界丢失。
    """
    h = size * 9.0 / 16.0
    return ThrowBounds(
        minX=-side_allow,
        minY=0.0,
        maxX=W - size + side_allow,
        maxY=H - h,
    )


def screen_of_box(space: "ThrowSpace", x: float, y: float) -> int:
    """包围盒左上角落在哪块屏上（返回索引，找不到返回 -1）。"""
    for index, area in enumerate(space.areas):
        if area.contains(x, y):
            return index
    return -1


@dataclass
class ThrowSpace:
    """多屏抛掷空间：工作区 / 面板 / 每屏边界。"""

    areas: list[Rect] = field(default_factory=list)     # 工作区（不含任务栏）
    panels: list[Rect] = field(default_factory=list)    # 整块面板（含任务栏）
    bounds: list[ThrowBounds] = field(default_factory=list)
    size: float = 0.0
    sideAllow: float = 0.0


def throw_space(areas: list[Rect], size: float, side_allow: float = 0.0,
                panels: list[Rect] | None = None) -> ThrowSpace:
    """构造多屏抛掷空间：逐屏算包围盒边界。"""
    panels = panels or list(areas)
    return ThrowSpace(
        areas=list(areas),
        panels=list(panels),
        bounds=[throw_bounds(a.w + a.x, a.h + a.y, size, side_allow) for a in areas],
        size=size,
        sideAllow=side_allow,
    )


def index_at_point(rects: list[Rect], px: float, py: float) -> int:
    """点命中哪块矩形（返回索引，没有返回 -1）。"""
    for index, rect in enumerate(rects):
        if rect.contains(px, py):
            return index
    return -1


# ── 甩抛步进 ───────────────────────────────────────────────

def throw_step(state: ThrowState, dt_raw: float, b: ThrowBounds,
               physics: PhysicsParams = DEFAULT_PHYSICS) -> ThrowStepResult:
    """单屏抛掷一步：重力 → 位移 → 边缘反弹 → 地面摩擦。

    与 ``throw_step_region`` 的差别只有边界来源：这里整块桌面当一块屏用。
    """
    dt = min(max(dt_raw, 0.0), MAX_STEP_DT)
    x, y, vx, vy = state.x, state.y, state.vx, state.vy
    vy += physics.gravity * dt
    x += vx * dt
    y += vy * dt
    bounced = False

    if x < b.minX:
        x = b.minX
        vx = abs(vx) * physics.restitution
        bounced = True
    elif x > b.maxX:
        x = b.maxX
        vx = -abs(vx) * physics.restitution
        bounced = True

    if y < b.minY:
        # ceilingBounce=False：顶部不夹不弹，飞出屏幕后靠重力落回
        if physics.ceilingBounce:
            y = b.minY
            vy = abs(vy) * physics.restitution
            bounced = True
    elif y >= b.maxY:
        y = b.maxY
        vx *= max(0.0, 1.0 - physics.groundFriction * dt)
        if abs(vy) < REST_VY:
            vy = 0.0
        else:
            vy = -abs(vy) * physics.restitution
        bounced = True

    speed = math.hypot(vx, vy)
    at_rest = (
        (y >= b.maxY - 1 and abs(vy) < 1 and abs(vx) < REST_VX)
        or (bounced and speed < REST_VY and abs(vy) < 1)
    )
    return ThrowStepResult(x=x, y=y, vx=vx, vy=vy, bounced=bounced, atRest=at_rest)


def throw_step_region(state: ThrowState, dt_raw: float, space: ThrowSpace,
                      physics: PhysicsParams = DEFAULT_PHYSICS,
                      lock_screen: int = -1) -> ThrowStepResult:
    """多屏抛掷一步。

    与单屏版的唯一区别：**边界取自「身体中心所在屏」**，且越界后先探一下
    越界侧有没有别的屏——有就放行（跨屏飞行/落到下一块屏），没有才反弹。
    这样宠物不会飞进多屏外接矩形里的空洞。

    ``lock_screen >= 0`` 表示锁定在该屏：屏缝与空洞一样当墙，甩出去只在本屏内弹。
    """
    dt = min(max(dt_raw, 0.0), MAX_STEP_DT)
    x, y, vx, vy = state.x, state.y, state.vx, state.vy
    vy += physics.gravity * dt
    x += vx * dt
    y += vy * dt
    if not space.areas:
        return ThrowStepResult(x=x, y=y, vx=vx, vy=vy, screen=-1)

    h = space.size * 9.0 / 16.0
    bounced = False
    # 索引越界（飞行途中拔掉显示器）视为不锁定，避免拿不存在的屏当边界
    locked = 0 <= lock_screen < len(space.areas)

    def pick(index: int) -> int:
        return lock_screen if locked else index

    # ── 横向 ──
    cur = pick(screen_of_box(space, x, y)) if screen_of_box(space, x, y) >= 0 else pick(0)
    b = space.bounds[cur]
    pa = space.panels[cur] if cur < len(space.panels) else space.areas[cur]
    cy = y + h / 2
    if x < b.minX:
        if locked or index_at_point(space.panels, pa.x - 1, cy) < 0:
            x = b.minX
            vx = abs(vx) * physics.restitution
            bounced = True
    elif x > b.maxX:
        if locked or index_at_point(space.panels, pa.right, cy) < 0:
            x = b.maxX
            vx = -abs(vx) * physics.restitution
            bounced = True

    # ── 纵向（横向被夹回后身体中心可能换屏，重新定位）──
    found = screen_of_box(space, x, y)
    cur = pick(found if found >= 0 else 0)
    b = space.bounds[cur]
    pa2 = space.panels[cur] if cur < len(space.panels) else space.areas[cur]
    cx = x + space.size / 2
    if y < b.minY:
        if physics.ceilingBounce and (locked or index_at_point(space.panels, cx, pa2.y - 1) < 0):
            y = b.minY
            vy = abs(vy) * physics.restitution
            bounced = True
    elif y >= b.maxY:
        # 下方还有屏（上下排布）⇒ 不当地面，继续往下掉
        if locked or index_at_point(space.panels, cx, pa2.bottom) < 0:
            y = b.maxY
            vx *= max(0.0, 1.0 - physics.groundFriction * dt)
            if abs(vy) < REST_VY:
                vy = 0.0
            else:
                vy = -abs(vy) * physics.restitution
            bounced = True

    found = screen_of_box(space, x, y)
    cur = pick(found if found >= 0 else 0)
    b = space.bounds[cur]
    speed = math.hypot(vx, vy)
    at_rest = (
        (y >= b.maxY - 1 and abs(vy) < 1 and abs(vx) < REST_VX)
        or (bounced and speed < REST_VY and abs(vy) < 1)
    )
    return ThrowStepResult(x=x, y=y, vx=vx, vy=vy, bounced=bounced, atRest=at_rest, screen=cur)


# ── 松手速度估算 ───────────────────────────────────────────

def trim_trail(trail: list[DragSample], now: float) -> list[DragSample]:
    """丢掉太旧的拖拽采样，只保留最近 ``TRAIL_KEEP_MS`` 内的点。"""
    return [s for s in trail if now - s.t <= TRAIL_KEEP_MS]


def release_velocity(trail: list[DragSample], now: float) -> tuple[float, float]:
    """由拖拽轨迹估算松手速度 (vx, vy)，px/s。

    取「最近一个采样」与「窗口内较早的一个采样」求平均速度，再乘
    ``PEAK_WEIGHT`` 做一点抑制——纯末帧差分容易被指针抖动放大成夸张甩抛。
    采样太旧或跨度过小时返回 (0, 0)，即「温柔放下」。
    """
    if len(trail) < 2:
        return 0.0, 0.0
    newest = trail[-1]
    if now - newest.t > RELEASE_STALE_MS:
        return 0.0, 0.0
    # 找窗口内最早的一个点；窗口内没有就退回列表首点
    oldest = None
    for sample in trail:
        if newest.t - sample.t <= RELEASE_WINDOW_MS:
            oldest = sample
            break
    if oldest is None:
        oldest = trail[0]
    span = newest.t - oldest.t
    if span < MIN_SPAN_MS:
        return 0.0, 0.0
    vx = (newest.x - oldest.x) / (span / 1000.0)
    vy = (newest.y - oldest.y) / (span / 1000.0)
    speed = math.hypot(vx, vy)
    if speed < DEAD_ZONE_SPEED:
        return 0.0, 0.0
    clamped = soft_clamp_speed(speed)
    scale = (clamped / speed) * PEAK_WEIGHT
    return vx * scale, vy * scale


# ── Q 弹挤压 ───────────────────────────────────────────────

def landing_squash(impact_speed: float) -> float:
    """落地冲击速度 → 最大压缩比（1 = 不压缩，越小压得越扁）。"""
    span = SQ_HARD_SPEED - SQ_SOFT_SPEED
    t = (abs(impact_speed) - SQ_SOFT_SPEED) / span if span else 0.0
    t = min(max(t, 0.0), 1.0)
    return min(0.8, 1.0 - t * (1.0 - SQ_MAX_SQUASH))


def squash_scale(u: float, squash: float = SQ_SQUASH) -> float:
    """Q 弹挤压曲线：进度 ``u ∈ [0,1]`` → scaleY。

    下压段（0~0.45）ease-in 压缩；回弹段（0.45~1）easeOutBack，带回弹过冲
    （约 4%）。两端共用同一条曲线，手感严格一致。
    """
    if u < 0.45:
        p = u / 0.45
        return 1.0 - (1.0 - squash) * p * p
    p = (u - 0.45) / 0.55
    c1 = 1.70158
    c3 = c1 + 1.0
    f = 1.0 + c3 * (p - 1.0) ** 3 + c1 * (p - 1.0) ** 2
    return min(1.12, squash + (1.0 - squash) * max(f, 0.0))


# ── 屏幕漫游 ───────────────────────────────────────────────

def plan_move(
    cx: float,
    cy: float,
    W: float,
    H: float,
    direction: int,
    min_dist: float,
    max_dist: float,
    margin: float,
    half_w: float,
    distance: float,
    side_allow: float = 0.0,
    areas: list[Rect] | None = None,
) -> float | None:
    """规划一次漫游的落点 x 坐标；越界返回 None（调用方应换个方向）。

    与原版差别：原版内部随机 ``distance``，这里由调用方传入，
    让随机源留在窗口层（便于测试与复现）。

    ``areas`` 给了就按「工作区并集」判定（多屏，允许骑缝行走）；
    不给就按单块 W/H 矩形判定。
    """
    side = side_allow
    target = cx + direction * distance
    if areas:
        body_half = half_w - side
        if not any(r.contains(target - body_half - margin, cy) for r in areas):
            return None
        if not any(r.contains(target + body_half + margin, cy) for r in areas):
            return None
        return target

    left_bound = margin + half_w - side
    right_bound = W - margin - half_w + side
    if target < left_bound or target > right_bound:
        return None
    return target
