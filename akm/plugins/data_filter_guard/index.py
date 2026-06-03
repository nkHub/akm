"""数据安全插件。

能力范围：
1. 请求侧：递归脱敏敏感字段，并对指定路径文本执行关键词/正则替换。
2. 响应侧：扫描非流式成功响应中的高风险命令/脚本片段，并按模式做拦截或局部替换。

说明：
- 该插件属于内置插件，但默认关闭；用户可按需在插件页启用。
- 当前仍不做上游可逆加密，因为那必须与上游约定解密协议。
"""

from akm.plugins import PluginBase
import json
import re


DEFAULT_CODE_SECRET_RULE_GROUPS = {
    "llm_keys": {
        "openai_project_key": {
            "pattern": r"\bsk-proj-[A-Za-z0-9_-]{40,}\b",
            "secret_type": "api_key",
            "subtype": "openai_project",
            "severity": "high",
            "confidence": 0.95,
            "replacement": "[CODE-SECRET:OPENAI-PROJECT-KEY]",
        },
        "openai_api_key": {
            "pattern": r"\bsk-[A-Za-z0-9_-]{24,}\b",
            "secret_type": "api_key",
            "subtype": "openai",
            "severity": "high",
            "confidence": 0.95,
            "replacement": "[CODE-SECRET:OPENAI-KEY]",
        },
        "anthropic_api_key": {
            "pattern": r"\bsk-ant-[A-Za-z0-9_-]{48,}\b",
            "secret_type": "api_key",
            "subtype": "anthropic",
            "severity": "high",
            "confidence": 0.95,
            "replacement": "[CODE-SECRET:ANTHROPIC-KEY]",
        },
    },
    "vcs_tokens": {
        "github_token": {
            "pattern": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
            "secret_type": "token",
            "subtype": "github",
            "severity": "high",
            "confidence": 0.95,
            "replacement": "[CODE-SECRET:GITHUB-TOKEN]",
        },
        "github_fine_grained_token": {
            "pattern": r"\bgithub_pat_[0-9A-Za-z_]{40,}\b",
            "secret_type": "token",
            "subtype": "github_fine_grained",
            "severity": "high",
            "confidence": 0.95,
            "replacement": "[CODE-SECRET:GITHUB-PAT]",
        },
        "gitlab_token": {
            "pattern": r"\bglpat-[0-9A-Za-z_-]{20,}\b",
            "secret_type": "token",
            "subtype": "gitlab",
            "severity": "high",
            "confidence": 0.95,
            "replacement": "[CODE-SECRET:GITLAB-TOKEN]",
        },
        "github_oauth_token": {
            "pattern": r"\bgho_[0-9A-Za-z]{20,}\b",
            "secret_type": "token",
            "subtype": "github_oauth",
            "severity": "high",
            "confidence": 0.95,
            "replacement": "[CODE-SECRET:GITHUB-OAUTH]",
        },
    },
    "cloud_keys": {
        "aws_access_key": {
            "pattern": r"\bAKIA[0-9A-Z]{16}\b",
            "secret_type": "api_key",
            "subtype": "aws_access_key",
            "severity": "high",
            "confidence": 0.95,
            "replacement": "[CODE-SECRET:AWS-ACCESS-KEY]",
        },
        "google_api_key": {
            "pattern": r"\bAIza[0-9A-Za-z\-_]{35}\b",
            "secret_type": "api_key",
            "subtype": "google",
            "severity": "high",
            "confidence": 0.9,
            "replacement": "[CODE-SECRET:GOOGLE-API-KEY]",
        },
        "sendgrid_api_key": {
            "pattern": r"\bSG\.[0-9A-Za-z\-_]{16,}\.[0-9A-Za-z\-_]{16,}\b",
            "secret_type": "api_key",
            "subtype": "sendgrid",
            "severity": "high",
            "confidence": 0.95,
            "replacement": "[CODE-SECRET:SENDGRID-KEY]",
        },
        "mailgun_api_key": {
            "pattern": r"\bkey-[0-9A-Za-z]{32}\b",
            "secret_type": "api_key",
            "subtype": "mailgun",
            "severity": "high",
            "confidence": 0.9,
            "replacement": "[CODE-SECRET:MAILGUN-KEY]",
        },
        "aws_secret_assignment": {
            "pattern": r"(?i)\baws_secret[_-]?key\b\s*[:=]\s*(?:['\"][A-Za-z0-9/+=]{40}['\"]|[A-Za-z0-9/+=]{40})",
            "secret_type": "api_key",
            "subtype": "aws_secret_key",
            "severity": "high",
            "confidence": 0.95,
            "replacement": "[CODE-SECRET:AWS-SECRET-KEY]",
        },
        "heroku_api_key": {
            "pattern": r"\bheroku_api_key\b\s*[:=]\s*(?:['\"]?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}['\"]?)",
            "secret_type": "api_key",
            "subtype": "heroku",
            "severity": "high",
            "confidence": 0.9,
            "replacement": "[CODE-SECRET:HEROKU-KEY]",
        },
    },
    "chatops_tokens": {
        "slack_token": {
            "pattern": r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b",
            "secret_type": "token",
            "subtype": "slack",
            "severity": "high",
            "confidence": 0.9,
            "replacement": "[CODE-SECRET:SLACK-TOKEN]",
        },
        "slack_webhook": {
            "pattern": r"https://hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+",
            "secret_type": "webhook",
            "subtype": "slack",
            "severity": "high",
            "confidence": 0.95,
            "replacement": "[CODE-SECRET:SLACK-WEBHOOK]",
        },
    },
    "auth_tokens": {
        "jwt_token": {
            "pattern": r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
            "secret_type": "token",
            "subtype": "jwt",
            "severity": "medium",
            "confidence": 0.75,
            "replacement": "[CODE-SECRET:JWT]",
        },
        "bearer_token": {
            "pattern": r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}\b",
            "secret_type": "token",
            "subtype": "bearer",
            "severity": "high",
            "confidence": 0.85,
            "replacement": "[CODE-SECRET:BEARER-TOKEN]",
        },
    },
    "private_keys": {
        "private_key": {
            "pattern": r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----",
            "secret_type": "private_key",
            "subtype": "private_key",
            "severity": "critical",
            "confidence": 0.99,
            "replacement": "[CODE-SECRET:PRIVATE-KEY]",
        },
    },
    "db_urls": {
        "connection_string": {
            "pattern": r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis):\/\/[^\s]+",
            "secret_type": "connection_string",
            "subtype": "database_url",
            "severity": "high",
            "confidence": 0.9,
            "replacement": "[CODE-SECRET:CONNECTION-STRING]",
        },
        "password_in_url": {
            "pattern": r"://[^:\s]+:[^@\s]+@[^\s/$,]+",
            "secret_type": "credential",
            "subtype": "password_in_url",
            "severity": "high",
            "confidence": 0.9,
            "replacement": "[CODE-SECRET:PASSWORD-IN-URL]",
        },
    },
    "credential_assignments": {
        "generic_secret_assignment": {
            "pattern": r"(?i)\b(?:api_key|apikey|secret_key|secret|password|passwd|pwd|token|access_token|refresh_token|database_url)\b\s*[:=]\s*(?:['\"][^'\"]{8,}['\"]|[^\s{}\[\]\",;<>]{8,})",
            "secret_type": "credential",
            "subtype": "assignment",
            "severity": "medium",
            "confidence": 0.7,
            "replacement": "[CODE-SECRET:CREDENTIAL]",
        },
    },
}

