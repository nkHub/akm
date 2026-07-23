"""数据安全插件。

能力范围：
1. 请求侧：递归处理敏感字段名、关键词/正则替换。
   敏感字段与文本规则统一使用 ``<AKM-SEC:tag@seq:hash/>`` 可逆占位符，建立反向映射表。
   默认 ``regex_rules`` 已并入原代码敏感分组（LLM Key / VCS / 云厂商 / ChatOps /
   JWT/Bearer / 私钥头 / 数据库连接串 / 凭据赋值等）及邮箱、手机号，命中即可逆脱敏。
2. 响应侧：先反向还原占位符为原始值，再扫描高风险命令/脚本片段做拦截。
   非流式扫描覆盖 Chat / Responses / Anthropic Messages 可见文本。
    3. 流式响应：对 SSE 在 ``delta.content`` / ``reasoning_content`` / ``text`` 等
    字段上做跨帧 content 截流换回（模型按 token 拆开占位符时仍可拼回）；
    纯文本 chunk 仍用前缀半截缓冲。完整占位符在 yield 前还原；流式安全扫描
    同样走字段级滑动窗口（``stream_guard_cache_chars``），边 yield 边扫；
    命中 block/mask 均中断并返回安全载荷（增量路径 mask 退化为 block）。


说明：
- 该插件默认关闭；用户可按需在插件页启用。
- ``messages[].content`` 路径同时覆盖字符串 content 与 content blocks 子路径。
- 默认 ``request_text_paths`` 覆盖对话正文、system/instructions/input，以及 Chat
  续接 ``messages[].tool_calls[].function.arguments``（原代码敏感扫描范围）。
- 响应还原兼容：精确匹配、JSON 转义 ``\\/``、JSON unicode ``\\u003c``/``\\u003e``/
  ``\\u002f``、中文标签规整为 ``t``+指纹，以及模型轻微改写 tag 后按 6 位内容指纹的宽松匹配。
"""

from akm.plugins import PluginBase
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone


# 默认请求文本扫描路径：对话正文 + 系统/指令 + Chat 续接工具参数
# - messages[].content：覆盖字符串 content 与 content[].text / content[].input 等子路径
# - input / instructions：Responses API
# - system：Anthropic Messages
# - messages[].tool_calls[].function.arguments：Chat 客户端续接中的工具参数（密钥常经此泄漏）
DEFAULT_REQUEST_TEXT_PATHS = (
    "messages[].content,input,instructions,system,"
    "messages[].tool_calls[].function.arguments"
)

# 默认可逆正则：邮箱/手机号 + 原代码敏感分组全部规则
# （llm_keys / vcs_tokens / cloud_keys / chatops_tokens / auth_tokens /
#  private_keys / db_urls / credential_assignments）。更具体的 sk-proj / sk-ant
# 写在通用 sk- 之前，避免宽匹配抢占。
DEFAULT_REGEX_RULES = '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}#@email\n(?<!\\d)(1[3-9]\\d{9})(?!\\d)#@手机号\n\\bsk-proj-[A-Za-z0-9_-]{40,}\\b#@openai_project\n\\bsk-ant-[A-Za-z0-9_-]{48,}\\b#@anthropic\n\\bsk-[A-Za-z0-9_-]{24,}\\b#@openai\n\\bgh[pousr]_[A-Za-z0-9]{20,}\\b#@github\n\\bgithub_pat_[0-9A-Za-z_]{40,}\\b#@github_fine_grained\n\\bglpat-[0-9A-Za-z_-]{20,}\\b#@gitlab\n\\bgho_[0-9A-Za-z]{20,}\\b#@github_oauth\n\\bAKIA[0-9A-Z]{16}\\b#@aws_access_key\n\\bAIza[0-9A-Za-z\\-_]{35}\\b#@google\n\\bSG\\.[0-9A-Za-z\\-_]{16,}\\.[0-9A-Za-z\\-_]{16,}\\b#@sendgrid\n\\bkey-[0-9A-Za-z]{32}\\b#@mailgun\n(?i)\\baws_secret[_-]?key\\b\\s*[:=]\\s*(?:[\'\\"][A-Za-z0-9/+=]{40}[\'\\"]|[A-Za-z0-9/+=]{40})#@aws_secret_key\n\\bheroku_api_key\\b\\s*[:=]\\s*(?:[\'\\"]?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}[\'\\"]?)#@heroku\n\\bxox[baprs]-[0-9A-Za-z-]{10,}\\b#@slack\nhttps://hooks\\.slack\\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+#@slack_webhook\n\\beyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\b#@jwt\n(?i)\\bBearer\\s+[A-Za-z0-9._\\-]{16,}\\b#@bearer\n-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----#@private_key\n(?i)\\b(?:postgres(?:ql)?|mysql|mongodb(?:\\+srv)?|redis):\\/\\/[^\\s]+#@database_url\n://[^:\\s]+:[^@\\s]+@[^\\s/$,]+#@password_in_url\n(?i)\\b(?:api_key|apikey|secret_key|secret|password|passwd|pwd|token|access_token|refresh_token|database_url)\\b\\s*[:=]\\s*(?:[\'\\"][^\'\\"]{8,}[\'\\"]|[^\\s{}\\[\\]\\",;<>]{8,})#@assignment'

