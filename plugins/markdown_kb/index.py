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

from fastapi import APIRouter, Body, File, HTTPException, UploadFile

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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kb_documents (
                    doc_id TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
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
                CREATE INDEX IF NOT EXISTS idx_kb_documents_content_hash ON kb_documents(content_hash);
                CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc_id ON kb_chunks(doc_id);
                CREATE INDEX IF NOT EXISTS idx_kb_chunks_file_name ON kb_chunks(file_name);
                CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc_chunk_index ON kb_chunks(doc_id, chunk_index);
                CREATE INDEX IF NOT EXISTS idx_kb_vectors_chunk_id ON kb_vectors(chunk_id);
                """
            )
            conn.commit()
        finally:
            conn.close()

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
                        doc_id, file_name, file_path, content_hash, file_size_bytes,
                        title, chunk_count, updated_at, indexed_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        first.get("file_name") or "",
                        first.get("file_path") or "",
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
                        chunk_id, doc_id, file_name, file_path, title, heading_level,
                        chunk_index, chunk_text, content_hash, created_at, updated_at, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.get("id"),
                        item.get("doc_id"),
                        item.get("file_name") or "",
                        item.get("file_path") or "",
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
async def upload_markdown(file: UploadFile = File(...)):
    """接收并保存单个 Markdown 文件。

    约束保持极简：
    1. 只接受 `.md` 扩展名；
    2. 直接保存原文件，不在上传阶段做切片或建索引；
    3. 若同名文件重复上传，则覆盖旧文件，降低骨架阶段的交互复杂度。
    """
    plugin = _get_plugin()
    return await plugin.save_markdown_file(file)


@router.get("/files")
async def list_markdown_files():
    """列出当前知识库中的 Markdown 文件。"""
    plugin = _get_plugin()
    return plugin.list_files()


@router.delete("/files/{name}")
async def delete_markdown_file(name: str):
    """删除指定 Markdown 文件，并同步移除其索引条目。"""
    plugin = _get_plugin()
    return plugin.delete_file(name)


@router.post("/rebuild")
async def rebuild_index():
    """全量重建知识库索引。"""
    plugin = _get_plugin()
    return await plugin.rebuild_index()


@router.post("/rebuild-file")
async def rebuild_single_file(payload: dict = Body(...)):
    """只重建单个 Markdown 文件。"""
    plugin = _get_plugin()
    return await plugin.rebuild_file(str((payload or {}).get("file_name", "") or ""))


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
        self._data_root = self._resolve_data_root()
        self._docs_dir = self._data_root / "docs"
        self._index_store_dir = self._data_root / "index_store"
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
        self.logger.info("[markdown_kb] 数据目录已就绪: %s", self._data_root)

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
        indexed_documents = self._store.list_documents()
        chunk_counts_by_file: dict[str, int] = {}
        for item in indexed_documents:
            file_name = str(item.get("file_name") or "")
            if not file_name:
                continue
            chunk_counts_by_file[file_name] = chunk_counts_by_file.get(file_name, 0) + 1

        files = []
        for path in sorted(self._docs_dir.glob("*.md")):
            stat = path.stat()
            payload = path.read_bytes()
            files.append({
                "file_name": path.name,
                "size_bytes": stat.st_size,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat(),
                "indexed": chunk_counts_by_file.get(path.name, 0) > 0,
                "chunk_count": chunk_counts_by_file.get(path.name, 0),
            })

        return {
            "ok": True,
            "files": files,
            "count": len(files),
        }

    def delete_file(self, name: str) -> dict:
        """删除指定 Markdown 文件，并同步清理索引中对应的 chunks。"""
        safe_name = Path((name or "").strip()).name
        if not safe_name or not safe_name.lower().endswith(".md"):
            raise HTTPException(status_code=400, detail="仅支持删除 .md 文件")

        target = self._docs_dir / safe_name
        if not target.exists():
            raise HTTPException(status_code=404, detail="文件不存在")

        target.unlink()

        delete_result = self._store.delete_by_file(safe_name)
        self._invalidate_embedding_cache()
        removed_count = delete_result.get("removed_chunks", 0)

        return {
            "ok": True,
            "file_name": safe_name,
            "removed_chunks": removed_count,
        }

    def clear_index(self, delete_docs: bool = False) -> dict:
        """清空索引，并可选删除原始 Markdown 文档。"""
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
        settings = self._settings()
        result = {}
        for path in sorted(self._docs_dir.glob("*.md")):
            payload = path.read_bytes()
            doc_id = self._get_doc_id_for_path(path)
            preview_chunks = self._chunk_markdown_file(path, settings)
            chunk_hashes = sorted(str(item.get("content_hash") or "") for item in preview_chunks)
            aggregate_hash = hashlib.sha256("|".join(chunk_hashes).encode("utf-8")).hexdigest() if chunk_hashes else hashlib.sha256(b"").hexdigest()
            result[doc_id] = {
                "doc_id": doc_id,
                "file_name": path.name,
                "file_path": str(path.resolve()),
                "content_hash": hashlib.sha256(payload).hexdigest(),
                "aggregate_hash": aggregate_hash,
                "chunk_count": len(preview_chunks),
                "size_bytes": len(payload),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat(),
            }
        return result

    def preview_sync(self) -> dict:
        """只做增量判断，不真正写索引。"""
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

    async def rebuild_file(self, file_name: str) -> dict:
        """只重建单个文件的 chunks 与向量。"""
        safe_name = Path((file_name or "").strip()).name
        if not safe_name or not safe_name.lower().endswith(".md"):
            raise HTTPException(status_code=400, detail="仅支持重建 .md 文件")

        target = self._docs_dir / safe_name
        if not target.exists():
            raise HTTPException(status_code=404, detail="文件不存在")

        settings = self._settings()
        chunks = self._chunk_markdown_file(target, settings)
        embeddings = await self._embed_texts([item["chunk_text"] for item in chunks], settings["embedding_model"])
        if len(embeddings) != len(chunks):
            raise HTTPException(status_code=502, detail="embedding 返回数量与 chunks 数量不一致")

        now = _utc_now_iso()
        for item, embedding in zip(chunks, embeddings):
            item["embedding"] = embedding
            item["indexed_at"] = now

        current_documents = [item for item in self._store.list_documents() if item.get("file_name") != safe_name]
        current_documents.extend(chunks)
        self._store.replace_all(current_documents, {
            "last_rebuilt_at": now,
            "embedding_model": settings["embedding_model"],
        })
        self._invalidate_embedding_cache()

        return {
            "ok": True,
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
            self.delete_file(str(item.get("file_name") or ""))
            applied["removed"].append(item.get("file_name"))

        for item in preview["added"]:
            result = await self.rebuild_file(str(item.get("file_name") or ""))
            applied["added"].append(result.get("file_name"))

        for item in preview["changed"]:
            result = await self.rebuild_file(str(item.get("file_name") or ""))
            applied["changed"].append(result.get("file_name"))

        latest = self.preview_sync()
        latest["applied"] = True
        latest["applied_changes"] = applied
        return latest

    async def save_markdown_file(self, file: UploadFile) -> dict:
        """保存上传的 Markdown 文件并返回最小元信息。"""
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

        target = self._docs_dir / safe_name
        target.write_bytes(payload)

        sha256 = hashlib.sha256(payload).hexdigest()
        return {
            "ok": True,
            "file_name": safe_name,
            "saved_path": str(target),
            "size_bytes": len(payload),
            "sha256": sha256,
            "uploaded_at": _utc_now_iso(),
        }

    async def rebuild_index(self) -> dict:
        """全量重建索引。

        当前实现选择“全量”而不是增量，原因有两个：
        1. 第一版目标是先验证切片和检索质量；
        2. 全量语义最简单，排障成本最低。
        """
        docs = sorted(self._docs_dir.glob("*.md"))
        settings = self._settings()

        if not docs:
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
        for path in docs:
            chunks.extend(self._chunk_markdown_file(path, settings))

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
            "doc_count": len(docs),
            "chunk_count": len(documents),
            "last_rebuilt_at": now,
            "embedding_model": settings["embedding_model"],
        }

    async def query(self, payload: dict) -> dict:
        """执行纯检索，不调用 chat。"""
        question = str((payload or {}).get("question", "") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question 不能为空")

        top_k = self._resolve_top_k((payload or {}).get("top_k"))
        settings = self._settings()
        embedding_model = str((payload or {}).get("embedding_model") or settings["embedding_model"]).strip() or settings["embedding_model"]
        reranker_model = str((payload or {}).get("reranker_model") or settings["reranker_model"]).strip()
        hits = await self._retrieve(question, top_k, embedding_model, reranker_model)
        return {
            "ok": True,
            "question": question,
            "top_k": top_k,
            "embedding_model": embedding_model,
            "reranker_model": reranker_model,
            "hits": hits,
        }

    async def ask(self, payload: dict) -> dict:
        """执行检索增强问答。"""
        question = str((payload or {}).get("question", "") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question 不能为空")

        top_k = self._resolve_top_k((payload or {}).get("top_k"))
        settings = self._settings()
        embedding_model = str((payload or {}).get("embedding_model") or settings["embedding_model"]).strip() or settings["embedding_model"]
        reranker_model = str((payload or {}).get("reranker_model") or settings["reranker_model"]).strip()
        chat_model = str((payload or {}).get("chat_model") or settings["chat_model"]).strip() or settings["chat_model"]
        hits = await self._retrieve(question, top_k, embedding_model, reranker_model)
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
        }

    def _settings(self) -> dict:
        """统一解析插件配置，并做最基本的类型收敛。"""
        cfg = self.config or {}
        chunk_size = max(200, _safe_int(cfg.get("chunk_size"), 800))
        chunk_overlap = max(0, min(chunk_size // 2, _safe_int(cfg.get("chunk_overlap"), 120)))
        top_k = max(1, _safe_int(cfg.get("top_k"), 4))
        return {
            "embedding_model": str(cfg.get("embedding_model") or "text-embedding-3-small").strip() or "text-embedding-3-small",
            "reranker_model": str(cfg.get("reranker_model") or "").strip(),
            "chat_model": str(cfg.get("chat_model") or "gpt-4o-mini").strip() or "gpt-4o-mini",
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "top_k": top_k,
        }

    def _resolve_top_k(self, requested: Any) -> int:
        """解析请求级 top_k，未传时回落到插件默认值。"""
        default_top_k = self._settings()["top_k"]
        if requested in (None, ""):
            return default_top_k
        return max(1, _safe_int(requested, default_top_k))

    def _load_index_data(self) -> dict:
        """读取索引文件。

        当前默认 backend 是 `JsonIndexStore`，但调用方不需要知道底层细节。
        """
        return self._store.load()

    def _save_index_data(self, data: dict) -> None:
        """落盘索引数据。"""
        self._store.save(data)

    def _chunk_markdown_file(self, path: Path, settings: dict) -> list[dict]:
        """按“标题优先”策略切分 Markdown 文件。

        处理顺序：
        1. 先识别 `# / ## / ###` 标题，把文档拆成若干 section；
        2. section 内优先按空行段落累计；
        3. 超过 chunk_size 时再切分；
        4. 用字符级 overlap 保留相邻上下文。
        """
        text = path.read_text("utf-8")
        sections = self._split_into_sections(text, path.stem)
        chunks = []
        for section in sections:
            chunks.extend(self._build_chunks_from_section(path, section, settings))
        return chunks

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

    def _build_chunks_from_section(self, path: Path, section: dict, settings: dict) -> list[dict]:
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
                "doc_id": hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest(),
                "file_name": path.name,
                "file_path": str(path.resolve()),
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

    async def _retrieve(self, question: str, top_k: int, embedding_model: str, reranker_model: str) -> list[dict]:
        """执行向量检索。"""
        documents = self._load_documents_for_retrieval()
        if not documents:
            raise HTTPException(status_code=400, detail="当前知识库为空，请先上传文档并执行重建")

        query_cache_key = self._query_cache_key(question, top_k, embedding_model, reranker_model)
        cached_hits = self._cache_get(self._query_result_cache, query_cache_key)
        if cached_hits is not None:
            return list(cached_hits)

        query_vector = await self._get_query_embedding(question, embedding_model)
        scored = self._score_documents(query_vector, documents)

        scored.sort(key=lambda item: item["score"], reverse=True)
        candidates = scored[:max(top_k, min(12, len(scored)))]
        if reranker_model and candidates:
            candidates = await self._rerank_hits(question, candidates, reranker_model)
        final_hits = candidates[:top_k]
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

    def _query_cache_key(self, question: str, top_k: int, embedding_model: str, reranker_model: str) -> tuple:
        """构造 query 结果缓存 key。"""
        return (
            question,
            int(top_k),
            embedding_model,
            reranker_model,
            self._current_embedding_cache_revision(),
        )

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

        matrix = np.asarray([item.get("embedding") or [] for item in documents], dtype=np.float32)
        if matrix.ndim != 2 or matrix.size == 0:
            self._embedding_cache_matrix = None
            self._embedding_cache_norms = None
            return

        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        self._embedding_cache_matrix = matrix
        self._embedding_cache_norms = norms

    def _score_documents(self, query_vector: list[float], documents: list[dict]) -> list[dict]:
        """根据 query 向量为当前文档集合打分。

        优先使用 NumPy 矩阵化计算；若当前环境尚未安装 `numpy`，则回退到
        原来的 Python 循环余弦计算，保证功能不受阻。
        """
        np = self._import_numpy_optional()
        if np is not None and self._embedding_cache_matrix is not None and self._embedding_cache_norms is not None:
            query = np.asarray(query_vector, dtype=np.float32)
            if query.ndim == 1 and query.size == self._embedding_cache_matrix.shape[1]:
                query_norm = np.linalg.norm(query)
                if query_norm == 0:
                    query_norm = 1.0
                scores = self._embedding_cache_matrix.dot(query) / (self._embedding_cache_norms * query_norm)
                return [
                    {
                        "score": float(scores[idx]),
                        "id": item.get("id"),
                        "file_name": item.get("file_name"),
                        "title": item.get("title"),
                        "chunk_index": item.get("chunk_index"),
                        "chunk_text": item.get("chunk_text"),
                        "content_hash": item.get("content_hash"),
                    }
                    for idx, item in enumerate(documents)
                ]

        scored = []
        for item in documents:
            embedding = item.get("embedding") or []
            scored.append({
                "score": self._cosine_similarity(query_vector, embedding),
                "id": item.get("id"),
                "file_name": item.get("file_name"),
                "title": item.get("title"),
                "chunk_index": item.get("chunk_index"),
                "chunk_text": item.get("chunk_text"),
                "content_hash": item.get("content_hash"),
            })
        return scored

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
            base["vector_score"] = base.get("score", 0.0)
            base["rerank_score"] = _safe_float(row.get("relevance_score"), 0.0)
            base["score"] = base["rerank_score"]
            reranked.append(base)
            used_indices.add(idx)

        for idx, item in enumerate(hits):
            if idx in used_indices:
                continue
            base = dict(item)
            base["vector_score"] = base.get("score", 0.0)
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
