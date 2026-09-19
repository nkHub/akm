"""本地数据目录自动维护 — 更新包缓存清理与文本日志轮转。

`~/.akm` 会随使用持续累积可回收内容，历史上只有审计日志有保留策略
（`akm.audit.auto_cleanup_logs` 按 `log_retention_days` 清理），其余全靠用户手动删：

1. ``updates/``：每次自动更新都会下载一个 zip，并在替换 ``.app`` 前留一份旧版本备份；
   旧 zip 与旧备份从不清理，是数据目录里最大的一块占用。
2. 根目录下的 append-only 文本日志（``error.log`` / ``keys.log`` 等）：只追加、无轮转。

本模块提供 ``run_auto_maintenance()`` 作为统一维护入口，在服务启动与系统唤醒恢复时调用：

- 审计日志清理：始终执行（沿用既有行为与配置）；
- 更新包缓存清理：受 ``update_cache_cleanup`` 开关控制，默认开启；
- 文本日志轮转：受 ``text_log_rotation`` 开关控制，默认关闭。

两个开关都只处理 AKM 自己产生的派生数据：不会触碰 ``config.json`` / ``secret.key`` /
``akm.db`` / ``plugins/`` / ``agent_sessions/`` / ``markdown_kb/`` 等用户数据与插件目录。
"""

import logging
import os
import shutil
import time

logger = logging.getLogger("akm.cleanup")

# 轮转后旧日志的后缀（保留一代）
ROTATED_SUFFIX = ".1"

# 更新包缓存：修改时间在该秒数内的文件视为“正在使用”，一律跳过，
# 避免打断正在下载/正在替换的更新流程。
UPDATE_GRACE_SEC = 600

# 更新包文件名前缀（与 akm.menubar 下载时保持一致）
_UPDATE_ZIP_PREFIX = "AI Key Manager-"

# 默认单文件轮转阈值（MB）
_DEFAULT_LOG_FILE_MAX_MB = 5


def akm_home() -> str:
    """返回 AKM 数据目录：默认 ``~/.akm``，``AKM_DB_DIR`` 可覆盖（与数据库路径同源）。

    与 ``akm.db.DB_DIR`` 保持一致，便于测试把整个数据目录隔离到临时路径。
    """
    from akm.db import DB_DIR

    return os.path.expanduser(str(DB_DIR))


def updates_dir() -> str:
    """返回更新包缓存目录（``<数据目录>/updates``）。"""
    return os.path.join(akm_home(), "updates")


def _path_size(path: str) -> int:
    """统计文件或目录占用字节数（只读，不跟随符号链接）。"""
    if os.path.islink(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
        # 不进入符号链接目录，避免统计/删除越界
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
    return total


def _remove_path(path: str) -> None:
    """删除文件/目录；符号链接只删链接本身，不跟随。"""
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _backup_version(name: str) -> str:
    """从 ``AI Key Manager-<version>.app`` 解析版本号，解析失败返回空串。"""
    if not name.startswith(_UPDATE_ZIP_PREFIX):
        return ""
    rest = name[len(_UPDATE_ZIP_PREFIX):]
    if rest.endswith(".app"):
        rest = rest[: -len(".app")]
    return rest


def _is_fresh(path: str, grace_seconds: int) -> bool:
    """判断文件是否仍在宽限期内（可能在用，不清理）。"""
    try:
        return (time.time() - os.path.getmtime(path)) < grace_seconds
    except OSError:
        # 取不到 mtime 时保守跳过
        return True


def cleanup_update_cache(grace_seconds: int = UPDATE_GRACE_SEC) -> dict:
    """清理历史更新包缓存，只保留最新 zip 与可回滚的旧版本备份。

    保留规则（只扫描 ``updates/`` 与 ``updates/backups/`` 两层，不递归更深）：

    - ``*.zip``：两层合起来只保留修改时间最新的一个（正在下载的包必然最新），删除其余；
    - ``updates/backups/*.app``：保留当前运行版本 ``akm.__version__`` 对应的备份
      （更新失败时的回滚点）与最新备份，删除其余；
    - 修改时间在 ``grace_seconds`` 内的文件一律跳过，避免打断进行中的更新。

    返回 ``{"deleted": [...], "kept": [...], "freed_bytes": int}``。
    """
    root = updates_dir()
    result = {"deleted": [], "kept": [], "freed_bytes": 0}
    if not os.path.isdir(root):
        return result

    try:
        from akm import __version__ as current_version
    except Exception:  # pragma: no cover - 版本模块异常时退化为“只留最新”
        current_version = ""

    # 1) 历史 zip：只留最新一个（正常下载落在 updates/ 根下；backups/ 里如出现 zip
    #    一律视作历史残留，一并纳入扫描，避免这块残留永远清不掉）
    zips = []
    for scan_dir in (root, os.path.join(root, "backups")):
        try:
            entries = os.listdir(scan_dir)
        except OSError:
            continue
        for name in entries:
            if not name.endswith(".zip") or name.startswith("."):
                continue
            path = os.path.join(scan_dir, name)
            if os.path.isdir(path):
                continue
            try:
                zips.append((os.path.getmtime(path), path))
            except OSError:
                continue
    zips.sort(reverse=True)
    for idx, (_, path) in enumerate(zips):
        if idx == 0 or _is_fresh(path, grace_seconds):
            result["kept"].append(path)
            continue
        size = _path_size(path)
        try:
            _remove_path(path)
        except OSError as exc:
            logger.warning("清理旧更新包失败: %s (%s)", path, exc)
            result["kept"].append(path)
            continue
        result["deleted"].append(path)
        result["freed_bytes"] += size

    # 2) 旧版本 .app 备份：保留当前版本 + 最新一份
    backup_dir = os.path.join(root, "backups")
    backups = []
    try:
        backup_entries = os.listdir(backup_dir) if os.path.isdir(backup_dir) else []
    except OSError:
        backup_entries = []
    for name in backup_entries:
        if not name.endswith(".app") or name.startswith("."):
            continue
        path = os.path.join(backup_dir, name)
        try:
            backups.append((os.path.getmtime(path), path, _backup_version(name)))
        except OSError:
            continue
    backups.sort(reverse=True)
    # 最新备份始终保留（刚从旧版本升级过来时，它就是唯一的回滚点）；
    # 正在运行的版本若也留有备份，一并保留。
    keep_paths = {backups[0][1]} if backups else set()
    for _, path, version in backups:
        if version and current_version and version == current_version:
            keep_paths.add(path)
    for _, path, _version in backups:
        if path in keep_paths or _is_fresh(path, grace_seconds):
            result["kept"].append(path)
            continue
        size = _path_size(path)
        try:
            _remove_path(path)
        except OSError as exc:
            logger.warning("清理旧版本备份失败: %s (%s)", path, exc)
            result["kept"].append(path)
            continue
        result["deleted"].append(path)
        result["freed_bytes"] += size

    if result["deleted"]:
        logger.info(
            "更新包缓存清理完成: 删除 %d 项，释放 %.1f MB",
            len(result["deleted"]),
            result["freed_bytes"] / 1048576,
        )
    return result


def log_rotation_targets() -> list[str]:
    """返回参与轮转的日志：数据目录直属的 ``*.log``（不递归）。

    只处理 AKM 自己写在数据目录根下的 append-only 日志，因此不会进入
    ``agent_sessions/``、``markdown_kb/``、``plugins/`` 等用户或插件目录。
    """
    root = akm_home()
    if not os.path.isdir(root):
        return []
    targets = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".log") or name.startswith("."):
            continue
        path = os.path.join(root, name)
        if os.path.isfile(path) and not os.path.islink(path):
            targets.append(path)
    return targets


