"""Markdown 知识库插件。

实现目标：
1. 管理本地 Markdown 文件；
2. 按“标题优先”策略切片；
3. 通过 AKM 本地代理调用 embedding / chat；
4. 维护一个本地 JSON 向量索引，先把完整闭环跑通。

说明：
当前仓库尚未声明成熟向量库存储依赖，因此这里先采用“本地 JSON + 向量缓存”的方式
实现第一版检索闭环。这样可以先验证插件 API、切片策略和 AKM 模型调用链路是否可用。
后续如果要切换到底层向量库存储，只需要替换索引读写层，不需要推翻现有 API。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
from collections import OrderedDict
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
try:
    # 优先从插件内置 _vendor 加载 jieba3，无需用户预装
    import sys as _vendor_sys
    _vendor_sys.path.insert(0, str(Path(__file__).resolve().parent / "_vendor"))
    from jieba3 import jieba3 as Jieba3Tokenizer
except Exception:
    try:
        from jieba3 import jieba3 as Jieba3Tokenizer
    except Exception:  # pragma: no cover - 运行环境未安装依赖时自动回退
        Jieba3Tokenizer = None

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

from akm.config import load_config

from akm.plugins import PluginBase

import os as _os
import sys as _sys
import importlib.util as _importlib_util

_dir = _os.path.dirname(_os.path.abspath(__file__))
_session_scanner_spec = _importlib_util.spec_from_file_location(
    "markdown_kb_session_scanner", _os.path.join(_dir, "session_scanner.py")
)
session_scanner = _importlib_util.module_from_spec(_session_scanner_spec)
_sys.modules["markdown_kb_session_scanner"] = session_scanner
_session_scanner_spec.loader.exec_module(session_scanner)


router = APIRouter()
_plugin_instance: "Plugin | None" = None


def _get_plugin() -> "Plugin":
    """返回当前插件实例。

    路由函数注册发生在模块导入阶段，因此这里通过模块级引用拿到由 PluginManager
    实际实例化后的插件对象。若对象尚未注入，说明插件生命周期异常，应直接返回 500，
    方便尽早暴露接入问题。
    """
    if _plugin_instance is None:
        raise HTTPException(status_code=500, detail="markdown_kb 插件尚未初始化")
    return _plugin_instance


def _utc_now_iso() -> str:
    """统一生成 UTC 时间字符串，便于状态接口和后续索引元数据复用。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    """把任意输入稳妥转换为浮点数，避免配置值类型漂移导致崩溃。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    """把任意输入稳妥转换为整数。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _normalize_embedding_vector(values: list[float]) -> list[float]:
    """把 embedding 归一化为单位向量，便于让 L2 距离更接近原有余弦相似度语义。

    设计说明：
    1. 现有 Python 回退链路使用的是余弦相似度；
    2. sqlite-vec 默认 KNN 更适合直接基于距离做粗召回；
    3. 这里把写入 vec 表和查询向量都归一化到单位长度，这样欧氏距离与余弦相似度
       存在稳定单调关系，能尽量减少切换召回后端后排序心智的突变。
    """
    vector = [_safe_float(item, 0.0) for item in list(values or [])]
    if not vector:
        return []
    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 1e-12:
        return vector
    return [item / norm for item in vector]


def _sqlite_vec_distance_to_score(distance: float) -> float:
    """把 sqlite-vec 的单位向量 L2 距离换算回近似余弦相似度分数。

    对单位向量 `u`、`v` 有：
    `||u-v||^2 = 2 - 2cos(theta)`，因此：
    `cos(theta) = 1 - distance^2 / 2`

    这样对外暴露的 `vector_score` 仍保持“越大越相关”的语义，便于继续和
    归一化 BM25 分做线性融合。
    """
    value = 1.0 - ((_safe_float(distance, 0.0) ** 2) / 2.0)
    return max(-1.0, min(1.0, value))


def _normalize_weights(*weights: float) -> tuple[float, ...]:
    """把多路权重归一化到 0~1，总和为 1。

    说明：
    1. 每权重 clamp 到 >= 0，保留 0 值；
    2. 全 0 时回退为第一路 = 1.0（保留最后手段的权重）；
    3. 支持任意路数（语义分 / 关键词分 / 记忆分 / ...）。
    """
    clamped = tuple(max(0.0, float(w or 0.0)) for w in weights)
    total = sum(clamped)
    if total <= 0:
        result = [0.0] * len(clamped)
        result[0] = 1.0
        return tuple(result)
    return tuple(w / total for w in clamped)


class IndexStore(ABC):
    """索引存储接口。

    设计目标：
    1. 让 `markdown_kb` 的主流程不依赖具体存储实现；
    2. 当前默认使用本地 JSON；
    3. 后续如果接入 SQLite Vector 扩展，也只需要替换这层即可。
    """

    backend_name = "unknown"

    @abstractmethod
    def load(self) -> dict:
        """读取完整索引快照。"""

    @abstractmethod
    def save(self, data: dict) -> None:
        """写入完整索引快照。"""

    @abstractmethod
    def replace_all(self, documents: list[dict], metadata: dict) -> dict:
        """使用新的文档集合整体替换索引，并返回最新快照。"""

    @abstractmethod
    def list_documents(self) -> list[dict]:
        """返回当前索引中的全部文档条目。"""

    @abstractmethod
    def delete_by_file(self, file_name: str) -> dict:
        """删除指定文件对应的索引条目，并返回操作结果。"""

    @abstractmethod
    def stats(self) -> dict:
        """返回索引层统计信息，供状态页直接展示。"""

    @abstractmethod
    def clear(self) -> dict:
        """清空索引内容，并返回清理结果。"""

    def list_documents_by_scope(self, workspace_root: str = "", selected_doc: dict | None = None) -> list[dict]:
        """按 workspace / selected_doc 返回当前候选文档集合。

        默认回退到全量 `list_documents()`，保证历史 backend 至少保持可用；
        更高效的 backend 可自行覆写，在存储层提前完成过滤。
        """
        documents = list(self.list_documents())
        normalized_workspace = str(workspace_root or "").strip()
        if normalized_workspace:
            documents = [
                item for item in documents
                if not str(item.get("workspace_root") or "").strip()
                or str(item.get("workspace_root") or "").strip() == normalized_workspace
            ]
        else:
            documents = [item for item in documents if not str(item.get("workspace_root") or "").strip()]

        if not isinstance(selected_doc, dict):
            return documents
        selected_doc_id = str(selected_doc.get("doc_id") or "").strip()
        if selected_doc_id:
            return [item for item in documents if str(item.get("doc_id") or "").strip() == selected_doc_id]
        selected_file_name = Path(str(selected_doc.get("file_name") or "").strip()).name
        selected_workspace = str(selected_doc.get("workspace_root") or "").strip()
        if not selected_file_name:
            return documents
        return [
            item for item in documents
            if Path(str(item.get("file_name") or "")).name == selected_file_name
            and str(item.get("workspace_root") or "").strip() == selected_workspace
        ]


class JsonIndexStore(IndexStore):
    """当前默认的本地 JSON backend。"""

    backend_name = "json"

    def __init__(self, index_path: Path, logger):
        self.index_path = index_path
        self.logger = logger

    def load(self) -> dict:
        """读取索引文件，失败时回退为空索引。"""
        if not self.index_path.exists():
            return {"documents": [], "last_rebuilt_at": None}
        try:
            data = json.loads(self.index_path.read_text("utf-8"))
        except Exception:
            self.logger.warning("[markdown_kb] 索引文件损坏，已按空索引处理: %s", self.index_path)
            return {"documents": [], "last_rebuilt_at": None}
        if not isinstance(data, dict):
            return {"documents": [], "last_rebuilt_at": None}
        data.setdefault("documents", [])
        data.setdefault("last_rebuilt_at", None)
        return data

    def save(self, data: dict) -> None:
        """把索引数据写回本地 JSON。"""
        self.index_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def replace_all(self, documents: list[dict], metadata: dict) -> dict:
        """全量替换索引内容。"""
        snapshot = {
            "ok": True,
            "documents": list(documents or []),
            "last_rebuilt_at": metadata.get("last_rebuilt_at"),
            "embedding_model": metadata.get("embedding_model"),
        }
        self.save(snapshot)
        return snapshot

    def list_documents(self) -> list[dict]:
        """返回索引中的全部文档条目。"""
        return list(self.load().get("documents", []))

    def delete_by_file(self, file_name: str) -> dict:
        """删除指定文件关联的所有 chunks。"""
        snapshot = self.load()
        original_documents = snapshot.get("documents", [])
        filtered_documents = [item for item in original_documents if item.get("file_name") != file_name]
        removed_count = len(original_documents) - len(filtered_documents)
        snapshot["documents"] = filtered_documents
        if removed_count > 0:
            snapshot["last_rebuilt_at"] = _utc_now_iso()
            self.save(snapshot)
        return {
            "snapshot": snapshot,
            "removed_chunks": removed_count,
        }

    def stats(self) -> dict:
        """返回索引统计信息。"""
        snapshot = self.load()
        return {
            "document_count": len(snapshot.get("documents", [])),
            "last_rebuilt_at": snapshot.get("last_rebuilt_at"),
            "embedding_model": snapshot.get("embedding_model"),
        }

    def clear(self) -> dict:
        """清空 JSON 索引内容。"""
        empty = {"ok": True, "documents": [], "last_rebuilt_at": None, "embedding_model": None}
        self.save(empty)
        return {"removed_chunks": 0, "snapshot": empty}


