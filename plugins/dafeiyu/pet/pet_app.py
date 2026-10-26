"""大肥鱼桌宠 — 原生窗口进程（macOS / pyobjc）。

这是一个**独立进程**，由 akm 插件（``plugins/dafeiyu/index.py``）按需 spawn。
它自己不做任何业务判断，只做三件事：

1. 轮询 ``AKM_DAFEIYU_STATE_URL`` 拿到状态快照（state / clip / text / balance / config）；
2. 把状态映射成素材动作帧，在无边框透明置顶窗口里逐帧播放；
3. 处理鼠标交互（拖动、点击摸头、右键菜单）并把窗口位置持久化。

为什么独立进程而不是跑在宿主里：AppKit 的 ``NSApplication.run()`` 会独占主线程，
塞进 FastAPI 的事件循环里会直接卡死服务；独立进程还能做到「关掉桌宠不影响代理」。

实现参考 dsh-dafeiyu 的 Swift 版 ``PetController`` / ``PetView``：
窗口用 ``NSPanel`` + ``borderless`` + ``clear`` 背景 + ``floating`` 层级 +
``canJoinAllSpaces``，鼠标用屏幕绝对坐标做 1:1 拖动。
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time
import subprocess
import urllib.request
from pathlib import Path

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSColor,
    NSEvent,
    NSFont,
    NSFontAttributeName,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSPanel,
    NSStatusWindowLevel,
    NSTextField,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject, NSTimer, NSURL
from PyObjCTools import AppHelper

# 物理模块与桌宠同目录（子进程直接按脚本目录导入）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import physics  # noqa: E402  （放在 sys.path 注入之后）
import companion  # noqa: E402  （同上）

# ── 环境变量（由插件在 spawn 时注入）────────────────────────
STATE_URL = os.environ.get("AKM_DAFEIYU_STATE_URL", "http://127.0.0.1:8800/api/dafeiyu/state")
ASSET_ROOT = Path(os.environ.get("AKM_DAFEIYU_ASSET_ROOT", "")).expanduser()
DATA_DIR = Path(os.environ.get("AKM_DAFEIYU_DATA_DIR", str(Path.home() / ".akm" / "dafeiyu")))
# 父进程（AKM 宿主）pid：由插件在 spawn 时注入。
# **用途**：宿主被强杀 / 崩溃时，桌宠子进程因 start_new_session=True 已脱离
# 进程组，会变成孤儿窗口一直挂在桌面上。tick 里定期检查这个 pid 是否还活着，
# 宿主没了就自己退出，保证「关闭 AKM 时桌宠一起关闭」。
PARENT_PID = int(os.environ.get("AKM_DAFEIYU_PARENT_PID") or 0)
LAYOUT_FILE = DATA_DIR / "layout.json"

# 轮询间隔：状态变化不需要毫秒级，1 秒足够，也不给宿主添压力。
POLL_INTERVAL = 1.0
# 单帧默认时长（素材 24fps ≈ 42ms）。manifest 里每个 clip 自带 frameMs。
DEFAULT_FRAME_MS = 42

# 交互动作：点击时优先播放这些一次性动作，播完回到当前状态动作。
CLICK_CLIPS = ("head_pat", "poke")
# 待机装饰动作：长时间待机时偶尔播放。
IDLE_DECOR_CLIPS = ("idle_stretch", "idle_yawn", "eat_token", "tail")

# 右键「选择动画」里的中文名。键必须是 manifest 里真实存在的 clip 名；
# 缺失的键回退显示 clip 原名，因此新增素材不需要改这里也能用。
CLIP_LABELS = {
    "idle": "待机",
    "waiting": "等待",
    "thinking": "思考",
    "working": "工作中",
    "working_search": "查找",
    "working_command": "执行命令",
    "success": "成功",
    "error": "出错",
    "dragging": "被拖拽",
    "dragging_release": "落地",
    "head_pat": "摸头",
    "poke": "戳一下",
    "tail": "吐泡泡",
    "eat_token": "吃 Token",
    "idle_stretch": "伸懒腰",
    "idle_yawn": "打哈欠",
}

# 气泡尺寸（未缩放基准），与素材画布 412×344 匹配。
BASE_PET_W = 412
BASE_PET_H = 344
# 气泡高度：基础态只有三行文字（各 20px + 上下内边距），
# 打开对话输入时再动态加一行 CHAT_ROW_H，避免平时底部空一块。
# 气泡内每一行的高度。12pt 字体实际占约 15px，行高减去文字高就是视觉行间距：
# 22 时约 7px（偏挤）、24 时约 9px（紧凑但清晰）、28 时约 13px（偏松）。
# 取 24：用户反馈 28 太散，22 太挤。
BUBBLE_LINE_H = 24.0
# 气泡文字行数（状态 / 结果 / 余额+计数）
# 第 0 行（对话/碎碎念回复）最多显示几行：超出按行数截断。
# 3 行能装下绝大多数回复（气泡宽 412 时每行约 30 个汉字），
# 再多气泡就太高、挡住角色和桌面。
BUBBLE_TEXT_LINES = 3
BUBBLE_ROWS = 3
# 三行文字总高 + 上下内边距（10 * 2）
BUBBLE_H = int(BUBBLE_LINE_H * 3 + 20)
# 内嵌输入行额外占用的高度（气泡总高 = BUBBLE_H + CHAT_ROW_H）。
# 布局本身靠几何关系保证不重叠（文字区从输入框上沿往上排），
# 这个值只决定「气泡比文字区多高」，24 时总高 120、row0 距顶仍有 16px 余量。
# 打开输入框时额外占用的高度。必须容得下：
#   输入框(26) + 底边距(8) + 与文字区的间隙(2) + 余量 ≈ 34
# 取 24 时文字区被压缩、间距变挤，且输入框下边框会被 masksToBounds 裁掉。
CHAT_ROW_H = 34
# 内嵌输入框高度：比文字行矮一些才像「输入条」，与行高解耦
CHAT_INPUT_H = 26.0
# 发送按钮宽度。**按钮不随角色缩放**（标题 11pt 固定、输入框 12pt 固定），
# 所以这里按「放得下两个字 + 圆角按钮内边距」取固定值：
# 「发送」实测 22.5px，NSBezelStyleRounded 左右各约 8px，50px 刚好。
CHAT_BTN_W = 50.0

# 默认角色缩放：50%（固定，不提供设置项）。启动时就用这个值，
# 避免窗口先按 100% 建好再缩到 50% 的可见跳动。
DEFAULT_SCALE = 0.5

# ── 打字机效果 ────────────────────────────────────────────
# 对话/碎碎念回复逐字显示。这两个常量控制「每字间隔」与「最小显示字数」。
# 逐字速度：0.035s/字 ≈ 28 字/秒，比人手打字快、又不至于一闪而过。
TYPEWRITER_CHAR_S = 0.035
# 一句话最多显示多久：长回复（比如 200 字）按 0.035s/字要 7 秒，
# 太慢会挡住后续状态，所以按总时长反推间隔，保证整句不超过 6 秒。
TYPEWRITER_MAX_S = 6.0
# ── 交互与漫游参数 ─────────────────────────────────────────
# 拖动阈值：小于这个位移视为「点一下」，不触发拖拽（避免点击时角色乱跳）
DRAG_THRESHOLD_PX = 5.0
# 漫游：每次走动的距离范围与距屏幕边缘的安全距
WANDER_MIN_DIST = 60.0
WANDER_MAX_DIST = 260.0
WANDER_MARGIN = 24.0
WANDER_SPEED = 90.0          # px/s，行走速度
# 两次漫游之间的间隔范围（秒）
WANDER_GAP = (6.0, 14.0)
# 素材左右透明边余量：身体贴边而不是画布贴边
SIDE_ALLOW = 60.0


def log(msg: str) -> None:
    """写一行带时间戳的诊断日志到 stderr（插件已把它重定向到 pet.log）。

    为什么带时间戳：用户报「用一阵就卡住」这类问题时，没有时间戳就无法判断
    是「某个操作触发的」还是「运行多久后自然发生的」。
    """
    ts = time.strftime("%H:%M:%S")
    sys.stderr.write(f"[dafeiyu-pet {ts}] {msg}\n")
    sys.stderr.flush()


def log_exc(where: str, exc: BaseException) -> None:
    """记录异常（含调用栈），用于排查「卡住」类问题。

    以前异常被静默吞掉，出问题时日志里什么都没有。现在关键路径统一走这里。
    """
    import traceback

    log(f"!! {where} 异常: {type(exc).__name__}: {exc}")
    for line in traceback.format_exc().splitlines()[-6:]:
        sys.stderr.write(f"    {line}\n")
    sys.stderr.flush()


class FrameCache:
    """素材帧缓存。

    3462 张 WebP 全量载入会吃掉几百 MB 内存，因此按 clip 懒加载：
    只在切换到某个动作时把该动作的帧读成 NSImage 并缓存。
    单个 clip 最多 241 帧（约 4MB 解码后更大），实际内存可接受。
    """

    def __init__(self, asset_root: Path) -> None:
        self.root = asset_root
        self.manifest: dict = {}
        self._clips: dict[str, list[NSImage]] = {}
        self._meta: dict[str, dict] = {}
        self._load_manifest()

    @objc.python_method
    def _load_manifest(self) -> None:
        """读取 pet-manifest.json：clips 提供帧列表与 frameMs。"""
        path = self.root / "pet-manifest.json"
        try:
            self.manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"素材清单读取失败 {path}: {exc}")
            self.manifest = {}
        for name, clip in (self.manifest.get("clips") or {}).items():
            if isinstance(clip, dict):
                self._meta[name] = clip

    @objc.python_method
    def has(self, clip: str) -> bool:
        return clip in self._meta

    def clip_names(self) -> list[str]:
        """返回 manifest 里声明且实际有帧的动作名（保持清单顺序）。

        右键菜单直接用它渲染列表，因此素材增减无需改菜单代码。
        """
        return [name for name in self._meta.keys() if self._meta[name].get("frames")]

    @objc.python_method
    def frames(self, clip: str) -> list[NSImage]:
        """取某个动作的全部帧（首次调用时加载并缓存）。"""
        if clip in self._clips:
            return self._clips[clip]
        meta = self._meta.get(clip) or {}
        names = meta.get("frames") or []
        images: list[NSImage] = []
        for name in names:
            path = self.root / "pet" / name
            try:
                url = NSURL.fileURLWithPath_(str(path))
                image = NSImage.alloc().initWithContentsOfURL_(url)
            except Exception:
                image = None
            if image is not None:
                images.append(image)
        if not images:
            log(f"动作 {clip} 没有可用帧")
        self._clips[clip] = images
        return images

    @objc.python_method
    def frame_ms(self, clip: str) -> int:
        meta = self._meta.get(clip) or {}
        try:
            return max(16, int(meta.get("frameMs") or DEFAULT_FRAME_MS))
        except (TypeError, ValueError):
            return DEFAULT_FRAME_MS

    @objc.python_method
    def loops(self, clip: str) -> bool:
        meta = self._meta.get(clip) or {}
        return bool(meta.get("loop", True))


class PetWindowController(NSObject):
    """窗口 + 动画 + 交互控制器。

    状态机：
    - ``state_clip``：当前状态对应的循环动作（idle/thinking/working/...）
    - ``override_clip``：一次性的临时动作（摸头、拖动、装饰），播完清空
    - 播放优先级 override > state
    """

    def init(self):
        self = objc.super(PetWindowController, self).init()
        if self is None:
            return None

        self.cache = FrameCache(ASSET_ROOT)
        self.panel = None
        self.image_view = None
        self.bubble = None
        # 气泡三行标签：建窗时填充，这里先占位，避免早期回调访问到未定义属性
        self.bubble_rows: list = []
        self.bubble_title = None
        self.bubble_sub = None

        # ── 播放状态 ──
        # 三层优先级（高 → 低）：manual_clip（右键手动锁定）> override_clip
        # （摸头/拖拽等一次性动作）> state_clip（AKM 请求状态）。
        self.state_clip = "idle"
        self.override_clip: str | None = None
        # 右键「选择动画」锁定的动作；None 表示跟随 AKM 状态
        self.manual_clip: str | None = None
        self.frame_index = 0
        self.last_frame_at = 0.0
        # 进程启动时刻：父进程存活检测的宽限期基准（启动 10 秒内不检测，
        # 避免宿主刚 spawn 完、pid 还没稳定时误判成「父进程已退出」）。
        self._started_at = time.time()
        self._last_parent_check = 0.0
        # **待播队列：不强制中断正在播放的动画**。
        # 新动作先记在这里，等当前动作播完一轮（_tickOnce 轮末）才切换。
        # 直接切会让动画从第 1 帧重头开始，高频切换时看起来就是闪动。
        self._pending_state_clip: str = ""
        self._pending_override_clip: str = ""

        # ── 交互状态 ──
        self.dragging = False
        self.drag_start: tuple[float, float] | None = None
        self.window_origin: tuple[float, float] | None = None
        # 拖拽弹簧：drag_target 是「鼠标 - 抓取偏移」，spring_vx/vy 是窗口速度
        self.drag_target: tuple[float, float] | None = None
        self.spring_vx = 0.0
        self.spring_vy = 0.0
        self.drag_trail: list = []
        self.drag_pressed_at = 0.0

        # ── 甩抛 ──
        self.throwing = False
        self.throw_state = physics.ThrowState(x=0.0, y=0.0)
        self.last_physics_at = 0.0

        # ── Q 弹挤压 ──
        # squash_started_at / squash_squash：一次挤压的起点与目标压缩比
        self.squash_started_at = 0.0
        self.squash_squash = 1.0
        self.squash_active = False

        # ── 漫游 ──
        self.wander_target: float | None = None   # 目标 x（左上角），None = 不走
        self.wander_dir = 1                        # 朝向：1 右 / -1 左
        self.next_wander_at = time.time() + random.uniform(6, 14)

        # 默认 50%：与插件下发的 scale=50 一致，避免「首帧 100% 再缩到 50%」
        # 造成的窗口闪一下。服务端配置到达后会以配置值为准。
        self.scale = DEFAULT_SCALE
        self.allow_drag = True
        self.always_on_top = True
        # 锁定在当前屏幕：漫游与甩抛都只在本屏内（屏缝当墙）
        self.lock_screen = False
        # 漂移（待机时自己走动）：默认关闭。用户可在插件配置里开启。
        self.drift_enabled = False
        self.bubble_enabled = True
        # 气泡里是否显示余额（插件配置 bubble_show_balance）
        self.bubble_show_balance = True
        # 对话期间强制开气泡时，记录用户原本的开关值以便还原
        self._bubble_was_enabled = True
        # 碎碎念临时借用气泡（用户可能设了「不显示气泡」）：True 表示
        # 当前气泡是碎碎念临时打开的，展示期结束后要还原成隐藏。
        self._whisper_forced_bubble = False
        self.reduce_motion = False
        self.idle_decor_at = time.time()
        self.next_decor_delay = random.uniform(25, 55)
        self.last_poll_ok = False
        self.poll_fail_count = 0

        # ── 对话 / 碎碎念 ──
        # 这些值由服务端 /state 的 config 下发（插件配置），启动时给默认值兜底
        self.base_url = os.environ.get("AKM_DAFEIYU_BASE_URL", "http://127.0.0.1:8800")
        self.api_key = os.environ.get("AKM_DAFEIYU_API_KEY", "")
        self.model_name = os.environ.get("AKM_DAFEIYU_MODEL", "")
        self.chat_prompt = companion.DEFAULT_CHAT_PROMPT
        self.whisper_prompt = companion.DEFAULT_WHISPER_PROMPT
        self.whisper_enabled = True
        self.whisper_interval_s = 180
        self.memory_file = companion.MEMORY_FILE
        self.memory = companion.load_memory(self.memory_file)
        self._busy = False
        # 请求开始时间：超时兜底用，避免网络挂死时 _busy 永久为 True
        self._busy_since = 0.0
        # performSelectorOnMainThread 失败时的兜底载荷，由 tick 消费
        # ── 打字机效果（对话/碎碎念回复逐字显示）──
        # _typewriter_text 是完整文本，_typewriter_index 是已显示字数；
        # done=True 表示没有正在进行的逐字动画（普通状态文案直接写满）。
        self._typewriter_text = ""
        self._typewriter_index = 0
        self._typewriter_interval = TYPEWRITER_CHAR_S
        self._typewriter_next_at = 0.0
        self._typewriter_done = True
        self._pending_reply = None
        # 下一次自动碎碎念的时间
        self.next_whisper_at = time.time() + self.whisper_interval_s
        # 最近一次气泡是否是对话/碎碎念内容（避免被状态文案立刻覆盖）
        self.bubble_hold_until = 0.0
        self.last_notify_at = 0.0
        # 对话输入：直接嵌在气泡里（不再用独立窗口）
        self.chat_field = None
        self.chat_send_btn = None

        self._load_layout()
        return self

    # ── 布局持久化 ──────────────────────────────────────────

    @objc.python_method
    def _load_layout(self) -> None:
        """读取上次窗口位置；文件损坏时用默认位置（屏幕右下角附近）。"""
        self.saved_origin: tuple[float, float] | None = None
        try:
            if LAYOUT_FILE.exists():
                data = json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))
                x, y = float(data.get("x")), float(data.get("y"))
                self.saved_origin = (x, y)
        except Exception as exc:
            log(f"布局读取失败，使用默认位置: {exc}")

    @objc.python_method
    def _save_layout(self) -> None:
        """保存窗口位置，下次启动恢复到同一处。

        **保存前必须夹进屏幕**：否则用户把窗口拖到越界处（或角色调大后越界）
        时，写进去的就是越界坐标；下次启动虽然会夹取，但随后的保存又把越界
        值写回去，形成「每次启动都在屏幕外」的循环。
        """
        if not self.panel:
            return
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            origin = self.panel.frame().origin
            x, y = self._clampToScreens((float(origin.x), float(origin.y)))
            LAYOUT_FILE.write_text(
                json.dumps({"x": x, "y": y}),
                encoding="utf-8",
            )
        except Exception as exc:
            log(f"布局保存失败: {exc}")

    # ── 窗口构建 ────────────────────────────────────────────

    @objc.python_method
    def buildWindow(self) -> None:
        """创建无边框透明置顶面板。

        ``NSStatusWindowLevel`` 让窗口浮在普通窗口之上但不遮挡菜单栏；
        ``canJoinAllSpaces`` 保证切换桌面空间时角色跟着走。
        """
        width, height = self._windowSize()
        # 用 PetPanel：气泡内输入框需要窗口能成为 key window 才能收键盘
        panel = PetPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary
        )
        panel.setHidesOnDeactivate_(False)
        panel.setIgnoresMouseEvents_(False)
        panel.setMovableByWindowBackground_(False)
        panel.setTitle_("AKM 大肥鱼")

        view = PetView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        view.controller = self
        # 让 panel 也能拿到 controller，供 cancelOperation_（Esc）收起输入框
        panel.controller = self
        panel.setContentView_(view)

        # 角色图层：用 NSImageView 直接显示当前帧，避免自绘的坐标换算。
        pet_h = BASE_PET_H * self.scale
        pet_w = BASE_PET_W * self.scale
        image_view = PetImageView.alloc().initWithFrame_(NSMakeRect(0, 0, pet_w, pet_h))
        image_view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        image_view.setEditable_(False)
        image_view.setAnimates_(False)
        # 必须显式开启 layer：否则 layer() 返回 None，Q 弹挤压会静默失效
        # （_applySquash 里拿不到 layer 就直接 return，用户完全看不到反馈）
        image_view.setWantsLayer_(True)
        view.addSubview_(image_view)

        self.panel = panel
        self.image_view = image_view
        self._buildBubble(view)

        # 位置：优先恢复上次保存的坐标，否则放在主屏右下角。
        # **必须夹进屏幕**：角色尺寸改大后旧坐标可能让窗口右半截跑到屏幕外，
        # 表现为「发送」按钮被屏幕边缘裁掉（用户截图里就是这样）。
        origin = self._clampToScreens(self.saved_origin or self._defaultOrigin())
        panel.setFrameOrigin_(origin)

        panel.orderFrontRegardless()
        log(f"窗口已创建 {int(width)}x{int(height)} @ {origin}")

    @objc.python_method
    def _buildBubble(self, view) -> None:
        """状态气泡：三行独立标签（状态 / 结果 / 统计），避免长文本互相挤压。"""
        width, _ = self._windowSize()
        bubble_h = self._bubbleHeight()
        # 气泡放在角色上方（AppKit 坐标系原点在左下，所以 y 要加角色高度）
        y = BASE_PET_H * self.scale
        bubble = PassThroughView.alloc().initWithFrame_(NSMakeRect(0, y, width, bubble_h))
        bubble.setWantsLayer_(True)
        layer = bubble.layer()
        if layer is not None:
            layer.setBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.13, 0.18, 0.9).CGColor()
            )
            layer.setCornerRadius_(10.0)
        bubble.setHidden_(not self.bubble_enabled)
        # 气泡同样要放行鼠标事件，否则点在上半部分会没有反应
        # 气泡内的文字标签同样要放行鼠标事件（见文件末尾的 PassThrough 类）

        # 三行标签：先按临时坐标建好，最终位置统一由 _layoutBubbleContent 排
        # （那里会给底部输入框留空间，避免第三行被输入框压住）。
        line_h = BUBBLE_LINE_H
        pad = 10.0
        rows = []
        for index in range(3):
            label = NSTextField.alloc().initWithFrame_(
                NSMakeRect(pad, 0, width - pad * 2, line_h)
            )
            label.setBezeled_(False)
            label.setDrawsBackground_(False)
            label.setEditable_(False)
            label.setSelectable_(False)
            # **第 0 行允许换行、其余两行单行截断**：
            # 第 0 行是对话/碎碎念回复或状态文案，最容易超长（用户看到
            # 「你好哇～大肥鱼甩甩鳍，溅起一…」被截断）。改成按宽度自动换行、
            # 最多 BUBBLE_TEXT_LINES 行，超出才省略；气泡高度按实际行数撑开。
            # 第 1/2 行是结构化短信息（结果/余额/计数），保持单行截断。
            #
            # 三处都要设，缺一不可：只设 label.setLineBreakMode_ 无效——真正
            # 控制排版的是内部 cell（NSTextFieldCell），它默认 wraps=True 会把
            # 长文本撑破气泡，所以必须显式设置 wraps 与 lineBreakMode。
            cell = label.cell()
            if index == 0:
                if cell is not None:
                    cell.setWraps_(True)                      # 允许换行
                    cell.setLineBreakMode_(0)                 # NSLineBreakByWordWrapping
                    cell.setTruncatesLastVisibleLine_(True)   # 超出行数才截断
                label.setLineBreakMode_(0)
                try:
                    label.setUsesSingleLineMode_(False)
                    label.setMaximumNumberOfLines_(BUBBLE_TEXT_LINES)
                except Exception:
                    pass
            else:
                if cell is not None:
                    cell.setWraps_(False)                    # 禁止换行
                    cell.setLineBreakMode_(4)                # NSLineBreakByTruncatingTail
                    cell.setTruncatesLastVisibleLine_(True)
                label.setLineBreakMode_(4)
                try:
                    label.setUsesSingleLineMode_(True)
                    label.setMaximumNumberOfLines_(1)
                except Exception:
                    pass
            # 字号必须跟随 scale：行高按 scale 缩放而字号不缩，小尺寸下
            # 行框会比文字还矮，三行直接重叠（40% 时行高 11px < 文字 15px）。
            base_pt = 12.0 if index == 0 else 11.0
            label.setFont_(NSFont.systemFontOfSize_(base_pt * self.scale))
            label.setTextColor_(
                NSColor.whiteColor()
                if index == 0
                else NSColor.colorWithCalibratedWhite_alpha_(0.76, 1.0)
            )
            # 关键：label 自身也要裁剪。cell 配了 truncatingTail 只影响「文本排版」，
            # 但 NSTextField 默认 clipsToBounds=False，超长内容仍会画到自己 frame
            # 之外（用户看到文字溢出气泡就是这个原因）。这里显式开启裁剪。
            label.setWantsLayer_(True)
            try:
                label.setClipsToBounds_(True)
            except Exception:
                pass
            label_layer = label.layer()
            if label_layer is not None:
                label_layer.setMasksToBounds_(True)
            label.setStringValue_("")
            bubble.addSubview_(label)
            rows.append(label)

        view.addSubview_(bubble)
        self.bubble = bubble
        self.bubble_rows = rows
        # 建完立刻排一次，保证初始布局与后续 resize 用的是同一套坐标
        self._layoutBubbleContent(width, bubble_h)
        # 兼容旧字段名：title 是第 0 行，sub 是第 1 行
        self.bubble_title = rows[0]
        self.bubble_sub = rows[1]

    @objc.python_method
    def _bubbleHeight(self) -> float:
        """当前气泡高度：文字区（BUBBLE_H）+ 输入行（CHAT_ROW_H，仅在打开时）。

        ``BUBBLE_H`` 是**文字区**高度（三行 × 20 + 上下边距），输入行是额外叠加的，
        所以打开输入框时总高 = BUBBLE_H + CHAT_ROW_H，文字区仍然只有 BUBBLE_H 高、
        整体上移到输入框之上，两者不重叠。

        不能把输入行算进 BUBBLE_H 里再从中扣——那样文字区高度不变，
        第三行会一直压在输入框上（之前就是这么错的）。
        """
        s = self.scale
        # 行高不能机械乘 scale：字号有 12pt 下限，缩小时文字高度不再变小，
        # 行高若仍按比例缩就会比文字还矮。这里按「最大文字高度 + 间距」兜底。
        font_pt = max(12.0, 12.0 * s)
        text_h = self._textHeightForPointSize(font_pt)
        # 间距用固定 8px 而不是 8*s：小尺寸下 8*0.4=3.2px 太挤，
        # 而字号已经触到 12pt 下限，间距也该有同样的可读性下限。
        # 间距 6px：8px 偏松，6px 更紧凑（用户反馈稍微缩小一点）
        line_h = max(BUBBLE_LINE_H * s, text_h + 6.0)
        # 气泡高 = 三行行高 + 上下边距，而不是 BUBBLE_H * scale——
        # 后者在小尺寸下会装不下三行 12pt 文字。
        # 用固定 3 而不是 len(self.bubble_rows)：建窗过程中 _buildBubble 还没
        # 填 rows，此时 len() 是 0，会算出只有边距高的气泡（20px）。
        # 第 0 行可能换行成多行（BUBBLE_TEXT_LINES），气泡要按**实际行数**
        # 撑开，否则多出来的行会被气泡底边裁掉。
        # 第 1/2 行只在有内容时占一行：对话回复时它们是空的，
        # 若也占高度，气泡底部会空出一大块（截图里文字与输入框之间的大空白）。
        title_lines = self._titleLineCount()
        used_extra = sum(
            1 for lb in self.bubble_rows[1:] if str(lb.stringValue() or "").strip()
        )
        total = line_h * max(1, title_lines + used_extra) + 20.0 * s
        if self.chat_field is not None:
            # **输入行完全不随 scale 缩放**（用固定 CHAT_ROW_H）：
            # 输入框字号固定 12pt、按钮标题固定 11pt，都不随角色大小变。
            # 若高度仍按 scale 缩，scale=0.5 时只剩 17px，装不下 20px 的
            # 输入框 → 下边框被裁；按钮也只有 13px 高，比文字还矮 →
            # 「发送」上下被裁（用户截图现象）。
            total += CHAT_ROW_H
        return total


    @objc.python_method
    def _titleLineCount(self) -> int:
        """第 0 行文字按当前宽度换行后需要几行（1..BUBBLE_TEXT_LINES）。

        **为什么需要测量**：第 0 行允许换行后，气泡高度必须跟着行数走。
        若仍固定按 1 行算高度，多出来的行会被气泡底边裁掉（就是用户截图里
        「你好哇～大肥鱼甩甩鳍，溅起一…」那种看不全）。

        用 ``NSString.sizeWithAttributes`` 按「气泡可用宽度」测量换行后的
        实际高度，再除以行高得到行数。建窗前 bubble_rows 还是空的，
        此时返回 1（建窗后 _resizeWindow 会用真实文本重新算）。
        """
        if not self.bubble_rows or self.bubble is None:
            return 1
        label = self.bubble_rows[0]
        # **打字机进行中要用完整文本测量**：逐字显示时 label 里只有前几个字，
        # 按它测行数会一直是 1 行，气泡高度不会跟着长——结果后半个回复被裁掉。
        # 所以优先用 _typewriter_text（完整文本），没有才读 label 当前值。
        text = getattr(self, "_typewriter_text", "") or str(label.stringValue() or "")
        if not text:
            return 1
        font = label.font()
        if font is None:
            return 1
        # 可用宽度 = 气泡宽 - 左右内边距（与 _layoutBubbleContent 一致）
        width = max(20.0, self.bubble.frame().size.width - 20.0 * self.scale)
        try:
            # **必须用 NSString 包装**：Python str 没有
            # boundingRectWithSize:options:attributes: 方法，直接调用会走
            # except 分支永远返回 1 行（实测 35 字被压成 1 行就是这么来的）。
            # options 必须含 1（NSStringDrawingUsesLineFragmentOrigin），
            # 否则按「单行」测量，返回的永远是 1 行的高度。
            ns_text = objc.lookUpClass("NSString").stringWithString_(text)
            attrs = {NSFontAttributeName: font}
            size = ns_text.boundingRectWithSize_options_attributes_(
                (width, 1e6), 1, attrs
            ).size
            text_h = self._labelTextHeight(label)
            if text_h <= 0:
                return 1
            lines = int(math.ceil(size.height / text_h))
        except Exception as exc:
            log(f"测量气泡行数失败: {exc}")
            return 1
        return max(1, min(BUBBLE_TEXT_LINES, lines))

    @objc.python_method
    def _textHeightForPointSize(self, point_size: float) -> float:
        """给定字号，返回文字实际占用高度（ascender - descender + 1）。"""
        font = NSFont.systemFontOfSize_(point_size)
        if font is None:
            return point_size * 1.25
        return font.ascender() - font.descender() + 1.0

    @objc.python_method
    def _sendButtonTitleSize(self) -> tuple[float, float]:
        """返回「发送」按钮标题的实测宽高（pt）。

        按钮标题字号是**固定的 11pt**，不随角色大小缩放。所以布局时不能
        直接拿 ``CHAT_BTN_W * scale`` 当按钮宽度：scale=0.5 时是 25px，而
        标题本身要 22.5px，加上 NSBezelStyleRounded 左右各约 8px 内边距
        就放不下，圆角按钮会把文字裁成「发」（用户截图现象）。

        这里按真实 attributedTitle 测量，取不到时退化为保守常量，
        保证任何 scale 下按钮都能完整显示两个字。
        """
        btn = getattr(self, "chat_send_btn", None)
        if btn is not None:
            try:
                attrs = btn.attributedTitle()
                if attrs is not None:
                    size = attrs.size()
                    if size.width > 0 and size.height > 0:
                        return float(size.width), float(size.height)
            except Exception:
                pass
        # 退化值：11pt 中文两个字实测约 22.5x14，宁可略大不要裁字
        return 24.0, 15.0

    @objc.python_method
    def _windowSize(self) -> tuple[float, float]:
        """窗口尺寸 = 角色画布 + 气泡高度（气泡关闭时只留角色）。"""
        width = BASE_PET_W * self.scale
        height = BASE_PET_H * self.scale + (self._bubbleHeight() if self.bubble_enabled else 0)
        return width, height

    @objc.python_method
    def _clampToScreens(self, origin: tuple[float, float]) -> tuple[float, float]:
        """把窗口左上角夹进「当前窗口所在屏」的可见范围内。

        为什么需要：``layout.json`` 里存的是上次的坐标，若之后把角色调大
        （或换了分辨率/拔了显示器），旧坐标会让窗口右半截跑到屏幕外，
        输入框的「发送」按钮正好在右边缘，就被屏幕裁掉了。
        """
        try:
            from AppKit import NSScreen
        except Exception:
            return origin
        screens = NSScreen.screens()
        if not screens:
            return origin
        x, y = float(origin[0]), float(origin[1])
        w, h = self._windowSize()

        # 选「窗口中心最接近」的那块屏；取不到就用主屏
        best = screens[0]
        best_dist = None
        cx, cy = x + w / 2, y + h / 2
        for screen in screens:
            fr = screen.frame()
            sx = fr.origin.x + fr.size.width / 2
            sy = fr.origin.y + fr.size.height / 2
            dist = abs(cx - sx) + abs(cy - sy)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = screen

        vf = best.visibleFrame()
        # 留 4px 余量，避免贴着屏幕边缘时被 Dock/圆角遮住
        margin = 4.0
        max_x = vf.origin.x + vf.size.width - w - margin
        max_y = vf.origin.y + vf.size.height - h - margin
        min_x = vf.origin.x + margin
        min_y = vf.origin.y + margin
        # 屏幕比窗口还小时（极端情况）直接贴左上角
        if max_x < min_x:
            max_x = min_x = vf.origin.x
        if max_y < min_y:
            max_y = min_y = vf.origin.y
        return (min(max(x, min_x), max_x), min(max(y, min_y), max_y))

    @objc.python_method
    def _defaultOrigin(self) -> tuple[float, float]:
        """默认位置：主屏右下角，留出边距。"""
        try:
            from AppKit import NSScreen

            screen = NSScreen.mainScreen()
            frame = screen.visibleFrame()
            width, height = self._windowSize()
            return (
                float(frame.origin.x + frame.size.width - width - 40),
                float(frame.origin.y + 60),
            )
        except Exception:
            return (100.0, 100.0)

    # ── 轮询状态 ────────────────────────────────────────────

    def poll(self) -> None:
        """拉取状态快照并应用。失败不清空当前状态，只记数。"""
        try:
            with urllib.request.urlopen(STATE_URL, timeout=3) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            self.poll_fail_count += 1
            # 连续失败才提示，避免宿主重启瞬间刷日志。
            if self.poll_fail_count == 3:
                log(f"状态接口不可用: {exc}")
            self._setBubbleSub("连不上 AKM 服务，正在重试…")
            return

        if self.poll_fail_count:
            log("状态接口恢复")
        self.poll_fail_count = 0
        self.last_poll_ok = True
        self._apply(payload)

    @objc.python_method
    def _apply(self, payload: dict) -> None:
        """把服务端快照应用到窗口：配置、状态动作、气泡文案。"""
        cfg = payload.get("config") or {}
        new_scale = max(0.4, min(2.0, float(cfg.get("scale") or 100) / 100.0))
        new_bubble = bool(cfg.get("bubble", True))
        # 对话期间气泡被强制打开（输入框住在里面），此时不要被服务端的
        # bubble=false 覆盖回去，否则输入框会连同气泡一起消失。
        if self.chat_field is not None:
            new_bubble = True
        new_top = bool(cfg.get("always_on_top", True))
        self.allow_drag = bool(cfg.get("allow_drag", True))
        self.reduce_motion = bool(cfg.get("reduce_motion", False))
        # 漂移：待机时自己走动。默认关闭，只有插件配置显式开启才走。
        self.drift_enabled = bool(cfg.get("drift", False))
        # 锁定在当前屏幕（漫游/甩抛不跨屏）。
        # **之前这个字段从未从 cfg 读取**，所以插件里「锁定在当前屏幕」的
        # 开关一直是摆设——这里补上。
        self.lock_screen = bool(cfg.get("lock_screen", False))

        # 对话 / 碎碎念配置：由插件设置下发，缺省保持启动时的值
        if cfg.get("model"):
            self.model_name = str(cfg.get("model") or "")
        if cfg.get("base_url"):
            self.base_url = str(cfg.get("base_url") or self.base_url)
        if cfg.get("api_key"):
            self.api_key = str(cfg.get("api_key") or "")
        if cfg.get("chat_prompt"):
            self.chat_prompt = str(cfg.get("chat_prompt") or self.chat_prompt)
        if cfg.get("whisper_prompt"):
            self.whisper_prompt = str(cfg.get("whisper_prompt") or self.whisper_prompt)
        self.bubble_show_balance = bool(cfg.get("bubble_balance", True))
        self.whisper_enabled = bool(cfg.get("whisper", True))
        try:
            interval = int(cfg.get("whisper_interval_s") or self.whisper_interval_s)
            self.whisper_interval_s = max(30, interval)
        except (TypeError, ValueError):
            pass

        # 缩放 / 气泡开关变化需要重建窗口尺寸（就地调整比重启进程省事）
        if abs(new_scale - self.scale) > 0.001 or new_bubble != self.bubble_enabled:
            self.scale = new_scale
            self.bubble_enabled = new_bubble
            self._resizeWindow()
        if new_top != self.always_on_top:
            self.always_on_top = new_top
            if self.panel:
                self.panel.setLevel_(NSStatusWindowLevel if new_top else 0)

        # 状态动作：**不强制中断正在播放的动画**。
        #
        # 用户明确要求「所有地方不能强制中断已运行的动画，否则会造成动画闪动」。
        # 原来的做法是：服务端 clip 一变就立刻切 state_clip 并把 frame_index 归零，
        # 于是动画从第 1 帧重头开始——高频状态切换时（thinking→working→waiting）
        # 看起来就是一帧一帧地闪。
        #
        # 现在改为「排队等待」：新 clip 先记到 _pending_state_clip，
        # 等当前动作**播完一轮**（_tickOnce 里检测到 loop 边界）才真正切换。
        # 循环动作（idle/thinking/working/waiting）本来就会自然走到轮末，
        # 一次性动作（success/error）播完才切，都不会被打断。
        clip = str(payload.get("clip") or "idle")
        if clip != self.state_clip and self.cache.has(clip):
            # 只登记目标，真正切换在 _tickOnce 里「当前动作播完一轮」时执行。
            # 临时动作/手动锁定期间也照常登记，它们自然结束后会看到这个目标。
            self._pending_state_clip = clip

        # 气泡文案：一次性写满三行
        # 气泡：对话/碎碎念内容在展示期内优先，不被状态文案立刻覆盖
        if time.time() >= self.bubble_hold_until:
            # 碎碎念的展示期结束：把临时打开的气泡还原成用户原本的设置
            # （用户可能是「不显示气泡」，碎碎念只是临时借用一下）。
            self._restoreBubbleAfterWhisper()
            self._setBubbleLines(self._format_lines(payload))

        # 待机装饰动作调度（手动锁定动画时不插播，否则会打断用户选的动作）
        state = str(payload.get("state") or "idle")
        if state == "idle" and not self.reduce_motion and self.manual_clip is None:
            if time.time() - self.idle_decor_at > self.next_decor_delay:
                self._play_override(random.choice(IDLE_DECOR_CLIPS))
                self.idle_decor_at = time.time()
                self.next_decor_delay = random.uniform(30, 70)

    @objc.python_method
    def _format_lines(self, payload: dict) -> tuple[str, str, str]:
        """把快照格式化成三行气泡文本。

        第 1 行：状态文案（服务端已给，缺省用状态名）
        第 2 行：最近一次请求结果（成功/失败 + 模型 + 耗时）
        第 3 行：余额 + 累计计数
        """
        line1 = str(payload.get("text") or payload.get("state") or "待命中")

        last = payload.get("last_result") or {}
        if last:
            mark = "✓" if last.get("ok") else "✗"
            model = str(last.get("model") or "").strip()
            code = last.get("status_code") or ""
            ms = last.get("elapsed_ms") or 0
            line2 = f"{mark} {model}".strip()
            if code:
                line2 += f" {code}"
            if ms:
                line2 += f" · {ms}ms"
        else:
            line2 = "还没有请求经过 AKM"

        tail: list[str] = []
        # 余额是否显示由插件配置 bubble_show_balance 决定：
        # 关闭时整段不出现（连「未配置 Key」的提示也不显示），
        # 否则用户明明关了开关却还能看到余额行。
        if self.bubble_show_balance:
            bal = payload.get("balance") or {}
            rows = bal.get("balances") or []
            if rows:
                row = rows[0]
                amount = row.get("total", "-")
                currency = row.get("currency", "")
                prefix = "余额(上次)" if bal.get("stale") else "余额"
                tail.append(f"{prefix} {amount} {currency}".strip())
            elif bal.get("reason") == "no_key":
                tail.append("未配置 DeepSeek Key")

        counters = payload.get("counters") or {}
        if counters:
            tail.append(
                f"请求 {counters.get('requests', 0)} · "
                f"成功 {counters.get('success', 0)} · "
                f"失败 {counters.get('error', 0)}"
            )
        return line1, line2, "　".join(tail)

    @objc.python_method
    def _setBubbleLines(self, lines: tuple[str, str, str],
                        typewriter: bool = False) -> None:
        """写入三行气泡文本。

        ``typewriter=True`` 时第 0 行用打字机效果逐字显示（对话/碎碎念回复），
        其余行立即写入。逐字推进由 ``_tickTypewriter`` 在 tick 里驱动。
        """
        for index, (label, text) in enumerate(zip(self.bubble_rows, lines)):
            text = text or ""
            if index == 0 and typewriter and text:
                self._startTypewriter(text)
                continue
            label.setStringValue_(text)
        if not (typewriter and (lines[0] or "")):
            # 非打字机写入：清掉残留的逐字状态，避免下一帧把旧文本续上
            self._typewriter_text = ""
            self._typewriter_done = True
        self._afterTextChange()

    @objc.python_method
    def _startTypewriter(self, text: str) -> None:
        """开始逐字显示第 0 行。"""
        self._typewriter_text = text
        self._typewriter_index = 0
        self._typewriter_done = False
        # 长回复按总时长反推间隔，保证整句不超过 TYPEWRITER_MAX_S
        per_char = TYPEWRITER_CHAR_S
        if len(text) * per_char > TYPEWRITER_MAX_S:
            per_char = TYPEWRITER_MAX_S / max(1, len(text))
        self._typewriter_interval = per_char
        self._typewriter_next_at = time.time()
        if self.bubble_rows:
            self.bubble_rows[0].setStringValue_("")

    @objc.python_method
    def _tickTypewriter(self, now: float) -> None:
        """由 tick 调用：按时间推进逐字显示。

        一次 tick 可能补多个字（掉帧时按 elapsed 追），但至少 1 个，
        避免间隔小于 tick 周期时永远停在第一个字。
        """
        if getattr(self, "_typewriter_done", True):
            return
        text = getattr(self, "_typewriter_text", "")
        if not text or not self.bubble_rows:
            self._typewriter_done = True
            return
        if now < getattr(self, "_typewriter_next_at", 0):
            return
        interval = max(0.001, getattr(self, "_typewriter_interval", TYPEWRITER_CHAR_S))
        index = getattr(self, "_typewriter_index", 0)
        # 按经过的时间补字：掉帧或间隔很小时一次多显示几个
        step = max(1, int((now - self._typewriter_next_at) / interval) + 1)
        index = min(len(text), index + step)
        self._typewriter_index = index
        self.bubble_rows[0].setStringValue_(text[:index])
        self._typewriter_next_at = now + interval
        if index >= len(text):
            self._typewriter_done = True
        # 文本变长可能跨行，气泡高度要跟着变
        self._afterTextChange()

    @objc.python_method
    def _afterTextChange(self) -> None:
        """文本变化后同步气泡高度（换行行数可能变了）。"""
        if self.bubble is None or self.panel is None:
            return
        # 只在高度真的变了才重建窗口，避免每帧 setFrame 抖动
        try:
            want = self._bubbleHeight() if self.bubble_enabled else 0.0
            if abs(want - self.bubble.frame().size.height) > 0.5:
                self._resizeWindow()
            else:
                # 高度没变也要重排，因为换行行数可能变了（宽度不变、内容变了）
                self._layoutBubbleContent(
                    self.panel.frame().size.width, self.bubble.frame().size.height
                )
        except Exception:
            pass

    @objc.python_method
    def _setBubbleSub(self, text: str) -> None:
        """只更新第 3 行（连接失败提示等局部更新场景）。"""
        if len(self.bubble_rows) > 2:
            self.bubble_rows[2].setStringValue_(text)

    @objc.python_method
    def _labelTextHeight(self, label) -> float:
        """label 里文字实际需要的高度（ascender + |descender|）。

        **为什么要它**：``NSTextField`` 把文字画在 cell 顶部，而不是垂直居中。
        若 label 的 frame 高度是 22px 而行高只有 17px，多出的 5px 全部落在
        文字**下方**——三行看起来就紧贴在一起（用户报的「行高不对」）。
        把 frame 高度设成文字实际高度，多出来的空间就自然变成行间距。
        """
        font = label.font()
        if font is None:
            return BUBBLE_LINE_H * self.scale
        # +1 补偿字体行距（ascender - descender 不含 leading），避免字顶被裁
        return font.ascender() - font.descender() + 1.0

    @objc.python_method
    def _layoutBubbleContent(self, width: float, bubble_h: float) -> None:
        """统一排布气泡内部：三行文字 + （可选）底部输入行。

        三行文字**从顶部往下排**，但底部要给输入框留出 ``CHAT_ROW_H``——
        否则打开输入框后第三行会被输入框压住（用户看到的重叠就是这样）。

        这里是气泡内所有子视图唯一的排版入口：``_buildBubble`` 建完后、
        ``_resizeWindow`` 缩放后、开关输入框后都调它，避免三处各算一套坐标。
        """
        # **所有尺寸都要按 scale 缩放**：否则小尺寸下文字行高仍按 20px 排，
        # 会溢出到气泡外（scale=40/70 时实测 row1/row2 纵向越界）。
        s = self.scale
        pad = 10.0 * s

        # 字号必须在这里统一设置：_buildBubble 只在启动时设一次，之后改 scale
        # （插件配置里的角色大小）不会重建 label，字号不跟着变就会出现
        # 「行框缩了、字没缩」→ 40% 时三行重叠。
        # 字号下限 12pt：再小就看不清了（用户反馈 8pt 无法阅读）。
        # 角色缩小时气泡文字**不再跟着缩**，宁可让气泡相对变大也要保证可读。
        for index, label in enumerate(self.bubble_rows):
            base_pt = 12.0 if index == 0 else 11.0
            label.setFont_(NSFont.systemFontOfSize_(max(12.0, base_pt * s)))

        # 行高以「最大字号的文字高度 + 间距」为准，而不是机械乘 scale：
        # 字号有下限时行高若仍按 scale 缩，就会比文字还矮 → 重叠。
        max_text_h = max(self._labelTextHeight(lb) for lb in self.bubble_rows)
        # 与 _bubbleHeight 同一套算法（间距固定 8px），否则窗口高度与内部排版不一致
        line_h = max(BUBBLE_LINE_H * s, max_text_h + 6.0)

        # 第 0 行占 title_lines 行（可能换行）；第 1/2 行**只在有内容时**占一行。
        # 对话回复时后两行是空的，若也占高度，气泡底部会空出一大块。
        title_lines = self._titleLineCount()
        used_extra = sum(
            1 for lb in self.bubble_rows[1:] if str(lb.stringValue() or "").strip()
        )
        total_lines = title_lines + used_extra

        if self.chat_field is not None:
            # 有输入框时：文字区**从输入框上沿往上排**，各行紧贴其上。
            #
            # 输入行**完全不随 scale 缩放**：输入框字号固定 12pt、按钮标题
            # 固定 11pt，都不跟角色大小变。若高度按 scale 缩，scale=0.5 时
            # 只剩 17px，装不下 20px 输入框 → 下边框被裁；按钮也只有 13px，
            # 比文字还矮 → 「发送」上下被裁（用户截图就是这个现象）。
            field_h = CHAT_INPUT_H
            # 底边距用 pad：输入框有 1px 边框，贴太近会被气泡 masksToBounds 裁掉
            text_bottom = pad + field_h + 2.0    # 文字区底边与输入框留 2pt 间隙
            # 文字区可用高度：从 text_bottom 到气泡顶减边距。
            # 若总行数按 line_h 排会顶出气泡，就按可用高度压缩行距——
            # 宁可行距小一点，也不能让首行被裁。
            avail = bubble_h - pad - text_bottom
            need = line_h * max(1, total_lines)
            row_step = line_h if need <= avail else max(1.0, avail / max(1, total_lines))
            self._placeRows(pad, width, text_bottom, row_step, title_lines)
        else:
            # 无输入框：文字区底边 = 气泡底边 + 下边距
            self._placeRows(pad, width, pad, line_h, title_lines)


    @objc.python_method
    def _placeRows(self, pad: float, width: float, text_bottom: float,
                   row_step: float, title_lines: int) -> None:
        """把三行标签从文字区底边 ``text_bottom`` 往上排。

        第 0 行占 ``title_lines`` 行（多行换行，高度 = row_step * title_lines），
        第 1/2 行各占 1 行，紧贴在第 0 行**下方**。AppKit 的 y 轴向上，
        所以「往上排」= y 递增；第 0 行在最上，第 2 行最下（贴近底边）。

        抽成一个方法是因为有两个调用点（有/无输入框），各写一遍必然走样。
        """
        # 只有**有内容**的行才占位（对话回复时第 1/2 行是空的，
        # 让它们占位会在文字与输入框之间留出一大块空白）。
        extra_rows = [lb for lb in self.bubble_rows[1:]
                      if str(lb.stringValue() or "").strip()]
        total_lines = title_lines + len(extra_rows)
        # 文字区顶边：从底边往上数总行数
        text_top = text_bottom + row_step * total_lines

        # 第 0 行：占 title_lines 行，高度给足让它内部自己换行
        label0 = self.bubble_rows[0]
        h0 = row_step * title_lines
        label0.setFrame_(NSMakeRect(pad, text_top - h0, width - pad * 2, h0))

        # 有内容的第 1/2 行：各占 1 行，从第 0 行下沿继续往下
        for index, label in enumerate(extra_rows):
            text_h = self._labelTextHeight(label)
            row_top = text_top - h0 - row_step * index
            row_y = row_top - row_step + (row_step - text_h) / 2.0
            label.setFrame_(NSMakeRect(pad, row_y, width - pad * 2, text_h))
        # 输入框与发送按钮固定在气泡最底部（尺寸固定，不乘 scale，理由同上）
        if self.chat_field is not None:
            field_h = CHAT_INPUT_H
            btn_w = max(CHAT_BTN_W, self._sendButtonTitleSize()[0] + 16.0)
            self.chat_field.setFrame_(
                NSMakeRect(pad, pad, width - pad * 2 - btn_w - 6.0, field_h)
            )
            if getattr(self, "chat_send_btn", None) is not None:
                self.chat_send_btn.setFrame_(
                    NSMakeRect(width - pad - btn_w, pad, btn_w, field_h)
                )


    @objc.python_method
    def _resizeWindow(self) -> None:
        """缩放或气泡开关变化后重建视图尺寸，并保持右下角锚点不跳。"""
        if not self.panel:
            return
        old = self.panel.frame()
        width, height = self._windowSize()
        # 以左下角为锚点调整，避免角色位置乱跳
        self.panel.setFrame_display_(
            NSMakeRect(old.origin.x, old.origin.y, width, height), True
        )
        # 尺寸变了，窗口可能已经越界：把位置夹回屏幕内
        clamped = self._clampToScreens((old.origin.x, old.origin.y))
        if clamped != (old.origin.x, old.origin.y):
            self.panel.setFrameOrigin_(clamped)

        if self.image_view is not None:
            self.image_view.setFrame_(
                NSMakeRect(0, 0, BASE_PET_W * self.scale, BASE_PET_H * self.scale)
            )
        if self.bubble is not None:
            bubble_h = self._bubbleHeight()
            self.bubble.setFrame_(
                NSMakeRect(0, BASE_PET_H * self.scale, width, bubble_h)
            )
            self._layoutBubbleContent(width, bubble_h)
            self.bubble.setHidden_(not self.bubble_enabled)

    # ── 动画 ────────────────────────────────────────────────

    def tick(self) -> None:
        """定时器回调（16ms 一跳）：驱动物理、挤压、漫游，再推进动画帧。

        物理与动画分开推进：动画按素材 frameMs 走，物理按固定 dt 积分，
        两者互不影响。物理与漫游用同一次 now，避免一帧内时间漂移。

        **整体包 try/except**：这是定时器回调，一旦抛异常，NSTimer 会认为
        回调失败而**停止重复触发**——表现就是「动画不动、拖拽失灵、整个卡住」。
        之前任何一处小异常（比如某帧取不到图）都能让桌宠彻底僵死且日志空白。
        现在异常只跳过当前这一帧，并把原因写进日志，动画继续跑。
        """
        try:
            self._tickOnce()
        except Exception as exc:
            # 限流：同一种异常最多每 5 秒记一次，避免刷爆日志
            now = time.time()
            if now - getattr(self, "_last_tick_err_at", 0) > 5:
                self._last_tick_err_at = now
                log_exc("tick", exc)

    @objc.python_method
    def _tickOnce(self) -> None:
        """单帧实际逻辑（由 tick 包一层异常兜底）。"""
        now = time.time()
        # dt 上限防掉帧：窗口被系统挂起后恢复时不要一步跳到很远
        dt = min(0.05, max(0.0, now - self.last_physics_at)) if self.last_physics_at else 0.016
        self.last_physics_at = now

        # 物理优先级：拖拽跟手 > 甩抛飞行 > 漫游
        if self.dragging:
            self._stepDragSpring(dt)
        elif self.throwing:
            self._stepThrow(dt)
        else:
            self._stepWander(dt)
        self._stepSquash()

        # 打字机效果：逐字推进第 0 行（对话/碎碎念回复）
        self._tickTypewriter(now)

        # 宿主存活检测：AKM 关掉/崩溃后桌宠要一起退出，不留孤儿窗口。
        # 用 os.kill(pid, 0) 探测（不真发信号，只检查进程存在）。
        # 父进程正常关闭时会走 on_unload → stop_pet 主动终止子进程，
        # 这里是「宿主被强杀 / 崩溃」时的兜底。
        # 宿主存活检测：AKM 关掉/崩溃后桌宠要一起退出，不留孤儿窗口。
        # 用 os.kill(pid, 0) 探测（不真发信号，只检查进程存在）。
        #
        # **三种结果要分开处理**：
        #   - 正常返回 → 进程存在，继续
        #   - ProcessLookupError → 进程不存在 → 退出
        #   - PermissionError → 进程存在但无权限（不该发生在同一用户下）→ **继续**
        #     之前把 PermissionError 也当「已退出」，会在某些沙箱/权限场景下
        #     误杀桌宠（表现为「启动失败」）。
        # 另外启动后 10 秒内不做检测：宿主刚 spawn 完子进程、pid 还没稳定，
        # 过早探测容易误判。
        if PARENT_PID > 0 and now - getattr(self, "_last_parent_check", 0) > 2.0:
            self._last_parent_check = now
            if getattr(self, "_started_at", 0) and now - self._started_at < 10.0:
                pass  # 启动宽限期，先不检测
            else:
                gone = False
                try:
                    os.kill(PARENT_PID, 0)
                except ProcessLookupError:
                    gone = True
                except PermissionError:
                    gone = False   # 进程存在，只是没权限探测
                except Exception:
                    gone = False   # 其它异常保守当存在，不误杀
                if gone:
                    log(f"父进程 {PARENT_PID} 已退出，桌宠自行结束")
                    AppHelper.stopEventLoop()
                    return

        # 自动碎碎念：到点且空闲时触发一次
        # 兜底消费：后台线程若没能通过 performSelectorOnMainThread 回主线程，
        # 载荷会留在 _pending_reply，这里在主线程安全地处理掉，避免回复丢失。
        if self._pending_reply is not None:
            pending = self._pending_reply
            self._pending_reply = None
            self.handleModelReply_(pending)

        # 超时兜底：请求挂死超过 60 秒就解除忙碌，否则后续对话永远进不来
        # 心跳日志：每 30 秒一行，卡住时能据此判断进程是否还活着
        if now - getattr(self, "_last_heartbeat", 0) > 30:
            self._last_heartbeat = now
            log(f"心跳 clip={self.state_clip} override={self.override_clip} "
                f"manual={self.manual_clip} dragging={self.dragging} "
                f"throwing={self.throwing} frame={self.frame_index}")

        if self._busy and self._busy_since and now - self._busy_since > 60:
            self._busy = False
            self._setBubbleLines(("刚才那次没等到回复…", "再试一次吧", ""))
            log("对话请求超时，已解除忙碌状态")

        if (self.whisper_enabled and not self._busy
                and not self.dragging and not self.throwing
                and now >= self.next_whisper_at):
            self.next_whisper_at = now + self.whisper_interval_s
            # 只在待机时碎碎念，工作状态不打扰
            if self.state_clip == "idle" and self.model_name:
                self._askModel("", kind="whisper")

        clip = self._active_clip()
        frames = self.cache.frames(clip)
        if not frames:
            return
        now = time.time()
        step = self.cache.frame_ms(clip) / 1000.0
        # 减少动态：固定在第一帧，不推进
        if self.reduce_motion:
            if self.frame_index != 0:
                self.frame_index = 0
                self._showFrame(frames[0])
            return
        if now - self.last_frame_at < step:
            return
        self.last_frame_at = now
        if self.frame_index >= len(frames):
            # 用户要求「不能强制中断已运行的动画」——所以动作切换必须等
            # 当前动作完整播完一轮，而不是新 clip 一到就归零重播（那会闪）。
            #
            # 优先级：待播的 override（摸头/装饰/拖拽）> 待切的状态动作。
            if self.manual_clip is None:
                pend_override = getattr(self, "_pending_override_clip", "")
                if pend_override and self.cache.has(pend_override):
                    self._pending_override_clip = ""
                    self.override_clip = pend_override
                    new_frames = self.cache.frames(pend_override)
                    if new_frames:
                        self.frame_index = 1
                        self.last_frame_at = now
                        self._showFrame(new_frames[0])
                        return
            if self.override_clip is None and self.manual_clip is None:
                target = getattr(self, "_pending_state_clip", "")
                if target and target != self.state_clip and self.cache.has(target):
                    self.state_clip = target
                    self._pending_state_clip = ""
                    # 换动作了：重取帧列表，从第 1 帧开始播新动作
                    new_frames = self.cache.frames(target)
                    if new_frames:
                        self.frame_index = 1
                        self.last_frame_at = now
                        self._showFrame(new_frames[0])
                        return
            # **状态动作也要遵守 loop=False**：success / error 是一次性动画，
            # 播完应该停在最后一帧，而不是归零重播。
            if not self.cache.loops(clip) and self.override_clip is None:
                # 停在最后一帧，等状态回落（服务端按动画时长兜底切到 idle）
                self._showFrame(frames[len(frames) - 1])
                return
            self.frame_index = 0
        self._showFrame(frames[self.frame_index])
        self.frame_index += 1

        # 一次性动作播完就回落到下一层（手动锁定或状态动作）
        # 注意：manual_clip 是「手动锁定」，不参与自动回落——即使素材标了
        # loop=False，用户选中的动作也要循环播放，直到主动点「跟随状态」。
        if self.override_clip is not None and self.frame_index >= len(frames):
            if not self.cache.loops(self.override_clip):
                self.override_clip = None
                self.frame_index = 0
                self.last_frame_at = 0.0

    @objc.python_method
    def _showFrame(self, image) -> None:
        if self.image_view is not None:
            self.image_view.setImage_(image)

    @objc.python_method
    def _active_clip(self) -> str:
        """当前该播哪个动作。优先级：手动锁定 > 一次性动作 > 状态动作。

        手动锁定放最高：用户在右键菜单里明确选了一个动画，摸头/拖拽这类
        自动触发的临时动作不应该把它顶掉，否则「选中的动画」会被点一下就丢。
        """
        if self.manual_clip and self.cache.has(self.manual_clip):
            return self.manual_clip
        return self.override_clip or self.state_clip

    @objc.python_method
    def _play_override(self, clip: str) -> None:
        """请求播放一次性动作（摸头 / 装饰 / 拖拽 / 思考等）。

        不存在该素材时静默跳过；手动锁定动画期间直接忽略（否则点击角色会把
        用户选中的动画切走）。

        **不强制中断正在播放的动作**（用户要求：强制中断会造成动画闪动）：
        新动作只登记到 `_pending_override_clip`，等当前动作播完一轮后
        由 `_tickOnce` 在轮末切换。这样摸头、拖拽、思考这些叠加触发
        不会把正在播的动画切成两截。
        """
        if self.manual_clip is not None:
            return
        if not self.cache.has(clip):
            return
        # 当前没有任何动作在播（override 为空且状态动作已到轮末）时直接生效；
        # 否则排队，等 _tickOnce 在轮末切换。
        self._pending_override_clip = clip

    # ── 屏幕空间（多屏） ────────────────────────────────────

    @objc.python_method
    def _screenSpace(self) -> physics.ThrowSpace:
        """用 NSScreen 构造抛掷空间：每块屏的工作区 + 面板。

        ``visibleFrame()`` 是工作区（不含菜单栏 / Dock），``frame()`` 是整块面板。
        抛掷边界按工作区算（脚不踩 Dock），跨屏探测按面板算（能穿过屏缝）。
        """
        from AppKit import NSScreen

        areas: list[physics.Rect] = []
        panels: list[physics.Rect] = []
        for screen in NSScreen.screens():
            vf = screen.visibleFrame()
            fr = screen.frame()
            areas.append(physics.Rect(vf.origin.x, vf.origin.y, vf.size.width, vf.size.height))
            panels.append(physics.Rect(fr.origin.x, fr.origin.y, fr.size.width, fr.size.height))
        size = BASE_PET_W * self.scale
        return physics.throw_space(areas, size=size, side_allow=SIDE_ALLOW * self.scale, panels=panels)

    @objc.python_method
    def _lockedScreenIndex(self) -> int:
        """锁定在当前屏幕时返回窗口所在屏索引；未锁定返回 -1。

        锁定语义：甩抛 / 漫游都只在当前这块屏内进行，屏缝当墙。
        """
        if not self.lock_screen or self.panel is None:
            return -1
        from AppKit import NSScreen

        frame = self.panel.frame()
        cx = frame.origin.x + frame.size.width / 2
        cy = frame.origin.y + frame.size.height / 2
        for index, screen in enumerate(NSScreen.screens()):
            vf = screen.visibleFrame()
            if (vf.origin.x <= cx < vf.origin.x + vf.size.width
                    and vf.origin.y <= cy < vf.origin.y + vf.size.height):
                return index
        return -1

    # ── 甩抛 ────────────────────────────────────────────────

    @objc.python_method
    def _startThrow(self, vx: float, vy: float) -> None:
        """以给定初速进入抛体飞行。"""
        if self.panel is None:
            return
        frame = self.panel.frame()
        self.throw_state = physics.ThrowState(
            x=frame.origin.x,
            y=self._flipY(frame.origin.y + frame.size.height),
            vx=vx,
            vy=vy,
        )
        self.throwing = True
        self.wander_target = None
        self.last_physics_at = time.time()
        self._play_override("dragging")

    @objc.python_method
    def _flipY(self, bottom_y: float) -> float:
        """把 AppKit 左下原点坐标转成物理用的左上原点坐标。

        多屏时以主屏顶端为基准即可——物理只需一个一致的 y 轴方向，
        绝对值不重要，重要的是 y 增大等于向下。
        """
        from AppKit import NSScreen

        screens = NSScreen.screens()
        if not screens:
            return bottom_y
        main = screens[0].frame()
        top = main.origin.y + main.size.height
        return top - bottom_y

    @objc.python_method
    def _stepThrow(self, dt: float) -> None:
        """推进抛体物理：多屏反弹 → 落地摩擦 → 静止判定。"""
        if not self.throwing or self.panel is None:
            return
        space = self._screenSpace()
        result = physics.throw_step_region(
            self.throw_state, dt, space, lock_screen=self._lockedScreenIndex()
        )
        self.throw_state = physics.ThrowState(x=result.x, y=result.y, vx=result.vx, vy=result.vy)

        h = space.size * 9.0 / 16.0
        bottom_y = self._flipY(result.y) - h
        self.panel.setFrameOrigin_((result.x, bottom_y))

        if result.bounced:
            impact = abs(result.vy) if abs(result.vy) > 1 else abs(result.vx)
            squash = physics.landing_squash(impact)
            if squash < 0.99:
                self._startSquash(squash)

        if result.atRest:
            self.throwing = False
            self._save_layout()
            self.next_wander_at = time.time() + random.uniform(*WANDER_GAP)

    @objc.python_method
    def _settleInPlace(self) -> None:
        """温柔放下：原地停住，只把窗口夹回当前屏的可视范围内。

        不触发下落——用户轻放就是想让它在当前位置待着。
        """
        if self.panel is None:
            return
        space = self._screenSpace()
        frame = self.panel.frame()
        index = self._lockedScreenIndex()
        if index < 0:
            index = physics.screen_of_box(space, frame.origin.x, self._flipY(frame.origin.y))
        if 0 <= index < len(space.bounds):
            b = space.bounds[index]
            x = min(max(frame.origin.x, b.minX), b.maxX)
            self.panel.setFrameOrigin_((x, frame.origin.y))
        self.next_wander_at = time.time() + random.uniform(*WANDER_GAP)

    # ── Q 弹挤压 ────────────────────────────────────────────

    @objc.python_method
    def _startSquash(self, squash: float) -> None:
        """开始一次 Q 弹挤压。减少动态时跳过。

        **幂等保护**：一次挤压还在播的时候不要重置起始时间。之前每次点击都
        无条件 ``squash_started_at = now``，连续双击时第二下把计时打回 0，
        配合逐帧推进就会出现「角色僵在压缩姿势不动」的观感。
        这里：正在播时只按需加深压缩比，不重置时间轴。
        """
        if self.reduce_motion or self.panel is None:
            return
        if self.squash_active:
            # 已经在压：取更狠的那个压缩比，但不重置进度
            self.squash_squash = min(self.squash_squash, squash)
            return
        self.squash_active = True
        self.squash_started_at = time.time()
        self.squash_squash = squash
        self._applySquash(1.0)

    @objc.python_method
    def _notify_if_unfocused(self, title: str, message: str) -> None:
        """窗口失焦时弹系统通知。

        用 ``osascript`` 而不是 NSUserNotification：后者在较新的 macOS 上已废弃
        且需要签名授权，osascript 的 display notification 在任何环境下都能用，
        也不依赖 App 的 bundle id。

        节流 10 秒：连续对话时不要刷屏。
        """
        now = time.time()
        if now - self.last_notify_at < 10:
            return
        if self.panel is not None and self.panel.isKeyWindow():
            # 窗口在前台就不打扰
            return
        self.last_notify_at = now
        # 转义引号，避免 AppleScript 语法错误
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')[:200]
        script = (
            f'display notification "{safe_msg}" '
            f'with title "{safe_title}"'
        )
        try:
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    @objc.python_method
    def _stepSquash(self) -> None:
        """按曲线推进挤压；播完恢复原状。"""
        if not self.squash_active:
            return
        elapsed_ms = (time.time() - self.squash_started_at) * 1000.0
        u = elapsed_ms / physics.SQ_DURATION_MS
        if u >= 1.0:
            self.squash_active = False
            self._applySquash(1.0)
            return
        self._applySquash(physics.squash_scale(u, self.squash_squash))

    @objc.python_method
    def _applySquash(self, scale_y: float) -> None:
        """把 scaleY 应用到角色，脚踩地往下蹲。

        **不用 layer.anchorPoint**：改锚点会连带改变图层的定位基准，而
        ``position`` 不会跟着补偿——实测挤压一次后 anchorPoint 从 (0,0) 变成
        (0.5, 0)，图层永久左移半个宽度，角色只剩右半边可见（用户报的
        「只剩一半」）。而且挤压结束后锚点也不会自己还原。

        改成直接调整 view 的 frame：纵向按 scale_y 压扁，并让**底边保持不动**
        （压扁时向下收缩而不是整体上移）；横向按 scale_x 略微撑开并保持中心。
        这套算法只用 frame，不碰 layer 状态，天然无残留。
        """
        view = self.image_view
        if view is None:
            return

        base_w = BASE_PET_W * self.scale
        base_h = BASE_PET_H * self.scale
        # 压缩时横向略微放大（体积守恒的视觉暗示），但不超过 1.08
        scale_x = min(1.0 + (1.0 - scale_y) * 0.35, 1.08)

        new_w = base_w * scale_x
        new_h = base_h * scale_y
        # 横向居中、纵向贴底（底边 y 保持 0 不变，即脚不动）
        new_x = (base_w - new_w) / 2.0
        view.setFrame_(NSMakeRect(new_x, 0.0, new_w, new_h))

    # ── 屏幕漫游 ────────────────────────────────────────────

    @objc.python_method
    def _stepWander(self, dt: float) -> None:
        """漫游：朝朝向方向走，走到目标就停下等下一轮。

        目标由 ``plan_move`` 规划，越界会返回 None，此时翻个朝向重试一次，
        即先探测空间、不走出屏幕。
        """
        if self.panel is None or self.dragging or self.throwing or self.reduce_motion:
            return
        # **漂移开关（默认关闭）**：关掉时桌宠只在原地待机，不会自己走动。
        # 想移动请直接拖动它；开启后才恢复「待机时自己走」的行为。
        if not self.drift_enabled:
            return
        if self.manual_clip is not None or self.override_clip is not None:
            return
        if self.state_clip != "idle":
            return

        now = time.time()
        frame = self.panel.frame()

        if self.wander_target is None:
            if now < self.next_wander_at:
                return
            space = self._screenSpace()
            size = space.size
            cx = frame.origin.x + size / 2
            cy = self._flipY(frame.origin.y + frame.size.height)
            target = None
            for _ in range(2):
                distance = random.uniform(WANDER_MIN_DIST, WANDER_MAX_DIST)
                target = physics.plan_move(
                    cx=cx, cy=cy, W=frame.size.width, H=frame.size.height,
                    direction=self.wander_dir,
                    min_dist=WANDER_MIN_DIST, max_dist=WANDER_MAX_DIST,
                    margin=WANDER_MARGIN, half_w=size / 2,
                    distance=distance, side_allow=space.sideAllow,
                    areas=space.areas,
                )
                if target is not None:
                    break
                self.wander_dir *= -1
            if target is None:
                self.next_wander_at = now + random.uniform(*WANDER_GAP)
                return
            self.wander_target = target - size / 2
            return

        delta = self.wander_target - frame.origin.x
        if abs(delta) <= WANDER_SPEED * dt:
            self.panel.setFrameOrigin_((self.wander_target, frame.origin.y))
            self.wander_target = None
            self.next_wander_at = time.time() + random.uniform(*WANDER_GAP)
            return
        step = math.copysign(WANDER_SPEED * dt, delta)
        self.panel.setFrameOrigin_((frame.origin.x + step, frame.origin.y))
    # ── 鼠标交互 ────────────────────────────────────────────

    @objc.python_method
    def handleMouseDown(self, event) -> None:
        """按下：记录抓取偏移，进入「拖拽中」但还没开始移动。

        抓取偏移用屏幕绝对坐标（与鼠标位置同一坐标系），拖动时窗口位置 =
        鼠标 - 偏移，这样指针下的点始终贴着角色，不会跳。
        """
        if not self.allow_drag or self.panel is None:
            return
        log("鼠标按下")
        # 抓起时打断甩抛与漫游
        self.throwing = False
        self.wander_target = None
        frame = self.panel.frame()
        mouse = NSEvent.mouseLocation()
        self.drag_start = (mouse.x - frame.origin.x, mouse.y - frame.origin.y)
        self.window_origin = (frame.origin.x, frame.origin.y)
        self.drag_trail = []
        self.spring_vx = 0.0
        self.spring_vy = 0.0
        # 记录按下时刻，用于区分「点一下」与「拖走」
        self.drag_pressed_at = time.time()

    @objc.python_method
    def handleMouseDragged(self, event) -> None:
        """拖动：超过 5px 阈值才算拖动；窗口用弹簧追鼠标（过阻尼跟手）。

        不直接 ``setFrameOrigin(鼠标 - 偏移)``，而是把「鼠标 - 偏移」当弹簧目标：
        窗口永远差一点点追上，于是快速甩动时会有轻微滞后与回弹——这是原版
        「过阻尼弹簧跟手」的手感来源。
        """
        if not self.allow_drag or self.panel is None:
            return
        if self.drag_start is None or self.window_origin is None:
            return
        mouse = NSEvent.mouseLocation()
        target_x = mouse.x - self.drag_start[0]
        target_y = mouse.y - self.drag_start[1]

        if not self.dragging:
            manhattan = abs(target_x - self.window_origin[0]) + abs(target_y - self.window_origin[1])
            if manhattan <= DRAG_THRESHOLD_PX:
                return
            self.dragging = True
            log("开始拖拽")
            self._play_override("dragging")
            # 起始速度清零，避免按下瞬间的旧值造成弹跳
            self.spring_vx = 0.0
            self.spring_vy = 0.0

        # 记录轨迹采样（松手时据此估算甩出速度）
        now_ms = time.time() * 1000.0
        self.drag_trail.append(physics.DragSample(t=now_ms, x=mouse.x, y=mouse.y))
        if len(self.drag_trail) > 64:
            del self.drag_trail[:-64]
        self.drag_trail = physics.trim_trail(self.drag_trail, now_ms)

        # 弹簧推进：dt 由 tick 统一驱动更稳，这里只更新目标，
        # 实际位移在 _stepDragSpring 里按固定步长积分。
        self.drag_target = (target_x, target_y)

    @objc.python_method
    def _stepDragSpring(self, dt: float) -> None:
        """拖拽弹簧积分：让窗口位置追 ``drag_target``。

        单独成函数是因为它必须由 tick 按固定频率驱动——放在 mouseDragged 里
        会变成「鼠标事件频率 = 物理频率」，快速拖动时弹簧行为会随事件率漂移。
        """
        if not self.dragging or self.panel is None or self.drag_target is None:
            return
        frame = self.panel.frame()
        cur_x, cur_y = frame.origin.x, frame.origin.y
        tgt_x, tgt_y = self.drag_target
        self.spring_vx = physics.spring_step(self.spring_vx, cur_x, tgt_x, dt)
        self.spring_vy = physics.spring_step(self.spring_vy, cur_y, tgt_y, dt)
        self.panel.setFrameOrigin_((cur_x + self.spring_vx * dt, cur_y + self.spring_vy * dt))

    @objc.python_method
    def handleMouseUp(self, event) -> None:
        """松手：拖动过就甩出去（抛物线），没拖动过算点击。

        甩出速度由拖拽轨迹估算；低于死区速度即「温柔放下」——原地停住不下落，
        避免用户轻放时宠物莫名其妙掉到屏幕底部。

        **双击单独处理**：原来完全不看 ``clickCount``，双击会连续走两次
        「播放摸头 + 重置挤压」，第二次把挤压计时重置回 0 又立刻被下一帧
        覆盖，视觉上就是角色僵在一个姿势不动（用户说的「双击卡住」）。
        现在：单击摸头、双击换成 ``poke`` 并给更强的挤压，且同一时刻只保留
        一个进行中的挤压。
        """
        was_dragging = self.dragging
        self.dragging = False
        self.drag_start = None
        self.window_origin = None
        self.drag_target = None

        # 取点击次数（事件可能为 None，例如内部调用/测试）
        click_count = 1
        if event is not None:
            try:
                click_count = int(event.clickCount())
            except Exception:
                click_count = 1

        if was_dragging:
            now_ms = time.time() * 1000.0
            vx, vy = physics.release_velocity(self.drag_trail, now_ms)
            self.drag_trail = []
            self._play_override("dragging_release")
            if vx or vy:
                # 有速度：进入抛体飞行（含边缘反弹、落地摩擦、落地 Q 弹）
                log(f"甩出 vx={vx:.0f} vy={vy:.0f}")
                self._startThrow(vx, vy)
            else:
                # 温柔放下：原地停住，只吸附到当前屏的可站范围
                self._settleInPlace()
            self._save_layout()
            return

        # 点击：单击摸头、双击戳一下（都是「一次性动作」，播完自动回落）
        log(f"鼠标松开 clickCount={click_count} was_dragging={was_dragging}")
        if click_count >= 2:
            clip = "poke" if self.cache.has("poke") else "head_pat"
            # 双击给更明显的挤压反馈
            self._startSquash(physics.SQ_MAX_SQUASH)
        else:
            clip = next((c for c in CLICK_CLIPS if self.cache.has(c)), "")
            self._startSquash(physics.SQ_SQUASH)
        if clip:
            self._play_override(clip)

    @objc.python_method
    def handleRightClick(self, event) -> None:
        """右键菜单：选择动画 / 隐藏这次运行 / 退出桌宠。

        外观项（大小、减少动态、始终置顶）统一由插件配置管理，保存后自动重启
        桌宠生效，右键不再重复提供，避免两处入口各改一半、状态对不上。
        """
        menu = NSMenu.alloc().init()

        # ── 选择动画子菜单 ──
        # 列出素材里实际存在的全部动作；选中后手动锁定播放，直到点「跟随状态」。
        anim_root = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "选择动画", b"", ""
        )
        submenu = NSMenu.alloc().initWithTitle_("选择动画")

        # 「跟随状态」放最前：清除手动锁定，回到 AKM 请求状态驱动
        follow = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            ("✓ " if self.manual_clip is None else "") + "跟随状态",
            b"menuSelectClip:",
            "",
        )
        follow.setTarget_(self)
        follow.setTag_(0)
        submenu.addItem_(follow)
        submenu.addItem_(NSMenuItem.separatorItem())

        # 按 manifest 顺序列出所有动作，当前手动选中的打勾
        for index, clip in enumerate(self.cache.clip_names(), start=1):
            mark = "✓ " if self.manual_clip == clip else ""
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"{mark}{CLIP_LABELS.get(clip, clip)}  ({clip})",
                b"menuSelectClip:",
                "",
            )
            item.setTarget_(self)
            # tag 0 保留给「跟随状态」，动作从 1 开始编号
            item.setTag_(index)
            submenu.addItem_(item)

        anim_root.setSubmenu_(submenu)
        menu.addItem_(anim_root)
        menu.addItem_(NSMenuItem.separatorItem())

        # ── 对话 ──
        chat_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "跟我说话…", b"menuChat:", ""
        )
        chat_item.setTarget_(self)
        menu.addItem_(chat_item)

        # ── 碎碎念（手动触发一次）──
        whisper_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "说点什么", b"menuWhisper:", ""
        )
        whisper_item.setTarget_(self)
        menu.addItem_(whisper_item)
        menu.addItem_(NSMenuItem.separatorItem())

        hide = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "隐藏这次运行", b"menuHide:", ""
        )
        hide.setTarget_(self)
        menu.addItem_(hide)

        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "退出桌宠", b"menuQuit:", ""
        )
        quit_item.setTarget_(self)
        menu.addItem_(quit_item)

        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self.panel.contentView())

    def menuSelectClip_(self, sender) -> None:
        """右键选择动画：tag 0 = 跟随状态，其余按序号取对应 clip。"""
        tag = int(sender.tag())
        if tag <= 0:
            # 清除手动锁定，交还给 AKM 状态驱动
            self.manual_clip = None
            self.frame_index = 0
            self.last_frame_at = 0.0
            log("右键菜单：恢复跟随状态")
            return
        names = self.cache.clip_names()
        if not 1 <= tag <= len(names):
            return
        clip = names[tag - 1]
        if not self.cache.has(clip):
            return
        # 手动锁定：优先于状态动作，且不因播完而回落
        self.manual_clip = clip
        self.frame_index = 0
        self.last_frame_at = 0.0

    # ── 对话 / 碎碎念 / 通知 ────────────────────────────────
    def menuChat_(self, sender) -> None:
        """右键「跟我说话」：把输入框直接嵌进气泡里，回车或点「发送」提交。

        为什么不再用独立窗口：
        1. 独立 ``NSPanel`` 会飘在角色旁边，与气泡风格割裂；
        2. 它需要 ``makeKeyAndOrderFront`` 抢焦点，在 ``Accessory`` 激活策略下
           还要先 ``activateIgnoringOtherApps_``，容易打不了字；
        3. 用 ``NSAlert.runModal()`` 更糟——嵌套模态循环会把 ``tick``/``poll``
           定时器全挡住，整个桌宠卡死。

        现在改为在**气泡内部**放一个可编辑 ``NSTextField``：气泡本来就贴在角色
        上方、已随窗口一起移动，输入时只需把窗口设为 key 并让输入框成为
        first responder，不需要额外的窗口层级与焦点抢占。
        """
        from AppKit import NSButton, NSBezelStyleRounded, NSTextField

        if self.chat_field is not None:
            # 已经开着：聚焦回输入框
            self._focusChatField()
            return
        if self.bubble is None:
            return

        # 输入框住在气泡里，所以对话期间气泡必须可见。
        # 记住用户原本的开关状态，关闭对话后还原——不能把用户关掉的气泡
        # 永久打开（那等于偷偷改了配置）。
        if not self.bubble_enabled:
            self._bubble_was_enabled = False
            self.bubble_enabled = True
            self.bubble.setHidden_(False)
            self._resizeWindow()

        bubble_h = self._bubbleHeight()
        width = BASE_PET_W * self.scale
        pad = 10.0

        # 输入框放在气泡**最下面一行**（AppKit y 轴向上，y 小 = 靠下）。
        # 输入框放在气泡**最下面一行**（AppKit y 轴向上，y 小 = 靠下）。
        # 具体坐标由 _layoutBubbleContent 统一计算，这里只给占位 frame。
        # **占位尺寸也要用固定 CHAT_INPUT_H，不能乘 scale**：输入框字号固定
        # 12pt，scale=0.5 时按比例缩成 13px 会装不下文字。
        field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(pad, pad, width - pad * 2 - CHAT_BTN_W - 6.0, CHAT_INPUT_H)
        )
        # 换成垂直居中文字的 cell：默认 cell 把文字画在绘制区顶部，
        # 26px 输入框里会显得贴顶、下方留白（详见 CenteredTextFieldCell 注释）。
        #
        # **必须显式清空 stringValue**：``NSTextFieldCell`` 的默认值是
        # 字符串 "Field"（``NSTextField`` 默认是空串）。直接 alloc().init()
        # 新建 cell 再 setCell_，就把这个默认值带进了输入框——
        # 用户看到的输入框里预填了「Field」就是这个原因。
        _centered = CenteredTextFieldCell.alloc().init()
        _centered.setStringValue_("")
        field.setCell_(_centered)
        field.setPlaceholderString_("跟我说点什么…（回车发送，Esc 取消）")
        field.setFont_(NSFont.systemFontOfSize_(12.0))
        field.setBezeled_(True)
        field.setEditable_(True)
        field.setSelectable_(True)
        field.setTarget_(self)
        field.setAction_(b"chatSubmit:")
        self.bubble.addSubview_(field)

        # 「发送」按钮放在输入框右侧，带 \r 等价键接管回车。
        # 占位尺寸同样固定（不乘 scale），真实尺寸由 _layoutBubbleContent 定。
        send_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - pad - CHAT_BTN_W, pad, CHAT_BTN_W, CHAT_INPUT_H)
        )
        # 位置稍后由 _layoutBubbleContent 统一排
        send_btn.setTitle_("发送")
        send_btn.setBezelStyle_(NSBezelStyleRounded)
        # **必须用 mini(3)，不能用 small(1)**：small 的 bezel 绘制高度固定
        # 14px，而输入框 26px，按钮看起来矮一截（用户反馈「怪怪的」）。
        # mini 的 bezel 高度约 24px，能填满 26px 的输入行，视觉齐平。
        send_btn.setControlSize_(3)   # NSControlSizeMini
        send_btn.setFont_(NSFont.systemFontOfSize_(11.0))
        send_btn.setTarget_(self)
        send_btn.setAction_(b"chatSendClicked:")
        send_btn.setKeyEquivalent_("\r")
        self.bubble.addSubview_(send_btn)

        self.chat_field = field
        self.chat_send_btn = send_btn
        # 气泡要加高一行来容纳输入框：必须重建窗口尺寸，否则输入框会被裁掉
        self._resizeWindow()
        # 不要把三行文字改成提示：那会把余额/计数挤掉，用户以为信息丢了。
        # 操作提示放输入框 placeholder（见上面 setPlaceholderString_），
        # 三行状态信息在输入期间保持原样可见。

        # 让桌宠窗口本身成为 key（它现在是输入宿主），再聚焦输入框
        app = objc.lookUpClass("NSApplication").sharedApplication()
        app.activateIgnoringOtherApps_(True)
        if self.panel is not None:
            self.panel.makeKeyWindow()
        self._focusChatField()

    @objc.python_method
    def _focusChatField(self) -> None:
        """把键盘焦点交给气泡内的输入框。"""
        field = self.chat_field
        if field is None or self.panel is None:
            return
        self.panel.makeFirstResponder_(field)
        # 光标移到末尾
        try:
            editor = field.currentEditor()
            if editor is not None:
                editor.setSelectedRange_((len(field.stringValue()), 0))
        except Exception:
            pass

    def chatSubmit_(self, sender) -> None:
        """输入框回车：取文本、收起输入、发请求。"""
        text = str(sender.stringValue() or "").strip()
        self._closeChatPanel()
        if not text:
            return
        self._askModel(text, kind="chat")

    def chatSendClicked_(self, sender) -> None:
        """「发送」按钮：与回车等价。

        按钮的 sender 是 NSButton、输入框的 sender 是 NSTextField，取值方式不同，
        所以统一从 ``self.chat_field`` 读文本。
        """
        field = self.chat_field
        text = str(field.stringValue() or "").strip() if field is not None else ""
        self._closeChatPanel()
        if not text:
            return
        self._askModel(text, kind="chat")

    def chatCancel_(self, sender) -> None:
        """取消输入（Esc 或关闭）。"""
        self._closeChatPanel()

    @objc.python_method
    def _closeChatPanel(self) -> None:
        """收起气泡内的输入框，恢复状态文案。"""
        if self.chat_field is not None:
            try:
                self.chat_field.removeFromSuperview()
            except Exception:
                pass
            self.chat_field = None
        if getattr(self, "chat_send_btn", None) is not None:
            try:
                self.chat_send_btn.removeFromSuperview()
            except Exception:
                pass
            self.chat_send_btn = None
        # 还原用户原本的气泡开关状态（见 menuChat_ 里的记录）
        if not getattr(self, "_bubble_was_enabled", True):
            self._bubble_was_enabled = True
            self.bubble_enabled = False
        # 气泡收回那一行高度，窗口跟着缩回去（否则底部会留一条空白）
        self._resizeWindow()
        # 下一帧 tick 会把状态文案写回来（清掉保持时间）
        self.bubble_hold_until = 0.0


    def menuWhisper_(self, sender) -> None:
        """右键「说点什么」：手动触发一次碎碎念。"""
        self._askModel("", kind="whisper")

    @objc.python_method
    def _askModel(self, user_text: str, kind: str = "chat") -> None:
        """在后台线程请求模型，结果回主线程更新气泡。

        AppKit 只能在主线程操作 UI，因此这里用 ``performSelectorOnMainThread``
        回主线程；网络请求本身放后台，避免卡住动画。
        """
        # 忙碌中：超过 60 秒视为上一次请求挂死，强制放行（不永久锁死）
        if self._busy:
            if time.time() - self._busy_since > 60:
                self._busy = False
                log("对话请求超时，强制解除忙碌状态")
            else:
                self._setBubbleLines(("我正在想上一句呢…", "", ""))
                return
        if not self.model_name:
            self._setBubbleLines(("还没配置模型呢", "在插件设置里选一个模型吧", ""))
            return
        self._busy = True
        self._busy_since = time.time()
        self._setBubbleLines(("正在想…" if kind == "chat" else "让我想想说点啥…", "", ""))
        # 思考动作
        self._play_override("thinking")

        # 碎碎念要「文本 + 动作」一起回来：thinking 动画播完也未必有结果，
        # 这里仍先播 thinking（表示「在想」），等回复到了再换成模型选的动作。
        self._play_override("thinking")

        memory = self.memory
        if kind == "chat":
            messages = companion.build_chat_messages(memory, user_text, self.chat_prompt)
        else:
            # 碎碎念用 JSON 提示词：让模型同时给出「一句话」和「配哪个动作」。
            # 用 companion.WHISPER_JSON_PROMPT 而不是用户自定义的 chat_prompt——
            # 后者是自由文本提示词，模型不会按 JSON 输出。
            messages = companion.build_whisper_messages(
                memory.whispers, companion.WHISPER_JSON_PROMPT
            )

        import threading

        def worker() -> None:
            """后台线程：请求模型，然后把结果甩回主线程。

            **必须保证任何路径都能解除 _busy**：之前只要
            ``performSelectorOnMainThread`` 这一步本身抛异常（selector 名错、
            对象已释放、主线程队列异常等），``_busy`` 就永久停在 True，
            之后每次对话都只回「我正在想上一句呢」——用户看到的就是卡死。
            这里用 try/except 兜底，异常时直接在主线程同步清状态。
            """
            try:
                result = companion.call_chat(
                    base_url=self.base_url,
                    model=self.model_name,
                    messages=messages,
                    api_key=self.api_key,
                )
            except Exception as exc:
                # 连 call_chat 内部都兜不住时，至少造一个失败结果
                result = companion.LlmResult(ok=False, error=str(exc) or "请求失败")

            try:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    b"handleModelReply:",
                    {"result": result, "kind": kind, "user": user_text},
                    False,
                )
            except Exception:
                # 甩不回主线程也不能让 _busy 卡死：置位让 tick 下一帧重试
                self._pending_reply = {
                    "result": result, "kind": kind, "user": user_text,
                }

        threading.Thread(target=worker, daemon=True).start()

    def handleModelReply_(self, payload) -> None:
        """主线程：把模型结果写进气泡与记忆。

        **注意**：``performSelectorOnMainThread`` 传过来的 dict 会被 pyobjc 转成
        ``NSDictionary``，而 ``isinstance(NSDictionary, dict)`` 是 **False**——
        之前用 ``isinstance(payload, dict)`` 做守卫，导致这里永远直接 return：
        回复不显示、``_busy`` 永远停在 True，之后再点对话只会提示
        「我正在想上一句呢」，看起来就是整个卡死。
        因此这里统一用「鸭子类型」读取：``NSDictionary`` 与 ``dict`` 都支持
        ``get`` / ``[]``，用 ``getattr`` 取到 get 方法即可，不做 isinstance 判断。
        """
        self._busy = False
        getter = getattr(payload, "get", None)
        if not callable(getter):
            self._setBubbleLines(("出了点问题…", "", ""))
            return
        result = getter("result")
        kind = str(getter("kind") or "chat")
        user_text = str(getter("user") or "")
        if result is None:
            self._setBubbleLines(("出了点问题…", "", ""))
            return

        if not result.ok:
            # 失败不伪造回复，明确告知原因
            self._setBubbleLines((f"没成功：{result.error}", "", ""))
            return

        text = result.text
        # 碎碎念：解析出「气泡文本 + 动画 clip」。对话保持原文不解析
        # （用户自己说的话，模型回复里不该出现动作名）。
        whisper_clip = ""
        if kind == "whisper":
            text, whisper_clip = companion.parse_whisper_reply(text)
        if not text:
            text = "…"
        now = time.time()
        if kind == "chat":
            self.memory.chat.append(
                companion.ChatTurn(user=user_text, assistant=text, at=now)
            )
        else:
            self.memory.whispers.append({"text": text, "at": now})
        companion.save_memory(self.memory, self.memory_file)

        # 气泡展示 + 说话动作；保持 8 秒不被状态文案覆盖。
        # 回复用打字机逐字显示（对话与碎碎念都走这里）。
        # 保持时长按文字长度放宽：长回复逐字显示本身要几秒，
        # 固定 8 秒会让它刚显示完就被状态文案顶掉。
        self.bubble_hold_until = time.time() + 8.0 + min(
            TYPEWRITER_MAX_S, len(text) * TYPEWRITER_CHAR_S
        )
        # **碎碎念强制显示气泡**：用户可能关掉了「显示气泡」，但碎碎念本身
        # 就是一句话，没有气泡就完全看不到。这里临时打开气泡并在展示期结束后
        # 还原用户原本的设置（复用对话输入框那套 _bubble_was_enabled 机制）。
        if kind == "whisper":
            self._showBubbleTemporarily()
        self._setBubbleLines((text, "", ""), typewriter=True)
        # **动画由模型选**（碎碎念）：clip 已在 parse_whisper_reply 里校验过
        # 存在于 WHISPER_CLIPS；这里再确认素材真的有，避免静默不播。
        # 对话 / 解析失败时退回 poke（戳一下）。
        if whisper_clip and self.cache.has(whisper_clip):
            self._play_override(whisper_clip)
        else:
            self._play_override("poke" if self.cache.has("poke") else "idle")
        self._notify_if_unfocused("大肥鱼", text)

    @objc.python_method
    def _showBubbleTemporarily(self) -> None:
        """临时打开气泡，展示期结束后由 ``_restoreBubbleAfterWhisper`` 还原。

        **为什么不直接改 bubble_enabled 就完事**：用户配置是「不显示气泡」，
        碎碎念必须显示（否则看不到那句话），但显示完要**自动隐藏回去**——
        不能因为一次碎碎念就把用户的设置永久改掉。
        这里记下原值到 ``_bubble_was_enabled``，与对话输入框复用同一套还原机制。
        """
        if self.bubble_enabled:
            # 本来就开着，什么都不用做，也不需要还原
            self._whisper_forced_bubble = False
            return
        self._bubble_was_enabled = False
        self._whisper_forced_bubble = True
        self.bubble_enabled = True
        if self.bubble is not None:
            self.bubble.setHidden_(False)
        self._resizeWindow()

    @objc.python_method
    def _restoreBubbleAfterWhisper(self) -> None:
        """碎碎念展示结束后把气泡还原成用户原本的设置（隐藏）。"""
        if not getattr(self, "_whisper_forced_bubble", False):
            return
        self._whisper_forced_bubble = False
        self.bubble_enabled = False
        if self.bubble is not None:
            self.bubble.setHidden_(True)
        self._resizeWindow()

    def menuHide_(self, sender) -> None:
        """把窗口藏起来（进程仍在跑，状态继续采集）。"""
        if self.panel:
            self.panel.orderOut_(None)

    def menuQuit_(self, sender) -> None:
        """保存窗口位置后结束桌宠进程。"""
        self._save_layout()
        AppHelper.stopEventLoop()


class PetPanel(NSPanel):
    """桌宠主窗口：必须能成为 key window，否则气泡内输入框收不到键盘。

    **这是「输入后没反应」的根因**：应用以
    ``NSApplicationActivationPolicyAccessory`` 运行（不在 Dock 显示图标），
    普通 ``NSPanel`` 在该策略下 ``canBecomeKeyWindow`` 默认返回 False——
    窗口能显示、能拖动，但拿不到键盘焦点，打字与回车都没有反应。

    由于对话输入框现在直接嵌在气泡里（而不是独立窗口），这个能力必须由
    **主窗口**提供，所以主窗口改用本类。
    """

    controller = objc.ivar()

    def canBecomeKeyWindow(self) -> bool:
        return True

    def canBecomeMainWindow(self) -> bool:
        return True

    def cancelOperation_(self, sender) -> None:
        """Esc：收起气泡内的输入框。

        ``cancelOperation:`` 是 AppKit 给 Esc 的标准入口，比拦截 keyDown
        更省事，也不会干扰输入法。
        """
        controller = getattr(self, "controller", None)
        if controller is not None:
            try:
                controller._closeChatPanel()
            except Exception:
                pass


class PassThroughView(NSView):
    """对鼠标完全透明的容器视图（气泡用）。

    背景：``setIgnoresMouseEvents:`` 只有 ``NSWindow`` 有，``NSView`` /
    ``NSImageView`` 上调用会直接抛 AttributeError（曾导致建窗中断、桌宠不出现）。
    AppKit 的正确做法是重写 ``hitTest:`` 返回 ``None``——该视图不参与命中测试，
    事件自然落到下层的 ``PetView``。
    """

    # 注意：**绝对不能加 @objc.python_method**。加了之后这个方法只存在于
    # Python 侧，ObjC 调用 hitTest: 时仍走 NSView 原生实现并返回视图自己，
    # 于是鼠标事件被气泡层吃掉——表现就是点不动、拖不动、右键失灵。
    def hitTest_(self, point):
        return None



class CenteredTextFieldCell(objc.lookUpClass("NSTextFieldCell")):
    """让输入框文字在框内**垂直居中**的 cell。

    **为什么需要它**：``NSTextField`` 把文字（以及编辑态的 field editor）画在
    ``drawingRectForBounds:`` 返回的区域顶部。实测 26px 高的输入框里，绘制区是
    y=3、h=21，文字只占上部 ~15px，剩下 6px 空白全在下面 → 看起来文字贴顶、
    整个输入行「上重下轻」（用户反馈的「行高要调一下」就是这个）。

    重写该方法、把绘制区收成「文字实际高度、垂直居中」，非编辑态和编辑态
    会一起居中（field editor 的 frame 就是按这个 rect 摆的，实测两者一致）。

    **不能加 @objc.python_method**：加了只存在于 Python 侧，ObjC 绘制时仍走
    父类实现，居中不会生效（和 hitTest_ 是同一类坑）。
    """

    def drawingRectForBounds_(self, rect):
        base = objc.lookUpClass("NSTextFieldCell").drawingRectForBounds_(self, rect)
        font = self.font()
        if font is None:
            return base
        # 文字实际占用高度：与 _labelTextHeight 同一套算法
        text_h = font.ascender() - font.descender() + 1.0
        if base.size.height <= text_h:
            return base
        # 保持宽度与左右内边距不变，只在纵向居中。
        # **必须返回 NSRect（NSMakeRect）**：直接返回 (origin, size) 元组时
        # pyobjc 无法把它转成 struct，ObjC 侧会拿到未定义数据（实测调用抛错）。
        y = base.origin.y + (base.size.height - text_h) / 2.0
        return NSMakeRect(base.origin.x, y, base.size.width, text_h)

class PetImageView(NSImageView):
    """角色图层：直接继承 ``NSImageView`` 并放行鼠标事件。

    必须继承 ``NSImageView`` 而不是 ``NSView``：``setImageScaling:`` /
    ``setAnimates:`` 等是 NSImageView 的 API，继承 NSView 会全部丢失。
    鼠标放行靠重写 ``hitTest:``，不能用 ``setIgnoresMouseEvents:``（NSView 没有）。
    """

    # 同上：必须是真正的 ObjC 方法，不能加 @objc.python_method
    def hitTest_(self, point):
        return None

class PetView(NSView):
    """内容视图：把鼠标事件转给控制器。

    用 flipped 坐标系（原点左上）与素材画布方向一致，
    点击判定时不需要再做 Y 轴翻转。
    """

    controller = objc.ivar()

    @objc.python_method
    def isFlipped(self) -> bool:
        return True

    @objc.python_method
    def acceptsFirstResponder(self) -> bool:
        return True

    def mouseDown_(self, event) -> None:
        if self.controller:
            self.controller.handleMouseDown(event)

    def mouseDragged_(self, event) -> None:
        if self.controller:
            self.controller.handleMouseDragged(event)

    def mouseUp_(self, event) -> None:
        if self.controller:
            self.controller.handleMouseUp(event)

    def rightMouseDown_(self, event) -> None:
        if self.controller:
            self.controller.handleRightClick(event)


class PetAppDelegate(NSObject):
    """应用代理：负责建窗口、起定时器、处理退出。"""

    controller = objc.ivar()
    poll_timer = objc.ivar()
    anim_timer = objc.ivar()

    def applicationDidFinishLaunching_(self, notification) -> None:
        # 必须在主线程创建窗口
        self.controller = PetWindowController.alloc().init()
        self.controller.buildWindow()

        # 首次立即拉一次，避免启动后一两秒是空白
        self.controller.poll()

        # 轮询定时器（状态）
        self.poll_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_INTERVAL, self.controller, b"poll", None, True
        )
        # 动画定时器（帧推进）：16ms 一跳，具体节奏由 tick 内部按 frameMs 控制
        self.anim_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.016, self.controller, b"tick", None, True
        )
        log("桌宠已启动")

    def applicationWillTerminate_(self, notification) -> None:
        if self.controller:
            self.controller._save_layout()


def main() -> int:
    """入口：校验素材与平台后进入 AppKit 事件循环。"""
    if not ASSET_ROOT or not (ASSET_ROOT / "pet-manifest.json").exists():
        log(f"素材目录无效: {ASSET_ROOT}")
        return 2

    app = objc.lookUpClass("NSApplication").sharedApplication()
    # 后台运行：不在 Dock 显示图标，不抢焦点
    app.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory
    delegate = PetAppDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
