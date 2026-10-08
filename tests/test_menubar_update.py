"""macOS 菜单栏应用更新链路的回归测试。

覆盖两类真实缺陷的防回归：
1. 选包兜底：架构匹配不到的 zip 绝不能被静默下载安装（Intel 机器装上
   arm64 包会「替换成功、启动即崩」，且旧版已不在原位）。
2. 架构守卫：替换旧 .app 前必须校验新 .app 主可执行架构与本机兼容，
   不匹配时中止安装并原位保留旧版。
"""

import os
import struct
import zipfile

import pytest

import akm.menubar as menubar_mod
from akm.menubar import AKMApp


def _asset(name: str) -> dict:
    """构造最小 Release 资产替身。"""
    return {"name": name, "browser_download_url": f"https://example.com/{name}"}


def _pick(assets: list) -> str:
    """统一入口：调用静态方法，便于用例集中断言。"""
    return menubar_mod.AKMApp._pick_zip_download_url(assets)


class TestPickZipDownloadUrl:
    """更新包选择：架构优先，且绝不退回错误架构。"""

    def test_prefers_matching_arch(self, monkeypatch):
        """arm64 机器在多架构资产中应选中 arm64 zip。"""
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "arm64")
        assets = [_asset("AI.Key.Manager-0.1.55-arm64.zip"), _asset("AI.Key.Manager-0.1.55-x86_64.zip")]
        assert _pick(assets) == "https://example.com/AI.Key.Manager-0.1.55-arm64.zip"

    def test_x86_64_machine_picks_x86_64_zip(self, monkeypatch):
        """Intel 机器应选中 x86_64 zip（若发布提供）。"""
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "x86_64")
        assets = [_asset("AI.Key.Manager-0.1.55-arm64.zip"), _asset("AI.Key.Manager-0.1.55-x86_64.zip")]
        assert _pick(assets) == "https://example.com/AI.Key.Manager-0.1.55-x86_64.zip"

    def test_never_falls_back_to_wrong_arch(self, monkeypatch):
        """回归：Release 只有他架构 zip 时必须返回空串，不得退回任意 zip。"""
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "x86_64")
        assert _pick([_asset("AI.Key.Manager-0.1.55-arm64.zip")]) == ""
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "arm64")
        assert _pick([_asset("AI.Key.Manager-0.1.55-x86_64.zip")]) == ""

    def test_returns_empty_without_zip_asset(self, monkeypatch):
        """没有 zip 资产时返回空串（走「未提供更新包」提示路径）。"""
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "arm64")
        assert _pick([_asset("AI.Key.Manager-0.1.55-arm64.dmg")]) == ""

    def test_returns_empty_for_unknown_machine(self, monkeypatch):
        """无法识别本机架构时宁可放弃自动更新，也不乱装。"""
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "")
        assert _pick([_asset("AI.Key.Manager-0.1.55-arm64.zip")]) == ""


def _macho(cputype: int) -> bytes:
    """构造最小 64 位 Mach-O 可执行头（足以让 /usr/bin/file 识别架构）。"""
    return struct.pack("<IiiIIIII", 0x0FEEDFACF, cputype, 0, 2, 0, 0, 0, 0)


def _make_app_bundle(parent, arch: str = "arm64") -> str:
    """在 parent 下构造带指定架构主二进制的最小 .app 目录，返回路径。"""
    cputype = 0x0100000C if arch == "arm64" else 0x01000007
    macos_dir = os.path.join(parent, "AI Key Manager.app", "Contents", "MacOS")
    os.makedirs(macos_dir, exist_ok=True)
    with open(os.path.join(macos_dir, "AI Key Manager"), "wb") as f:
        f.write(_macho(cputype))
    return os.path.join(parent, "AI Key Manager.app")


class TestValidateNewAppArch:
    """替换前架构校验：不匹配硬性拦截，探测失败放行。"""

    def test_blocks_mismatched_binary(self, tmp_path, monkeypatch):
        """回归：arm64 更新包装上 x86_64 机器前必须被拦截并给出原因。"""
        new_app = _make_app_bundle(str(tmp_path), arch="arm64")
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "x86_64")
        error = menubar_mod._validate_new_app_arch(new_app)
        assert error
        assert "arm64" in error and "x86_64" in error

    def test_allows_matching_binary(self, tmp_path, monkeypatch):
        """架构一致时放行，返回空串。"""
        new_app = _make_app_bundle(str(tmp_path), arch="arm64")
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "arm64")
        assert menubar_mod._validate_new_app_arch(new_app) == ""

    def test_allows_universal_binary(self, tmp_path, monkeypatch):
        """通用二进制（含两种架构）在任何机器上都应放行。"""
        new_app = _make_app_bundle(str(tmp_path), arch="arm64")
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "x86_64")

        def fake_file(*args, **kwargs):
            class R:
                stdout = "Mach-O universal binary with 2 architectures"
                returncode = 0

            return R()

        monkeypatch.setattr(menubar_mod.subprocess, "run", fake_file)
        assert menubar_mod._validate_new_app_arch(new_app) == ""

    def test_passes_when_file_unavailable(self, tmp_path, monkeypatch):
        """file 不可用时放行（返回空串），校验本身不阻断更新。"""
        new_app = _make_app_bundle(str(tmp_path), arch="arm64")
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "x86_64")

        def boom(*args, **kwargs):
            raise FileNotFoundError("no file")

        monkeypatch.setattr(menubar_mod.subprocess, "run", boom)
        assert menubar_mod._validate_new_app_arch(new_app) == ""

    def test_current_machine_arch_normalizes_names(self, monkeypatch):
        """aarch64 视同 arm64，x86_64/amd64 视同 x86_64。"""
        for raw, want in (("arm64", "arm64"), ("aarch64", "arm64"), ("x86_64", "x86_64"), ("AMD64", "x86_64")):
            monkeypatch.setattr(menubar_mod.platform, "machine", lambda raw=raw: raw)
            assert menubar_mod._current_machine_arch() == want


