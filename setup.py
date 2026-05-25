"""py2app 打包脚本 — 生成 macOS .app 应用"""

import os
from setuptools import setup

APP = ["akm/menubar.py"]
DATA_FILES = [
    ("", ["logo.jpg"]),
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
]

OPTIONS = {
    "argv_emulation": False,
    "packages": ["akm", "rumps", "uvicorn", "fastapi", "httpx", "click", "cryptography", "PIL"],
    "includes": ["akm.server", "akm.db", "akm.key_pool", "akm.proxy", "akm.audit", "akm.models"],
    "excludes": ["tkinter", "PyQt5", "PySide2", "wx"],
    "plist": {
        "CFBundleName": "AI Key Manager",
        "CFBundleDisplayName": "AI Key Manager",
        "CFBundleIdentifier": "com.akm.app",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
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