def rotate_text_logs(max_bytes: int) -> dict:
    """按大小轮转数据目录根下的 append-only 文本日志。

    超过 ``max_bytes`` 时把当前文件改名为 ``<name>.log.1``（覆盖上一代），
    写入方在下次追加时会自动重建当前文件（各日志均为“每次写入 open/close”，
    因此改名不影响正在运行的进程继续写日志）。

    返回 ``{"rotated": [...], "dropped_bytes": int, "freed_bytes": int}``。
    """
    result = {"rotated": [], "dropped_bytes": 0, "freed_bytes": 0}
    if max_bytes <= 0:
        return result
    for path in log_rotation_targets():
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size <= max_bytes:
            continue
        rotated = path + ROTATED_SUFFIX
        dropped = 0
        try:
            if os.path.exists(rotated):
                dropped = _path_size(rotated)
                _remove_path(rotated)
            os.rename(path, rotated)
        except OSError as exc:
            logger.warning("日志轮转失败: %s (%s)", path, exc)
            continue
        result["rotated"].append(path)
        result["dropped_bytes"] += dropped
        result["freed_bytes"] += dropped
    if result["rotated"]:
        logger.info(
            "文本日志轮转完成: %d 个文件，回收 %.1f MB",
            len(result["rotated"]),
            result["freed_bytes"] / 1048576,
        )
    return result


def _log_file_max_bytes() -> int:
    """读取 ``log_file_max_mb`` 配置并换算为字节。"""
    from akm.config import get as config_get

    try:
        mb = int(config_get("log_file_max_mb", _DEFAULT_LOG_FILE_MAX_MB) or _DEFAULT_LOG_FILE_MAX_MB)
    except (TypeError, ValueError):
        mb = _DEFAULT_LOG_FILE_MAX_MB
    return max(1, mb) * 1048576


def run_auto_maintenance() -> dict:
    """按配置执行一次完整维护：审计日志清理 + 更新包清理 + 文本日志轮转。

    在服务启动与系统唤醒恢复时调用，任何一步失败都只记日志、不影响其余步骤。
    返回各步骤的执行结果，便于测试与排障。
    """
    from akm.config import get as config_get

    result: dict = {"audit_logs": False, "update_cache": None, "text_logs": None}

    # 审计日志：沿用既有行为，始终执行（受 log_retention_days 控制）
    try:
        from akm.audit import auto_cleanup_logs

        result["audit_logs"] = auto_cleanup_logs()
    except Exception as exc:
        logger.warning("审计日志自动清理失败: %s", exc)

    # A. 更新包缓存：默认开启，仅显式设为 false 才关闭
    if config_get("update_cache_cleanup", True) is not False:
        try:
            result["update_cache"] = cleanup_update_cache()
        except Exception as exc:
            logger.warning("更新包缓存清理失败: %s", exc)

    # B. 文本日志轮转：默认关闭，仅显式设为 true 才开启
    if config_get("text_log_rotation", False) is True:
        try:
            result["text_logs"] = rotate_text_logs(_log_file_max_bytes())
        except Exception as exc:
            logger.warning("文本日志轮转失败: %s", exc)

    return result
