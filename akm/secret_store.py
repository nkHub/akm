"""主密钥存放层 — 文件 / macOS Keychain 后端、权限加固、迁移与轮换原语。

本模块只负责"密钥放在哪里、权限对不对、怎么搬"，不参与加解密本身
（加解密在 ``akm.crypto``，令牌格式与 Fernet 互通，保持不变）。

## 密钥材料模型

- **当前密钥（current）**：所有新加密都用它；
- **历史密钥（previous）**：只用于解密，来自上一次轮换（默认只保留最近一次，见 ``MAX_PREVIOUS``）；
- 读取顺序由 ``akm.crypto._read_or_create_keys`` 决定：
  ``AKM_SECRET_KEY``（内联，只读）→ ``secret_backend=keychain`` 且可用时的钥匙串 →
  文件后端（``AKM_SECRET_FILE`` / ``<SECRET_DIR>/secret.key``）→ 遗留路径 ``~/.akm/secret.key``。

## 后端的取舍（实测结论，见 docs/design/key-custody.md）

macOS 钥匙串有两条路：

1. **Data Protection Keychain**（``kSecUseDataProtectionKeychain``）：需要 keychain-access-group
   权限，未签名/无 entitlement 的进程会拿到 ``errSecMissingEntitlement``（-34018），
   实测本项目的运行方式不可用，因此本模块不使用它；
2. **Legacy Keychain**（默认路径）：实测可用，且条目 ACL 归属创建它的应用，
   其他进程读取需要用户授权——这是相对 0600 文件的实际增益（同用户任意进程读文件是静默的）。

代价：应用二进制变化（自更新重新签名）后，首次读取可能弹一次系统授权框；
用户的拒绝/超时会表现为读取失败，此时调用方回退到文件后端，功能不受影响
（这也是 ``secret_backend`` 默认保持 ``file`` 的原因）。

## 权限策略

- 密钥文件 0600、密钥目录 0700，创建与读取时都会加固（读取时自愈，失败只告警不阻断）；
- 加固目录时跳过用户家目录本身，避免误改用户目录权限；
- 只处理密钥文件本身：不动数据目录里用户手工备份的副本（例如 ``secret.key.zip``）。
"""

from __future__ import annotations

import base64
import logging
import os
import stat
import sys
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────

BACKEND_FILE = "file"
BACKEND_KEYCHAIN = "keychain"
SUPPORTED_BACKENDS = (BACKEND_FILE, BACKEND_KEYCHAIN)

SECRET_FILE_NAME = "secret.key"
PREVIOUS_PREFIX = "secret.key.previous."

KEYCHAIN_SERVICE = "ai-key-manager"
KEYCHAIN_ACCOUNT = "master-key"
KEYCHAIN_PREVIOUS_PREFIX = "master-key.previous."
KEYCHAIN_PROBE_ACCOUNT = "__probe__"

FILE_MODE = 0o600
DIR_MODE = 0o700

# 轮换后保留的历史密钥个数（只保留最近一次，避免历史密钥无限堆积）
MAX_PREVIOUS = 1

_KEY_SIZE = 32


class SecretStoreError(Exception):
    """密钥存放层错误（路径不可写、既有密钥损坏、钥匙串不可用等）"""


@dataclass
class KeyMaterial:
    """一次读取得到的密钥材料。"""

    keys: list[bytes]                       # [当前, 历史...]
    source: str                             # 人类可读来源
    notes: list[str] = field(default_factory=list)   # 本次发生的迁移动作

    @property
    def current(self) -> bytes:
        return self.keys[0]

    @property
    def previous(self) -> list[bytes]:
        return self.keys[1:]


def generate_key() -> bytes:
    """生成与 ``Fernet.generate_key()`` 等价的 urlsafe base64 密钥（32 字节随机数）"""
    return base64.urlsafe_b64encode(os.urandom(_KEY_SIZE))


