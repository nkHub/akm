"""数据安全插件。

能力范围：
1. 请求侧：递归脱敏敏感字段，对文本执行关键词/正则/代码敏感替换。
   所有替换使用 ``<AKM-SEC:tag@seq:hash/>`` 可逆占位符，建立反向映射表。
2. 响应侧：先反向还原占位符为原始值，再扫描高风险命令/脚本片段做拦截。
   非流式扫描覆盖 Chat / Responses / Anthropic Messages 可见文本。
3. 流式响应：前缀锚点检测 ``<AKM-SEC:``，按需缓冲后在输出前还原原始值；
   流式安全扫描默认有界完整缓冲，超限后增量扫描（mask 退化为 block）。

说明：
- 该插件默认关闭；用户可按需在插件页启用。
- 请求侧 code_secret 的 block 模式等同 mask + 告警，不阻断请求。
- ``messages[].content`` 路径同时覆盖字符串 content 与 content blocks 子路径。
- 代码敏感扫描路径独立于关键词/正则的 ``request_text_paths``，二者可分别配置。
- 响应还原兼容：精确匹配、JSON 转义 ``\\/``、中文标签规整为 ``t``+指纹，
  以及模型轻微改写 tag 后按 6 位内容指纹的宽松匹配。
"""

from akm.plugins import PluginBase
import hashlib
import json
import re


# 默认代码敏感扫描路径：对话正文 + 系统/指令 + Chat 续接工具参数
# - messages[].content：覆盖字符串 content 与 content[].text / content[].input 等子路径
# - input / instructions：Responses API
# - system：Anthropic Messages
# - messages[].tool_calls[].function.arguments：Chat 客户端续接中的工具参数（密钥常经此泄漏）
DEFAULT_CODE_SECRET_PATHS = (
    "messages[].content,input,instructions,system,"
    "messages[].tool_calls[].function.arguments"
)

# 默认关键词/正则文本处理路径（与代码敏感路径对齐主文本字段，不含 tool 参数以免误伤结构化 JSON）
DEFAULT_REQUEST_TEXT_PATHS = "messages[].content,input,instructions,system"


# ═══════════════════════════════════════════════════════════════
# 代码敏感规则库
# ═══════════════════════════════════════════════════════════════

DEFAULT_CODE_SECRET_RULE_GROUPS = {
    "llm_keys": {
        "openai_project_key": {
            "pattern": r"\bsk-proj-[A-Za-z0-9_-]{40,}\b",
            "secret_type": "api_key",
            "subtype": "openai_project",
            "severity": "high",
            "confidence": 0.95,
        },
        "openai_api_key": {
            "pattern": r"\bsk-[A-Za-z0-9_-]{24,}\b",
            "secret_type": "api_key",
            "subtype": "openai",
            "severity": "high",
            "confidence": 0.95,
        },
        "anthropic_api_key": {
            "pattern": r"\bsk-ant-[A-Za-z0-9_-]{48,}\b",
            "secret_type": "api_key",
            "subtype": "anthropic",
            "severity": "high",
            "confidence": 0.95,
        },
    },
    "vcs_tokens": {
        "github_token": {
            "pattern": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
            "secret_type": "token",
            "subtype": "github",
            "severity": "high",
            "confidence": 0.95,
        },
        "github_fine_grained_token": {
            "pattern": r"\bgithub_pat_[0-9A-Za-z_]{40,}\b",
            "secret_type": "token",
            "subtype": "github_fine_grained",
            "severity": "high",
            "confidence": 0.95,
        },
        "gitlab_token": {
            "pattern": r"\bglpat-[0-9A-Za-z_-]{20,}\b",
            "secret_type": "token",
            "subtype": "gitlab",
            "severity": "high",
            "confidence": 0.95,
        },
        "github_oauth_token": {
            "pattern": r"\bgho_[0-9A-Za-z]{20,}\b",
            "secret_type": "token",
            "subtype": "github_oauth",
            "severity": "high",
            "confidence": 0.95,
        },
    },
    "cloud_keys": {
        "aws_access_key": {
            "pattern": r"\bAKIA[0-9A-Z]{16}\b",
            "secret_type": "api_key",
            "subtype": "aws_access_key",
            "severity": "high",
            "confidence": 0.95,
        },
        "google_api_key": {
            "pattern": r"\bAIza[0-9A-Za-z\-_]{35}\b",
            "secret_type": "api_key",
            "subtype": "google",
            "severity": "high",
            "confidence": 0.9,
        },
        "sendgrid_api_key": {
            "pattern": r"\bSG\.[0-9A-Za-z\-_]{16,}\.[0-9A-Za-z\-_]{16,}\b",
            "secret_type": "api_key",
            "subtype": "sendgrid",
            "severity": "high",
            "confidence": 0.95,
        },
        "mailgun_api_key": {
            "pattern": r"\bkey-[0-9A-Za-z]{32}\b",
            "secret_type": "api_key",
            "subtype": "mailgun",
            "severity": "high",
            "confidence": 0.9,
        },
        "aws_secret_assignment": {
            "pattern": r"(?i)\baws_secret[_-]?key\b\s*[:=]\s*(?:['\"][A-Za-z0-9/+=]{40}['\"]|[A-Za-z0-9/+=]{40})",
            "secret_type": "api_key",
            "subtype": "aws_secret_key",
            "severity": "high",
            "confidence": 0.95,
        },
        "heroku_api_key": {
            "pattern": r"\bheroku_api_key\b\s*[:=]\s*(?:['\"]?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}['\"]?)",
            "secret_type": "api_key",
            "subtype": "heroku",
            "severity": "high",
            "confidence": 0.9,
        },
    },
    "chatops_tokens": {
        "slack_token": {
            "pattern": r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b",
            "secret_type": "token",
            "subtype": "slack",
            "severity": "high",
            "confidence": 0.9,
        },
        "slack_webhook": {
            "pattern": r"https://hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+",
            "secret_type": "webhook",
            "subtype": "slack",
            "severity": "high",
            "confidence": 0.95,
        },
    },
    "auth_tokens": {
        "jwt_token": {
            "pattern": r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
            "secret_type": "token",
            "subtype": "jwt",
            "severity": "medium",
            "confidence": 0.75,
        },
        "bearer_token": {
            "pattern": r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}\b",
            "secret_type": "token",
            "subtype": "bearer",
            "severity": "high",
            "confidence": 0.85,
        },
    },
    "private_keys": {
        "private_key": {
            "pattern": r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----",
            "secret_type": "private_key",
            "subtype": "private_key",
            "severity": "critical",
            "confidence": 0.99,
        },
    },
    "db_urls": {
        "connection_string": {
            "pattern": r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis):\/\/[^\s]+",
            "secret_type": "connection_string",
            "subtype": "database_url",
            "severity": "high",
            "confidence": 0.9,
        },
        "password_in_url": {
            "pattern": r"://[^:\s]+:[^@\s]+@[^\s/$,]+",
            "secret_type": "credential",
            "subtype": "password_in_url",
            "severity": "high",
            "confidence": 0.9,
        },
    },
    "credential_assignments": {
        "generic_secret_assignment": {
            "pattern": r"(?i)\b(?:api_key|apikey|secret_key|secret|password|passwd|pwd|token|access_token|refresh_token|database_url)\b\s*[:=]\s*(?:['\"][^'\"]{8,}['\"]|[^\s{}\[\]\",;<>]{8,})",
            "secret_type": "credential",
            "subtype": "assignment",
            "severity": "medium",
            "confidence": 0.7,
        },
    },
}

