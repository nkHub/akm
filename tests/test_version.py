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