def validate_key(raw: bytes | str) -> bytes:
    """校验密钥长度（32 字节 urlsafe base64），返回规范化后的 bytes。"""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    raw = raw.strip()
    try:
        decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except Exception as exc:  # noqa: BLE001
        raise SecretStoreError(f"主密钥不是合法的 urlsafe base64: {exc}") from exc
    if len(decoded) != _KEY_SIZE:
        raise SecretStoreError(f"主密钥必须是 32 字节，实际 {len(decoded)} 字节")
    return raw


# ── 后端选择 ────────────────────────────────────────────────

def effective_backend() -> str:
    """解析当前生效的密钥存放后端。

    优先级：``AKM_SECRET_BACKEND`` 环境变量 → ``config.json`` 的 ``secret_backend`` →
    ``file``。配置读取失败一律回退 ``file``，保证密钥加载不因配置异常而失败。
    """
    env = (os.environ.get("AKM_SECRET_BACKEND") or "").strip().lower()
    if env in SUPPORTED_BACKENDS:
        return env
    try:
        from akm.config import load_config

        value = str(load_config().get("secret_backend", "") or "").strip().lower()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[secret] 读取 secret_backend 配置失败，回退 file: %s", exc)
        value = ""
    return value if value in SUPPORTED_BACKENDS else BACKEND_FILE


# ── 权限加固 ────────────────────────────────────────────────

_warned: set[str] = set()


def _warn_once(key: str, message: str, *args) -> None:
    """同一 key 只告警一次，避免每次读取都刷日志。"""
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message, *args)


def harden_dir(path: str) -> bool:
    """把密钥目录收紧到 0700；跳过用户家目录本身。返回是否实际改动。"""
    if not path:
        return False
    real = os.path.realpath(path)
    if real == os.path.realpath(os.path.expanduser("~")):
        return False
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode == DIR_MODE:
            return False
        os.chmod(path, DIR_MODE)
        logger.info("[secret] 已把密钥目录权限收紧为 0700: %s（原 %o）", path, mode)
        return True
    except OSError as exc:
        _warn_once(f"dir:{path}", "[secret] 无法收紧目录权限 %s: %s", path, exc)
        return False


def harden_file(path: str) -> bool:
    """把密钥文件收紧到 0600（读取时自愈）。返回是否实际改动。"""
    if not path:
        return False
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode == FILE_MODE:
            return False
        os.chmod(path, FILE_MODE)
        logger.warning(
            "[secret] 检测到主密钥文件权限过宽（%o），已收紧为 0600: %s", mode, path
        )
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        _warn_once(f"file:{path}", "[secret] 无法收紧密钥文件权限 %s: %s", path, exc)
        return False


# ── 文件后端 ────────────────────────────────────────────────

def _read_one(path: str) -> bytes | None:
    """读取并校验单个密钥文件；文件不存在返回 None，内容非法抛 SecretStoreError。"""
    if not path or not os.path.exists(path):
        return None
    harden_dir(os.path.dirname(path))
    harden_file(path)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise SecretStoreError(f"读取主密钥文件失败 {path}: {exc}") from exc
    if not raw.strip():
        raise SecretStoreError(f"主密钥文件为空: {path}")
    try:
        return validate_key(raw)
    except SecretStoreError as exc:
        # 关键安全语义：既有密钥文件损坏时绝不生成新密钥（否则已存数据永久不可解）
        raise SecretStoreError(f"主密钥文件内容非法，拒绝覆盖（{path}）：{exc}") from exc


def previous_files(primary_path: str) -> list[tuple[str, bytes]]:
    """返回主密钥目录下的历史密钥文件 ``[(path, key), ...]``，按时间倒序（新的在前）。"""
    directory = os.path.dirname(primary_path)
    if not directory or not os.path.isdir(directory):
        return []
    try:
        names = [
            name for name in os.listdir(directory)
            if name.startswith(PREVIOUS_PREFIX)
        ]
    except OSError:
        return []
    names.sort(reverse=True)   # 文件名后缀为时间戳，倒序即最新在前
    result: list[tuple[str, bytes]] = []
    for name in names:
        path = os.path.join(directory, name)
        try:
            key = _read_one(path)
        except SecretStoreError as exc:
            logger.warning("[secret] 跳过损坏的历史密钥文件 %s: %s", path, exc)
            continue
        if key:
            result.append((path, key))
    return result


