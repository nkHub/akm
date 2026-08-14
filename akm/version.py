"""版本比较工具 — 供 App 自动更新与插件市场共用"""


def version_greater(a: str, b: str) -> bool:
    """按语义化版本比较 a > b（逐段数字比较，忽略前导 v 与尾部分隔符）。

    规则：
    1. 先逐段比较数字部分（0.2.0 > 0.1.99、0.1.22 > 0.1.21）。
    2. 数字部分完全相等时，无后缀（正式版）大于有后缀（预发布版）：
       0.1.22 > 0.1.22-beta。
    3. 两段都是非纯数字段时退化为字符串比较，保证不抛异常。
    """

    def _split(v: str) -> tuple[list[int], str | None]:
        """拆分版本号为数字段列表与末尾后缀（无后缀为 None）。"""
        raw = v.strip().lstrip("v").rstrip(".")
        nums: list[int] = []
        suffix: str | None = None
        for seg in raw.split("."):
            if not seg:
                continue
            head = ""
            for ch in seg:
                if ch.isdigit():
                    head += ch
                else:
                    break
            if head:
                nums.append(int(head))
                if head != seg and suffix is None:
                    suffix = seg[len(head):]
            elif suffix is None:
                suffix = seg
        return nums, suffix

    na, sa = _split(a)
    nb, sb = _split(b)
    for x, y in zip(na, nb):
        if x != y:
            return x > y
    if len(na) != len(nb):
        return len(na) > len(nb)
    # 数字完全相等：无后缀 > 有后缀（pre-release 语义）
    if sa is None and sb is not None:
        return True
    if sb is None and sa is not None:
        return False
    if sa is not None and sb is not None:
        return sa > sb
    return False
