"""version.py 版本比较工具测试。

version_greater 被 App 自动更新与插件市场共用，保证语义化版本比较行为一致。
"""

from akm.version import version_greater


def test_equal_versions():
    """相同版本不认为更大。"""
    assert not version_greater("0.1.2", "0.1.2")
    assert not version_greater("v0.1.2", "0.1.2")
    assert not version_greater("0.1.2", "0.1.2.")


def test_patch_bump():
    """补丁版本号递增应判定更大。"""
    assert version_greater("0.1.3", "0.1.2")
    assert not version_greater("0.1.2", "0.1.3")


def test_minor_and_major_bump():
    """次版本与主版本递增应判定更大。"""
    assert version_greater("0.2.0", "0.1.99")
    assert version_greater("1.0.0", "0.9.9")


def test_missing_segment():
    """缺段视为较小：0.1.22 > 0.1。"""
    assert version_greater("0.1.22", "0.1")
    assert not version_greater("0.1", "0.1.22")


def test_prerelease_less_than_release():
    """正式版大于预发布版：0.1.22 > 0.1.22-beta。"""
    assert version_greater("0.1.22", "0.1.22-beta")
    assert not version_greater("0.1.22-beta", "0.1.22")


def test_leading_v_is_ignored():
    """前导 v 不影响比较。"""
    assert version_greater("v0.2.0", "0.1.9")
    assert version_greater("v0.1.3", "v0.1.2")


# ── scripts/check_version.py：发布前版本号校验 ──────────────────
# 这些用例保护「防止跳版本号 / 版本倒挂」的判定口径，避免后续改动
# 把「本地等于线上」误判成可发布。

import importlib.util
from pathlib import Path


def _load_check_version():
    """按文件路径加载 scripts/check_version.py（不在包内，无法直接 import）。"""
    path = Path(__file__).resolve().parent.parent / "scripts" / "check_version.py"
    spec = importlib.util.spec_from_file_location("akm_check_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_one_rejects_equal_version():
    """本地等于线上：未 bump，必须报错。

    这正是「跳版本号」问题的相邻形态——不 bump 就发布会让 zip 覆盖同名资产、
    sha256 变化而版本号不动，已安装用户永远收不到更新。
    """
    cv = _load_check_version()
    level, msg = cv.check_one("0.1.59", "0.1.59")
    assert level == "error"
    assert "未 bump" in msg


def test_check_one_rejects_local_lower_than_online():
    """本地低于线上：版本倒挂，必须报错。"""
    cv = _load_check_version()
    level, msg = cv.check_one("0.1.58", "0.1.59")
    assert level == "error"
    assert "倒挂" in msg


def test_check_one_accepts_local_higher_and_suggests_bump():
    """本地高于线上：通过，并给出 bump 级别建议。"""
    cv = _load_check_version()
    level, msg = cv.check_one("0.1.60", "0.1.59")
    assert level == "ok"
    assert "patch" in msg


def test_check_one_skips_when_online_unknown():
    """线上版本读不到：跳过比对，不判失败。"""
    cv = _load_check_version()
    level, _ = cv.check_one("0.1.59", None)
    assert level == "skip"


def test_check_one_rejects_empty_local_version():
    """本地没有版本号：必须报错。"""
    cv = _load_check_version()
    level, _ = cv.check_one("", "0.1.59")
    assert level == "error"


def test_suggest_bump_maps_changed_segment_to_level():
    """差异出现在哪一段，就建议对应的 bump 级别。"""
    cv = _load_check_version()
    assert "patch" in cv.suggest_bump("0.1.60", "0.1.59")
    assert "minor" in cv.suggest_bump("0.2.0", "0.1.59")
    assert "major" in cv.suggest_bump("1.0.0", "0.1.59")


def test_suggest_bump_treats_missing_segments_as_zero():
    """0.2 与 0.2.0 等价，不应被判成「有变化」。"""
    cv = _load_check_version()
    assert cv.suggest_bump("0.2", "0.2.0") == "无变化"


def test_parse_parts_strips_prefix_and_suffix():
    """版本号解析：忽略前导 v 与后缀，只取数字段。"""
    cv = _load_check_version()
    assert cv.parse_parts("v0.1.59") == [0, 1, 59]
    assert cv.parse_parts("0.1.59-beta") == [0, 1, 59]
