"""按工作区自动维护的项目上下文与最近修改记忆（context.md / memory.md）。

设计边界（与 markdown_kb 主流程解耦）：
1. 每个 workspace（工作区根目录）在插件数据根下拥有独立项目目录：
   ``<data_root>/projects/<project_id>/``，内含 context.md、memory.md、meta.json。
2. context.md / memory.md **不进入** docs 清单、不切片、不建向量索引、不参与
   检索候选与记忆分，因此完全不影响 markdown_kb 既有的 RAG 检索与自动注入。
3. 触发源只接收“知识库内容变化事件”（文档新增/覆盖/删除、learn/scan 沉淀），
   不做项目目录 fs 监听，也不依赖 git。
4. 维护采用两级：
   - 文件级事件：零成本、同步追加（meta.json 事件表 + 重渲染 memory.md）；
   - 语义摘要（digest）：按 冷却/批量 合并，调用本地 chat 一次把自上次 digest
     以来的事件折叠成“近期要点”；chat 不可用或模型为空时降级为纯文本摘要。
5. context.md 的职责是“长期稳定的项目说明书”，首次创建时先用本地材料
   （工作区 AGENTS.md / README.md 与知识库内该工作区文档）快速生成骨架，
   随后在后台调用本地 chat 做一次“首次生成”（按固定章节重新组织材料，
   只依据提供的真实材料、限制行数、禁止临期词）；项目演化后按冷却周期做
   “最小化增量更新”（只改过时条目）。人工编辑的 context.md 不被自动覆盖；
   memory.md（热记忆）仍是确定性渲染，两者职责严格分开。
6. 支持“初始化/回填”：把知识库中已存在绑定（manifest / learn 记录 / bindings）
   的工作区批量补齐这两个文件，兼容老数据升级场景。

注：context 生成只在“触发时刻”一次性读取工作区目录里的固定名称文件
（AGENTS/README/常用配置文件 + 两级目录结构）作为模型材料，不监听目录、
不依赖 git，不影响事件触发的 A 类边界。

所有文件写入都使用“tmp + rename”原子替换，并以实例级 RLock 串行化，
避免并发钩子/路由撕裂文件。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

PROJECTS_DIR_NAME = "projects"
CONTEXT_FILE = "context.md"
MEMORY_FILE = "memory.md"
META_FILE = "meta.json"

# 事件表在 meta 中最多保留条数（机器侧）；渲染进 memory.md 的最近事件只取后 N 条。
MAX_META_EVENTS = 300
RENDER_EVENTS_LIMIT = 30
# 初始化简介时从项目根读取的说明文件（按顺序取第一个存在且非空的）。
INTRO_SOURCE_FILES = ("AGENTS.md", "README.md", "CLAUDE.md", "README.MD")
INTRO_SOURCE_MAX_CHARS = 1400
# 生成 context / digest 时，知识库内该工作区文档最多收录条数与每条摘录截断长度。
KB_MATERIAL_MAX_DOCS = 12
KB_MATERIAL_EXCERPT_CHARS = 300
# 语义摘要（digest）合并阈值（代码级默认，不在管理台暴露）。
DIGEST_MIN_PENDING = 3
DIGEST_COOLDOWN_SECONDS = 300  # 距上次 digest 不足该秒数时继续攒批
# 防止伪造工作区路径导致项目目录无限膨胀。
MAX_PROJECTS = 500

# context.md 语义生成（LLM“首次生成”/“最小化增量更新”）。
CONTEXT_TARGET_MAX_LINES = 150  # 提示词要求的目标行数上限
CONTEXT_HARD_MAX_LINES = 200  # 生成后防御性截断（LLM 超限时直接截掉尾部）
CONTEXT_REFRESH_COOLDOWN_SECONDS = 86400  # 增量更新冷却（默认 1 天，避免每次 digest 都改）
CONTEXT_SURVEY_MAX_CHARS = 6000  # 仓库调研材料总预算（目录结构 + 文件原文）
CONTEXT_SURVEY_MAX_DEPTH = 2  # 目录树最多展开的层数
CONTEXT_SURVEY_MAX_ENTRIES = 220  # 目录树最多列出的条目数
CONTEXT_SURVEY_SKIP_DIRS = {  # 目录树里跳过的典型噪音目录
    ".git", "node_modules", "venv", ".venv", "dist", "build", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".idea", ".vscode", "coverage", "target",
}
CONTEXT_README_MAX_CHARS = 2000  # 单个说明/配置文件读取上限
CONTEXT_SURVEY_READ_FILES = (  # 读取优先级：说明文件优先，配置次之
    "AGENTS.md", "README.md", "CLAUDE.md", "README.MD",
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "requirements.txt", "setup.cfg", "Makefile",
)
CONTEXT_SOURCE_SKELETON = "skeleton"  # 本地材料骨架（等待 LLM 首次生成）
CONTEXT_SOURCE_LLM = "llm"  # 已由 LLM 生成/维护
AUTO_HEADER_MARKER = "由 markdown_kb 自动维护"  # 自动生成 context.md 的头部标记

CONTEXT_INIT_SYSTEM_PROMPT = (
    "你是资深架构师，负责为 AI 编码会话维护一份项目级 <context.md>："
    "一份只写“长期稳定事实”的项目说明书。\n"
    "输入只包含该工作区的真实材料：说明文件/配置文件原文、目录结构、"
    "以及该工作区在本地知识库中的关联文档。\n"
    "【硬性要求】\n"
    "1. 只能依据提供的材料归纳，严禁编造材料里不存在的目录、命令、依赖或约定；\n"
    "2. 只写长期稳定的事实：技术栈、目录职责、运行命令、编码约定、架构决策；\n"
    "3. 严禁搬运大段代码；涉及接口/命令只写“用法一句话 + 关键参数”；\n"
    "4. 以要点和短句为主，Markdown 输出，总行数控制在 150 行以内，越精简越好；\n"
    "5. 不要写“最近/本次/临时/待办/下一步”这类会过期的词——那是 memory.md 的活。\n"
    "【必须包含的章节】（材料缺失时如实省略该章节并注明“（材料缺失）”，不要编造）\n"
    "1. 项目一句话定位（一行说清是干嘛的）\n"
    "2. 技术栈清单（语言/框架/关键库 + 各自一句话用途）\n"
    "3. 目录结构速览（按提供的目录结构归纳，注明每块职责）\n"
    "4. 常用命令（dev / build / test / lint / 单测入口：从提供的 package.json 等\n"
    "   真实脚本里照抄，不存在的命令不写）\n"
    "5. 编码约定（命名、目录组织、错误处理、测试风格——从提供的材料里归纳，别瞎编）\n"
    "6. 关键架构决策（为什么这么分层/选型，一句话一条）\n"
    "7. 环境与依赖注意点（配置文件在哪、敏感项怎么处理）\n"
    "只输出 context.md 全文（不要 ``` 代码围栏、不要任何解释），可直接写入文件。"
)

CONTEXT_UPDATE_SYSTEM_PROMPT = (
    "你是资深架构师，负责对项目现有 <context.md> 做“最小化增量更新”。\n"
    "输入包含：现有 context.md、仓库最新材料（说明文件/配置/目录结构）、\n"
    "以及该工作区在知识库中最近的文档沉淀列表。\n"
    "【修订规则】\n"
    "1. 只修改已过时的条目（版本升级、目录新增、命令变化、约定调整、架构演进）；\n"
    "2. 技术栈/架构/约定确实变了才动；纯新增功能、近期进展**不要**写进 context.md\n"
    "   （那些属于 memory.md 的热记忆）；\n"
    "3. 保持原有章节结构、行数量级与口吻，不要越改越长；\n"
    "4. 人工编辑过的部分同样保留，除非它已与真实材料冲突；\n"
    "5. 只依据提供的最新材料判断，禁止脑补不存在的目录/命令。\n"
    "若没有任何条目过时，请**原样逐字返回**现有 context.md 全文（不要围栏、不要解释）。\n"
    "只输出 context.md 全文。"
)

DIGEST_SYSTEM_PROMPT = (
    "你是项目记忆整理助手。请根据提供的「自上次整理以来的变更事件」与「知识库近期"
    "沉淀材料」，把该项目最近在知识库侧发生的变化折叠成一份简短、准确、可直接注入"
    "上下文的 Markdown 摘要（digest_markdown）。要求：\n"
    "1. 只总结知识库能感知到的变化：新增/更新/删除的知识文档、学习沉淀的主要主题；\n"
    "2. 不要编造事件之外的信息，不要写流水账式时间戳；\n"
    "3. 使用项目符号列表，最多 8 条，全文控制在 500 字以内，不要包含最外层 # 标题；\n"
    "4. 若材料不足以归纳出任何有价值要点，返回 digest_markdown 为空字符串。\n"
    "你必须只返回一个 JSON 对象，不要输出代码块围栏。字段固定为："
    '{"digest_markdown": string}'
)
DIGEST_TIMEOUT_SECONDS = 120

ACTION_LABELS = {
    "added": "新增文档",
    "updated": "更新文档",
    "deleted": "删除文档",
    "learn": "沉淀知识",
    "sync": "目录同步",
}


def _utc_now_iso() -> str:
    """统一生成 UTC ISO 时间字符串（去掉微秒）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_int(value: Any, default: int = 0) -> int:
    """把任意输入稳妥转换为整数。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _safe_text(value: Any, limit: int = 0) -> str:
    """把任意输入收敛为字符串并按需截断。"""
    text = str(value or "").strip()
    if limit > 0 and len(text) > limit:
        return text[:limit]
    return text


def normalize_workspace(value: Any) -> str:
    """工作区根目录归一化；空串表示“公共/未绑定”。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw == "/":
        return "/"
    path = str(Path(raw).expanduser().resolve()).rstrip("/\\")
    return path or "/"