DEFAULT_CODE_SECRET_GROUP_SELECTION = "llm_keys,vcs_tokens,cloud_keys,chatops_tokens,auth_tokens,private_keys,db_urls"


class Plugin(PluginBase):
    """数据安全插件。"""

    async def on_load(self):
        """初始化内部缓存。"""
        self._sensitive_fields = set()
        self._keyword_rules = []
        self._regex_rules = []
        self._request_text_paths = set()
        self._response_block_patterns = []
        self._code_secret_rules = []

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
        self._keyword_rules = self._parse_keyword_rules(cfg.get("keyword_rules", "") or "")
        self._regex_rules = self._parse_regex_rules(cfg.get("regex_rules", "") or "")
        self._request_text_paths = set(self._split_items(cfg.get("request_text_paths", "") or ""))
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
        self._code_secret_paths = set(self._split_items(cfg.get("code_secret_paths", "") or ""))
        self._code_secret_rule_groups = set(
            self._split_items(cfg.get("code_secret_rule_groups", DEFAULT_CODE_SECRET_GROUP_SELECTION) or DEFAULT_CODE_SECRET_GROUP_SELECTION)
        )
        self._code_secret_rules = self._build_code_secret_rules(
            cfg.get("code_secret_rule_set", "default") or "default",
            self._code_secret_rule_groups,
        )
        self._enable_response_guard = cfg.get("enable_response_guard", True) is True
        # 流式响应无法像普通 JSON 那样按对象粒度扫描；若开启该能力，
        # 服务端会先缓冲整段 SSE，再统一做规则检查。因此默认单独关闭，
        # 避免用户只想做请求脱敏时误伤流式首字节返回体验。
        self._enable_stream_response_guard = cfg.get("enable_stream_response_guard", False) is True
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

    def _parse_keyword_rules(self, raw: str) -> list[tuple[str, str]]:
        """解析关键词替换规则。"""
        rules = []
        for item in self._split_items(raw):
            if "=" not in item:
                continue
            source, target = item.split("=", 1)
            source = source.strip()
            target = target.strip()
            if source:
                rules.append((source, target))
        return rules

    def _parse_regex_rules(self, raw: str) -> list[tuple[re.Pattern, str]]:
        """解析正则替换规则。"""
        rules = []
        for line in str(raw).replace("\r", "\n").split("\n"):
            item = line.strip()
            if not item or "=>" not in item:
                continue
            pattern_text, replacement = item.split("=>", 1)
            pattern_text = pattern_text.strip()
            replacement = replacement.strip()
            if not pattern_text:
                continue
            try:
                rules.append((re.compile(pattern_text), replacement))
            except re.error as exc:
                self.logger.warning(f"[data_filter_guard] 忽略非法正则规则: {pattern_text} ({exc})")
        return rules

    def _compile_patterns(self, patterns: list[str]) -> list[re.Pattern]:
        """编译响应拦截正则列表。"""
        compiled = []
        for item in patterns:
            try:
                compiled.append(re.compile(item))
            except re.error as exc:
                self.logger.warning(f"[data_filter_guard] 忽略非法拦截正则: {item} ({exc})")
        return compiled

    def _build_code_secret_rules(self, rule_set: str, enabled_groups: set[str]) -> list[dict]:
        """构建轻量代码敏感规则列表。

        这里不直接引入第三方扫描器，而是提炼一组高确定性的运行时规则：
        - 优先覆盖 API Key、Token、私钥、连接串等开发场景高频泄漏项；
        - 规则规模刻意控制在较小范围，避免把实时请求路径拖成完整仓库扫描器；
        - 每条规则都附带类型、置信度与替换文本，便于按动作模式统一处理。
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
                        "replacement": str(item["replacement"]),
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

    def _normalize_key(self, key: str) -> str:
        """按配置决定字段名是否大小写归一。"""
        if self._process_keys_case_insensitive:
            return key.lower()
        return key

    def _should_filter_text_path(self, path: str) -> bool:
        """判断当前字符串路径是否在允许处理范围内。"""
        if not self._request_text_paths:
            return True
        normalized = path.replace('.[', '[')
        generalized = re.sub(r"\[\d+\]", "[]", normalized)
        for allowed in self._request_text_paths:
            candidate = allowed.replace('[]', '[0]')
            if normalized == allowed or normalized == candidate or normalized.startswith(candidate + '.'):
                return True
            if generalized == allowed or generalized.startswith(allowed + '.'):
                return True
        return False

    def _path_matches(self, path: str, candidates: set[str]) -> bool:
        """判断路径是否命中指定候选集合。

        该插件已经在多个位置使用 `messages[].content` 这种简写；
        这里复用相同匹配语义，避免“普通文本替换”和“代码敏感识别”出现路径判断不一致。
        """
        if not candidates:
            return True
        normalized = path.replace('.[', '[')
        generalized = re.sub(r"\[\d+\]", "[]", normalized)
        for allowed in candidates:
            candidate = allowed.replace('[]', '[0]')
            if normalized == allowed or normalized == candidate or normalized.startswith(candidate + '.'):
                return True
            if generalized == allowed or generalized.startswith(allowed + '.'):
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
        """扫描字符串中的代码类敏感信息并返回命中列表。

        设计目标：
        - 轻量、可解释：仅依赖正则和内置规则，不引入重型运行时依赖；
        - 高确定性优先：默认只收录开发场景常见且误报可控的泄漏模式；
        - 可裁剪：后续若要扩规则，只需增补内置规则表即可。
        """
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
                    "replacement": rule["replacement"],
                })

        if not matches:
            return matches

        # 使用“置信度优先，其次命中长度优先”的重叠去重策略。
        # 这样像 `Bearer sk-...` 这类嵌套命中场景，会保留更确定、更具体的规则，
        # 避免一个长但泛化的规则把另一个高价值规则覆盖掉，或同一片段被重复替换。
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

    def _mask_code_secrets(self, text: str, matches: list[dict]) -> str:
        """根据命中结果对文本执行局部替换。"""
        if not matches:
            return text
        parts = []
        cursor = 0
        for match in matches:
            parts.append(text[cursor:match["start"]])
            parts.append(match.get("replacement") or self._code_secret_mask_replacement)
            cursor = match["end"]
        parts.append(text[cursor:])
        return "".join(parts)

    def _apply_text_rules(self, text: str) -> str:
        """依次应用关键词和正则替换规则。"""
        new_text = text
        for source, target in self._keyword_rules:
            if source in new_text:
                new_text = new_text.replace(source, target)
        for pattern, replacement in self._regex_rules:
            new_text = pattern.sub(replacement, new_text)
        return new_text

    def _apply_request_text_guards(self, text: str, path: str) -> tuple[str, bool, dict | None]:
        """对请求字符串统一执行文本替换与代码敏感识别。

        返回值：
        - 处理后的文本
        - 是否发生改写

        说明：
        - 普通关键词/正则替换仍然沿用原先逻辑；
        - 代码敏感识别按独立开关和路径白名单生效；
        - `warn` 只记录日志，不改写请求；`mask` / `block` 则在请求侧统一改写，
          其中 `block` 当前返回安全占位文本，避免把原始敏感值继续转发到上游。
        """
        new_text = self._apply_text_rules(text)
        changed = new_text != text

        if not self._enable_code_secret_guard:
            return new_text, changed, None
        if not self._path_matches(path, self._code_secret_paths):
            return new_text, changed, None

        matches = self._scan_code_secrets(new_text)
        if not matches:
            return new_text, changed, None

        summary = ", ".join(f"{m['secret_type']}.{m['subtype']}" for m in matches[:5])
        if self._code_secret_guard_mode == "warn":
            self.logger.warning(f"[data_filter_guard] 请求体命中代码敏感规则(仅告警): {summary}")
            return new_text, changed, None

        if self._code_secret_guard_mode == "block":
            self.logger.warning(f"[data_filter_guard] 请求体命中代码敏感规则，已阻断请求: {summary}")
            blocked_preview = self._mask_code_secrets(new_text, [
                {**m, "replacement": self._code_secret_mask_replacement} for m in matches
            ])
            return new_text, changed, {
                "path": path,
                "summary": summary,
                "preview": blocked_preview,
                "matches": matches,
            }

        self.logger.warning(f"[data_filter_guard] 请求体命中代码敏感规则，已替换敏感片段: {summary}")
        masked = self._mask_code_secrets(new_text, matches)
        return masked, True, None

    def _mask_and_filter(self, value, path: str = ""):
        """递归处理任意 JSON 风格数据。"""
        if isinstance(value, dict):
            changed = False
            result = {}
            for raw_key, raw_val in value.items():
                key = str(raw_key)
                current_path = f"{path}.{key}" if path else key
                if self._normalize_key(key) in self._sensitive_fields:
                    result[raw_key] = self._redact_replacement
                    changed = True
                    continue
                new_val, sub_changed, blocked = self._mask_and_filter(raw_val, current_path)
                if blocked is not None:
                    return value, changed, blocked
                result[raw_key] = new_val
                changed = changed or sub_changed
            return result, changed, None

        if isinstance(value, list):
            changed = False
            result = []
            total = len(value)
            for idx, item in enumerate(value):
                if self._is_top_level_messages_list(path) and not self._should_scan_message_item(path, idx, total):
                    result.append(item)
                    continue
                current_path = f"{path}[{idx}]" if path else f"[{idx}]"
                new_item, sub_changed, blocked = self._mask_and_filter(item, current_path)
                if blocked is not None:
                    return value, changed, blocked
                result.append(new_item)
                changed = changed or sub_changed
            return result, changed, None

        if isinstance(value, str):
            if not self._should_filter_text_path(path):
                return value, False, None
            return self._apply_request_text_guards(value, path)

        return value, False, None

    async def on_request(self, request) -> dict | None:
        """请求预处理。"""
        self._reload_config()
        if not self._enabled:
            return None

        new_request, changed, blocked = self._mask_and_filter(request)
        if blocked is not None:
            message = f"请求命中代码敏感规则，已被数据安全插件拦截: {blocked['summary']}"
            return {
                "__akm_action__": "block",
                "status_code": 400,
                "error": message,
                "security_action": "block",
                "security_reason": f"request_code_secret:{blocked['path']}"[:2000],
                "body": json.dumps(
                    {
                        "error": message,
                        "path": blocked["path"],
                        "preview": blocked["preview"],
                    },
                    ensure_ascii=False,
                ),
            }
        if changed:
            self.logger.info("[data_filter_guard] 请求体已执行字段脱敏/关键词过滤")
            return new_request
        return None

    def _extract_response_text(self, response_body: str) -> str:
        """尽量从非流式 JSON 响应中提取可见文本，用于安全扫描。"""
        try:
            data = json.loads(response_body)
        except Exception:
            return response_body

        texts = []
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    message = choice.get("message")
                    if isinstance(message, dict) and isinstance(message.get("content"), str):
                        texts.append(message.get("content", ""))
                    delta = choice.get("delta")
                    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                        texts.append(delta.get("content", ""))
            output = data.get("output")
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    content = item.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and isinstance(part.get("text"), str):
                                texts.append(part.get("text", ""))
        return "\n".join(x for x in texts if x)

    def _make_safe_response_body(self) -> str:
        """构造统一的安全拦截响应体。"""
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": self._response_block_message,
                        }
                    }
                ]
            },
            ensure_ascii=False,
        )

    def _resolve_rule_action(self, pattern: re.Pattern) -> str:
        """根据规则覆盖或全局默认值决定本条命中的处理动作。"""
        return self._response_rule_actions.get(pattern.pattern, self._response_guard_mode)

    def _build_safe_stream_payload(self, api_path: str) -> str:
        """构造客户端侧安全流式返回。"""
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

    def is_stream_guard_active(self) -> bool:
        """判断是否启用了流式响应安全保护。"""
        self._reload_config()
        return self._enabled and self._enable_response_guard and self._enable_stream_response_guard

    def stream_guard_requires_buffering(self) -> bool:
        """判断当前流式响应保护是否必须走整段缓冲。

        只有 `mask` 动作必须缓冲：
        - 流式数据一旦已经发给客户端，就无法安全回收并重写；
        - 因此局部替换只能在拿到完整 SSE 文本后统一处理；
        - `warn` / `block` 则可以基于“已返回 + 新增片段”的滑动窗口做增量扫描。
        """
        self._reload_config()
        if not self.is_stream_guard_active():
            return False
        if self._response_guard_mode == "mask":
            return True
        return any(action == "mask" for action in self._response_rule_actions.values())

    def create_stream_guard_state(self) -> dict:
        """创建流式增量扫描状态。

        状态字段说明：
        - `tail`: 最近一段已发送文本，用于和下一个 chunk 拼接，覆盖跨 chunk 命中场景；
        - `matched_patterns`: 已经处理过的规则集合，避免对同一条规则重复告警/重复阻断；
        - `cache_limit`: `tail` 的最大保留字符数，控制内存与重复扫描成本。
        """
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
                # `mask` 需要在完整响应上做替换；增量扫描分支不直接处理，交由服务端决定回退到缓冲模式。
                return next_state, False, pattern.pattern, "mask_requires_buffer"
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

    async def on_response(self, request, response):
        """响应安全处理。"""
        self._reload_config()
        if not self._enabled or not self._enable_response_guard:
            return None
        if not isinstance(response, dict):
            return None
        if not response.get("ok") or response.get("stream") is True:
            return None

        response_body = response.get("response_body")
        if not isinstance(response_body, str) or not response_body:
            return None

        scan_text = self._extract_response_text(response_body)
        for pattern in self._response_block_patterns:
            if not pattern.search(scan_text):
                continue
            guarded = dict(response)
            guarded["status_code"] = 200
            guarded["security_reason"] = pattern.pattern
            action = self._resolve_rule_action(pattern)

            if action == "warn":
                guarded["error"] = "warned_by_data_filter_guard"
                guarded["security_warned"] = True
                self.logger.warning("[data_filter_guard] 响应命中高风险规则，已标记告警")
                return guarded

            if action == "mask":
                masked_body, changed = self._mask_response_body(response_body)
                if changed:
                    guarded["response_body"] = masked_body
                    guarded["error"] = "masked_by_data_filter_guard"
                    guarded["security_masked"] = True
                    self.logger.warning("[data_filter_guard] 响应命中高风险规则，已局部替换")
                    return guarded

            guarded["error"] = "blocked_by_data_filter_guard"
            guarded["security_blocked"] = True
            guarded["response_body"] = self._make_safe_response_body()
            self.logger.warning("[data_filter_guard] 响应命中高风险规则，已拦截返回")
            return guarded
        return None