# 可逆占位符前缀 — ``<AKM-SEC:`` 在自然语言中极低概率出现，检测准确
_REVERSE_PREFIX = "<AKM-SEC:"
_REVERSE_SUFFIX = "/>"
# JSON 序列化常见把 ``/`` 写成 ``\/``，闭合后缀也需兼容
_REVERSE_SUFFIX_JSON_ESC = r"\/>"
# 部分上游/SDK 会把 ``<`` ``>`` ``/`` 编成 JSON unicode 转义，字面扫描不到 ``<AKM-SEC:``
# 例：``\u003cAKM-SEC:tag@1:abcdef/\u003e`` 或 ``\u003cAKM-SEC:tag@1:abcdef\u002f\u003e``
_REVERSE_PREFIX_JSON_U = "\\u003cAKM-SEC:"
# 宽松匹配：兼容空白、可选 JSON 转义斜杠、可选结尾 ``>`` 前空白
_PLACEHOLDER_LOOSE_RE = re.compile(
    r"<AKM-SEC:([A-Za-z0-9_.-]{1,48})@(\d+):([0-9a-fA-F]{6})\s*\\?/?>"
)
# 仅解码占位符相关字符的 ``\u00XX``（大小写 hex），避免全文 unicode_escape 破坏其它内容
_JSON_U_LT_RE = re.compile(r"\\u003[cC]")
_JSON_U_GT_RE = re.compile(r"\\u003[eE]")
_JSON_U_SLASH_RE = re.compile(r"\\u002[fF]")
# 流式半截 ``\\u`` + 0–3 位 hex（完整 ``\\u003c`` 为 4 位 hex，不匹配）
_JSON_U_INCOMPLETE_TAIL_RE = re.compile(r"\\u[0-9a-fA-F]{0,3}$")
# SSE JSON 中参与 content 级截流换回 / 流式安全扫描的字符串字段（值为 str 时才处理）
_SSE_CONTENT_FIELD_KEYS = frozenset({
    "content",
    "reasoning_content",
    "text",
    "thinking",
})


