"""macOS 菜单栏应用 — 状态栏图标 + 服务管理"""

import os
import sys
import time
import threading
import webbrowser
import socket

import uvicorn
import rumps
from akm.config import get as config_get


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
        self.startup_error: str | None = None
        self.port = 8800
        self.host = "127.0.0.1"

        # 动态菜单项
        self.status_item = rumps.MenuItem(title="🟡 启动中...")
        self.menu = [
            self.status_item,
            rumps.MenuItem(title="打开管理", callback=self.open_admin),
            None,  # 分隔线
            rumps.MenuItem(title="退出", callback=self.quit_app),
        ]

        # 后台启动服务并监控状态
        self._start_server()

    def _get_icon(self) -> str | None:
        """获取菜单栏图标，支持圆角处理"""
        candidates = [
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "logo.jpg"),
            os.path.expanduser("~/.akm/logo.png"),
        ]
        for path in candidates:
            if os.path.exists(path):
                rounded = _round_corners(path)
                return rounded
        return None

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

        def run_server():
            try:
                uvicorn.run(
                    "akm.server:app",
                    host=self.host,
                    port=self.port,
                    log_level="warning",
                )
            except Exception as e:
                self.startup_error = str(e)

        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()

        # 异步监控启动状态
        def monitor_startup():
            max_wait = 10  # 最多等待 10 秒
            for _ in range(max_wait * 2):
                time.sleep(0.5)
                if self._check_port():
                    self.server_ready = True
                    self.status_item.title = "🟢 运行中"
                    # 根据配置决定是否自动打开浏览器
                    if config_get("auto_open_admin", True):
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
