"""请求脱敏插件。

能力边界：
1. 仅处理请求侧，不参与响应改写，也不尝试做可逆还原。
2. 目标是让上游模型永远看不到敏感明文，因此所有命中都替换为稳定占位符。
3. 默认规则集参考 opencode-vibeguard 的思路：覆盖常见 API Key、Token、PII、网络与系统标识，
   但不维护 placeholder 映射，也不依赖客户端本地工具生命周期。

设计取舍：
1. 只在 on_request 生命周期工作，最大限度减少对现有代理链路的侵入。
2. 默认关闭，避免用户在未理解规则前就出现“内容被自动改写”的惊讶感。
3. 对文本路径采用白名单，默认仅扫描 messages/input/instructions，避免误改结构化字段。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re

from akm.key_pool import _get_secret_path
from akm.plugins import PluginBase


DEFAULT_RULE_GROUPS = {
    "llm_keys": [
        {
            "pattern": r"\bsk-proj-[A-Za-z0-9_-]{20,}\b",
            "replacement": "[OPENAI-PROJECT-KEY]",
        },
        {
            "pattern": r"\bsk-[A-Za-z0-9_-]{20,}\b",
            "replacement": "[OPENAI-KEY]",
        },
        {
            "pattern": r"\bsk-ant-[A-Za-z0-9_-]{20,}\b",
            "replacement": "[ANTHROPIC-KEY]",
        },
    ],
    "vcs_tokens": [
        {
            "pattern": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
            "replacement": "[GITHUB-TOKEN]",
        },
        {
            "pattern": r"\bgithub_pat_[0-9A-Za-z_]{20,}\b",
            "replacement": "[GITHUB-PAT]",
        },
        {
            "pattern": r"\bglpat-[0-9A-Za-z_-]{20,}\b",
            "replacement": "[GITLAB-TOKEN]",
        },
    ],
    "cloud_keys": [
        {
            "pattern": r"\bAKIA[0-9A-Z]{16}\b",
            "replacement": "[AWS-ACCESS-KEY]",
        },
        {
            "pattern": r"\bAIza[0-9A-Za-z\-_]{35}\b",
            "replacement": "[GOOGLE-API-KEY]",
        },
        {
            "pattern": r"\bSG\.[0-9A-Za-z\-_]{16,}\.[0-9A-Za-z\-_]{16,}\b",
            "replacement": "[SENDGRID-KEY]",
        },
        {
            "pattern": r"\bkey-[0-9A-Za-z]{32}\b",
            "replacement": "[MAILGUN-KEY]",
        },
    ],
    "chatops_tokens": [
        {
            "pattern": r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b",
            "replacement": "[SLACK-TOKEN]",
        },
        {
            "pattern": r"https://hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+",
            "replacement": "[SLACK-WEBHOOK]",
        },
    ],
    "auth_tokens": [
        {
            "pattern": r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}\b",
            "replacement": "[BEARER-TOKEN]",
        },
        {
            "pattern": r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
            "replacement": "[JWT]",
        },
    ],
    "pii": [
        {
            "pattern": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            "replacement": "[EMAIL]",
        },
        {
            "pattern": r"(?<!\d)1[3-9]\d{9}(?!\d)",
            "replacement": "[CHINA-PHONE]",
        },
        {
            "pattern": r"(?<!\d)\d{17}[\dXx](?!\d)",
            "replacement": "[CHINA-ID]",
        },
    ],
    "network": [
        {
            "pattern": r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
            "replacement": "[UUID]",
        },
        {
            "pattern": r"(?:\d{1,3}\.){3}\d{1,3}",
            "replacement": "[IPV4]",
        },
        {
            "pattern": r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}",
            "replacement": "[MAC]",
        },
    ],
    "system": [
        {
            "pattern": r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----",
            "replacement": "[PRIVATE-KEY]",
        },
        {
            "pattern": r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis):\/\/[^\s]+",
            "replacement": "[CONNECTION-STRING]",
        },
    ],
    "credential_assignments": [
        {
            "pattern": r"(?i)([\"']?(?:api_key|apikey|api-key|x-api-key|authorization|proxy-authorization|password|passwd|pwd|secret|client_secret|token|access_token|refresh_token|id_token|session_token|cookie|set-cookie)[\"']?\s*[:=]\s*[\"'])([^\"'\n]{1,})([\"'])",
            "category": "CREDENTIAL_VALUE",
            "value_group": 2,
        },
    ],
}

DEFAULT_RULE_GROUP_SELECTION = "llm_keys,vcs_tokens,cloud_keys,chatops_tokens,auth_tokens,pii,network,system,credential_assignments"
DEFAULT_REQUEST_TEXT_PATHS = "messages[].content,input,instructions,tools[].function.arguments"
DEFAULT_SENSITIVE_FIELDS = (
    "api_key,apikey,api-key,x-api-key,authorization,proxy-authorization,"
    "password,passwd,pwd,secret,client_secret,private_key,token,access_token,"
    "refresh_token,id_token,session_token,cookie,set-cookie"
)
DEFAULT_PLACEHOLDER_PREFIX = "__AKM_"


class Plugin(PluginBase):
    """请求侧单向脱敏插件。"""

    async def on_load(self):
        """初始化可热更新的规则缓存。

        这里不在 on_load 一次性固化配置，而是在每次请求前 reload，
        这样用户在管理台修改配置后无需重启服务即可生效。
        """
        self._enabled = True
        self._request_text_paths: set[str] = set()
        self._sensitive_fields: set[str] = set()
        self._placeholder_prefix = DEFAULT_PLACEHOLDER_PREFIX
        self._process_keys_case_insensitive = True
        self._keyword_rules: list[tuple[str, str]] = []
        self._regex_rules: list[tuple[re.Pattern, str]] = []
        self._structured_regex_rules: list[dict] = []
        self._hash_secret = self._load_hash_secret()

    def _load_hash_secret(self) -> bytes:
        """加载本地稳定哈希盐。

        默认优先复用 `~/.akm/secret.key`：
        1. 这是当前项目已经存在的本地 secret，生命周期与用户 AKM 环境一致；
        2. 复用它可以避免再引入第二套“本地盐”文件；
        3. 即便用户未显式配置 request_redactor，也能得到对本机稳定、对外部不可预期的占位符哈希。

        回退策略：
        - 若读取失败，则退回到一个进程内固定常量，确保插件仍可工作，
          但这种回退主要用于测试或极端异常场景，不作为首选路径。
        """
        try:
            with open(_get_secret_path(), "rb") as f:
                data = f.read().strip()
            if data:
                return data
        except Exception:
            pass
        return b"akm-request-redactor-fallback-secret"

    def _reload_config(self):
        """按当前插件配置重新构建运行时规则。

        说明：
        1. 关键词规则用于少量高确定性的固定前缀替换。
        2. 正则规则用于覆盖大部分敏感值模式。
        3. 规则顺序固定为“内置规则在前，自定义规则在后”，方便用户做补充覆盖。
        """
        cfg = self.config or {}
        self._enabled = cfg.get("enabled", True) is True
        self._request_text_paths = set(
            self._split_items(
                cfg.get("request_text_paths", DEFAULT_REQUEST_TEXT_PATHS)
                or DEFAULT_REQUEST_TEXT_PATHS
            )
        )
        self._process_keys_case_insensitive = cfg.get("process_keys_case_insensitive", True) is True
        self._placeholder_prefix = str(
            cfg.get("placeholder_prefix", DEFAULT_PLACEHOLDER_PREFIX)
            or DEFAULT_PLACEHOLDER_PREFIX
        )

        raw_fields = cfg.get("sensitive_fields", DEFAULT_SENSITIVE_FIELDS) or DEFAULT_SENSITIVE_FIELDS
        fields = self._split_items(raw_fields)
        if self._process_keys_case_insensitive:
            self._sensitive_fields = {item.lower() for item in fields}
        else:
            self._sensitive_fields = set(fields)

        builtin_enabled = cfg.get("builtin_rules_enabled", True) is True
        group_names = set(
            self._split_items(
                cfg.get("builtin_rule_groups", DEFAULT_RULE_GROUP_SELECTION)
                or DEFAULT_RULE_GROUP_SELECTION
            )
        )
        self._keyword_rules = []
        self._regex_rules = []
        self._structured_regex_rules = []

        if builtin_enabled:
            self._keyword_rules.extend(self._build_builtin_keyword_rules())
            self._regex_rules.extend(self._build_builtin_regex_rules(group_names))
            self._structured_regex_rules.extend(self._build_builtin_structured_regex_rules(group_names))

        self._keyword_rules.extend(
            self._parse_keyword_rules(cfg.get("custom_keyword_rules", "") or "")
        )
        self._regex_rules.extend(
            self._parse_regex_rules(cfg.get("custom_regex_rules", "") or "")
        )

    def _split_items(self, raw: str) -> list[str]:
        """把逗号/换行风格配置拆成条目列表。"""
        items = []
        for line in str(raw).replace("\r", "\n").split("\n"):
            for part in line.split(","):
                item = part.strip()
                if item:
                    items.append(item)
        return items

    def _split_lines(self, raw: str) -> list[str]:
        """只按行拆分配置。

        正则里经常会出现逗号，如果仍按逗号拆分，规则很容易被截断。
        因此自定义 regex 规则统一要求按行配置。
        """
        items = []
        for line in str(raw).replace("\r", "\n").split("\n"):
            item = line.strip()
            if item:
                items.append(item)
        return items

    def _normalize_key(self, key: str) -> str:
        """根据配置决定字段名是否按大小写归一。"""
        return key.lower() if self._process_keys_case_insensitive else key

    def _sanitize_category(self, value: str) -> str:
        """把类别名规整为占位符可安全承载的标识。

        这里兼容两类来源：
        1. 内置规则中的显式标签，如 `[EMAIL]`；
        2. 敏感字段名，如 `authorization` / `x-api-key`。
        """
        raw = str(value or "").strip()
        if raw.startswith("[") and raw.endswith("]") and len(raw) >= 3:
            raw = raw[1:-1]
        raw = raw.upper()
        safe = re.sub(r"[^A-Z0-9_]+", "_", raw)
        safe = re.sub(r"_+", "_", safe).strip("_")
        return safe or "REDACTED"

    def _serialize_sensitive_value(self, value) -> str:
        """把整字段替换场景中的原值序列化为稳定字符串。

        说明：
        1. 对绝大多数凭据字段，原值本来就是字符串；
        2. 若上层误传 dict/list，这里仍生成一个稳定占位符，避免因为类型异常漏掉明文；
        3. 即便字段整体被压缩成一个字符串占位符，也符合当前插件“只保护上游不可见”的目标。
        """
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            except Exception:
                return str(value)
        return str(value)

    def _make_placeholder(self, original: str, category: str) -> str:
        """为命中的明文生成稳定占位符。

        格式：`__AKM_<CATEGORY>_<hash12>__`

        设计取舍：
        1. 不维护会话级映射，也不尝试恢复明文，因此只需要“同值稳定”而不需要可逆；
        2. 使用本地 secret 做 HMAC-SHA256，再截前 12 位十六进制摘要，让相同明文在多次请求中保持一致；
        3. 类别名保留在占位符里，方便模型与人工排障理解“这是一类什么敏感值”。
        """
        normalized = self._sanitize_category(category)
        digest = hmac.new(
            self._hash_secret,
            str(original).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:12]
        prefix = self._placeholder_prefix or DEFAULT_PLACEHOLDER_PREFIX
        return f"{prefix}{normalized}_{digest}__"

    def _build_builtin_keyword_rules(self) -> list[tuple[str, str]]:
        """构建少量固定字符串替换规则。

        这些规则只覆盖非常高确定性的前缀片段，目的不是独立完成识别，
        而是在一些常见 prompt 中先把肉眼可见的危险前缀替换掉，减少漏出概率。
        """
        return [
            ("Bearer sk-", "BEARER_OPENAI_KEY_PREFIX"),
            ("Bearer rk-", "BEARER_OPENAI_KEY_PREFIX"),
        ]

    def _build_builtin_regex_rules(self, enabled_groups: set[str]) -> list[tuple[re.Pattern, str]]:
        """从内置分组中编译默认正则规则。"""
        rules: list[tuple[re.Pattern, str]] = []
        selected = enabled_groups or set(DEFAULT_RULE_GROUPS.keys())
        for group_name, group_rules in DEFAULT_RULE_GROUPS.items():
            if group_name not in selected:
                continue
            for item in group_rules:
                if "replacement" not in item:
                    continue
                try:
                    rules.append((re.compile(item["pattern"]), self._sanitize_category(item["replacement"])))
                except re.error as exc:
                    self.logger.warning(
                        f"[request_redactor] 忽略非法内置规则({group_name}): {item['pattern']} ({exc})"
                    )
        return rules

    def _build_builtin_structured_regex_rules(self, enabled_groups: set[str]) -> list[dict]:
        """构建需要“只替换值、不替换整段键值结构”的内置规则。

        这里专门覆盖“普通文本里的 JSON / 配置片段”场景，例如：
        1. `{ "api_key": "123123123" }`
        2. `authorization: "Bearer xxx"`

        如果仍按普通正则整段替换，会把键名也一并吞掉，不利于模型保留
        “这里原本是一个 api_key 字段”的上下文语义。因此这里改成只替换 value_group。
        """
        rules: list[dict] = []
        selected = enabled_groups or set(DEFAULT_RULE_GROUPS.keys())
        for group_name, group_rules in DEFAULT_RULE_GROUPS.items():
            if group_name not in selected:
                continue
            for item in group_rules:
                if "value_group" not in item:
                    continue
                try:
                    rules.append({
                        "pattern": re.compile(item["pattern"]),
                        "category": self._sanitize_category(item.get("category", "REDACTED")),
                        "value_group": int(item["value_group"]),
                    })
                except re.error as exc:
                    self.logger.warning(
                        f"[request_redactor] 忽略非法结构化规则({group_name}): {item['pattern']} ({exc})"
                    )
        return rules

    def _parse_keyword_rules(self, raw: str) -> list[tuple[str, str]]:
        """解析用户自定义关键词规则。

        配置格式保持兼容：`keyword=>replacement`。
        但运行时不再直接把右值当最终输出，而是把它当作类别标签，
        最终产出稳定占位符，既保留语义，又避免把真正的“固定掩码文本”写死进上下文。
        """
        rules = []
        for line in self._split_lines(raw):
            if "=>" not in line:
                continue
            source, target = line.split("=>", 1)
            source = source.strip()
            target = target.strip()
            if source:
                rules.append((source, self._sanitize_category(target or "REDACTED")))
        return rules

    def _parse_regex_rules(self, raw: str) -> list[tuple[re.Pattern, str]]:
        """解析用户自定义正则规则。"""
        rules = []
        for line in self._split_lines(raw):
            if "=>" not in line:
                continue
            pattern_text, replacement = line.split("=>", 1)
            pattern_text = pattern_text.strip()
            replacement = self._sanitize_category(replacement.strip() or "REDACTED")
            if not pattern_text:
                continue
            try:
                rules.append((re.compile(pattern_text), replacement))
            except re.error as exc:
                self.logger.warning(
                    f"[request_redactor] 忽略非法自定义正则: {pattern_text} ({exc})"
                )
        return rules

    def _path_matches(self, path: str, candidates: set[str]) -> bool:
        """判断字符串路径是否命中白名单。

        兼容 `messages[].content` 这种简写形式，统一映射到数组首元素路径判断，
        从而避免每个索引位都要单独枚举配置。

        注意：
        1. 仅匹配 `candidate + '.'` 不够，因为像 OpenCode / Responses 这类客户端
           可能把内容发成 block 数组，实际路径会是 `messages[0].content[0].text`；
        2. 这类路径在 `content` 后面紧跟的是 `[`，不是 `.`；
        3. 因此这里需要同时接受“子属性继续展开”和“子数组继续展开”两种形式，
           否则就会出现纯字符串 content 能脱敏、数组块 content 却直接漏掉的情况。
        """
        if not candidates:
            return True

        def _canonicalize(candidate_text: str) -> str:
            """把路径中的具体数组下标统一归一化为 []。

            之前的实现会把 `messages[].content` 只映射成 `messages[0].content`，
            导致：
            1. 第 0 条消息能命中；
            2. 第 1/2/... 条消息全部漏掉；
            3. 审计里就会出现“同样结构的 user message，有的脱敏、有的明文”这种现象。

            这里统一把任意 `[数字]` 折叠成 `[]`，让规则真正表达“任意数组元素”。
            """
            text = str(candidate_text or "").replace('.[', '[')
            return re.sub(r"\[\d+\]", "[]", text)

        normalized = _canonicalize(path)
        for allowed in candidates:
            candidate = _canonicalize(allowed)
            if (
                normalized == candidate
                or normalized == candidate
                or normalized.startswith(candidate + '.')
                or normalized.startswith(candidate + '[')
            ):
                return True
        return False

    def _is_schema_path(self, path: str) -> bool:
        """判断当前路径是否位于 JSON Schema / tool schema 子树中。

        最近 Codex 的 `invalid_json_schema` 问题，本质是请求脱敏误改了 schema 定义：
        例如把 `text.format.schema.properties.appId` 下面原本应为对象的定义，
        改成了 `__AKM_APPID_xxx__` 字符串，导致上游直接 400。

        这里采用保守策略：
        1. 对 schema 结构子树完全跳过脱敏；
        2. 这样即便 schema 里出现了 `appId` / `token` / `password` 这类名字，
           也不会把“字段定义对象”误判成真实敏感值；
        3. 这类路径的目标是描述输出结构，不是承载真实 secret，优先保证协议合法性。
        """
        normalized = str(path or "").replace('.[', '[')
        schema_prefixes = (
            "text.format.schema",
            "response_format.schema",
            "response_format.json_schema.schema",
            "tools[].function.parameters",
            "tools[].function.output_schema",
            "tools[].parameters",
        )

        def _canonicalize(candidate_text: str) -> str:
            text = str(candidate_text or "").replace('.[', '[')
            return re.sub(r"\[\d+\]", "[]", text)

        normalized = _canonicalize(normalized)
        for prefix in schema_prefixes:
            candidate = _canonicalize(prefix)
            if (
                normalized == candidate
                or normalized.startswith(candidate + '.')
                or normalized.startswith(candidate + '[')
            ):
                return True
        return False

    def _apply_text_rules(self, text: str) -> tuple[str, bool]:
        """对单段文本执行稳定占位符替换。"""
        updated = text
        changed = False
        for source, category in self._keyword_rules:
            if source in updated:
                updated = updated.replace(source, self._make_placeholder(source, category))
                changed = True
        for item in self._structured_regex_rules:
            pattern = item["pattern"]
            category = item["category"]
            value_group = int(item["value_group"])

            def _replace_value(match):
                groups = list(match.groups())
                original = match.group(value_group)
                groups[value_group - 1] = self._make_placeholder(original, category)
                rebuilt = ""
                last = 0
                for idx in range(1, len(groups) + 1):
                    start, end = match.span(idx)
                    rebuilt += match.group(0)[last:start - match.start(0)]
                    rebuilt += groups[idx - 1]
                    last = end - match.start(0)
                rebuilt += match.group(0)[last:]
                return rebuilt

            replaced = pattern.sub(_replace_value, updated)
            if replaced != updated:
                updated = replaced
                changed = True
        for pattern, category in self._regex_rules:
            replaced = pattern.sub(lambda m: self._make_placeholder(m.group(0), category), updated)
            if replaced != updated:
                updated = replaced
                changed = True
        return updated, changed

    def _redact_request(self, value, path: str = ""):
        """递归处理 JSON 风格请求体。

        返回值：
        1. 新值
        2. 当前子树是否发生改写

        这里只处理 dict / list / str，其他标量原样透传。
        """
        if isinstance(value, dict):
            changed = False
            result = {}
            for raw_key, raw_val in value.items():
                key = str(raw_key)
                current_path = f"{path}.{key}" if path else key
                if self._is_schema_path(current_path):
                    result[raw_key] = raw_val
                    continue
                if self._normalize_key(key) in self._sensitive_fields:
                    result[raw_key] = self._make_placeholder(
                        self._serialize_sensitive_value(raw_val),
                        key,
                    )
                    changed = True
                    continue
                new_val, sub_changed = self._redact_request(raw_val, current_path)
                result[raw_key] = new_val
                changed = changed or sub_changed
            return result, changed

        if isinstance(value, list):
            changed = False
            result = []
            for idx, item in enumerate(value):
                current_path = f"{path}[{idx}]" if path else f"[{idx}]"
                new_item, sub_changed = self._redact_request(item, current_path)
                result.append(new_item)
                changed = changed or sub_changed
            return result, changed

        if isinstance(value, str):
            if self._is_schema_path(path):
                return value, False
            if not self._path_matches(path, self._request_text_paths):
                return value, False
            return self._apply_text_rules(value)

        return value, False

    async def on_request(self, request) -> dict | None:
        """请求预处理：把即将发往上游的敏感明文替换为固定掩码。"""
        self._reload_config()
        if not self._enabled:
            return None

        new_request, changed = self._redact_request(request)
        if not changed:
            return None

        self.logger.info("[request_redactor] 请求体已执行稳定占位符脱敏")
        return new_request