class SqliteKbIndexStore(IndexStore):
    """基于插件私有 `kb.db` 的 SQLite 索引存储。

    当前版本把：
    1. 文件元数据
    2. chunk 元数据
    3. embedding 向量
    统一收口到一个 SQLite 文件里。

    检索路径采用“sqlite-vec 优先、Python 回退兜底”：
    1. 如果当前运行时能够加载 sqlite-vec，则第一阶段粗召回优先在 SQLite 内完成；
    2. 同时继续保留 `embedding_json`，便于在 vec 不可用、维度不一致或测试环境下
       自动回退到 Python 余弦计算；
    3. 这样既能把 workspace 过滤前推到 SQL 层，也不会把插件可用性强绑到某个单一
       本地 Python 发行版上。
    """

    backend_name = "sqlite"

    _vec_table_name = "kb_vec_chunks"

    def __init__(self, db_path: Path, logger):
        self.db_path = db_path
        self.logger = logger
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._sqlite_vec_module = None
        self._vec_runtime_import_error = None
        self._vec_runtime_available = False
        self._vec_runtime_checked = False
        self._vec_version = ""
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_vec_runtime(conn)
        return conn

    def _import_sqlite_vec_optional(self):
        """按需导入 sqlite-vec Python 包，并缓存导入结果。"""
        if self._vec_runtime_checked:
            return self._sqlite_vec_module
        self._vec_runtime_checked = True
        try:
            import sqlite_vec  # type: ignore
            self._sqlite_vec_module = sqlite_vec
        except Exception as exc:
            self._sqlite_vec_module = None
            self._vec_runtime_import_error = str(exc)
        return self._sqlite_vec_module

    def _ensure_vec_runtime(self, conn: sqlite3.Connection) -> bool:
        """尝试为当前 SQLite 连接加载 sqlite-vec 扩展。

        注意这里的“可用”是按“当前 Python 运行时 + 当前连接”判断的：
        - 运行时不支持 `enable_load_extension()` 时，直接返回 False；
        - 包未安装、扩展加载失败时，也返回 False；
        - 成功后会把版本号缓存起来，供状态接口展示。
        """
        sqlite_vec_module = self._import_sqlite_vec_optional()
        if sqlite_vec_module is None:
            self._vec_runtime_available = False
            return False
        if not hasattr(conn, "enable_load_extension"):
            self._vec_runtime_available = False
            return False
        try:
            conn.enable_load_extension(True)
            sqlite_vec_module.load(conn)
            row = conn.execute("select vec_version()").fetchone()
            self._vec_version = str(row[0] if row else "") if row else ""
            self._vec_runtime_available = True
            return True
        except Exception as exc:
            self._vec_runtime_available = False
            self._vec_runtime_import_error = str(exc)
            return False

    def _vec_table_exists(self, conn: sqlite3.Connection) -> bool:
        """判断当前库里是否已经存在 vec 虚表。"""
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = ?",
            (self._vec_table_name,),
        ).fetchone()
        return bool(row and int(row[0]) > 0)

    def _drop_vec_table(self, conn: sqlite3.Connection) -> None:
        """删除 vec 虚表。

        说明：
        1. vec0 的维度在建表时固定，因此 embedding 维度变化时必须整表重建；
        2. 当前 `replace_all()` 本来就是全量替换，直接 drop/recreate 最简单稳定；
        3. 如果当前运行时无法加载 sqlite-vec，则这里只做温和跳过，继续保留 JSON
           向量与 Python 回退路径，避免因为本地环境差异直接让重建失败。
        """
        if not self._vec_runtime_available:
            return
        if not self._vec_table_exists(conn):
            return
        conn.execute(f"DROP TABLE IF EXISTS {self._vec_table_name}")

    def _create_vec_table(self, conn: sqlite3.Connection, embedding_dim: int) -> None:
        """按当前 embedding 维度创建 vec0 虚表。"""
        if not self._vec_runtime_available or embedding_dim <= 0:
            return
        conn.execute(
            f"CREATE VIRTUAL TABLE {self._vec_table_name} "
            f"USING vec0(chunk_id text primary key, embedding float[{int(embedding_dim)}])"
        )

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()
            if row and row[0] > 0:
                self._ensure_column(conn, "kb_documents", "workspace_root", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, "kb_chunks", "workspace_root", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, "kb_chunks", "categories", "TEXT DEFAULT ''")
                self._ensure_column(conn, "kb_chunks", "heading_path", "TEXT DEFAULT ''")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kb_documents (
                    doc_id TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    workspace_root TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL DEFAULT 0,
                    title TEXT DEFAULT '',
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    indexed_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(file_path)
                );

                CREATE TABLE IF NOT EXISTS kb_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    workspace_root TEXT NOT NULL DEFAULT '',
                    title TEXT DEFAULT '',
                    heading_level INTEGER NOT NULL DEFAULT 0,
                    heading_path TEXT DEFAULT '',
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    categories TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    indexed_at TEXT NOT NULL,
                    FOREIGN KEY(doc_id) REFERENCES kb_documents(doc_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS kb_vectors (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                    chunk_id TEXT NOT NULL UNIQUE,
                    embedding_json TEXT NOT NULL,
                    FOREIGN KEY(chunk_id) REFERENCES kb_chunks(chunk_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS kb_index_meta (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT
                );

                CREATE TABLE IF NOT EXISTS kb_chunk_memory (
                    chunk_id      TEXT PRIMARY KEY,
                    hit_count     INTEGER NOT NULL DEFAULT 0,
                    last_hit_at   TEXT,
                    memory_value  REAL NOT NULL DEFAULT 0.0,
                    created_at    TEXT NOT NULL,
                    FOREIGN KEY(chunk_id) REFERENCES kb_chunks(chunk_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_kb_documents_file_name ON kb_documents(file_name);
                CREATE INDEX IF NOT EXISTS idx_kb_documents_workspace_root ON kb_documents(workspace_root);
                CREATE INDEX IF NOT EXISTS idx_kb_documents_content_hash ON kb_documents(content_hash);
                CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc_id ON kb_chunks(doc_id);
                CREATE INDEX IF NOT EXISTS idx_kb_chunks_file_name ON kb_chunks(file_name);
                CREATE INDEX IF NOT EXISTS idx_kb_chunks_workspace_root ON kb_chunks(workspace_root);
                CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc_chunk_index ON kb_chunks(doc_id, chunk_index);
                CREATE INDEX IF NOT EXISTS idx_kb_vectors_chunk_id ON kb_vectors(chunk_id);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
        """为历史数据库补齐新增列，避免升级后直接重建索引前无法启动。"""
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row[1]) for row in rows}
        if column_name in existing:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")

    def _load_meta_map(self, conn: sqlite3.Connection) -> dict:
        rows = conn.execute("SELECT meta_key, meta_value FROM kb_index_meta").fetchall()
        return {row["meta_key"]: row["meta_value"] for row in rows}

    def _save_meta_map(self, conn: sqlite3.Connection, meta: dict) -> None:
        conn.execute("DELETE FROM kb_index_meta")
        for key, value in (meta or {}).items():
            conn.execute(
                "INSERT INTO kb_index_meta(meta_key, meta_value) VALUES (?, ?)",
                (str(key), "" if value is None else str(value)),
            )

    def _document_from_row(self, row: sqlite3.Row, embedding_json: str | None) -> dict:
        return {
            "id": row["chunk_id"],
            "doc_id": row["doc_id"],
            "file_name": row["file_name"],
            "file_path": row["file_path"],
            "workspace_root": row["workspace_root"],
            "title": row["title"],
            "heading_level": row["heading_level"],
            "heading_path": row["heading_path"] if "heading_path" in row.keys() else "",
            "categories": row["categories"] if "categories" in row.keys() else "",
            "chunk_index": row["chunk_index"],
            "chunk_text": row["chunk_text"],
            "content_hash": row["content_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "indexed_at": row["indexed_at"],
            "embedding": json.loads(embedding_json or "[]"),
        }

    def load(self) -> dict:
        conn = self._connect()
        try:
            meta = self._load_meta_map(conn)
            rows = conn.execute(
                """
                SELECT c.*, v.embedding_json
                FROM kb_chunks c
                LEFT JOIN kb_vectors v ON v.chunk_id = c.chunk_id
                ORDER BY c.file_name ASC, c.chunk_index ASC
                """
            ).fetchall()
            return {
                "ok": True,
                "documents": [self._document_from_row(row, row["embedding_json"]) for row in rows],
                "last_rebuilt_at": meta.get("last_rebuilt_at"),
                "embedding_model": meta.get("embedding_model"),
            }
        finally:
            conn.close()

    def save(self, data: dict) -> None:
        documents = list((data or {}).get("documents", []))
        metadata = dict((data or {}).get("metadata") or {})
        metadata.setdefault("last_rebuilt_at", (data or {}).get("last_rebuilt_at"))
        metadata.setdefault("embedding_model", (data or {}).get("embedding_model"))
        self.replace_all(documents, metadata)

    def replace_all(self, documents: list[dict], metadata: dict) -> dict:
        conn = self._connect()
        try:
            now = metadata.get("last_rebuilt_at")
            embeddings = [
                list(item.get("embedding") or [])
                for item in list(documents or [])
                if isinstance(item.get("embedding"), list) and list(item.get("embedding") or [])
            ]
            dims = {len(vector) for vector in embeddings if vector}
            embedding_dim = int(next(iter(dims))) if len(dims) == 1 else 0
            vec_ready = bool(self._vec_runtime_available and embedding_dim > 0 and len(dims) == 1)

            conn.execute("DELETE FROM kb_vectors")
            conn.execute("DELETE FROM kb_chunks")
            conn.execute("DELETE FROM kb_documents")
            self._drop_vec_table(conn)
            if vec_ready:
                self._create_vec_table(conn, embedding_dim)

            by_doc: dict[str, list[dict]] = {}
            for item in list(documents or []):
                by_doc.setdefault(str(item.get("doc_id") or ""), []).append(item)

            for doc_id, chunks in by_doc.items():
                if not doc_id or not chunks:
                    continue
                first = chunks[0]
                conn.execute(
                    """
                    INSERT INTO kb_documents(
                        doc_id, file_name, file_path, workspace_root, content_hash, file_size_bytes,
                        title, chunk_count, updated_at, indexed_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        first.get("file_name") or "",
                        first.get("file_path") or "",
                        first.get("workspace_root") or "",
                        first.get("content_hash") or "",
                        0,
                        first.get("title") or "",
                        len(chunks),
                        first.get("updated_at") or now or _utc_now_iso(),
                        now,
                        first.get("created_at") or now or _utc_now_iso(),
                    ),
                )

            for item in list(documents or []):
                conn.execute(
                    """
                    INSERT INTO kb_chunks(
                        chunk_id, doc_id, file_name, file_path, workspace_root, title, heading_level,
                        heading_path, categories,
                        chunk_index, chunk_text, content_hash, created_at, updated_at, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.get("id"),
                        item.get("doc_id"),
                        item.get("file_name") or "",
                        item.get("file_path") or "",
                        item.get("workspace_root") or "",
                        item.get("title") or "",
                        _safe_int(item.get("heading_level"), 0),
                        str(item.get("heading_path") or ""),
                        str(item.get("categories") or ""),
                        _safe_int(item.get("chunk_index"), 0),
                        item.get("chunk_text") or "",
                        item.get("content_hash") or "",
                        item.get("created_at") or now or _utc_now_iso(),
                        item.get("updated_at") or now or _utc_now_iso(),
                        item.get("indexed_at") or now or _utc_now_iso(),
                    ),
                )
                conn.execute(
                    "INSERT INTO kb_vectors(chunk_id, embedding_json) VALUES (?, ?)",
                    (item.get("id"), json.dumps(item.get("embedding") or [], ensure_ascii=False)),
                )
                if vec_ready and item.get("id"):
                    conn.execute(
                        f"INSERT INTO {self._vec_table_name}(chunk_id, embedding) VALUES (?, ?)",
                        (
                            item.get("id"),
                            json.dumps(_normalize_embedding_vector(item.get("embedding") or []), ensure_ascii=False),
                        ),
                    )

            metadata_to_save = dict(metadata or {})
            metadata_to_save["vec_available"] = "1" if self._vec_runtime_available else "0"
            metadata_to_save["vec_ready"] = "1" if vec_ready else "0"
            metadata_to_save["vec_enabled"] = "1" if (self._vec_runtime_available and vec_ready) else "0"
            metadata_to_save["vec_version"] = self._vec_version or ""
            metadata_to_save["embedding_dim"] = str(int(embedding_dim))
            if embeddings and len(dims) > 1:
                metadata_to_save["vec_disabled_reason"] = "mixed_embedding_dimensions"
            elif embeddings and embedding_dim <= 0:
                metadata_to_save["vec_disabled_reason"] = "invalid_embedding_dimension"
            elif not self._vec_runtime_available:
                metadata_to_save["vec_disabled_reason"] = "sqlite_vec_unavailable"
            else:
                metadata_to_save["vec_disabled_reason"] = ""
            self._save_meta_map(conn, metadata_to_save)
            conn.commit()
        finally:
            conn.close()

        return self.load()

    def list_documents(self) -> list[dict]:
        return list(self.load().get("documents", []))

    def delete_by_file(self, file_name: str) -> dict:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT chunk_id FROM kb_chunks WHERE file_name = ?",
                (file_name,),
            ).fetchall()
            chunk_ids = [row["chunk_id"] for row in rows]
            removed_count = len(chunk_ids)
            if chunk_ids:
                conn.executemany("DELETE FROM kb_vectors WHERE chunk_id = ?", [(chunk_id,) for chunk_id in chunk_ids])
                conn.execute("DELETE FROM kb_chunks WHERE file_name = ?", (file_name,))
                conn.execute("DELETE FROM kb_documents WHERE file_name = ?", (file_name,))
                meta = self._load_meta_map(conn)
                meta["last_rebuilt_at"] = _utc_now_iso()
                self._save_meta_map(conn, meta)
                conn.commit()
            snapshot = self.load()
            return {
                "snapshot": snapshot,
                "removed_chunks": removed_count,
            }
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self._connect()
        try:
            meta = self._load_meta_map(conn)
            row = conn.execute("SELECT COUNT(*) AS count FROM kb_chunks").fetchone()
            return {
                "document_count": int(row["count"] if row else 0),
                "last_rebuilt_at": meta.get("last_rebuilt_at"),
                "embedding_model": meta.get("embedding_model"),
                "db_path": str(self.db_path),
                "vec_available": self._vec_runtime_available,
                "vec_ready": str(meta.get("vec_ready") or "") == "1",
                "vec_enabled": self._vec_runtime_available and str(meta.get("vec_ready") or "") == "1",
                "vec_version": self._vec_version or str(meta.get("vec_version") or ""),
                "embedding_dim": _safe_int(meta.get("embedding_dim"), 0),
                "vec_disabled_reason": str(meta.get("vec_disabled_reason") or ""),
            }
        finally:
            conn.close()

    def clear(self) -> dict:
        """清空 SQLite 索引内容，但保留表结构。"""
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS count FROM kb_chunks").fetchone()
            removed_count = int(row["count"] if row else 0)
            conn.execute("DELETE FROM kb_vectors")
            conn.execute("DELETE FROM kb_chunks")
            conn.execute("DELETE FROM kb_documents")
            self._drop_vec_table(conn)
            conn.execute("DELETE FROM kb_index_meta")
            conn.commit()
        finally:
            conn.close()
        return {
            "removed_chunks": removed_count,
            "snapshot": self.load(),
        }

    def search_by_vector(
        self,
        query_vector: list[float],
        limit: int,
        workspace_root: str = "",
        selected_doc: dict | None = None,
    ) -> list[dict]:
        """使用 sqlite-vec 在数据库侧完成第一阶段向量召回。

        返回值约定：
        - 只返回候选 `chunk_id` 与原始 `distance`，由上层继续换算成 `vector_score`；
        - workspace / selected_doc 过滤会前推到 SQL 条件里，避免别的项目 chunk 先把
          top-N 名额挤满，再在 Python 里“后过滤”导致结果看起来不准。
        """
        conn = self._connect()
        try:
            meta = self._load_meta_map(conn)
            if not (self._vec_runtime_available and str(meta.get("vec_ready") or "") == "1"):
                return []
            embedding_dim = _safe_int(meta.get("embedding_dim"), 0)
            normalized_query = _normalize_embedding_vector(query_vector)
            if embedding_dim <= 0 or len(normalized_query) != embedding_dim:
                return []

            normalized_workspace = str(workspace_root or "").strip()
            selected_doc = selected_doc if isinstance(selected_doc, dict) else {}
            params: list[Any] = [json.dumps(normalized_query, ensure_ascii=False), max(1, int(limit))]
            clauses = []
            if normalized_workspace:
                clauses.append("(c.workspace_root = '' OR c.workspace_root = ?)")
                params.append(normalized_workspace)
            else:
                clauses.append("c.workspace_root = ''")

            selected_doc_id = str(selected_doc.get("doc_id") or "").strip()
            if selected_doc_id:
                clauses.append("c.doc_id = ?")
                params.append(selected_doc_id)
            else:
                selected_file_name = Path(str(selected_doc.get("file_name") or "").strip()).name
                selected_workspace = str(selected_doc.get("workspace_root") or "").strip()
                if selected_file_name:
                    clauses.append("c.file_name = ?")
                    clauses.append("c.workspace_root = ?")
                    params.extend([selected_file_name, selected_workspace])

            where_sql = ""
            if clauses:
                where_sql = " AND " + " AND ".join(clauses)
            rows = conn.execute(
                f"""
                SELECT c.chunk_id, c.doc_id, c.file_name, c.workspace_root, distance
                FROM {self._vec_table_name} v
                JOIN kb_chunks c ON c.chunk_id = v.chunk_id
                WHERE v.embedding MATCH ?
                  AND k = ?
                  {where_sql}
                ORDER BY distance ASC
                """,
                params,
            ).fetchall()
            return [
                {
                    "id": row["chunk_id"],
                    "doc_id": row["doc_id"],
                    "file_name": row["file_name"],
                    "workspace_root": row["workspace_root"],
                    "distance": _safe_float(row["distance"], 0.0),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def get_documents_by_chunk_ids(self, chunk_ids: list[str]) -> list[dict]:
        """按给定 chunk_id 顺序批量读取完整文档条目。"""
        normalized_chunk_ids = [str(item or "").strip() for item in list(chunk_ids or []) if str(item or "").strip()]
        if not normalized_chunk_ids:
            return []
        conn = self._connect()
        try:
            placeholders = ", ".join("?" for _ in normalized_chunk_ids)
            rows = conn.execute(
                f"""
                SELECT c.*, v.embedding_json
                FROM kb_chunks c
                LEFT JOIN kb_vectors v ON v.chunk_id = c.chunk_id
                WHERE c.chunk_id IN ({placeholders})
                """,
                normalized_chunk_ids,
            ).fetchall()
            by_id = {
                str(row["chunk_id"]): self._document_from_row(row, row["embedding_json"])
                for row in rows
            }
            return [by_id[item] for item in normalized_chunk_ids if item in by_id]
        finally:
            conn.close()

    def list_documents_by_scope(self, workspace_root: str = "", selected_doc: dict | None = None) -> list[dict]:
        """在 SQLite 层按 scope 过滤文档，避免 fallback 前先把全量文档拉回内存。"""
        conn = self._connect()
        try:
            normalized_workspace = str(workspace_root or "").strip()
            selected_doc = selected_doc if isinstance(selected_doc, dict) else {}
            params: list[Any] = []
            clauses = []
            if normalized_workspace:
                clauses.append("(c.workspace_root = '' OR c.workspace_root = ?)")
                params.append(normalized_workspace)
            else:
                clauses.append("c.workspace_root = ''")

            selected_doc_id = str(selected_doc.get("doc_id") or "").strip()
            if selected_doc_id:
                clauses.append("c.doc_id = ?")
                params.append(selected_doc_id)
            else:
                selected_file_name = Path(str(selected_doc.get("file_name") or "").strip()).name
                if selected_file_name:
                    clauses.append("c.file_name = ?")
                    params.append(selected_file_name)
                    clauses.append("c.workspace_root = ?")
                    params.append(str(selected_doc.get("workspace_root") or "").strip())

            where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = conn.execute(
                f"""
                SELECT c.*, v.embedding_json
                FROM kb_chunks c
                LEFT JOIN kb_vectors v ON v.chunk_id = c.chunk_id
                {where_sql}
                ORDER BY c.file_name ASC, c.chunk_index ASC
                """,
                params,
            ).fetchall()
            return [self._document_from_row(row, row["embedding_json"]) for row in rows]
        finally:
            conn.close()

    # ---------- 记忆系统方法 ----------

    def read_memory_map(self, chunk_ids: list[str]) -> dict[str, dict]:
        """批量读取 chunk 的记忆状态，返回 {chunk_id: {memory_value, hit_count, last_hit_at}}。"""
        if not chunk_ids:
            return {}
        conn = self._connect()
        try:
            placeholders = ",".join(["?" for _ in chunk_ids])
            rows = conn.execute(
                f"SELECT chunk_id, hit_count, last_hit_at, memory_value FROM kb_chunk_memory WHERE chunk_id IN ({placeholders})",
                chunk_ids,
            ).fetchall()
            return {
                row["chunk_id"]: {
                    "memory_value": max(0.0, min(1.0, float(row["memory_value"] or 0.0))),
                    "hit_count": int(row["hit_count"] or 0),
                    "last_hit_at": str(row["last_hit_at"] or ""),
                }
                for row in rows
            }
        finally:
            conn.close()

    def update_memory(
        self,
        upserts: dict[str, dict],
        cap: float = 1.0,
    ) -> None:
        """批量 upsert 记忆记录。

        upserts: {chunk_id: {memory_value, hit_count_delta, last_hit_at}}
        其中 hit_count_delta 表示需要在其现有 hit_count 基础上增加的值。
        cap 为记忆值上限，最终 memory_value 不会超过该值。
        """
        if not upserts:
            return
        conn = self._connect()
        try:
            # 先读取现有 hit_count 做 delta 叠加
            placeholders = ",".join(["?" for _ in upserts])
            existing_rows = conn.execute(
                f"SELECT chunk_id, hit_count FROM kb_chunk_memory WHERE chunk_id IN ({placeholders})",
                list(upserts.keys()),
            ).fetchall()
            existing = {row["chunk_id"]: int(row["hit_count"] or 0) for row in existing_rows}

            now = _utc_now_iso()
            cap_value = max(0.0, min(1.0, cap))
            for chunk_id, info in upserts.items():
                mv = max(0.0, min(cap_value, _safe_float(info.get("memory_value"), 0.0)))
                hit_delta = _safe_int(info.get("hit_count_delta"), 0)
                total_hits = existing.get(chunk_id, 0) + hit_delta
                last_hit = str(info.get("last_hit_at") or now)
                conn.execute(
                    """
                    INSERT INTO kb_chunk_memory(chunk_id, hit_count, last_hit_at, memory_value, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        hit_count = excluded.hit_count,
                        last_hit_at = excluded.last_hit_at,
                        memory_value = excluded.memory_value
                    """,
                    (chunk_id, total_hits, last_hit, mv, now),
                )
            conn.commit()
        finally:
            conn.close()

    def cleanup_expired_memory(self) -> int:
        """清理过期记忆记录（memory_value < 0.001 且超过 30 天未命中），返回删除行数。"""
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                DELETE FROM kb_chunk_memory
                WHERE memory_value < 0.001
                  AND (last_hit_at IS NULL OR last_hit_at < datetime('now', '-30 days'))
                """
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def clear_memory(self) -> None:
        """清空所有记忆记录（清除索引时调用）。"""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM kb_chunk_memory")
            conn.commit()
        finally:
            conn.close()

    def find_nearest_chunk(self, embedding_vector: list[float]) -> dict | None:
        """全局向量搜索：在所有 workspace 中查找与给定 embedding 最相似的 chunk。

        返回值：{"chunk_id": ..., "distance": ...} 或 None。
        仅当 sqlite-vec 可用且索引中存在向量时生效，否则返回 None。
        用于去重合并时判断新 chunk 是否与存量 chunk 高度相似。
        """
        conn = self._connect()
        try:
            meta = self._load_meta_map(conn)
            if not (self._vec_runtime_available and str(meta.get("vec_ready") or "") == "1"):
                return None
            embedding_dim = _safe_int(meta.get("embedding_dim"), 0)
            normalized = _normalize_embedding_vector(embedding_vector)
            if embedding_dim <= 0 or len(normalized) != embedding_dim:
                return None
            rows = conn.execute(
                f"""
                SELECT c.chunk_id, distance
                FROM {self._vec_table_name} v
                JOIN kb_chunks c ON c.chunk_id = v.chunk_id
                WHERE v.embedding MATCH ?
                  AND k = 1
                ORDER BY distance ASC
                """,
                [json.dumps(normalized, ensure_ascii=False)],
            ).fetchall()
            if not rows:
                return None
            return {
                "chunk_id": rows[0]["chunk_id"],
                "distance": _safe_float(rows[0]["distance"], 0.0),
            }
        finally:
            conn.close()

    def get_learn_doc_memory_stats(self) -> list[dict]:
        """获取所有 .learn.md 文档的 chunk 记忆统计，按 doc_id 聚合。

        返回每个 learn 文档的：doc_id / file_name / chunk_count / max_memory / total_hits。
        用于 organizer 判断哪些 learn 文档已无价值、可以清理。
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT c.doc_id, c.file_name,
                       COUNT(DISTINCT c.chunk_id) as chunk_count,
                       COALESCE(MAX(m.memory_value), 0.0) as max_memory,
                       COALESCE(SUM(m.hit_count), 0) as total_hits
                FROM kb_chunks c
                LEFT JOIN kb_chunk_memory m ON c.chunk_id = m.chunk_id
                WHERE c.file_name LIKE '%.learn.md'
                GROUP BY c.doc_id
                """
            ).fetchall()
            return [
                {
                    "doc_id": row["doc_id"],
                    "file_name": row["file_name"],
                    "chunk_count": int(row["chunk_count"] or 0),
                    "max_memory": round(float(row["max_memory"] or 0.0), 4),
                    "total_hits": int(row["total_hits"] or 0),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def get_sibling_chunks(self, doc_id: str, heading_path: str = "") -> list[dict]:
        """获取同 doc_id + heading_path 下的所有兄弟 chunk，按 chunk_index 排序。"""
        conn = self._connect()
        try:
            if heading_path:
                rows = conn.execute(
                    """
                    SELECT c.*, v.embedding_json
                    FROM kb_chunks c
                    LEFT JOIN kb_vectors v ON v.chunk_id = c.chunk_id
                    WHERE c.doc_id = ? AND c.heading_path = ?
                    ORDER BY c.chunk_index ASC
                    """,
                    (doc_id, heading_path),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT c.*, v.embedding_json
                    FROM kb_chunks c
                    LEFT JOIN kb_vectors v ON v.chunk_id = c.chunk_id
                    WHERE c.doc_id = ?
                    ORDER BY c.chunk_index ASC
                    """,
                    (doc_id,),
                ).fetchall()
            return [self._document_from_row(row, row["embedding_json"]) for row in rows]
        finally:
            conn.close()

    def get_all_memory_stats(self) -> dict:
        """获取记忆表统计指标。"""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as cnt,
                    COALESCE(AVG(memory_value), 0.0) as avg_val,
                    SUM(hit_count) as total_hits,
                    SUM(CASE WHEN memory_value > 0.5 THEN 1 ELSE 0 END) as high_val_cnt
                FROM kb_chunk_memory
                """
            ).fetchone()
            return {
                "memory_chunk_count": int(row["cnt"] or 0),
                "memory_avg_value": round(float(row["avg_val"] or 0.0), 4),
                "memory_total_hits": int(row["total_hits"] or 0),
                "memory_high_value_count": int(row["high_val_cnt"] or 0),
            }
        finally:
            conn.close()

    def get_memory_detail_stats(self, top_n: int = 20) -> dict:
        """获取记忆详细统计，包含按文档聚合和 top-N 高记忆 chunk。

        返回：
        - summary: 总体统计
        - by_doc: 按文档聚合的记忆统计
        - top_chunks: 记忆值最高的 N 个 chunk 详情
        """
        conn = self._connect()
        try:
            # 总体统计
            summary_row = conn.execute(
                """
                SELECT
                    COUNT(*) as cnt,
                    COALESCE(AVG(memory_value), 0.0) as avg_val,
                    SUM(hit_count) as total_hits,
                    SUM(CASE WHEN memory_value > 0.5 THEN 1 ELSE 0 END) as high_val_cnt
                FROM kb_chunk_memory
                """
            ).fetchone()

            # 按文档聚合（JOIN kb_chunks 获取 doc_id / file_name）
            doc_rows = conn.execute(
                """
                SELECT c.doc_id, c.file_name,
                       COUNT(DISTINCT m.chunk_id) as chunk_count,
                       COALESCE(AVG(m.memory_value), 0.0) as avg_memory,
                       COALESCE(MAX(m.memory_value), 0.0) as max_memory,
                       COALESCE(SUM(m.hit_count), 0) as total_hits
                FROM kb_chunks c
                INNER JOIN kb_chunk_memory m ON c.chunk_id = m.chunk_id
                GROUP BY c.doc_id
                ORDER BY max_memory DESC, total_hits DESC
                """
            ).fetchall()

            # Top-N 高记忆 chunk
            top_rows = conn.execute(
                """
                SELECT c.chunk_id, c.doc_id, c.file_name, c.heading_path, c.chunk_text,
                       m.memory_value, m.hit_count, m.last_hit_at
                FROM kb_chunk_memory m
                INNER JOIN kb_chunks c ON m.chunk_id = c.chunk_id
                ORDER BY m.memory_value DESC, m.hit_count DESC
                LIMIT ?
                """,
                (top_n,),
            ).fetchall()

            return {
                "summary": {
                    "memory_chunk_count": int(summary_row["cnt"] or 0),
                    "memory_avg_value": round(float(summary_row["avg_val"] or 0.0), 4),
                    "memory_total_hits": int(summary_row["total_hits"] or 0),
                    "memory_high_value_count": int(summary_row["high_val_cnt"] or 0),
                },
                "by_doc": [
                    {
                        "doc_id": row["doc_id"],
                        "file_name": row["file_name"],
                        "chunk_count": int(row["chunk_count"] or 0),
                        "avg_memory": round(float(row["avg_memory"] or 0.0), 4),
                        "max_memory": round(float(row["max_memory"] or 0.0), 4),
                        "total_hits": int(row["total_hits"] or 0),
                    }
                    for row in doc_rows
                ],
                "top_chunks": [
                    {
                        "chunk_id": row["chunk_id"],
                        "doc_id": row["doc_id"],
                        "file_name": row["file_name"],
                        "heading_path": str(row["heading_path"] or ""),
                        "chunk_text_preview": (str(row["chunk_text"] or "")[:200] + "..." if len(str(row["chunk_text"] or "")) > 200 else str(row["chunk_text"] or "")),
                        "memory_value": round(float(row["memory_value"] or 0.0), 4),
                        "hit_count": int(row["hit_count"] or 0),
                        "last_hit_at": str(row["last_hit_at"] or ""),
                    }
                    for row in top_rows
                ],
            }
        finally:
            conn.close()


@router.get("/status")
async def plugin_status():
    """返回插件当前最小状态。

    第一版只返回：
    - 数据目录位置
    - 文档目录位置
    - 当前 Markdown 文件数量
    - 最近一次上传时间（若存在）
    """
    plugin = _get_plugin()
    return plugin.get_status()


@router.post("/files/upload")
async def upload_markdown(
    files: list[UploadFile] = File(...),
    workspace_root: str = Form(default=""),
):
    """接收并保存多个 Markdown 文件。

    约束保持极简：
    1. 只接受 `.md` 扩展名；
    2. 直接保存原文件，不在上传阶段做切片或建索引；
    3. 若同名文件重复上传，则覆盖旧文件，降低骨架阶段的交互复杂度。
    """
    plugin = _get_plugin()
    return await plugin.save_markdown_files(files, workspace_root)


@router.get("/files")
async def list_markdown_files():
    """列出当前知识库中的 Markdown 文件。"""
    plugin = _get_plugin()
    return plugin.list_files()


@router.post("/files/bind-workspace")
async def bind_markdown_file_workspace(payload: dict = Body(...)):
    """为单个 Markdown 文件绑定工作目录。"""
    plugin = _get_plugin()
    return plugin.bind_file_workspace(
        str((payload or {}).get("file_name", "") or ""),
        str((payload or {}).get("workspace_root", "") or ""),
        str((payload or {}).get("doc_id", "") or ""),
    )


@router.delete("/files/{name}")
async def delete_markdown_file(name: str):
    """删除指定 Markdown 文件，并同步移除其索引条目。"""
    plugin = _get_plugin()
    return plugin.delete_file(name)


@router.post("/files/delete")
async def delete_markdown_file_by_payload(payload: dict = Body(...)):
    """按 doc_id 或 (file_name, workspace_root) 删除 Markdown 文件。"""
    plugin = _get_plugin()
    return plugin.delete_file(
        str((payload or {}).get("file_name", "") or ""),
        str((payload or {}).get("workspace_root", "") or ""),
        str((payload or {}).get("doc_id", "") or ""),
    )


@router.post("/rebuild")
async def rebuild_index():
    """全量重建知识库索引。"""
    plugin = _get_plugin()
    return await plugin.rebuild_index()


@router.post("/rebuild-file")
async def rebuild_single_file(payload: dict = Body(...)):
    """只重建单个 Markdown 文件。"""
    plugin = _get_plugin()
    return await plugin.rebuild_file(
        str((payload or {}).get("file_name", "") or ""),
        str((payload or {}).get("workspace_root", "") or ""),
        str((payload or {}).get("doc_id", "") or ""),
    )


@router.post("/sync")
async def sync_markdown_kb(payload: dict = Body(default={})):
    """按 docs 目录和索引状态做增量同步。"""
    plugin = _get_plugin()
    return await plugin.sync_index(bool((payload or {}).get("apply", False)))


@router.post("/query")
async def query_markdown_kb(payload: dict = Body(...)):
    """执行纯检索，返回最相关的 chunk 列表。"""
    plugin = _get_plugin()
    return await plugin.query(payload)


@router.post("/ask")
async def ask_markdown_kb(payload: dict = Body(...)):
    """执行检索增强问答，返回答案和引用片段。"""
    plugin = _get_plugin()
    return await plugin.ask(payload)


@router.post("/learn")
async def learn_markdown_kb(payload: dict = Body(...)):
    """根据客户端 Hook 上送的材料提炼知识并写回知识库。"""
    plugin = _get_plugin()
    return await plugin.learn(payload)


@router.post("/scan-sessions")
async def scan_sessions_route(payload: dict = Body(...)):
    """扫描客户端本地会话文件，生成知识并更新记忆。

    请求参数：
    - since_hours (float): 扫描多久以内的会话，默认 24 小时
    - max_sessions (int): 最多处理多少个会话，默认 5
    - learn_enabled (bool): 是否生成 .learn.md 知识，默认 true
    - memory_enabled (bool): 是否做交叉验证 boost，默认 true
    """
    plugin = _get_plugin()
    return await plugin.scan_sessions(
        since_hours=float((payload or {}).get("since_hours", 24)),
        max_sessions=int((payload or {}).get("max_sessions", 5)),
        learn_enabled=bool((payload or {}).get("learn_enabled", True)),
        memory_enabled=bool((payload or {}).get("memory_enabled", True)),
    )


@router.post("/clear")
async def clear_markdown_kb(payload: dict = Body(default={})):
    """清空索引，并可选删除原始 Markdown 文档。"""
    plugin = _get_plugin()
    return plugin.clear_index(bool((payload or {}).get("delete_docs", False)))


@router.get("/health")
async def markdown_kb_health():
    """返回知识库健康/漂移检查结果。"""
    plugin = _get_plugin()
    return plugin.health_check()


@router.get("/memory-stats")
async def markdown_kb_memory_stats():
    """返回记忆系统详细统计，包含按文档聚合和 top-N 高记忆 chunk。"""
    plugin = _get_plugin()
    return plugin.get_memory_stats()


class Plugin(PluginBase):
    """Markdown 知识库最小应用插件。"""

    router = router

    async def on_load(self):
        """初始化本地数据目录。

        数据目录默认放在 `~/.akm/markdown_kb/`，避免项目级示例插件把用户上传内容写回仓库。
        """
        global _plugin_instance
        _plugin_instance = self
        self._ensure_runtime_ready()
        self.logger.info("[markdown_kb] 数据目录已就绪: %s", self._data_root)

    def _ensure_runtime_ready(self) -> None:
        if getattr(self, "_store", None) is not None:
            return
        if not getattr(self, "name", ""):
            return
        self._data_root = self._resolve_data_root()
        self._docs_dir = self._data_root / "docs"
        self._index_store_dir = self._data_root / "index_store"
        self._file_bindings_path = self._data_root / "file_bindings.json"
        self._doc_manifest_path = self._data_root / "doc_manifest.json"
        self._learn_records_path = self._data_root / "learn_records.json"
        self._scanned_sessions_path = self._data_root / "scanned_sessions.json"
        self._organizer_state_path = self._data_root / "organizer_state.json"
        self._organize_running = False
        self._index_path = self._index_store_dir / "index.json"
        self._docs_dir.mkdir(parents=True, exist_ok=True)
        self._index_store_dir.mkdir(parents=True, exist_ok=True)
        self._store = self._create_index_store()
        self._embedding_cache_revision = None
        self._embedding_cache_documents = []
        self._bm25_cache_revision = None
        self._bm25_cache_documents = []
        self._bm25_cache_stats = None
        self._jieba3_warned_unavailable = False
        self._jieba3_small_tokenizer = None
        self._jieba3_small_tokenizer_failed = False
        self._query_embedding_cache = OrderedDict()
        self._query_result_cache = OrderedDict()

    def _load_doc_manifest(self) -> list[dict]:
        """读取文档清单；若不存在则从现有 docs 和旧绑定表自动迁移。"""
        manifest_path = getattr(self, "_doc_manifest_path", None)
        if manifest_path is not None and manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text("utf-8"))
            except Exception:
                data = []
            if isinstance(data, list):
                entries = [self._normalize_doc_entry(item) for item in data if isinstance(item, dict)]
                return self._merge_unmanaged_docs_into_manifest(entries)

        entries = []
        bindings = self._load_file_bindings()
        for path in sorted(self._docs_dir.glob("*.md")):
            file_name = path.name
            workspace_root = self._normalize_workspace_root(bindings.get(file_name) or "")
            entries.append(self._build_doc_entry(file_name, workspace_root, file_name))
        if manifest_path is not None:
            self._save_doc_manifest(entries)
        return entries

    def _merge_unmanaged_docs_into_manifest(self, entries: list[dict]) -> list[dict]:
        """把尚未登记到 manifest 的历史物理文件自动补进清单。"""
        known_storage_names = {str(item.get("storage_name") or "") for item in entries}
        bindings = self._load_file_bindings()
        changed = False
        for path in sorted(self._docs_dir.glob("*.md")):
            if path.name in known_storage_names:
                continue
            workspace_root = self._normalize_workspace_root(bindings.get(path.name) or "")
            entries.append(self._build_doc_entry(path.name, workspace_root, path.name))
            changed = True
        if changed and getattr(self, "_doc_manifest_path", None) is not None:
            self._save_doc_manifest(entries)
        return entries

    def _save_doc_manifest(self, entries: list[dict]) -> None:
        """写入文档清单。"""
        manifest_path = getattr(self, "_doc_manifest_path", None)
        if manifest_path is None:
            raise RuntimeError("markdown_kb 文档清单尚未初始化")
        normalized = [self._normalize_doc_entry(item) for item in entries if isinstance(item, dict)]
        manifest_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), "utf-8")

    def _build_doc_entry(self, file_name: str, workspace_root: str, storage_name: str) -> dict:
        """构造文档清单条目。"""
        safe_name = Path(str(file_name or "")).name
        normalized_workspace = self._normalize_workspace_root(workspace_root)
        return {
            "doc_id": self._make_doc_id(safe_name, normalized_workspace),
            "file_name": safe_name,
            "workspace_root": normalized_workspace,
            "storage_name": Path(str(storage_name or safe_name)).name,
        }

    def _normalize_doc_entry(self, entry: dict) -> dict:
        """规整文档清单条目，兼容历史数据。"""
        file_name = Path(str((entry or {}).get("file_name") or "")).name
        workspace_root = self._normalize_workspace_root((entry or {}).get("workspace_root") or "")
        storage_name = Path(str((entry or {}).get("storage_name") or file_name)).name
        doc_id = str((entry or {}).get("doc_id") or self._make_doc_id(file_name, workspace_root))
        return {
            "doc_id": doc_id,
            "file_name": file_name,
            "workspace_root": workspace_root,
            "storage_name": storage_name,
        }

    def _make_doc_id(self, file_name: str, workspace_root: str) -> str:
        """用“工作目录 + 文件名”生成稳定文档 ID。"""
        basis = f"{self._normalize_workspace_root(workspace_root)}::{Path(str(file_name or '')).name}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()

    def _storage_name_for_entry(self, file_name: str, workspace_root: str) -> str:
        """为不同工作目录下的同名逻辑文档生成唯一物理文件名。"""
        safe_name = Path(str(file_name or "")).name
        normalized_workspace = self._normalize_workspace_root(workspace_root)
        if not normalized_workspace:
            return safe_name
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix or ".md"
        short_hash = hashlib.sha1(normalized_workspace.encode("utf-8")).hexdigest()[:10]
        return f"{stem}__{short_hash}{suffix}"

    def _doc_storage_path(self, entry: dict) -> Path:
        """解析文档条目的实际物理存储路径。"""
        return self._docs_dir / Path(str((entry or {}).get("storage_name") or "")).name

    def _find_doc_entry(self, *, doc_id: str = "", file_name: str = "", workspace_root: str = "") -> dict:
        """按 doc_id 或 (file_name, workspace_root) 查找单个文档条目。"""
        entries = self._load_doc_manifest()
        normalized_doc_id = str(doc_id or "").strip()
        normalized_name = Path(str(file_name or "")).name
        normalized_workspace = self._normalize_workspace_root(workspace_root)

        if normalized_doc_id:
            for entry in entries:
                if entry.get("doc_id") == normalized_doc_id:
                    return entry
            raise HTTPException(status_code=404, detail="文档不存在")

        if not normalized_name:
            raise HTTPException(status_code=400, detail="file_name 不能为空")

        matches = [entry for entry in entries if entry.get("file_name") == normalized_name]
        if normalized_workspace or not matches:
            matches = [entry for entry in matches if entry.get("workspace_root") == normalized_workspace]
        if not matches:
            raise HTTPException(status_code=404, detail="文档不存在")
        if len(matches) > 1:
            raise HTTPException(status_code=409, detail="存在同名文档，请补充 workspace_root 或 doc_id")
        return matches[0]

    def _load_file_bindings(self) -> dict[str, str]:
        """读取文件级工作目录绑定表。"""
        bindings_path = getattr(self, "_file_bindings_path", None)
        if bindings_path is None:
            return {}
        if not bindings_path.exists():
            return {}
        try:
            data = json.loads(bindings_path.read_text("utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        result = {}
        for key, value in data.items():
            file_name = Path(str(key or "")).name
            if not file_name:
                continue
            result[file_name] = self._normalize_workspace_root(value)
        return result

    def _save_file_bindings(self, bindings: dict[str, str]) -> None:
        """写入文件级工作目录绑定表。"""
        bindings_path = getattr(self, "_file_bindings_path", None)
        if bindings_path is None:
            raise RuntimeError("markdown_kb 文件绑定存储尚未初始化")
        normalized = {}
        for key, value in (bindings or {}).items():
            file_name = Path(str(key or "")).name
            if not file_name:
                continue
            normalized[file_name] = self._normalize_workspace_root(value)
        bindings_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), "utf-8")

    def _resolve_file_workspace_root(self, file_name: str, settings: dict | None = None) -> str:
        """解析单个文件最终使用的工作目录绑定。"""
        safe_name = Path(str(file_name or "")).name
        bindings = self._load_file_bindings()
        if safe_name in bindings:
            return self._normalize_workspace_root(bindings.get(safe_name) or "")
        return ""

    def _create_index_store(self) -> IndexStore:
        """创建当前使用的索引 backend。

        当前默认固定使用 SQLite `kb.db` backend。

        说明：
         1. 当前主路径是 `kb.db + sqlite-vec 优先粗召回 + Python 自动回退`；
        2. `VectorLiteDbIndexStore` 仍保留在代码里，作为更早期骨架的参考；
        3. 对外依然不暴露“后端切换”配置，避免出现“能配但其实不会生效”的伪配置。
        """
        self._kb_db_path = self._index_store_dir / "kb.db"
        return SqliteKbIndexStore(self._kb_db_path, self.logger)

    def _resolve_data_root(self) -> Path:
        """解析插件数据根目录。

        设计选择：
        1. 默认落到用户目录下的 `~/.akm/markdown_kb`；
        2. 这里不把上传内容写到仓库内，避免项目级样例插件污染 git worktree；
        3. 当前阶段不再把 data_dir 暴露成用户配置项，避免和插件固定目录心智冲突。
        """
        return (Path.home() / ".akm" / self.name).resolve()

    def get_status(self) -> dict:
        """汇总插件当前状态，供页面和 API 直接复用。"""
        self._ensure_runtime_ready()
        docs = sorted(self._docs_dir.glob("*.md"))
        index_stats = self._store.stats()
        health = self.health_check()
        latest_updated_at = None
        if docs:
            latest_mtime = max(p.stat().st_mtime for p in docs)
            latest_updated_at = datetime.fromtimestamp(latest_mtime, tz=timezone.utc).replace(microsecond=0).isoformat()

        return {
            "ok": True,
            "plugin": self.name,
            "data_dir": str(self._data_root),
            "docs_dir": str(self._docs_dir),
            "index_store_dir": str(self._index_store_dir),
            "index_backend": getattr(self._store, "backend_name", "unknown"),
            "doc_count": len(docs),
            "chunk_count": index_stats.get("document_count", 0),
            "last_updated_at": latest_updated_at,
            "last_rebuilt_at": index_stats.get("last_rebuilt_at"),
            "embedding_model": index_stats.get("embedding_model"),
            "vec_available": bool(index_stats.get("vec_available")),
            "vec_ready": bool(index_stats.get("vec_ready")),
            "vec_enabled": bool(index_stats.get("vec_enabled")),
            "vec_version": index_stats.get("vec_version") or "",
            "embedding_dim": _safe_int(index_stats.get("embedding_dim"), 0),
            "vector_retrieval_backend": self._vector_retrieval_backend_label(),
            "vector_compute_backend": self._vector_compute_backend_label(),
            "health": health,
            "ready": True,
        }

    def list_files(self) -> dict:
        """返回当前知识库中的 Markdown 文件列表。

        这里同时附带是否已进入索引的标记，方便前端后续区分“已上传但未重建”与“已入库”。
        """
        self._ensure_runtime_ready()
        indexed_documents = self._store.list_documents()
        chunk_counts_by_doc: dict[str, int] = {}
        for item in indexed_documents:
            doc_id = str(item.get("doc_id") or "")
            if not doc_id:
                continue
            chunk_counts_by_doc[doc_id] = chunk_counts_by_doc.get(doc_id, 0) + 1

        files = []
        for entry in self._load_doc_manifest():
            path = self._doc_storage_path(entry)
            if not path.exists():
                continue
            stat = path.stat()
            payload = path.read_bytes()
            files.append({
                "doc_id": entry.get("doc_id") or "",
                "file_name": entry.get("file_name") or path.name,
                "size_bytes": stat.st_size,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat(),
                "indexed": chunk_counts_by_doc.get(entry.get("doc_id") or "", 0) > 0,
                "chunk_count": chunk_counts_by_doc.get(entry.get("doc_id") or "", 0),
                "workspace_root": self._normalize_workspace_root(entry.get("workspace_root") or ""),
            })

        return {
            "ok": True,
            "files": files,
            "count": len(files),
        }

    def delete_file(self, name: str, workspace_root: str = "", doc_id: str = "") -> dict:
        """删除指定 Markdown 文件，并同步清理索引中对应的 chunks。"""
        self._ensure_runtime_ready()
        entry = self._find_doc_entry(doc_id=doc_id, file_name=name, workspace_root=workspace_root)
        safe_name = Path(str(entry.get("file_name") or "")).name
        if not safe_name or not safe_name.lower().endswith(".md"):
            raise HTTPException(status_code=400, detail="仅支持删除 .md 文件")

        target = self._doc_storage_path(entry)
        if not target.exists():
            raise HTTPException(status_code=404, detail="文件不存在")

        target.unlink()

        manifest = [item for item in self._load_doc_manifest() if str(item.get("doc_id") or "") != str(entry.get("doc_id") or "")]
        self._save_doc_manifest(manifest)

        existing_documents = self._store.list_documents()
        removed_count = sum(1 for item in existing_documents if str(item.get("doc_id") or "") == str(entry.get("doc_id") or ""))
        current_documents = [item for item in existing_documents if str(item.get("doc_id") or "") != str(entry.get("doc_id") or "")]
        self._store.replace_all(current_documents, {
            "last_rebuilt_at": _utc_now_iso(),
            "embedding_model": self._settings()["embedding_model"],
        })
        self._invalidate_embedding_cache()

        return {
            "ok": True,
            "doc_id": entry.get("doc_id") or "",
            "file_name": safe_name,
            "removed_chunks": removed_count,
        }

    def bind_file_workspace(self, file_name: str, workspace_root: str, doc_id: str = "") -> dict:
        """为单个文件绑定工作目录。

        规则：
        1. 文件必须已存在于 `docs_dir`；
        2. 同一工作目录下文件名天然唯一，因为绑定键就是 `file_name`；
        3. 绑定修改后不会自动重建索引，需要调用方显式执行 `rebuild-file` / `sync` / `rebuild`。
        """
        self._ensure_runtime_ready()
        entry = self._find_doc_entry(doc_id=doc_id, file_name=file_name, workspace_root="")
        safe_name = Path(str(entry.get("file_name") or "")).name
        if not safe_name or not safe_name.lower().endswith(".md"):
            raise HTTPException(status_code=400, detail="仅支持为 .md 文件绑定工作目录")

        target = self._doc_storage_path(entry)
        if not target.exists():
            raise HTTPException(status_code=404, detail="文件不存在")

        normalized_workspace = self._normalize_workspace_root(workspace_root)
        manifest = self._load_doc_manifest()
        updated = []
        for item in manifest:
            if str(item.get("doc_id") or "") == str(entry.get("doc_id") or ""):
                updated.append({
                    "doc_id": str(item.get("doc_id") or ""),
                    "file_name": safe_name,
                    "workspace_root": normalized_workspace,
                    "storage_name": str(item.get("storage_name") or safe_name),
                })
            else:
                updated.append(item)
        self._save_doc_manifest(updated)

        return {
            "ok": True,
            "doc_id": entry.get("doc_id") or "",
            "file_name": safe_name,
            "workspace_root": normalized_workspace,
            "needs_rebuild": True,
        }

    def clear_index(self, delete_docs: bool = False) -> dict:
        """清空索引，并可选删除原始 Markdown 文档。"""
        self._ensure_runtime_ready()
        clear_result = self._store.clear()
        self._invalidate_embedding_cache()
        removed_docs = 0
        if delete_docs:
            for path in sorted(self._docs_dir.glob("*.md")):
                path.unlink()
                removed_docs += 1
        return {
            "ok": True,
            "removed_chunks": clear_result.get("removed_chunks", 0),
            "removed_docs": removed_docs,
            "delete_docs": bool(delete_docs),
        }

    def _get_doc_id_for_path(self, path: Path) -> str:
        """生成稳定 doc_id。"""
        return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()

    def _build_doc_index_map(self) -> dict[str, dict]:
        """把当前索引中的 chunk 聚合成文件级视图，便于增量判断。"""
        self._ensure_runtime_ready()
        grouped: dict[str, dict] = {}
        for item in self._store.list_documents():
            doc_id = str(item.get("doc_id") or "")
            if not doc_id:
                continue
            current = grouped.get(doc_id)
            if current is None:
                current = {
                    "doc_id": doc_id,
                    "file_name": item.get("file_name") or "",
                    "file_path": item.get("file_path") or "",
                    "workspace_root": item.get("workspace_root") or "",
                    "chunk_count": 0,
                    "content_hashes": [],
                }
                grouped[doc_id] = current
            current["chunk_count"] += 1
            current["content_hashes"].append(str(item.get("content_hash") or ""))
        for value in grouped.values():
            value["content_hashes"].sort()
            value["aggregate_hash"] = hashlib.sha256("|".join(value["content_hashes"]).encode("utf-8")).hexdigest()
        return grouped

    def _scan_doc_sources(self) -> dict[str, dict]:
        """扫描当前 docs 目录，生成文件级源数据快照。"""
        self._ensure_runtime_ready()
        settings = self._settings()
        result = {}
        for entry in self._load_doc_manifest():
            path = self._doc_storage_path(entry)
            if not path.exists():
                continue
            payload = path.read_bytes()
            doc_id = str(entry.get("doc_id") or self._get_doc_id_for_path(path))
            preview_chunks = self._chunk_markdown_file(path, settings, entry)
            chunk_hashes = sorted(str(item.get("content_hash") or "") for item in preview_chunks)
            aggregate_hash = hashlib.sha256("|".join(chunk_hashes).encode("utf-8")).hexdigest() if chunk_hashes else hashlib.sha256(b"").hexdigest()
            result[doc_id] = {
                "doc_id": doc_id,
                "file_name": entry.get("file_name") or path.name,
                "file_path": str(path.resolve()),
                "content_hash": hashlib.sha256(payload).hexdigest(),
                "aggregate_hash": aggregate_hash,
                "chunk_count": len(preview_chunks),
                "size_bytes": len(payload),
                "workspace_root": self._normalize_workspace_root(entry.get("workspace_root") or ""),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat(),
            }
        return result

    def preview_sync(self) -> dict:
        """只做增量判断，不真正写索引。"""
        self._ensure_runtime_ready()
        indexed_docs = self._build_doc_index_map()
        source_docs = self._scan_doc_sources()

        added = []
        changed = []
        unchanged = []
        removed = []

        for doc_id, source in source_docs.items():
            indexed = indexed_docs.get(doc_id)
            if indexed is None:
                added.append(source)
                continue
            if source["file_name"] != indexed.get("file_name"):
                changed.append(source)
                continue
            if source.get("workspace_root") != (indexed.get("workspace_root") or ""):
                changed.append(source)
                continue
            if source["aggregate_hash"] != indexed.get("aggregate_hash"):
                changed.append(source)
                continue
            unchanged.append(source)

        for doc_id, indexed in indexed_docs.items():
            if doc_id not in source_docs:
                removed.append(indexed)

        return {
            "ok": True,
            "added": added,
            "changed": changed,
            "removed": removed,
            "unchanged": unchanged,
            "summary": {
                "added": len(added),
                "changed": len(changed),
                "removed": len(removed),
                "unchanged": len(unchanged),
            },
        }

    def health_check(self) -> dict:
        """返回更偏运维视角的健康/漂移信息，含记忆状态指标。"""
        self._ensure_runtime_ready()
        preview = self.preview_sync()
        summary = preview.get("summary", {})
        issues = []
        if summary.get("added", 0) > 0:
            issues.append("存在未入索引的新文件")
        if summary.get("changed", 0) > 0:
            issues.append("存在内容已变化但尚未同步的文件")
        if summary.get("removed", 0) > 0:
            issues.append("存在索引中残留但 docs 中已删除的文件")

        memory_stats = self._store.get_all_memory_stats() or {}

        return {
            "ok": True,
            "in_sync": not issues,
            "issues": issues,
            "summary": summary,
            "memory": {
                "chunk_count": memory_stats.get("memory_chunk_count", 0),
                "avg_value": memory_stats.get("memory_avg_value", 0.0),
                "high_value_count": memory_stats.get("memory_high_value_count", 0),
                "total_hits": memory_stats.get("memory_total_hits", 0),
            },
        }

    def get_memory_stats(self) -> dict:
        """返回记忆系统详细统计，供前端可视化面板使用。"""
        self._ensure_runtime_ready()
        return self._store.get_memory_detail_stats()

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """计算两个向量的余弦相似度，返回 0~1 之间的值。"""
        if not a or not b or len(a) != len(b):
            return 0.0
        import math
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return max(0.0, min(1.0, dot / (norm_a * norm_b)))

    def _find_nearest_chunk_for_dedup(self, embedding_vector: list[float]) -> dict | None:
        """查找与给定 embedding 最相似的存量 chunk，用于去重合并判断。

        优先使用 sqlite-vec 全局向量搜索（在所有 workspace 中查找），
        不可用时返回 None，调用方应回退到 _cosine_similarity 遍历存量 chunks。

        返回值：{"chunk_id": str, "cosine_similarity": float} 或 None。
        """
        if not embedding_vector or not self._store:
            return None
        nearest = self._store.find_nearest_chunk(embedding_vector)
        if nearest is None:
            return None
        distance = _safe_float(nearest.get("distance"), 2.0)
        cosine_sim = _sqlite_vec_distance_to_score(distance)
        return {"chunk_id": str(nearest.get("chunk_id") or ""), "cosine_similarity": cosine_sim}

    async def rebuild_file(self, file_name: str, workspace_root: str = "", doc_id: str = "") -> dict:
        """只重建单个文件的 chunks 与向量。

        新增去重合并能力：新 chunk 与存量 chunk 的向量余弦相似度超过
        dedup_similarity_threshold 时，不再新增 chunk，改为 boost 已有 chunk 的记忆值。
        """
        self._ensure_runtime_ready()
        entry = self._find_doc_entry(doc_id=doc_id, file_name=file_name, workspace_root=workspace_root)
        safe_name = Path(str(entry.get("file_name") or "")).name
        if not safe_name or not safe_name.lower().endswith(".md"):
            raise HTTPException(status_code=400, detail="仅支持重建 .md 文件")

        target = self._doc_storage_path(entry)
        if not target.exists():
            raise HTTPException(status_code=404, detail="文件不存在")

        settings = self._settings()
        chunks = self._chunk_markdown_file(target, settings, entry)
        embeddings = await self._embed_texts([item["chunk_text"] for item in chunks], settings["embedding_model"])
        if len(embeddings) != len(chunks):
            raise HTTPException(status_code=502, detail="embedding 返回数量与 chunks 数量不一致")

        now = _utc_now_iso()
        for item, embedding in zip(chunks, embeddings):
            item["embedding"] = embedding
            item["indexed_at"] = now

        current_documents = [item for item in self._store.list_documents() if str(item.get("doc_id") or "") != str(entry.get("doc_id") or "")]

        # 嵌入向量去重合并：新 chunk 与存量 chunk 做余弦相似度比较
        dedup_threshold = _safe_float(settings.get("dedup_similarity_threshold"), 0.92)
        dedup_boosts: dict[str, float] = {}
        dedup_skipped = 0
        dedup_merged = 0
        if dedup_threshold > 0 and current_documents:
            unique_chunks: list[dict] = []
            for chunk in chunks:
                nearest = self._find_nearest_chunk_for_dedup(chunk["embedding"])
                # 优先走 sqlite-vec，不可用时回退到 brute-force 余弦相似度
                if nearest is not None and nearest["cosine_similarity"] >= dedup_threshold:
                    existing = next(
                        (c for c in current_documents if str(c.get("id") or "") == nearest["chunk_id"]),
                        None,
                    )
                    merged_text = None
                    if existing and isinstance(existing.get("chunk_text"), str) and chunk.get("chunk_text"):
                        merged_text = await self._merge_chunks_via_llm(
                            existing["chunk_text"], chunk["chunk_text"], settings["chat_model"],
                        )
                    if merged_text:
                        new_emb = await self._embed_texts([merged_text], settings["embedding_model"])
                        if new_emb:
                            existing["chunk_text"] = merged_text
                            existing["embedding"] = new_emb[0]
                            existing["indexed_at"] = now
                            dedup_merged += 1
                            self.logger.info(
                                "[markdown_kb] 去重合并文本（vec），chunk %s 已更新",
                                nearest["chunk_id"],
                            )
                    dedup_boosts[nearest["chunk_id"]] = max(
                        dedup_boosts.get(nearest["chunk_id"], 0.0), 0.10,
                    )
                    dedup_skipped += 1
                    self.logger.info(
                        "[markdown_kb] 去重合并 chunk（vec），相似度 %.3f >= %.2f，boost 已有 chunk %s",
                        nearest["cosine_similarity"], dedup_threshold, nearest["chunk_id"],
                    )
                elif nearest is None:
                    # sqlite-vec 不可用，遍历存量 chunks 做余弦相似度比较
                    best_sim = 0.0
                    best_cid = ""
                    for existing in current_documents:
                        emb = existing.get("embedding")
                        if isinstance(emb, list) and emb:
                            sim = self._cosine_similarity(chunk["embedding"], emb)
                            if sim > best_sim:
                                best_sim = sim
                                best_cid = str(existing.get("id") or "")
                    if best_sim >= dedup_threshold and best_cid:
                        existing = next(
                            (c for c in current_documents if str(c.get("id") or "") == best_cid),
                            None,
                        )
                        merged_text = None
                        if existing and isinstance(existing.get("chunk_text"), str) and chunk.get("chunk_text"):
                            merged_text = await self._merge_chunks_via_llm(
                                existing["chunk_text"], chunk["chunk_text"], settings["chat_model"],
                            )
                        if merged_text:
                            new_emb = await self._embed_texts([merged_text], settings["embedding_model"])
                            if new_emb:
                                existing["chunk_text"] = merged_text
                                existing["embedding"] = new_emb[0]
                                existing["indexed_at"] = now
                                dedup_merged += 1
                                self.logger.info(
                                    "[markdown_kb] 去重合并文本（余弦），chunk %s 已更新",
                                    best_cid,
                                )
                        dedup_boosts[best_cid] = max(dedup_boosts.get(best_cid, 0.0), 0.10)
                        dedup_skipped += 1
                        self.logger.info(
                            "[markdown_kb] 去重合并 chunk（余弦），相似度 %.3f >= %.2f，boost 已有 chunk %s",
                            best_sim, dedup_threshold, best_cid,
                        )
                    else:
                        unique_chunks.append(chunk)
                else:
                    unique_chunks.append(chunk)
        else:
            unique_chunks = chunks

        current_documents.extend(unique_chunks)
        self._store.replace_all(current_documents, {
            "last_rebuilt_at": now,
            "embedding_model": settings["embedding_model"],
        })
        self._invalidate_embedding_cache()

        # 应用去重记忆 boost
        if dedup_boosts and settings.get("memory_enabled") and self._store:
            batch = {
                cid: {"memory_value": boost, "hit_count_delta": 1, "last_hit_at": now}
                for cid, boost in dedup_boosts.items()
            }
            self._store.update_memory(batch)

        return {
            "ok": True,
            "doc_id": entry.get("doc_id") or "",
            "file_name": safe_name,
            "chunk_count": len(unique_chunks),
            "chunk_ids": [str(c.get("id", "")) for c in unique_chunks],
            "last_rebuilt_at": now,
            "dedup_skipped": dedup_skipped,
            "dedup_boosted": len(dedup_boosts),
            "dedup_merged": dedup_merged,
        }

    async def sync_index(self, apply_changes: bool = False) -> dict:
        """执行或预览增量同步。

        第一版只基于 docs 目录与索引中文件级聚合信息做最小判断：
        - 新文件 -> added
        - hash 变化 -> changed
        - 索引有但 docs 没有 -> removed
        """
        self._ensure_runtime_ready()
        preview = self.preview_sync()
        if not apply_changes:
            preview["applied"] = False
            return preview

        applied = {
            "added": [],
            "changed": [],
            "removed": [],
        }

        for item in preview["removed"]:
            self.delete_file(str(item.get("file_name") or ""), str(item.get("workspace_root") or ""), str(item.get("doc_id") or ""))
            applied["removed"].append(item.get("file_name"))

        for item in preview["added"]:
            result = await self.rebuild_file(str(item.get("file_name") or ""), str(item.get("workspace_root") or ""), str(item.get("doc_id") or ""))
            applied["added"].append(result.get("file_name"))

        for item in preview["changed"]:
            result = await self.rebuild_file(str(item.get("file_name") or ""), str(item.get("workspace_root") or ""), str(item.get("doc_id") or ""))
            applied["changed"].append(result.get("file_name"))

        latest = self.preview_sync()
        latest["applied"] = True
        latest["applied_changes"] = applied
        return latest

    async def save_markdown_file(self, file: UploadFile, workspace_root: str = "") -> dict:
        """保存单个上传的 Markdown 文件并返回最小元信息。"""
        self._ensure_runtime_ready()
        filename = (file.filename or "").strip()
        if not filename:
            raise HTTPException(status_code=400, detail="文件名不能为空")
        if not filename.lower().endswith(".md"):
            raise HTTPException(status_code=400, detail="仅支持 .md 文件")

        safe_name = Path(filename).name
        if safe_name in ("", ".", ".."):
            raise HTTPException(status_code=400, detail="非法文件名")

        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="上传内容不能为空")

        return self._save_markdown_payload(safe_name, payload, workspace_root)

    def _save_markdown_payload(self, file_name: str, payload: bytes, workspace_root: str = "") -> dict:
        """把 Markdown 二进制内容写入 docs 目录，并维护 manifest。

        这里抽成独立内部方法的原因是：
        1. 上传接口与 `/learn` 都需要复用同一套“逻辑文件名 + workspace 绑定 + manifest”落盘规则；
        2. 这样可以避免学习入库为了复用上传能力而再伪造一层 HTTP 文件对象；
        3. 同时继续保证所有 Markdown 文档入口最终都走同一套持久化语义。
        """
        self._ensure_runtime_ready()
        safe_name = Path(str(file_name or "")).name
        if safe_name in ("", ".", ".."):
            raise HTTPException(status_code=400, detail="非法文件名")
        if not safe_name.lower().endswith(".md"):
            raise HTTPException(status_code=400, detail="仅支持 .md 文件")
        if not payload:
            raise HTTPException(status_code=400, detail="上传内容不能为空")

        normalized_workspace = self._normalize_workspace_root(workspace_root)
        manifest = self._load_doc_manifest()
        for item in manifest:
            if item.get("file_name") == safe_name and self._normalize_workspace_root(item.get("workspace_root") or "") == normalized_workspace:
                entry = self._normalize_doc_entry(item)
                break
        else:
            storage_name = self._storage_name_for_entry(safe_name, normalized_workspace)
            entry = self._build_doc_entry(safe_name, normalized_workspace, storage_name)
            manifest.append(entry)
        target = self._doc_storage_path(entry)
        target.write_bytes(payload)
        self._save_doc_manifest(manifest)

        sha256 = hashlib.sha256(payload).hexdigest()
        return {
            "ok": True,
            "doc_id": entry.get("doc_id") or "",
            "file_name": safe_name,
            "saved_path": str(target),
            "size_bytes": len(payload),
            "sha256": sha256,
            "workspace_root": normalized_workspace,
            "uploaded_at": _utc_now_iso(),
        }

    async def save_markdown_files(self, files: list[UploadFile], workspace_root: str = "") -> dict:
        """批量保存 Markdown 文件。

        设计说明：
        1. 只要请求里带了一个非法文件，就直接返回 400，避免出现“半成功半失败”后用户不清楚哪些文件已落盘；
        2. 同名文件仍保持覆盖语义，和单文件上传保持一致；
        3. 返回统一摘要，前端可以直接展示“共上传 N 个”的结果。
        """
        self._ensure_runtime_ready()
        if not files:
            raise HTTPException(status_code=400, detail="至少上传一个文件")

        results = []
        for file in files:
            results.append(await self.save_markdown_file(file, workspace_root))

        return {
            "ok": True,
            "count": len(results),
            "files": results,
        }

    async def rebuild_index(self) -> dict:
        """全量重建索引。

        当前实现选择“全量”而不是增量，原因有两个：
        1. 第一版目标是先验证切片和检索质量；
        2. 全量语义最简单，排障成本最低。
        """
        self._ensure_runtime_ready()
        manifest = [entry for entry in self._load_doc_manifest() if self._doc_storage_path(entry).exists()]
        settings = self._settings()

        if not manifest:
            empty_index = self._store.replace_all([], {
                "last_rebuilt_at": _utc_now_iso(),
                "embedding_model": settings["embedding_model"],
            })
            return {
                "ok": True,
                "doc_count": 0,
                "chunk_count": 0,
                "last_rebuilt_at": empty_index["last_rebuilt_at"],
            }
        
        chunks = []
        for entry in manifest:
            path = self._doc_storage_path(entry)
            chunks.extend(self._chunk_markdown_file(path, settings, entry))

        embeddings = await self._embed_texts(
            [item["chunk_text"] for item in chunks],
            settings["embedding_model"],
        )
        if len(embeddings) != len(chunks):
            raise HTTPException(status_code=502, detail="embedding 返回数量与 chunks 数量不一致")

        now = _utc_now_iso()
        documents = []
        for item, embedding in zip(chunks, embeddings):
            item["embedding"] = embedding
            item["indexed_at"] = now
            documents.append(item)

        self._store.replace_all(documents, {
            "last_rebuilt_at": now,
            "embedding_model": settings["embedding_model"],
        })
        self._invalidate_embedding_cache()

        return {
            "ok": True,
            "doc_count": len(manifest),
            "chunk_count": len(documents),
            "last_rebuilt_at": now,
            "embedding_model": settings["embedding_model"],
        }

    async def query(self, payload: dict) -> dict:
        """执行纯检索，不调用 chat。"""
        self._ensure_runtime_ready()
        question = str((payload or {}).get("question", "") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question 不能为空")

        top_k = self._resolve_top_k((payload or {}).get("top_k"))
        settings = self._settings()
        embedding_model = str((payload or {}).get("embedding_model") or settings["embedding_model"]).strip() or settings["embedding_model"]
        reranker_model = str((payload or {}).get("reranker_model") or settings["reranker_model"]).strip()
        project_context = self._extract_project_context(payload)
        selected_doc = self._resolve_selected_doc_scope(payload)
        hits = await self._retrieve(question, top_k, embedding_model, reranker_model, project_context, selected_doc)
        return {
            "ok": True,
            "question": question,
            "top_k": top_k,
            "embedding_model": embedding_model,
            "reranker_model": reranker_model,
            "workspace_root": project_context.get("workspace_root") if isinstance(project_context, dict) else "",
            "selected_doc_id": selected_doc.get("doc_id") if isinstance(selected_doc, dict) else "",
            "hits": hits,
        }

    async def ask(self, payload: dict) -> dict:
        """执行检索增强问答。"""
        self._ensure_runtime_ready()
        question = str((payload or {}).get("question", "") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question 不能为空")

        top_k = self._resolve_top_k((payload or {}).get("top_k"))
        settings = self._settings()
        embedding_model = str((payload or {}).get("embedding_model") or settings["embedding_model"]).strip() or settings["embedding_model"]
        reranker_model = str((payload or {}).get("reranker_model") or settings["reranker_model"]).strip()
        chat_model = str((payload or {}).get("chat_model") or settings["chat_model"]).strip() or settings["chat_model"]
        project_context = self._extract_project_context(payload)
        selected_doc = self._resolve_selected_doc_scope(payload)
        hits = await self._retrieve(question, top_k, embedding_model, reranker_model, project_context, selected_doc)
        answer = await self._generate_answer(question, hits, chat_model)
        return {
            "ok": True,
            "question": question,
            "top_k": top_k,
            "answer": answer,
            "citations": hits,
            "chat_model": chat_model,
            "embedding_model": embedding_model,
            "reranker_model": reranker_model,
            "workspace_root": project_context.get("workspace_root") if isinstance(project_context, dict) else "",
            "selected_doc_id": selected_doc.get("doc_id") if isinstance(selected_doc, dict) else "",
        }

    async def learn(self, payload: dict) -> dict:
        """把一次会话片段归纳为新的 Markdown 知识条目并写回知识库。

        当前实现刻意保持在插件内部闭环，避免把客户端 transcript 解析逻辑耦合进来：
        1. 客户端 Hook 只负责把本轮候选材料送进来；
        2. 服务端统一做校验、归纳、落盘、索引刷新与幂等处理；
        3. 如果模型判断“这一轮没有稳定知识可沉淀”，则返回 `ignored=True`，但不会报错。
        """
        self._ensure_runtime_ready()
        request = self._normalize_learn_request(payload)
        dedupe_key = request["dedupe_key"]
        learn_records = self._load_learn_records()
        existing = learn_records.get(dedupe_key)
        if isinstance(existing, dict):
            return {
                "ok": True,
                "deduped": True,
                "ignored": str(existing.get("status") or "") == "ignored",
                "status": str(existing.get("status") or "completed"),
                "dedupe_key": dedupe_key,
                "doc_id": str(existing.get("doc_id") or ""),
                "file_name": str(existing.get("file_name") or ""),
                "workspace_root": str(existing.get("workspace_root") or ""),
                "learned_at": str(existing.get("learned_at") or existing.get("updated_at") or ""),
            }

        settings = self._settings()
        learn_result = await self._generate_learn_summary(request, settings["chat_model"])
        summary_markdown = str(learn_result.get("summary_markdown") or "").strip()
        should_learn = bool(learn_result.get("should_learn")) and bool(summary_markdown)
        if not should_learn:
            learn_records[dedupe_key] = {
                "status": "ignored",
                "source": request["source"],
                "trigger_phase": request["trigger_phase"],
                "workspace_root": request["workspace_root"],
                "updated_at": _utc_now_iso(),
            }
            self._save_learn_records(learn_records)
            return {
                "ok": True,
                "ignored": True,
                "deduped": False,
                "status": "ignored",
                "dedupe_key": dedupe_key,
                "reason": "no_stable_knowledge",
            }

        title = self._resolve_learn_title(learn_result, request)
        quotes = self._normalize_learn_quotes(learn_result.get("quotes"))
        keywords = [str(k).strip() for k in (learn_result.get("keywords") or []) if str(k).strip()]
        categories = [str(c).strip() for c in (learn_result.get("categories") or []) if str(c).strip()]
        file_name = self._make_learn_file_name(title, dedupe_key, request)
        markdown_text = self._render_learn_document(
            title=title,
            request=request,
            summary_markdown=summary_markdown,
            quotes=quotes,
            keywords=keywords,
            categories=categories,
        )
        saved = self._save_markdown_payload(
            file_name=file_name,
            payload=markdown_text.encode("utf-8"),
            workspace_root=request["workspace_root"],
        )
        rebuilt = await self.rebuild_file(
            file_name=str(saved.get("file_name") or ""),
            workspace_root=request["workspace_root"],
            doc_id=str(saved.get("doc_id") or ""),
        )
        learned_at = _utc_now_iso()

        # 记忆更新：新 chunk 初始记忆 + 交叉验证存量 chunks
        new_chunk_ids: list[str] = rebuilt.get("chunk_ids", [])
        memory_boosted_chunks = 0
        if settings.get("memory_enabled") and new_chunk_ids and self._store:
            # 新 chunk 初始记忆值 (learn_new = 0.30)
            init_batch = {}
            for cid in new_chunk_ids:
                init_batch[cid] = {"memory_value": 0.30, "hit_count_delta": 1, "last_hit_at": learned_at}
            self._store.update_memory(init_batch)

            # 交叉验证：用 summary_markdown 检索存量 chunks，排除本次新生成的
            try:
                query_vector = await self._get_query_embedding(summary_markdown[:2000], settings["embedding_model"])
                all_documents = self._store.list_documents()
                # 排除新生成的 doc 的所有 chunk
                exclude_doc_id = str(saved.get("doc_id") or "")
                existing_docs = [d for d in all_documents if str(d.get("doc_id") or "") != exclude_doc_id]
                if existing_docs:
                    # 用 sqlite-vec 粗召回
                    candidates = self._retrieve_candidates_with_sqlite_vec(
                        query_vector=query_vector,
                        documents=existing_docs,
                        top_k=max(settings["top_k"], 4) * 2,
                        request_workspace=request["workspace_root"],
                    )
                    if candidates:
                        scored = self._score_documents(
                            "", query_vector, candidates,
                            semantic_weight=1.0, keyword_weight=0.0, memory_weight=0.0,
                            category_bonus=0.0, category_list=[],
                            vector_score_overrides={
                                str(c.get("id") or ""): _safe_float(c.get("vector_score"), 0.0)
                                for c in candidates if str(c.get("id") or "")
                            },
                        )
                        # 取向量分 > 0.5 的作为交叉验证命中，排除新 chunk
                        cross_boost = 0.20
                        cross_batch = {}
                        max_score = max((_safe_float(h.get("vector_score"), 0.0) for h in scored), default=1.0) or 1.0
                        for hit in scored:
                            vs = _safe_float(hit.get("vector_score"), 0.0)
                            chunk_id = str(hit.get("id") or "")
                            if vs >= 0.5 and chunk_id and chunk_id not in new_chunk_ids:
                                score_ratio = vs / max_score
                                cross_batch[chunk_id] = {
                                    "memory_value": cross_boost * score_ratio,
                                    "hit_count_delta": 1,
                                    "last_hit_at": learned_at,
                                }
                                memory_boosted_chunks += 1
                        if cross_batch:
                            self._store.update_memory(cross_batch)
            except Exception:
                # 交叉验证失败不影响 learn 主流程
                pass
        learn_records[dedupe_key] = {
            "status": "completed",
            "source": request["source"],
            "trigger_phase": request["trigger_phase"],
            "workspace_root": request["workspace_root"],
            "doc_id": str(saved.get("doc_id") or ""),
            "file_name": str(saved.get("file_name") or ""),
            "learned_at": learned_at,
            "updated_at": learned_at,
        }
        self._save_learn_records(learn_records)
        return {
            "ok": True,
            "ignored": False,
            "deduped": False,
            "status": "completed",
            "source": request["source"],
            "trigger_phase": request["trigger_phase"],
            "dedupe_key": dedupe_key,
            "workspace_root": request["workspace_root"],
            "doc_id": str(saved.get("doc_id") or ""),
            "file_name": str(saved.get("file_name") or ""),
            "chunk_count": rebuilt.get("chunk_count", 0),
            "keywords": keywords,
            "categories": categories,
            "memory_new_chunks": len(new_chunk_ids),
            "memory_boosted_chunks": memory_boosted_chunks,
            "chat_model": settings["chat_model"],
            "learned_at": learned_at,
        }

    # ──────────────────────── Session Scanner ────────────────────────

    async def scan_sessions(
        self,
        since_hours: float = 24,
        max_sessions: int = 5,
        learn_enabled: bool = True,
        memory_enabled: bool = True,
    ) -> dict:
        """扫描客户端本地会话文件，生成知识并更新记忆。

        支持 Codex 和 Claude Code 两种客户端格式，自动检测并归一化。
        采用两阶段原子写入：learn dedupe 先落盘，memory 后补。
        """
        self._ensure_runtime_ready()
        settings = self._settings()
        learn_enabled = learn_enabled and bool(settings.get("memory_enabled"))
        memory_enabled = memory_enabled and bool(settings.get("memory_enabled"))
        now = _utc_now_iso()

        # 收集会话文件
        codex_files = session_scanner.list_codex_sessions(since_hours)[:max_sessions * 2]
        claude_files = session_scanner.list_claude_sessions(since_hours)[:max_sessions * 2]
        all_files = codex_files + claude_files
        all_files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

        scanned_records = session_scanner.load_scanned_records(self._data_root)
        results: list[dict] = []
        total_learned = 0
        total_boosted = 0

        for fpath in all_files:
            if len(results) >= max_sessions:
                break

            session_data = session_scanner.parse_session_file(fpath)
            if not session_data or not session_data.get("turns"):
                continue

            source = session_data["source"]
            session_id = session_data["session_id"]
            dedupe_key = f"{source}:{session_id}"

            # 检查是否已处理
            existing = scanned_records.get(dedupe_key)
            if isinstance(existing, dict):
                if session_scanner.needs_memory_update(existing) and memory_enabled:
                    # 上次 learn 成功但 memory 未完成，补做
                    boosted = await self._scan_memory_only(session_data, settings)
                    session_scanner.mark_scanned_memory(scanned_records, dedupe_key, boosted, now)
                    session_scanner.save_scanned_records(self._data_root, scanned_records)
                    total_boosted += boosted
                results.append({
                    "session_id": session_id,
                    "source": source,
                    "deduped": True,
                    "memory_patch": session_scanner.needs_memory_update(existing) if isinstance(existing, dict) else False,
                })
                continue

            # 新会话 → learn + memory
            doc_count = 0
            boosted = 0
            try:
                doc_count = await self._scan_learn_from_session(session_data, settings) if learn_enabled else 0
            except Exception as exc:
                self.logger.warning("[markdown_kb] 扫描学习失败 %s: %s", fpath.name, exc)

            if learn_enabled and doc_count > 0:
                session_scanner.mark_scanned_learned(scanned_records, dedupe_key, doc_count, now)
                session_scanner.save_scanned_records(self._data_root, scanned_records)

            if memory_enabled:
                try:
                    boosted = await self._scan_memory_only(session_data, settings)
                except Exception:
                    pass

            if learn_enabled and doc_count > 0:
                session_scanner.mark_scanned_memory(scanned_records, dedupe_key, boosted, now)
            elif not learn_enabled and boosted > 0:
                # 只做 memory 的场景
                session_scanner.mark_scanned_memory(scanned_records, dedupe_key, boosted, now)
            session_scanner.save_scanned_records(self._data_root, scanned_records)

            total_learned += doc_count
            total_boosted += boosted
            results.append({
                "session_id": session_id,
                "source": source,
                "deduped": False,
                "doc_count": doc_count,
                "boosted_chunks": boosted,
            })

        # 更新最后扫描时间
        scanned_records["_last_scan_at"] = now
        session_scanner.save_scanned_records(self._data_root, scanned_records)

        return {
            "ok": True,
            "scanned": len(results),
            "total_learned": total_learned,
            "total_boosted": total_boosted,
            "results": results,
        }

    async def _scan_learn_from_session(self, session_data: dict, settings: dict) -> int:
        """从单个 session 生成知识条目。返回生成的 .learn.md 数量。"""
        source = session_data["source"]
        session_id = session_data["session_id"]
        cwd = session_data.get("cwd", "")
        turns = session_data.get("turns", [])

        user_msgs = [t for t in turns if t.get("role") == "user"]
        assistant_msgs = [t for t in turns if t.get("role") == "assistant"]

        if not user_msgs:
            return 0

        # 构造 learn 兼容的请求对象
        trigger_phase = "scan"
        dedupe_key = f"{source}:{session_id}"
        first_user = str(user_msgs[0].get("text", "")).strip()
        title_hint = first_user[:80] if first_user else ""

        learn_request = {
            "source": source,
            "trigger_phase": trigger_phase,
            "session_id": session_id,
            "dedupe_key": dedupe_key,
            "workspace_root": cwd,
            "title_hint": title_hint,
            "user_prompt": first_user,
            "turn_id": "",
            "assistant_excerpt": "\n".join(
                str(m.get("text", "")) for m in assistant_msgs[-3:]
            )[:4000] if assistant_msgs else "",
            "conversation_excerpt": turns,
            "learn_keyword": "扫描入库",
        }

        # 调用 learn 核心逻辑（跳过幂等检查，由扫描记录管理去重）
        chat_model = settings["chat_model"]
        learn_result = await self._generate_learn_summary(learn_request, chat_model)
        summary_markdown = str(learn_result.get("summary_markdown") or "").strip()
        should_learn = bool(learn_result.get("should_learn")) and bool(summary_markdown)
        if not should_learn:
            return 0

        title = self._resolve_learn_title(learn_result, learn_request)
        quotes = self._normalize_learn_quotes(learn_result.get("quotes"))
        keywords = [str(k).strip() for k in (learn_result.get("keywords") or []) if str(k).strip()]
        categories = [str(c).strip() for c in (learn_result.get("categories") or []) if str(c).strip()]
        file_name = self._make_learn_file_name(title, dedupe_key, learn_request)
        markdown_text = self._render_learn_document(
            title=title, request=learn_request, summary_markdown=summary_markdown,
            quotes=quotes, keywords=keywords, categories=categories,
        )
        saved = self._save_markdown_payload(
            file_name=file_name, payload=markdown_text.encode("utf-8"), workspace_root=cwd,
        )
        rebuilt = await self.rebuild_file(
            file_name=str(saved.get("file_name") or ""),
            workspace_root=cwd,
            doc_id=str(saved.get("doc_id") or ""),
        )
        new_chunk_ids = rebuilt.get("chunk_ids", [])
        if settings.get("memory_enabled") and new_chunk_ids and self._store:
            init_batch = {}
            for cid in new_chunk_ids:
                init_batch[cid] = {"memory_value": 0.20, "hit_count_delta": 1, "last_hit_at": _utc_now_iso()}
            self._store.update_memory(init_batch)

        return 1 if rebuilt.get("chunk_count", 0) > 0 else 0

    async def _scan_memory_only(self, session_data: dict, settings: dict) -> int:
        """对 session 中的 user questions 做交叉验证 boost，不生成知识。"""
        turns = session_data.get("turns", [])
        user_questions = [t.get("text", "").strip() for t in turns if t.get("role") == "user" and t.get("text")]
        if not user_questions:
            return 0

        combined = "\n".join(user_questions[:3])[:2000]
        query_vector = await self._get_query_embedding(combined, settings["embedding_model"])
        all_documents = self._store.list_documents()
        if not all_documents:
            return 0

        candidates = self._retrieve_candidates_with_sqlite_vec(
            query_vector=query_vector,
            documents=all_documents,
            top_k=max(settings["top_k"], 4) * 2,
            request_workspace=session_data.get("cwd", ""),
        )
        if not candidates:
            return 0

        scored = self._score_documents(
            "", query_vector, candidates,
            semantic_weight=1.0, keyword_weight=0.0, memory_weight=0.0,
            category_bonus=0.0, category_list=[],
            vector_score_overrides={
                str(c.get("id") or ""): _safe_float(c.get("vector_score"), 0.0)
                for c in candidates if str(c.get("id") or "")
            },
        )
        max_score = max((_safe_float(h.get("vector_score"), 0.0) for h in scored), default=1.0) or 1.0
        scan_boost = 0.15
        batch = {}
        for hit in scored:
            vs = _safe_float(hit.get("vector_score"), 0.0)
            chunk_id = str(hit.get("id") or "")
            if vs >= 0.4 and chunk_id:
                score_ratio = vs / max_score
                batch[chunk_id] = {"memory_value": scan_boost * score_ratio, "hit_count_delta": 1, "last_hit_at": _utc_now_iso()}
        if batch and self._store:
            self._store.update_memory(batch)
        return len(batch)

    async def _trigger_lazy_scan(self) -> None:
        """惰性后台扫描触发器：距上次扫描超过 30 分钟则异步启动。"""
        try:
            data_root = getattr(self, "_data_root", None)
            if not data_root:
                return
            records = session_scanner.load_scanned_records(data_root)
            last_scan = records.get("_last_scan_at", "")
            if last_scan:
                try:
                    last_dt = datetime.fromisoformat(last_scan.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                    if elapsed < 1800:  # 30 分钟
                        return
                except (ValueError, TypeError):
                    pass
            asyncio.create_task(self.scan_sessions(
                since_hours=24, max_sessions=3, learn_enabled=False, memory_enabled=True,
            ))
        except Exception:
            pass

    # ======================== 自动整理记忆 ========================
    def _load_organizer_state(self) -> dict:
        """加载 organizer 持久化状态。"""
        path = getattr(self, "_organizer_state_path", None)
        if not path or not path.exists():
            return {"message_count": 0, "last_organize_at": ""}
        try:
            return json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"message_count": 0, "last_organize_at": ""}

    def _save_organizer_state(self, state: dict) -> None:
        """保存 organizer 持久化状态。"""
        path = getattr(self, "_organizer_state_path", None)
        if not path:
            return
        try:
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        except OSError:
            pass

    async def _trigger_organize(self) -> None:
        """自动整理触发器：满足消息计数或定时周期任一条件时异步触发。

        设计说明：
        1. 每次 `_retrieve` 调用时检查，消息计数 +1；
        2. 消息数达到 `organize_message_threshold` 或距上次整理超过 `organize_interval_hours` 触发；
        3. 互斥锁 `_organize_running` 防止重复触发；
        4. 异步执行，不阻塞检索。
        """
        settings = self._settings()
        msg_threshold = _safe_int(settings.get("organize_message_threshold"), 50)
        interval_hours = _safe_float(settings.get("organize_interval_hours"), 24.0)

        state = self._load_organizer_state()
        state["message_count"] = _safe_int(state.get("message_count"), 0) + 1

        should_trigger = False
        if state["message_count"] >= msg_threshold:
            should_trigger = True
        else:
            last_at = str(state.get("last_organize_at") or "")
            if last_at:
                try:
                    last_dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                    if elapsed >= interval_hours * 3600:
                        should_trigger = True
                except (ValueError, TypeError):
                    should_trigger = True
            else:
                should_trigger = True

        self._save_organizer_state(state)

        if not should_trigger:
            return

        # 互斥锁：上一次整理未完成则跳过
        if getattr(self, "_organize_running", False):
            return
        self._organize_running = True
        asyncio.create_task(self._organize())

    async def _organize(self) -> None:
        """执行一次完整的自动整理：扫描 session → 学习新知识 → 交叉验证存量 → 清理过期记忆。"""
        try:
            data_root = getattr(self, "_data_root", None)
            if not data_root:
                return

            # 扫描最近 24h 的 session，生成知识 + 更新记忆
            await self.scan_sessions(
                since_hours=24, max_sessions=5, learn_enabled=True, memory_enabled=True,
            )

            # 清理过期记忆
            if self._store:
                self._store.cleanup_expired_memory()

            # 清理无价值的 learn 文档
            await self._cleanup_stale_learn_docs()

        except Exception:
            pass
        finally:
            # 无论成功失败都重置计数器，避免卡死触发
            state = self._load_organizer_state()
            state["message_count"] = 0
            state["last_organize_at"] = _utc_now_iso()
            self._save_organizer_state(state)
            self._organize_running = False

    async def _cleanup_stale_learn_docs(self) -> None:
        """清理长期无价值的 .learn.md 文档。

        判断标准：
        1. chunk_count == 0 的空文档直接清理；
        2. 从索引创建后被用户检索命中（retrieval_hits > 0）的文档保留；
        3. 从未被检索命中且超过保活天数的文档视为无价值，予以清理。
        注：hit_count 中有 1 次来自创建时的伪命中，排除后才得到真实检索命中数。
        """
        settings = getattr(self, "config", {}) or {}
        cleanup_enabled = bool(settings.get("organize_cleanup_enabled", True))
        if not cleanup_enabled:
            return
        memory_threshold = _safe_float(settings.get("organize_cleanup_memory_threshold"), 0.05)
        keep_days = _safe_int(settings.get("organize_cleanup_keep_days"), 7)
        if not self._store:
            return

        try:
            stats = self._store.get_learn_doc_memory_stats()
        except Exception:
            return

        now_ts = datetime.utcnow()
        stale_docs = []
        for stat in stats:
            if stat["chunk_count"] == 0:
                # 没有 chunk 的 learn 文档直接清理
                stale_docs.append(stat)
                continue

            # 真实检索命中数 = 总命中数 - chunk 数（扣除创建时的伪命中）
            retrieval_hits = stat["total_hits"] - stat["chunk_count"]
            if retrieval_hits > 0:
                # 曾被用户检索命中过，有价值，保留
                continue
            if stat["max_memory"] >= memory_threshold:
                # 记忆值较高（可能来自交叉验证 boost），即使未检索也保留
                continue

            # 从未被检索命中，检查创建时间
            manifest = self._load_doc_manifest()
            doc_info = next((item for item in manifest if str(item.get("doc_id") or "") == stat["doc_id"]), None)
            if doc_info:
                created_str = str(doc_info.get("created_at") or "")
                try:
                    created_ts = datetime.fromisoformat(created_str)
                    age_days = (now_ts - created_ts).total_seconds() / 86400
                    if age_days < keep_days:
                        continue
                except (ValueError, TypeError):
                    pass
            stale_docs.append(stat)

        removed = 0
        for doc in stale_docs:
            try:
                self.delete_file(
                    name=doc["file_name"],
                    doc_id=doc["doc_id"],
                )
                removed += 1
            except Exception:
                pass

        if removed > 0:
            self.logger.info(
                "[markdown_kb] 整理完成：清理了 %d 个无价值的 learn 文档（剩余 learn 文档：%d）",
                removed,
                len(stats) - removed,
            )

    def _settings(self) -> dict:
        """统一解析插件配置，并做最基本的类型收敛。"""
        cfg = self.config or {}
        chunk_size = max(200, _safe_int(cfg.get("chunk_size"), 800))
        chunk_overlap = max(0, min(chunk_size // 2, _safe_int(cfg.get("chunk_overlap"), 120)))
        top_k = max(1, min(10, _safe_int(cfg.get("top_k"), 4)))

        semantic_weight, keyword_weight, memory_weight = _normalize_weights(
            _safe_float(cfg.get("semantic_weight"), 1.0),
            _safe_float(cfg.get("keyword_weight"), 0.0),
            _safe_float(cfg.get("memory_weight"), 0.1),
        )

        default_categories = [
            "技术实现", "业务逻辑", "架构设计", "调试修复", "配置部署", "代码风格",
        ]
        category_list = cfg.get("category_list") or default_categories
        if not isinstance(category_list, list):
            category_list = default_categories

        return {
            "embedding_model": str(cfg.get("embedding_model") or "text-embedding-3-small").strip() or "text-embedding-3-small",
            "reranker_model": str(cfg.get("reranker_model") or "").strip(),
            "chat_model": str(cfg.get("chat_model") or "").strip(),
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "top_k": top_k,
            "semantic_weight": semantic_weight,
            "keyword_weight": keyword_weight,
            "memory_weight": memory_weight,
            "score_threshold": min(1.0, max(0.0, _safe_float(cfg.get("score_threshold"), 0.7))),
            # 记忆系统配置
            "memory_enabled": bool(cfg.get("memory_enabled", True)),
            "memory_boost": _safe_float(cfg.get("memory_boost"), 0.15),
            "memory_decay_half_life_hours": _safe_float(cfg.get("memory_decay_half_life_hours"), 24.0),
            "memory_value_cap": _safe_float(cfg.get("memory_value_cap"), 0.7),
            # 分类加权配置
            "category_bonus": _safe_float(cfg.get("category_bonus"), 0.10),
            "category_list": category_list,
            # 自动整理记忆配置
            "organize_interval_hours": _safe_float(cfg.get("organize_interval_hours"), 24.0),
            "organize_message_threshold": max(1, _safe_int(cfg.get("organize_message_threshold"), 50)),
            "organize_cleanup_enabled": bool(cfg.get("organize_cleanup_enabled", True)),
            "organize_cleanup_memory_threshold": _safe_float(cfg.get("organize_cleanup_memory_threshold"), 0.05),
            "organize_cleanup_keep_days": _safe_int(cfg.get("organize_cleanup_keep_days"), 7),
            # 去重合并配置
            "dedup_similarity_threshold": min(1.0, max(0.0, _safe_float(cfg.get("dedup_similarity_threshold"), 0.92))),
            # Prompt 配置（可在插件配置页直接编辑优化）
            "learn_summary_system_prompt": str(cfg.get("learn_summary_system_prompt") or "").strip(),
            "merge_chunks_system_prompt": str(cfg.get("merge_chunks_system_prompt") or "").strip(),
            "merge_chunks_user_prompt": str(cfg.get("merge_chunks_user_prompt") or "").strip(),
        }

    def _resolve_top_k(self, requested: Any) -> int:
        """解析请求级 top_k，未传时回落到插件默认值。"""
        default_top_k = self._settings()["top_k"]
        if requested in (None, ""):
            return default_top_k
        return max(1, min(10, _safe_int(requested, default_top_k)))

    def _load_index_data(self) -> dict:
        """读取索引文件。

        当前默认 backend 是 `JsonIndexStore`，但调用方不需要知道底层细节。
        """
        return self._store.load()

    def _save_index_data(self, data: dict) -> None:
        """落盘索引数据。"""
        self._store.save(data)

    def _chunk_markdown_file(self, path: Path, settings: dict, entry: dict | None = None) -> list[dict]:
        """使用内置标题树切片器切分 Markdown 文件。

        采用标题树优先策略，每个标题 section 独立成 chunk，
        超长 section 按 list 项 → 段落 → 字符级逐级拆分。
        块级元素（代码块、引用块、表格、HTML）保持完整不可截断。
        """
        text = path.read_text("utf-8")
        entry = self._normalize_doc_entry(entry or self._build_doc_entry(path.name, self._resolve_file_workspace_root(path.name, settings), path.name))
        workspace_root = self._normalize_workspace_root(entry.get("workspace_root") or "")
        chunk_size = max(200, _safe_int(settings.get("chunk_size"), 800))
        overlap = max(0, _safe_int(settings.get("chunk_overlap"), 120))
        return self._chunk_markdown_tree(text, path, chunk_size, overlap, workspace_root, entry)

    def _chunk_markdown_tree(
        self,
        text: str,
        path: Path,
        chunk_size: int,
        overlap: int,
        workspace_root: str,
        entry: dict,
    ) -> list[dict]:
        """标题树优先切片器。"""

        # --------------------------- 第 1 步：解析标题树 ---------------------------
        section_sep_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
        lines = text.splitlines()
        root_sections: list[dict] = []
        stack: list[dict] = [{"level": 0, "children": root_sections}]
        current_lines: list[str] = []

        for line in lines:
            matched = section_sep_re.match(line.strip())
            if matched:
                level = len(matched.group(1))
                title = matched.group(2).strip()
                content_text = "\n".join(current_lines).strip()
                current_lines = []

                # 将累积内容保存到当前最深节点（即将被弹出的节点）
                if stack and content_text:
                    stack[-1]["content"] = content_text

                # 创建新节点
                node: dict = {
                    "title": title,
                    "level": level,
                    "content": "",
                    "heading_path": [],
                    "children": [],
                }

                # 找到父节点（弹出同层或更深层节点）
                while stack and stack[-1]["level"] >= level:
                    stack.pop()

                parent = stack[-1] if stack else {"children": root_sections}
                parent["children"].append(node)
                stack.append(node)
                continue
            current_lines.append(line)

        # 最后一段内容归属于最深节点
        content_text = "\n".join(current_lines).strip()
        if stack and content_text:
            stack[-1]["content"] = content_text

        # --------------------------- 第 2 步：为每个节点计算 heading_path ---------------------------
        def assign_heading_paths(nodes: list[dict], parent_path: list[str]):
            for node in nodes:
                node["heading_path"] = list(parent_path) + [node["title"]]
                assign_heading_paths(node.get("children", []), node["heading_path"])

        assign_heading_paths(root_sections, [])

        # --------------------------- 第 3 步：将树扁平化为 chunk 列表 ---------------------------
        doc_id = str(entry.get("doc_id") or hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest())
        max_section_size = chunk_size * 2
        all_chunks: list[dict] = []
        chunk_id_counter = 0

        def _make_chunk(lines_body: list[str], h_path: list[str], title: str, level: int) -> dict:
            nonlocal chunk_id_counter
            chunk_text = "\n".join(lines_body).strip()
            content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
            stable_source = f"{path.resolve()}::{chunk_id_counter}::{content_hash}"
            chunk_id = hashlib.sha1(stable_source.encode("utf-8")).hexdigest()
            chunk_id_counter += 1
            # 从 chunk_text 中提取知识分类元数据
            categories = ""
            cat_match = re.search(r"\*\*知识分类\*\*[：:]\s*(.+?)(?:\n|$)", chunk_text)
            if cat_match:
                categories = cat_match.group(1).strip()
            return {
                "id": chunk_id,
                "doc_id": doc_id,
                "file_name": str(entry.get("file_name") or path.name),
                "file_path": str(path.resolve()),
                "workspace_root": workspace_root,
                "title": title,
                "heading_level": level,
                "heading_path": json.dumps(h_path, ensure_ascii=False) if h_path else "",
                "categories": categories,
                "chunk_index": chunk_id_counter - 1,
                "chunk_text": chunk_text,
                "content_hash": content_hash,
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
            }

        def _is_block_element_start(line: str) -> bool:
            """检测块级元素起始行（代码块、引用、表格、HTML）。"""
            stripped = line.strip()
            if stripped.startswith("```"):
                return True
            if stripped.startswith(">"):
                return True
            if stripped.startswith("|") and "|" in stripped[1:]:
                return True
            if re.match(r"^</?[a-zA-Z]", stripped):
                return True
            return False

        def _is_same_block_element(start_marker: str) -> callable:
            """返回判断后续行是否属于同一块级元素的函数。"""
            if start_marker.startswith("```"):
                lang = start_marker[3:].strip()
                if lang:
                    return lambda line, inside: inside or not line.strip().startswith("```")
                return lambda line, inside: not line.strip().startswith("```") or inside
            if start_marker.startswith(">"):
                return lambda line, inside: line.strip().startswith(">") or (inside and line.strip() == "")
            if start_marker.startswith("|") and "|" in start_marker[1:]:
                return lambda line, inside: line.strip().startswith("|")
            if re.match(r"^</?[a-zA-Z]", start_marker):
                return lambda line, inside: inside and not re.match(r"^</[a-zA-Z]", line.strip())
            return lambda line, inside: False

        def _collect_block_element(lines_list: list[str], start_idx: int) -> tuple[list[str], int]:
            """从 start_idx 收集完整块级元素，返回 (块内行列表, 结束索引)。"""
            start_line = lines_list[start_idx].strip()
            block_lines = [lines_list[start_idx]]
            checker = _is_same_block_element(start_line)
            inside = True
            idx = start_idx + 1
            while idx < len(lines_list):
                inside = checker(lines_list[idx], inside)
                block_lines.append(lines_list[idx])
                idx += 1
                if not inside:
                    break
            return block_lines, idx

        def _is_list_item(line: str) -> bool:
            """检测是否为顶层 list 项。"""
            stripped = line.strip()
            return bool(re.match(r"^[-*+]\s+", stripped)) or bool(re.match(r"^\d+[.)]\s+", stripped))

        def _split_section_into_chunks(section_lines: list[str], h_path: list[str], title: str, level: int):
            """将单个 section 的行列表拆分为一个或多个 chunk。"""
            section_text = "\n".join(section_lines).strip()
            if not section_text:
                return

            if len(section_text) <= max_section_size:
                all_chunks.append(_make_chunk(section_lines, h_path, title, level))
                return

            # 超长 section —— 三级级联拆分
            # 第 0 级：块级元素完整性检查
            i = 0
            while i < len(section_lines):
                line = section_lines[i].strip()
                if _is_block_element_start(line):
                    block_lines, end_idx = _collect_block_element(section_lines, i)
                    block_text = "\n".join(block_lines)
                    if len(block_text) > chunk_size:
                        raise HTTPException(
                            status_code=400,
                            detail=f"文档 {path.name} 中块级元素（代码块/引用/表格/HTML）超过 chunk_size({chunk_size}字)，请优化源文档",
                        )
                    i = end_idx
                    continue
                i += 1

            # 第 1 级：分离 list 和非 list 段
            # 将 section_lines 按 list 项和非 list 段落分组
            segments: list[tuple[str, list[str]]] = []  # [(type, lines), ...]
            i = 0
            while i < len(section_lines):
                stripped = section_lines[i].strip()
                if _is_list_item(stripped):
                    # 收集连续 list 项
                    list_lines = []
                    while i < len(section_lines):
                        stripped_i = section_lines[i].strip()
                        if _is_list_item(stripped_i):
                            # 收集单个 list 项（可能多行）
                            item_lines = [section_lines[i]]
                            i += 1
                            while i < len(section_lines) and section_lines[i].strip() and not _is_list_item(section_lines[i].strip()):
                                if _is_block_element_start(section_lines[i].strip()):
                                    blk, end = _collect_block_element(section_lines, i)
                                    item_lines.extend(blk)
                                    i = end
                                    continue
                                item_lines.append(section_lines[i])
                                i += 1
                            list_lines.append(("\n".join(item_lines), item_lines))
                        elif not stripped_i:
                            list_lines.append(("\n", [section_lines[i]]))
                            i += 1
                        else:
                            break
                    segments.append(("list", list_lines))
                else:
                    # 收集非 list 段落直到遇到 list 项
                    para_lines = []
                    while i < len(section_lines):
                        s = section_lines[i].strip()
                        if _is_list_item(s):
                            break
                        if _is_block_element_start(s):
                            blk, end = _collect_block_element(section_lines, i)
                            para_lines.extend(blk)
                            i = end
                            continue
                        para_lines.append(section_lines[i])
                        i += 1
                    para_text = "\n".join(para_lines).strip()
                    if para_text:
                        segments.append(("para", para_lines))
            # 将 list 段按 chunk_size 分组打包
            current_batch_lines: list[str] = []
            current_batch_length = 0

            for seg_type, seg_data in segments:
                if seg_type == "para":
                    # 非 list 段落独立成 chunk
                    para_text = "\n".join(seg_data).strip()
                    if para_text:
                        if len(para_text) > chunk_size:
                            # 按段落边界再拆
                            sub_paras = [p.strip() for p in re.split(r"\n\s*\n", para_text) if p.strip()]
                            for sp in sub_paras:
                                if len(sp) > chunk_size:
                                    for part in self._split_long_text(sp, chunk_size, max(0, chunk_size // 4)):
                                        if part.strip():
                                            all_chunks.append(_make_chunk([part.strip()], h_path, title, level))
                                else:
                                    all_chunks.append(_make_chunk([sp], h_path, title, level))
                        else:
                            all_chunks.append(_make_chunk(seg_data, h_path, title, level))
                    continue

                # seg_type == "list"
                list_items: list[tuple[str, list[str]]] = seg_data
                for item_text, item_lines in list_items:
                    if len(item_text) > chunk_size:
                        # 单个 list 项超长 → 按段落边界拆分
                        sub_paras = [p.strip() for p in re.split(r"\n\s*\n", item_text) if p.strip()]
                        for sp in sub_paras:
                            if len(sp) > chunk_size:
                                for part in self._split_long_text(sp, chunk_size, max(0, chunk_size // 4)):
                                    if part.strip():
                                        all_chunks.append(_make_chunk([part.strip()], h_path, title, level))
                            else:
                                all_chunks.append(_make_chunk([sp], h_path, title, level))
                        continue

                    candidate_lines = current_batch_lines + item_lines
                    candidate_len = len("\n".join(candidate_lines))
                    if candidate_len <= chunk_size:
                        current_batch_lines = candidate_lines
                        current_batch_length = candidate_len
                    else:
                        if current_batch_lines:
                            all_chunks.append(_make_chunk(current_batch_lines, h_path, title, level))
                        current_batch_lines = list(item_lines)
                        current_batch_length = len(item_text)

                # 排空最后一批
                if current_batch_lines:
                    all_chunks.append(_make_chunk(current_batch_lines, h_path, title, level))
                    current_batch_lines = []
                    current_batch_length = 0

        def _flatten_section(node: dict):
            """递归展开标题树节点为 chunk。"""
            content = (node.get("content") or "").strip()
            heading_path = node.get("heading_path", [])
            children = node.get("children", [])
            title = node.get("title", "")
            level = node.get("level", 0)

            if content:
                _split_section_into_chunks(content.splitlines(), heading_path, title, level)
            elif not children and heading_path:
                all_chunks.append(_make_chunk([title], heading_path, title, level))

            for child in children:
                _flatten_section(child)

        for root_node in root_sections:
            if root_node.get("content") or root_node.get("children"):
                _flatten_section(root_node)

        if not all_chunks and root_sections:
            root = root_sections[0]
            all_chunks.append(_make_chunk([root.get("title", path.stem)], root.get("heading_path", []), root.get("title", path.stem), root.get("level", 0)))

        # 重新分配 chunk_index 确保连续
        for i, chunk in enumerate(all_chunks):
            chunk["chunk_index"] = i

        return all_chunks

    async def _retrieve(
        self,
        question: str,
        top_k: int,
        embedding_model: str,
        reranker_model: str,
        project_context: dict | None = None,
        selected_doc: dict | None = None,
    ) -> list[dict]:
        """执行检索。

        检索策略：
        1. 三路融合排序（语义分 + 关键词分 + 记忆分）× 分类加权；
        2. 若配置 reranker_model，则追加 rerank 替换 score；
        3. 记忆值 > 0.5 的 chunk 豁免 score_threshold 过滤；
        4. 命中后按比例更新记忆值（boost = memory_boost × score_ratio）。
        """
        settings = self._settings()
        memory_enabled = bool(settings.get("memory_enabled"))

        # 惰性后台扫描：距上次扫描超过 30 分钟则异步触发（不阻塞检索）
        if memory_enabled:
            asyncio.create_task(self._trigger_lazy_scan())

        # 自动整理记忆触发（消息计数 + 定时周期）
        asyncio.create_task(self._trigger_organize())

        # 记忆启用时跳过缓存（记忆值随时间衰减，缓存会导致 stale score）
        if not memory_enabled:
            query_cache_key = self._query_cache_key(
                question,
                top_k,
                embedding_model,
                reranker_model,
                settings["semantic_weight"],
                settings["keyword_weight"],
                settings["score_threshold"],
                project_context,
                selected_doc,
            )
            cached_hits = self._cache_get(self._query_result_cache, query_cache_key)
            if cached_hits is not None:
                return list(cached_hits)

        request_workspace = ""
        if self._has_request_workspace(project_context):
            request_workspace = self._normalize_workspace_root(
                (project_context or {}).get("workspace_root")
                or (project_context or {}).get("working_directory")
                or ""
            )
        query_vector = await self._get_query_embedding(question, embedding_model)
        documents = self._store.list_documents_by_scope(request_workspace, selected_doc)
        if not documents:
            all_documents = self._load_documents_for_retrieval()
            if not all_documents:
                raise HTTPException(status_code=400, detail="当前知识库为空，请先上传文档并执行重建")
            scoped_documents = self._filter_documents_by_workspace(all_documents, project_context)
            if not scoped_documents:
                raise HTTPException(status_code=400, detail="当前工作目录下没有匹配的知识文档，请先为该工作目录配置并重建索引")
            scoped_documents = self._filter_documents_by_selected_doc(scoped_documents, selected_doc)
            if not scoped_documents:
                raise HTTPException(status_code=400, detail="当前所选文档不在本次检索范围内，请检查 workspace 或改为不指定文档")
            documents = scoped_documents

        vector_candidates = self._retrieve_candidates_with_sqlite_vec(
            query_vector=query_vector,
            documents=documents,
            top_k=top_k,
            project_context=project_context,
            selected_doc=selected_doc,
            request_workspace=request_workspace,
        )
        candidate_documents = vector_candidates if vector_candidates else documents
        semantic_weight = settings["semantic_weight"]
        keyword_weight = settings["keyword_weight"]
        memory_weight = settings.get("memory_weight", 0.0)
        memory_boost = _safe_float(settings.get("memory_boost"), 0.10)
        memory_half_life = _safe_float(settings.get("memory_decay_half_life_hours"), 24.0)
        memory_value_cap = _safe_float(settings.get("memory_value_cap"), 0.7)
        category_bonus = _safe_float(settings.get("category_bonus"), 0.10)
        category_list = list(settings.get("category_list") or [])

        # 读取记忆表并做时间衰减
        memory_map: dict[str, float] = {}
        if memory_enabled and memory_weight > 0:
            candidate_ids = [str(item.get("id") or "") for item in candidate_documents if str(item.get("id") or "")]
            if candidate_ids:
                raw_memory = self._store.read_memory_map(candidate_ids)
                decay_lambda = math.log(2) / max(memory_half_life, 0.1)
                now = datetime.now(timezone.utc)
                for cid, mem_info in (raw_memory or {}).items():
                    stored = _safe_float(mem_info.get("memory_value"), 0.0)
                    last_hit = mem_info.get("last_hit_at") or ""
                    if stored > 0 and last_hit:
                        try:
                            last = datetime.fromisoformat(last_hit.replace("Z", "+00:00"))
                            delta_hours = (now - last).total_seconds() / 3600.0
                            if delta_hours > 0:
                                stored = stored * math.exp(-decay_lambda * delta_hours)
                        except (ValueError, TypeError):
                            pass
                    memory_map[cid] = max(0.0, min(1.0, stored))

        # 计算 parent_bonus_map：query 关键词命中 heading_path → 树形加权
        parent_bonus_map: dict[str, float] = {}
        if candidate_documents:
            # 简单分词：非字母数字字符分割 + 中文 2-4 字滑窗
            q_words = set()
            q_lower = question.lower()
            for token in re.split(r"[^a-z0-9\u4e00-\u9fff]+", q_lower):
                if not token:
                    continue
                if re.search(r"[\u4e00-\u9fff]", token):
                    for win_size in (2, 3, 4):
                        for i in range(len(token) - win_size + 1):
                            q_words.add(token[i:i + win_size])
                elif len(token) >= 2:
                    q_words.add(token)
            if q_words:
                for item in candidate_documents:
                    chunk_id = str(item.get("id") or "")
                    heading_path_str = str(item.get("heading_path") or "")
                    if not heading_path_str or not chunk_id:
                        continue
                    try:
                        h_path = json.loads(heading_path_str)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(h_path, list) or not h_path:
                        continue
                    bonus = 0.0
                    for level_idx, heading_title in enumerate(h_path):
                        h_lower = str(heading_title).lower()
                        h_words = set()
                        for token in re.split(r"[^a-z0-9\u4e00-\u9fff]+", h_lower):
                            if not token:
                                continue
                            if re.search(r"[\u4e00-\u9fff]", token):
                                for ws in (2, 3, 4):
                                    for i in range(len(token) - ws + 1):
                                        h_words.add(token[i:i + ws])
                            elif len(token) >= 2:
                                h_words.add(token)
                        if h_words & q_words:
                            if level_idx == 0:
                                bonus = max(bonus, 0.05)
                            elif level_idx == 1:
                                bonus = max(bonus, 0.10)
                    if bonus > 0:
                        parent_bonus_map[chunk_id] = bonus

        scored = self._score_documents(
            question,
            query_vector,
            candidate_documents,
            semantic_weight,
            keyword_weight,
            vector_score_overrides={
                str(item.get("id") or ""): _safe_float(item.get("vector_score"), 0.0)
                for item in vector_candidates
                if str(item.get("id") or "")
            } if vector_candidates else None,
            memory_weight=memory_weight,
            memory_map=memory_map,
            category_bonus=category_bonus,
            category_list=category_list,
            parent_bonus_map=parent_bonus_map,
        )

        scored.sort(key=lambda item: item["score"], reverse=True)
        candidates = scored[:max(top_k, min(12, len(scored)))]
        if reranker_model and candidates:
            candidates = await self._rerank_hits(question, candidates, reranker_model)

        # score_threshold 过滤 + 记忆豁免
        score_threshold = settings["score_threshold"]
        filtered: list[dict] = []
        for item in candidates:
            chunk_id = str(item.get("id") or "")
            mem_val = memory_map.get(chunk_id, 0.0)
            score = _safe_float(item.get("score"), 0.0)
            # 记忆豁免：被多次验证过的有价值 chunk 即使 rerank 分略低也保留
            if mem_val > 0.5 or score >= score_threshold:
                filtered.append(item)

        if not self._has_request_workspace(project_context):
            filtered = self._prefer_project_hits(filtered, project_context)

        # 分片合并：同 doc_id 的兄弟 chunk 合并为一个完整文档结果
        merged = self._merge_sibling_hits(filtered, top_k)

        # 更新记忆：按合并后 score 比例做 boost
        if memory_enabled and memory_boost > 0 and merged:
            max_score = max((_safe_float(h.get("score"), 0.0) for h in merged), default=0.01)
            now_iso = _utc_now_iso()
            decay_lambda = math.log(2) / max(memory_half_life, 0.1)
            memory_updates: dict[str, dict] = {}
            for hit in merged:
                chunk_id = str(hit.get("id") or "")
                if not chunk_id:
                    continue
                score_ratio = _safe_float(hit.get("score"), 0.0) / max(max_score, 0.01)
                old_memory = memory_map.get(chunk_id, 0.0)
                boost = memory_boost * score_ratio
                new_memory = min(memory_value_cap, old_memory + boost * (1.0 - old_memory))
                memory_updates[chunk_id] = {
                    "memory_value": new_memory,
                    "hit_count_delta": 1,
                    "last_hit_at": now_iso,
                }
                # 合并结果可能包含兄弟 chunks，全部做 boost
                for sibling_id in (hit.get("sibling_ids") or []):
                    if sibling_id not in memory_updates:
                        sib_old = memory_map.get(sibling_id, 0.0)
                        sib_new = min(memory_value_cap, sib_old + boost * (1.0 - sib_old))
                        memory_updates[sibling_id] = {
                            "memory_value": sib_new,
                            "hit_count_delta": 1,
                            "last_hit_at": now_iso,
                        }
            if memory_updates:
                self._store.update_memory(memory_updates, cap=memory_value_cap)
                self._store.cleanup_expired_memory()

        # 仅非记忆模式缓存（记忆模式下分数有漂移）
        if not memory_enabled:
            self._cache_set(self._query_result_cache, query_cache_key, list(merged), max_items=64)

        return merged

    def _merge_sibling_hits(self, hits: list[dict], top_k: int) -> list[dict]:
        """将同 doc_id 的兄弟 chunk 合并为一个完整文档结果。

        设计说明：
        1. 同一文档被切片后，不同 chunk 语义关联断裂 —— 检索阶段做合并；
        2. 按 chunk_index 排序拼接所有兄弟 chunk 的文本；
        3. score 取所有合并 chunk 中的最高分；
        4. 多候选属于同一文档时去重合并为一条结果；
        5. heading_path[0] 作为展示前缀。
        """
        if not hits:
            return []

        # 按 doc_id 分组
        doc_groups: dict[str, list[dict]] = {}
        for hit in hits:
            doc_id = str(hit.get("doc_id") or "")
            if not doc_id:
                continue
            if doc_id not in doc_groups:
                doc_groups[doc_id] = []
            doc_groups[doc_id].append(hit)

        merged_results: list[dict] = []
        for doc_id, group_hits in doc_groups.items():
            # 取一个样本 hit 获取元信息
            sample = group_hits[0]
            file_name = sample.get("file_name", "")
            workspace_root = sample.get("workspace_root", "")

            try:
                siblings = self._store.get_sibling_chunks(doc_id)
            except Exception:
                # get_sibling_chunks 内部可能报错，此时按 chunk_index 排序已有的 hits
                group_hits.sort(key=lambda h: _safe_int(h.get("chunk_index"), 0))
                siblings = [{k: h[k] for k in h if k in ("id", "chunk_index", "chunk_text", "doc_id", "title", "heading_level", "heading_path", "categories") if k in h} for h in group_hits]

            if not siblings:
                # 只用已有 hits
                siblings = [{k: h[k] for k in h if k in ("id", "chunk_index", "chunk_text", "doc_id", "title", "heading_level", "heading_path", "categories") if k in h} for h in group_hits]

            # 按 chunk_index 排序并拼接文本
            siblings.sort(key=lambda c: _safe_int(c.get("chunk_index"), 0))
            merged_text_parts = [str(c.get("chunk_text") or "") for c in siblings if str(c.get("chunk_text") or "").strip()]
            merged_text = "\n\n".join(merged_text_parts)
            if not merged_text:
                continue

            # score 取所有关联 chunks 中的最高分
            max_score = max((_safe_float(h.get("score"), 0.0) for h in group_hits), default=0.0)

            # heading_path 前缀提取
            heading_path = sample.get("heading_path", "")
            display_prefix = ""
            if heading_path:
                try:
                    path_parts = json.loads(heading_path)
                    if isinstance(path_parts, list) and path_parts:
                        display_prefix = " > ".join(str(p) for p in path_parts)
                except (json.JSONDecodeError, TypeError):
                    pass

            sibling_ids = [str(c.get("id") or "") for c in siblings]

            merged_results.append({
                "score": max_score,
                "hybrid_score": sample.get("hybrid_score", max_score),
                "vector_score": sample.get("vector_score", 0.0),
                "keyword_score": sample.get("keyword_score", 0.0),
                "rerank_score": sample.get("rerank_score"),  # 透传 rerank 分（若有）
                "memory_score": sample.get("memory_score", 0.0),
                "category_multiplier": sample.get("category_multiplier", 1.0),
                "parent_bonus": sample.get("parent_bonus", 0.0),
                "id": sample.get("id", ""),
                "doc_id": doc_id,
                "file_name": file_name,
                "title": display_prefix or str(sample.get("title") or "") or file_name,
                "heading_level": sample.get("heading_level", 0),
                "heading_path": heading_path,
                "chunk_index": sample.get("chunk_index"),
                "chunk_text": merged_text,
                "content_hash": sample.get("content_hash", ""),
                "workspace_root": workspace_root,
                "categories": sample.get("categories", ""),
                "sibling_ids": sibling_ids,
                "merged_from": len(siblings),
            })

        # 按 score 降序排序，取 top_k
        merged_results.sort(key=lambda item: _safe_float(item.get("score"), 0.0), reverse=True)
        return merged_results[:top_k]

    def _retrieve_candidates_with_sqlite_vec(
        self,
        query_vector: list[float],
        documents: list[dict],
        top_k: int,
        project_context: dict | None,
        selected_doc: dict | None,
        request_workspace: str,
    ) -> list[dict]:
        """优先使用 sqlite-vec 完成第一阶段候选召回。

        返回空列表表示“当前这次请求没有走成 sqlite-vec”，调用方会自动退回原有
        的全量文档 + Python 相似度计算路径。
        """
        if not documents:
            return []
        candidate_limit = min(max(12, top_k * 6), max(len(documents), top_k))
        rows = self._store.search_by_vector(
            query_vector=query_vector,
            limit=candidate_limit,
            workspace_root=request_workspace,
            selected_doc=selected_doc,
        )
        if not rows:
            return []
        candidate_ids = [str(item.get("id") or "") for item in rows if str(item.get("id") or "")]
        if not candidate_ids:
            return []
        documents_by_id = self._store.get_documents_by_chunk_ids(candidate_ids)
        if not documents_by_id:
            return []
        distance_map = {
            str(item.get("id") or ""): _safe_float(item.get("distance"), 0.0)
            for item in rows
            if str(item.get("id") or "")
        }
        results = []
        for item in documents_by_id:
            chunk_id = str(item.get("id") or "")
            if not chunk_id:
                continue
            enriched = dict(item)
            enriched["vector_score"] = _sqlite_vec_distance_to_score(distance_map.get(chunk_id, 0.0))
            results.append(enriched)
        return results

    def _invalidate_embedding_cache(self) -> None:
        """在索引写操作后清空内存向量缓存。"""
        self._embedding_cache_revision = None
        self._embedding_cache_documents = []
        self._bm25_cache_revision = None
        self._bm25_cache_documents = []
        self._bm25_cache_stats = None
        self._query_result_cache.clear()

    def _invalidate_query_caches(self) -> None:
        """清空问题 embedding 和 query 结果缓存。"""
        self._query_embedding_cache.clear()
        self._query_result_cache.clear()

    def _current_embedding_cache_revision(self) -> tuple[Any, Any]:
        """根据当前索引状态生成缓存 revision。"""
        stats = self._store.stats()
        return (stats.get("last_rebuilt_at"), stats.get("document_count"))

    def _query_cache_key(
        self,
        question: str,
        top_k: int,
        embedding_model: str,
        reranker_model: str,
        semantic_weight: float,
        keyword_weight: float,
        score_threshold: float,
        project_context: dict | None,
        selected_doc: dict | None,
    ) -> tuple:
        """构造 query 结果缓存 key。"""
        project_id = ""
        project_name = ""
        selected_doc_id = ""
        if isinstance(project_context, dict):
            project_id = str(project_context.get("project_id") or "")
            project_name = str(project_context.get("project_name") or "")
        if isinstance(selected_doc, dict):
            selected_doc_id = str(selected_doc.get("doc_id") or "")
        return (
            question,
            int(top_k),
            embedding_model,
            reranker_model,
            round(float(semantic_weight), 6),
            round(float(keyword_weight), 6),
            round(float(score_threshold), 6),
            project_id,
            project_name,
            selected_doc_id,
            self._current_embedding_cache_revision(),
        )

    def _resolve_selected_doc_scope(self, payload: dict | None) -> dict:
        """把请求中的文档选择字段收敛成稳定结构。

        设计说明：
        1. 前端测试页优先传 `doc_id`，因为它能唯一标识“文件名 + workspace_root”组合；
        2. 仍兼容只传 `file_name/workspace_root` 的调用方，避免把选择逻辑强耦合到单一前端；
        3. 未显式选择文档时返回空对象，表示继续按 workspace 语义检索整批候选文档。
        """
        if not isinstance(payload, dict):
            return {}
        doc_id = str(payload.get("doc_id") or payload.get("selected_doc_id") or "").strip()
        file_name = Path(str(payload.get("file_name") or payload.get("selected_file_name") or "").strip()).name
        workspace_root = self._normalize_workspace_root(
            payload.get("selected_workspace_root")
            or ""
        )
        if not doc_id and not file_name:
            return {}
        return {
            "doc_id": doc_id,
            "file_name": file_name,
            "workspace_root": workspace_root,
        }

    def _prefer_project_hits(self, hits: list[dict], project_context: dict | None) -> list[dict]:
        """优先保留与当前项目更贴近的候选文档。

        当前阶段先不修改索引结构，因此只能做轻量启发式收敛：
        1. 先看 `project_name` 是否与文件名 stem 完全一致；
        2. 再看标题或文件名里是否包含项目名；
        3. 如果没有任何候选命中这些规则，则直接回退原始 hits，避免因为过滤过强把结果清空。
        """
        if not hits or not isinstance(project_context, dict):
            return hits

        project_name = self._normalize_project_name(project_context.get("project_name") or "")
        if not project_name:
            return hits

        matched = []
        for item in hits:
            if self._is_hit_matching_project(item, project_name):
                matched.append(item)
        return matched or hits

    def _filter_documents_by_workspace(self, documents: list[dict], project_context: dict | None) -> list[dict]:
        """按工作目录对索引文档做强匹配过滤。

        行为约定：
        1. 如果请求里有当前工作目录，则保留“公共文档 + 当前工作目录文档”；
        2. 如果请求里没有当前工作目录，则只保留 `workspace_root` 为空的公共文档；
        3. 这样既能让有上下文的请求复用公共知识，也能在无上下文时避免误命中其他项目文档。
        """
        if not documents:
            return documents

        if not isinstance(project_context, dict):
            return [item for item in documents if not self._normalize_workspace_root(item.get("workspace_root") or "")]

        request_workspace = self._normalize_workspace_root(
            project_context.get("workspace_root") or project_context.get("working_directory") or ""
        )
        if request_workspace:
            return [
                item for item in documents
                if not self._normalize_workspace_root(item.get("workspace_root") or "")
                or self._normalize_workspace_root(item.get("workspace_root") or "") == request_workspace
            ]

        return [item for item in documents if not self._normalize_workspace_root(item.get("workspace_root") or "")]

    def _filter_documents_by_selected_doc(self, documents: list[dict], selected_doc: dict | None) -> list[dict]:
        """在 workspace 过滤后，再按显式选中的文档继续收窄候选。

        这里刻意把“文档选择”放在 workspace 过滤之后，原因是：
        1. 用户请求本身仍然应该先受当前工作域约束；
        2. 文档下拉只是把当前候选集进一步收窄，而不是绕过工作域隔离；
        3. 这样默认“不选文档”与显式“选择单文档”可以共用同一条检索主链路。
        """
        if not documents or not isinstance(selected_doc, dict):
            return documents

        selected_doc_id = str(selected_doc.get("doc_id") or "").strip()
        if selected_doc_id:
            return [item for item in documents if str(item.get("doc_id") or "") == selected_doc_id]

        selected_file_name = Path(str(selected_doc.get("file_name") or "").strip()).name
        if not selected_file_name:
            return documents

        selected_workspace = self._normalize_workspace_root(selected_doc.get("workspace_root") or "")
        return [
            item for item in documents
            if Path(str(item.get("file_name") or "")).name == selected_file_name
            and self._normalize_workspace_root(item.get("workspace_root") or "") == selected_workspace
        ]

    def _is_hit_matching_project(self, hit: dict, project_name: str) -> bool:
        """判断单条命中是否属于当前项目候选。"""
        if not isinstance(hit, dict) or not project_name:
            return False

        file_name = str(hit.get("file_name") or "").strip()
        file_stem = self._normalize_project_name(Path(file_name).stem)
        title = self._normalize_project_name(hit.get("title") or "")
        if file_stem == project_name:
            return True
        if project_name in file_stem:
            return True
        if project_name in title:
            return True
        return False

    def _normalize_project_name(self, value: Any) -> str:
        """把项目名规整成便于比较的轻量形式。"""
        text = str(value or "").strip().lower()
        if not text:
            return ""
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)

    def _normalize_workspace_root(self, value: Any) -> str:
        """规整工作目录，避免尾斜杠等细节导致本应命中的文档失配。"""
        text = str(value or "").strip()
        if not text:
            return ""
        return text.rstrip("/\\")

    def _has_request_workspace(self, project_context: dict | None) -> bool:
        """判断当前请求是否携带可用工作目录。"""
        if not isinstance(project_context, dict):
            return False
        return bool(self._normalize_workspace_root(
            project_context.get("workspace_root") or project_context.get("working_directory") or ""
        ))

    def _cache_get(self, cache: OrderedDict, key):
        """读取 LRU 缓存并刷新最近使用顺序。"""
        if key not in cache:
            return None
        value = cache.pop(key)
        cache[key] = value
        return value

    def _cache_set(self, cache: OrderedDict, key, value, max_items: int) -> None:
        """写入 LRU 缓存并限制容量。"""
        if key in cache:
            cache.pop(key)
        cache[key] = value
        while len(cache) > max_items:
            cache.popitem(last=False)

    async def _get_query_embedding(self, question: str, embedding_model: str) -> list[float]:
        """获取问题 embedding，优先走进程内缓存。"""
        key = (question, embedding_model)
        cached = self._cache_get(self._query_embedding_cache, key)
        if cached is not None:
            return list(cached)
        vector = (await self._embed_texts([question], embedding_model))[0]
        self._cache_set(self._query_embedding_cache, key, list(vector), max_items=128)
        return vector

    def _load_documents_for_retrieval(self) -> list[dict]:
        """返回检索使用的文档集合，并在内存中缓存。"""
        revision = self._current_embedding_cache_revision()
        if self._embedding_cache_revision == revision and self._embedding_cache_documents:
            return self._embedding_cache_documents

        snapshot = self._load_index_data()
        documents = list(snapshot.get("documents", []))
        self._embedding_cache_documents = documents
        self._embedding_cache_revision = revision
        return documents

    def _score_documents(
        self,
        question: str,
        query_vector: list[float],
        documents: list[dict],
        semantic_weight: float,
        keyword_weight: float,
        vector_score_overrides: dict[str, float] | None = None,
        memory_weight: float = 0.0,
        memory_map: dict[str, float] | None = None,
        category_bonus: float = 0.0,
        category_list: list[str] | None = None,
        parent_bonus_map: dict[str, float] | None = None,
    ) -> list[dict]:
        """根据 query 为当前文档集合计算混合分。

        打分策略：
        1. `vector_score`：向量余弦相似度，负责语义召回；
        2. `keyword_score`：问题词在标题 / chunk 文本中的覆盖度与短语命中分，负责精确词补强；
        3. `memory_score`：艾宾浩斯衰减后的记忆值，负责长期验证；
        4. `parent_bonus`：query 关键词命中 heading_path 时的树形加权；
        5. `category_multiplier`：query 分类与 chunk 分类的重叠加成；
        6. `hybrid_score`：(v×sem + kw×kw + mem×mw + parent_bonus) × cat_mult。
        """
        parent_bonus_map = parent_bonus_map or {}
        # 计算记忆分映射
        memory_map = memory_map or {}
        decay_lambda = math.log(2) / 24.0
        now = _utc_now_iso()

        def _get_memory(chunk_id: str) -> float:
            stored = memory_map.get(chunk_id, 0.0)
            if stored <= 0:
                return 0.0
            return stored  # 已衰减的值由 _retrieve 预处理

        def _get_parent_bonus(chunk_id: str) -> float:
            return _safe_float(parent_bonus_map.get(chunk_id), 0.0)

        # 检测 query 涉及的分类
        category_list = category_list or []
        query_categories: list[str] = []
        if category_bonus > 0 and category_list:
            q_lower = question.lower()
            for cat in category_list:
                if any(word.lower() in q_lower for word in cat.split()):
                    query_categories.append(cat)

        def _get_category_multiplier(chunk_categories_str: str) -> float:
            if not query_categories or category_bonus <= 0:
                return 1.0
            chunk_cats = [c.strip() for c in (chunk_categories_str or "").split(",") if c.strip()]
            overlap = len(set(chunk_cats) & set(query_categories))
            if not overlap:
                return 1.0
            ratio = overlap / len(query_categories)
            return 1.0 + category_bonus * ratio

        if vector_score_overrides:
            keyword_scores = self._score_keywords(question, documents)
            scored = []
            for idx, item in enumerate(documents):
                chunk_id = str(item.get("id") or "")
                scored.append(
                    self._build_scored_hit(
                        item,
                        _safe_float(vector_score_overrides.get(chunk_id), 0.0),
                        keyword_scores[idx],
                        semantic_weight,
                        keyword_weight,
                        memory_weight=memory_weight,
                        memory_score=_get_memory(chunk_id),
                        category_multiplier=_get_category_multiplier(str(item.get("categories") or "")),
                        parent_bonus=_get_parent_bonus(chunk_id),
                    )
                )
            return scored

        keyword_scores = self._score_keywords(question, documents)
        scored = []
        for idx, item in enumerate(documents):
            embedding = item.get("embedding") or []
            chunk_id = str(item.get("id") or "")
            scored.append(
                self._build_scored_hit(
                    item,
                    self._cosine_similarity(query_vector, embedding),
                    keyword_scores[idx],
                    semantic_weight,
                    keyword_weight,
                    memory_weight=memory_weight,
                    memory_score=_get_memory(chunk_id),
                    category_multiplier=_get_category_multiplier(str(item.get("categories") or "")),
                    parent_bonus=_get_parent_bonus(chunk_id),
                )
            )
        return scored

    def _build_scored_hit(
        self,
        item: dict,
        vector_score: float,
        keyword_score: float,
        semantic_weight: float,
        keyword_weight: float,
        memory_weight: float = 0.0,
        memory_score: float = 0.0,
        category_multiplier: float = 1.0,
        parent_bonus: float = 0.0,
    ) -> dict:
        """把单条文档命中组装为带多路分数的统一结构。

        分数公式：
        (v×sem + kw×kw + mem×mw + parent_bonus) × category_multiplier
        """
        hybrid_score = (
            (vector_score * semantic_weight)
            + (keyword_score * keyword_weight)
            + (memory_score * memory_weight)
            + parent_bonus
        ) * category_multiplier
        return {
            "score": hybrid_score,
            "hybrid_score": hybrid_score,
            "vector_score": vector_score,
            "keyword_score": keyword_score,
            "memory_score": memory_score,
            "category_multiplier": category_multiplier,
            "parent_bonus": parent_bonus,
            "id": item.get("id"),
            "doc_id": item.get("doc_id", ""),
            "file_name": item.get("file_name"),
            "title": item.get("title"),
            "heading_level": item.get("heading_level", 0),
            "heading_path": item.get("heading_path", ""),
            "chunk_index": item.get("chunk_index"),
            "chunk_text": item.get("chunk_text"),
            "content_hash": item.get("content_hash"),
            "workspace_root": item.get("workspace_root", ""),
            "categories": item.get("categories", ""),
        }

    def _score_keywords(self, question: str, documents: list[dict]) -> list[float]:
        """为候选文档计算归一化 BM25 分。

        设计说明：
        1. query 与 document 统一复用当前的轻量 tokenization，保持中英文行为一致；
        2. BM25 原始分只在当前候选集内比较，因此这里再做一次 0~1 归一化；
        3. 对外字段仍沿用 `keyword_score`，避免打破现有 API 与前端展示结构。
        """
        normalized_question = self._normalize_keyword_text(question)
        query_tokens = list(dict.fromkeys(self._tokenize_keywords(normalized_question)))
        if not normalized_question or not query_tokens or not documents:
            return [0.0 for _ in documents]

        bm25_stats = self._get_bm25_stats(documents)
        if not bm25_stats:
            return [0.0 for _ in documents]

        raw_scores = []
        tokenized_documents = list(bm25_stats.get("tokenized_documents") or [])
        document_lengths = list(bm25_stats.get("document_lengths") or [])
        average_length = float(bm25_stats.get("average_length") or 0.0)
        document_frequency = dict(bm25_stats.get("document_frequency") or {})
        doc_count = max(1, len(tokenized_documents))
        k1 = 1.5
        b = 0.75

        for idx, tokens in enumerate(tokenized_documents):
            length = float(document_lengths[idx] if idx < len(document_lengths) else len(tokens))
            term_frequency = self._build_term_frequency(tokens)
            score = 0.0
            for token in query_tokens:
                freq = float(term_frequency.get(token) or 0.0)
                if freq <= 0:
                    continue
                df = int(document_frequency.get(token) or 0)
                idf = math.log(1.0 + ((doc_count - df + 0.5) / (df + 0.5)))
                denominator = freq + k1 * (1.0 - b + b * (length / max(1e-6, average_length)))
                score += idf * ((freq * (k1 + 1.0)) / max(1e-6, denominator))
            raw_scores.append(score)
        return self._normalize_keyword_scores(raw_scores)

    def _normalize_keyword_text(self, text: Any) -> str:
        """把文本规整成适合关键词匹配的形式。"""
        normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
        return normalized

    def _tokenize_keywords(self, text: str) -> list[str]:
        """从问题中提取关键词 token。

        这里同时兼顾中英文：
        - 英文 / 数字 / 连字符词使用正则拆词；
        - 中文优先使用 `jieba3 small` 做自然分词，让“考试大纲 / 复习计划 / 订单管理”
          这类词组更稳定地进入 BM25；
        - 若当前环境没有安装 `jieba3`，则自动回退到现有的 2~4 字滑窗，避免
          检索链路因为可选依赖缺失直接不可用。
        """
        if not text:
            return []
        latin_tokens = re.findall(r"[a-z0-9][a-z0-9_\-\.]{1,}", text)
        cjk_tokens = []
        for segment in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            cjk_tokens.append(segment)
            cjk_tokens.extend(self._tokenize_cjk_segment(segment))
        return latin_tokens + cjk_tokens

    def _tokenize_cjk_segment(self, segment: str) -> list[str]:
        """对连续中文片段执行“jieba3 small 优先、滑窗回退”的分词。

        设计说明：
        1. query 与 document 两侧都复用这一入口，避免分词语义漂移；
        2. 优先使用 `jieba3 small`，是为了减少纯滑窗带来的高噪声碎片 token；
        3. 回退逻辑继续保留，保证本地环境没装 `jieba3` 时仍能工作。
        """
        segment = str(segment or "").strip()
        if len(segment) < 2:
            return []
        jieba3_tokens = self._tokenize_cjk_with_jieba3(segment)
        if jieba3_tokens:
            return jieba3_tokens
        return self._expand_cjk_segment_tokens(segment)

    def _tokenize_cjk_with_jieba3(self, segment: str) -> list[str]:
        """使用 `jieba3 small` 对中文连续片段做自然分词。

        这里会过滤掉单字 token：
        1. 单字 token 在 BM25 里通常噪声很高；
        2. 更长的中文词组更符合当前知识库检索的目标；
        3. 整段原文仍会在外层保留，因此不会完全丢掉长短语信号。
        """
        tokenizer = self._get_jieba3_small_tokenizer()
        if tokenizer is None:
            return []
        try:
            tokens = []
            for raw in tokenizer.cut_text(segment):
                token = str(raw or "").strip()
                if len(token) < 2:
                    continue
                tokens.append(token)
            return tokens
        except Exception as exc:
            self.logger.warning("[markdown_kb] jieba3 small 中文分词失败，已回退到滑窗分词: %s", exc)
            return []

    def _get_jieba3_small_tokenizer(self):
        """懒加载 `jieba3 small` 分词器实例。

        这里刻意做成延迟初始化：
        1. 避免插件导入阶段就触发模型文件加载，减少启动抖动；
        2. 让未安装 `jieba3` 的环境仍能走完整回退链路；
        3. 单实例复用可以避免 query/document 两侧重复构造 tokenizer。
        """
        if getattr(self, "_jieba3_small_tokenizer", None) is not None:
            return self._jieba3_small_tokenizer
        if getattr(self, "_jieba3_small_tokenizer_failed", False):
            return None
        if Jieba3Tokenizer is None:
            if not self._jieba3_warned_unavailable:
                self.logger.info("[markdown_kb] jieba3 不可用，中文 BM25 分词已回退到 2~4 字滑窗")
                self._jieba3_warned_unavailable = True
            return None
        try:
            self._jieba3_small_tokenizer = Jieba3Tokenizer(model="small")
            return self._jieba3_small_tokenizer
        except Exception as exc:
            self._jieba3_small_tokenizer_failed = True
            if not self._jieba3_warned_unavailable:
                self.logger.warning("[markdown_kb] jieba3 small 初始化失败，中文 BM25 分词已回退到滑窗分词: %s", exc)
                self._jieba3_warned_unavailable = True
            return None

    def _expand_cjk_segment_tokens(self, segment: str) -> list[str]:
        """把连续中文片段展开成 2~4 字滑窗 token。

        设计目标：
        1. 不再把整段本身作为高权重 token，只补充更短窗口；
        2. 让“考试大纲 / 复习计划”这类局部短语能被正常识别；
        3. 不引入额外中文分词依赖，保持当前插件最小闭环特性。
        """
        segment = str(segment or "").strip()
        if len(segment) < 2:
            return []
        tokens = []
        max_window = min(4, len(segment))
        for window in range(2, max_window + 1):
            for start in range(0, len(segment) - window + 1):
                tokens.append(segment[start:start + window])
        return tokens

    def _get_bm25_stats(self, documents: list[dict]) -> dict:
        """返回当前文档集合对应的 BM25 统计缓存。"""
        revision = self._current_embedding_cache_revision()
        if self._bm25_cache_revision == revision and self._bm25_cache_documents == documents and self._bm25_cache_stats:
            return self._bm25_cache_stats

        tokenized_documents = [self._tokenize_document_for_bm25(item) for item in documents]
        document_lengths = [len(tokens) for tokens in tokenized_documents]
        average_length = (sum(document_lengths) / len(document_lengths)) if document_lengths else 0.0
        document_frequency: dict[str, int] = {}
        for tokens in tokenized_documents:
            for token in set(tokens):
                document_frequency[token] = document_frequency.get(token, 0) + 1
        stats = {
            "tokenized_documents": tokenized_documents,
            "document_lengths": document_lengths,
            "average_length": average_length,
            "document_frequency": document_frequency,
        }
        self._bm25_cache_revision = revision
        self._bm25_cache_documents = list(documents)
        self._bm25_cache_stats = stats
        return stats

    def _tokenize_document_for_bm25(self, item: dict) -> list[str]:
        """为单个候选文档生成 BM25 token 序列。"""
        title_text = self._normalize_keyword_text(item.get("title") or "")
        chunk_text = self._normalize_keyword_text(item.get("chunk_text") or "")
        merged = "\n".join(part for part in [title_text, chunk_text] if part)
        tokens = self._tokenize_keywords(merged)
        if title_text:
            title_tokens = self._tokenize_keywords(title_text)
            # 标题词额外重复一遍，等价于给标题更高词频权重，尽量保留旧逻辑中的标题偏置。
            tokens.extend(title_tokens)
        return tokens

    def _build_term_frequency(self, tokens: list[str]) -> dict[str, int]:
        """统计单篇文档内 token 词频。"""
        frequencies: dict[str, int] = {}
        for token in tokens:
            if not token:
                continue
            frequencies[token] = frequencies.get(token, 0) + 1
        return frequencies

    def _normalize_keyword_scores(self, raw_scores: list[float]) -> list[float]:
        """把 BM25 原始分压到 0~1，便于与向量分融合。"""
        if not raw_scores:
            return []
        minimum = min(raw_scores)
        maximum = max(raw_scores)
        if maximum - minimum <= 1e-9:
            return [0.0 for _ in raw_scores]
        return [
            max(0.0, min(1.0, (float(score) - minimum) / (maximum - minimum)))
            for score in raw_scores
        ]

    def _vector_compute_backend_label(self) -> str:
        """返回当前向量计算后端标签，便于状态页展示。"""
        return "python"

    def _vector_retrieval_backend_label(self) -> str:
        """返回当前第一阶段向量召回后端标签。

        这里区分“向量召回”和“向量计算”两个概念：
        - `vector_retrieval_backend` 表示第一阶段粗召回到底走 sqlite-vec 还是 Python；
        - `vector_compute_backend` 始终为 python。
        """
        stats = self._store.stats()
        if bool(stats.get("vec_enabled")):
            return "sqlite-vec"
        return self._vector_compute_backend_label()

    def _extract_user_question_for_chat(self, request: dict) -> str:
        """从 Chat Completions 请求中抽取最后一条用户问题。"""
        messages = request.get("messages")
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
                        parts.append(str(item.get("text") or ""))
                return "\n".join(part for part in parts if part).strip()
        return ""

    def _extract_user_question_for_messages(self, request: dict) -> str:
        """从 Anthropic Messages 风格请求中抽取最后一个用户问题。"""
        return self._extract_user_question_for_chat(request)

    def _extract_user_question_for_responses(self, request: dict) -> str:
        """从 Responses 请求中尽量抽取最后一个用户问题。"""
        input_value = request.get("input")
        if isinstance(input_value, str):
            return input_value.strip()
        if isinstance(input_value, list):
            for item in reversed(input_value):
                if not isinstance(item, dict) or item.get("role") != "user":
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") in {"input_text", "text"}:
                            parts.append(str(block.get("text") or ""))
                    return "\n".join(part for part in parts if part).strip()
        return ""

    def _extract_project_context(self, request: dict) -> dict:
        """从不同客户端请求体中抽取统一的工作域上下文。

        当前先兼容三类来源：
        1. OpenCode：`messages[].content` 中的 `<env>...</env>` 文本
        2. Codex Responses：`input[].content[].text` 中的 `<environment_context>...</environment_context>` 文本
        3. Claude Messages：文本中的 `Primary working directory`

        设计原则：
        1. 只做保守抽取，不臆测不存在的字段；
        2. 优先使用 workspace/root 级信息，其次才回退到 working directory；
        3. 返回结构统一，便于后续直接注入提示词或进一步做项目级过滤。
        """
        if not isinstance(request, dict):
            return {}

        direct_workspace = str(request.get("workspace_root") or "").strip()
        direct_working_directory = str(request.get("working_directory") or "").strip()
        if direct_workspace or direct_working_directory:
            return self._normalize_project_context({
                "workspace_root": direct_workspace,
                "working_directory": direct_working_directory,
                "source": "direct.payload",
            })

        messages = request.get("messages")
        if isinstance(messages, list):
            context = self._extract_project_context_from_opencode_messages(messages)
            if context:
                return context
            context = self._extract_project_context_from_message_texts(messages)
            if context:
                return context

        input_value = request.get("input")
        if isinstance(input_value, list):
            context = self._extract_project_context_from_responses_input(input_value)
            if context:
                return context

        system_value = request.get("system")
        context = self._extract_project_context_from_claude_system(system_value)
        if context:
            return context
        return {}

    def _extract_project_context_from_opencode_messages(self, messages: list[Any]) -> dict:
        """从 OpenCode 风格消息文本中的 `<env>` 片段抽取工作域信息。"""
        for message in messages:
            if not isinstance(message, dict):
                continue
            for text in self._iter_text_blocks(message.get("content")):
                workspace_root = self._extract_xml_like_env_value(text, "Workspace root folder")
                working_directory = self._extract_xml_like_env_value(text, "Working directory")
                if workspace_root or working_directory:
                    return self._normalize_project_context({
                        "workspace_root": workspace_root,
                        "working_directory": working_directory,
                        "source": "opencode.messages.content.env",
                    })
        return {}

    def _extract_project_context_from_responses_input(self, input_value: list[Any]) -> dict:
        """从 Codex Responses 文本里的 `<environment_context>` 抽取工作域信息。"""
        for item in input_value:
            if not isinstance(item, dict):
                continue
            for text in self._iter_text_blocks(item.get("content")):
                workspace_root = self._extract_tag_value(text, "workspace_root")
                working_directory = self._extract_tag_value(text, "cwd")
                if workspace_root or working_directory:
                    return self._normalize_project_context({
                        "workspace_root": workspace_root,
                        "working_directory": working_directory,
                        "source": "codex.input.content.text.environment_context",
                    })
        return {}

    def _extract_project_context_from_claude_system(self, system_value: Any) -> dict:
        """从 Claude Messages 的 system 文本中抽取工作目录。

        Claude 当前已知形态通常会在 `system[].text` 里写入：
        `Primary working directory: /path/to/project`

        这里仅提取这一条，不尝试从整段 system prompt 中做更激进的语义解析。
        """
        blocks = system_value if isinstance(system_value, list) else [system_value]
        for block in blocks:
            text = ""
            if isinstance(block, dict):
                text = str(block.get("text") or "")
            elif isinstance(block, str):
                text = block
            if not text:
                continue
            match = re.search(r"Primary working directory\s*:\s*(.+)", text)
            if not match:
                continue
            working_directory = match.group(1).strip()
            return self._normalize_project_context({
                "workspace_root": "",
                "working_directory": working_directory,
                "source": "claude.system.text",
            })
        for text in self._iter_text_blocks(system_value):
            match = re.search(r"Primary working directory\s*:\s*(.+)", text)
            if not match:
                continue
            working_directory = match.group(1).strip()
            return self._normalize_project_context({
                "workspace_root": "",
                "working_directory": working_directory,
                "source": "claude.messages.content.text",
            })
        return {}

    def _extract_project_context_from_message_texts(self, messages: list[Any]) -> dict:
        """从任意消息文本块中回退提取工作目录提示。

        设计目的：
        1. 兼容把环境提示包进 `messages[].content[].text` 的客户端；
        2. 避免把 Claude / 其他兼容客户端强耦合到单一顶层字段结构；
        3. 当前只识别 `Primary working directory` 这一条保守信号。
        """
        for message in messages:
            if not isinstance(message, dict):
                continue
            for text in self._iter_text_blocks(message.get("content")):
                match = re.search(r"Primary working directory\s*:\s*(.+)", text)
                if not match:
                    continue
                working_directory = match.group(1).strip()
                return self._normalize_project_context({
                    "workspace_root": "",
                    "working_directory": working_directory,
                    "source": "claude.messages.content.text",
                })
        return {}

    def _iter_text_blocks(self, value: Any) -> list[str]:
        """把不同协议里的文本块统一摊平成字符串列表。"""
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            text = value.get("text")
            return [str(text)] if text else []
        if isinstance(value, list):
            texts = []
            for item in value:
                texts.extend(self._iter_text_blocks(item))
            return texts
        return []

    def _extract_xml_like_env_value(self, text: str, label: str) -> str:
        """从 OpenCode `<env>` 文本块中提取 `Label: value` 形式字段。"""
        if not isinstance(text, str) or not text:
            return ""
        match = re.search(rf"{re.escape(label)}\s*:\s*(.+)", text)
        return match.group(1).strip() if match else ""

    def _extract_tag_value(self, text: str, tag_name: str) -> str:
        """从类似 XML 的上下文文本中提取指定标签内容。"""
        if not isinstance(text, str) or not text:
            return ""
        match = re.search(rf"<{re.escape(tag_name)}>(.*?)</{re.escape(tag_name)}>", text, re.DOTALL)
        return match.group(1).strip() if match else ""

    def _normalize_project_context(self, raw: dict) -> dict:
        """把不同来源的工作域字段收敛成统一结构。"""
        if not isinstance(raw, dict):
            return {}

        workspace_root = str(raw.get("workspace_root") or "").strip()
        working_directory = str(raw.get("working_directory") or "").strip()
        source = str(raw.get("source") or "").strip()
        canonical_root = workspace_root or working_directory
        if not canonical_root:
            return {}

        project_name = Path(canonical_root).name.strip()
        return {
            "workspace_root": workspace_root,
            "working_directory": working_directory,
            "project_name": project_name,
            "project_id": canonical_root,
            "source": source,
        }

    def _detect_request_protocol(self, request: dict) -> str:
        """根据请求体特征判断当前协议类型。"""
        if isinstance(request.get("messages"), list):
            if "max_tokens" in request and "system" in request:
                return "messages"
            return "chat"
        if "input" in request:
            return "responses"
        return ""

    def _build_injection_text(self, hits: list[dict], project_context: dict | None = None) -> str:
        """把命中片段组装成统一注入模板。"""
        parts = [
            "以下是与当前问题相关的参考资料。你必须优先依据这些资料回答。",
            "",
            "如果资料不足以支持结论，必须明确回答“不知道”或“资料中未提及”。",
            "",
        ]
        if isinstance(project_context, dict) and project_context:
            parts.extend([
                "当前工作域：",
                f"- project_name: {project_context.get('project_name') or '-'}",
                f"- workspace_root: {project_context.get('workspace_root') or '-'}",
                f"- working_directory: {project_context.get('working_directory') or '-'}",
                f"- source: {project_context.get('source') or '-'}",
                "",
                "回答时请优先以当前工作域为准；如果参考资料无法证明与当前工作域一致，必须明确说明“资料中未提及”。",
                "",
            ])
        parts.extend([
            "参考资料：",
        ])
        for idx, hit in enumerate(hits, start=1):
            parts.append(f"[片段 {idx}] 文件: {hit.get('file_name', '-')} | 标题: {hit.get('title', '-')} | chunk: {hit.get('chunk_index', 0)}")
            parts.append(str(hit.get("chunk_text") or ""))
            parts.append("---")
        return "\n".join(parts).rstrip("-\n")

    def _inject_for_chat(self, request: dict, injection_text: str) -> dict:
        """为 Chat Completions 注入 system 参考资料。"""
        messages = list(request.get("messages") or [])
        messages.insert(0, {"role": "system", "content": injection_text})
        request["messages"] = messages
        return request

    def _inject_for_messages(self, request: dict, injection_text: str) -> dict:
        """为 Messages 请求注入 system。"""
        original = request.get("system")
        if isinstance(original, str) and original.strip():
            request["system"] = injection_text + "\n\n原始系统要求：\n" + original.strip()
        else:
            request["system"] = injection_text
        return request

    def _inject_for_responses(self, request: dict, injection_text: str) -> dict:
        """为 Responses 请求注入 instructions。"""
        original = str(request.get("instructions") or "").strip()
        request["instructions"] = injection_text if not original else injection_text + "\n\n原始系统要求：\n" + original
        return request

    async def on_request(self, request) -> dict | None:
        """为三类文本请求执行按需知识库注入。

        当前规则：
        1. 仅处理 `chat / messages / responses` 三类文本请求；
        2. 默认对这三类请求都尝试抽取最后一个用户问题并执行检索；
        3. 只有检索命中非空时才真正注入参考资料；
        4. 请求里的模型名不再承担知识库开关语义，保持原样继续下游转发；
        5. 没命中、抽不到问题或检索失败时都直接透传。
        """
        self._ensure_runtime_ready()
        if not isinstance(request, dict):
            return None

        protocol = self._detect_request_protocol(request)
        if not protocol:
            return request

        if protocol == "chat":
            question = self._extract_user_question_for_chat(request)
        elif protocol == "messages":
            question = self._extract_user_question_for_messages(request)
        elif protocol == "responses":
            question = self._extract_user_question_for_responses(request)
        else:
            return request

        if not question:
            return request

        project_context = self._extract_project_context(request)

        settings = self._settings()
        try:
            hits = await self._retrieve(
                question,
                settings["top_k"],
                settings["embedding_model"],
                settings["reranker_model"],
                project_context,
            )
        except Exception:
            return request

        if not hits:
            return request

        injection_text = self._build_injection_text(hits, project_context)
        if protocol == "chat":
            return self._inject_for_chat(request, injection_text)
        if protocol == "messages":
            return self._inject_for_messages(request, injection_text)
        if protocol == "responses":
            return self._inject_for_responses(request, injection_text)
        return request

    async def _rerank_hits(self, question: str, hits: list[dict], model: str) -> list[dict]:
        """对向量召回结果执行第二阶段 rerank。

        设计说明：
        1. 第一阶段仍然先用 embedding 做粗召回；
        2. 只有在显式配置 `reranker_model` 时才调用 `/v1/rerank`；
        3. rerank 只处理少量候选，避免额外成本无限放大。
        """
        payload = {
            "model": model,
            "query": question,
            "documents": [item.get("chunk_text") or "" for item in hits],
        }

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self._akm_base_url()}/rerank", json=payload)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="rerank 接口返回了非法 JSON") from exc

        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=data.get("detail") or data.get("error") or "rerank 请求失败")

        reranked = []
        index_map = {idx: item for idx, item in enumerate(hits)}
        used_indices = set()
        for row in data.get("results", []):
            idx = _safe_int(row.get("index"), -1)
            if idx < 0 or idx not in index_map:
                continue
            base = dict(index_map[idx])
            base.setdefault("vector_score", base.get("score", 0.0))
            base.setdefault("keyword_score", 0.0)
            base.setdefault("hybrid_score", base.get("score", 0.0))
            base["rerank_score"] = _safe_float(row.get("relevance_score"), 0.0)
            base["score"] = base["rerank_score"]
            reranked.append(base)
            used_indices.add(idx)

        for idx, item in enumerate(hits):
            if idx in used_indices:
                continue
            base = dict(item)
            base.setdefault("vector_score", base.get("score", 0.0))
            base.setdefault("keyword_score", 0.0)
            base.setdefault("hybrid_score", base.get("score", 0.0))
            base["rerank_score"] = None
            reranked.append(base)
        return reranked

    async def _embed_texts(self, texts: list[str], model: str) -> list[list[float]]:
        """通过本地 AKM `/v1/embeddings` 生成向量。"""
        if not texts:
            return []

        payload = {"model": model, "input": texts}
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self._akm_base_url()}/embeddings", json=payload)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="embedding 接口返回了非法 JSON") from exc

        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=data.get("detail") or data.get("error") or "embedding 请求失败")

        vectors = []
        for item in data.get("data", []):
            vectors.append([_safe_float(v, 0.0) for v in item.get("embedding", [])])
        return vectors

    async def _generate_answer(self, question: str, hits: list[dict], model: str) -> str:
        """基于命中片段调用本地 AKM chat 接口生成回答。"""
        context_parts = []
        for idx, hit in enumerate(hits, start=1):
            context_parts.append(
                f"[片段 {idx}] 文件: {hit['file_name']} | 标题: {hit['title']} | chunk: {hit['chunk_index']}\n{hit['chunk_text']}"
            )
        context = "\n\n---\n\n".join(context_parts)

        system_prompt = (
            "你是一个严格依据知识库内容回答问题的助手。"
            "你只能根据提供的参考资料作答；如果资料不足以支持结论，必须明确回答“资料中未提及”或“不知道”。"
            "回答时尽量简洁，并优先引用资料中的原始信息。"
        )
        user_prompt = f"参考资料：\n{context}\n\n用户问题：{question}"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self._akm_base_url()}/chat/completions", json=payload)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="chat 接口返回了非法 JSON") from exc

        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=data.get("detail") or data.get("error") or "chat 请求失败")

        try:
            return str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise HTTPException(status_code=502, detail="chat 返回结构不符合预期") from exc

    async def _merge_chunks_via_llm(self, old_text: str, new_text: str, model: str) -> str | None:
        """调用本地 chat 模型判断新文本是否补充了已有文本没有的信息，有则合并返回。

        返回合并后的精简文本；若新文本只是重复已有信息或调用失败则返回 None，调用方降级为只 boost 记忆。
        system_prompt 和 user_prompt 均可通过插件配置页编辑优化，user_prompt 支持 {old_text} / {new_text} 占位符。
        """
        settings = self._settings()
        system_prompt = (settings.get("merge_chunks_system_prompt") or "").strip() or (
            "你是一个知识合并助手。比较已有文本和新文本，判断新文本是否包含已有文本中没有的实质性补充信息。"
            "实质性补充指：新事实、新步骤、新细节、新约束条件、新注意事项等。"
            "如果新文本只是换个说法重复已有信息，视为无补充。"
        )
        user_prompt_template = (settings.get("merge_chunks_user_prompt") or "").strip() or (
            "## 已有文本\n"
            "{old_text}\n\n"
            "## 新文本\n"
            "{new_text}\n\n"
            "请判断新文本是否有实质性补充。"
            "如果有，将两段合并为一段精简连贯的 Markdown 文本，保留原有结构要点，不重复，输出合并后的文本。"
            "如果没有任何实质性补充，只输出 NO_NEW_INFO。"
            "不要输出任何其他解释或前缀。"
        )
        user_prompt = user_prompt_template.replace("{old_text}", old_text).replace("{new_text}", new_text)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    f"{self._akm_base_url()}/chat/completions", json=payload
                )
            data = response.json()
            if response.status_code != 200:
                self.logger.warning(
                    "[markdown_kb] 合并 LLM 请求失败 status=%d", response.status_code,
                )
                return None
            content = str(data["choices"][0]["message"]["content"] or "").strip()
        except Exception as exc:
            self.logger.warning("[markdown_kb] 合并 LLM 调用异常: %s", exc)
            return None

        if not content or content.upper().startswith("NO_NEW_INFO"):
            return None
        return content

    async def _generate_learn_summary(self, request: dict, model: str) -> dict:
        """调用本地 chat 接口，把候选材料提炼成可落盘的知识结构。

        这里要求模型返回 JSON，而不是直接返回最终 Markdown 文档，主要出于两个考虑：
        1. 服务端仍需要统一包一层稳定元信息壳子，便于后续检索与排障；
        2. 结构化结果能更清楚地区分“无可沉淀知识”与“有知识但标题/摘录为空”两类情况。
        """
        source_label = {
            "codex": "Codex",
            "claude_code": "Claude Code",
        }.get(request["source"], request["source"])
        material_prompt = self._build_learn_material_prompt(request)
        settings = self._settings()
        default_learn_prompt = (
            "你负责把一次 AI 协作对话提炼成可检索的 Markdown 知识条目。"
            "只保留稳定、可复用的知识结论、原因、判断依据、修复方式和注意事项。"
            "不要输出触发关键词，不要把结果写成完整聊天记录。"
            "如果材料不足以沉淀为稳定知识，请返回 should_learn=false。"
            "你必须只返回 JSON 对象，不要输出代码块围栏。"
            "JSON 字段固定为："
            "should_learn(boolean), "
            "title(string), "
            "keywords(array[string], 3-8个核心中文/英文关键词，用于增强检索匹配度，提取文中核心概念和技术名词), "
            "categories(array[string], 从以下类别中选择 1-3 个最匹配的: 技术实现、业务逻辑、架构设计、调试修复、配置部署、代码风格), "
            "summary_markdown(string，使用 Markdown，可含小标题和列表，但不要包含最外层 # 标题，也不要包含\"关键原话摘录\"标题), "
            "quotes(array[string]，最多 3 条关键原话摘录)。"
        )
        system_prompt = (settings.get("learn_summary_system_prompt") or "").strip() or default_learn_prompt
        user_prompt = (
            f"来源：{source_label}\n"
            f"触发阶段：{request['trigger_phase']}\n"
            f"工作区：{request['workspace_root'] or '未提供'}\n"
            f"标题提示：{request['title_hint'] or '未提供'}\n\n"
            f"{material_prompt}"
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self._akm_base_url()}/chat/completions", json=payload)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="learn chat 接口返回了非法 JSON") from exc

        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=data.get("detail") or data.get("error") or "learn chat 请求失败")

        try:
            raw_content = str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise HTTPException(status_code=502, detail="learn chat 返回结构不符合预期") from exc

        normalized = raw_content.strip()
        if normalized.startswith("```"):
            normalized = re.sub(r"^```(?:json)?\s*", "", normalized, count=1).strip()
            normalized = re.sub(r"\s*```$", "", normalized, count=1).strip()
        try:
            parsed = json.loads(normalized)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="learn chat 未返回合法 JSON 对象") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=502, detail="learn chat 返回结构不符合预期")
        return parsed

    def _normalize_learn_request(self, payload: dict) -> dict:
        """校验并规整 `/learn` 请求体。"""
        data = payload if isinstance(payload, dict) else {}
        source = str(data.get("source") or "").strip().lower()
        if source not in {"codex", "claude_code"}:
            raise HTTPException(status_code=400, detail="source 仅支持 codex 或 claude_code")
        trigger_phase = str(data.get("trigger_phase") or "").strip().lower()
        if trigger_phase not in {"stop", "pre_compact"}:
            raise HTTPException(status_code=400, detail="trigger_phase 仅支持 stop 或 pre_compact")
        session_id = str(data.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id 不能为空")
        dedupe_key = str(data.get("dedupe_key") or "").strip()
        if not dedupe_key:
            raise HTTPException(status_code=400, detail="dedupe_key 不能为空")
        workspace_root = self._normalize_workspace_root(data.get("workspace_root") or "")
        normalized_excerpt = []
        raw_excerpt = data.get("conversation_excerpt")
        if isinstance(raw_excerpt, list):
            for item in raw_excerpt:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip() or "unknown"
                text = str(item.get("text") or "").strip()
                if text:
                    normalized_excerpt.append({
                        "role": role,
                        "text": text,
                    })
        return {
            "source": source,
            "trigger_phase": trigger_phase,
            "session_id": session_id,
            "turn_id": str(data.get("turn_id") or "").strip(),
            "workspace_root": workspace_root,
            "title_hint": str(data.get("title_hint") or "").strip(),
            "user_prompt": str(data.get("user_prompt") or "").strip(),
            "assistant_excerpt": str(data.get("assistant_excerpt") or "").strip(),
            "conversation_excerpt": normalized_excerpt,
            "learn_keyword": str(data.get("learn_keyword") or "").strip(),
            "dedupe_key": dedupe_key,
        }

    def _build_learn_material_prompt(self, request: dict) -> str:
        """把请求里的候选材料整理成一个清晰的模型输入块。"""
        lines = [
            "候选材料如下。",
            f"用户问题：{request['user_prompt'] or '未提供'}",
            f"助手摘录：{request['assistant_excerpt'] or '未提供'}",
        ]
        if request["learn_keyword"]:
            lines.append(f"触发关键词：{request['learn_keyword']}（仅用于审计，不要出现在最终知识中）")
        if request["conversation_excerpt"]:
            lines.append("")
            lines.append("对话摘录：")
            for item in request["conversation_excerpt"]:
                lines.append(f"- {item['role']}: {item['text']}")
        return "\n".join(lines).strip()

    def _resolve_learn_title(self, learn_result: dict, request: dict) -> str:
        """确定最终知识文档标题，优先使用模型结果，回退到请求侧提示。"""
        candidates = [
            str((learn_result or {}).get("title") or "").strip(),
            str(request.get("title_hint") or "").strip(),
            str(request.get("user_prompt") or "").strip(),
            "未命名知识条目",
        ]
        for item in candidates:
            cleaned = re.sub(r"\s+", " ", item).strip(" #\t\r\n")
            if cleaned:
                return cleaned[:80]
        return "未命名知识条目"

    def _normalize_learn_quotes(self, value: Any) -> list[str]:
        """规整模型返回的关键原话摘录。"""
        quotes = []
        if not isinstance(value, list):
            return quotes
        for item in value:
            text = str(item or "").strip()
            if text:
                quotes.append(text[:500])
            if len(quotes) >= 3:
                break
        return quotes

    def _make_learn_file_name(self, title: str, dedupe_key: str, request: dict) -> str:
        """生成学习文档文件名。

        这里使用“日期 + dedupe hash + 标题短名”的组合，而不是纯时间戳，
        这样在极端情况下即使同一轮因为异常重试，也更容易落到同一个逻辑文件名。
        """
        date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        short_hash = hashlib.sha1(dedupe_key.encode("utf-8")).hexdigest()[:8]
        title_seed = str(request.get("title_hint") or request.get("user_prompt") or title or "").strip()
        safe_title = re.sub(r"[\\/:*?\"<>|\s]+", "-", title_seed).strip("-.")[:40]
        if not safe_title:
            safe_title = "learn"
        return f"{date_prefix}-{short_hash}-{safe_title}.learn.md"

    def _render_learn_document(
        self, title: str, request: dict, summary_markdown: str, quotes: list[str],
        keywords: list[str] | None = None, categories: list[str] | None = None,
    ) -> str:
        """把结构化学习结果包装成最终落盘的 Markdown 文档。

        新模板一个 .learn.md 就是一个知识点（一个 chunk）：
        - H2 标题改为粗体，避免切片器按标题拆分
        - 新增关联关键词和知识分类元数据行
        """
        source_label = {
            "codex": "Codex",
            "claude_code": "Claude Code",
        }.get(request["source"], request["source"])
        lines = [
            f"# {title}",
            "",
        ]
        # 关联关键词（chat 模型自动生成，用于增强 BM25 和语义检索匹配度）
        if keywords:
            lines.append(f"**关联关键词**：{', '.join(str(k).strip() for k in keywords if str(k).strip())}")
            lines.append("")
        # 知识分类（chat 模型自动归类：技术实现/业务逻辑/架构设计/调试修复/配置部署/代码风格）
        if categories:
            lines.append(f"**知识分类**：{', '.join(str(c).strip() for c in categories if str(c).strip())}")
            lines.append("")
        lines.extend([
            f"- 来源：{source_label}",
            f"- 触发阶段：{request['trigger_phase']}",
            f"- 工作区：{request['workspace_root'] or '未提供'}",
            f"- 会话 ID：{request['session_id']}",
        ])
        if request.get("turn_id"):
            lines.append(f"- 轮次 ID：{request['turn_id']}")
        lines.extend([
            f"- 记录时间：{_utc_now_iso()}",
            "",
            "---",
            "",
            "**知识摘要**",
            "",
            summary_markdown.strip(),
        ])
        if quotes:
            lines.extend([
                "",
                "**关键原话摘录**",
                "",
            ])
            for quote in quotes:
                lines.append(f'- "{quote}"')
                lines.append("")
            if lines and not lines[-1].strip():
                lines.pop()
        return "\n".join(lines).strip() + "\n"

    def _load_learn_records(self) -> dict[str, dict]:
        """读取学习幂等记录。

        第一版只需要一个轻量的本地 JSON 文件：
        1. 记录已经处理过的 `dedupe_key`；
        2. 记录最终落盘文件名与状态；
        3. 不引入新的数据库或全局审计表，保持实现边界最小。
        """
        path = getattr(self, "_learn_records_path", None)
        if path is None or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        result = {}
        for key, value in data.items():
            normalized_key = str(key or "").strip()
            if normalized_key and isinstance(value, dict):
                result[normalized_key] = dict(value)
        return result

    def _save_learn_records(self, records: dict[str, dict]) -> None:
        """保存学习幂等记录。"""
        path = getattr(self, "_learn_records_path", None)
        if path is None:
            raise RuntimeError("markdown_kb learn 记录存储尚未初始化")
        normalized = {}
        for key, value in (records or {}).items():
            normalized_key = str(key or "").strip()
            if normalized_key and isinstance(value, dict):
                normalized[normalized_key] = value
        path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), "utf-8")

    def _akm_base_url(self) -> str:
        """根据全局配置构造本地 AKM v1 基础地址。"""
        cfg = load_config()
        port = _safe_int(cfg.get("server_port"), 8800)
        return f"http://127.0.0.1:{port}/v1"

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """计算两个向量的余弦相似度。"""
        if not a or not b or len(a) != len(b):
            return 0.0
        numerator = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a <= 0 or norm_b <= 0:
            return 0.0
        return numerator / (norm_a * norm_b)
