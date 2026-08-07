"""会话持久化层：把多轮对话历史与运行参数保存到磁盘。

服务端 `/v1/agent` 无状态，messages 需要每次请求全量回传。本模块把一次
Agent 会话（消息历史 + model + workspace_root 等）持久化到
``~/.akm/agent_sessions/*.json``，供 `akm_load_session` / `akm_list_sessions`
工具以及客户端会话回顾使用。

会话文件格式（JSON）::

    {
        "name": "20260805-142301",
        "created_at": "2026-08-05T14:23:01",
        "updated_at": "2026-08-05T14:30:12",
        "model": "",
        "workspace_root": "/path/to/workspace",
        "api_path": "chat/completions",
        "instructions": "",
        "messages": [...]
    }

只依赖标准库，目录可注入便于测试。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import akm.config as config_module

# 会话文件默认扩展名
_SESSION_EXT = ".json"


def _default_session_dir() -> Path:
    """默认会话目录：与配置目录同级的 agent_sessions 子目录。"""
    return Path(config_module.CONFIG_DIR) / "agent_sessions"


class SessionStore:
    """Agent 会话的磁盘存取。

    Args:
        base_dir: 会话目录绝对路径；缺省使用 ``~/.akm/agent_sessions``。
            CLI 调用时可用当前目录作为 workspace_root，因此会话目录放在
            全局配置目录而非当前项目内。
    """

    def __init__(self, base_dir: str | Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else _default_session_dir()

    def _path(self, name: str) -> Path:
        """根据会话名拼出完整文件路径。

        会话名格式 ``YYYYMMDD-HHMMSS``（``next_name()`` 生成的默认名）
        会自动归入 ``YYYY-MM-DD/`` 日期子目录；不符合该格式的会话名回退到
        旧版扁平路径（向后兼容老数据）。校验会话名不含路径分隔符。"""
        if not name or name != os.path.basename(name) or name in (".", ".."):
            raise ValueError(f"非法的会话名: {name!r}")
        # 提取日期前缀：YYYYMMDD-HHMMSS 或 YYYYMMDD-HHMMSS-N
        _date_match = re.match(r'^(\d{4})(\d{2})(\d{2})-\d{6}(?:-\d+)?$', name)
        if _date_match:
            y, m, d = _date_match.groups()
            return self.base_dir / f"{y}-{m}-{d}" / f"{name}{_SESSION_EXT}"
        return self.base_dir / f"{name}{_SESSION_EXT}"

    def _ensure_dir(self) -> None:
        """确保会话目录存在（创建时带 0700 权限）。"""
        self.base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def list(self) -> list[dict[str, Any]]:
        """列出全部会话的元信息（不含 messages），按更新时间倒序。

        同时扫描日期子目录下的文件与旧版扁平文件（递归 glob）。"""
        if not self.base_dir.is_dir():
            return []
        sessions: list[dict[str, Any]] = []
        for path in self.base_dir.glob(f"**/*{_SESSION_EXT}"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            sessions.append({
                "name": str(data.get("name") or path.stem),
                "created_at": str(data.get("created_at") or ""),
                "updated_at": str(data.get("updated_at") or ""),
                "message_count": len(data.get("messages") or []),
                "model": str(data.get("model") or ""),
            })
        sessions.sort(key=lambda item: item["updated_at"], reverse=True)
        return sessions

    def load(self, name: str) -> dict[str, Any] | None:
        """读取指定会话，文件不存在或损坏时返回 None。

        先按日期文件夹定位，再尝试旧版扁平路径（向后兼容老数据）。"""
        path = self._path(name)
        if not path.is_file():
            # 向后兼容：旧版扁平目录中挪过来的会话文件
            old_path = self.base_dir / f"{name}{_SESSION_EXT}"
            if old_path.is_file():
                path = old_path
            else:
                return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        data.setdefault("messages", [])
        return data

    def save(self, session: dict[str, Any]) -> None:
        """保存会话（自动补全 name / created_at / updated_at）。"""
        name = str(session.get("name") or "").strip()
        if not name:
            raise ValueError("会话缺少 name")
        self._ensure_dir()
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        data = dict(session)
        data["name"] = name
        data.setdefault("created_at", now)
        data["updated_at"] = now
        data.setdefault("messages", [])
        # 原子写入：先写临时文件再重命名，避免中途崩溃留下半截文件
        path = self._path(name)
        # 日期子目录（如 2026-08-06/）自动创建
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(f"{_SESSION_EXT}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, name: str) -> bool:
        """删除指定会话，返回是否确实删除。

        同时检查日期文件夹路径与旧版扁平路径。"""
        path = self._path(name)
        deleted = False
        for candidate in (path, self.base_dir / f"{name}{_SESSION_EXT}"):
            if candidate.is_file():
                try:
                    candidate.unlink()
                    deleted = True
                except OSError:
                    pass
        return deleted

    def next_name(self) -> str:
        """生成一个不冲突的会话名（时间戳 + 序号，线程安全）。

        同时检查日期文件夹路径与旧版扁平路径。"""
        with _NAME_LOCK:
            base = time.strftime("%Y%m%d-%H%M%S")
            candidate = base
            index = 1
            while self._path(candidate).is_file() or (
                self.base_dir / f"{candidate}{_SESSION_EXT}"
            ).is_file():
                index += 1
                candidate = f"{base}-{index}"
            return candidate


# 会话名生成用的全局锁，避免多个线程同时 new 会话撞名
_NAME_LOCK = threading.Lock()