def read_file_keys(primary_path: str, legacy_path: str | None = None) -> KeyMaterial | None:
    """读取文件后端密钥材料。

    - 主路径存在：用它（并带上目录下的历史密钥）；
    - 主路径不存在但遗留路径存在：**复制**到主路径完成迁移，**保留**遗留文件；
    - 都不存在：返回 None（由调用方决定是否生成新密钥）。
    """
    current = _read_one(primary_path)
    if current:
        previous = [key for _, key in previous_files(primary_path)][:MAX_PREVIOUS]
        return KeyMaterial([current, *previous], "file", [])

    legacy_current = _read_one(legacy_path) if legacy_path else None
    if not legacy_current:
        return None

    notes: list[str] = []
    try:
        written = _write_secret_file(primary_path, legacy_current)
        notes.append(f"已从遗留路径迁移到 {written}（原文件保留）")
        logger.info("[secret] 主密钥已迁移到新路径 %s（遗留文件保留在 %s）", written, legacy_path)
    except SecretStoreError as exc:
        # 迁移失败不阻断：继续用遗留路径的密钥
        _warn_once(
            "migrate", "[secret] 迁移主密钥到 %s 失败，继续使用遗留路径: %s", primary_path, exc
        )
        notes.append(f"迁移失败（继续使用遗留路径）：{exc}")
    return KeyMaterial([legacy_current], f"legacy({legacy_path})", notes)