def _make_app(silent_capture: list) -> AKMApp:
    """构造最小 AKMApp 实例（跳过 rumps.App.__init__，避免依赖图标资源）。"""
    app = AKMApp.__new__(AKMApp)
    app._updating = False
    app._updating_msg = ""
    app._update_failed_msg = ""
    app._update_cancelled_msg = ""
    app._relaunch_pending = False
    app._update_progress_value = 0.0
    app._update_progress_done = False
    app._update_cancel_requested = False
    app._update_dialog = None
    app._safe_notify = lambda title, message: silent_capture.append((title, message))
    return app


def _write_update_zip(dest: str, arch: str = "arm64") -> None:
    """把最小 .app（指定架构）打进 zip，模拟更新包。"""
    cputype = 0x0100000C if arch == "arm64" else 0x01000007
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("AI Key Manager.app/Contents/MacOS/AI Key Manager", _macho(cputype))
        zf.writestr("AI Key Manager.app/Contents/Info.plist", "<plist/>")


class TestPerformUpdateArchGuard:
    """端到端：架构不符中止安装并保留旧版，架构一致正常安装。"""

    def _setup_env(self, monkeypatch, tmp_path):
        """隔离 HOME 与安装位置，返回安装目标路径。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        target = os.path.join(str(tmp_path), "installed", "AI Key Manager.app")
        os.makedirs(os.path.join(target, "Contents", "MacOS"), exist_ok=True)
        with open(os.path.join(target, "Contents", "MacOS", "old-binary"), "w") as f:
            f.write("old")
        monkeypatch.setattr(menubar_mod, "_bundle_app_path", lambda: target)
        monkeypatch.setattr(menubar_mod.sys, "frozen", True, raising=False)
        return target

    def test_aborts_on_arch_mismatch_and_keeps_old_app(self, tmp_path, monkeypatch):
        """回归：装错架构的包必须停在校验，旧 .app 原位保留、不触发重启。"""
        capture: list = []
        app = _make_app(capture)
        target = self._setup_env(monkeypatch, tmp_path)
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "x86_64")

        def fake_download(url, dest, progress_cb=None, cancel_check=None):
            _write_update_zip(dest, arch="arm64")
            return True

        monkeypatch.setattr(menubar_mod, "_download_file", fake_download)

        app._perform_update({"latest": "0.2.0", "download_url": "https://example.com/a.zip"}, silent=True)

        assert app._relaunch_pending is False
        assert os.path.isfile(os.path.join(target, "Contents", "MacOS", "old-binary"))
        assert any("架构" in msg and "不匹配" in msg for _, msg in capture)
        assert not any("更新完成" in title for title, _ in capture)

    def test_installs_matching_arch(self, tmp_path, monkeypatch):
        """架构一致时不被守卫拦截：正常备份、替换并进入重启流程。"""
        capture: list = []
        app = _make_app(capture)
        target = self._setup_env(monkeypatch, tmp_path)
        monkeypatch.setattr(menubar_mod, "_current_machine_arch", lambda: "arm64")
        monkeypatch.setattr(menubar_mod.AKMApp, "_wait_for_idle", lambda self, timeout: True)

        def fake_download(url, dest, progress_cb=None, cancel_check=None):
            _write_update_zip(dest, arch="arm64")
            return True

        monkeypatch.setattr(menubar_mod, "_download_file", fake_download)

        app._perform_update({"latest": "0.2.0", "download_url": "https://example.com/a.zip"}, silent=True)

        assert app._relaunch_pending is True
        new_bin = os.path.join(target, "Contents", "MacOS", "AI Key Manager")
        assert os.path.isfile(new_bin)
        with open(new_bin, "rb") as f:
            assert f.read(4) == _macho(0x0100000C)[:4]
        backup_dir = os.path.join(str(tmp_path), ".akm", "updates", "backups")
        assert any(name.endswith(".app") for name in os.listdir(backup_dir))
        assert any("更新完成" in title for title, _ in capture)
