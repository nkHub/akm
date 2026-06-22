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
    from markdown_chunker import MarkdownChunkingStrategy
except Exception:  # pragma: no cover - 运行环境未安装依赖时自动回退
    MarkdownChunkingStrategy = None

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

from akm.config import load_config

from akm.plugins import PluginBase


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


def _normalize_weight_pair(primary: float, secondary: float) -> tuple[float, float]:
    """把两路权重归一化到 0~1，总和为 1。

    说明：
    1. 配置面板里允许用户填任意非负数，因此这里统一做一次归一化；
    2. 如果两项都 <= 0，则自动回退为纯主路权重，避免出现全 0 导致所有命中分都被压成 0；
    3. 当前 markdown_kb 中 primary 对应语义分，secondary 对应关键词分。
    """
    primary = max(0.0, float(primary or 0.0))
    secondary = max(0.0, float(secondary or 0.0))
    total = primary + secondary
    if total <= 0:
        return 1.0, 0.0
    return primary / total, secondary / total


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

    当前版本先把：
    1. 文件元数据
    2. chunk 元数据
    3. embedding 向量
    统一收口到一个 SQLite 文件里。

    检索阶段暂时仍由 Python 层读取向量后计算相似度，避免在本地 SQLite Vector
    扩展尚未确认安装方式之前，就把迁移绑定到某个未验证的运行时依赖上。
    """

    backend_name = "sqlite"

    def __init__(self, db_path: Path, logger):
        self.db_path = db_path
        self.logger = logger
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()
            if row and row[0] > 0:
                self._ensure_column(conn, "kb_documents", "workspace_root", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, "kb_chunks", "workspace_root", "TEXT NOT NULL DEFAULT ''")
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
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
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
        metadata = {
            "last_rebuilt_at": (data or {}).get("last_rebuilt_at"),
            "embedding_model": (data or {}).get("embedding_model"),
        }
        self.replace_all(documents, metadata)

    def replace_all(self, documents: list[dict], metadata: dict) -> dict:
        conn = self._connect()
        try:
            now = metadata.get("last_rebuilt_at")
            conn.execute("DELETE FROM kb_vectors")
            conn.execute("DELETE FROM kb_chunks")
            conn.execute("DELETE FROM kb_documents")

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
                        chunk_index, chunk_text, content_hash, created_at, updated_at, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.get("id"),
                        item.get("doc_id"),
                        item.get("file_name") or "",
                        item.get("file_path") or "",
                        item.get("workspace_root") or "",
                        item.get("title") or "",
                        _safe_int(item.get("heading_level"), 0),
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

            self._save_meta_map(conn, metadata)
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
            conn.execute("DELETE FROM kb_index_meta")
            conn.commit()
        finally:
            conn.close()
        return {
            "removed_chunks": removed_count,
            "snapshot": self.load(),
        }


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
        self._index_path = self._index_store_dir / "index.json"
        self._docs_dir.mkdir(parents=True, exist_ok=True)
        self._index_store_dir.mkdir(parents=True, exist_ok=True)
        self._store = self._create_index_store()
        self._embedding_cache_revision = None
        self._embedding_cache_documents = []
        self._embedding_cache_matrix = None
        self._embedding_cache_norms = None
        self._numpy_available = None
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
        settings = settings or self._settings()
        return self._normalize_workspace_root(settings.get("document_workspace_root") or "")

    def _create_index_store(self) -> IndexStore:
        """创建当前使用的索引 backend。

        当前默认固定使用 SQLite `kb.db` backend。

        说明：
        1. `kb.db + Python 层相似度计算` 是当前确认可用的主路径；
        2. `VectorLiteDbIndexStore` 仍保留在代码里，作为未来真实接入时的骨架参考；
        3. 但在真正接入前，不再把“后端切换”暴露成当前用户可配置项，避免出现“能配但其实不会生效”的伪配置。
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
        """返回更偏运维视角的健康/漂移信息。"""
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

        return {
            "ok": True,
            "in_sync": not issues,
            "issues": issues,
            "summary": summary,
        }

    async def rebuild_file(self, file_name: str, workspace_root: str = "", doc_id: str = "") -> dict:
        """只重建单个文件的 chunks 与向量。"""
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
        current_documents.extend(chunks)
        self._store.replace_all(current_documents, {
            "last_rebuilt_at": now,
            "embedding_model": settings["embedding_model"],
        })
        self._invalidate_embedding_cache()

        return {
            "ok": True,
            "doc_id": entry.get("doc_id") or "",
            "file_name": safe_name,
            "chunk_count": len(chunks),
            "last_rebuilt_at": now,
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

    def _settings(self) -> dict:
        """统一解析插件配置，并做最基本的类型收敛。"""
        cfg = self.config or {}
        chunk_size = max(200, _safe_int(cfg.get("chunk_size"), 800))
        chunk_overlap = max(0, min(chunk_size // 2, _safe_int(cfg.get("chunk_overlap"), 120)))
        top_k = max(1, min(10, _safe_int(cfg.get("top_k"), 4)))
        semantic_weight, keyword_weight = _normalize_weight_pair(
            _safe_float(cfg.get("semantic_weight"), 1.0),
            _safe_float(cfg.get("keyword_weight"), 0.0),
        )
        return {
            "embedding_model": str(cfg.get("embedding_model") or "text-embedding-3-small").strip() or "text-embedding-3-small",
            "reranker_model": str(cfg.get("reranker_model") or "").strip(),
            "chat_model": str(cfg.get("chat_model") or "gpt-4o-mini").strip() or "gpt-4o-mini",
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "top_k": top_k,
            "semantic_weight": semantic_weight,
            "keyword_weight": keyword_weight,
            "score_threshold": min(1.0, max(0.0, _safe_float(cfg.get("score_threshold"), 0.7))),
            "document_workspace_root": self._normalize_workspace_root(cfg.get("document_workspace_root") or ""),
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
        """优先使用 `markdown-chunker` 切分 Markdown 文件。

        设计说明：
        1. 默认优先走第三方 `markdown-chunker`，利用它对标题、表格、代码块和列表的结构感知切片；
        2. 若运行环境尚未安装依赖，或第三方切片过程中抛错，则自动回退到仓库原有的“标题优先 + 段落累计 + 字符级兜底”逻辑；
        3. 这样既能提升 Markdown 结构保持能力，也不会把插件可用性绑定到外部依赖是否安装成功。
        """
        text = path.read_text("utf-8")
        entry = self._normalize_doc_entry(entry or self._build_doc_entry(path.name, self._resolve_file_workspace_root(path.name, settings), path.name))
        workspace_root = self._normalize_workspace_root(entry.get("workspace_root") or "")
        chunk_texts = self._chunk_markdown_text_with_markdown_chunker(text, path, settings)
        chunks = self._build_chunks_from_texts(path, chunk_texts, workspace_root, entry)
        if chunks:
            return chunks

        sections = self._split_into_sections(text, path.stem)
        chunks = []
        for section in sections:
            chunks.extend(self._build_chunks_from_section(path, section, settings, workspace_root, entry))
        return chunks

    def _chunk_markdown_text_with_markdown_chunker(self, text: str, path: Path, settings: dict) -> list[str]:
        """使用 `markdown-chunker` 生成结构感知 chunk 文本列表。

        参数映射策略：
        1. `soft_max_len` 直接沿用现有 `chunk_size`，保持“目标 chunk 大小”的用户心智不变；
        2. `hard_max_len` 允许在 `chunk_size + chunk_overlap` 范围内稍微放宽，减少为了严格卡长度而切断结构块；
        3. `min_chunk_len` 使用 `chunk_size - chunk_overlap`，让相邻内容在更接近原先 overlap 语义的前提下尽量被合并。

        注意：
        `markdown-chunker` 本身并不直接暴露字符级 overlap 选项，因此这里只做“尺寸语义映射”；
        真正需要字符滑窗兜底时，仍由本地 fallback 逻辑负责。
        """
        if MarkdownChunkingStrategy is None:
            return []

        chunk_size = max(200, _safe_int(settings.get("chunk_size"), 800))
        overlap = max(0, _safe_int(settings.get("chunk_overlap"), 120))
        min_chunk_len = max(200, chunk_size - overlap)
        hard_max_len = max(chunk_size, chunk_size + overlap)

        try:
            strategy = MarkdownChunkingStrategy(
                min_chunk_len=min_chunk_len,
                soft_max_len=chunk_size,
                hard_max_len=hard_max_len,
                detect_headers_footers=False,
                remove_duplicates=False,
                add_metadata=False,
                document_title=path.stem,
                source_document=path.name,
            )
            chunks = strategy.chunk_markdown(text)
        except Exception as exc:
            self.logger.warning("[markdown_kb] markdown-chunker 切片失败，已回退到内置切片器: %s", exc)
            return []

        normalized_chunks = []
        for item in chunks or []:
            compact = str(item or "").strip()
            if compact:
                normalized_chunks.append(compact)
        return normalized_chunks

    def _build_chunks_from_texts(self, path: Path, chunk_texts: list[str], workspace_root: str, entry: dict) -> list[dict]:
        """把纯文本 chunk 列表包装成索引所需的标准 metadata 结构。"""
        chunks = []
        for idx, raw_chunk_text in enumerate(chunk_texts):
            chunk_text = str(raw_chunk_text or "").strip()
            if not chunk_text:
                continue

            title, heading_level = self._extract_chunk_heading(chunk_text, path.stem)
            content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
            stable_source = f"{path.resolve()}::{idx}::{content_hash}"
            chunk_id = hashlib.sha1(stable_source.encode("utf-8")).hexdigest()
            chunks.append({
                "id": chunk_id,
                "doc_id": str(entry.get("doc_id") or hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()),
                "file_name": str(entry.get("file_name") or path.name),
                "file_path": str(path.resolve()),
                "workspace_root": workspace_root,
                "title": title,
                "heading_level": heading_level,
                "chunk_index": idx,
                "chunk_text": chunk_text,
                "content_hash": content_hash,
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
            })
        return chunks

    def _extract_chunk_heading(self, chunk_text: str, fallback_title: str) -> tuple[str, int]:
        """从 chunk 文本中提取首个 Markdown 标题，供检索结果展示来源标题。"""
        heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
        for line in str(chunk_text or "").splitlines():
            matched = heading_re.match(line.strip())
            if matched:
                return matched.group(2).strip(), len(matched.group(1))
        return fallback_title, 0

    def _split_into_sections(self, text: str, fallback_title: str) -> list[dict]:
        """把 Markdown 原文按标题拆成 section。"""
        lines = text.splitlines()
        sections = []
        current_title = fallback_title
        current_level = 0
        current_lines: list[str] = []
        heading_re = re.compile(r"^(#{1,3})\s+(.+?)\s*$")

        for line in lines:
            matched = heading_re.match(line.strip())
            if matched:
                if current_lines:
                    sections.append({
                        "title": current_title,
                        "heading_level": current_level,
                        "content": "\n".join(current_lines).strip(),
                    })
                current_level = len(matched.group(1))
                current_title = matched.group(2).strip()
                current_lines = []
                continue
            current_lines.append(line)

        if current_lines or not sections:
            sections.append({
                "title": current_title,
                "heading_level": current_level,
                "content": "\n".join(current_lines).strip(),
            })
        return sections

    def _build_chunks_from_section(self, path: Path, section: dict, settings: dict, workspace_root: str, entry: dict) -> list[dict]:
        """把单个 section 再拆成多个 chunk。"""
        title = str(section.get("title") or path.stem).strip() or path.stem
        heading_level = _safe_int(section.get("heading_level"), 0)
        content = str(section.get("content") or "").strip()
        chunk_size = settings["chunk_size"]
        overlap = settings["chunk_overlap"]

        if not content:
            body_parts = [title]
        else:
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
            if not paragraphs:
                paragraphs = [content]
            body_parts = self._pack_paragraphs(paragraphs, chunk_size, overlap)

        chunks = []
        for idx, body in enumerate(body_parts):
            chunk_text = title if not body else f"{title}\n\n{body}".strip()
            content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
            stable_source = f"{path.resolve()}::{idx}::{content_hash}"
            chunk_id = hashlib.sha1(stable_source.encode("utf-8")).hexdigest()
            chunks.append({
                "id": chunk_id,
                "doc_id": str(entry.get("doc_id") or hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()),
                "file_name": str(entry.get("file_name") or path.name),
                "file_path": str(path.resolve()),
                "workspace_root": workspace_root,
                "title": title,
                "heading_level": heading_level,
                "chunk_index": idx,
                "chunk_text": chunk_text,
                "content_hash": content_hash,
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
            })
        return chunks

    def _pack_paragraphs(self, paragraphs: list[str], chunk_size: int, overlap: int) -> list[str]:
        """优先按段落累计；超出时再退化为字符级切片。"""
        chunks: list[str] = []
        current = ""

        for paragraph in paragraphs:
            if len(paragraph) > chunk_size:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(self._split_long_text(paragraph, chunk_size, overlap))
                continue

            candidate = paragraph if not current else f"{current}\n\n{paragraph}"
            if len(candidate) <= chunk_size:
                current = candidate
                continue

            if current:
                chunks.append(current)
            current = paragraph

        if current:
            chunks.append(current)
        return chunks

    def _split_long_text(self, text: str, chunk_size: int, overlap: int) -> list[str]:
        """当单段本身过长时，退化为字符级切片。"""
        compact = text.strip()
        if not compact:
            return []
        if len(compact) <= chunk_size:
            return [compact]

        step = max(1, chunk_size - overlap)
        parts = []
        start = 0
        while start < len(compact):
            end = min(len(compact), start + chunk_size)
            parts.append(compact[start:end].strip())
            if end >= len(compact):
                break
            start += step
        return [item for item in parts if item]

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

        当前采用“两阶段、尽量保守”的策略：
        1. 第一阶段默认做语义分 + 关键词分的混合召回；
        2. 但如果配置了 reranker_model，则第一阶段退回纯语义召回，把最终排序权完全交给 rerank；
        3. 最终统一按 score_threshold 过滤，避免把明显不相关的低分片段继续暴露给 query / ask / 自动注入链路。
        """
        documents = self._load_documents_for_retrieval()
        if not documents:
            raise HTTPException(status_code=400, detail="当前知识库为空，请先上传文档并执行重建")

        settings = self._settings()
        documents = self._filter_documents_by_workspace(documents, project_context)
        if not documents:
            raise HTTPException(status_code=400, detail="当前工作目录下没有匹配的知识文档，请先为该工作目录配置并重建索引")
        documents = self._filter_documents_by_selected_doc(documents, selected_doc)
        if not documents:
            raise HTTPException(status_code=400, detail="当前所选文档不在本次检索范围内，请检查 workspace 或改为不指定文档")

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

        query_vector = await self._get_query_embedding(question, embedding_model)
        semantic_weight = settings["semantic_weight"]
        keyword_weight = settings["keyword_weight"]
        if reranker_model:
            semantic_weight = 1.0
            keyword_weight = 0.0

        scored = self._score_documents(
            question,
            query_vector,
            documents,
            semantic_weight,
            keyword_weight,
        )

        scored.sort(key=lambda item: item["score"], reverse=True)
        candidates = scored[:max(top_k, min(12, len(scored)))]
        if reranker_model and candidates:
            candidates = await self._rerank_hits(question, candidates, reranker_model)
        score_threshold = settings["score_threshold"]
        filtered = [item for item in candidates if _safe_float(item.get("score"), 0.0) >= score_threshold]
        if not self._has_request_workspace(project_context):
            filtered = self._prefer_project_hits(filtered, project_context)
        final_hits = filtered[:top_k]
        self._cache_set(self._query_result_cache, query_cache_key, list(final_hits), max_items=64)
        return final_hits

    def _invalidate_embedding_cache(self) -> None:
        """在索引写操作后清空内存向量缓存。"""
        self._embedding_cache_revision = None
        self._embedding_cache_documents = []
        self._embedding_cache_matrix = None
        self._embedding_cache_norms = None
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
        self._rebuild_numpy_cache(documents)
        return documents

    def _rebuild_numpy_cache(self, documents: list[dict]) -> None:
        """根据当前文档集合重建 NumPy 检索缓存。

        如果当前环境还没有安装 `numpy`，则保持回退路径，不让主流程中断。
        """
        np = self._import_numpy_optional()
        if np is None or not documents:
            self._embedding_cache_matrix = None
            self._embedding_cache_norms = None
            return

        embeddings = [item.get("embedding") or [] for item in documents]
        if not embeddings:
            self._embedding_cache_matrix = None
            self._embedding_cache_norms = None
            return

        dims = {len(vector) for vector in embeddings if isinstance(vector, list)}
        if len(dims) != 1 or 0 in dims:
            # 说明索引里混入了不同 embedding 维度的数据，常见于：
            # 1. 历史上换过 embedding 模型；
            # 2. 没有做一次完整 rebuild，旧 chunk 仍保留旧向量。
            # 这时不能再走矩阵化路径，否则 NumPy 会因为 ragged array 直接抛异常。
            # 这里选择温和回退到逐条余弦计算，让 query 至少可用；
            # 用户后续执行一次全量重建后，会自动恢复 NumPy 快路径。
            self.logger.warning(
                "[markdown_kb] 检测到索引中 embedding 维度不一致，已回退到逐条计算；建议执行一次全量重建。dims=%s",
                sorted(dims),
            )
            self._embedding_cache_matrix = None
            self._embedding_cache_norms = None
            return

        matrix = np.asarray(embeddings, dtype=np.float32)
        if matrix.ndim != 2 or matrix.size == 0:
            self._embedding_cache_matrix = None
            self._embedding_cache_norms = None
            return

        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        self._embedding_cache_matrix = matrix
        self._embedding_cache_norms = norms

    def _score_documents(
        self,
        question: str,
        query_vector: list[float],
        documents: list[dict],
        semantic_weight: float,
        keyword_weight: float,
    ) -> list[dict]:
        """根据 query 为当前文档集合计算混合分。

        打分策略：
        1. `vector_score`：向量余弦相似度，负责语义召回；
        2. `keyword_score`：问题词在标题 / chunk 文本中的覆盖度与短语命中分，负责精确词补强；
        3. `hybrid_score`：两者按配置权重归一化后的线性组合；
        4. `score`：第一阶段默认等于 `hybrid_score`；若后续启用 rerank，则会在 `_rerank_hits()` 中被重写为 rerank 分。

        这样 query / ask 的结果里会同时保留各阶段分数，便于后续观察为什么某个 chunk 被召回。
        """
        np = self._import_numpy_optional()
        keyword_scores = self._score_keywords(question, documents)
        if np is not None and self._embedding_cache_matrix is not None and self._embedding_cache_norms is not None:
            query = np.asarray(query_vector, dtype=np.float32)
            if query.ndim == 1 and query.size == self._embedding_cache_matrix.shape[1]:
                query_norm = np.linalg.norm(query)
                if query_norm == 0:
                    query_norm = 1.0
                scores = self._embedding_cache_matrix.dot(query) / (self._embedding_cache_norms * query_norm)
                return [
                    self._build_scored_hit(
                        item,
                        float(scores[idx]),
                        keyword_scores[idx],
                        semantic_weight,
                        keyword_weight,
                    )
                    for idx, item in enumerate(documents)
                ]

        scored = []
        for idx, item in enumerate(documents):
            embedding = item.get("embedding") or []
            scored.append(
                self._build_scored_hit(
                    item,
                    self._cosine_similarity(query_vector, embedding),
                    keyword_scores[idx],
                    semantic_weight,
                    keyword_weight,
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
    ) -> dict:
        """把单条文档命中组装为带多路分数的统一结构。"""
        hybrid_score = (vector_score * semantic_weight) + (keyword_score * keyword_weight)
        return {
            "score": hybrid_score,
            "hybrid_score": hybrid_score,
            "vector_score": vector_score,
            "keyword_score": keyword_score,
            "id": item.get("id"),
            "file_name": item.get("file_name"),
            "title": item.get("title"),
            "chunk_index": item.get("chunk_index"),
            "chunk_text": item.get("chunk_text"),
            "content_hash": item.get("content_hash"),
        }

    def _score_keywords(self, question: str, documents: list[dict]) -> list[float]:
        """为候选文档计算轻量关键词分。

        目标不是替代全文检索，而是在不引入额外依赖的前提下补一层“字面命中”信号：
        - 标题命中应比正文命中更值钱；
        - 不再依赖“整句原样匹配”拿高分，而是统一回到词块 / 短窗口覆盖率；
        - 分值最终压到 0~1，方便和向量分做线性混合。
        """
        normalized_question = self._normalize_keyword_text(question)
        tokens = self._tokenize_keywords(normalized_question)
        if not normalized_question or not tokens:
            return [0.0 for _ in documents]

        scores = []
        unique_tokens = list(dict.fromkeys(tokens))
        token_weights = self._build_keyword_token_weights(unique_tokens)
        total_weight = max(1e-6, sum(token_weights.values()))
        for item in documents:
            title_text = self._normalize_keyword_text(item.get("title") or "")
            chunk_text = self._normalize_keyword_text(item.get("chunk_text") or "")
            title_compact = title_text.replace(" ", "")
            chunk_compact = chunk_text.replace(" ", "")

            title_hit_weight = 0.0
            body_hit_weight = 0.0
            for token in unique_tokens:
                if not token:
                    continue
                weight = token_weights.get(token, 1.0)
                if token in title_text or token in title_compact:
                    title_hit_weight += weight
                if token in chunk_text or token in chunk_compact:
                    body_hit_weight += weight

            coverage_score = (title_hit_weight * 1.4 + body_hit_weight * 1.0) / (total_weight * 2.4)
            scores.append(min(1.0, coverage_score))
        return scores

    def _normalize_keyword_text(self, text: Any) -> str:
        """把文本规整成适合关键词匹配的形式。"""
        normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
        return normalized

    def _tokenize_keywords(self, text: str) -> list[str]:
        """从问题中提取关键词 token。

        这里同时兼顾中英文：
        - 英文 / 数字 / 连字符词使用正则拆词；
        - 中文不再只保留整段，而是把连续中文片段进一步切成 2~4 字滑窗，避免
          “参考考试大纲生成复习计划” 这类长句只有整句完全命中时才有分。
        """
        if not text:
            return []
        latin_tokens = re.findall(r"[a-z0-9][a-z0-9_\-\.]{1,}", text)
        cjk_tokens = []
        for segment in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            cjk_tokens.append(segment)
            cjk_tokens.extend(self._expand_cjk_segment_tokens(segment))
        return latin_tokens + cjk_tokens

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

    def _build_keyword_token_weights(self, tokens: list[str]) -> dict[str, float]:
        """为关键词 token 分配轻量权重。

        权重规则保持简单：
        - 更长的中文窗口给予更高权重，避免 2 字片段把所有分值稀释得过于平均；
        - 英文 token 统一按长度做温和加权；
        - 结果只用于相对覆盖率计算，不追求 BM25 级复杂度。
        """
        weights = {}
        for token in tokens:
            if not token:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                weights[token] = float(min(4, len(token)))
            else:
                weights[token] = 1.0 + min(1.5, len(token) / 10.0)
        return weights

    def _import_numpy_optional(self):
        """按需导入 NumPy。

        说明：
        1. 当前代码已经把 numpy 记入项目依赖；
        2. 但为了兼容尚未重新安装依赖的本地环境，这里仍然做一次温和回退；
        3. 一旦成功导入，会缓存结果，避免每次 query 重复 import。
        """
        if self._numpy_available is False:
            return None
        if self._numpy_available is True:
            import numpy as np
            return np
        try:
            import numpy as np
            self._numpy_available = True
            return np
        except Exception:
            self._numpy_available = False
            return None

    def _vector_compute_backend_label(self) -> str:
        """返回当前向量计算后端标签，便于状态页展示。"""
        return "numpy" if self._import_numpy_optional() is not None else "python"

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
