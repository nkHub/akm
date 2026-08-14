"""macOS 菜单栏应用 — 状态栏图标 + 服务管理"""

import os
import sys
import time
import json
import shutil
import platform
import logging
import asyncio
import threading
import tempfile
import subprocess
import webbrowser
import socket
from datetime import datetime

import importlib
from typing import Any

import httpx
import rumps
from akm import __version__
from akm.version import version_greater
from akm.config import get as config_get
from akm.key_pool import list_keys
from akm.proxy import test_key_connectivity

# AppKit/Foundation 为 macOS 可选运行时依赖（pyobjc），且通常无完整类型桩。
# 使用 importlib 动态加载，避免静态检查报 unknown import symbol。
NSWorkspace: Any = None
NSWorkspaceDidWakeNotification: Any = None
NSObject: Any = object
NSWindow: Any = None
NSProgressIndicator: Any = None
NSTextField: Any = None
NSButton: Any = None
NSMakeRect: Any = None
NSScrollView: Any = None
NSTextView: Any = None
NSView: Any = None
NSColor: Any = None
NSFont: Any = None
NSAttributedString: Any = None
NSMutableAttributedString: Any = None
NSForegroundColorAttributeName: Any = None
NSFontAttributeName: Any = None
NSImage: Any = None
NSPasteboard: Any = None
NSAlert: Any = None

try:
    _appkit = importlib.import_module("AppKit")
    _foundation = importlib.import_module("Foundation")
    NSWorkspace = getattr(_appkit, "NSWorkspace", None)
    NSWorkspaceDidWakeNotification = getattr(
        _appkit, "NSWorkspaceDidWakeNotification", None
    )
    NSObject = getattr(_foundation, "NSObject", object)
    # 进度窗口组件：用于「立即更新」时展示下载/安装进度。
    NSWindow = getattr(_appkit, "NSWindow", None)
    NSProgressIndicator = getattr(_appkit, "NSProgressIndicator", None)
    NSTextField = getattr(_appkit, "NSTextField", None)
    NSButton = getattr(_appkit, "NSButton", None)
    NSMakeRect = getattr(_foundation, "NSMakeRect", None)
    # 可滚动内容区组件：用于「发现新版本」弹窗展示完整 Release Note
    NSScrollView = getattr(_appkit, "NSScrollView", None)
    NSTextView = getattr(_appkit, "NSTextView", None)
    # 弹窗美化组件：配色 / 字体 / 富文本（accent 色高亮版本号）
    NSView = getattr(_appkit, "NSView", None)
    NSColor = getattr(_appkit, "NSColor", None)
    NSFont = getattr(_appkit, "NSFont", None)
    NSAttributedString = getattr(_appkit, "NSAttributedString", None)
    NSMutableAttributedString = getattr(_appkit, "NSMutableAttributedString", None)
    NSForegroundColorAttributeName = getattr(
        _appkit, "NSForegroundColorAttributeName", None
    )
    NSFontAttributeName = getattr(_appkit, "NSFontAttributeName", None)
    # 「关于」弹窗：logo 图标显示（NSImage）与版本号复制（NSPasteboard）
    NSImage = getattr(_appkit, "NSImage", None)
    NSPasteboard = getattr(_appkit, "NSPasteboard", None)
    # 原生「关于」弹窗（NSAlert）：顶部 logo + 软件名称，底部并排按钮
    NSAlert = getattr(_appkit, "NSAlert", None)
except ImportError:
    pass


# GitHub 仓库标识，格式固定为 "owner/repo"，用于拼接 Releases API 地址。
GITHUB_REPO = "nkHub/akm"
# 更新检查时间间隔（秒）。这里使用 24 小时，避免每次唤醒都请求 API，降低限流风险。
CHECK_INTERVAL = 86400
# 更新包下载超时（秒）：zip 内含完整 Python 运行时，体积较大，给足下载时间。
UPDATE_DOWNLOAD_TIMEOUT = 600
# 静默（自动）更新在应用启动后等待的秒数：避免刚启动就被更新重启打断用户操作。
AUTO_UPDATE_STARTUP_DELAY_SEC = 60.0
# NSAlert.runModal 的按钮返回码：第一个 addButtonWithTitle_ 是主按钮（右侧，响应 Enter），
# 依次对应 1000/1001/1002。rumps.alert 透传该值，不取模转换。
NSAlertFirstButtonReturn = 1000

logger = logging.getLogger("akm.menubar")
DEFAULT_WAKE_RECOVER_DELAY_SEC = 8.0


def _bundle_app_path() -> str:
    """返回当前运行中的 .app bundle 路径。

    仅打包后的 frozen 环境有效（sys.executable 指向
    `<app>/Contents/MacOS/<可执行名>`，向上两级即可回到 .app 目录）。
    开发环境下返回空串，表示不执行自动更新替换。
    """
    if not hasattr(sys, "frozen"):
        return ""
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(os.path.join(exe_dir, "..", ".."))


class _UpdateCancelled(Exception):
    """用户点击「取消下载」后中断更新流程的内部控制信号。"""


def _download_file(url: str, dest: str, progress_cb=None, cancel_check=None) -> bool:
    """流式下载远程文件到 dest，返回是否成功；失败只记日志不抛异常。

    progress_cb(done_bytes, total_bytes) 在每块数据写盘后回调，
    total_bytes 为 None 表示响应头未提供长度。
    cancel_check 为可选的可调用对象，每块数据后调用；返回 True 时中断下载
    并抛 _UpdateCancelled，由调用方按「用户取消」处理。
    """
    try:
        with httpx.stream(
            "GET", url, follow_redirects=True, timeout=UPDATE_DOWNLOAD_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0) or None
            done = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes():
                    if cancel_check and cancel_check():
                        raise _UpdateCancelled()
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
        return True
    except _UpdateCancelled:
        raise
    except Exception as exc:
        logger.warning("下载更新包失败: %s", exc)
        return False


def _extract_app(zip_path: str, dest_dir: str) -> str:
    """用系统 ditto 解压 zip 到 dest_dir，返回解压出的 .app 目录路径。

    使用 ditto 而非 zipfile 是为了完整保留可执行文件的权限与符号链接
    （py2app 产物需要这些才能正常启动）。解压结果里没有 .app 时返回空串。
    """
    subprocess.run(["/usr/bin/ditto", "-x", "-k", zip_path, dest_dir], check=True)
    for name in os.listdir(dest_dir):
        if name.endswith(".app"):
            return os.path.join(dest_dir, name)
    return ""


def _schedule_relaunch(app_path: str) -> None:
    """生成一个延时后 open 新应用的独立脚本并 detached 执行，随后调用方退出当前应用。

    不能直接 `open`：新进程与旧进程 bundle id 相同，若旧进程尚未完全退出，
    `open` 只会激活旧实例。这里先睡几秒等旧进程退出，再拉起新应用。
    """
    script = os.path.join(tempfile.gettempdir(), f"akm-relaunch-{int(time.time() * 1000)}.sh")
    try:
        with open(script, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\n")
            f.write("sleep 4\n")
            f.write(f'open "{app_path}"\n')
            f.write(f'rm -f "{script}"\n')
        os.chmod(script, 0o755)
        subprocess.Popen(["/bin/bash", script], start_new_session=True)
    except Exception as exc:
        logger.warning("生成重启脚本失败: %s", exc)



def _wake_recovery_log_path() -> str:
    """返回唤醒恢复日志路径，并确保目录存在。"""
    log_dir = os.path.expanduser("~/.akm")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "wake_recovery.log")


def _trigger_log_cleanup():
    """在后台线程中执行一次审计日志自动清理，不阻塞恢复流程。"""
    try:
        from akm.audit import auto_cleanup_logs
        auto_cleanup_logs()
    except Exception:
        pass


class _WakeObserver(NSObject):
    """监听 macOS 唤醒通知，并把回调转发给 AKMApp。"""

    def initWithApp_(self, app):
        self = self.init()
        if self is None:
            return None
        self.app = app
        return self

    def handleWake_(self, _notification):
        self.app._schedule_wake_recovery()


