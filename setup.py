"""py2app 打包脚本 — 生成 macOS .app 应用"""

import os
import re
from pathlib import Path
from setuptools import setup


def _read_version() -> str:
    content = Path("akm/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    if not match:
        raise RuntimeError("无法读取版本号")
    return match.group(1)


__version__ = _read_version()

APP = ["akm/menubar.py"]
DATA_FILES = [
    ("", ("logo.png",)),
    ("templates", (
        "akm/templates/_layout.html",
        "akm/templates/_sidebar.html",
        "akm/templates/_header.html",
        "akm/templates/_styles.html",
        "akm/templates/_theme.html",
        "akm/templates/_toggle_sidebar.html",
        "akm/templates/dashboard.html",
        "akm/templates/logs.html",
        "akm/templates/keys.html",
        "akm/templates/settings.html",
        "akm/templates/pool.html",
        "akm/templates/about.html",
        "akm/templates/plugins.html",
        "akm/templates/plugin_host.html",
    )),
    ("static", (
        "akm/static/akm-ui.js",
        "akm/static/marked.min.js",
        "akm/static/tailwindcss.js",
        "akm/static/chat-viewer.js",
        "akm/static/json-viewer.js",
        "akm/static/json-worker.js",
        "akm/static/index.html",
    )),
]

OPTIONS = {
    "argv_emulation": False,
    "packages": ["akm", "rumps", "uvicorn", "fastapi", "httpx", "click", "anyio", "sqlite_vec", "objc", "croniter", "dateutil"],
    "includes": ["akm.server", "akm.db", "akm.key_pool", "akm.crypto", "akm.secret_store", "akm.proxy", "akm.audit", "akm.models", "akm.config", "akm.agent", "akm.adapter", "akm.cli", "sqlite_vec", "_cffi_backend"],
    "excludes": ["tkinter", "PyQt5", "PySide2", "wx", "jieba3", "numpy", "numpy._core", "numpy.linalg", "numpy.fft", "numpy.random", "numpy.distutils", "numpy.lib", "numpy.ma", "numpy.matrixlib", "numpy.polynomial", "numpy.testing", "numpy.typing", "docutils", "PIL", "rich", "pygments", "websockets", "tinyaes"],
    "iconfile": "logo.icns",
    "plist": {
        "CFBundleName": "AI Key Manager",
        "CFBundleDisplayName": "AI Key Manager",
        "CFBundleIdentifier": "com.akm.app",
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        "LSUIElement": True,  # 菜单栏应用，不显示 Dock 图标
        "NSHighResolutionCapable": True,
        # 目录用途声明：插件工作区可能位于桌面/文稿/下载等受保护目录（例如知识库
        # 绑定的项目就在桌面上），缺少声明时系统弹窗只能显示默认文案，无法向用户
        # 解释访问原因。声明本身不授予权限，仍需用户在弹窗中确认一次。
        "NSDesktopFolderUsageDescription": "AKM 需要读取你绑定为插件工作区的桌面项目文件（例如知识库索引与项目记忆），仅在你使用相关功能时访问。",
        "NSDocumentsFolderUsageDescription": "AKM 需要读取你绑定为插件工作区的文稿目录文件，仅在你使用相关功能时访问。",
        "NSDownloadsFolderUsageDescription": "AKM 需要读取你指定为下载/更新目录的文件（例如自动更新包），仅在你使用相关功能时访问。",
    },
}

setup(
    app=APP,
    name="AI Key Manager",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)