DEFAULT_CODE_SECRET_GROUP_SELECTION = "llm_keys,vcs_tokens,cloud_keys,chatops_tokens,auth_tokens,private_keys,db_urls"

# 可逆占位符前缀 — ``<AKM-SEC:`` 在自然语言中极低概率出现，检测准确
_REVERSE_PREFIX = "<AKM-SEC:"
_REVERSE_SUFFIX = "/>"
# JSON 序列化常见把 ``/`` 写成 ``\/``，闭合后缀也需兼容
_REVERSE_SUFFIX_JSON_ESC = r"\/>"
# 宽松匹配：兼容空白、可选 JSON 转义斜杠、可选结尾 ``>`` 前空白
_PLACEHOLDER_LOOSE_RE = re.compile(
    r"<AKM-SEC:([A-Za-z0-9_.-]{1,48})@(\d+):([0-9a-fA-F]{6})\s*\\?/?>"
)


class Plugin(PluginBase):
    """数据安全插件 — 可逆占位符 + 响应安全拦截。"""

    # ── 占位符/反向映射 ──────────────────────────────────────

    def _reset_reverse_map(self):
        """每个请求开始时重置反向映射表和占位符序号。"""
        self._reverse_map: dict[str, str] = {}
        # 指纹索引：hash → original，用于模型轻微改写占位符时的宽松还原
        self._reverse_by_fingerprint: dict[str, str] = {}
        self._reverse_seq = 0

    @staticmethod
    def _safe_placeholder_tag(tag: str) -> str:
        """把标签规整为占位符可用的 ASCII 片段。

        中文等非 ASCII 标签若直接替换成 ``_``，会得到无辨识度的 ``___``，
        既不利于日志排查，也容易被模型在回显时弄乱。全非 ASCII 时改为
        ``t`` + 原标签 md5 前 6 位，保证稳定可读。
        """
        raw = str(tag or "x").strip() or "x"
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:48] or "x"
        # 仅有下划线/点/横线、没有字母数字时，用指纹标签兜底
        if not re.search(r"[A-Za-z0-9]", safe):
            safe = "t" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:6]
        return safe

    @staticmethod
    def _placeholder_variants(placeholder: str) -> list[str]:
        """生成占位符在响应文本中可能出现的变体（含 JSON 转义 ``/``）。"""
        variants = [placeholder]
        # 标准 JSON 对 ``/`` 的可选转义：``/>`` → ``\/>``
        if "/" in placeholder:
            escaped = placeholder.replace("/", r"\/")
            if escaped != placeholder:
                variants.append(escaped)
        return variants

    def _make_placeholder(self, tag: str, original: str) -> str:
        """生成 ``<AKM-SEC:tag@seq:hash/>`` 占位符并建立反向映射。

        序号保证同 tag / 同内容指纹下也不会互相覆盖；短 hash 仅便于日志辨认，
        并作为响应侧宽松还原的辅助索引。
        """
        self._reverse_seq += 1
        safe_tag = self._safe_placeholder_tag(tag)
        fprint = hashlib.md5(original.encode("utf-8")).hexdigest()[:6]
        placeholder = f"<AKM-SEC:{safe_tag}@{self._reverse_seq}:{fprint}/>"
        self._reverse_map[placeholder] = original
        # 同指纹后写覆盖：同一敏感值多次命中时还原到同一原文即可
        self._reverse_by_fingerprint[fprint] = original
        return placeholder

    def _reverse_replace(self, text: str, reverse_map: dict | None = None) -> tuple[str, bool]:
        """扫描 ``<AKM-SEC:`` 前缀做反向替换。可传入请求级 map 支持并发隔离。

        兼容三类回显形态：
        1. 精确占位符；
        2. JSON 转义斜杠 ``\\/``；
        3. 模型轻微改写 tag/空白后，仍带相同 6 位指纹的宽松匹配。
        """
        rmap = reverse_map if reverse_map is not None else self._reverse_map
        if not rmap or _REVERSE_PREFIX not in text:
            return text, False
        changed = False
        for placeholder, original in rmap.items():
            for variant in self._placeholder_variants(placeholder):
                if variant in text:
                    text = text.replace(variant, original)
                    changed = True

        # 宽松还原：按指纹从 map 反查；仅在精确/转义变体都未命中时启用
        if _REVERSE_PREFIX in text:
            # 从当前 reverse_map 重建指纹表，避免并发请求读到实例级脏索引
            fp_index: dict[str, str] = {}
            for placeholder, original in rmap.items():
                m = _PLACEHOLDER_LOOSE_RE.search(placeholder)
                if m:
                    fp_index[m.group(3).lower()] = original
            if not fp_index and reverse_map is None:
                fp_index = dict(getattr(self, "_reverse_by_fingerprint", {}) or {})

            def _loose_repl(match: re.Match) -> str:
                nonlocal changed
                fprint = match.group(3).lower()
                original = fp_index.get(fprint)
                if original is None:
                    return match.group(0)
                changed = True
                return original

            text = _PLACEHOLDER_LOOSE_RE.sub(_loose_repl, text)

        return text, changed

    def is_reverse_map_active(self, reverse_map: dict | None = None) -> bool:
        """判断是否有反向映射表需要还原。"""
        rmap = reverse_map if reverse_map is not None else self._reverse_map
        return bool(rmap)

    def reverse_stream_flush(self, state: dict, reverse_map: dict | None = None) -> str:
        """强制刷新流式还原缓冲。"""
        rmap = reverse_map if reverse_map is not None else self._reverse_map
        if not rmap:
            return ""
        pending = state.get("pending", "")
        if not pending:
            return ""
        output, _ = self._reverse_replace(pending, reverse_map=rmap)
        state["pending"] = ""
        return output

    def reverse_stream_state(self) -> dict:
        """创建流式还原缓冲状态。"""
        return {"pending": ""}

    def _max_placeholder_pending(self, reverse_map: dict) -> int:
        """未闭合占位符允许缓冲的最大长度；超出则视为假阳性前缀。"""
        # 真实占位符形如 <AKM-SEC:tag@seq:hash/>，通常远小于 128；
        # JSON 转义会多若干反斜杠，这里按 map 内最长 key 与下限取 max。
        longest = max((len(k) for k in reverse_map), default=0)
        return max(256, longest + 16)

    @staticmethod
    def _find_placeholder_close(text: str, start: int) -> tuple[int, int] | None:
        """在 ``start`` 之后查找占位符闭合位置。

        返回 ``(close_start, close_end)``：close_start 为 ``/`` 或 ``\\/`` 起点，
        close_end 为 ``>`` 之后的下标。同时兼容 ``/>`` 与 JSON 转义 ``\\/>``。
        """
        # 优先找未转义闭合；若 JSON 写了 \\/，``/>`` 仍是 ``\\/>`` 的后缀子串，
        # 但为了 closed_end 覆盖完整转义，需要单独识别 ``\\/>``。
        esc_idx = text.find(_REVERSE_SUFFIX_JSON_ESC, start)
        plain_idx = text.find(_REVERSE_SUFFIX, start)
        candidates: list[tuple[int, int]] = []
        if esc_idx >= 0:
            candidates.append((esc_idx, esc_idx + len(_REVERSE_SUFFIX_JSON_ESC)))
        if plain_idx >= 0:
            # 若 plain 命中的其实是 esc 的后半段，以更长的 esc 为准
            if esc_idx < 0 or plain_idx < esc_idx or plain_idx > esc_idx:
                candidates.append((plain_idx, plain_idx + len(_REVERSE_SUFFIX)))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0]

    def reverse_stream_chunk(self, text: str, state: dict, reverse_map: dict | None = None) -> str:
        """流式 chunk 还原：基于 ``<AKM-SEC:`` 前缀做缓冲与反向替换。"""
        rmap = reverse_map if reverse_map is not None else self._reverse_map
        if not rmap:
            return text

        state.setdefault("pending", "")
        full = state["pending"] + text
        max_pending = self._max_placeholder_pending(rmap)

        last_open = full.rfind(_REVERSE_PREFIX)
        if last_open >= 0:
            close_span = self._find_placeholder_close(full, last_open)
            if close_span is None:
                incomplete = full[last_open:]
                if last_open > 0:
                    output = self._reverse_replace(full[:last_open], reverse_map=rmap)[0]
                    # 未闭合前缀过长：不可能是本请求生成的占位符，直接放行
                    if len(incomplete) > max_pending:
                        state["pending"] = ""
                        return output + incomplete
                    state["pending"] = incomplete
                    return output
                if len(full) > max_pending:
                    # 假阳性前缀，整段按普通文本输出（无法匹配完整 key）
                    state["pending"] = ""
                    return full
                state["pending"] = full
                return ""
            # 闭合点之后可能还有下一段未闭合前缀，递归处理剩余
            _close_start, closed_end = close_span
            head = full[:closed_end]
            tail = full[closed_end:]
            output, _ = self._reverse_replace(head, reverse_map=rmap)
            state["pending"] = ""
            if tail:
                return output + self.reverse_stream_chunk(tail, state, reverse_map=rmap)
            return output

        if state["pending"]:
            output, _ = self._reverse_replace(full, reverse_map=rmap)
            state["pending"] = ""
            return output

        return text

    # ── 配置解析 ────────────────────────────────────────────

    async def on_load(self):
        """初始化内部缓存和反向映射表。"""
        self._sensitive_fields = set()
        self._keyword_rules = []
        self._regex_rules = []
        self._request_text_paths = set()
        self._response_block_patterns = []
        self._code_secret_rules = []
        self._reset_reverse_map()

    def _reload_config(self):
        """从当前插件配置重新解析规则。"""
        cfg = self.config or {}
        case_insensitive = cfg.get("process_keys_case_insensitive", True) is True

        raw_fields = cfg.get("sensitive_fields", "") or ""
        fields = self._split_items(raw_fields)
        if case_insensitive:
            self._sensitive_fields = {item.lower() for item in fields}
        else:
            self._sensitive_fields = set(fields)

        self._process_keys_case_insensitive = case_insensitive
        self._redact_replacement = str(cfg.get("redact_replacement", "[REDACTED]") or "[REDACTED]")
        self._keyword_rules = self._parse_keyword_sources(cfg.get("keyword_rules", "") or "")
        self._regex_rules = self._parse_regex_patterns(cfg.get("regex_rules", "") or "")
        # 留空表示处理所有字符串；缺省键时使用内置默认路径集合
        raw_text_paths = cfg.get("request_text_paths", DEFAULT_REQUEST_TEXT_PATHS)
        self._request_text_paths = set(self._split_items(raw_text_paths if raw_text_paths is not None else ""))
        self._recent_message_scan_limit = max(0, int(cfg.get("recent_message_scan_limit", 5) or 5))
        self._enabled = cfg.get("enabled", True) is True
        self._enable_code_secret_guard = cfg.get("enable_code_secret_guard", False) is True
        self._code_secret_guard_mode = str(cfg.get("code_secret_guard_mode", "warn") or "warn").strip().lower()
        self._code_secret_mask_replacement = str(
            cfg.get("code_secret_mask_replacement", "[CODE-SECRET]") or "[CODE-SECRET]"
        )
        self._code_secret_max_text_length = max(0, int(cfg.get("code_secret_max_text_length", 8000) or 8000))
        self._code_secret_confidence_threshold = self._safe_float(
            cfg.get("code_secret_confidence_threshold", 85),
            85.0,
        ) / 100.0
        # 代码敏感路径与关键词路径独立；缺省键时使用扩展后的默认集合
        raw_secret_paths = cfg.get("code_secret_paths", DEFAULT_CODE_SECRET_PATHS)
        self._code_secret_paths = set(self._split_items(raw_secret_paths if raw_secret_paths is not None else ""))
        self._code_secret_rule_groups = set(
            self._split_items(cfg.get("code_secret_rule_groups", DEFAULT_CODE_SECRET_GROUP_SELECTION) or DEFAULT_CODE_SECRET_GROUP_SELECTION)
        )
        self._code_secret_rules = self._build_code_secret_rules(
            cfg.get("code_secret_rule_set", "default") or "default",
            self._code_secret_rule_groups,
        )
        self._enable_response_guard = cfg.get("enable_response_guard", True) is True
        self._enable_stream_response_guard = cfg.get("enable_stream_response_guard", False) is True
        self._stream_guard_buffer_max_bytes = max(16384, int(cfg.get("stream_guard_buffer_max_bytes", 262144) or 262144))
        self._stream_guard_cache_chars = max(0, int(cfg.get("stream_guard_cache_chars", 2048) or 2048))
        self._response_guard_mode = str(cfg.get("response_guard_mode", "block") or "block").strip().lower()
        self._response_rule_actions = self._parse_rule_actions(cfg.get("response_rule_actions", "") or "")
        self._response_mask_replacement = str(
            cfg.get("response_mask_replacement", "[BLOCKED-RISKY-CONTENT]") or "[BLOCKED-RISKY-CONTENT]"
        )
        self._response_block_message = str(
            cfg.get("response_block_message", "检测到疑似高风险指令或恶意载荷，已由数据安全插件拦截。")
            or "检测到疑似高风险指令或恶意载荷，已由数据安全插件拦截。"
        )
        self._response_block_patterns = self._compile_patterns(
            self._split_lines(cfg.get("response_block_patterns", "") or "")
        )

    def _split_items(self, raw: str) -> list[str]:
        """把逗号/换行配置拆成条目列表。"""
        items = []
        for line in str(raw).replace("\r", "\n").split("\n"):
            for part in line.split(","):
                item = part.strip()
                if item:
                    items.append(item)
        return items

    def _split_lines(self, raw: str) -> list[str]:
        """只按行拆分配置，适合包含逗号的正则列表。"""
        items = []
        for line in str(raw).replace("\r", "\n").split("\n"):
            item = line.strip()
            if item:
                items.append(item)
        return items

    def _safe_float(self, value, fallback: float) -> float:
        """把配置值安全转成浮点数，避免非法输入破坏规则加载。"""
        try:
            return float(value)
        except Exception:
            return fallback

    def _parse_keyword_sources(self, raw: str) -> list[tuple[str, str]]:
        """解析关键词匹配源，支持 ``关键词#标签`` / ``关键词#@标签``（标签可选，默认 'keyword'）。"""
        sources = []
        for item in self._split_items(raw):
            if not item:
                continue
            # 去掉旧的 =target 部分
            if "=" in item:
                item = item.split("=", 1)[0].strip()
            if not item:
                continue
            if "#@" in item:
                source, tag = item.split("#@", 1)
            elif "#" in item:
                source, tag = item.rsplit("#", 1)
            else:
                source, tag = item, "keyword"
            source = source.strip()
            tag = tag.strip() or "keyword"
            if source:
                sources.append((source, tag))
        return sources

    def _parse_regex_patterns(self, raw: str) -> list[tuple[re.Pattern, str]]:
        """解析正则匹配模式，支持 ``正则#标签`` 格式（标签可选，默认 'regex'）。"""
        patterns = []
        for line in str(raw).replace("\r", "\n").split("\n"):
            item = line.strip()
            if not item:
                continue
            # 去掉旧的 =>target 部分
            if "=>" in item:
                item = item.split("=>", 1)[0].strip()
            if not item:
                continue
            # 提取 #@标签
            if "#@" in item:
                pattern_text, tag = item.rsplit("#@", 1)
            else:
                pattern_text, tag = item, "regex"
            pattern_text = pattern_text.strip()
            tag = tag.strip() or "regex"
            if not pattern_text:
                continue
            try:
                patterns.append((re.compile(pattern_text), tag))
            except re.error as exc:
                self.logger.warning(f"[data_filter_guard] 忽略非法正则规则: {pattern_text} ({exc})")
        return patterns

    def _compile_patterns(self, patterns: list[str]) -> list[re.Pattern]:
        """编译响应拦截正则列表。

        兼容历史默认值每行末尾多余逗号（JSON 多行字符串手误），避免规则整体失效。
        """
        compiled = []
        for item in patterns:
            pattern_text = str(item or "").strip().rstrip(",").strip()
            if not pattern_text:
                continue
            try:
                compiled.append(re.compile(pattern_text))
            except re.error as exc:
                self.logger.warning(f"[data_filter_guard] 忽略非法拦截正则: {pattern_text} ({exc})")
        return compiled

    def _build_code_secret_rules(self, rule_set: str, enabled_groups: set[str]) -> list[dict]:
        """构建轻量代码敏感规则列表。

        这里不直接引入第三方扫描器，而是提炼一组高确定性的运行时规则：
        - 优先覆盖 API Key、Token、私钥、连接串等开发场景高频泄漏项；
        - 规则规模刻意控制在较小范围，避免把实时请求路径拖成完整仓库扫描器；
        - 替换由调用方通过可逆占位符动态生成。
        """
        if str(rule_set).strip().lower() not in ("", "default"):
            return []

        rules = []
        selected_groups = enabled_groups or set(DEFAULT_CODE_SECRET_RULE_GROUPS.keys())
        for group_name, group_rules in DEFAULT_CODE_SECRET_RULE_GROUPS.items():
            if group_name not in selected_groups:
                continue
            for rule_id, item in group_rules.items():
                try:
                    rules.append({
                        "id": rule_id,
                        "group": group_name,
                        "pattern": re.compile(item["pattern"]),
                        "secret_type": item["secret_type"],
                        "subtype": item["subtype"],
                        "severity": item["severity"],
                        "confidence": float(item["confidence"]),
                    })
                except re.error as exc:
                    self.logger.warning(f"[data_filter_guard] 忽略非法代码敏感规则: {rule_id} ({exc})")
        return rules

    def _parse_rule_actions(self, raw: str) -> dict[str, str]:
        """解析单条响应规则的动作覆盖。"""
        actions = {}
        for line in str(raw).replace("\r", "\n").split("\n"):
            item = line.strip()
            if not item or "=>" not in item:
                continue
            pattern_text, action = item.rsplit("=>", 1)
            pattern_text = pattern_text.strip()
            action = action.strip().lower()
            if pattern_text and action in ("warn", "mask", "block"):
                actions[pattern_text] = action
        return actions

    # ── 请求处理 ────────────────────────────────────────────

    def _normalize_key(self, key: str) -> str:
        """按配置决定字段名是否大小写归一。"""
        if self._process_keys_case_insensitive:
            return key.lower()
        return key

    def _path_matches(self, path: str, candidates: set[str]) -> bool:
        """判断路径是否命中指定候选集合。

        ``messages[].content`` 需同时覆盖：
        - 字符串 content：``messages[0].content``
        - 多模态 / Anthropic content blocks：``messages[0].content[0].text``
        因此候选既匹配自身，也匹配以其为前缀的 ``.`` / ``[`` 子路径。
        """
        if not candidates:
            return True
        normalized = path.replace(".[", "[")
        generalized = re.sub(r"\[\d+\]", "[]", normalized)

        def _is_prefix(parent: str, child: str) -> bool:
            if not parent:
                return False
            if child == parent:
                return True
            # 避免 messages[].content 误匹配 messages[].content_type
            return child.startswith(parent + ".") or child.startswith(parent + "[")

        for allowed in candidates:
            allowed_n = str(allowed or "").replace(".[", "[").strip()
            if not allowed_n:
                continue
            candidate = allowed_n.replace("[]", "[0]")
            if _is_prefix(allowed_n, normalized) or _is_prefix(allowed_n, generalized):
                return True
            if _is_prefix(candidate, normalized) or _is_prefix(candidate, generalized):
                return True
        return False

    def _should_scan_message_item(self, path: str, idx: int, total: int) -> bool:
        """判断 `messages` 列表中的当前项是否需要进入文本扫描。"""
        if path != "messages":
            return True
        if self._recent_message_scan_limit <= 0:
            return True
        start = max(0, total - self._recent_message_scan_limit)
        return idx >= start

    def _is_top_level_messages_list(self, path: str) -> bool:
        """判断当前列表节点是否为请求顶层 `messages`。"""
        return path == "messages"

    def _scan_code_secrets(self, text: str) -> list[dict]:
        """扫描字符串中的代码类敏感信息并返回命中列表。"""
        matches = []
        if not text or not self._enable_code_secret_guard:
            return matches
        if self._code_secret_max_text_length > 0 and len(text) > self._code_secret_max_text_length:
            return matches

        for rule in self._code_secret_rules:
            if rule["confidence"] < self._code_secret_confidence_threshold:
                continue
            for item in rule["pattern"].finditer(text):
                matches.append({
                    "id": rule["id"],
                    "start": item.start(),
                    "end": item.end(),
                    "value": item.group(0),
                    "secret_type": rule["secret_type"],
                    "subtype": rule["subtype"],
                    "severity": rule["severity"],
                    "confidence": rule["confidence"],
                })

        if not matches:
            return matches

        # 消除重叠：置信度优先，其次命中长度优先
        matches.sort(key=lambda m: (-m["confidence"], -(m["end"] - m["start"]), m["start"]))
        selected = []
        for match in matches:
            overlapped = False
            for existing in selected:
                if not (match["end"] <= existing["start"] or match["start"] >= existing["end"]):
                    overlapped = True
                    break
            if not overlapped:
                selected.append(match)

        selected.sort(key=lambda m: m["start"])
        return selected

    def _mask_code_secrets_with_placeholders(self, text: str, matches: list[dict]) -> str:
        """用可逆占位符替换命中片段。"""
        if not matches:
            return text
        parts = []
        cursor = 0
        for match in matches:
            parts.append(text[cursor:match["start"]])
            placeholder = self._make_placeholder(
                f"secret_{match['subtype']}",
                match["value"],
            )
            parts.append(placeholder)
            cursor = match["end"]
        parts.append(text[cursor:])
        return "".join(parts)

    def _apply_text_rules(self, text: str) -> str:
        """依次应用关键词和正则替换规则，统一用可逆占位符。

        正则替换会跳过已生成的 ``<AKM-SEC:.../>`` 片段，避免二次匹配嵌套破坏映射。
        """
        new_text = text
        for source, tag in self._keyword_rules:
            if source and source in new_text:
                # 已是占位符本体时不再替换
                if source.startswith(_REVERSE_PREFIX) and source.endswith(_REVERSE_SUFFIX):
                    continue
                placeholder = self._make_placeholder(tag, source)
                new_text = new_text.replace(source, placeholder)
        for pattern, tag in self._regex_rules:
            def _repl(m, t=tag):
                matched = m.group(0)
                # 防止规则匹配到占位符前缀/正文导致嵌套
                if _REVERSE_PREFIX in matched or matched.startswith("AKM-SEC"):
                    return matched
                # 若命中落在已有占位符内部，保持原样
                start = m.start()
                left = new_text.rfind(_REVERSE_PREFIX, 0, start)
                if left >= 0:
                    right = new_text.find(_REVERSE_SUFFIX, left)
                    if right >= 0 and left <= start <= right + len(_REVERSE_SUFFIX):
                        return matched
                return self._make_placeholder(t, matched)
            new_text = pattern.sub(_repl, new_text)
        return new_text

    def _apply_request_text_guards(
        self,
        text: str,
        path: str,
        *,
        apply_text_rules: bool = True,
        apply_code_secret: bool = True,
    ) -> tuple[str, bool]:
        """对请求字符串执行文本替换与/或代码敏感识别。

        - 关键词/正则替换：可逆占位符（由 ``request_text_paths`` 门控）。
        - 代码敏感识别：warn 仅告警不改写；mask/block 统一用可逆占位符替换并告警
          （由 ``code_secret_paths`` 门控，与关键词路径相互独立）。
        - block 不再阻断请求，改为等同 mask + 告警。
        """
        new_text = self._apply_text_rules(text) if apply_text_rules else text

        if not apply_code_secret or not self._enable_code_secret_guard:
            return new_text, new_text != text
        if not self._path_matches(path, self._code_secret_paths):
            return new_text, new_text != text

        matches = self._scan_code_secrets(new_text)
        if not matches:
            return new_text, new_text != text

        summary = ", ".join(f"{m['secret_type']}.{m['subtype']}" for m in matches[:5])

        if self._code_secret_guard_mode == "warn":
            self.logger.warning(f"[data_filter_guard] 请求体命中代码敏感规则(仅告警): {summary}")
            return new_text, new_text != text

        # mask 与 block 统一：替换为可逆占位符并告警
        mode_label = self._code_secret_guard_mode.upper()
        self.logger.warning(f"[data_filter_guard] 请求体命中代码敏感规则({mode_label}): {summary}")
        masked = self._mask_code_secrets_with_placeholders(new_text, matches)
        return masked, True

    def _mask_and_filter(self, value, path: str = ""):
        """递归处理任意 JSON 风格数据，返回 (处理后数据, 是否发生改写)。"""
        if isinstance(value, dict):
            changed = False
            result = {}
            for raw_key, raw_val in value.items():
                key = str(raw_key)
                current_path = f"{path}.{key}" if path else key
                if self._normalize_key(key) in self._sensitive_fields:
                    # 敏感字段名命中 → 整个字段值替换为 [REDACTED]
                    # 这类字段值 AI 不会在响应中引用，不需要可逆映射
                    result[raw_key] = self._redact_replacement
                    changed = True
                    continue
                new_val, sub_changed = self._mask_and_filter(raw_val, current_path)
                result[raw_key] = new_val
                changed = changed or sub_changed
            return result, changed

        if isinstance(value, list):
            changed = False
            result = []
            total = len(value)
            for idx, item in enumerate(value):
                if self._is_top_level_messages_list(path) and not self._should_scan_message_item(path, idx, total):
                    result.append(item)
                    continue
                current_path = f"{path}[{idx}]" if path else f"[{idx}]"
                new_item, sub_changed = self._mask_and_filter(item, current_path)
                result.append(new_item)
                changed = changed or sub_changed
            return result, changed

        if isinstance(value, str):
            # 关键词/正则与代码敏感使用独立路径门控：
            # 仅在 code_secret_paths 命中、request_text_paths 未命中时，仍应执行密钥扫描。
            apply_text = self._path_matches(path, self._request_text_paths)
            apply_secret = self._enable_code_secret_guard and self._path_matches(path, self._code_secret_paths)
            if not apply_text and not apply_secret:
                return value, False
            new_text, changed = self._apply_request_text_guards(
                value,
                path,
                apply_text_rules=apply_text,
                apply_code_secret=apply_secret,
            )
            return new_text, changed

        return value, False

    # ── on_request / on_response ─────────────────────────────

    async def on_request(self, ctx) -> dict | None:
        """请求预处理：建立反向映射表，对请求体执行脱敏替换。

        可逆映射写入 ``ctx.bag['data_filter_guard.reverse_map']``，
        不再污染业务 request，避免转发层遗漏剥离或下游插件丢字段。
        """
        self._reload_config()
        self._reset_reverse_map()
        if not self._enabled:
            return None

        request = ctx.request
        if not isinstance(request, dict):
            return None

        new_request, changed = self._mask_and_filter(request)
        if self._reverse_map:
            # 请求级 bag：同一 ctx 贯穿 on_response / 流式还原，并发隔离
            ctx.bag_set("data_filter_guard.reverse_map", dict(self._reverse_map))
        if changed:
            self.logger.info("[data_filter_guard] 请求体已执行脱敏/过滤（可逆占位符）")
            return new_request
        return None

    def _collect_content_texts(self, content) -> list[str]:
        """从 OpenAI/Anthropic/Responses 风格 content 字段收集可见文本。"""
        texts: list[str] = []
        if isinstance(content, str):
            if content:
                texts.append(content)
            return texts
        if isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    if part:
                        texts.append(part)
                    continue
                if not isinstance(part, dict):
                    continue
                for key in ("text", "content", "output_text", "refusal"):
                    val = part.get(key)
                    if isinstance(val, str) and val:
                        texts.append(val)
                    elif isinstance(val, list):
                        texts.extend(self._collect_content_texts(val))
        return texts

    def _extract_response_text(self, response_body: str) -> str:
        """尽量从非流式 JSON 响应中提取可见文本，用于安全扫描。

        覆盖：
        - OpenAI Chat：choices[].message/delta.content
        - OpenAI Responses：output[].content[].text
        - Anthropic Messages：顶层 content[].text
        """
        try:
            data = json.loads(response_body)
        except Exception:
            return response_body

        texts: list[str] = []
        if isinstance(data, dict):
            # Anthropic Messages 非流式
            if "content" in data:
                texts.extend(self._collect_content_texts(data.get("content")))

            choices = data.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    message = choice.get("message")
                    if isinstance(message, dict):
                        texts.extend(self._collect_content_texts(message.get("content")))
                        if isinstance(message.get("refusal"), str):
                            texts.append(message.get("refusal", ""))
                    delta = choice.get("delta")
                    if isinstance(delta, dict):
                        texts.extend(self._collect_content_texts(delta.get("content")))
                    # 少数实现把文本放在 choice.text
                    if isinstance(choice.get("text"), str):
                        texts.append(choice.get("text", ""))

            output = data.get("output")
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    texts.extend(self._collect_content_texts(item.get("content")))
                    if isinstance(item.get("text"), str):
                        texts.append(item.get("text", ""))
        return "\n".join(x for x in texts if x)

    def _make_safe_response_body(self, api_path: str) -> str:
        """根据 api_path 构造对应协议的安全拦截响应体。"""
        msg = self._response_block_message
        if api_path == "messages":
            return json.dumps(
                {
                    "id": "msg_akm_security",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": msg}],
                    "model": "data_filter_guard",
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
                ensure_ascii=False,
            )
        if api_path == "responses":
            return json.dumps(
                {
                    "id": "resp_akm_security",
                    "object": "response",
                    "status": "completed",
                    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": msg}]}],
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "id": "akm_security",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": msg}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
            ensure_ascii=False,
        )

    def _resolve_rule_action(self, pattern: re.Pattern) -> str:
        """根据规则覆盖或全局默认值决定本条命中的处理动作。"""
        return self._response_rule_actions.get(pattern.pattern, self._response_guard_mode)

    def _build_safe_stream_payload(self, api_path: str) -> str:
        """根据 api_path 构造对应协议的流式安全返回。"""
        msg = self._response_block_message
        if api_path == "messages":
            return (
                'event: message_start\n'
                'data: {"type":"message_start","message":{"id":"akm_security","type":"message","role":"assistant","model":"data_filter_guard","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":0,"output_tokens":0}}}\n\n'
                'event: content_block_start\n'
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                'event: content_block_delta\n'
                'data: ' + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": msg}}, ensure_ascii=False) + '\n\n'
                'event: content_block_stop\n'
                'data: {"type":"content_block_stop","index":0}\n\n'
                'event: message_delta\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":0}}\n\n'
                'event: message_stop\n'
                'data: {"type":"message_stop"}\n\n'
                'data: [DONE]\n\n'
            )
        if api_path == "responses":
            return (
                'event: response.output_text.delta\n'
                'data: ' + json.dumps({"type": "response.output_text.delta", "delta": msg}, ensure_ascii=False) + '\n\n'
                'event: response.output_text.done\n'
                'data: {"type":"response.output_text.done"}\n\n'
                'event: response.completed\n'
                'data: {"type":"response.completed"}\n\n'
                'data: [DONE]\n\n'
            )
        return (
            'data: ' + json.dumps(
                {
                    "id": "akm_security",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": msg}, "finish_reason": None}],
                },
                ensure_ascii=False,
            ) + '\n\n'
            + 'data: ' + json.dumps(
                {
                    "id": "akm_security",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
                ensure_ascii=False,
            ) + '\n\n'
            + 'data: [DONE]\n\n'
        )

    def _mask_response_body(self, response_body: str) -> tuple[str, bool]:
        """对非流式响应正文做局部危险片段替换。"""
        updated = response_body
        changed = False
        for pattern in self._response_block_patterns:
            replaced = pattern.sub(self._response_mask_replacement, updated)
            if replaced != updated:
                changed = True
                updated = replaced
        return updated, changed

    async def on_response(self, ctx):
        """响应处理：先反向还原占位符为原始值，再做安全扫描拦截。"""
        self._reload_config()
        if not self._enabled:
            return None
        response = ctx.response
        if not isinstance(response, dict):
            return None

        # ── 反向还原占位符（优先 bag，兼容遗留 request 字段） ──
        rev_map = ctx.bag_get("data_filter_guard.reverse_map")
        if not isinstance(rev_map, dict) or not rev_map:
            request = ctx.request
            rev_map = request.get("__akm_reverse_map__") if isinstance(request, dict) else None
        response_body = response.get("response_body")
        if isinstance(response_body, str) and response_body and rev_map:
            restored, reverted = self._reverse_replace(response_body, reverse_map=rev_map)
            if reverted:
                response = dict(response)
                response["response_body"] = restored
                self.logger.info("[data_filter_guard] 响应体已反向还原占位符")

        # ── 响应安全拦截 ──
        if not self._enable_response_guard:
            return response

        # 流式响应的安全扫描在 protect_stream_payload / inspect_stream_chunk 中处理
        if response.get("stream") is True:
            return response

        scan_text = self._extract_response_text(response.get("response_body", ""))
        for pattern in self._response_block_patterns:
            if not pattern.search(scan_text):
                continue
            guarded = dict(response)
            guarded["status_code"] = 200
            guarded["security_reason"] = pattern.pattern
            action = self._resolve_rule_action(pattern)

            if action == "warn":
                guarded["security_warned"] = True
                guarded["security_action"] = "warn"
                self.logger.warning("[data_filter_guard] 响应命中高风险规则，已标记告警")
                return guarded

            api_path = response.get("api_path", "chat/completions")
            if action == "mask":
                current_body = guarded.get("response_body", "")
                # 只替换当前命中规则，避免把其它应为 warn 的规则一并 mask
                masked_body = pattern.sub(self._response_mask_replacement, current_body)
                if masked_body != current_body:
                    guarded["response_body"] = masked_body
                    guarded["security_masked"] = True
                    guarded["security_action"] = "mask"
                    self.logger.warning("[data_filter_guard] 响应命中高风险规则，已局部替换")
                    return guarded

            guarded["security_blocked"] = True
            guarded["security_action"] = "block"
            guarded["response_body"] = self._make_safe_response_body(api_path)
            self.logger.warning("[data_filter_guard] 响应命中高风险规则，已拦截返回")
            return guarded
        return response

    # ── 流式响应安全方法（供 proxy/server 调用） ─────────────────

    def is_stream_guard_active(self) -> bool:
        """判断是否启用了流式响应安全保护。"""
        self._reload_config()
        return self._enabled and self._enable_response_guard and self._enable_stream_response_guard

    def stream_guard_requires_buffering(self) -> bool:
        """判断当前流式响应保护是否需要先完成整段缓冲。

        安全规则在流尾才命中时，若此前内容已透传，block 只能中断后续输出而
        不能真正阻止危险片段抵达客户端。因此只要用户显式开启流式保护，就在
        配置的有界上限内先完成整段扫描；超限时才退为增量扫描并尽早中断。
        """
        self._reload_config()
        return self.is_stream_guard_active()

    def stream_guard_buffer_max_bytes(self) -> int:
        """返回整段缓冲模式允许占用的最大字节数。"""
        self._reload_config()
        return max(self._stream_guard_buffer_max_bytes, 16384)

    def create_stream_guard_state(self) -> dict:
        """创建流式增量扫描状态。"""
        self._reload_config()
        return {
            "tail": "",
            "matched_patterns": set(),
            "cache_limit": self._stream_guard_cache_chars,
        }

    def inspect_stream_chunk(self, api_path: str, payload_text: str, state: dict) -> tuple[dict, bool, str, str]:
        """基于滑动窗口对流式 chunk 做增量安全扫描。

        返回值：
        - 新状态
        - 是否需要改写当前输出（仅 block 会返回 True）
        - 命中规则原因
        - 动作（warn / blocked / ""）
        """
        self._reload_config()
        if not self.is_stream_guard_active():
            return state, False, "", ""

        matched_patterns = state.get("matched_patterns")
        if not isinstance(matched_patterns, set):
            matched_patterns = set(matched_patterns or [])

        tail = str(state.get("tail", "") or "")
        cache_limit = max(0, int(state.get("cache_limit", self._stream_guard_cache_chars) or self._stream_guard_cache_chars))
        scan_text = tail + payload_text

        for pattern in self._response_block_patterns:
            if pattern.pattern in matched_patterns:
                continue
            if not pattern.search(scan_text):
                continue

            action = self._resolve_rule_action(pattern)
            matched_patterns.add(pattern.pattern)
            next_state = {
                "tail": scan_text[-cache_limit:] if cache_limit > 0 else "",
                "matched_patterns": matched_patterns,
                "cache_limit": cache_limit,
            }
            if action == "warn":
                return next_state, False, pattern.pattern, "warn"
            if action == "mask":
                # 增量扫描路径无法对已发出/当前 chunk 做局部替换（server 只认 blocked）。
                # 为避免危险内容透传，溢出后的 mask 退化为 block。
                return next_state, True, pattern.pattern, "blocked"
            return next_state, True, pattern.pattern, "blocked"

        return {
            "tail": scan_text[-cache_limit:] if cache_limit > 0 else "",
            "matched_patterns": matched_patterns,
            "cache_limit": cache_limit,
        }, False, "", ""

    def protect_stream_payload(self, api_path: str, payload_text: str) -> tuple[str, bool, str, str]:
        """对客户端侧 SSE 文本做安全处理。"""
        self._reload_config()
        if not self._enabled or not self._enable_response_guard or not self._enable_stream_response_guard:
            return payload_text, False, "", ""

        for pattern in self._response_block_patterns:
            if not pattern.search(payload_text):
                continue
            action = self._resolve_rule_action(pattern)
            if action == "warn":
                return payload_text, False, pattern.pattern, "warn"
            if action == "mask":
                replaced = pattern.sub(self._response_mask_replacement, payload_text)
                if replaced != payload_text:
                    return replaced, True, pattern.pattern, "masked"
            return self._build_safe_stream_payload(api_path), True, pattern.pattern, "blocked"
        return payload_text, False, "", ""
