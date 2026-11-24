"""Agent Skill 发现与按需加载。

Skill 是「一个目录 + 一份 SKILL.md」的领域知识包，可带 references/、scripts/、
assets/ 等附属文件。全量注入正文不可行：实测 17 个 skill 正文合计约 55k token，
是全部内置工具声明（约 5.5k）的十倍。因此这里只做两件事：

- ``list_skills()``：扫描目录，取 name/description 做**索引**，常驻 system prompt；
- ``load_skill()``：按名字取**正文**，由模型主动调用。

路径安全：skills 目录通常在工作区之外（如 ``~/.agents/skills``），文件工具
（akm_read_file）的 ``_safe_resolve_workspace_path`` 会直接拒绝，所以这里独立
解析路径，并把所有访问限制在 skills 根目录内——软链接解析后仍须落在根内，
否则 skill 目录里一个指向 ``~/.ssh`` 的链接就能读走私钥。
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

from akm.config import load_config

logger = logging.getLogger(__name__)

#: 默认 skills 根目录。与 PI 等客户端共用的全局目录，改配置即可指向别处。
DEFAULT_SKILLS_DIR = "~/.agents/skills"

#: SKILL.md 文件名（大小写固定，与 PI skill 规范一致）。
SKILL_FILE = "SKILL.md"

#: 索引里单条描述的最大字符数。个别 skill 的 description 长达上千字符
#: （modern-web-guidance 的多行块），全量拼进 system prompt 会把索引本身撑成正文。
_MAX_INDEX_DESCRIPTION = 200

#: load_skill 单次返回正文的最大字节数。diagram-design 正文 31KB，
#: 一次塞进上下文就是 10k token；超出时提示模型用 section 精确取。
_MAX_BODY_BYTES = 24000

#: 附属文件清单最多列多少条。diagram-design 有 297 个文件（180+ 是 assets 示例），
#: 全列出来等于让模型拿一份文件树，这里按目录折叠。
_MAX_LISTED_FILES = 40

#: 允许出现在附属文件清单里的子目录（顶层）。assets 只报数量不逐条列。
_LISTED_SUBDIRS = ("references", "reference", "scripts", "guides", "data")

#: 正文按 markdown 二级/三级标题切分时使用的匹配。
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)


def skills_root() -> Path | None:
    """返回 skills 根目录（已展开 ~、已 resolve）；目录不存在时返回 None。"""
    configured = load_config().get("agent_skills_dir")
    # 区分「未配置」（None，用默认目录）与「显式留空」（""，关闭该能力）：
    # 用 `or` 会把空串也当成未配置，配置页里关不掉这个功能。
    raw = str(DEFAULT_SKILLS_DIR if configured is None else configured).strip()
    if not raw:
        return None
    root = Path(raw).expanduser()
    try:
        resolved = root.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 SKILL.md 的 ``---`` frontmatter，返回 (元数据, 正文)。

    刻意不引入 yaml 依赖（打包后的依赖解析成本高于收益），只支持 skill 实际用到的
    三种写法：``key: value``、``key: "带引号的值"``、``key: |`` 多行块。引号值优先按
    JSON 解析，这样 description 里的 ``\\"`` 转义能正确还原。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, text

    meta: dict[str, Any] = {}
    block_key: str | None = None
    block_lines: list[str] = []

    def flush() -> None:
        nonlocal block_key, block_lines
        if block_key is not None:
            meta[block_key] = "\n".join(block_lines).strip()
            block_key = None
            block_lines = []

    for line in lines[1:end]:
        if block_key is not None:
            # 多行块：后续缩进行都属于该键，直到出现新的顶层 key
            if line.startswith((" ", "\t")):
                block_lines.append(line.strip())
                continue
            if not line.strip():
                block_lines.append("")
                continue
            flush()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, raw_value = stripped.partition(":")
        key = key.strip()
        value = raw_value.strip()
        if value in ("|", ">", "|-", ">-"):
            block_key = key
            block_lines = []
            continue
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            try:
                meta[key] = json.loads(value)
                continue
            except json.JSONDecodeError:
                meta[key] = value[1:-1]
                continue
        if value.startswith("'") and value.endswith("'") and len(value) >= 2:
            meta[key] = value[1:-1]
            continue
        meta[key] = value
    flush()
    return meta, "\n".join(lines[end + 1:]).lstrip("\n")


def _skill_entry(skill_dir: Path, root: Path) -> dict[str, Any] | None:
    """读取单个 skill 目录的元数据；无 SKILL.md 或不可读时返回 None。

    这里**不**校验解析后的路径是否仍在 root 内：skill 目录本身可以是指向别处的
    符号链接（ego-browser 就整个链到 ego 的安装目录），按解析结果判越界会把合法
    skill 误杀。真正的边界在 ``_resolve_skill``——名字只能来自 ``list_skills()``
    的目录列举结果，模型无法构造路径，所以 root 内的软链接是用户主动放置的可信项。
    """
    try:
        target = (skill_dir / SKILL_FILE).resolve()
    except OSError:
        logger.warning("[AgentSkill] 无法解析 skill 路径: %s", skill_dir)
        return None
    if not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("[AgentSkill] 读取失败 %s: %s", target, exc)
        return None
    meta, _ = _parse_frontmatter(text)
    name = str(meta.get("name") or skill_dir.name).strip() or skill_dir.name
    description = " ".join(str(meta.get("description") or "").split())
    return {
        "name": name,
        "dir": skill_dir.name,
        "path": str(target),
        "description": description,
        # 该字段语义是「别让模型自动触发」，索引里保留但标注为需用户显式要求
        "manual_only": str(meta.get("disable-model-invocation") or "").strip().lower() in ("true", "1", "yes"),
    }


# 目录扫描结果缓存：(skills 根目录, 该目录的 mtime_ns) → 索引条目。
# load_skill 每次都要按名字定位，而索引会拼进 system prompt；不缓存的话每轮
# 请求都要 stat 一遍所有 skill 目录。用根目录 mtime 作失效键：新增/删除 skill
# 会改父目录 mtime，但**改单个 SKILL.md 内容不会**，所以正文一律实时读取。
_INDEX_CACHE: dict[tuple[str, int], list[dict[str, Any]]] = {}


def list_skills() -> list[dict[str, Any]]:
    """扫描 skills 根目录，返回全部 skill 的索引元数据（按目录名排序）。

    顺序固定按目录名——索引会进 system prompt，顺序不稳定会让每轮请求的
    system 前缀都变，上游 prompt 缓存全部失效。
    """
    root = skills_root()
    if root is None:
        return []
    try:
        root_mtime = root.stat().st_mtime_ns
    except OSError:
        return []
    cache_key = (str(root), root_mtime)
    cached = _INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached

    entries: list[dict[str, Any]] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        logger.warning("[AgentSkill] 无法列出 skills 目录 %s: %s", root, exc)
        return []
    for child in children:
        if child.name.startswith("."):
            continue
        if not child.is_dir():
            continue
        entry = _skill_entry(child, root)
        if entry is not None:
            entries.append(entry)
    # 只保留当前根目录的缓存，避免换目录后旧键堆积
    for key in [k for k in _INDEX_CACHE if k[0] != str(root)]:
        _INDEX_CACHE.pop(key, None)
    _INDEX_CACHE[cache_key] = entries
    return entries


def _clip_description(text: str, limit: int | None = None) -> str:
    """把描述压成一行并限长，避免索引被个别超长描述撑爆。"""
    if limit is None:
        limit = _MAX_INDEX_DESCRIPTION
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


def build_index_text() -> str:
    """构造注入 system prompt 的 skill 索引段；没有可用 skill 时返回空串。"""
    entries = list_skills()
    if not entries:
        return ""
    lines = [
        "## 可用 Skill（按需加载）",
        "",
        "下面这些 skill 是特定领域的详细操作指引。它们没有出现在工具清单里，但**不代表",
        "没有这个能力**——需要时先调用 `akm_load_skill` 取正文，再按正文执行。",
        "",
        "判断是否要用：用户的任务与某条描述吻合时就用，不要凭常识跳过。",
        "",
    ]
    for entry in entries:
        suffix = "（仅在用户显式要求时使用）" if entry["manual_only"] else ""
        lines.append(f"- **{entry['name']}** — {_clip_description(entry['description'])}{suffix}")
    lines.append("")
    lines.append(
        "调用方式：`akm_load_skill(name=\"<上面加粗的名字>\")`。正文很长时可用 "
        "`section` 参数只取某一节（如 `section=\"用法\"`）。长对话里忘了这份清单时，"
        "可以不带 name 调一次 `akm_load_skill()` 重新取回。"
    )
    return "\n".join(lines)


def _resolve_skill(name: str) -> tuple[Path, dict[str, Any]] | None:
    """按名字或目录名定位一个 skill，返回 (SKILL.md 路径, 元数据)。

    只接受 ``list_skills()`` 里出现过的名字，等于用白名单挡住一切路径构造：
    模型传 ``../../.ssh/id_rsa`` 之类的值根本匹配不到任何条目。
    """
    key = str(name or "").strip()
    if not key:
        return None
    for entry in list_skills():
        if key in (entry["name"], entry["dir"]):
            return Path(entry["path"]), entry
    return None


def _extract_section(body: str, section: str) -> tuple[str, bool]:
    """按标题名截取正文中的一节，返回 (内容, 是否命中)。

    匹配标题文本包含给定关键字的第一节，截到下一个同级或更高级标题为止。
    skill 正文动辄几千行，让模型精确取一节比整篇塞进上下文便宜得多。
    """
    wanted = str(section or "").strip().lower()
    if not wanted:
        return body, True
    matches = list(_HEADING_RE.finditer(body))
    for index, match in enumerate(matches):
        title = match.group(2).strip()
        if wanted not in title.lower():
            continue
        level = len(match.group(1))
        start = match.start()
        end = len(body)
        for later in matches[index + 1:]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        return body[start:end].rstrip(), True
    return body, False


def _find_reference_doc(skill_path: Path, wanted: str) -> tuple[str | None, str]:
    """在 skill 的附属文档里按文件名查找，返回 (相对路径, 内容)；找不到返回 (None, "")。

    查找范围限于 ``_LISTED_SUBDIRS`` 列出的目录，按「文件名去掉扩展名后包含关键字」
    匹配（忽略大小写）。越界仍由 skill 根目录兜底：附属文件路径基于已解析的
    SKILL.md 所在目录构造，不经过用户输入，因此天然不会指向 root 之外。
    """
    needle = str(wanted or "").strip().lower().replace(" ", "-")
    if not needle:
        return None, ""
    skill_dir = skill_path.parent
    for sub in _LISTED_SUBDIRS:
        folder = skill_dir / sub
        if not folder.is_dir():
            continue
        try:
            candidates = sorted(
                p for p in folder.rglob("*.md")
                if p.is_file() and not p.name.startswith(".")
            )
        except OSError:
            continue
        for candidate in candidates:
            stem = candidate.stem.lower()
            if needle in stem or needle in candidate.name.lower():
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                rel = candidate.relative_to(skill_dir)
                return str(rel), text
    return None, ""


def _list_attachments(skill_path: Path) -> dict[str, Any]:
    """汇总 skill 目录下的附属文件，按目录折叠并限量。

    diagram-design 有 297 个文件（180+ 是 assets 示例 HTML），逐条列出等于把文件树
    塞进上下文；这里只列 references/scripts 等指引性目录的顶层文件，assets 只报数量。
    """
    skill_dir = skill_path.parent
    listed: list[str] = []
    counts: dict[str, int] = {}
    truncated = False
    try:
        children = sorted(skill_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return {"files": [], "dir_summary": {}, "truncated": False}
    for child in children:
        if child.name == SKILL_FILE or child.name.startswith("."):
            continue
        if child.is_file():
            if len(listed) < _MAX_LISTED_FILES:
                listed.append(child.name)
            else:
                truncated = True
            continue
        if not child.is_dir():
            continue
        try:
            files = sorted(p for p in child.iterdir() if p.is_file() and not p.name.startswith("."))
        except OSError:
            continue
        counts[child.name] = len(files)
        if child.name not in _LISTED_SUBDIRS:
            continue
        for item in files:
            if len(listed) < _MAX_LISTED_FILES:
                listed.append(f"{child.name}/{item.name}")
            else:
                truncated = True
    return {"files": listed, "dir_summary": counts, "truncated": truncated}


def skill_catalog() -> dict[str, Any]:
    """返回 skill 清单（名字 + 一句话描述），供不带 name 的调用自查。

    与 system prompt 里的索引同源。给模型一个「自己重查」的入口：长对话里索引可能
    被较早的上下文挤出注意力，此时它能主动查一次，而不是凭记忆瞎猜名字。
    """
    entries = list_skills()
    catalog = []
    for entry in entries:
        item: dict[str, Any] = {
            "name": entry["name"],
            "description": _clip_description(entry["description"]),
        }
        if entry["manual_only"]:
            item["manual_only"] = True
        catalog.append(item)
    return {
        "ok": True,
        "skills": catalog,
        "hint": (
            "传 name 取该 skill 的正文；正文很长时用 section 只取一节。"
            "manual_only 的 skill 只在用户显式要求时使用"
            if catalog
            else "没有可用的 skill：检查 agent_skills_dir 指向的目录"
        ),
    }


def load_skill(name: str, section: str = "") -> dict[str, Any]:
    """读取一个 skill 的正文与附属文件清单；name 留空时返回全部 skill 的清单。

    Args:
        name: skill 名字（索引里加粗的那个）或目录名；留空返回清单。
        section: 可选，只取标题包含该关键字的某一节。

    Returns:
        含 ``ok``、``body`` 等字段的 dict；失败时含 ``error``。
    """
    # 空 name = 查清单，与 akm_load_mcp 不传 server 时的行为对齐：
    # 三个入口统一成「不传参数 / 传空 = 列清单，传参数 = 取详情」
    if not str(name or "").strip():
        return skill_catalog()
    located = _resolve_skill(name)
    if located is None:
        available = ", ".join(entry["name"] for entry in list_skills()) or "（无）"
        return {
            "ok": False,
            "error": f"未找到 skill: {name}",
            "available": available,
        }
    path, entry = located
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": f"读取 skill 失败: {exc}"}
    _, body = _parse_frontmatter(text)

    section_miss = False
    section_file: str | None = None
    wanted = str(section or "").strip()
    if wanted:
        body, hit = _extract_section(body, wanted)
        if not hit:
            # 正文里没有对应标题时，按文件名到附属文档里找：impeccable 这类 skill
            # 的知识全在 reference/audit.md，正文只有一句「读 reference/xxx.md」，
            # 而文件工具读不到工作区外的 skill 目录，只能由这里代读。
            located_file, file_text = _find_reference_doc(path, wanted)
            if located_file is not None:
                body = file_text
                section_file = located_file
            else:
                section_miss = True

    encoded = body.encode("utf-8")
    clipped = len(encoded) > _MAX_BODY_BYTES
    if clipped:
        body = encoded[:_MAX_BODY_BYTES].decode("utf-8", "ignore")

    attachments = _list_attachments(path)
    result: dict[str, Any] = {
        "ok": True,
        "name": entry["name"],
        "description": entry["description"],
        "body": body,
        "files": attachments["files"],
        # skill 目录对 shell 只读（seatbelt 拒绝写 ~/.agents）：脚本只能读数据、
        # 把结果输出到 stdout 或工作区，写回自身目录会失败。
        "scripts_note": (
            "skill 目录是只读的：脚本可以用 akm_run_shell 执行（传绝对路径，"
            "路径基于本 skill 目录），但不要尝试往 skill 目录写文件。"
        ),
    }
    if attachments["dir_summary"]:
        result["dir_summary"] = attachments["dir_summary"]
    if section_file:
        result["section_file"] = section_file
    if clipped:
        result["truncated"] = True
        result["notice"] = (
            f"正文超过 {_MAX_BODY_BYTES} 字节已截断；请用 section 参数精确取需要的章节"
        )
    if section_miss:
        result["section_miss"] = True
        result["notice"] = f"未找到标题或文件含「{section}」的内容，已返回全文开头"
    if attachments["truncated"]:
        result["files_truncated"] = True
    return result