def _write_secret_file(path: str, key: bytes) -> str:
    """以 0600 原子写入密钥文件（临时文件 O_EXCL + rename，避免权限竞态与半截文件）。"""
    key = validate_key(key)
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
    except OSError as exc:
        raise SecretStoreError(f"无法创建密钥目录 {directory}: {exc}") from exc
    harden_dir(directory)

    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    except FileExistsError:
        os.unlink(tmp_path)
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    except OSError as exc:
        raise SecretStoreError(f"无法写入密钥文件 {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise SecretStoreError(f"写入密钥文件失败 {path}: {exc}") from exc
    harden_file(path)
    return path


def write_file_keys(primary_path: str, current: bytes, previous: list[bytes]) -> list[str]:
    """写入当前密钥与历史密钥（历史只保留 ``MAX_PREVIOUS`` 个，多余的清掉）。返回写入路径列表。"""
    previous = previous[:MAX_PREVIOUS]
    written = [_write_secret_file(primary_path, current)]

    existing = {path: key for path, key in previous_files(primary_path)}
    keep_paths: set[str] = set()
    stamp = int(time.time())
    for index, key in enumerate(previous):
        # 复用已有的历史文件，避免同一把密钥反复换名
        matched = next((path for path, old in existing.items() if old == key), None)
        if matched:
            keep_paths.add(matched)
            continue
        path = _write_secret_file(
            os.path.join(os.path.dirname(primary_path), f"{PREVIOUS_PREFIX}{stamp + index}"),
            key,
        )
        keep_paths.add(path)

    for path in existing:
        if path not in keep_paths:
            try:
                os.unlink(path)
                logger.info("[secret] 已清理过期历史密钥文件 %s", path)
            except OSError as exc:
                _warn_once(f"unlink:{path}", "[secret] 清理历史密钥文件失败 %s: %s", path, exc)
    written.extend(sorted(keep_paths))
    return written


def file_key_entries(primary_path: str) -> list[tuple[str, bytes | None]]:
    """列出磁盘上实际存在的密钥文件 ``[(path, key)]``（当前 + 历史）。

    读取失败或内容非法的文件返回 ``key=None``——清理流程必须把这种情况视为
    "无法确认"，而不是当作不存在。
    """
    entries: list[tuple[str, bytes | None]] = []
    if primary_path and os.path.exists(primary_path):
        try:
            entries.append((primary_path, _read_one(primary_path)))
        except SecretStoreError as exc:
            logger.warning("[secret] 密钥文件无法校验，按不可确认处理 %s: %s", primary_path, exc)
            entries.append((primary_path, None))
    for path, key in previous_files(primary_path):
        entries.append((path, key))
    return entries


def delete_file_keys(primary_path: str) -> list[str]:
    """删除当前密钥文件与目录下的历史密钥文件（仅用于 ``--purge-file``）。返回被删除的路径。"""
    removed: list[str] = []
    for path in [primary_path, *[p for p, _ in previous_files(primary_path)]]:
        if not path or not os.path.exists(path):
            continue
        try:
            os.unlink(path)
            removed.append(path)
        except OSError as exc:
            _warn_once(f"purge:{path}", "[secret] 删除密钥文件失败 %s: %s", path, exc)
    return removed


def file_status(primary_path: str, legacy_path: str | None = None) -> dict:
    """文件后端的可观测状态（供 ``akm secret status`` 与排障使用）。"""
    def _describe(path: str | None) -> dict:
        if not path:
            return {"path": "", "exists": False, "mode": "", "previous": 0}
        exists = os.path.exists(path)
        mode = ""
        if exists:
            try:
                mode = oct(stat.S_IMODE(os.stat(path).st_mode))
            except OSError:
                mode = "?"
        return {
            "path": path,
            "exists": exists,
            "mode": mode,
            "previous": len(previous_files(path)) if exists else 0,
        }

    directory = os.path.dirname(primary_path) if primary_path else ""
    dir_mode = ""
    if directory and os.path.isdir(directory):
        try:
            dir_mode = oct(stat.S_IMODE(os.stat(directory).st_mode))
        except OSError:
            dir_mode = "?"
    return {
        "primary": _describe(primary_path),
        "legacy": _describe(legacy_path),
        "primary_dir": directory,
        "primary_dir_mode": dir_mode,
    }


# ── macOS 钥匙串后端（legacy keychain，ctypes 直连 Security.framework）──

class KeychainError(SecretStoreError):
    """钥匙串操作失败"""


_STATUS_MESSAGES = {
    0: "成功",
    -25293: "认证失败（errSecAuthFailed）",
    -25300: "条目不存在（errSecItemNotFound）",
    -25308: "当前会话不允许交互（errSecInteractionNotAllowed）",
    -128: "用户取消（errSecUserCanceled）",
    -34018: "缺少 entitlement（errSecMissingEntitlement）",
}


class _KeychainBridge:
    """Security.framework 的最小 ctypes 封装（用完即释放 CF 对象）。

    只用 legacy keychain：Data Protection Keychain 在无 entitlement 的进程上返回 -34018，实测不可用。
    """

    def __init__(self) -> None:
        import ctypes
        import ctypes.util

        from ctypes import POINTER, byref, c_char_p, c_int32, c_long, c_uint32, c_void_p

        security_path = (ctypes.util.find_library("Security")
                         or "/System/Library/Frameworks/Security.framework/Security")
        cf_path = (ctypes.util.find_library("CoreFoundation")
                   or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._ctypes = ctypes
        self._c_void_p = c_void_p
        self._byref = byref
        self.sec = ctypes.CDLL(security_path)
        self.cf = ctypes.CDLL(cf_path)

        self.cf.CFStringCreateWithCString.restype = c_void_p
        self.cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        self.cf.CFDataCreate.restype = c_void_p
        self.cf.CFDataCreate.argtypes = [c_void_p, c_char_p, c_long]
        self.cf.CFDataGetLength.restype = c_long
        self.cf.CFDataGetLength.argtypes = [c_void_p]
        self.cf.CFDataGetBytePtr.restype = c_void_p
        self.cf.CFDataGetBytePtr.argtypes = [c_void_p]
        self.cf.CFDictionaryCreate.restype = c_void_p
        self.cf.CFDictionaryCreate.argtypes = [
            c_void_p, POINTER(c_void_p), POINTER(c_void_p), c_long, c_void_p, c_void_p,
        ]
        self.cf.CFRelease.restype = None
        self.cf.CFRelease.argtypes = [c_void_p]

        self.sec.SecItemAdd.restype = c_int32
        self.sec.SecItemAdd.argtypes = [c_void_p, c_void_p]
        self.sec.SecItemCopyMatching.restype = c_int32
        self.sec.SecItemCopyMatching.argtypes = [c_void_p, POINTER(c_void_p)]
        self.sec.SecItemUpdate.restype = c_int32
        self.sec.SecItemUpdate.argtypes = [c_void_p, c_void_p]
        self.sec.SecItemDelete.restype = c_int32
        self.sec.SecItemDelete.argtypes = [c_void_p]

        self._utf8 = 0x08000100

    # -- CF 原语 --

    def _sym(self, name: str):
        return self._c_void_p.in_dll(self.sec, name).value

    def _cfstr(self, value: str):
        return self.cf.CFStringCreateWithCString(None, value.encode(), self._utf8)

    def _cfdata(self, value: bytes):
        return self.cf.CFDataCreate(None, value, len(value))

    def _true(self):
        return self._c_void_p.in_dll(self.cf, "kCFBooleanTrue")

    def _dict(self, items):
        n = len(items)
        keys = (self._c_void_p * n)(*[k for k, _ in items])
        vals = (self._c_void_p * n)(*[v for _, v in items])
        return self.cf.CFDictionaryCreate(
            None, keys, vals, n,
            self._c_void_p.in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks"),
            self._c_void_p.in_dll(self.cf, "kCFTypeDictionaryValueCallBacks"),
        )

    def _release_all(self, *objects) -> None:
        for obj in objects:
            if obj:
                try:
                    self.cf.CFRelease(obj)
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def status_text(status: int) -> str:
        return _STATUS_MESSAGES.get(status, f"OSStatus {status}")

    # -- 条目操作 --

    def _query(self, account: str, *, return_data: bool):
        items = [
            (self._sym("kSecClass"), self._sym("kSecClassGenericPassword")),
            (self._sym("kSecAttrService"), self._cfstr(KEYCHAIN_SERVICE)),
            (self._sym("kSecAttrAccount"), self._cfstr(account)),
        ]
        if return_data:
            items.append((self._sym("kSecReturnData"), self._true()))
            items.append((self._sym("kSecMatchLimit"), self._sym("kSecMatchLimitOne")))
        return self._dict(items)

    def read(self, account: str) -> bytes | None:
        """读取单个条目；不存在返回 None，其他错误抛 KeychainError。"""
        import ctypes

        query = self._query(account, return_data=True)
        out = self._c_void_p()
        status = self.sec.SecItemCopyMatching(query, self._byref(out))
        self._release_all(query)
        if status == -25300:
            return None
        if status != 0:
            raise KeychainError(f"读取钥匙串条目失败（{self.status_text(status)}）")
        if not out.value:
            return None
        try:
            length = self.cf.CFDataGetLength(out)
            raw = ctypes.string_at(self.cf.CFDataGetBytePtr(out), length)
        finally:
            self._release_all(out)
        return raw

    def write(self, account: str, value: bytes) -> None:
        """写入条目：已存在则更新，不存在则新增。"""
        query = self._query(account, return_data=False)
        attrs = self._dict([(self._sym("kSecValueData"), self._cfdata(value))])
        status = self.sec.SecItemUpdate(query, attrs)
        self._release_all(attrs)
        if status == -25300:
            add = self._dict([
                (self._sym("kSecClass"), self._sym("kSecClassGenericPassword")),
                (self._sym("kSecAttrService"), self._cfstr(KEYCHAIN_SERVICE)),
                (self._sym("kSecAttrAccount"), self._cfstr(account)),
                (self._sym("kSecValueData"), self._cfdata(value)),
            ])
            status = self.sec.SecItemAdd(add, None)
            self._release_all(add)
        self._release_all(query)
        if status != 0:
            raise KeychainError(f"写入钥匙串条目失败（{self.status_text(status)}）")

    def delete(self, account: str) -> bool:
        """删除条目；不存在返回 False。"""
        query = self._query(account, return_data=False)
        status = self.sec.SecItemDelete(query)
        self._release_all(query)
        if status in (0, -25300):
            return status == 0
        raise KeychainError(f"删除钥匙串条目失败（{self.status_text(status)}）")

    def accounts(self) -> list[str]:
        """列出本服务下的所有 account（用于找历史密钥）。"""
        import ctypes

        query = self._dict([
            (self._sym("kSecClass"), self._sym("kSecClassGenericPassword")),
            (self._sym("kSecAttrService"), self._cfstr(KEYCHAIN_SERVICE)),
            (self._sym("kSecReturnAttributes"), self._true()),
            (self._sym("kSecMatchLimit"), self._sym("kSecMatchLimitAll")),
        ])
        out = self._c_void_p()
        status = self.sec.SecItemCopyMatching(query, self._byref(out))
        self._release_all(query)
        if status == -25300:
            return []
        if status != 0:
            raise KeychainError(f"枚举钥匙串条目失败（{self.status_text(status)}）")
        if not out.value:
            return []
        try:
            count = self._cfarray_count(out)
            names: list[str] = []
            for index in range(count):
                item = self._cfarray_item(out, index)
                account = self._cfdict_account(item)
                if account:
                    names.append(account)
            return names
        finally:
            self._release_all(out)

    def _cfarray_count(self, array) -> int:
        self.cf.CFArrayGetCount.restype = self._ctypes.c_long
        self.cf.CFArrayGetCount.argtypes = [self._c_void_p]
        return int(self.cf.CFArrayGetCount(array))

    def _cfarray_item(self, array, index: int):
        self.cf.CFArrayGetValueAtIndex.restype = self._c_void_p
        self.cf.CFArrayGetValueAtIndex.argtypes = [self._c_void_p, self._ctypes.c_long]
        return self.cf.CFArrayGetValueAtIndex(array, index)

    def _cfdict_account(self, item) -> str:
        """从条目属性字典里取 kSecAttrAccount 字符串。"""
        import ctypes

        self.cf.CFDictionaryGetValue.restype = self._c_void_p
        self.cf.CFDictionaryGetValue.argtypes = [self._c_void_p, self._c_void_p]
        self.cf.CFStringGetCStringPtr.restype = self._ctypes.c_char_p
        self.cf.CFStringGetCStringPtr.argtypes = [self._c_void_p, self._ctypes.c_uint32]
        key = self._cfstr("acct")
        try:
            value = self.cf.CFDictionaryGetValue(item, key)
        finally:
            self._release_all(key)
        if not value:
            return ""
        ptr = self.cf.CFStringGetCStringPtr(value, self._utf8)
        if ptr:
            return ptr.decode("utf-8", "replace")
        # 兜底：CFStringGetCString 到缓冲区
        buf = ctypes.create_string_buffer(256)
        self.cf.CFStringGetCString.restype = self._ctypes.c_bool
        self.cf.CFStringGetCString.argtypes = [self._c_void_p, ctypes.c_char_p,
                                               self._ctypes.c_long, self._ctypes.c_uint32]
        if self.cf.CFStringGetCString(value, buf, len(buf), self._utf8):
            return buf.value.decode("utf-8", "replace")
        return ""


_bridge = None


def _get_bridge() -> "_KeychainBridge":
    global _bridge
    if _bridge is None:
        _bridge = _KeychainBridge()
    return _bridge


def keychain_supported() -> bool:
    """当前平台是否具备钥匙串后端（仅 macOS）。"""
    return sys.platform == "darwin"


def keychain_read() -> KeyMaterial | None:
    """读取当前密钥与历史密钥；没有任何条目返回 None。"""
    bridge = _get_bridge()
    current = bridge.read(KEYCHAIN_ACCOUNT)
    if current is None:
        return None
    keys = [validate_key(current)]
    try:
        others = [
            name for name in bridge.accounts()
            if name.startswith(KEYCHAIN_PREVIOUS_PREFIX)
        ]
    except KeychainError as exc:
        logger.warning("[secret] 枚举钥匙串历史密钥失败，仅使用当前密钥: %s", exc)
        others = []
    for name in sorted(others, reverse=True)[:MAX_PREVIOUS]:
        raw = bridge.read(name)
        if not raw:
            continue
        try:
            keys.append(validate_key(raw))
        except SecretStoreError as exc:
            logger.warning("[secret] 跳过非法的钥匙串历史密钥 %s: %s", name, exc)
    return KeyMaterial(keys, f"keychain({KEYCHAIN_SERVICE}/{KEYCHAIN_ACCOUNT})", [])


def keychain_write(current: bytes, previous: list[bytes]) -> list[str]:
    """写入当前密钥与历史密钥；清理超出 ``MAX_PREVIOUS`` 的历史条目。返回写入的 account 列表。"""
    bridge = _get_bridge()
    current = validate_key(current)
    previous = [validate_key(key) for key in previous[:MAX_PREVIOUS]]
    written = [KEYCHAIN_ACCOUNT]
    bridge.write(KEYCHAIN_ACCOUNT, current)

    existing = {
        name: raw for name in bridge.accounts()
        if name.startswith(KEYCHAIN_PREVIOUS_PREFIX)
        for raw in [bridge.read(name)]
        if raw
    }
    keep: set[str] = set()
    stamp = int(time.time())
    for index, key in enumerate(previous):
        matched = next((name for name, old in existing.items() if old == key), None)
        if matched:
            keep.add(matched)
            continue
        name = f"{KEYCHAIN_PREVIOUS_PREFIX}{stamp + index}"
        bridge.write(name, key)
        keep.add(name)
    for name in existing:
        if name not in keep:
            try:
                bridge.delete(name)
                logger.info("[secret] 已清理过期历史钥匙串条目 %s", name)
            except KeychainError as exc:
                logger.warning("[secret] 清理历史钥匙串条目失败 %s: %s", name, exc)
    return written + sorted(keep)


def keychain_delete() -> list[str]:
    """删除本服务下的当前密钥与历史密钥条目。返回被删除的 account 列表。"""
    bridge = _get_bridge()
    removed: list[str] = []
    names = [KEYCHAIN_ACCOUNT]
    try:
        names += [n for n in bridge.accounts() if n.startswith(KEYCHAIN_PREVIOUS_PREFIX)]
    except KeychainError:
        pass
    for name in names:
        try:
            if bridge.delete(name):
                removed.append(name)
        except KeychainError as exc:
            logger.warning("[secret] 删除钥匙串条目失败 %s: %s", name, exc)
    return removed


def keychain_probe() -> tuple[bool, str]:
    """自检：往钥匙串写一个探针条目再读回删除，判断后端是否真正可用。

    返回 ``(是否可用, 说明)``。探针使用独立的 account，不触碰真实密钥。
    """
    if not keychain_supported():
        return False, "当前平台不是 macOS"
    try:
        bridge = _get_bridge()
    except Exception as exc:  # noqa: BLE001
        return False, f"加载 Security.framework 失败: {exc}"
    payload = os.urandom(16)
    try:
        bridge.write(KEYCHAIN_PROBE_ACCOUNT, payload)
    except KeychainError as exc:
        return False, f"写入探针失败: {exc}"
    try:
        read_back = bridge.read(KEYCHAIN_PROBE_ACCOUNT)
    except KeychainError as exc:
        return False, f"读回探针失败: {exc}"
    finally:
        try:
            bridge.delete(KEYCHAIN_PROBE_ACCOUNT)
        except KeychainError:
            pass
    if read_back != payload:
        return False, "读回的探针内容不一致"
    return True, "钥匙串可用（legacy keychain，条目 ACL 归属本应用）"


def keychain_status() -> dict:
    """钥匙串后端的可观测状态（不写探针，只读）。"""
    if not keychain_supported():
        return {"supported": False, "service": KEYCHAIN_SERVICE, "accounts": [], "note": "非 macOS"}
    try:
        bridge = _get_bridge()
        accounts = bridge.accounts()
    except Exception as exc:  # noqa: BLE001
        return {"supported": True, "service": KEYCHAIN_SERVICE, "accounts": [],
                "note": f"读取失败: {exc}"}
    return {"supported": True, "service": KEYCHAIN_SERVICE, "accounts": accounts, "note": ""}
