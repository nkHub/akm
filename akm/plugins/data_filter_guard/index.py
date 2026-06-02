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


class Plugin(PluginBase):
    """数据安全插件。"""

    async def on_load(self):
        """初始化内部缓存。"""
        self._sensitive_fields = set()
        self._keyword_rules = []
        self._regex_rules = []
        self._request_text_paths = set()
        self._response_block_patterns = []

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
        self._enabled = cfg.get("enabled", True) is True
        self._enable_response_guard = cfg.get("enable_response_guard", True) is True
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
            self._split_items(cfg.get("response_block_patterns", "") or "")
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
        for allowed in self._request_text_paths:
            candidate = allowed.replace('[]', '[0]')
            if normalized == allowed or normalized == candidate or normalized.startswith(candidate + '.'):
                return True
        return False

    def _apply_text_rules(self, text: str) -> str:
        """依次应用关键词和正则替换规则。"""
        new_text = text
        for source, target in self._keyword_rules:
            if source in new_text:
                new_text = new_text.replace(source, target)
        for pattern, replacement in self._regex_rules:
            new_text = pattern.sub(replacement, new_text)
        return new_text

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
                new_val, sub_changed = self._mask_and_filter(raw_val, current_path)
                result[raw_key] = new_val
                changed = changed or sub_changed
            return result, changed

        if isinstance(value, list):
            changed = False
            result = []
            for idx, item in enumerate(value):
                current_path = f"{path}[{idx}]" if path else f"[{idx}]"
                new_item, sub_changed = self._mask_and_filter(item, current_path)
                result.append(new_item)
                changed = changed or sub_changed
            return result, changed

        if isinstance(value, str):
            if not self._should_filter_text_path(path):
                return value, False
            new_text = self._apply_text_rules(value)
            return new_text, new_text != value

        return value, False

    async def on_request(self, request) -> dict | None:
        """请求预处理。"""
        self._reload_config()
        if not self._enabled:
            return None

        new_request, changed = self._mask_and_filter(request)
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
        return self._enabled and self._enable_response_guard

    def protect_stream_payload(self, api_path: str, payload_text: str) -> tuple[str, bool, str, str]:
        """对客户端侧 SSE 文本做安全处理。"""
        self._reload_config()
        if not self._enabled or not self._enable_response_guard:
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
