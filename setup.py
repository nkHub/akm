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
    ("", ["logo.png"]),
    ("templates", [
        "akm/templates/_layout.html",
        "akm/templates/_sidebar.html",
        "akm/templates/_header.html",
        "akm/templates/_styles.html",
        "akm/templates/_toggle_sidebar.html",
        "akm/templates/dashboard.html",
        "akm/templates/logs.html",
        "akm/templates/keys.html",
        "akm/templates/settings.html",
        "akm/templates/about.html",
    ]),
    ("static", [
        "akm/static/marked.min.js",
        "akm/static/tailwindcss.js",
        "akm/static/chat-viewer.js",
        "akm/static/json-viewer.js",
        "akm/static/json-worker.js",
    ]),
]

OPTIONS = {
    "argv_emulation": False,
    "packages": ["akm", "rumps", "uvicorn", "fastapi", "httpx", "click", "cryptography", "PIL", "anyio"],
    "includes": ["akm.server", "akm.db", "akm.key_pool", "akm.proxy", "akm.audit", "akm.models", "akm.config", "akm.agent", "akm.adapter", "akm.cli", "_cffi_backend"],
    "excludes": ["tkinter", "PyQt5", "PySide2", "wx"],
    "iconfile": "logo.icns",
    "plist": {
        "CFBundleName": "AI Key Manager",
        "CFBundleDisplayName": "AI Key Manager",
        "CFBundleIdentifier": "com.akm.app",
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        "LSUIElement": True,  # 菜单栏应用，不显示 Dock 图标
        "NSHighResolutionCapable": True,
    },
}

setup(
    app=APP,
    name="AI Key Manager",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