class Plugin(PluginBase):
    """数据安全插件 — 可逆占位符 + 响应安全拦截。"""

    # 换回诊断日志文件：打包 App 下 uvicorn 仅 warning，info 不会进控制台，
    # 因此把换回链路单独落到 ~/.akm，方便本地 tail 排查。
    _DIAG_LOG_PATH = os.path.expanduser("~/.akm/data_filter_guard.log")
    _file_logger_ready = False

    # ── 占位符/反向映射 ──────────────────────────────────────

    def _ensure_diag_file_logger(self) -> None:
        """给插件 logger 挂载本地文件 Handler（幂等）。"""
        if self._file_logger_ready:
            return
        log = self.logger
        if log is None:
            return
        try:
            os.makedirs(os.path.dirname(self._DIAG_LOG_PATH), exist_ok=True)
            # 避免重复挂载同一文件
            for h in list(log.handlers):
                if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == self._DIAG_LOG_PATH:
                    self._file_logger_ready = True
                    return
            handler = logging.FileHandler(self._DIAG_LOG_PATH, encoding="utf-8")
            handler.setLevel(logging.INFO)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            # 打包环境 root 可能是 WARNING，插件自身拉到 INFO 才能写出换回诊断
            log.setLevel(logging.INFO)
            log.addHandler(handler)
            self._file_logger_ready = True
            log.info(
                "[data_filter_guard] 换回诊断日志已启用: %s",
                self._DIAG_LOG_PATH,
            )
        except Exception:
            # 文件日志失败不影响主链路
            pass

    def _diag(self, level: str, msg: str, *args) -> None:
        """写换回诊断：同时走 logger 与直接 append 文件（双保险）。"""
        self._ensure_diag_file_logger()
        text = msg % args if args else msg
        try:
            log = self.logger
            if log is not None:
                if level == "warning":
                    log.warning(text)
                else:
                    log.info(text)
        except Exception:
            pass
        # 直接落盘：不依赖 handler 是否被 uvicorn 清掉
        try:
            os.makedirs(os.path.dirname(self._DIAG_LOG_PATH), exist_ok=True)
            ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
            with open(self._DIAG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{ts} {level.upper()} {text}\n")
        except Exception:
            pass

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
    def _normalize_json_placeholder_escapes(text: str) -> str:
        """把响应中的 JSON unicode 转义占位符字符还原为字面 ``<>/``。

        仅处理 ``\\u003c`` / ``\\u003e`` / ``\\u002f``（大小写 hex），不做全文
        unicode_escape，避免误伤其它 ``\\uXXXX`` 内容。
        """
        if not text or "\\u" not in text:
            return text
        out = _JSON_U_LT_RE.sub("<", text)
        out = _JSON_U_GT_RE.sub(">", out)
        out = _JSON_U_SLASH_RE.sub("/", out)
        return out

    @staticmethod
    def _text_has_placeholder_anchor(text: str) -> bool:
        """是否含可逆占位符锚点（字面 ``<AKM-SEC:`` 或 JSON ``\\u003cAKM-SEC:``）。"""
        if not text:
            return False
        if _REVERSE_PREFIX in text:
            return True
        # ``\\u003cAKM-SEC:``（hex 大小写由正则覆盖）
        return bool(_JSON_U_LT_RE.search(text) and "AKM-SEC:" in text)

    @staticmethod
    def _placeholder_variants(placeholder: str) -> list[str]:
        """生成占位符在响应文本中可能出现的变体（含 JSON 转义）。"""
        variants = [placeholder]
        # 标准 JSON 对 ``/`` 的可选转义：``/>`` → ``\/>``
        if "/" in placeholder:
            escaped = placeholder.replace("/", r"\/")
            if escaped != placeholder:
                variants.append(escaped)
        # JSON unicode 转义 ``<`` / ``>`` / ``/``（部分 SDK 默认 escapeHTML）
        # 例：``\u003cAKM-SEC:tag@1:abcdef/\u003e``、``\u003c...\u002f\u003e``
        u_lt = placeholder.replace("<", "\\u003c").replace(">", "\\u003e")
        if u_lt != placeholder:
            variants.append(u_lt)
            variants.append(u_lt.replace("/", "\\u002f"))
            # 混合：只转义 ``<`` ``>``，``/`` 仍字面或 ``\/``
            variants.append(u_lt.replace("/", r"\/"))
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

    def _reverse_map_summary(self, reverse_map: dict | None) -> str:
        """生成 reverse_map 诊断摘要（只含占位符与原文长度，不落敏感明文）。"""
        if not isinstance(reverse_map, dict) or not reverse_map:
            return "empty"
        items = []
        for placeholder, original in list(reverse_map.items())[:8]:
            items.append(f"{placeholder}→len={len(str(original or ''))}")
        more = "" if len(reverse_map) <= 8 else f" …(+{len(reverse_map) - 8})"
        return f"count={len(reverse_map)} [{'; '.join(items)}{more}]"

    def _reverse_replace(
        self,
        text: str,
        reverse_map: dict | None = None,
        *,
        log: bool = True,
    ) -> tuple[str, bool]:
        """扫描 ``<AKM-SEC:`` 前缀做反向替换。可传入请求级 map 支持并发隔离。

        兼容四类回显形态：
        1. 精确占位符；
        2. JSON 转义斜杠 ``\\/``；
        3. JSON unicode 转义 ``\\u003c`` / ``\\u003e`` / ``\\u002f``（部分上游 escapeHTML）；
        4. 模型轻微改写 tag/空白后，仍带相同 6 位指纹的宽松匹配。

        ``log=False`` 用于流式中间片段：避免每个无前缀 chunk 刷诊断日志。
        """
        rmap = reverse_map if reverse_map is not None else self._reverse_map
        # 先把 ``\\u003cAKM-SEC:...`` 规范成字面 ``<AKM-SEC:...``，后续路径统一
        if text:
            normalized = self._normalize_json_placeholder_escapes(text)
            text = normalized
        has_prefix = self._text_has_placeholder_anchor(text)
        if not rmap:
            if has_prefix and log:
                self._diag(
                    "warning",
                    "[data_filter_guard] 换回跳过: reverse_map 为空，但文本含 %s 前缀 body_len=%s",
                    _REVERSE_PREFIX,
                    len(text),
                )
            return text, False
        if not has_prefix:
            if log:
                self._diag(
                    "info",
                    "[data_filter_guard] 换回跳过: 文本无占位符前缀 map=%s body_len=%s",
                    self._reverse_map_summary(rmap),
                    len(text or ""),
                )
            return text, False

        changed = False
        exact_hits = 0
        for placeholder, original in rmap.items():
            for variant in self._placeholder_variants(placeholder):
                if variant in text:
                    text = text.replace(variant, original)
                    changed = True
                    exact_hits += 1
            # normalize 之后字面 key 是主路径；再扫一遍防 residual unicode 变体
            # （variants 已含 unicode 形态，此处仅保证 normalize 后字面能命中）

        # 宽松还原：按指纹从 map 反查；仅在精确/转义变体都未命中时启用
        loose_hits = 0
        loose_misses = 0
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
                nonlocal changed, loose_hits, loose_misses
                fprint = match.group(3).lower()
                original = fp_index.get(fprint)
                if original is None:
                    loose_misses += 1
                    return match.group(0)
                changed = True
                loose_hits += 1
                return original

            text = _PLACEHOLDER_LOOSE_RE.sub(_loose_repl, text)

        remaining = text.count(_REVERSE_PREFIX) if text else 0
        if log:
            self._diag(
                "info",
                "[data_filter_guard] 换回结果: exact=%s loose_hit=%s loose_miss=%s remaining_prefix=%s "
                "changed=%s map=%s body_len=%s",
                exact_hits,
                loose_hits,
                loose_misses,
                remaining,
                changed,
                self._reverse_map_summary(rmap),
                len(text or ""),
            )
            if remaining > 0:
                # 截取首个残留前缀附近片段，便于对照模型改写形态
                idx = text.find(_REVERSE_PREFIX)
                snippet = text[max(0, idx - 16) : idx + 96].replace("\n", "\\n")
                self._diag(
                    "warning",
                    "[data_filter_guard] 换回残留占位符: snippet=%r",
                    snippet,
                )
        return text, changed

    def is_reverse_map_active(self, reverse_map: dict | None = None) -> bool:
        """判断是否有反向映射表需要还原。"""
        rmap = reverse_map if reverse_map is not None else self._reverse_map
        return bool(rmap)

    def reverse_stream_state(self) -> dict:
        """创建流式还原缓冲状态。

        - ``pending``：纯文本路径的半截前缀缓冲
        - ``line_buf``：不完整 SSE 行
        - ``content_bufs``：各 content 字段跨帧字符缓冲（字段级截流）
        - ``sse_mode``：是否已进入 SSE 解析路径（flush 时决定是否合成 data 帧）
        """
        return {
            "pending": "",
            "line_buf": "",
            "content_bufs": {},
            "sse_mode": False,
        }

    def reverse_stream_flush(self, state: dict, reverse_map: dict | None = None) -> str:
        """强制刷新流式还原缓冲（流结束时调用）。"""
        rmap = reverse_map if reverse_map is not None else self._reverse_map
        parts: list[str] = []

        # 1) 未完成的 SSE 行按整行处理（可能继续写入 content_bufs）
        line_buf = state.get("line_buf") or ""
        if line_buf:
            state["line_buf"] = ""
            if rmap:
                parts.append(self._reverse_sse_line(line_buf.rstrip("\r"), state, rmap))
            else:
                parts.append(line_buf)

        if not rmap:
            state["pending"] = ""
            state["content_bufs"] = {}
            return "".join(parts)

        # 2) content 字段级残留：换回后，SSE 模式合成最小 delta 帧，避免裸文本破坏 SSE
        content_bufs = state.get("content_bufs") or {}
        residual_contents: list[str] = []
        for field_key, buf in list(content_bufs.items()):
            if not buf:
                continue
            restored, _ = self._reverse_replace(buf, reverse_map=rmap, log=True)
            if restored:
                residual_contents.append(restored)
            content_bufs[field_key] = ""
        state["content_bufs"] = {}
        if residual_contents:
            joined = "".join(residual_contents)
            self._diag(
                "info",
                "[data_filter_guard] 流式换回 flush content_buf: len=%s map=%s sse=%s",
                len(joined),
                self._reverse_map_summary(rmap),
                bool(state.get("sse_mode")),
            )
            if state.get("sse_mode"):
                payload = json.dumps(
                    {"choices": [{"index": 0, "delta": {"content": joined}}]},
                    ensure_ascii=False,
                )
                parts.append(f"data: {payload}\n\n")
            else:
                parts.append(joined)

        # 3) 纯文本 pending
        pending = state.get("pending", "")
        if pending:
            self._diag(
                "info",
                "[data_filter_guard] 流式换回 flush: pending_len=%s map=%s",
                len(pending),
                self._reverse_map_summary(rmap),
            )
            output, _ = self._reverse_replace(pending, reverse_map=rmap, log=True)
            state["pending"] = ""
            if output:
                parts.append(output)
        else:
            state["pending"] = ""

        return "".join(parts)

    def _max_placeholder_pending(self, reverse_map: dict) -> int:
        """未闭合占位符允许缓冲的最大长度；超出则视为假阳性前缀。"""
        # 真实占位符形如 <AKM-SEC:tag@seq:hash/>，通常远小于 128；
        # JSON 转义会多若干反斜杠，这里按 map 内最长 key 与下限取 max。
        longest = max((len(k) for k in reverse_map), default=0)
        return max(256, longest + 16)

    def _content_hold_threshold(self, reverse_map: dict) -> int:
        """content 字段「短/长」分界：与典型占位符长度对齐。

        短于该阈值时按「是否以 ``<`` 开头或结尾」决定是否截流；
        达到或超过则先换回再截可能未闭合的尾部。
        """
        longest = max((len(k) for k in reverse_map), default=0)
        return max(longest + 8, 32)

    @staticmethod
    def _suffix_overlap_len(text: str, prefix: str) -> int:
        """计算 ``text`` 尾部与 ``prefix`` 头部的最长重叠长度。

        用于 SSE 把 ``<AKM-SEC:`` 切到两个 chunk 时，保留可能成为完整前缀的尾部。
        例：text 以 ``<AKM`` 结尾 → 返回 5，下一 chunk 以 ``-SEC:...`` 开头时可拼回。
        """
        if not text or not prefix:
            return 0
        max_n = min(len(text), len(prefix) - 1)
        for n in range(max_n, 0, -1):
            if text.endswith(prefix[:n]):
                return n
        return 0

    @staticmethod
    def _find_placeholder_close(text: str, start: int) -> tuple[int, int] | None:
        """在 ``start`` 处的占位符前缀之后查找闭合位置。

        优先用宽松正则（兼容 ``/>`` / ``\\/>`` / 仅 ``>`` 以及轻微改写 tag）；
        再回退到字面 ``/>`` / ``\\/>`` 扫描，避免未完整形态时过早放行。

        返回 ``(close_start, close_end)``：close_end 为闭合 ``>`` 之后的下标。
        """
        # 1) 宽松完整占位符：与 _reverse_replace 的 loose 规则对齐
        m = _PLACEHOLDER_LOOSE_RE.match(text, start)
        if m:
            return m.start(), m.end()

        # 2) 字面闭合：完整 ``/>`` 或 JSON 转义 ``\\/>``
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

    @staticmethod
    def _short_content_should_hold(text: str) -> bool:
        """短 content 是否需要截流：以 ``<`` 开头或结尾，或半截前缀 / ``\\u`` 转义。"""
        if not text:
            return False
        # 用户约定：短片段仅在以 < 开头或结尾时才缓存
        if text.startswith("<") or text.endswith("<"):
            return True
        # 整段是 ``<AKM-SEC:`` 的真前缀（如 ``<A``、``<AKM``）
        if _REVERSE_PREFIX.startswith(text):
            return True
        # JSON unicode 半截：``\\u`` / ``\\u0`` / ``\\u00`` / ``\\u003``
        if text.startswith("\\u") or _JSON_U_INCOMPLETE_TAIL_RE.search(text):
            return True
        if text.startswith("\\u003") or text.startswith("\\u003c"):
            return True
        return False

    def _content_tail_hold_len(self, text: str, reverse_map: dict) -> int:
        """长 content 换回后，尾部仍可能被截断的长度（需继续缓存）。"""
        if not text:
            return 0
        # 未闭合的完整前缀
        last_open = text.rfind(_REVERSE_PREFIX)
        if last_open >= 0 and self._find_placeholder_close(text, last_open) is None:
            return len(text) - last_open
        # 尾部与 ``<AKM-SEC:`` 重叠
        overlap = self._suffix_overlap_len(text, _REVERSE_PREFIX)
        if overlap:
            return overlap
        # 以 < 结尾（下一帧可能接 AKM-SEC）
        if text.endswith("<"):
            return 1
        # 半截 ``\\u00..``
        u_tail = _JSON_U_INCOMPLETE_TAIL_RE.search(text)
        if u_tail:
            return len(u_tail.group(0))
        return 0

    def _gate_content_stream(
        self,
        piece: str,
        state: dict,
        reverse_map: dict,
        field_key: str = "content",
    ) -> str | None:
        """content 字段级截流换回。

        规则：
        1. 与已缓存字符拼接后，若长度 **短于** 占位符量级阈值：
           - 仅当以 ``<`` **开头或结尾**（或半截前缀 / ``\\u``）时截流，本帧不输出该字段；
           - 否则直接换回并放行。
        2. 达到或超过阈值：先整段换回，再截可能未闭合的尾部继续缓存，安全前缀立刻放行。

        返回：
        - ``str``：本帧应写入 JSON 字段的文本（可为 ``""``）
        - ``None``：本帧该字段应置空（内容仍在 ``content_bufs`` 中）
        """
        bufs: dict = state.setdefault("content_bufs", {})
        prev = bufs.get(field_key, "") or ""
        full = prev + (piece if piece is not None else "")
        if not full:
            bufs[field_key] = ""
            return ""

        full = self._normalize_json_placeholder_escapes(full)
        threshold = self._content_hold_threshold(reverse_map)
        is_short = len(full) < threshold

        # 短/长共用：先换回，再按尾部是否可能截断决定截流
        restored, changed = self._reverse_replace(
            full, reverse_map=reverse_map, log=not is_short
        )
        hold = self._content_tail_hold_len(restored, reverse_map)
        max_hold = self._max_placeholder_pending(reverse_map)

        if hold > max_hold:
            # 假阳性超长：整段放行（已换回）
            bufs[field_key] = ""
            self._diag(
                "warning",
                "[data_filter_guard] content 截流: 尾部超限放行 field=%s hold=%s",
                field_key,
                hold,
            )
            return restored

        if hold > 0:
            if hold >= len(restored):
                # 整段仍是未闭合前缀：继续截流，本帧 content 置空
                bufs[field_key] = restored
                return None
            bufs[field_key] = restored[-hold:]
            return restored[:-hold]

        # 换回后已干净
        if is_short and (not changed) and self._short_content_should_hold(full):
            # 短片段、未命中 map，但以 < 开头或结尾 / 半截前缀：截流等下一帧
            bufs[field_key] = full
            return None

        bufs[field_key] = ""
        return restored

    def _rewrite_json_content_fields(self, obj, state: dict, reverse_map: dict) -> bool:
        """递归改写 SSE JSON 中的 content 类字符串字段。返回是否有改动。"""
        changed = False
        if isinstance(obj, dict):
            for key, value in list(obj.items()):
                # Responses: {"type":"response.output_text.delta","delta":"..." }
                # 仅当 delta 值为 str 时按 content 流处理，避免误伤 delta 对象
                is_content_key = key in _SSE_CONTENT_FIELD_KEYS or (
                    key == "delta" and isinstance(value, str)
                )
                if is_content_key and isinstance(value, str):
                    emitted = self._gate_content_stream(
                        value, state, reverse_map, field_key=key
                    )
                    if emitted is None:
                        if value != "":
                            obj[key] = ""
                            changed = True
                    elif emitted != value:
                        obj[key] = emitted
                        changed = True
                elif isinstance(value, (dict, list)):
                    if self._rewrite_json_content_fields(value, state, reverse_map):
                        changed = True
        elif isinstance(obj, list):
            for item in obj:
                if self._rewrite_json_content_fields(item, state, reverse_map):
                    changed = True
        return changed

    def _reverse_sse_line(self, line: str, state: dict, reverse_map: dict) -> str:
        """处理单行 SSE（不含尾部 ``\\n``）。非 data JSON 原样返回。"""
        if not line.startswith("data:"):
            return line
        payload = line[5:]
        if payload.startswith(" "):
            payload = payload[1:]
        if not payload or payload == "[DONE]":
            return line
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            return line
        if not isinstance(obj, (dict, list)):
            return line
        changed = self._rewrite_json_content_fields(obj, state, reverse_map)
        # 即使本帧 content 被截流置空，也重序列化，保证客户端不收到半截占位符
        if not changed:
            # 仍可能有 content_bufs 在累计；若字段被置空也算 changed
            return line
        try:
            new_payload = json.dumps(obj, ensure_ascii=False)
        except (TypeError, ValueError):
            return line
        return f"data: {new_payload}"

    def _reverse_sse_stream_chunk(self, text: str, state: dict, reverse_map: dict) -> str:
        """按 SSE 行边界拆帧，在 content 字段上做跨帧截流换回。"""
        state["sse_mode"] = True
        state.setdefault("line_buf", "")
        buf = (state.get("line_buf") or "") + (text or "")
        if not buf:
            return ""
        out: list[str] = []
        while True:
            idx = buf.find("\n")
            if idx < 0:
                state["line_buf"] = buf
                break
            line = buf[:idx]
            buf = buf[idx + 1 :]
            # 保留 \\r 语义：处理时去掉，写出时用 \\n 结尾（与上游 \\n 或 \\r\\n 兼容）
            out.append(self._reverse_sse_line(line.rstrip("\r"), state, reverse_map))
            out.append("\n")
        return "".join(out)

    @staticmethod
    def _looks_like_sse(text: str, state: dict) -> bool:
        """判断当前 chunk 是否应按 SSE 路径处理。"""
        if state.get("sse_mode") or state.get("line_buf"):
            return True
        if not text:
            return False
        sample = text.lstrip()
        return sample.startswith("data:") or sample.startswith("event:") or "\ndata:" in text

    def reverse_stream_chunk(self, text: str, state: dict, reverse_map: dict | None = None) -> str:
        """流式 chunk 还原：SSE content 字段级截流 + 纯文本前缀半截缓冲。

        SSE 路径（模型按 token 拆 ``delta.content`` 时的主路径）：
        1. 按行解析 ``data: {json}``，只改写 content / reasoning_content / text 等字段；
        2. 短 content（低于占位符长度量级）：仅当以 ``<`` **开头或结尾** 时截流缓存；
        3. 长 content：先换回，再把可能被截断的尾部继续缓存，安全前缀立刻下发；
        4. 换回后的文本写回 JSON 再 yield（截流式放行，非整段憋完）。

        纯文本路径：保留跨 chunk 半截 ``<AKM-SEC:`` / ``\\u003c`` 缓冲（兼容测试与非 SSE）。
        """
        rmap = reverse_map if reverse_map is not None else self._reverse_map
        if not rmap:
            if text and self._text_has_placeholder_anchor(text):
                if not state.get("_logged_empty_map"):
                    state["_logged_empty_map"] = True
                    self._diag(
                        "warning",
                        "[data_filter_guard] 流式换回跳过: reverse_map 为空但 chunk 含占位符前缀 chunk_len=%s",
                        len(text),
                    )
            return text

        if not state.get("_logged_stream_map"):
            state["_logged_stream_map"] = True
            self._diag(
                "info",
                "[data_filter_guard] 流式换回已启用: map=%s first_chunk_len=%s has_prefix=%s sse_hint=%s",
                self._reverse_map_summary(rmap),
                len(text or ""),
                self._text_has_placeholder_anchor(text or ""),
                self._looks_like_sse(text or "", state),
            )

        # ── SSE：字段级 content 截流 ──
        if self._looks_like_sse(text or "", state):
            return self._reverse_sse_stream_chunk(text or "", state, rmap)

        # ── 纯文本：前缀半截缓冲 ──
        return self._reverse_plain_stream_chunk(text or "", state, rmap)

    def _reverse_plain_stream_chunk(self, text: str, state: dict, rmap: dict) -> str:
        """纯文本流式还原：前缀半截缓冲 + 完整占位符反向替换。"""
        state.setdefault("pending", "")
        full = state["pending"] + (text or "")
        if not full:
            return ""
        # 尾部半截 ``\\u`` / ``\\u0`` / ``\\u00`` / ``\\u003``：压 pending，等下一 chunk 补齐
        u_tail = _JSON_U_INCOMPLETE_TAIL_RE.search(full)
        if u_tail:
            incomplete_u = u_tail.group(0)
            safe = full[: -len(incomplete_u)]
            state["pending"] = incomplete_u
            if safe:
                safe = self._normalize_json_placeholder_escapes(safe)
                return self._reverse_replace(safe, reverse_map=rmap, log=False)[0]
            return ""
        # 规范 JSON unicode 转义后，后续只处理字面 ``<AKM-SEC:``
        full = self._normalize_json_placeholder_escapes(full)
        max_pending = self._max_placeholder_pending(rmap)

        last_open = full.rfind(_REVERSE_PREFIX)
        if last_open >= 0:
            close_span = self._find_placeholder_close(full, last_open)
            if close_span is None:
                incomplete = full[last_open:]
                head = full[:last_open]
                output = (
                    self._reverse_replace(head, reverse_map=rmap, log=True)[0]
                    if head
                    else ""
                )
                if len(incomplete) > max_pending:
                    state["pending"] = ""
                    released, _ = self._reverse_replace(incomplete, reverse_map=rmap, log=True)
                    self._diag(
                        "warning",
                        "[data_filter_guard] 流式换回: 未闭合前缀过长，尝试换回后放行 pending_len=%s",
                        len(incomplete),
                    )
                    return output + released
                state["pending"] = incomplete
                return output

            _close_start, closed_end = close_span
            head = full[:closed_end]
            tail = full[closed_end:]
            output, _ = self._reverse_replace(head, reverse_map=rmap, log=True)
            state["pending"] = ""
            if tail:
                return output + self._reverse_plain_stream_chunk(tail, state, rmap)
            return output

        overlap = self._suffix_overlap_len(full, _REVERSE_PREFIX)
        if overlap > 0:
            safe = full[:-overlap]
            state["pending"] = full[-overlap:]
            if safe:
                return self._reverse_replace(safe, reverse_map=rmap, log=False)[0]
            return ""

        if state["pending"]:
            state["pending"] = ""
            return self._reverse_replace(full, reverse_map=rmap, log=False)[0]
        return full

    # ── 配置解析 ────────────────────────────────────────────

    async def on_load(self):
        """初始化内部缓存和反向映射表。"""
        self._sensitive_fields = set()
        self._keyword_rules = []
        self._regex_rules = []
        self._request_text_paths = set()
        self._response_block_patterns = []
        self._reset_reverse_map()
        # 启动时挂上本地诊断文件，避免打包环境 info 日志被丢弃
        self._ensure_diag_file_logger()
        self._diag("info", "[data_filter_guard] on_load 完成，换回诊断文件: %s", self._DIAG_LOG_PATH)

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
        # redact_replacement 已废弃：敏感字段统一走可逆 <AKM-SEC:...>，不再使用固定脱敏文案
        self._keyword_rules = self._parse_keyword_sources(cfg.get("keyword_rules", "") or "")
        # 缺省键时使用内置默认可逆正则（含原代码敏感分组）；显式空串表示用户关闭正则
        raw_regex = cfg.get("regex_rules", DEFAULT_REGEX_RULES)
        if raw_regex is None:
            raw_regex = DEFAULT_REGEX_RULES
        self._regex_rules = self._parse_regex_patterns(raw_regex if raw_regex is not None else "")
        # 留空表示处理所有字符串；缺省键时使用内置默认路径集合（含 tool_calls 参数）
        raw_text_paths = cfg.get("request_text_paths", DEFAULT_REQUEST_TEXT_PATHS)
        self._request_text_paths = set(self._split_items(raw_text_paths if raw_text_paths is not None else ""))
        # 已移除 recent_message_scan_limit：顶层 messages 始终扫描全部历史消息
        self._enabled = cfg.get("enabled", True) is True
        self._enable_response_guard = cfg.get("enable_response_guard", True) is True
        self._enable_stream_response_guard = cfg.get("enable_stream_response_guard", False) is True
        # 流式安全扫描仅用字段级滑动窗口长度（对齐换回截流的「边下发边处理」），
        # 不再做整段 SSE 完整缓冲。
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

    def _apply_request_text_guards(self, text: str) -> tuple[str, bool]:
        """对请求字符串执行关键词/正则可逆替换（由 ``request_text_paths`` 门控）。

        原代码敏感规则已并入默认 ``regex_rules``，统一走可逆占位符 + reverse_map 换回。
        """
        new_text = self._apply_text_rules(text)
        return new_text, new_text != text

    @staticmethod
    def _sensitive_value_to_text(raw_val) -> str:
        """把敏感字段值规整为可写入 reverse_map 的文本。

        字符串原样使用；dict/list 等用 JSON 序列化，失败时退回 ``str()``，
        以便占位符还原时仍能得到可读明文（结构类型在请求体中会变为字符串，
        与旧版整字段替换为固定脱敏文案的行为一致）。
        """
        if isinstance(raw_val, str):
            return raw_val
        try:
            return json.dumps(raw_val, ensure_ascii=False)
        except Exception:
            return str(raw_val)

    def _mask_and_filter(self, value, path: str = ""):
        """递归处理任意 JSON 风格数据，返回 (处理后数据, 是否发生改写)。"""
        if isinstance(value, dict):
            changed = False
            result = {}
            for raw_key, raw_val in value.items():
                key = str(raw_key)
                current_path = f"{path}.{key}" if path else key
                if self._normalize_key(key) in self._sensitive_fields:
                    # 敏感字段名命中 → 整个字段值替换为可逆占位符（与关键词/正则一致）
                    # 非字符串先序列化为文本再映射，保证响应侧能按占位符还原明文
                    original = self._sensitive_value_to_text(raw_val)
                    result[raw_key] = self._make_placeholder(key, original)
                    changed = True
                    continue
                new_val, sub_changed = self._mask_and_filter(raw_val, current_path)
                result[raw_key] = new_val
                changed = changed or sub_changed
            return result, changed

        if isinstance(value, list):
            # 列表节点（含顶层 messages）全部递归扫描，不再截断最近 N 条
            changed = False
            result = []
            for idx, item in enumerate(value):
                current_path = f"{path}[{idx}]" if path else f"[{idx}]"
                new_item, sub_changed = self._mask_and_filter(item, current_path)
                result.append(new_item)
                changed = changed or sub_changed
            return result, changed

        if isinstance(value, str):
            # 关键词/正则（含原代码敏感默认规则）由 request_text_paths 统一门控
            if not self._path_matches(path, self._request_text_paths):
                return value, False
            return self._apply_request_text_guards(value)

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
            rev_copy = dict(self._reverse_map)
            ctx.bag_set("data_filter_guard.reverse_map", rev_copy)
            self._diag(
                "info",
                "[data_filter_guard] 已挂载 reverse_map 到 bag: %s changed=%s",
                self._reverse_map_summary(rev_copy),
                changed,
            )
        elif changed:
            # 正常路径改写后应已写入 map；此处仅作诊断兜底
            self._diag(
                "info",
                "[data_filter_guard] 请求已改写但 reverse_map 为空（异常：应检查占位符生成）",
            )
        if changed:
            self._diag("info", "[data_filter_guard] 请求体已执行脱敏/过滤（可逆占位符）")
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
        map_source = "bag"
        rev_map = ctx.bag_get("data_filter_guard.reverse_map")
        if not isinstance(rev_map, dict) or not rev_map:
            request = ctx.request
            rev_map = request.get("__akm_reverse_map__") if isinstance(request, dict) else None
            map_source = "request.__akm_reverse_map__" if rev_map else "none"
        response_body = response.get("response_body")
        body_is_str = isinstance(response_body, str)
        body_len = len(response_body) if body_is_str else -1
        # 含字面 ``<AKM-SEC:`` 或 JSON ``\\u003cAKM-SEC:``
        has_prefix = body_is_str and self._text_has_placeholder_anchor(response_body or "")
        is_stream = response.get("stream") is True
        self._diag(
            "info",
            "[data_filter_guard] on_response 换回入口: stream=%s map_source=%s map=%s "
            "body_is_str=%s body_len=%s has_prefix=%s",
            is_stream,
            map_source,
            self._reverse_map_summary(rev_map if isinstance(rev_map, dict) else None),
            body_is_str,
            body_len,
            has_prefix,
        )
        if body_is_str and response_body and isinstance(rev_map, dict) and rev_map:
            restored, reverted = self._reverse_replace(response_body, reverse_map=rev_map)
            if reverted:
                response = dict(response)
                response["response_body"] = restored
                self._diag("info", "[data_filter_guard] 响应体已反向还原占位符")
            else:
                self._diag(
                    "warning",
                    "[data_filter_guard] 响应体未还原: 有 map 但 _reverse_replace 未命中 "
                    "(stream=%s has_prefix=%s)",
                    is_stream,
                    has_prefix,
                )
        elif has_prefix and not (isinstance(rev_map, dict) and rev_map):
            self._diag(
                "warning",
                "[data_filter_guard] 响应含占位符但 reverse_map 不可用: map_source=%s stream=%s",
                map_source,
                is_stream,
            )

        # ── 响应安全拦截 ──
        if not self._enable_response_guard:
            return response

        # 流式响应的安全扫描在 inspect_stream_chunk 中边下发边处理
        if is_stream:
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
    #
    # 扫描策略对齐请求侧脱敏的「SSE 字段级」路径：
    # 1. 优先解析 data JSON 中的 content / text 等字段，只对可见文本做规则匹配；
    # 2. 抽离统一的流式匹配方法，规则命中即按 action 处理；
    # 3. 仅增量字段级滑动窗口（stream_guard_cache_chars）；mask 在流式路径退化为 block。

    def _find_rule_match(
        self,
        text: str,
        pattern: re.Pattern,
        matched_patterns: set | None = None,
    ) -> re.Match | None:
        """在文本中查找第一条有效规则命中（跳过本帧已记录的 pattern）。"""
        if not text:
            return None
        if matched_patterns is not None and pattern.pattern in matched_patterns:
            return None
        return pattern.search(text)

    def _match_stream_rules(
        self,
        scan_texts: list[str],
        matched_patterns: set | None = None,
    ) -> tuple[re.Pattern | None, str, re.Match | None]:
        """对多段扫描文本统一做规则匹配。

        返回 ``(命中 pattern, 动作, match 对象)``；未命中时 pattern 为 None、动作为空串。
        """
        for scan_text in scan_texts:
            if not scan_text:
                continue
            for pattern in self._response_block_patterns:
                match = self._find_rule_match(scan_text, pattern, matched_patterns)
                if match is None:
                    continue
                action = self._resolve_rule_action(pattern)
                return pattern, action, match
        return None, "", None

    @staticmethod
    def _is_stream_content_field_key(key: str, value) -> bool:
        """判断 JSON 键值是否属于流式可见 content 字段。"""
        if not isinstance(value, str):
            return False
        # Responses: {"type":"response.output_text.delta","delta":"..."} 仅 str delta 视为 content
        return key in _SSE_CONTENT_FIELD_KEYS or key == "delta"

    def _trim_stream_guard_buf(self, text: str, cache_limit: int) -> str:
        """将字段/纯文本扫描缓冲裁到缓存上限（保留尾部以覆盖跨 chunk 命中）。"""
        if cache_limit <= 0:
            return ""
        if len(text) <= cache_limit:
            return text
        return text[-cache_limit:]

    def _stream_guard_ingest_plain(self, text: str, state: dict) -> list[str]:
        """纯文本路径：与 tail 拼接后返回本帧扫描窗口，并更新 tail。"""
        cache_limit = max(
            0,
            int(state.get("cache_limit", self._stream_guard_cache_chars) or self._stream_guard_cache_chars),
        )
        tail = str(state.get("tail", "") or "")
        scan_text = tail + (text or "")
        state["tail"] = self._trim_stream_guard_buf(scan_text, cache_limit)
        return [scan_text] if scan_text else []

    def _stream_guard_append_field(self, state: dict, field_key: str, piece: str) -> str:
        """向字段级缓冲追加文本，返回本帧用于规则匹配的扫描窗口。"""
        cache_limit = max(
            0,
            int(state.get("cache_limit", self._stream_guard_cache_chars) or self._stream_guard_cache_chars),
        )
        bufs: dict = state.setdefault("content_bufs", {})
        prev = str(bufs.get(field_key, "") or "")
        full = prev + (piece if piece is not None else "")
        bufs[field_key] = self._trim_stream_guard_buf(full, cache_limit)
        return full

    def _stream_guard_ingest_json_fields(self, obj, state: dict, out: list[str]) -> None:
        """递归摄入 JSON content 字段到缓冲，并收集扫描窗口。"""
        if isinstance(obj, dict):
            for key, value in obj.items():
                if self._is_stream_content_field_key(key, value):
                    if value:
                        out.append(self._stream_guard_append_field(state, key, value))
                elif isinstance(value, (dict, list)):
                    self._stream_guard_ingest_json_fields(value, state, out)
        elif isinstance(obj, list):
            for item in obj:
                self._stream_guard_ingest_json_fields(item, state, out)

    def _stream_guard_ingest_sse_line(self, line: str, state: dict, out: list[str]) -> None:
        """处理单行 SSE：解析 data JSON 后做字段级摄入。"""
        if not line.startswith("data:"):
            return
        payload = line[5:]
        if payload.startswith(" "):
            payload = payload[1:]
        if not payload or payload == "[DONE]":
            return
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            # 非 JSON data 行：按纯文本并入 tail，避免漏扫
            out.extend(self._stream_guard_ingest_plain(payload, state))
            return
        if isinstance(obj, (dict, list)):
            self._stream_guard_ingest_json_fields(obj, state, out)
        elif isinstance(obj, str) and obj:
            out.append(self._stream_guard_append_field(state, "content", obj))

    def _stream_guard_ingest_sse(self, text: str, state: dict) -> list[str]:
        """SSE 路径：按行边界拆帧，在 content 字段上做跨帧字段级扫描缓冲。"""
        state["sse_mode"] = True
        state.setdefault("line_buf", "")
        buf = (state.get("line_buf") or "") + (text or "")
        out: list[str] = []
        if not buf:
            return out
        while True:
            idx = buf.find("\n")
            if idx < 0:
                state["line_buf"] = buf
                break
            line = buf[:idx]
            buf = buf[idx + 1 :]
            self._stream_guard_ingest_sse_line(line.rstrip("\r"), state, out)
        return out

    def _stream_guard_ingest_chunk(self, text: str, state: dict) -> list[str]:
        """摄入流式 chunk，返回本帧待扫描文本列表（字段级优先，纯文本兜底）。

        与 ``reverse_stream_chunk`` 相同：能识别 SSE 时只扫 content 类字段，
        避免整段 SSE/JSON 外壳参与匹配；纯文本路径保留 tail 滑动窗口。
        """
        if self._looks_like_sse(text or "", state):
            return self._stream_guard_ingest_sse(text or "", state)
        return self._stream_guard_ingest_plain(text or "", state)

    def is_stream_guard_active(self) -> bool:
        """判断是否启用了流式响应安全保护。"""
        self._reload_config()
        return self._enabled and self._enable_response_guard and self._enable_stream_response_guard

    def create_stream_guard_state(self) -> dict:
        """创建流式增量扫描状态（字段级缓冲 + 纯文本 tail）。

        ``cache_limit`` 来自 ``stream_guard_cache_chars``，与换回截流一样只保留
        跨 chunk 所需的最近字符，不做整段 SSE 完整缓冲。
        """
        self._reload_config()
        return {
            "tail": "",
            "line_buf": "",
            "content_bufs": {},
            "matched_patterns": set(),
            "cache_limit": self._stream_guard_cache_chars,
            "sse_mode": False,
        }

    def inspect_stream_chunk(self, api_path: str, payload_text: str, state: dict) -> tuple[dict, bool, str, str]:
        """基于字段级滑动窗口对流式 chunk 做增量安全扫描。

        匹配方式对齐请求脱敏 / 换回截流的字段级路径：
        - SSE：按行解析 data JSON，只对 content / text / delta 等字段累计扫描；
        - 纯文本：tail + chunk 滑动窗口；
        - 窗口长度由 ``stream_guard_cache_chars`` 约束；
        - 规则命中即按 action 处理（warn 仅记录；block/mask 均中断，mask 退化为 block）。

        返回值：
        - 新状态
        - 是否需要改写当前输出（block/mask 退化为 blocked 时为 True）
        - 命中规则原因
        - 动作（warn / blocked / ""）
        """
        self._reload_config()
        if not self.is_stream_guard_active():
            return state, False, "", ""

        matched_patterns = state.get("matched_patterns")
        if not isinstance(matched_patterns, set):
            matched_patterns = set(matched_patterns or [])
            state["matched_patterns"] = matched_patterns

        cache_limit = max(
            0,
            int(state.get("cache_limit", self._stream_guard_cache_chars) or self._stream_guard_cache_chars),
        )
        state.setdefault("cache_limit", cache_limit)
        state.setdefault("content_bufs", {})
        state.setdefault("line_buf", "")
        state.setdefault("tail", "")
        state.setdefault("sse_mode", False)

        scan_texts = self._stream_guard_ingest_chunk(payload_text or "", state)
        pattern, action, _match = self._match_stream_rules(scan_texts, matched_patterns)
        if pattern is None:
            return state, False, "", ""

        matched_patterns.add(pattern.pattern)
        state["matched_patterns"] = matched_patterns
        if action == "warn":
            return state, False, pattern.pattern, "warn"
        # 流式已边下发边扫：无法回写已透传 chunk 做字段级 mask，mask 统一退化为 block
        return state, True, pattern.pattern, "blocked"
