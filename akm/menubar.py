"""macOS 菜单栏应用 — 状态栏图标 + 服务管理"""

import os
import sys
import time
import threading
import webbrowser
import socket
import logging
import asyncio
import json
from datetime import datetime

import httpx
import rumps
from akm import __version__
from akm.config import get as config_get
from akm.key_pool import list_keys
from akm.proxy import test_key_connectivity

try:
    from AppKit import NSWorkspace, NSWorkspaceDidWakeNotification
    from Foundation import NSObject
except ImportError:
    NSWorkspace = None
    NSWorkspaceDidWakeNotification = None
    NSObject = object


# GitHub 仓库标识，格式固定为 "owner/repo"，用于拼接 Releases API 地址。
GITHUB_REPO = "nkHub/akm"
# 更新检查时间间隔（秒）。这里使用 24 小时，避免每次唤醒都请求 API，降低限流风险。
CHECK_INTERVAL = 86400

logger = logging.getLogger("akm.menubar")
DEFAULT_WAKE_RECOVER_DELAY_SEC = 8.0


def _wake_recovery_log_path() -> str:
    """返回唤醒恢复日志路径，并确保目录存在。"""
    log_dir = os.path.expanduser("~/.akm")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "wake-recovery.log")


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
        img = img.resize((size, size), Image.LANCZOS)

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
            quit_button=None,
        )
        self.server_thread: threading.Thread | None = None
        self.server_ready = False
        self.server_running = False
        self.startup_error: str | None = None
        self.port = config_get("server_port", 8800)
        self.host = "127.0.0.1"
        self._uvicorn_server = None  # uvicorn.Server 实例，用于优雅关闭
        self._first_start = True     # 首次启动标记，仅首次自动打开浏览器
        self._wake_recovering = False
        self._last_wake_recover_at = 0.0
        self._wake_recover_delay_sec = self._read_wake_recover_delay_seconds()
        self._wake_recover_min_interval_sec = 20.0
        self._wake_observer = None
        self._wake_notification_center = None
        # 更新菜单项对象。默认没有更新提示，只有检测到新版本后才动态插入菜单。
        self.update_item: rumps.MenuItem | None = None

        # 动态菜单项
        self.status_item = rumps.MenuItem(title="🟡 启动中...")
        self.menu = [
            self.status_item,
            rumps.MenuItem(title="打开管理", callback=self.open_admin),
            None,  # 分隔线
            rumps.MenuItem(title="重启服务", callback=self.restart_server),
            rumps.MenuItem(title="退出", callback=self.quit_app),
        ]

        # 后台启动服务并监控状态
        self._start_server()
        # 后台启动更新检查线程。该线程与服务启动解耦，即使服务未成功启动也可提示新版本。
        self._start_update_checker()
        self._install_wake_observer()

    def _fetch_update_info(self) -> dict:
        """从 GitHub Releases API 拉取最新版本信息并与本地版本比对。"""
        try:
            resp = httpx.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                timeout=10,
            )
            if resp.status_code != 200:
                return {"has_update": False}

            payload = resp.json()
            latest = payload.get("tag_name", "").lstrip("v")
            if latest and latest != __version__:
                return {
                    "has_update": True,
                    "latest": latest,
                    "current": __version__,
                    "url": payload.get("html_url", ""),
                }
        except Exception:
            # 更新检查属于非关键路径：网络异常、API 失败都不影响主功能，静默降级即可。
            pass
        return {"has_update": False}

    def _open_release_page(self, _):
        """点击更新菜单后打开 Release 页面。"""
        if self.update_item and self.update_item.key:
            webbrowser.open(self.update_item.key)

    def _apply_update_menu(self, info: dict):
        """根据检查结果动态维护“更新到 vX.Y.Z”菜单项。"""
        has_update = info.get("has_update", False)
        if not has_update:
            # 已无更新时，移除旧的更新菜单，避免 UI 残留过期提示。
            if self.update_item and self.update_item in self.menu:
                self.menu.pop(self.menu.index(self.update_item))
            self.update_item = None
            return

        latest = info.get("latest", "")
        release_url = info.get("url", "")
        if not latest or not release_url:
            return

        title = f"更新到 v{latest}"
        if self.update_item is None:
            self.update_item = rumps.MenuItem(title=title, callback=self._open_release_page)
            # 利用 MenuItem.key 暂存链接，减少额外状态字段，保持改动最小。
            self.update_item.key = release_url
            # 插在“打开管理”后面，保证更新入口显眼但不干扰状态项。
            self.menu.insert_after("打开管理", self.update_item)
            return

        self.update_item.title = title
        self.update_item.key = release_url

    def _start_update_checker(self):
        """后台循环检查更新并更新菜单提示。"""

        def run_checker():
            # 首次立即检查一次，启动后尽快给用户反馈。
            info = self._fetch_update_info()
            self._apply_update_menu(info)

            # 后续按固定间隔轮询，避免频繁请求 API。
            while True:
                time.sleep(CHECK_INTERVAL)
                info = self._fetch_update_info()
                self._apply_update_menu(info)

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
        self._uvicorn_server = uvicorn.server.Server(config)
        self.server_ready = False
        self.startup_error = None

        def run_server():
            try:
                self._uvicorn_server.run()
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

    def quit_app(self, _):
        """退出应用"""
        rumps.quit_application()


def main():
    """菜单栏应用入口"""
    AKMApp().run()


if __name__ == "__main__":
    main()
