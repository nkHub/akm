#!/usr/bin/env python3
"""发布前的版本号校验：防止跳版本号、防止本地版本号倒挂。

## 为什么需要这个脚本

发布流程此前只看本地 `akm/__init__.py` 的值，直接拿它当 tag 建 Release。
一旦本地版本号因为改错、拉错分支、或连续两次 bump 而没有对齐线上，就会出现：

- **跳版本号**：线上 0.1.59，本地写成 0.1.70，用户看到版本号断层，中间版本
  永远不存在，自动更新链路上也无法解释「0.1.60~0.1.69 去哪了」。
- **版本倒挂**：本地版本号低于或等于线上，`gh release create` 会失败或覆盖
  语义混乱，且已安装的客户端判断 `version_greater(latest, current)` 永远为假，
  用户收不到更新。
- **插件重复发布**：同一版本号重复上传 zip（`--clobber` 覆盖同名资产），
  sha256 变了但版本号没变，已安装用户的市场判断 `version_greater` 为假、
  永远拿不到这次修复。

## 逻辑（应用与插件共用一套）

1. 读**本地**版本号（应用读 `akm/__init__.py`，插件读各自 `plugin.json`）。
2. 读**线上**版本号：应用的权威来源是 **GitHub Releases latest 的 tag**；
   插件是 Release `plugin-market` 资产 `{name}-{version}.zip` 的最高版本。
3. 校验：
   - 本地 < 线上 → **错误**（版本倒挂，必须先修）
   - 本地 == 线上 → **错误**（没有 bump，发布会产生重复版本）
   - 本地 > 线上 → 通过，但按差异大小给出 bump 建议
4. 「按功能更新大小增加版本号」由人决定，脚本只**校验 + 建议**，不自动改版本号。

## 用法

    # 应用：只校验，输出建议
    ./scripts/check_version.py app

    # 插件：校验全部插件
    ./scripts/check_version.py plugins

    # 只看建议，不因不匹配而失败（本地开发时用）
    ./scripts/check_version.py app --warn-only

    # 离线：跳过网络，只用本地 git tag 判断
    ./scripts/check_version.py app --offline

退出码：0 = 校验通过；1 = 校验失败（版本倒挂 / 未 bump / 读取失败）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from akm.version import version_greater  # noqa: E402

GITHUB_REPO = "nkHub/akm"
PLUGIN_MARKET_TAG = "plugin-market"
PLUGIN_INDEX_PATH = ROOT_DIR / "plugins" / "plugins.json"


# ── 版本号读取 ──────────────────────────────────────────────

def read_app_version() -> str:
    """从 akm/__init__.py 读应用版本号（唯一运行时来源）。"""
    init_file = ROOT_DIR / "akm" / "__init__.py"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init_file.read_text("utf-8"))
    if not match:
        raise SystemExit(f"错误: 无法从 {init_file} 解析 __version__")
    return match.group(1)


def read_plugin_versions() -> dict[str, str]:
    """读取仓库内所有插件的 plugin.json 版本号。"""
    versions: dict[str, str] = {}
    for manifest in sorted((ROOT_DIR / "plugins").glob("*/plugin.json")):
        try:
            data = json.loads(manifest.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  警告: 跳过 {manifest}（{exc}）")
            continue
        name = data.get("name") or manifest.parent.name
        versions[str(name)] = str(data.get("version") or "")
    return versions


# ── 线上版本号 ──────────────────────────────────────────────

def _gh_api(path: str) -> dict | None:
    """调用 GitHub API；优先 `gh` CLI（带认证、额度高），否则退回匿名 HTTP。"""
    # 先用 gh：已登录时额度 5000/h，远高于匿名的 60/h
    try:
        proc = subprocess.run(
            ["gh", "api", path],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass  # 没装 gh / 超时 / 输出不是 JSON，都继续走匿名 HTTP

    try:
        import httpx
    except ImportError:
        print("  警告: 未安装 httpx，且 gh 不可用，无法查询线上版本")
        return None

    try:
        resp = httpx.get(f"https://api.github.com/{path}", timeout=15)
        if resp.status_code != 200:
            print(f"  警告: GitHub API 返回 HTTP {resp.status_code}（匿名限流时常见）")
            return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - 网络问题一律降级
        print(f"  警告: 查询 GitHub 失败: {exc}")
        return None


def fetch_app_online_version() -> str | None:
    """应用线上版本号 = GitHub Releases latest 的 tag（权威来源）。"""
    payload = _gh_api(f"repos/{GITHUB_REPO}/releases/latest")
    if not isinstance(payload, dict):
        return None
    tag = str(payload.get("tag_name") or "").strip()
    return tag.lstrip("v") or None


def fetch_plugins_online_versions() -> dict[str, str] | None:
    """插件线上版本号：取 Release `plugin-market` 资产里每个插件的最高版本。

    用 Release 资产而不是 plugins.json 索引：资产是真正被下载的产物，
    索引可能滞后于资产（索引靠人工 push 生效）。索引作为兜底。
    """
    payload = _gh_api(f"repos/{GITHUB_REPO}/releases/tags/{PLUGIN_MARKET_TAG}")
    result: dict[str, str] = {}
    if isinstance(payload, dict):
        for asset in payload.get("assets") or []:
            name = str(asset.get("name") or "")
            # 资产名形如 {plugin}-{version}.zip
            match = re.match(r"^(.+)-(\d[\w.\-]*)\.zip$", name)
            if not match:
                continue
            plugin, version = match.group(1), match.group(2)
            if plugin not in result or version_greater(version, result[plugin]):
                result[plugin] = version
    if result:
        return result

    # 兜底：仓库里的索引文件（push 后才生效，可能滞后）
    if PLUGIN_INDEX_PATH.exists():
        try:
            index = json.loads(PLUGIN_INDEX_PATH.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        plugins = index.get("plugins") or {}
        if isinstance(plugins, dict):
            for name, info in plugins.items():
                if isinstance(info, dict) and info.get("version"):
                    result[str(name)] = str(info["version"])
    return result or None


def fetch_online_from_git_tags() -> str | None:
    """离线兜底：本地已 fetch 的 `v*` tag 里数值最大的那个。"""
    try:
        proc = subprocess.run(
            ["git", "tag", "--list", "v*"],
            capture_output=True, text=True, timeout=10, check=False, cwd=ROOT_DIR,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    best: str | None = None
    for line in proc.stdout.splitlines():
        candidate = line.strip().lstrip("v")
        if not re.match(r"^\d", candidate):
            continue
        if best is None or version_greater(candidate, best):
            best = candidate
    return best


# ── 版本号差异分析 ──────────────────────────────────────────

def parse_parts(version: str) -> list[int]:
    """取版本号的数字段（0.1.59 → [0, 1, 59]）。"""
    raw = version.strip().lstrip("v")
    parts: list[int] = []
    for seg in raw.split("."):
        head = ""
        for ch in seg:
            if ch.isdigit():
                head += ch
            else:
                break
        if head:
            parts.append(int(head))
    return parts


def suggest_bump(local: str, online: str) -> str:
    """按「本地相对线上改了多少」给出 bump 级别建议。

    判定口径（保守、不猜意图）：
    - 只动最后一段 → 建议 patch（修 bug、小改动）
    - 动了倒数第二段 → 建议 minor（新增功能、行为变化）
    - 动了主版本段 → 建议 major（不兼容改动）
    """
    lp, op = parse_parts(local), parse_parts(online)
    # 补齐位数后比较，避免 0.2 与 0.2.0 被判成不同段
    width = max(len(lp), len(op), 3)
    lp += [0] * (width - len(lp))
    op += [0] * (width - len(op))
    changed = [i for i in range(width) if lp[i] != op[i]]
    if not changed:
        return "无变化"
    first = min(changed)
    if first == 0:
        return "major（主版本段变化）"
    if first == 1:
        return "minor（次版本段变化，通常对应新增功能 / 行为变化）"
    return "patch（仅修订号变化，通常对应修复与小改动）"


# ── 校验主流程 ──────────────────────────────────────────────

def check_one(local: str, online: str | None) -> tuple[str, str]:
    """校验单个版本号，返回 (等级, 说明文本)。

    等级：
    - ``error``：必须先修，否则发布会出问题（倒挂 / 未 bump / 未设置）
    - ``ok``：可以发布
    - ``skip``：线上版本未知，无法比对（不判失败）
    """
    if not local:
        return "error", "本地未设置版本号"
    if not online:
        # 线上读不到：无法判定是否跳版本，只做格式提示，不判失败
        return "skip", f"本地 {local}，线上版本未知（跳过比对）"

    if local == online:
        return "error", (
            f"本地 {local} **等于**线上 {online}：未 bump 版本号。"
            f"发布会产生重复版本 / 覆盖同名资产，请先升级版本号"
        )
    if version_greater(online, local):
        return "error", (
            f"本地 {local} **低于**线上 {online}：版本倒挂。"
            f"请先把本地版本号提到高于 {online}"
        )
    return "ok", (
        f"本地 {local} > 线上 {online} ✓　"
        f"差异定位：{suggest_bump(local, online)}"
    )


def run(mode: str, warn_only: bool, offline: bool, emit_pending: str | None = None) -> int:
    """执行校验。返回进程退出码。"""
    failures: list[str] = []

    if mode == "app":
        local = read_app_version()
        print(f"[app] 本地版本: {local}")
        online = None if offline else fetch_app_online_version()
        if online is None:
            # 网络失败或 --offline 时退到本地 git tag，避免完全无法判定
            tag_latest = fetch_online_from_git_tags()
            if tag_latest:
                reason = "--offline 模式" if offline else "线上读不到"
                print(f"[app] {reason}，改用本地 git tag 最高版本: {tag_latest}")
                online = tag_latest
        level, msg = check_one(local, online)
        print(f"       {msg}")
        if level == "error":
            failures.append(f"app: {msg}")
        # 同步检查 pyproject.toml，避免两处版本号漂移
        pyproject = ROOT_DIR / "pyproject.toml"
        if pyproject.exists():
            match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text("utf-8"), re.M)
            if match and match.group(1) != local:
                print(
                    f"       警告: pyproject.toml 版本为 {match.group(1)}，"
                    f"与 akm/__init__.py 的 {local} 不一致"
                )
        return _finish(failures, warn_only)

    # 插件
    local_versions = read_plugin_versions()
    if not local_versions:
        print("错误: plugins/ 下未找到任何 plugin.json")
        return 1
    print(f"[plugins] 本地共 {len(local_versions)} 个插件")
    online_versions = None if offline else fetch_plugins_online_versions()
    if online_versions is None:
        print("[plugins] 线上版本未知（网络失败或未发布过），只做本地格式检查")
        online_versions = {}

    # 索引最后一次提交：用于判断「插件改过但版本号没动」
    index_commit = last_index_commit()
    pending: list[str] = []
    for name, local in sorted(local_versions.items()):
        online = online_versions.get(name)
        level, msg = check_one(local, online)
        if online is None:
            # 新插件首次发布：没有线上版本，属于正常，需要打包
            print(f"  {name:26s} 本地 {local:8s} 线上 {'未发布':8s} 首次发布，正常")
            pending.append(name)
            continue
        # 版本号与线上相同，但要看这个插件在索引发布之后有没有改过代码：
        # 没改 → 本次不需要重新发布（跳过，不算问题）
        # 改过 → 必须 bump（否则 zip 内容变了、版本号没变，已安装用户永远收不到）
        if level == "error" and local == online and not plugin_changed_since(index_commit, name):
            print(f"  {name:26s} 本地 {local:8s} 线上 {online:8s} 未改动，本次无需发布")
            continue
        print(f"  {name:26s} 本地 {local:8s} 线上 {online:8s} {msg}")
        if level == "error":
            failures.append(f"{name}: {msg}")
        else:
            pending.append(name)

    # --emit-pending：把需要发布的插件名逐行写进文件，供 publish_plugins.sh 使用
    if emit_pending:
        Path(emit_pending).write_text("\n".join(pending) + ("\n" if pending else ""), "utf-8")
        print(f"\n需要发布的插件（{len(pending)} 个）: {' '.join(pending) or '无'}")
    return _finish(failures, warn_only)


def last_index_commit() -> str:
    """插件市场索引文件最后一次提交的 commit hash（空串表示无历史）。"""
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", "plugins/plugins.json"],
            capture_output=True, text=True, timeout=10, check=False, cwd=ROOT_DIR,
        )
        return proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def plugin_changed_since(commit: str, name: str) -> bool:
    """插件目录在 ``commit`` 之后是否改过。

    无法判断时（没有 git、没有索引历史、命令失败）保守返回 True——
    宁可要求用户显式确认「这个插件真的没动」，也不要静默放过一个
    「内容变了但版本号没变」的包。
    """
    if not commit:
        return True
    try:
        proc = subprocess.run(
            ["git", "diff", "--quiet", commit, "--", f"plugins/{name}"],
            capture_output=True, text=True, timeout=10, check=False, cwd=ROOT_DIR,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return True
    # git diff --quiet：0 = 无差异，1 = 有差异，其它 = 出错
    return proc.returncode != 0


def _finish(failures: list[str], warn_only: bool) -> int:
    """统一收尾：输出结论并返回退出码。"""
    print()
    if not failures:
        print("✓ 版本号校验通过")
        return 0
    if warn_only:
        print(f"⚠ 发现 {len(failures)} 处问题（--warn-only，仅提示不失败）：")
        for item in failures:
            print(f"  - {item}")
        return 0
    print(f"✗ 版本号校验失败（{len(failures)} 处）：")
    for item in failures:
        print(f"  - {item}")
    print()
    print("提示：按功能更新大小决定 bump 级别——")
    print("  修 bug / 小改动 → patch（最后一段 +1，如 0.1.59 → 0.1.60）")
    print("  新增功能 / 行为变化 → minor（中间段 +1，如 0.1.59 → 0.2.0）")
    print("  不兼容改动 → major（第一段 +1）")
    print("改完同步：akm/__init__.py、pyproject.toml、README.md、uv.lock（如依赖变动）")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="发布前校验版本号，防止跳版本号与版本倒挂",
    )
    parser.add_argument("mode", choices=["app", "plugins"], help="校验应用还是插件")
    parser.add_argument(
        "--warn-only", action="store_true",
        help="只提示不失败（本地开发时用，发布时不要加）",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="跳过网络查询，只用本地 git tag 判断",
    )
    parser.add_argument(
        "--emit-pending", metavar="FILE", default=None,
        help="把需要发布的插件名逐行写入 FILE（供 publish_plugins.sh 使用）",
    )
    args = parser.parse_args()
    return run(args.mode, args.warn_only, args.offline, args.emit_pending)


if __name__ == "__main__":
    sys.exit(main())