class _UpdateCancelTarget(NSObject):
    """进度窗口「取消下载」按钮的 action target，把点击事件转成 Python 回调。"""

    def initWithCallback_(self, callback):
        self = self.init()
        if self is None:
            return None
        self.callback = callback
        return self

    def handleClick_(self, _sender):
        if self.callback:
            self.callback()


def _round_corners(input_path: str) -> str:
    """将图片转为圆角图标（macOS 菜单栏适配），返回处理后文件路径"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return input_path

    try:
        img = Image.open(input_path).convert("RGBA")
        # 缩放到菜单栏图标尺寸 (22x22 像素，2x 分辨率)
        size = 44
        img = img.resize((size, size), Image.Resampling.LANCZOS)

        # 创建圆角遮罩
        mask = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(mask)
        radius = 10  # 圆角半径
        draw.rounded_rectangle([(0, 0), (size - 1, size - 1)], radius=radius, fill=255)

        # 应用遮罩
        img.putalpha(mask)

        output = os.path.expanduser("~/.akm/logo_rounded.png")
        os.makedirs(os.path.dirname(output), exist_ok=True)
        img.save(output, "PNG")
        return output
    except Exception:
        return input_path


class AKMApp(rumps.App):
    """AI Key Manager 菜单栏应用"""

    def __init__(self):
        icon_path = self._get_icon()
        super().__init__(
            name="AKM",
            title=None,
            icon=icon_path,
            quit_button=None,  # type: ignore[arg-type]
        )
        self.server_thread: threading.Thread | None = None
        self.server_ready = False
        self.server_running = False
        self.startup_error: str | None = None
        try:
            self.port = int(config_get("server_port", 8800) or 8800)
        except (TypeError, ValueError):
            self.port = 8800
        self.host = "127.0.0.1"
        self._uvicorn_server = None  # uvicorn.Server 实例，用于优雅关闭
        self._update_url = ""
        self._first_start = True     # 首次启动标记，仅首次自动打开浏览器
        self._wake_recovering = False
        self._last_wake_recover_at = 0.0
        self._wake_recover_delay_sec = self._read_wake_recover_delay_seconds()
        self._wake_recover_min_interval_sec = 20.0
        self._wake_observer = None
        self._wake_notification_center = None
        # 更新菜单项对象。默认没有更新提示，只有检测到新版本后才动态插入菜单。
        self.update_item: rumps.MenuItem | None = None
        # 最近一次检测到的更新信息（含 zip 下载地址），供菜单点击/确认框使用
        self._last_update_info: dict = {}
        # 更新流程状态（后台线程写入，主线程 tick 读取）
        self._updating = False          # 是否正在执行更新，防止重复触发
        self._updating_msg = ""         # 当前更新进度文案，非空时状态栏展示
        self._relaunch_pending = False  # 更新安装完成，待主线程执行退出+重启
        self._last_auto_update_at = 0.0 # 上次静默更新触发时间，防止频繁触发
        self._started_at = time.time()  # 应用启动时间，用于静默更新的启动缓冲
        # 待主线程弹出的对话框请求（后台线程只写入，避免跨线程调用 AppKit）：
        # (title, message, ok, cancel, other, handler)，handler 接收点击按钮文本。
        self._pending_alert: tuple | None = None
        # 待主线程打开的「发现新版本」自定义弹窗请求：后台检查线程写入待打开信息。
        self._pending_update_dialog: dict | None = None
        # 「发现新版本」自定义弹窗状态（主线程创建/刷新/关闭，后台线程只写进度数值）。
        # 该弹窗是更新确认与进度展示的容器：确认 → 进度条 → 可取消更新。
        self._update_dialog = None              # NSWindow 实例，None 表示未打开
        self._update_dialog_note_view = None    # NSTextView 实例（可滚动 Release Note）
        self._update_dialog_progress_bar = None # NSProgressIndicator 实例（初始隐藏）
        self._update_dialog_cancel_btn = None   # 底部左侧「取消」NSButton
        self._update_dialog_ok_btn = None       # 底部右侧「立即更新」/「取消更新」NSButton
        self._update_dialog_status_label = None # 进度/状态文字 NSTextField（进度条上方）
        self._update_dialog_state = "confirm"   # confirm / downloading / cancelled
        self._update_dialog_info: dict = {}     # 弹窗对应的更新信息（供下载线程使用）
        self._update_progress_value = 0.0 # 后台线程写入的下载进度（0-100）
        self._update_progress_done = False  # 下载完成标志，安装阶段转不确定进度
        self._update_failed_msg = ""      # 更新失败原因，主线程检测后关窗并弹框
        self._update_cancel_requested = False  # 用户点击「取消更新」后置 True，下载循环检测后中断
        # 弹窗右侧按钮（立即更新 / 取消更新 状态切换）与两个按钮的 action target：
        # target 复用 _UpdateCancelTarget（通用回调封装），持有引用防止被 GC 回收。
        self._update_dialog_ok_target = None
        self._update_cancelled_msg = ""   # 更新被用户取消的提示文案，主线程检测后恢复弹窗
        self._progress_timer = None       # 快速刷新进度窗口的定时器

        # 原生功能：开机自启动 & 菜单栏用量展示
        self._launch_login_enabled: bool | None = None  # 上次同步状态，避免重复调用 SMAppService
        self._native_timer: rumps.Timer | None = None
        self._last_usage_title: str | None = None

        # 动态菜单项
        self.status_item = rumps.MenuItem(title="🟡 启动中...")
        self.menu = [
            self.status_item,
            rumps.MenuItem(title="应用管理", callback=self.open_admin),
            None,  # 分隔线
            rumps.MenuItem(title="检查更新", callback=self.check_update_now),
            rumps.MenuItem(title="关于 AKM", callback=self.show_about_dialog),
            rumps.MenuItem(title="重启服务", callback=self.restart_server),
            rumps.MenuItem(title="退出", callback=self.quit_app),
        ]

        # 后台启动服务并监控状态
        self._start_server()
        # 后台启动更新检查线程。该线程与服务启动解耦，即使服务未成功启动也可提示新版本。
        self._start_update_checker()
        self._install_wake_observer()
        self._start_native_timer()

    def _fetch_update_info(self) -> dict:
        """从 GitHub Releases API 拉取最新版本信息并与本地版本比对。"""
        try:
            resp = httpx.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                timeout=10,
            )
            if resp.status_code != 200:
                # 非 200（常见为匿名限流 403 / 网络 5xx）：不能当作「已是最新」，
                # 否则会误导用户。返回检查失败标记，由调用方决定提示方式。
                return {"has_update": False, "check_error": f"GitHub 返回 HTTP {resp.status_code}"}

            payload = resp.json()
            latest = payload.get("tag_name", "").lstrip("v")
            # 只有线上版本大于本地版本才提示更新；本地与线上相等或本地更新均不提示
            if latest and version_greater(latest, __version__):
                return {
                    "has_update": True,
                    "latest": latest,
                    "current": __version__,
                    "url": payload.get("html_url", ""),
                    "body": payload.get("body") or "",
                    "download_url": self._pick_zip_download_url(payload.get("assets") or []),
                }
        except Exception:
            # 更新检查属于非关键路径：网络异常、API 失败都不影响主功能，静默降级即可。
            # 但需要向用户提示「检查失败」，避免误报为「已是最新」。
            return {"has_update": False, "check_error": "网络异常，无法连接 GitHub"}
        return {"has_update": False}

    @staticmethod
    def _pick_zip_download_url(assets: list) -> str:
        """从 Release 资产中挑选 zip 更新包下载地址。

        匹配规则：优先选择与当前机器架构（arm64/x86_64）匹配且以 .zip 结尾的资产，
        其次退回任意 .zip 资产；没有任何 zip 时返回空串（表示该版本未提供自动更新包）。
        """
        arch = platform.machine().lower()
        zip_names = [
            a.get("name", "")
            for a in assets
            if str(a.get("name", "")).lower().endswith(".zip")
        ]
        if not zip_names:
            return ""
        for name in zip_names:
            if arch in name.lower():
                for a in assets:
                    if a.get("name") == name:
                        return str(a.get("browser_download_url", "") or "")
        # 无架构匹配时退回任意 zip，保证通用 Release 也能自动更新
        for a in assets:
            if str(a.get("name", "")).lower().endswith(".zip"):
                return str(a.get("browser_download_url", "") or "")
        return ""

    def _safe_notify(self, title: str, message: str) -> None:
        """发送系统通知，失败只记日志不打断更新流程。"""
        try:
            rumps.notification(str(title), "", str(message))
        except Exception as exc:
            logger.warning("更新通知发送失败: %s", exc)

    def _handle_update_info(self, info: dict) -> None:
        """根据检查结果与自动更新开关决定后续动作：静默更新 / 菜单提示 / 忽略。"""
        if not info.get("has_update"):
            self._apply_update_menu(info)
            return
        # 开发环境（未打包成 .app）无法替换自身，只保留菜单提示，不静默更新。
        if not hasattr(sys, "frozen"):
            self._apply_update_menu(info)
            return
        auto_update = config_get("auto_update", True) is not False
        if auto_update:
            self._start_auto_update(info)
        else:
            self._apply_update_menu(info)

    def _start_auto_update(self, info: dict) -> None:
        """按自动更新开关触发静默更新：启动初期延迟执行，并做去重保护。"""
        if self._updating:
            return
        if time.time() - self._last_auto_update_at < 60:
            # 避免启动首查与 24h 轮询重叠导致重复下载
            return
        self._last_auto_update_at = time.time()
        delay = max(0.0, AUTO_UPDATE_STARTUP_DELAY_SEC - (time.time() - self._started_at))
        logger.info("检测到新版本 v%s，%.0f 秒后自动更新", info.get("latest", ""), delay)
        threading.Timer(delay, self._start_auto_update_exec, args=(info,)).start()

    def _start_auto_update_exec(self, info: dict) -> None:
        """延迟到点后真正开始静默更新（后台线程执行下载/安装）。"""
        if self._updating:
            return
        threading.Thread(target=self._perform_update, args=(info, True), daemon=True).start()

    def _perform_update(self, info: dict, silent: bool) -> None:
        """后台线程执行完整更新流程：下载 zip → 解压 → 校验 → 替换 .app → 重启。

        silent 为 True 表示静默模式（自动更新开关开启），失败只发系统通知；
        否则失败时额外弹对话框告知用户。
        """
        if self._updating:
            if not silent and self._update_dialog is not None:
                # 已有更新任务在执行，让主线程在弹窗内提示
                self._update_failed_msg = "已有更新任务正在执行，请稍候"
            return
        self._updating = True
        self._updating_msg = ""
        zip_path = ""
        try:
            download_url = info.get("download_url") or ""
            latest = info.get("latest", "")
            if not latest or not download_url:
                raise RuntimeError("当前 Release 未提供 zip 更新包，请到 Release 页面手动下载")
            if not hasattr(sys, "frozen"):
                raise RuntimeError("开发环境不执行自动更新")
            target = _bundle_app_path()
            if not target or not os.path.isdir(target):
                raise RuntimeError("无法定位当前应用安装位置")

            # 1. 下载 zip 到 ~/.akm/updates
            self._updating_msg = f"正在下载 v{latest}..."
            self._safe_notify("AKM 更新", f"正在下载 v{latest} 更新包")
            cache_dir = os.path.join(os.path.expanduser("~/.akm"), "updates")
            os.makedirs(cache_dir, exist_ok=True)
            zip_path = os.path.join(cache_dir, f"AI Key Manager-{latest}-{int(time.time())}.zip")

            def _on_download_progress(done: int, total: int | None):
                # 状态栏与进度窗口共用百分比；无长度信息时退化为已下载字节数
                if total:
                    pct = min(100, round(done * 100 / total))
                    self._update_progress_value = float(pct)
                    self._updating_msg = f"正在下载 v{latest}... {pct}%"
                else:
                    mb = round(done / 1024 / 1024, 1)
                    self._updating_msg = f"正在下载 v{latest}... {mb}MB"

            if not _download_file(
                download_url,
                zip_path,
                progress_cb=_on_download_progress,
                cancel_check=lambda: self._update_cancel_requested,
            ):
                raise RuntimeError("更新包下载失败，请检查网络后重试")

            # 2. 解压 zip，找到其中的 .app（下载完成：进度条转不确定动画）
            self._update_progress_value = 100.0
            self._update_progress_done = True
            self._updating_msg = "正在安装..."
            tmp_dir = tempfile.mkdtemp(prefix="akm-update-")
            new_app = _extract_app(zip_path, tmp_dir)
            if not new_app or not os.path.isdir(new_app):
                raise RuntimeError("更新包内容无效，未找到 .app")

            # 3. 备份当前 .app 后替换为新版本，替换失败时回滚旧版本
            backup_dir = os.path.join(cache_dir, "backups")
            os.makedirs(backup_dir, exist_ok=True)
            backup = os.path.join(backup_dir, f"AI Key Manager-{__version__}.app")
            shutil.rmtree(backup, ignore_errors=True)
            os.rename(target, backup)
            try:
                shutil.move(new_app, target)
            except Exception:
                shutil.rmtree(target, ignore_errors=True)
                os.rename(backup, target)
                raise

            # 4. 交给主线程退出并重启（重启脚本在旧进程退出后才 open 新应用）
            self._updating_msg = ""
            self._relaunch_pending = True
            self._safe_notify("AKM 更新完成", f"已更新到 v{latest}，即将自动重启")
        except _UpdateCancelled:
            # 用户点击「取消下载」：中断更新，清理临时 zip，通知后结束（不视为失败）
            logger.warning("用户取消更新下载")
            self._updating_msg = ""
            try:
                if zip_path and os.path.exists(zip_path):
                    os.remove(zip_path)
            except Exception:
                pass
            self._safe_notify("AKM 更新", "已取消更新下载")
            # 通知主线程关闭进度窗口（复用失败通道的关闭逻辑但走取消分支）
            self._update_cancelled_msg = "已取消更新下载"
        except Exception as exc:
            logger.warning("自动更新失败: %s", exc)
            self._updating_msg = ""
            self._safe_notify("AKM 更新失败", str(exc))
            if not silent:
                if self._update_dialog is not None:
                    # 弹窗已打开：交由主线程在弹窗内提示失败原因
                    self._update_failed_msg = str(exc)
                else:
                    self._queue_alert("更新失败", str(exc), "确定", None, None)
        finally:
            self._updating = False

    def _do_relaunch(self) -> None:
        """更新安装完成后重启应用：先生成延时 open 脚本再退出当前实例。"""
        target = _bundle_app_path()
        logger.warning("更新安装完成，准备重启应用: %s", target)
        if target and os.path.isdir(target):
            _schedule_relaunch(target)
        rumps.quit_application()

    # ── 「发现新版本」自定义弹窗（确认 + 进度 + 取消更新） ──

    def _open_update_dialog(self, info: dict) -> None:
        """在主线程打开「发现新版本」自定义弹窗。

        弹窗是更新确认与进度展示的容器：
        初始显示 Release Note 与底部「取消 / 立即更新」按钮；
        点击「立即更新」后在内容区底部显示进度条，右侧按钮变为「取消更新」；
        下载可被中断，中断后弹窗保留并恢复「立即更新」。
        组件缺失时静默降级为状态栏进度。

        外观为现代 macOS 卡片式弹窗：透明标题栏 + 内容延伸到标题栏、
        大号粗体标题配 accent 色版本号、圆角浅底 Release Note 卡片、
        分隔线与底部主按钮，全部使用系统语义色以适配深色/浅色模式。
        """
        if self._update_dialog is not None:
            return
        if not NSWindow or not NSProgressIndicator or not NSTextField or NSMakeRect is None:
            logger.warning("AppKit 组件不可用，更新弹窗降级为状态栏展示")
            return
        try:
            w = 560
            h = 480
            # 透明标题栏 + 内容延伸到标题栏（FullSizeContentView = 1<<15），
            # 保留系统圆角、阴影与红绿灯关闭按钮，背景由系统自动适配深浅色。
            window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, w, h),
                1 | 2 | (1 << 15),  # Titled | Closable | FullSizeContentView
                2,                  # NSBackingStoreBuffered
                False,
            )
            window.setTitle_("发现新版本")
            window.setTitlebarAppearsTransparent_(True)
            window.setTitleVisibility_(1)  # NSWindowTitleHidden：隐藏标题文字

            content = window.contentView()

            # 顶部留白：为透明标题栏（约 28pt）让位
            top_pad = 44
            latest = info.get("latest", "")

            # ---- 标题行：粗体「发现新版本」+ accent 色版本号（富文本） ----
            title_label = NSTextField.alloc().initWithFrame_(NSMakeRect(24, h - top_pad - 34, w - 48, 34))
            title_label.setBezeled_(False)
            title_label.setDrawsBackground_(False)
            title_label.setEditable_(False)
            title_label.setSelectable_(False)
            if (
                NSFont is not None
                and NSColor is not None
                and NSAttributedString is not None
                and NSMutableAttributedString is not None
                and NSForegroundColorAttributeName is not None
                and NSFontAttributeName is not None
            ):
                attrs = NSMutableAttributedString.alloc().initWithString_("发现新版本  ")
                attrs.addAttribute_value_range_(
                    NSFontAttributeName, NSFont.boldSystemFontOfSize_(20), (0, len("发现新版本  "))
                )
                attrs.addAttribute_value_range_(
                    NSForegroundColorAttributeName, NSColor.labelColor(), (0, len("发现新版本  "))
                )
                ver = NSAttributedString.alloc().initWithString_attributes_(
                    f"v{latest}",
                    {
                        NSFontAttributeName: NSFont.boldSystemFontOfSize_(20),
                        NSForegroundColorAttributeName: NSColor.controlAccentColor(),
                    },
                )
                attrs.appendAttributedString_(ver)
                title_label.setAttributedStringValue_(attrs)
            else:
                title_label.setStringValue_(f"发现新版本 v{latest}")
                if NSFont is not None:
                    title_label.setFont_(NSFont.boldSystemFontOfSize_(20))

            # ---- 副标题：当前版本（次要灰字） ----
            subtitle_label = NSTextField.alloc().initWithFrame_(NSMakeRect(24, h - top_pad - 60, w - 48, 18))
            subtitle_label.setStringValue_(f"当前版本 v{__version__}")
            subtitle_label.setBezeled_(False)
            subtitle_label.setDrawsBackground_(False)
            subtitle_label.setEditable_(False)
            subtitle_label.setSelectable_(False)
            if NSFont is not None:
                subtitle_label.setFont_(NSFont.systemFontOfSize_(12))
            if NSColor is not None:
                subtitle_label.setTextColor_(NSColor.secondaryLabelColor())

            # ---- 分隔线 ----
            if NSView is not None and NSColor is not None:
                sep = NSView.alloc().initWithFrame_(NSMakeRect(24, h - top_pad - 74, w - 48, 1))
                sep.setWantsLayer_(True)
                sep.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
                content.addSubview_(sep)

            # ---- 「更新内容」小节标题 ----
            section_label = NSTextField.alloc().initWithFrame_(NSMakeRect(24, h - top_pad - 94, w - 48, 16))
            section_label.setStringValue_("更新内容")
            section_label.setBezeled_(False)
            section_label.setDrawsBackground_(False)
            section_label.setEditable_(False)
            section_label.setSelectable_(False)
            if NSFont is not None:
                section_label.setFont_(NSFont.systemFontOfSize_(11))
            if NSColor is not None:
                section_label.setTextColor_(NSColor.secondaryLabelColor())
            content.addSubview_(section_label)

            # ---- 可滚动的 Release Note：圆角浅底卡片 ----
            note_view = None
            note_top = h - top_pad - 118  # 内容区顶部 y 坐标（小节标题下方留 24）
            if NSScrollView is not None and NSTextView is not None:
                note_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, w - 48, 200))
                note_view.setString_(str(info.get("body") or "（该版本未填写更新说明）"))
                note_view.setEditable_(False)
                note_view.setSelectable_(True)
                note_view.setDrawsBackground_(False)  # 让卡片背景透出
                if NSFont is not None:
                    note_view.setFont_(NSFont.systemFontOfSize_(13))
                if NSColor is not None:
                    note_view.setTextColor_(NSColor.labelColor())
                    # 文本与卡片内边距：pyobjc 下 NSSize 从 Foundation 获取
                    _ns_size = getattr(_foundation, "NSSize", None)
                    if _ns_size is not None:
                        note_view.setTextContainerInset_(_ns_size(12, 10))
                scroll = NSScrollView.alloc().initWithFrame_(
                    NSMakeRect(24, 100, w - 48, note_top - 100)
                )
                scroll.setDocumentView_(note_view)
                scroll.setHasVerticalScroller_(True)
                scroll.setAutohidesScrollers_(True)
                scroll.setBorderType_(0)  # NSNoBorder
                if NSColor is not None:
                    scroll.setDrawsBackground_(True)
                    scroll.setBackgroundColor_(NSColor.textBackgroundColor())
                if NSView is not None:
                    # 卡片圆角
                    scroll.setWantsLayer_(True)
                    scroll.layer().setCornerRadius_(8.0)
                content.addSubview_(scroll)

            # ---- 状态文字标签（进度条上方）：初始提示，下载/安装时展示进度文案 ----
            status_label = NSTextField.alloc().initWithFrame_(NSMakeRect(24, 70, w - 48, 18))
            status_label.setStringValue_("是否立即下载并自动安装？安装完成后应用将自动重启。")
            status_label.setBezeled_(False)
            status_label.setDrawsBackground_(False)
            status_label.setEditable_(False)
            status_label.setSelectable_(False)
            if NSFont is not None:
                status_label.setFont_(NSFont.systemFontOfSize_(12))
            if NSColor is not None:
                status_label.setTextColor_(NSColor.secondaryLabelColor())

            # 确定进度条（0-100），下载完成后切为不确定动画；初始隐藏
            bar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(24, 40, w - 48, 18))
            bar.setStyle_(getattr(_appkit, "NSProgressIndicatorBarStyle", 0))
            bar.setIndeterminate_(False)
            bar.setMinValue_(0.0)
            bar.setMaxValue_(100.0)
            bar.setDoubleValue_(0.0)
            bar.setHidden_(True)

            # ---- 底部按钮排：左侧「取消」、右侧「立即更新」/「取消更新」 ----
            cancel_btn = None
            ok_btn = None
            if NSButton is not None:
                cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(24, 12, 110, 28))
                cancel_btn.setTitle_("取消")
                cancel_btn.setBezelStyle_(1)  # NSRoundedBezelStyle
                self._update_dialog_cancel_target = _UpdateCancelTarget.alloc().initWithCallback_(
                    self._on_dialog_cancel_clicked
                )
                cancel_btn.setTarget_(self._update_dialog_cancel_target)
                cancel_btn.setAction_(b"handleClick:")
                content.addSubview_(cancel_btn)

                ok_btn = NSButton.alloc().initWithFrame_(NSMakeRect(w - 134, 12, 110, 28))
                ok_btn.setTitle_("立即更新")
                ok_btn.setBezelStyle_(1)  # NSRoundedBezelStyle
                # 设为主按钮（Enter 键触发），系统渲染为 accent 色默认按钮样式
                ok_btn.setKeyEquivalent_("\r")
                self._update_dialog_ok_target = _UpdateCancelTarget.alloc().initWithCallback_(
                    self._on_dialog_ok_clicked
                )
                ok_btn.setTarget_(self._update_dialog_ok_target)
                ok_btn.setAction_(b"handleClick:")
                content.addSubview_(ok_btn)

            content.addSubview_(title_label)
            content.addSubview_(subtitle_label)
            content.addSubview_(status_label)
            content.addSubview_(bar)

            self._update_dialog = window
            self._update_dialog_note_view = note_view
            self._update_dialog_status_label = status_label
            self._update_dialog_progress_bar = bar
            self._update_dialog_cancel_btn = cancel_btn
            self._update_dialog_ok_btn = ok_btn
            self._update_dialog_state = "confirm"
            self._update_dialog_info = info
            self._update_progress_value = 0.0
            self._update_progress_done = False
            self._update_cancel_requested = False
            # 窗口居中，并提到前台（菜单栏应用无 dock 图标也能显示）
            window.center()
            window.orderFrontRegardless()
            window.makeKeyAndOrderFront_(None)
        except Exception as exc:
            logger.warning("打开更新弹窗失败: %s", exc)
            self._update_dialog = None
            self._update_dialog_note_view = None
            self._update_dialog_status_label = None
            self._update_dialog_progress_bar = None
            self._update_dialog_cancel_btn = None
            self._update_dialog_ok_btn = None
            self._update_dialog_state = "confirm"
            self._update_dialog_info = {}

    def _on_dialog_cancel_clicked(self):
        """弹窗左侧「取消」按钮回调：未开始下载时点击，关闭弹窗放弃更新。"""
        if self._update_dialog_state != "confirm":
            return
        self._close_update_dialog()

    def _on_dialog_ok_clicked(self):
        """弹窗右侧按钮回调：confirm 状态开始更新；downloading 状态请求取消。"""
        if self._update_dialog is None:
            return
        if self._update_dialog_state == "confirm":
            # 确认更新：显示进度条、右侧按钮变「取消更新」、禁用左侧「取消」防误关
            self._update_dialog_state = "downloading"
            self._update_progress_value = 0.0
            self._update_progress_done = False
            self._update_cancel_requested = False
            if self._update_dialog_progress_bar is not None:
                self._update_dialog_progress_bar.setHidden_(False)
                self._update_dialog_progress_bar.setIndeterminate_(False)
                self._update_dialog_progress_bar.setDoubleValue_(0.0)
            if self._update_dialog_status_label is not None:
                self._update_dialog_status_label.setStringValue_("正在准备更新...")
            if self._update_dialog_ok_btn is not None:
                self._update_dialog_ok_btn.setTitle_("取消更新")
            if self._update_dialog_cancel_btn is not None:
                self._update_dialog_cancel_btn.setEnabled_(False)
            info = self._update_dialog_info or {}
            if info.get("has_update"):
                threading.Thread(
                    target=self._perform_update, args=(info, False), daemon=True
                ).start()
        elif self._update_dialog_state == "downloading":
            # 点击「取消更新」：请求中断下载并禁用按钮防重复点击
            self._update_cancel_requested = True
            if self._update_dialog_ok_btn is not None:
                self._update_dialog_ok_btn.setTitle_("正在取消...")
                self._update_dialog_ok_btn.setEnabled_(False)

    def _refresh_update_dialog(self) -> None:
        """刷新弹窗内容（主线程定时调用，读取后台线程写入的进度状态）。"""
        if self._update_dialog is None:
            return
        try:
            if self._update_dialog_progress_bar is not None:
                if self._update_progress_done:
                    # 下载完成进入安装阶段：切换为不确定进度动画
                    self._update_dialog_progress_bar.setIndeterminate_(True)
                    self._update_dialog_progress_bar.startAnimation_(None)
                else:
                    self._update_dialog_progress_bar.setIndeterminate_(False)
                    self._update_dialog_progress_bar.setDoubleValue_(self._update_progress_value)
            if self._update_dialog_status_label is not None:
                msg = self._updating_msg or "正在准备更新..."
                self._update_dialog_status_label.setStringValue_(msg)
        except Exception as exc:
            logger.warning("刷新更新弹窗失败: %s", exc)

    def _reset_dialog_to_confirm(self, hint: str) -> None:
        """取消更新后恢复弹窗为初始状态（保留弹窗），并在状态标签展示提示。"""
        if self._update_dialog is None:
            return
        try:
            self._update_dialog_state = "confirm"
            if self._update_dialog_progress_bar is not None:
                self._update_dialog_progress_bar.stopAnimation_(None)
                self._update_dialog_progress_bar.setHidden_(True)
            if self._update_dialog_status_label is not None:
                self._update_dialog_status_label.setStringValue_(hint)
            if self._update_dialog_ok_btn is not None:
                self._update_dialog_ok_btn.setTitle_("立即更新")
                self._update_dialog_ok_btn.setEnabled_(True)
            if self._update_dialog_cancel_btn is not None:
                self._update_dialog_cancel_btn.setEnabled_(True)
        except Exception as exc:
            logger.warning("恢复更新弹窗状态失败: %s", exc)

    def _close_update_dialog(self) -> None:
        """关闭弹窗并清理状态（主线程调用）。"""
        if self._update_dialog is None:
            return
        try:
            if self._update_dialog_progress_bar is not None:
                self._update_dialog_progress_bar.stopAnimation_(None)
            self._update_dialog.close()
        except Exception as exc:
            logger.warning("关闭更新弹窗失败: %s", exc)
        finally:
            self._update_dialog = None
            self._update_dialog_note_view = None
            self._update_dialog_status_label = None
            self._update_dialog_progress_bar = None
            self._update_dialog_cancel_btn = None
            self._update_dialog_ok_btn = None
            self._update_dialog_cancel_target = None
            self._update_dialog_ok_target = None
            self._update_dialog_state = "confirm"
            self._update_dialog_info = {}
            self._update_progress_value = 0.0
            self._update_progress_done = False
            self._update_failed_msg = ""
            self._update_cancel_requested = False
            self._update_cancelled_msg = ""

    def _on_update_progress_tick(self, _):
        """进度定时器回调：弹窗存在时刷新进度，失败关窗弹框，取消则恢复弹窗。"""
        try:
            if self._update_failed_msg:
                msg = self._update_failed_msg
                self._update_failed_msg = ""
                self._close_update_dialog()
                self._queue_alert("更新失败", msg, "确定", None, None)
                return
            if self._update_cancelled_msg:
                # 用户取消了下载：保留弹窗并恢复初始状态，无需弹失败框
                msg = self._update_cancelled_msg
                self._update_cancelled_msg = ""
                self._reset_dialog_to_confirm(msg)
                self._safe_notify("AKM 更新", msg)
                return
            if self._update_dialog is not None:
                self._refresh_update_dialog()
        except Exception as exc:
            logger.warning("更新弹窗定时回调失败: %s", exc)

    def _open_release_page(self, _):
        """点击更新菜单后打开 Release 页面。"""
        if self._update_url:
            webbrowser.open(self._update_url)

    def _on_update_menu_click(self, _):
        """点击“更新到 vX.Y.Z”菜单项：打开「发现新版本」自定义弹窗确认更新。"""
        info = self._last_update_info or {}
        if not info.get("has_update"):
            return
        self._open_update_dialog(info)

    def _find_app_logo_path(self) -> str:
        """定位应用 logo 图片路径：打包环境在 .app/Contents/Resources，开发环境在项目根目录。"""
        bundle = _bundle_app_path()
        if bundle:
            cand = os.path.join(bundle, "Contents", "Resources", "logo.png")
            if os.path.isfile(cand):
                return cand
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cand = os.path.join(root, "logo.png")
        return cand if os.path.isfile(cand) else ""

    def _copy_version_to_clipboard(self) -> None:
        """把当前版本号写入系统剪贴板，并弹出系统通知确认。"""
        text = f"v{__version__}"
        try:
            if NSPasteboard is None:
                logger.warning("NSPasteboard 不可用，无法复制版本号")
                return
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(
                text, getattr(_appkit, "NSPasteboardTypeString", "public.utf8-plain-text")
            )
            self._safe_notify("AI Key Manager", f"已复制版本号 {text}")
        except Exception as exc:
            logger.warning("复制版本号失败: %s", exc)

    def show_about_dialog(self, _):
        """点击「关于 AKM」：弹出 macOS 原生关于框（NSAlert）。

        外观沿用最初的系统原生弹窗样式：顶部为应用 logo 图标 + 软件名称标题，
        正文居中显示版本号，底部并排「复制版本号」与「取消」按钮。
        """
        if NSAlert is None or NSImage is None:
            logger.warning("AppKit 组件不可用，无法显示关于弹窗")
            return
        try:
            alert = NSAlert.alloc().init()
            alert.setMessageText_("AI Key Manager")
            alert.setInformativeText_(f"版本 v{__version__}")
            # 菜单栏应用无 dock 图标，需手动把 logo 设为弹窗图标
            logo_path = self._find_app_logo_path()
            if logo_path:
                img = NSImage.alloc().initWithContentsOfFile_(logo_path)
                if img is not None:
                    alert.setIcon_(img)
            # 第一个 add 的按钮为主按钮（右侧、响应 Enter），复制是主要操作
            alert.addButtonWithTitle_("复制")
            alert.addButtonWithTitle_("取消")
            clicked = alert.runModal()
            if clicked == NSAlertFirstButtonReturn:
                self._copy_version_to_clipboard()
        except Exception as exc:
            logger.warning("关于弹窗失败: %s", exc)

    def _queue_alert(self, title: str, message: str, ok=None, cancel=None, other=None) -> None:
        """把弹窗请求写入待处理队列，由主线程 tick 弹出。

        后台线程禁止直接调用 AppKit（rumps.alert），因此先写入队列，
        主线程 `_on_native_tick` 消费后再弹出，保证线程安全。
        """
        self._pending_alert = (title, message, ok, cancel, other)

    def check_update_now(self, _):
        """菜单“检查更新”：立即检查一次并弹窗反馈结果。

        有更新时展示 Release Note 并让用户确认是否立即更新；
        无更新时弹窗提示已是最新版本。
        """
        if self._updating:
            self._safe_notify("检查更新", "更新正在执行中，请稍候")
            return

        def do_check():
            info = self._fetch_update_info()
            if self._updating:
                return
            # 记录本次检查结果，供弹窗确认后的更新流程使用
            self._last_update_info = info
            if info.get("check_error"):
                # 检查失败（如 GitHub 匿名限流 403）：如实提示，不误报「已是最新」
                self._queue_alert(
                    "检查更新失败",
                    f"{info.get('check_error')}\n\n请稍后重试，或在设置中关闭后重新开启自动更新。",
                    "好的",
                    None,
                    None,
                )
                return
            if not info.get("has_update"):
                # 无更新：弹窗提示已是最新，并清理可能残留的过期更新菜单项
                self._apply_update_menu({"has_update": False})
                self._queue_alert(
                    "检查更新",
                    f"当前已是最新版本 v{__version__}",
                    "好的",
                    None,
                    None,
                )
                return
            # 有更新：交给主线程打开「发现新版本」自定义弹窗（展示 Release Note 并确认）
            # 后台线程只写入待打开信息，避免跨线程创建 NSWindow。
            self._pending_update_dialog = info

        threading.Thread(target=do_check, daemon=True).start()

    def _apply_update_menu(self, info: dict):
        """根据检查结果动态维护“更新到 vX.Y.Z”菜单项。"""
        has_update = info.get("has_update", False)
        if not has_update:
            # 已无更新时，移除旧的更新菜单，避免 UI 残留过期提示。
            if self.update_item and self.update_item in self.menu:
                menu: Any = self.menu
                menu.pop(menu.index(self.update_item))
            self.update_item = None
            self._update_url = ""
            return

        latest = info.get("latest", "")
        release_url = info.get("url", "")
        if not latest or not release_url:
            return

        self._last_update_info = info
        title = f"更新到 v{latest}"
        if self.update_item is None:
            self.update_item = rumps.MenuItem(title=title, callback=self._on_update_menu_click)
            self._update_url = release_url
            # 插在“应用管理”后面，保证更新入口显眼但不干扰状态项。
            self.menu.insert_after("应用管理", self.update_item)
            return

        self.update_item.title = title
        self._update_url = release_url

    def _start_update_checker(self):
        """后台循环检查更新：按自动更新开关静默更新或维护菜单提示。"""

        def run_checker():
            # 首次立即检查一次，启动后尽快给用户反馈。
            info = self._fetch_update_info()
            self._handle_update_info(info)

            # 后续按固定间隔轮询，避免频繁请求 API。
            while True:
                time.sleep(CHECK_INTERVAL)
                info = self._fetch_update_info()
                self._handle_update_info(info)

        threading.Thread(target=run_checker, daemon=True).start()

    def _install_wake_observer(self):
        """注册 macOS 唤醒通知；缺少桥接依赖时静默降级，不影响主功能。"""
        if NSWorkspace is None or NSWorkspaceDidWakeNotification is None:
            logger.warning("未检测到 AppKit/Foundation，跳过系统唤醒监听")
            return
        try:
            center = NSWorkspace.sharedWorkspace().notificationCenter()
            observer = _WakeObserver.alloc().initWithApp_(self)
            center.addObserver_selector_name_object_(
                observer,
                "handleWake:",
                NSWorkspaceDidWakeNotification,
                None,
            )
            self._wake_observer = observer
            self._wake_notification_center = center
        except Exception as exc:
            logger.warning("注册系统唤醒监听失败: %s", exc)

    def _start_native_timer(self):
        """启动原生功能定时器：开机自启动同步 + 菜单栏用量刷新 + 更新进度窗口刷新。"""
        self._native_timer = rumps.Timer(self._on_native_tick, 5)
        self._native_timer.start()
        # 进度窗口刷新走独立短间隔定时器，保证下载进度平滑更新（窗口未打开时无开销）。
        self._progress_timer = rumps.Timer(self._on_update_progress_tick, 0.5)
        self._progress_timer.start()

    def _on_native_tick(self, _):
        """每 5 秒回调一次：同步开机自启动状态 + 刷新菜单栏用量 + 处理待执行操作。
        所有操作在 rumps 主线程执行，安全更新 UI。"""
        import akm
        # 更新进行中：优先展示更新进度，暂停常规状态刷新。
        if self._updating_msg:
            self.status_item.title = f"🔄 {self._updating_msg}"
            return
        # 更新安装完成：执行退出+重启（重启脚本会延迟拉起新应用）。
        if self._relaunch_pending:
            self._relaunch_pending = False
            self._close_update_dialog()
            self._do_relaunch()
            return
        # 后台检查线程提交的「发现新版本」弹窗请求：在主线程打开自定义弹窗。
        if self._pending_update_dialog:
            info = self._pending_update_dialog
            self._pending_update_dialog = None
            if self._update_dialog is None:
                self._open_update_dialog(info)
            return
        # 后台线程提交的简单系统弹窗请求（无更新提示 / 更新失败提示等）。
        if self._pending_alert:
            title, message, ok, cancel, other = self._pending_alert
            self._pending_alert = None
            try:
                rumps.alert(title, message, ok, cancel, other)
            except Exception as exc:
                logger.warning("弹窗失败: %s", exc)
            return
        try:
            self._sync_launch_at_login()
        except Exception:
            pass
        try:
            self._refresh_usage_title()
        except Exception:
            pass
        # 处理由 server.py API 触发的待执行操作
        action = akm._pending_action
        akm._pending_action = None
        if action == "restart":
            self._restart_server_internal("api_restart")

    # ── 开机自启动 ──────────────────────────────────────

    def _sync_launch_at_login(self):
        """根据配置同步 SMAppService 登录项状态。
        仅在打包后的 .app 中生效（hasattr(sys, 'frozen')）。"""
        if not hasattr(sys, "frozen"):
            return
        try:
            cfg = self._load_config_safe()
        except Exception:
            return
        enabled = bool(cfg.get("launch_at_login", False))
        if self._launch_login_enabled == enabled:
            return
        try:
            import objc  # type: ignore[import-untyped]
            objc.loadBundle(  # type: ignore[attr-defined]
                "ServiceManagement",
                globals(),
                bundle_path="/System/Library/Frameworks/ServiceManagement.framework",
            )
            SMAppService = objc.lookUpClass("SMAppService")  # type: ignore[attr-defined]
            service = SMAppService.mainAppService()
            if enabled:
                service.register()
                logger.info("已注册为开机自启动")
            else:
                service.unregister()
                logger.info("已取消开机自启动")
            self._launch_login_enabled = enabled
        except Exception as exc:
            logger.warning("SMAppService 操作失败: %s", exc)

    def _load_config_safe(self) -> dict:
        """安全读取配置，失败时返回空字典避免拖垮定时器。"""
        try:
            from akm.config import load_config
            return load_config()
        except Exception:
            return {}

    # ── 菜单栏用量展示 ──────────────────────────────────

    def _refresh_usage_title(self):
        """从本地服务获取今日用量并更新菜单栏标题。
        配置关闭或服务未就绪时恢复默认标题。"""
        cfg = self._load_config_safe()
        if not cfg.get("menu_bar_show_usage", False):
            if self._last_usage_title is not None:
                self.title = None
                self._last_usage_title = None
            return
        if not self.server_ready:
            return
        try:
            resp = httpx.get(
                f"http://{self.host}:{self.port}/api/stats?days=1",
                timeout=3,
            )
            if resp.status_code != 200:
                return
            data = resp.json()
            tokens = int(data.get("total_tokens", 0) or 0)
            cost = float(data.get("total_cost", 0) or 0)
            if tokens >= 1_000_000_000:
                token_str = f"{tokens / 1_000_000_000:.2f}B"
            elif tokens >= 1_000_000:
                token_str = f"{tokens / 1_000_000:.2f}M"
            elif tokens >= 1000:
                token_str = f"{tokens / 1000:.2f}K"
            else:
                token_str = str(tokens)
            # 紧凑单行：费用在前，Token 在后，用 / 分割；未开启费用估算则不显示费用
            cost_stats_enabled = cfg.get("cost_stats_enabled", False)
            if cost_stats_enabled:
                cost_str = f"${cost:.2f}"
                title = f"{cost_str} / {token_str}"
            else:
                title = token_str
            self._set_small_title(title)
            self._last_usage_title = title
        except Exception:
            pass

    def _set_small_title(self, text: str):
        """用较小字号设置菜单栏标题，图标居左。"""
        try:
            from Foundation import NSAttributedString  # type: ignore[import-untyped]
            from AppKit import NSFont, NSFontAttributeName  # type: ignore[import-untyped]
            button = self._nsapp.nsstatusitem.button()
            if button is None:
                self.title = text
                return
            icon = getattr(self, "_icon_nsimage", None)
            if icon is not None:
                button.setImage_(icon)
                button.setImagePosition_(2)  # NSImageLeft
            font = NSFont.menuBarFontOfSize_(NSFont.systemFontSize() - 1)
            attr_title = NSAttributedString.alloc().initWithString_attributes_(
                " " + text, {NSFontAttributeName: font}
            )
            button.setAttributedTitle_(attr_title)
        except Exception:
            self.title = text

    def _read_wake_recover_delay_seconds(self) -> float:
        """读取唤醒恢复延迟配置，并对异常值做兜底，避免配置错误把恢复流程搞坏。"""
        try:
            delay = float(config_get("wake_recover_delay_sec", DEFAULT_WAKE_RECOVER_DELAY_SEC) or DEFAULT_WAKE_RECOVER_DELAY_SEC)
        except (TypeError, ValueError):
            delay = DEFAULT_WAKE_RECOVER_DELAY_SEC
        return max(0.0, delay)

    def _schedule_wake_recovery(self):
        """对唤醒恢复做并发保护和去抖，避免短时间重复触发多次恢复。"""
        now = time.time()
        if self._wake_recovering:
            logger.info("唤醒恢复已在进行中，跳过重复触发")
            return
        if now - self._last_wake_recover_at < self._wake_recover_min_interval_sec:
            logger.info("唤醒恢复触发过于频繁，本次跳过")
            return
        threading.Thread(target=self._recover_after_wake, daemon=True).start()

    def _update_status_for_recovery(self, title: str):
        """统一更新唤醒恢复相关状态文案，避免恢复流程散落多处直接改 UI。"""
        self.status_item.title = title

    def _append_wake_recovery_log(self, event: str, **details):
        """将唤醒恢复关键节点追加写入独立 JSONL 日志，便于事后排查恢复链路。"""
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": str(event or "unknown"),
            "details": details,
        }
        try:
            with open(_wake_recovery_log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("写入唤醒恢复日志失败: %s", exc)

    def _probe_local_service_after_wake(self) -> tuple[bool, str]:
        """唤醒后检查本地服务是否可用。

        第一版只做本地探针：先查端口，再查 `/health/ready`，尽量用最小代价判断
        AKM 是否需要自愈。这样可以先覆盖“服务线程没了”“端口还在但服务未 ready”
        这两类最常见问题，而不把上游探活复杂度提前引进来。
        """
        if not self._check_port():
            self._append_wake_recovery_log("probe.local.failed", reason="port_unreachable")
            return False, "port_unreachable"
        url = f"http://{self.host}:{self.port}/health/ready"
        try:
            resp = httpx.get(url, timeout=3)
        except Exception as exc:
            logger.warning("唤醒后就绪探针请求失败: %s", exc)
            self._append_wake_recovery_log("probe.local.failed", reason="ready_probe_failed", error=str(exc))
            return False, "ready_probe_failed"
        if resp.status_code != 200:
            logger.warning("唤醒后就绪探针返回非 200: %s", resp.status_code)
            self._append_wake_recovery_log("probe.local.failed", reason="service_not_ready", status_code=resp.status_code)
            return False, "service_not_ready"
        try:
            payload = resp.json()
        except ValueError:
            logger.warning("唤醒后就绪探针响应不是合法 JSON")
            self._append_wake_recovery_log("probe.local.failed", reason="ready_probe_invalid_json")
            return False, "ready_probe_invalid_json"
        if payload.get("ready") is not True:
            logger.warning("唤醒后本地服务未 ready: %s", payload)
            self._append_wake_recovery_log("probe.local.failed", reason="service_not_ready", payload=payload)
            return False, "service_not_ready"
        self._append_wake_recovery_log("probe.local.ok")
        return True, "ok"

    def _restart_server_internal(self, reason: str) -> bool:
        """封装服务重启动作，供菜单点击和唤醒自愈统一复用。"""
        logger.warning("准备重启本地服务，原因: %s", reason)
        self._append_wake_recovery_log("server.restart.begin", reason=reason)
        self._stop_server()
        # 给 uvicorn 一点时间退出旧线程，避免旧端口尚未释放时立刻拉起新实例。
        time.sleep(1)
        self._start_server()
        for _ in range(10):
            time.sleep(0.5)
            if self._check_port():
                logger.info("本地服务重启成功")
                self._append_wake_recovery_log("server.restart.ok", reason=reason)
                return True
        logger.error("本地服务重启后端口仍不可达")
        self._append_wake_recovery_log("server.restart.failed", reason=reason)
        return False

    def _pick_probe_key_after_wake(self) -> dict | None:
        """挑一个最适合做唤醒后真实探活的 key。

        这里故意不引入新的“默认探活 key”配置，而是优先复用当前已启用、且有模型列表的
        第一个 key。这样能用最小改动把真实上游请求接进恢复流程，同时避免把探活逻辑绑死
        在某个供应商或固定模型上。
        """
        for key in list_keys():
            if str(key.get("status") or "") != "active":
                continue
            if not key.get("model_list"):
                continue
            self._append_wake_recovery_log(
                "probe.upstream.key_selected",
                alias=key.get("alias", ""),
                provider=key.get("provider", ""),
            )
            return key
        return None

    def _probe_upstream_after_wake(self) -> tuple[bool, str]:
        """唤醒后做一次真实上游轻探活，避免“本地 ready 但上游链路仍未恢复”的漏检。"""
        key = self._pick_probe_key_after_wake()
        if key is None:
            logger.info("唤醒后未找到可用 key，跳过真实上游探活")
            self._append_wake_recovery_log("probe.upstream.skipped", reason="no_probe_key")
            return True, "no_probe_key"
        try:
            result = asyncio.run(test_key_connectivity(key, allow_fallback=True))
        except Exception as exc:
            logger.warning("唤醒后真实上游探活执行失败: %s", exc)
            self._append_wake_recovery_log(
                "probe.upstream.failed",
                alias=key.get("alias", ""),
                provider=key.get("provider", ""),
                reason="upstream_probe_failed",
                error=str(exc),
            )
            return False, "upstream_probe_failed"
        if result.get("ok") is True:
            logger.info(
                "唤醒后真实上游探活成功: alias=%s provider=%s api_path=%s",
                key.get("alias", ""),
                key.get("provider", ""),
                result.get("api_path", ""),
            )
            self._append_wake_recovery_log(
                "probe.upstream.ok",
                alias=key.get("alias", ""),
                provider=key.get("provider", ""),
                api_path=result.get("api_path", ""),
                latency_ms=result.get("latency_ms", 0),
            )
            return True, "ok"
        logger.warning(
            "唤醒后真实上游探活失败: alias=%s provider=%s status=%s error=%s",
            key.get("alias", ""),
            key.get("provider", ""),
            result.get("status_code", 0),
            result.get("error", ""),
        )
        self._append_wake_recovery_log(
            "probe.upstream.failed",
            alias=key.get("alias", ""),
            provider=key.get("provider", ""),
            reason="upstream_probe_failed",
            status_code=result.get("status_code", 0),
            error=result.get("error", ""),
            api_path=result.get("api_path", ""),
        )
        return False, "upstream_probe_failed"

    def _recover_after_wake(self):
        """系统唤醒后执行分级自愈：先保本地服务 ready，再用真实上游探活决定是否重启。"""
        self._wake_recovering = True
        self._last_wake_recover_at = time.time()
        self._wake_recover_delay_sec = self._read_wake_recover_delay_seconds()
        previous_title = self.status_item.title
        logger.info("检测到系统唤醒，开始执行恢复流程")
        self._append_wake_recovery_log(
            "wake.recovery.begin",
            delay_sec=self._wake_recover_delay_sec,
            previous_status=previous_title,
        )
        try:
            self._update_status_for_recovery("🟡 唤醒恢复中...")
            # 唤醒后的前几秒通常还在恢复 Wi-Fi、VPN、DNS 或代理路由，过早探针容易误判。
            time.sleep(self._wake_recover_delay_sec)
            self._append_wake_recovery_log("wake.recovery.after_delay", delay_sec=self._wake_recover_delay_sec)
            ok, reason = self._probe_local_service_after_wake()
            if not ok:
                logger.warning("唤醒后本地服务探针失败，准备自愈: %s", reason)
                if self._restart_server_internal(f"wake_recovery:{reason}"):
                    self._append_wake_recovery_log("wake.recovery.ok", reason=reason, action="restart_local_server")
                    self._update_status_for_recovery("🟢 运行中")
                    return
                self._append_wake_recovery_log("wake.recovery.failed", reason=reason, action="restart_local_server")
                self._update_status_for_recovery("🔴 唤醒恢复失败")
                return
            logger.info("唤醒后本地服务探针通过，继续执行真实上游探活")
            upstream_ok, upstream_reason = self._probe_upstream_after_wake()
            if upstream_ok:
                self._append_wake_recovery_log("wake.recovery.ok", reason=upstream_reason, action="none")
                self._update_status_for_recovery("🟢 运行中")
                return
            logger.warning("唤醒后真实上游探活失败，准备通过重启本地服务做进一步自愈: %s", upstream_reason)
            if self._restart_server_internal(f"wake_recovery:{upstream_reason}"):
                self._append_wake_recovery_log("wake.recovery.ok", reason=upstream_reason, action="restart_local_server")
                self._update_status_for_recovery("🟢 运行中")
                return
            self._append_wake_recovery_log("wake.recovery.failed", reason=upstream_reason, action="restart_local_server")
            self._update_status_for_recovery("🔴 唤醒恢复失败")
        finally:
            if self.status_item.title == previous_title and previous_title:
                self._update_status_for_recovery(previous_title)
            self._wake_recovering = False
            # 唤醒恢复完成后触发一次日志清理
            _trigger_log_cleanup()

    def _get_icon(self) -> str | None:
        """获取菜单栏图标，支持圆角处理"""
        candidates = [
            os.path.join(self._resources_dir(), "logo.png"),
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "logo.png"),
            os.path.expanduser("~/.akm/logo.png"),
        ]
        for path in candidates:
            if os.path.exists(path):
                rounded = _round_corners(path)
                return rounded
        return None

    @staticmethod
    def _resources_dir() -> str:
        """py2app 打包后的 Resources 目录，开发环境返回当前目录"""
        if hasattr(sys, "frozen") or "Python" not in sys.executable:
            return os.path.join(os.path.dirname(sys.executable), "..", "Resources")
        return os.path.dirname(os.path.dirname(__file__))

    def _check_port(self) -> bool:
        """检查目标端口是否可达（服务已启动）"""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=0.5)
            sock.close()
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    def _start_server(self):
        """启动 FastAPI 服务（后台线程）并监控启动状态"""
        if self.server_running:
            return

        import uvicorn.server
        import uvicorn.config

        # 打包环境下确保模块已加载，uvicorn 才能通过字符串 "akm.server:app" 找到
        import akm.server  # noqa: F401

        config = uvicorn.config.Config(
            "akm.server:app",
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        server = uvicorn.server.Server(config)
        self._uvicorn_server = server
        self.server_ready = False
        self.startup_error = None

        def run_server():
            try:
                server.run()
            except Exception as e:
                self.startup_error = str(e)
            finally:
                self.server_running = False

        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()
        self.server_running = True

        # 异步监控启动状态
        self.status_item.title = "🟡 启动中..."
        def monitor_startup():
            max_wait = 10
            for _ in range(max_wait * 2):
                time.sleep(0.5)
                if self._check_port():
                    self.server_ready = True
                    self.status_item.title = "🟢 运行中"
                    # 服务就绪后注入宿主系统通知，供 webhook_notifier 等插件使用
                    self._register_host_notify()
                    if config_get("auto_open_admin", True) and self._first_start:
                        self._first_start = False
                        threading.Timer(
                            0.5,
                            lambda: webbrowser.open(f"http://{self.host}:{self.port}/admin"),
                        ).start()
                    return
                if self.startup_error or (self.server_thread and not self.server_thread.is_alive()):
                    self.status_item.title = "🔴 启动失败"
                    return
            self.status_item.title = "🔴 启动失败"

        threading.Thread(target=monitor_startup, daemon=True).start()

    def _stop_server(self):
        """停止 FastAPI 服务"""
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
            self.server_running = False
            self.server_ready = False
            self.status_item.title = "⚫ 已停止"

    def restart_server(self, _):
        """重启 FastAPI 服务"""
        self._restart_server_internal("manual_restart")

    # ── 回调 ────────────────────────────────────────────

    def open_admin(self, _):
        """打开 Web 管理页面"""
        webbrowser.open(f"http://{self.host}:{self.port}/admin")

    def host_notify(self, title: str, subtitle: str = "", message: str = "") -> None:
        """向 macOS 发送系统通知（由插件经 PluginManager 调用）。

        rumps.notification 最终走 AppKit，可从服务线程调用；
        失败只记日志，避免拖垮转发主链路。
        """
        try:
            rumps.notification(
                title=str(title or "AKM"),
                subtitle=str(subtitle or ""),
                message=str(message or ""),
            )
        except Exception as exc:
            logger.warning("宿主系统通知发送失败: %s", exc)

    def _register_host_notify(self) -> None:
        """把 host_notify 注入到当前进程内的 PluginManager（服务线程已 load_all）。

        菜单栏与 uvicorn 共享同一进程与 ``akm.server:app`` 单例，
        因此可在端口就绪后直接访问 app.state.plugin_manager。
        """
        try:
            from akm.server import app as fastapi_app

            pm = getattr(getattr(fastapi_app, "state", None), "plugin_manager", None)
            if pm is None:
                logger.warning("服务已就绪但 plugin_manager 尚未挂载，稍后重试注入通知")
                # 插件加载可能略晚于端口监听，短暂重试几次
                def _retry():
                    for _ in range(10):
                        time.sleep(0.3)
                        try:
                            from akm.server import app as app2
                            pm2 = getattr(getattr(app2, "state", None), "plugin_manager", None)
                            if pm2 is not None:
                                pm2.set_notify(self.host_notify)
                                logger.info("宿主系统通知已注入 PluginManager（延迟）")
                                return
                        except Exception as inner:
                            logger.warning("延迟注入宿主通知失败: %s", inner)
                            return
                    logger.warning("多次重试后仍无法注入宿主通知")

                threading.Thread(target=_retry, daemon=True).start()
                return
            pm.set_notify(self.host_notify)
            logger.info("宿主系统通知已注入 PluginManager")
        except Exception as exc:
            logger.warning("注入宿主系统通知失败: %s", exc)

    def quit_app(self, _):
        """退出应用"""
        # 尽量清空注入，避免退出后残留可调用闭包
        try:
            from akm.server import app as fastapi_app
            pm = getattr(getattr(fastapi_app, "state", None), "plugin_manager", None)
            if pm is not None:
                pm.set_notify(None)
        except Exception:
            pass
        rumps.quit_application()


def main():
    """菜单栏应用入口"""
    AKMApp().run()


if __name__ == "__main__":
    main()