def _render_events_markdown(events: list[dict], limit: int = RENDER_EVENTS_LIMIT) -> str:
    """把事件表渲染成 markdown 列表（最新在前）。"""
    if not events:
        return "（暂无事件）"
    lines = []
    for item in reversed(events[-limit:]):
        ts = _safe_text(item.get("ts"))
        action = _safe_text(item.get("action"))
        file_name = _safe_text(item.get("file_name"))
        label = ACTION_LABELS.get(action, action or "变更")
        if file_name:
            lines.append(f"- {ts} · {label} · {file_name}")
        else:
            lines.append(f"- {ts} · {label}")
    return "\n".join(lines)


class ProjectMemory:
    """按工作区维护 context.md / memory.md 及其机器侧元数据。

    线程安全：文件读改写受实例级 RLock 保护；digest 的 chat 调用在锁外执行，
    结果落盘仍在锁内串行。
    """

    def __init__(self, data_root: Any, logger: Any = None):
        self._projects_root = Path(str(data_root)) / PROJECTS_DIR_NAME
        self._projects_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._logger = logger

    def _log(self, message: str, level: str = "info") -> None:
        logger = getattr(self, "_logger", None)
        if logger is None:
            return
        try:
            getattr(logger, level)("[markdown_kb] %s", message)
        except Exception:
            pass

    # ───────────────────────── 路径与元数据 ─────────────────────────

    def project_id(self, workspace_root: str) -> str:
        """用归一化工作区根目录生成稳定项目 ID。"""
        normalized = normalize_workspace(workspace_root)
        if not normalized:
            return ""
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    def project_dir(self, workspace_root: str) -> Path:
        """返回项目数据目录（不存在时调用方应先行 ensure）。"""
        return self._projects_root / self.project_id(workspace_root)

    def has_project(self, workspace_root: str) -> bool:
        """是否已存在该项目（以 meta 文件为准）。"""
        normalized = normalize_workspace(workspace_root)
        if not normalized:
            return False
        with self._lock:
            return self._meta_path(normalized).exists()

    def project_count(self) -> int:
        with self._lock:
            if not self._projects_root.exists():
                return 0
            return sum(1 for _ in self._projects_root.glob(f"*/{META_FILE}"))

    def list_projects(self) -> list[dict]:
        """列出全部已创建的项目记忆摘要。"""
        with self._lock:
            projects = []
            if not self._projects_root.exists():
                return projects
            for meta_path in sorted(self._projects_root.glob(f"*/{META_FILE}")):
                try:
                    meta = json.loads(meta_path.read_text("utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(meta, dict):
                    continue
                workspace = _safe_text(meta.get("workspace_root"))
                if not workspace:
                    continue
                projects.append(self._summary(meta, workspace))
            return projects

    def _summary(self, meta: dict, workspace_root: str) -> dict:
        context_path = self._context_path(workspace_root)
        memory_path = self._memory_path(workspace_root)
        return {
            "project_id": self.project_id(workspace_root),
            "workspace_root": workspace_root,
            "project_name": Path(workspace_root).name if workspace_root != "/" else "/",
            "created_at": _safe_text(meta.get("created_at")),
            "updated_at": _safe_text(meta.get("updated_at")),
            "revision": _safe_int(meta.get("revision")),
            "last_digest_at": _safe_text(meta.get("last_digest_at")),
            "pending_events": _safe_int(meta.get("pending_events")),
            "context_exists": context_path.exists(),
            "memory_exists": memory_path.exists(),
            "context_source": _safe_text(meta.get("context_source")) or "",
            "context_updated_at": _safe_text(meta.get("context_updated_at")),
            "last_context_refresh_at": _safe_text(meta.get("last_context_refresh_at")),
            "event_count": len(meta.get("events") or []),
            "has_digest": bool(_safe_text(meta.get("digest_text"))),
        }

    def _context_path(self, workspace_root: str) -> Path:
        return self.project_dir(workspace_root) / CONTEXT_FILE

    def _memory_path(self, workspace_root: str) -> Path:
        return self.project_dir(workspace_root) / MEMORY_FILE

    def _meta_path(self, workspace_root: str) -> Path:
        return self.project_dir(workspace_root) / META_FILE

    def _load_meta_locked(self, workspace_root: str) -> dict:
        meta_path = self._meta_path(workspace_root)
        if not meta_path.exists():
            return {}
        try:
            data = json.loads(meta_path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_meta_locked(self, workspace_root: str, meta: dict) -> None:
        meta_path = self._meta_path(workspace_root)
        tmp = meta_path.with_name(meta_path.name + ".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(meta_path)

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, "utf-8")
        tmp.replace(path)

    # ───────────────────────── 懒创建与初始化/回填 ─────────────────────────

    def ensure_skeleton(self, workspace_root: str, kb_material: list[dict] | None = None) -> dict:
        """首次遇到某工作区时创建项目目录与 context.md / memory.md。

        幂等：项目已存在时只补全缺失文件，不覆盖已有 context.md（尊重人工编辑）。
        kb_material 由调用方提供：该工作区在知识库中的文档摘要，形如
        [{"file_name": str, "updated_at": str, "excerpt": str}, ...]。
        """
        normalized = normalize_workspace(workspace_root)
        if not normalized:
            return {"created": False, "reason": "empty_workspace"}
        if self.project_count() >= MAX_PROJECTS and not self.has_project(normalized):
            return {"created": False, "reason": "project_limit"}
        with self._lock:
            if self.has_project(normalized):
                # 只补可能缺失的 memory 渲染文件，不动 context.md 内容。
                meta = self._load_meta_locked(normalized)
                memory_path = self._memory_path(normalized)
                if not memory_path.exists():
                    self._atomic_write_text(memory_path, self._render_memory_text(meta))
                return {"created": False, "reason": "already_exists"}

            project_dir = self.project_dir(normalized)
            project_dir.mkdir(parents=True, exist_ok=True)
            now = _utc_now_iso()
            context_text = self._build_context_text(normalized, kb_material or [], now)
            self._atomic_write_text(self._context_path(normalized), context_text)
            meta = {
                "workspace_root": normalized,
                "created_at": now,
                "updated_at": now,
                "revision": 0,
                "last_digest_at": "",
                "pending_events": 0,
                "digest_text": "",
                "context_updated_at": now,
                "context_source": CONTEXT_SOURCE_SKELETON,
                "last_context_refresh_at": "",
                "events": [],
            }
            self._save_meta_locked(normalized, meta)
            self._atomic_write_text(self._memory_path(normalized), self._render_memory_text(meta))
            return {
                "created": True,
                "project_id": self.project_id(normalized),
                "workspace_root": normalized,
            }

    def init_projects(self, workspace_roots: list[str], kb_material_fn: Any = None) -> dict:
        """初始化/回填：为一批已存在绑定的工作区补齐 context.md / memory.md。

        kb_material_fn 可选：callable(workspace_root) -> list[dict]，为每个工作区
        提供知识库文档摘要；未提供时按空材料创建（context 仅含项目说明/占位）。
        """
        created: list[dict] = []
        skipped: list[str] = []
        seen: set[str] = set()
        for raw in workspace_roots or []:
            normalized = normalize_workspace(raw)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            try:
                material = []
                if kb_material_fn is not None:
                    try:
                        material = kb_material_fn(normalized) or []
                    except Exception:
                        material = []
                result = self.ensure_skeleton(normalized, material)
                if result.get("created"):
                    created.append(result)
                else:
                    skipped.append(normalized)
            except Exception as exc:  # noqa: BLE001
                self._log(f"初始化项目记忆失败 {normalized}: {exc}", "warning")
                skipped.append(normalized)
        return {
            "ok": True,
            "total": len(seen),
            "created": len(created),
            "skipped": len(skipped),
            "projects": created,
            "skipped_roots": skipped,
        }

    def _build_context_text(self, workspace_root: str, kb_material: list[dict], now: str) -> str:
        """组装 context.md 初始文本（不调用模型，纯文件级材料）。"""
        project_name = Path(workspace_root).name if workspace_root != "/" else "/"
        lines = [
            f"# {project_name} 项目上下文",
            "",
            f"> 由 markdown_kb 自动维护 · 创建于 {now}",
            f"> 工作区路径：`{workspace_root}`",
            "",
        ]

        intro = self._read_project_intro(workspace_root)
        if intro:
            lines.append("## 项目简介")
            lines.append("")
            lines.append(intro)
            lines.append("")

        material_docs = list(kb_material or [])[:KB_MATERIAL_MAX_DOCS]
        if material_docs:
            lines.append("## 知识库关联文档（自动维护）")
            lines.append("")
            for doc in material_docs:
                file_name = _safe_text(doc.get("file_name"))
                updated_at = _safe_text(doc.get("updated_at"))
                if file_name:
                    suffix = f"（更新于 {updated_at}）" if updated_at else ""
                    lines.append(f"- {file_name}{suffix}")
            lines.append("")

        if not intro and not material_docs:
            lines.append("## 项目简介")
            lines.append("")
            lines.append("（暂无简介材料：项目目录未提供说明文件，知识库中也尚无该工作区文档。")
            lines.append("后续有知识沉淀后会自动补充最近记忆；简介可由人工直接编辑本文件。）")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _read_project_intro(self, workspace_root: str) -> str:
        """读取工作区根目录下的说明文件（仅固定文件名，返回截断文本）。"""
        workspace = Path(workspace_root)
        if workspace_root == "/" or not workspace.is_dir():
            return ""
        for name in INTRO_SOURCE_FILES:
            candidate = workspace / name
            try:
                if not candidate.is_file():
                    continue
                text = candidate.read_text("utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text:
                continue
            return text[:INTRO_SOURCE_MAX_CHARS]
        return ""

    # ───────────────────────── 事件记录（零成本级） ─────────────────────────

    def record_event(self, workspace_root: str, action: str, file_name: str) -> dict | None:
        """追加一条文件级事件并刷新 memory.md。

        action 取值：added / updated / deleted / learn。
        工作区为空（公共文档/未绑定）时直接忽略返回 None。
        """
        normalized = normalize_workspace(workspace_root)
        action = _safe_text(action)
        file_name = _safe_text(file_name)
        if not normalized or not action:
            return None
        with self._lock:
            self.ensure_skeleton(normalized)
            meta = self._load_meta_locked(normalized)
            events = meta.get("events")
            if not isinstance(events, list):
                events = []
            events.append({"ts": _utc_now_iso(), "action": action, "file_name": file_name})
            if len(events) > MAX_META_EVENTS:
                # 最早的事件被裁掉不影响“近期要点”（digest 文本独立保存在 meta）。
                events = events[-MAX_META_EVENTS:]
            meta["events"] = events
            meta["pending_events"] = _safe_int(meta.get("pending_events")) + 1
            meta["revision"] = _safe_int(meta.get("revision")) + 1
            meta["updated_at"] = _utc_now_iso()
            self._save_meta_locked(normalized, meta)
            self._atomic_write_text(self._memory_path(normalized), self._render_memory_text(meta))
            return {
                "project_id": self.project_id(normalized),
                "pending_events": meta["pending_events"],
                "revision": meta["revision"],
            }

    def pending_count(self, workspace_root: str) -> int:
        normalized = normalize_workspace(workspace_root)
        if not normalized:
            return 0
        with self._lock:
            meta = self._load_meta_locked(normalized)
            return _safe_int(meta.get("pending_events"))

    def due_for_digest(self, workspace_root: str, cooldown_seconds: int = DIGEST_COOLDOWN_SECONDS,
                       min_pending: int = DIGEST_MIN_PENDING) -> bool:
        """判断某项目是否应当触发一次语义摘要（pending 达标或冷却期已过）。"""
        normalized = normalize_workspace(workspace_root)
        if not normalized:
            return False
        with self._lock:
            meta = self._load_meta_locked(normalized)
            pending = _safe_int(meta.get("pending_events"))
            if pending <= 0:
                return False
            if pending >= min_pending:
                return True
            last_digest_at = _safe_text(meta.get("last_digest_at"))
            if not last_digest_at:
                return True
            try:
                last_dt = datetime.fromisoformat(last_digest_at.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                return elapsed >= cooldown_seconds
            except (ValueError, TypeError):
                return True

    # ───────────────────────── 读取（供注入侧使用） ─────────────────────────

    def snapshot(self, workspace_root: str) -> dict | None:
        """返回供注入使用的项目快照；项目不存在时返回 None。"""
        normalized = normalize_workspace(workspace_root)
        if not normalized:
            return None
        with self._lock:
            if not self.has_project(normalized):
                return None
            meta = self._load_meta_locked(normalized)
            context_path = self._context_path(normalized)
            memory_path = self._memory_path(normalized)
            context_md = context_path.read_text("utf-8", errors="replace") if context_path.exists() else ""
            memory_md = memory_path.read_text("utf-8", errors="replace") if memory_path.exists() else ""
            return {
                "project_id": self.project_id(normalized),
                "workspace_root": normalized,
                "project_name": Path(normalized).name if normalized != "/" else "/",
                "context_md": context_md,
                "memory_md": memory_md,
                "revision": _safe_int(meta.get("revision")),
                "updated_at": _safe_text(meta.get("updated_at")),
                "last_digest_at": _safe_text(meta.get("last_digest_at")),
                "context_source": _safe_text(meta.get("context_source")) or "",
                "context_updated_at": _safe_text(meta.get("context_updated_at")),
            }

    def read_project(self, workspace_root: str) -> dict | None:
        """读取单个项目的完整详情（含事件表与 digest 文本），供 API 与管理台。"""
        snapshot = self.snapshot(workspace_root)
        if snapshot is None:
            return None
        with self._lock:
            meta = self._load_meta_locked(workspace_root)
            return {
                **snapshot,
                "digest_text": _safe_text(meta.get("digest_text")),
                "events": list(meta.get("events") or []),
                "created_at": _safe_text(meta.get("created_at")),
                "context_updated_at": _safe_text(meta.get("context_updated_at")),
                "context_source": _safe_text(meta.get("context_source")) or "",
                "last_context_refresh_at": _safe_text(meta.get("last_context_refresh_at")),
            }

    # ───────────────────────── 语义摘要（digest） ─────────────────────────

    def _pending_events(self, workspace_root: str) -> list[dict]:
        """自上次 digest 以来累积的待消费事件。"""
        with self._lock:
            meta = self._load_meta_locked(workspace_root)
            events = meta.get("events") or []
            last_digest_at = _safe_text(meta.get("last_digest_at"))
            if not last_digest_at:
                return [item for item in events if isinstance(item, dict)]
            return [item for item in events if isinstance(item, dict) and _safe_text(item.get("ts")) > last_digest_at]

    async def run_digest(self, workspace_root: str, *, base_url: str, model: str,
                         kb_material: list[dict] | None = None) -> dict:
        """消费待处理事件并更新 memory.md 的“近期要点”。

        模型不可用/返回异常时降级为纯文本摘要并照样消费（避免同一批事件反复重试）。
        """
        normalized = normalize_workspace(workspace_root)
        if not normalized:
            return {"ok": False, "reason": "empty_workspace"}
        pending_events = self._pending_events(normalized)
        if not pending_events:
            return {"ok": True, "consumed": 0, "digest_text": "", "degraded": False}

        digest_text = ""
        degraded = False
        try:
            if model:
                digest_text = await self._chat_digest(
                    base_url=base_url, model=model,
                    workspace=normalized, pending_events=pending_events,
                    kb_material=kb_material or [],
                )
            else:
                degraded = True
        except Exception as exc:  # noqa: BLE001
            self._log(f"项目记忆 digest 调用失败，降级文本摘要: {exc}", "warning")
            degraded = True
        if degraded or not digest_text:
            digest_text = self._textual_digest(pending_events)

        with self._lock:
            meta = self._load_meta_locked(normalized)
            meta["digest_text"] = digest_text
            meta["pending_events"] = 0
            meta["last_digest_at"] = _utc_now_iso()
            meta["revision"] = _safe_int(meta.get("revision")) + 1
            meta["updated_at"] = _utc_now_iso()
            self._save_meta_locked(normalized, meta)
            self._atomic_write_text(self._memory_path(normalized), self._render_memory_text(meta))
        return {"ok": True, "consumed": len(pending_events), "digest_text": digest_text, "degraded": degraded}

    async def _chat_digest(self, *, base_url: str, model: str, workspace: str,
                           pending_events: list[dict], kb_material: list[dict]) -> str:
        """调用本地 chat 生成摘要文本；返回空串表示“无要点”。"""
        user_parts = [
            f"工作区：{workspace}",
            "",
            "## 自上次整理以来的变更事件",
            _render_events_markdown(pending_events, limit=len(pending_events)),
            "",
        ]
        if kb_material:
            user_parts.append("## 知识库近期沉淀材料")
            user_parts.append("")
            for doc in kb_material[:KB_MATERIAL_MAX_DOCS]:
                file_name = _safe_text(doc.get("file_name"))
                updated_at = _safe_text(doc.get("updated_at"))
                excerpt = _safe_text(doc.get("excerpt"), KB_MATERIAL_EXCERPT_CHARS)
                header = f"- **{file_name}**" + (f"（{updated_at}）" if updated_at else "")
                user_parts.append(header)
                if excerpt:
                    user_parts.append(f"  {excerpt}")
            user_parts.append("")
        user_prompt = "\n".join(user_parts)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        data = await self._post_chat(base_url, payload)
        try:
            raw_content = str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("digest chat 返回结构不符合预期")
        raw_content = re_sub_fence(raw_content)
        if not raw_content:
            return ""
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError:
            raise RuntimeError("digest chat 未返回合法 JSON")
        if not isinstance(parsed, dict):
            raise RuntimeError("digest chat 返回结构不符合预期")
        return _safe_text(parsed.get("digest_markdown"), 2000)

    async def _post_chat(self, base_url: str, payload: dict) -> dict:
        """调用 AKM chat 接口（单独抽出便于测试替换）。"""
        async with httpx.AsyncClient(timeout=DIGEST_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{base_url}/chat/completions", json=payload)
        if response.status_code != 200:
            raise RuntimeError(f"digest chat 请求失败: HTTP {response.status_code}")
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError("digest chat 返回非法 JSON") from exc

    def _textual_digest(self, pending_events: list[dict]) -> str:
        """无模型/调用失败时的降级摘要：仅按文件级事件归纳。"""
        lines = ["（降级摘要：模型不可用，仅记录文件级变化。）"]
        lines.append(_render_events_markdown(pending_events, limit=8))
        return "\n".join(lines)

    # ───────────────────────── context.md：LLM 首次生成 / 最小化增量更新 ─────────────────────────

    def context_state(self, workspace_root: str) -> dict:
        """读取某项目的 context.md 状态（供注入/调度判断用）。"""
        normalized = normalize_workspace(workspace_root)
        if not normalized:
            return {"exists": False, "source": "", "auto_marker": False, "has_content": False,
                    "last_context_refresh_at": ""}
        with self._lock:
            meta = self._load_meta_locked(normalized)
            context_path = self._context_path(normalized)
            text = context_path.read_text("utf-8", errors="replace") if context_path.exists() else ""
            return {
                "exists": context_path.exists(),
                "source": _safe_text(meta.get("context_source")) or "",
                "auto_marker": AUTO_HEADER_MARKER in text,
                "has_content": bool(str(text).strip()),
                "last_context_refresh_at": _safe_text(meta.get("last_context_refresh_at")),
            }

    def context_due_for_refresh(self, workspace_root: str,
                                cooldown_seconds: int = CONTEXT_REFRESH_COOLDOWN_SECONDS) -> bool:
        """是否已到下一次“增量更新”时间点（仅 LLM 维护过的 context 才轮询）。"""
        normalized = normalize_workspace(workspace_root)
        if not normalized:
            return False
        with self._lock:
            meta = self._load_meta_locked(normalized)
            if _safe_text(meta.get("context_source")) != CONTEXT_SOURCE_LLM:
                return False
            last_at = _safe_text(meta.get("last_context_refresh_at"))
            if not last_at:
                return True
            try:
                last_dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
                return (datetime.now(timezone.utc) - last_dt).total_seconds() >= cooldown_seconds
            except (ValueError, TypeError):
                return True

    async def run_context_init(self, workspace_root: str, *, base_url: str, model: str,
                               kb_material: list[dict] | None = None) -> dict:
        """context.md“首次生成”：按固定章节把真实材料重组织成项目说明书。

        守护规则：只在骨架/缺失状态下生成，绝不覆盖人工编辑过（无自动标记）的内容；
        无模型/调用失败时保留原骨架，不写任何垃圾文本。
        """
        normalized = normalize_workspace(workspace_root)
        if not normalized:
            return {"ok": False, "reason": "empty_workspace"}
        with self._lock:
            meta = self._load_meta_locked(normalized)
            source = _safe_text(meta.get("context_source"))
            context_path = self._context_path(normalized)
            existing = context_path.read_text("utf-8", errors="replace") if context_path.exists() else ""
        if source == CONTEXT_SOURCE_LLM and existing.strip():
            return {"ok": True, "written": False, "reason": "already_llm"}
        if existing.strip() and AUTO_HEADER_MARKER not in existing:
            # 无自动标记 = 人工自建 context.md，绝不覆盖。
            return {"ok": True, "written": False, "reason": "manual_context"}
        if not model:
            return {"ok": True, "written": False, "degraded": True, "reason": "no_model"}

        survey = self._repo_survey(normalized)
        user_prompt = self._context_user_prompt("init", normalized, survey, kb_material or [], existing)
        try:
            generated = await self._chat_context_text(base_url, model, CONTEXT_INIT_SYSTEM_PROMPT, user_prompt)
        except Exception as exc:  # noqa: BLE001
            self._log(f"context 首次生成失败，保留骨架: {exc}", "warning")
            return {"ok": True, "written": False, "degraded": True, "reason": "chat_failed"}
        if not generated.strip():
            return {"ok": True, "written": False, "degraded": True, "reason": "empty_generation"}
        generated = self._cap_context_text(generated)

        with self._lock:
            meta = self._load_meta_locked(normalized)
            now = _utc_now_iso()
            self._atomic_write_text(self._context_path(normalized), generated)
            meta["context_source"] = CONTEXT_SOURCE_LLM
            meta["context_updated_at"] = now
            meta["updated_at"] = now
            meta["last_context_refresh_at"] = now
            self._save_meta_locked(normalized, meta)
        return {"ok": True, "written": True, "degraded": False, "reason": "",
                "chars": len(generated), "lines": len(generated.splitlines())}

    async def run_context_update(self, workspace_root: str, *, base_url: str, model: str,
                                 kb_material: list[dict] | None = None, force: bool = False) -> dict:
        """context.md“最小化增量更新”：以现有文件为基线只改过时条目。

        未到冷却周期（默认 1 天）且非 force 时跳过；模型把全文原样返回时不落盘。
        """
        normalized = normalize_workspace(workspace_root)
        if not normalized:
            return {"ok": False, "reason": "empty_workspace"}
        with self._lock:
            meta = self._load_meta_locked(normalized)
            context_path = self._context_path(normalized)
            existing = context_path.read_text("utf-8", errors="replace") if context_path.exists() else ""
        if not existing.strip():
            return {"ok": True, "written": False, "reason": "no_context"}
        if not force:
            if _safe_text(meta.get("context_source")) != CONTEXT_SOURCE_LLM:
                return {"ok": True, "written": False, "reason": "not_llm"}
            if not self.context_due_for_refresh(normalized):
                return {"ok": True, "written": False, "reason": "cooldown"}
        if not model:
            return {"ok": True, "written": False, "degraded": True, "reason": "no_model"}

        survey = self._repo_survey(normalized)
        user_prompt = self._context_user_prompt("update", normalized, survey, kb_material or [], existing)
        try:
            generated = await self._chat_context_text(base_url, model, CONTEXT_UPDATE_SYSTEM_PROMPT, user_prompt)
        except Exception as exc:  # noqa: BLE001
            self._log(f"context 增量更新失败: {exc}", "warning")
            return {"ok": True, "written": False, "degraded": True, "reason": "chat_failed"}
        if not generated.strip():
            return {"ok": True, "written": False, "degraded": True, "reason": "empty_generation"}
        if generated.strip() == existing.strip():
            return {"ok": True, "written": False, "reason": "unchanged"}
        generated = self._cap_context_text(generated)

        with self._lock:
            meta = self._load_meta_locked(normalized)
            now = _utc_now_iso()
            self._atomic_write_text(self._context_path(normalized), generated)
            meta["context_source"] = CONTEXT_SOURCE_LLM
            meta["context_updated_at"] = now
            meta["updated_at"] = now
            meta["last_context_refresh_at"] = now
            self._save_meta_locked(normalized, meta)
        return {"ok": True, "written": True, "degraded": False, "reason": "",
                "chars": len(generated), "lines": len(generated.splitlines())}

    async def _chat_context_text(self, base_url: str, model: str, system_prompt: str, user_prompt: str) -> str:
        """调用本地 chat 生成纯文本（模型输出可直接落盘的 markdown）。"""
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        data = await self._post_chat(base_url, payload)
        try:
            raw_content = str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("context chat 返回结构不符合预期")
        return re_sub_fence(raw_content).strip()

    @staticmethod
    def _cap_context_text(text: str) -> str:
        """防御性行数截断（正常由提示词约束在 150 行内）。"""
        lines = text.splitlines()
        if len(lines) > CONTEXT_HARD_MAX_LINES:
            lines = lines[:CONTEXT_HARD_MAX_LINES]
            lines.append("")
            lines.append("> （已由 markdown_kb 截断超长内容，请精简后重试生成。）")
        return "\n".join(lines).rstrip() + "\n"

    def _context_user_prompt(self, action: str, workspace_root: str, survey: str,
                             kb_material: list[dict], existing: str) -> str:
        """组装 context 生成/修订的用户材料（全部来自真实输入，防模型脑补）。"""
        parts = [f"工作区路径：{workspace_root}", ""]
        if action == "update":
            parts.append("## 现有 context.md 全文")
            parts.append("")
            parts.append(existing.strip() or "（无现有内容）")
            parts.append("")
        if survey:
            parts.append("## 仓库真实材料（目录结构 + 说明/配置文件原文）")
            parts.append("")
            parts.append(survey)
        else:
            parts.append("## 仓库真实材料")
            parts.append("")
            parts.append("（该工作区目录当前不可读取或为空：只能在知识库材料基础上生成/修订，")
            parts.append("缺失章节请如实标注“（材料缺失）”。）")
        material_docs = list(kb_material or [])[:KB_MATERIAL_MAX_DOCS]
        if material_docs:
            parts.append("")
            parts.append("## 该工作区在知识库中的关联文档")
            parts.append("")
            for doc in material_docs:
                file_name = _safe_text(doc.get("file_name"))
                updated_at = _safe_text(doc.get("updated_at"))
                excerpt = _safe_text(doc.get("excerpt"), KB_MATERIAL_EXCERPT_CHARS)
                header = f"- **{file_name}**" + (f"（{updated_at}）" if updated_at else "")
                parts.append(header)
                if excerpt:
                    parts.append(f"  {excerpt}")
        return "\n".join(parts)

    def _repo_survey(self, workspace_root: str) -> str:
        """一次性读取工作区目录的固定材料（目录树两层 + 固定名称文件原文）。

        只做触发时刻的读取，不监听；用于约束 LLM 只能依据真实文件归纳。
        """
        root = Path(workspace_root)
        if workspace_root == "/" or not root.is_dir():
            return ""
        lines: list[str] = []
        total = 0
        limit = CONTEXT_SURVEY_MAX_CHARS
        used = 0

        def push(text: str) -> None:
            nonlocal used
            remaining = limit - used
            if remaining <= 0:
                return
            lines.append(text[:remaining])
            used += len(text[:remaining])

        # 1) 两级目录树（跳过隐藏与常见噪音目录，条目数有上限）
        entries: list[Path] = []
        try:
            entries = sorted(
                (child for child in root.iterdir()
                 if not child.name.startswith(".") and child.name not in CONTEXT_SURVEY_SKIP_DIRS),
                key=lambda p: (p.is_file(), p.name.lower()),
            )
        except OSError:
            entries = []
        tree_lines: list[str] = ["## 仓库目录结构（节选）", ""]
        listed = 0
        for child in entries:
            if listed >= CONTEXT_SURVEY_MAX_ENTRIES:
                tree_lines.append("- …（条目过多，已省略）")
                break
            if child.is_dir():
                tree_lines.append(f"- {child.name}/")
                listed += 1
                sub: list[Path] = []
                try:
                    sub = sorted(
                        (s for s in child.iterdir()
                         if not s.name.startswith(".") and s.name not in CONTEXT_SURVEY_SKIP_DIRS),
                        key=lambda p: (p.is_file(), p.name.lower()),
                    )
                except OSError:
                    sub = []
                for subchild in sub[:12]:
                    if listed >= CONTEXT_SURVEY_MAX_ENTRIES:
                        break
                    tree_lines.append(f"  - {'📄' if subchild.is_file() else '📁'} {subchild.name}")
                    listed += 1
            else:
                tree_lines.append(f"- {child.name}")
                listed += 1
        tree_lines.append("")
        push("\n".join(tree_lines))

        # 2) 固定名称说明/配置文件原文（截断单个文件长度）
        push("## 说明 / 配置文件原文（节选）")
        push("")
        for name in CONTEXT_SURVEY_READ_FILES:
            candidate = root / name
            try:
                if not candidate.is_file():
                    continue
                text = candidate.read_text("utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text:
                continue
            push(f"### {name}")
            push("")
            push(text[:CONTEXT_README_MAX_CHARS])
            push("")
        return "\n".join(lines).strip()

    # ───────────────────────── 渲染 ─────────────────────────

    def _render_memory_text(self, meta: dict) -> str:
        """根据 meta 状态渲染 memory.md 全文（digest + 最近事件）。"""
        revision = _safe_int(meta.get("revision"))
        updated_at = _safe_text(meta.get("updated_at"))
        digest_text = _safe_text(meta.get("digest_text"))
        events = meta.get("events")
        if not isinstance(events, list):
            events = []
        lines = [
            "# 最近修改记忆",
            "",
            f"> 由 markdown_kb 自动维护 · 版本 v{revision}" + (f" · 更新于 {updated_at}" if updated_at else ""),
            "> 覆盖范围：该工作区在知识库中的文档新增/更新/删除与知识沉淀。",
            "> 仓库内未经知识库感知的改动不在其中。",
            "",
            "## 近期要点",
            "",
        ]
        lines.append(digest_text if digest_text else "（暂无要点：最近还没有足够的沉淀。）")
        lines.append("")
        lines.append("## 最近事件")
        lines.append("")
        lines.append(_render_events_markdown(events))
        lines.append("")
        return "\n".join(lines)


def re_sub_fence(raw_content: str) -> str:
    """去掉模型可能输出的 ```json ``` 围栏。"""
    import re

    text = raw_content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1).strip()
        text = re.sub(r"\s*```$", "", text, count=1).strip()
    return text
