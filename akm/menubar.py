"""macOS 菜单栏应用 — 状态栏图标 + 服务管理"""

import os
import sys
import time
import threading
import webbrowser
import socket

import httpx
import rumps
from akm import __version__
from akm.config import get as config_get


# GitHub 仓库标识，格式固定为 "owner/repo"，用于拼接 Releases API 地址。
GITHUB_REPO = "nkHub/akm"
# 更新检查时间间隔（秒）。这里使用 24 小时，避免每次唤醒都请求 API，降低限流风险。
CHECK_INTERVAL = 86400


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
        self._stop_server()
        # 等待旧服务完全停止
        time.sleep(1)
        self._start_server()

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
