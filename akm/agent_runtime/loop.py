"""Agent Loop — 多轮工具调用编排器

与 proxy.py 的分工：
- proxy.py：单次 LLM 调用的转发、重试、协议转换、Key 选择
- agent_loop.py：多轮编排，循环调用 LLM → 解析 tool_calls → 执行工具 → 回传结果
"""

import json
import inspect
import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional

from akm.config import load_config

logger = logging.getLogger("akm.agent_runtime.loop")

# 未传 tools 时默认不注入的内置工具（联网搜索、图片生成/编辑等涉及外部服务
# 调用与资源消耗的工具；客户端如需使用须在 tools 中显式声明）。
# 注意：写文件（write/edit/make_dir/delete）与 shell/git 工具**不在**排除之列——
# 它们的可用性由 config 开关（agent_write_tools_enabled / agent_run_shell_enabled /
# agent_git_enabled）控制注册，注册后默认即下发，避免「开关已开但模型仍看不到」。
_DEFAULT_EXCLUDED_TOOLS: frozenset[str] = frozenset(
    {
        "tavily_search",
        "akm_generate_image",
        "akm_edit_image",
    }
)

# Chat 客户端的轻量开关协议：普通工具完全由服务端当前注册状态决定，只有联网
# 搜索和图片生成/编辑工具可由界面显式启用。读图不受 chat 图片开关控制：只有
# agent_read_image_enabled 开启、工具已在服务启动时注册后，才会随普通工具自动注入，
# 这样非视觉模型上传图片时仍可获取描述。与 _DEFAULT_EXCLUDED_TOOLS 分开，避免改变
# 未升级客户端未传 tool_options 时的既有默认行为。
_CHAT_OPTIONAL_TOOLS: frozenset[str] = frozenset(
    {
        "tavily_search",
        "akm_generate_image",
        "akm_edit_image",
    }
)

# 框架工具：除上下文管理外，还包括「向用户澄清提问」的交互工具。
# 与业务工具不同，这些工具不注册到 ToolRegistry（其 handler 需要访问当前
# 运行的 AgentLoop 上下文），而是由 AgentLoop 在 run/run_stream 内部内联
# 拦截处理。默认模式下随内置工具注入；白名单模式下需客户端显式声明。
# 交互澄清工具 akm_ask_user 在默认注入（未传 tools）与白名单注入（传了
# tools）时都可用，只有显式传空数组 [] 时才会被排除。
_AGENT_ASK_USER_TOOL: str = "akm_ask_user"
_AGENT_CONTEXT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": _AGENT_ASK_USER_TOOL,
            "description": (
                "当你需要向用户确认信息才能继续时调用本工具：用户上一条消息"
                "信息不完整、存在歧义、缺少关键参数，或你需要用户做选择时，"
                "把想确认的内容作为 question 传给本工具，等待用户回答后继续。"
                "问题要具体、一次只问一件事，避免让用户费解。"
                "如果不传 options，用户以自由文本回答；传了 options 则用户从"
                "选项中挑选（multiple=true 可多选，否则单选）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "需要向用户确认的问题，需具体明确",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "可选的候选答案列表。不传时用户自由文本回答；"
                            "传入后用户从这些选项中挑选回答"
                        ),
                    },
                    "multiple": {
                        "type": "boolean",
                        "description": (
                            "传了 options 时是否允许用户多选（默认 false 单选）。"
                            "不传 options 时本字段无意义"
                        ),
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "akm_context_status",
            "description": (
                "查询当前对话上下文的 token 占用情况：返回估算的已用 token 数、"
                "上限与剩余空间，用于判断是否需要压缩早期历史。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "akm_compact_context",
            "description": (
                "主动压缩当前对话的早期历史为一段摘要，保留最近约 agent_keep_recent_messages 条消息"
                "（工具调用与配对消息会自动完整保留），用于在上下文接近上限时释放空间，不丢失关键信息。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# Agent Loop 最大迭代次数，防止工具调用无限循环（可通过 config.json 覆盖）


class ToolDef:
    """工具定义，兼容 OpenAI function calling 格式"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler

    def to_openai(self) -> dict:
        """转为 OpenAI tool 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """全局工具注册中心

    Agent Loop 通过 execute() 查找并执行工具。
    """

    _instance: Optional["ToolRegistry"] = None

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    @classmethod
    def instance(cls) -> "ToolRegistry":
        """获取全局单例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置注册表（主要用于测试）"""
        if cls._instance is not None:
            cls._instance._tools.clear()
        cls._instance = None

    def register(self, tool_def: ToolDef) -> None:
        """注册一个工具"""
        if tool_def.name in self._tools:
            logger.warning("[ToolRegistry] 工具名冲突，将覆盖: %s", tool_def.name)
        self._tools[tool_def.name] = tool_def
        logger.info("[ToolRegistry] 注册工具: %s (共 %d 个)", tool_def.name, len(self._tools))

    def unregister(self, name: str) -> None:
        """注销一个工具"""
        self._tools.pop(name, None)
        logger.info("[ToolRegistry] 注销工具: %s (剩余 %d 个)", name, len(self._tools))

    def get_handler(self, name: str) -> Optional[Callable]:
        """获取工具的执行函数"""
        tool = self._tools.get(name)
        return tool.handler if tool else None

    def get_definition(self, name: str) -> Optional[ToolDef]:
        """获取服务端注册的完整工具定义，供覆盖同名客户端声明。"""
        return self._tools.get(name)

    def list_tools(self) -> list[dict]:
        """以 OpenAI 格式返回所有已注册工具的定义"""
        return [t.to_openai() for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)

    async def execute(self, name: str, arguments: dict) -> str:
        """执行指定工具，返回序列化后的结果字符串

        Args:
            name: 工具名称
            arguments: 工具参数（key-value dict）

        Returns:
            工具执行结果序列化后的字符串（JSON 或纯文本）
        """
        handler = self.get_handler(name)
        if handler is None:
            error_msg = f"未找到工具: {name}"
            logger.warning("[ToolRegistry] %s", error_msg)
            return json.dumps({"error": error_msg}, ensure_ascii=False)

        try:
            if inspect.iscoroutinefunction(handler):
                result = await handler(**arguments)
            else:
                result = handler(**arguments)
        except Exception as e:
            error_msg = f"工具执行异常: {e}"
            logger.warning("[ToolRegistry] %s(%s) 异常: %s", name, arguments, error_msg)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(result)


def _extract_tool_calls_from_response(response_body: str) -> list[dict]:
    """从 LLM 非流式响应中提取 tool_calls，兼容 Chat/Responses/Messages 三种协议

    Returns:
        [{"id": "", "name": "", "arguments": "{}"}, ...]
    """
    try:
        data = json.loads(response_body)
    except (TypeError, json.JSONDecodeError):
        return []

    if not isinstance(data, dict):
        return []

    # Chat 格式：choices[0].message.tool_calls
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message", {})
        if isinstance(msg, dict):
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                return [
                    {
                        "id": tc.get("id", ""),
                        "name": (tc.get("function", {}) or {}).get("name", ""),
                        "arguments": (tc.get("function", {}) or {}).get("arguments", "{}"),
                    }
                    for tc in tool_calls
                    if isinstance(tc, dict)
                ]

    # Responses 格式：output[] 中的 function_call
    output = data.get("output")
    if isinstance(output, list):
        result = []
        for item in output:
            if isinstance(item, dict) and item.get("type") == "function_call":
                result.append({
                    "id": item.get("call_id", item.get("id", "")),
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                })
        if result:
            return result

    # Messages 格式：content[] 中的 tool_use 块
    content = data.get("content")
    if isinstance(content, list):
        result = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                result.append({
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                })
        if result:
            return result

    return []


def _extract_text_content(response_body: str) -> str:
    """从 LLM 非流式响应中提取纯文本内容，兼容 Chat/Responses/Messages 三种协议"""
    try:
        data = json.loads(response_body)
    except (TypeError, json.JSONDecodeError):
        return ""

    if not isinstance(data, dict):
        return ""

    # Chat 格式：choices[0].message.content
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message", {})
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                return content

    # Responses 格式：output[].content[].text
    output = data.get("output")
    if isinstance(output, list):
        texts = []
        for item in output:
            if isinstance(item, dict) and item.get("type") == "message":
                for part in (item.get("content") or []):
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        texts.append(part.get("text", ""))
        if texts:
            return "\n".join(texts)

    # Messages 格式：content[].text
    content = data.get("content")
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts)

    return ""


def _extract_reasoning_content(response_body: str) -> str:
    """从 LLM 非流式响应中提取 Chat 格式的推理内容。"""
    try:
        data = json.loads(response_body)
    except (TypeError, json.JSONDecodeError):
        return ""

    if not isinstance(data, dict):
        return ""

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message", {})
    if not isinstance(message, dict):
        return ""
    reasoning = message.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) else ""


def _estimate_text_tokens(text: str) -> int:
    """粗略估算一段文本的 token 数（不含消息结构开销）

    中文等 CJK 字符约 1 token/字符，其余字符约 4 字符 ≈ 1 token。
    仅用于触发上下文压缩的粗粒度判断，不追求精确。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """粗略估算整个对话历史的 token 数

    兼容 content 为字符串或内容块列表（text / image_url 等）两种形态，
    并把 tool_calls 参数 JSON 计入；每条消息额外加少量固定结构开销。
    """
    total = 0
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, str):
            total += _estimate_text_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    total += _estimate_text_tokens(str(block.get("text", "")))
                elif block.get("type") == "image_url":
                    # 图片内容按固定 token 估算，避免 base64 数据撑爆估算
                    total += 1000
                else:
                    total += _estimate_text_tokens(json.dumps(block, ensure_ascii=False))
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    total += _estimate_text_tokens(json.dumps(tc, ensure_ascii=False))
        total += 4  # 每条消息固定结构开销
    return total


class AgentResult:
    """Agent Loop 运行结果"""

    def __init__(
        self,
        ok: bool,
        final_message: dict | None = None,
        messages: list[dict] | None = None,
        turns: int = 0,
        error: str = "",
        usage: dict | None = None,
        compacted: int = 0,
        ask_user: dict | None = None,
    ):
        self.ok = ok
        self.final_message = final_message or {}
        self.messages = messages or []
        self.turns = turns
        self.error = error
        self.usage = usage or {}
        self.compacted = compacted  # 本次运行中上下文被压缩的次数
        self.ask_user = ask_user  # 非空表示 AI 需要向用户澄清提问（见 akm_ask_user）

    def to_dict(self) -> dict:
        """转为可序列化的 dict"""
        return {
            "ok": self.ok,
            "final_message": self.final_message,
            "messages": self.messages,
            "turns": self.turns,
            "error": self.error,
            "usage": self.usage,
            "compacted": self.compacted,
            "ask_user": self.ask_user,
        }


def _extract_cached_tokens(usage) -> tuple[int, int]:
    """从 usage 提取缓存命中/写入 token 数（兼容 Chat / Messages / Responses）。

    统一口径与 server._extract_tokens 一致：
    - 优先 Chat 的 cached_tokens；
    - 其次 Anthropic Messages 的 cache_read_input_tokens；
    - 再退到 prompt_tokens_details / input_tokens_details 里的 cached_tokens。
    返回 (cached_tokens, cache_creation_tokens)。
    """
    if not isinstance(usage, dict):
        return 0, 0
    cached = int(usage.get("cached_tokens", 0) or 0)
    if not cached:
        cached = int(usage.get("cache_read_input_tokens", 0) or 0)
    if not cached:
        for key in ("prompt_tokens_details", "input_tokens_details"):
            details = usage.get(key)
            if isinstance(details, dict):
                cached = int(details.get("cached_tokens", 0) or 0)
                if cached:
                    break
    creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    return cached, creation


class _SSEStreamAccumulator:
    """SSE 流式累加器

    从上游 SSE 增量流中提取可见文本和工具调用。无论上游使用 Chat、
    Responses 还是 Messages 事件，均重建为内部统一的 Chat 响应，避免
    Agent API 向客户端泄露或混杂上游协议帧。
    """

    def __init__(self):
        self._line_buf = ""  # 行缓冲区，处理跨 chunk 边界的 SSE 行
        self._content_parts: list[str] = []  # 文本增量累积
        self._reasoning_parts: list[str] = []  # 推理内容增量累积 (delta.reasoning_content)
        self._pending_reasoning: list[str] = []  # 本段 feed 新到的推理增量，供 drain_reasoning_deltas 取走
        self._tool_calls: dict[int, dict] = {}  # index → 累积后的 tool_call
        self._usage: dict = {}
        self._model = ""

    def drain_reasoning_deltas(self) -> list[str]:
        """取出并清空累积的推理内容增量，供 reasoning_delta 事件实时下发。

        正文增量由 feed() 直接返回，而推理内容（思考过程）不混入正文，
        由调用方在每次 feed 后主动 drain，保证思考先于正文/工具事件流出。
        """
        deltas = self._pending_reasoning
        self._pending_reasoning = []
        return deltas

    def feed(self, text: str) -> list[str]:
        """喂入一段 SSE 文本，返回本段新解析出的可见文本增量。"""
        self._line_buf += text
        return self._extract_lines()

    def finish(self) -> list[str]:
        """处理流结束时未以换行结尾的最后一个 SSE 数据行。"""
        if not self._line_buf:
            return []
        self._line_buf += "\n"
        return self._extract_lines()

    def _extract_lines(self) -> list[str]:
        deltas: list[str] = []
        while "\n" in self._line_buf:
            idx = self._line_buf.index("\n")
            line = self._line_buf[:idx].rstrip("\r")
            self._line_buf = self._line_buf[idx + 1 :]
            line = line.strip()
            if line.startswith("data: "):
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    continue
                try:
                    delta = self._process_chunk(json.loads(data_str))
                    if delta:
                        deltas.append(delta)
                except json.JSONDecodeError:
                    pass
        return deltas

    def _update_usage(self, usage: dict) -> None:
        """兼容三种协议的字段名，且保留上游给出的 total_tokens。"""
        self._usage.update(usage)
        if "input_tokens" in usage and "prompt_tokens" not in usage:
            self._usage["prompt_tokens"] = usage["input_tokens"]
        if "output_tokens" in usage and "completion_tokens" not in usage:
            self._usage["completion_tokens"] = usage["output_tokens"]

    def _tool_call(self, index: int) -> dict:
        if index not in self._tool_calls:
            self._tool_calls[index] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            }
        return self._tool_calls[index]

    def _process_chunk(self, data: dict) -> str:
        """解析单个协议帧并返回其中的可见文本增量。"""
        if not self._model:
            self._model = str(data.get("model", "") or "")

        usage = data.get("usage")
        if isinstance(usage, dict):
            self._update_usage(usage)

        visible_parts: list[str] = []
        for choice in (data.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                continue
            # 文本增量
            content = delta.get("content")
            if isinstance(content, str) and content:
                self._content_parts.append(content)
                visible_parts.append(content)
            # 推理内容增量（thinking/reasoning，如 deepseek-reasoner 的 reasoning_content）
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                self._reasoning_parts.append(reasoning)
                self._pending_reasoning.append(reasoning)
            # 工具调用增量（OpenAI Chat 流式 tool_calls 以 delta 形式分片到达）
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    try:
                        idx = int(tc.get("index", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    cur = self._tool_call(idx)
                    if "id" in tc and tc["id"]:
                        cur["id"] = str(tc["id"])
                    if "type" in tc:
                        cur["type"] = str(tc["type"])
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        if fn.get("name"):
                            cur["function"]["name"] += str(fn["name"])
                        if fn.get("arguments"):
                            cur["function"]["arguments"] += str(fn["arguments"])

        event_type = str(data.get("type", "") or "")
        if event_type == "response.output_text.delta":
            content = data.get("delta")
            if isinstance(content, str) and content:
                self._content_parts.append(content)
                visible_parts.append(content)
        elif event_type == "response.output_item.added":
            item = data.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                try:
                    index = int(data.get("output_index", len(self._tool_calls)) or 0)
                except (TypeError, ValueError):
                    index = len(self._tool_calls)
                cur = self._tool_call(index)
                cur["id"] = str(item.get("call_id", item.get("id", "")) or "")
                cur["function"]["name"] = str(item.get("name", "") or "")
                cur["function"]["arguments"] = str(item.get("arguments", "") or "")
        elif event_type == "response.function_call_arguments.delta":
            try:
                index = int(data.get("output_index", 0) or 0)
            except (TypeError, ValueError):
                index = 0
            cur = self._tool_call(index)
            if data.get("call_id"):
                cur["id"] = str(data["call_id"])
            content = data.get("delta")
            if isinstance(content, str):
                cur["function"]["arguments"] += content
        elif event_type == "content_block_start":
            block = data.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                try:
                    index = int(data.get("index", 0) or 0)
                except (TypeError, ValueError):
                    index = 0
                cur = self._tool_call(index)
                cur["id"] = str(block.get("id", "") or "")
                cur["function"]["name"] = str(block.get("name", "") or "")
                input_data = block.get("input")
                cur["function"]["arguments"] = (
                    json.dumps(input_data, ensure_ascii=False)
                    if input_data else ""
                )
        elif event_type == "content_block_delta":
            delta = data.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                content = delta.get("text")
                if isinstance(content, str) and content:
                    self._content_parts.append(content)
                    visible_parts.append(content)
            elif isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                try:
                    index = int(data.get("index", 0) or 0)
                except (TypeError, ValueError):
                    index = 0
                partial_json = delta.get("partial_json")
                if isinstance(partial_json, str):
                    self._tool_call(index)["function"]["arguments"] += partial_json
        elif event_type == "message_start":
            message = data.get("message")
            if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                self._update_usage(message["usage"])

        return "".join(visible_parts)

    @property
    def prompt_tokens(self) -> int:
        return int(self._usage.get("prompt_tokens", 0) or 0)

    @property
    def completion_tokens(self) -> int:
        return int(self._usage.get("completion_tokens", 0) or 0)

    @property
    def total_tokens(self) -> int:
        total = self._usage.get("total_tokens")
        if total is not None:
            return int(total or 0)
        return self.prompt_tokens + self.completion_tokens

    @property
    def cached_tokens(self) -> int:
        return _extract_cached_tokens(self._usage)[0]

    @property
    def cache_creation_tokens(self) -> int:
        return _extract_cached_tokens(self._usage)[1]

    @property
    def response_body(self) -> str:
        """重建非流式完整响应 JSON，供 _extract_tool_calls_from_response 等复用"""
        sorted_calls = [self._tool_calls[i] for i in sorted(self._tool_calls)]
        reasoning = "".join(self._reasoning_parts) or None
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "".join(self._content_parts) or None,
                            "reasoning_content": reasoning,
                            "tool_calls": sorted_calls if sorted_calls else None,
                        }
                    }
                ],
                "usage": self._usage,
            },
            ensure_ascii=False,
        )


class AgentLoop:
    """Agent Loop 编排器

    职责：
    1. 接收 messages 和 tools 定义
    2. 循环：调用 LLM → 解析 tool_calls → 执行工具 → 回传结果
    3. 达到最大轮次或无 tool_calls 时返回最终结果

    每次 LLM 调用通过 proxy.forward_request 透传，
    自动复用 Key 选择、协议转换、重试等所有现有能力。
    """

    def __init__(
        self,
        http_client,
        plugin_manager=None,
        tool_registry: Optional[ToolRegistry] = None,
        audit_submitter: Optional[Callable[[dict], Awaitable[Any]]] = None,
    ):
        """初始化 AgentLoop

        Args:
            http_client: httpx.AsyncClient 实例
            plugin_manager: PluginManager 实例，传给 forward_request 用
            tool_registry: 工具注册中心，默认使用全局单例
            audit_submitter: 审计日志异步回调，接收 dict 参数写入 DB
        """
        self._http_client = http_client
        self._plugin_manager = plugin_manager
        self._tool_registry = tool_registry or ToolRegistry.instance()
        self._max_turns = max(1, int(load_config().get("agent_max_turns", 100) or 100))
        self._max_context_tokens = max(
             0, int(load_config().get("agent_max_context_tokens", 272000) or 272000)
        )
        self._keep_recent_messages = max(
            2, int(load_config().get("agent_keep_recent_messages", 10) or 10)
        )
        self._context_warning_ratio = max(
            0.0, min(1.0, float(load_config().get("agent_context_warning_ratio", 0.8) or 0.8))
        )
        # 自愈重试：工具调用失败后允许强制模型修正参数再试的最大轮次（0 关闭）
        self._tool_retry_max = max(
            0, int(load_config().get("agent_tool_retry_max_retries", 1) or 0)
        )
        self._max_tool_calls = max(
            1, int(load_config().get("agent_max_tool_calls", 30) or 30)
        )
        # /v1/agent 会话自动落盘开关（默认开启）：流式请求结束时把完整对话历史
        # 保存到 ~/.akm/agent_sessions/，供 akm_load_session / akm_list_sessions
        # 串联回顾使用；设为 false 时保持无状态，不写磁盘。
        self._session_auto_save = bool(load_config().get("agent_session_auto_save", True))
        self._audit_submitter = audit_submitter

    async def _try_audit(
        self,
        result: dict,
        model: str,
        request_body: dict,
        response_body: str,
        status_code: int,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        error: str = "",
        source: str = "chat",
        cached_tokens: int = 0,
        cache_creation_tokens: int = 0,
        client_request_body: dict | list | None | str = None,
    ) -> None:
        """如果注册了审计回调，则写入审计日志。

        来源标记通过 request_headers 的 x-akm-source 字段区分：
        chat（/v1/agent 直接调用）、task（定时任务触发）。

        cached_tokens / cache_creation_tokens 记录上游上下文缓存命中与写入的
        token 数（上游支持自动缓存时可能大于 0）。

        client_request_body 是客户端发来的原始请求体（与 request_body 加工后的
        上游请求体区分，对应 audit_logs.client_request_body 列）。未传时回退
        使用 request_body，保证直连路径与 agent 路径在该列上口径一致。
        """
        if self._audit_submitter is None:
            return
        try:
            from akm.config import load_config

            cfg = load_config()
            save_request_body = bool(cfg.get("log_request_body", False))
            save_response_body = bool(cfg.get("log_response_body", False))

            resp_for_log = ""
            if save_response_body:
                resp_for_log = response_body
                if len(resp_for_log) > 64000:
                    resp_for_log = resp_for_log[:32000] + f"\n...(截断，共 {len(resp_for_log)} 字符)" + resp_for_log[-32000:]
            req_body_for_log = ""
            client_req_for_log = ""
            if save_request_body:
                req_body_for_log = json.dumps(request_body, ensure_ascii=False)
                client_req_src = request_body if client_request_body is None else client_request_body
                if isinstance(client_req_src, str):
                    client_req_for_log = client_req_src
                else:
                    client_req_for_log = json.dumps(client_req_src, ensure_ascii=False, default=str)
            up_resp_for_log = ""
            if save_response_body:
                up_resp_for_log = str(result.get("upstream_response_body_for_log", "") or "") or resp_for_log
                if len(up_resp_for_log) > 64000:
                    up_resp_for_log = up_resp_for_log[:32000] + f"\n...(截断，共 {len(up_resp_for_log)} 字符)" + up_resp_for_log[-32000:]
            attempts_for_log = ""
            raw_attempts = result.get("attempts")
            if raw_attempts:
                try:
                    if isinstance(raw_attempts, str):
                        attempts_for_log = raw_attempts
                    else:
                        attempts_for_log = json.dumps(raw_attempts, ensure_ascii=False, default=str)[:120000]
                except Exception:
                    attempts_for_log = ""
            await self._audit_submitter({
                "provider": str(result.get("provider", "") or ""),
                "key_alias": str(result.get("key_alias", "") or ""),
                "model": model,
                "request_body": req_body_for_log,
                "response_body": resp_for_log,
                "status_code": status_code,
                "latency_ms": int(result.get("latency_ms", 0) or 0),
                "error": error,
                "request_headers": json.dumps({"user-agent": "agent/1.0", "x-akm-source": source}) if save_request_body else "",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cached_tokens": cached_tokens,
                "cache_creation_tokens": cache_creation_tokens,
                "client_request_headers": str(result.get("client_request_headers_for_log", "") or "") if save_request_body else "",
                "client_request_body": client_req_for_log,
                "upstream_request_headers": str(result.get("upstream_headers_for_log", "") or "") if save_request_body else "",
                "upstream_response_body": up_resp_for_log,
                "attempts": attempts_for_log,
            })
        except Exception:
            logger.warning("[AgentLoop] 审计日志写入失败", exc_info=True)

    async def _summarize_history(
        self,
        messages: list[dict],
        model: str,
        api_path: str,
    ) -> str:
        """让 LLM 把一段历史消息压缩成一段简短摘要，供上下文压缩复用。

        复用 forward_request 的 Key 选择 / 协议转换 / 重试链路，用非流式调用，
        不注入任何工具，避免模型自主调用工具。摘要失败（HTTP 错误、空内容等）
        时返回空字符串，由调用方决定降级策略。

        Args:
            messages: 需要被压缩的历史消息（Chat 格式）
            model: 当前主循环使用的模型
            api_path: 上游协议格式（与主循环一致）

        Returns:
            压缩后的摘要文本；失败或为空时返回 ""
        """
        from akm.proxy import forward_request

        # 把历史序列化成易读的对话回放，交给模型总结
        history = json.dumps(messages, ensure_ascii=False, default=str)
        prompt = (
            "下面是一段多轮对话历史，请用简洁的中文要点总结其中已发生的关键内容："
            "用户的核心诉求、已完成的工具调用及其结果、已经得出的结论。"
            "保留后续仍可能需要的细节（如文件路径、数值、已确认的事实）。"
            "直接输出总结，不要复述原文，不要添加额外说明。\n\n"
            f"对话历史：\n{history}"
        )
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个对话摘要助手。"},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        result = await forward_request(
            body,
            self._http_client,
            api_path=api_path,
            plugin_manager=self._plugin_manager,
        )
        status_code = int(result.get("status_code", 0) or 0)
        response_body = str(result.get("body", "") or "")
        if status_code < 200 or status_code >= 300:
            logger.warning("[AgentLoop] 上下文摘要调用失败: HTTP %s", status_code)
            return ""
        summary = _extract_text_content(response_body).strip()
        if not summary:
            logger.warning("[AgentLoop] 上下文摘要调用返回空内容")
            return ""
        return summary

    async def _compact_context(
        self,
        working_messages: list[dict],
        model: str,
        api_path: str,
        force: bool = False,
    ) -> tuple[list[dict], int]:
        """估算上下文 token 数，超过阈值时压缩早期历史。

        保留最近的 agent_keep_recent_messages 条消息（工具消息与其配对的
        assistant tool_calls 整体保留），更早的历史用一次 LLM 摘要替换；
        摘要失败时降级为直接截断。未超阈值或配置为 0（禁用）时原样返回。

        Args:
            working_messages: 当前对话历史（Chat 格式）
            model: 当前主循环使用的模型
            api_path: 上游协议格式（与主循环一致）
            force: True 时跳过阈值判断，强制压缩（供 AI 主动压缩工具使用）

        Returns:
            (压缩后的 messages 列表, 被压缩移除的旧消息条数；未压缩时为 (原列表, 0))
        """
        if self._max_context_tokens <= 0:
            return working_messages, 0
        if not force and _estimate_messages_tokens(working_messages) <= self._max_context_tokens:
            return working_messages, 0

        keep_recent = self._keep_recent_messages

        # 定位保留边界：从尾部向前取 keep_recent 条，但工具消息必须与其
        # 配对的 assistant tool_calls 消息一起保留，避免产生游离 tool 消息。
        split = len(working_messages)
        keep_count = 0
        i = len(working_messages) - 1
        while i >= 0 and keep_count < keep_recent:
            msg = working_messages[i]
            # 遇到 tool 消息时向前找到配对的 assistant（带 tool_calls），整组保留
            if msg.get("role") == "tool":
                j = i
                while j >= 0 and working_messages[j].get("role") != "assistant":
                    j -= 1
                # 保护：若找不到配对 assistant，则最多保留到该 tool 为止
                if j >= 0 and working_messages[j].get("tool_calls"):
                    keep_count += i - j + 1
                    i = j - 1
                    continue
            keep_count += 1
            i -= 1
        split = i + 1

        # 至少保留首条 system（instructions 所在），且必须真的压缩掉一些内容
        if split <= 1 or split >= len(working_messages):
            return working_messages, 0

        old_head = working_messages[:split]
        kept_tail = working_messages[split:]
        summary = ""
        summary_msg: list[dict] = []
        if old_head:
            summary = await self._summarize_history(old_head, model, api_path)
        if summary:
            # 摘要成功：用一条 system 摘要消息替换旧历史（放在保留区之前）
            summary_msg = [{"role": "system", "content": f"以下是对较早对话历史的摘要：\n{summary}"}]
        else:
            # 摘要失败：降级为直接丢弃旧历史，只保留最近的 keep_recent 条
            logger.warning("[AgentLoop] 上下文摘要失败，降级为截断旧历史（丢弃 %d 条）", len(old_head))
        new_messages = summary_msg + kept_tail
        return new_messages, len(old_head)

    async def compact_messages(
        self,
        messages: list[dict],
        *,
        model: str = "",
        api_path: str = "chat/completions",
    ) -> dict:
        """手动压缩一段对话历史，供 HTTP 接口 /v1/agent/compact 调用。

        与自动压缩（run 循环内的阈值触发）不同，这里强制压缩：保留最近的
        agent_keep_recent_messages 条消息，更早的历史交给 LLM 生成摘要替换。
        这是对外公开的只读改造，不修改任何已保存的会话文件（是否回写由
        调用方决定，前端拿到结果后自行替换会话内的历史消息）。

        Args:
            messages: 需要被压缩的对话历史（Chat 格式）
            model: 用于生成摘要的模型；为空时由 forward_request 自动选 Key
            api_path: 上游协议格式（与主循环一致）

        Returns:
            字典：
            - ok: 是否处理成功
            - messages: 压缩后的消息列表（不可压缩时与入参相同）
            - summary: 摘要消息的内容（含 system system 前缀）或 ""
            - removed_count: 被移除的旧消息条数（0 表示未压缩）
            - before_count / after_count: 压缩前后消息条数
            - estimated_tokens: 压缩后上下文的 token 估算值
        """
        if not isinstance(messages, list) or not messages:
            return {"ok": False, "detail": "messages 不能为空"}

        try:
            new_messages, removed = await self._compact_context(
                messages, model, api_path, force=True
            )
        except Exception as exc:
            logger.warning("[AgentLoop] 手动压缩上下文失败", exc_info=True)
            return {"ok": False, "detail": f"压缩上下文失败: {exc}"}

        summary = ""
        if new_messages and new_messages[0].get("role") == "system":
            head_content = str(new_messages[0].get("content", "") or "")
            prefix = "以下是对较早对话历史的摘要："
            if head_content.startswith(prefix):
                summary = head_content[len(prefix):].strip()

        return {
            "ok": True,
            "messages": new_messages,
            "summary": summary,
            "removed_count": removed,
            "before_count": len(messages),
            "after_count": len(new_messages),
            "estimated_tokens": _estimate_messages_tokens(new_messages),
        }

    async def _execute_context_tool(
        self,
        tc_name: str,
        tc_args: dict,
        working_messages: list[dict],
        model: str,
        api_path: str,
        compacted_count: int,
    ) -> tuple[Optional[str], list[dict], int]:
        """处理上下文管理框架工具（akm_context_status / akm_compact_context）。

        这两个工具不注册到 ToolRegistry，由 AgentLoop 在工具执行处内联拦截：
        - akm_context_status：返回当前上下文 token 估算、上限与剩余空间
        - akm_compact_context：强制压缩早期历史，返回压缩结果

        Args:
            tc_name: 工具名
            tc_args: 工具参数（框架工具无参数，保留字段兼容）
            working_messages: 当前对话历史（Chat 格式）
            model: 当前主循环使用的模型
            api_path: 上游协议格式（与主循环一致）
            compacted_count: 本轮已累计的压缩次数

        Returns:
            (工具结果字符串 或 None, 可能被压缩替换后的 messages, 更新后的压缩次数)；
            非框架工具返回 (None, working_messages, compacted_count)，由调用方走 ToolRegistry。
        """
        if tc_name == "akm_context_status":
            estimated = _estimate_messages_tokens(working_messages)
            result = json.dumps({
                "estimated_tokens": estimated,
                "max_tokens": self._max_context_tokens,
                "remaining_tokens": max(0, self._max_context_tokens - estimated),
                "message_count": len(working_messages),
            }, ensure_ascii=False)
            return result, working_messages, compacted_count

        if tc_name == "akm_compact_context":
            new_messages, removed = await self._compact_context(
                working_messages, model, api_path, force=True
            )
            if removed:
                compacted_count += 1
                logger.info(
                    "[AgentLoop] AI 主动压缩上下文（移除 %d 条旧消息）", removed
                )
            result = json.dumps({
                "compacted": bool(removed),
                "removed_messages": removed,
                "estimated_tokens": _estimate_messages_tokens(new_messages),
            }, ensure_ascii=False)
            return result, new_messages, compacted_count

        return None, working_messages, compacted_count

    async def _execute_registered_tool(self, tc_name: str, tc_args: dict, workspace_root: str) -> str:
        """执行注册在 ToolRegistry 中的普通工具，期间注入请求级工作区覆盖。

        文件/写/shell 等工具通过 tools._workspace_root() 读取工作区根目录，
        默认取全局配置 agent_workspace_root。这里在工具执行期间临时把
        请求级的 workspace_root 写入 ContextVar，使 /v1/agent 请求可以按
        请求指定工作区（如 CLI 指向当前目录），执行完立即恢复，避免泄漏
        到并发请求。延迟导入 tools 避免循环依赖（tools 顶层导入本模块的 ToolDef）。
        """
        from akm.agent_runtime.tools import (
            reset_request_workspace_root,
            set_request_workspace_root,
        )

        ws_token = set_request_workspace_root(workspace_root)
        try:
            return await self._tool_registry.execute(tc_name, tc_args)
        finally:
            reset_request_workspace_root(ws_token)

    @staticmethod
    def _tool_error_text(result_text: str) -> str:
        """判断工具结果是否为失败：结果是含非空 error 字段的 JSON 时返回错误文本，否则返回空串。

        工具内部统一用 {"error": "..."} 表示失败（路径越界、开关未启用、命令失败等），
        自愈重试据此识别失败，并把它作为修正提示反馈给模型。
        """
        try:
            data = json.loads(result_text)
        except (TypeError, json.JSONDecodeError):
            return ""
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
        return ""

    async def run(
        self,
        messages: list[dict],
        *,
        model: str = "",
        tools: list[dict] | None = None,
        tool_options: dict | None = None,
        instructions: str = "",
        max_turns: int = 0,
        api_path: str = "chat/completions",
        workspace_root: str = "",
        source: str = "chat",
    ) -> AgentResult:
        """运行 Agent Loop

        Args:
            messages: 对话历史（Chat 格式的 messages 数组）
            model: 指定模型，为空时使用第一个可用 Key 的模型自动选择
            tools: 额外的工具定义列表（OpenAI function calling 格式），
                   与 ToolRegistry 中已注册的工具合并
            instructions: 系统级指令，注入到 messages 首条 system 消息
            max_turns: 最大迭代轮次，传入 0 使用默认值 MAX_AGENT_TURNS
            api_path: LLM 调用协议格式（chat/completions / responses / messages）
            workspace_root: 本次请求的工作区沙箱根目录（覆盖全局配置），
                            空字符串时使用 config.json 的 agent_workspace_root

        Returns:
            AgentResult 包含 ok、final_message、完整 messages 历史等
        """
        from akm.proxy import forward_request

        _max_turns = max_turns if max_turns > 0 else self._max_turns

        # 准备 messages，如有 instructions 则注入为 system 消息
        working_messages: list[dict] = list(messages or [])
        if instructions and working_messages:
            if working_messages[0].get("role") == "system":
                working_messages[0]["content"] = (
                    str(working_messages[0].get("content", "")) + "\n\n" + instructions
                )
            else:
                working_messages.insert(0, {"role": "system", "content": instructions})

        # 工具注入策略：调用方显式传 tools 时只注入调用方声明的工具
        # （未声明的内置工具如 tavily_search / akm_search_kb 不注入，LLM 不会自主调用）；
        # 显式传空数组 [] 表示不注入任何工具；未传 tools（None）时注入除
        # _DEFAULT_EXCLUDED_TOOLS（联网搜索、图片生成/编辑）外的全部内置工具。
        # Chat 可传 tool_options（search/image）切换搜索和图片生成/编辑；读图与
        # 普通工具始终以服务端当前注册状态为准，前端无需维护工具 schema。
        # 上下文管理框架工具（akm_context_status / akm_compact_context）始终注入，
        # 除非显式传空数组 []；非空白名单时也追加，保证 AI 主动压缩能力可用。
        registered_tools = self._tool_registry.list_tools()
        if tools is not None:
            all_tools = []
            for declared in tools:
                if not isinstance(declared, dict):
                    continue
                name = (declared.get("function", {}) or {}).get("name", "")
                registered = self._tool_registry.get_definition(name)
                # 内置/插件已注册工具必须使用服务端 schema，不能由客户端伪造
                # 同名参数描述误导模型，再由本地 handler 按另一份契约执行。
                all_tools.append(registered.to_openai() if registered is not None else declared)
            if all_tools:
                all_tools.extend(_AGENT_CONTEXT_TOOLS)
        elif isinstance(tool_options, dict):
            search_enabled = bool(tool_options.get("search", False))
            image_enabled = bool(tool_options.get("image", False))
            all_tools = []
            for tool in registered_tools:
                name = (tool.get("function", {}) or {}).get("name", "")
                if name in _CHAT_OPTIONAL_TOOLS:
                    if name == "tavily_search" and not search_enabled:
                        continue
                    if name != "tavily_search" and not image_enabled:
                        continue
                all_tools.append(tool)
            all_tools.extend(_AGENT_CONTEXT_TOOLS)
        else:
            all_tools = [
                t
                for t in registered_tools
                if (t.get("function", {}) or {}).get("name", "")
                not in _DEFAULT_EXCLUDED_TOOLS
            ]
            all_tools.extend(_AGENT_CONTEXT_TOOLS)
        # 按 function name 去重；已注册的同名工具在构建 all_tools 时已被服务端
        # schema 替换，客户端只能声明授权，不能覆盖服务端参数契约。
        seen_names: set[str] = set()
        deduped_tools: list[dict] = []
        for t in all_tools:
            name = (t.get("function", {}) or {}).get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                deduped_tools.append(t)
        # 工具定义同时也是本次请求的执行授权名单。不能只控制发给模型的
        # tools 字段，否则模型仍可伪造一个未声明的 tool_call 触发注册处理器。
        allowed_tool_names = seen_names

        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        compacted_count = 0
        # 自愈重试计数：记录本次请求已注入的修正提示次数，必须为局部变量
        # （AgentLoop 是共享单例，不能把状态放到实例属性上，避免跨请求串扰）
        retry_error_count = 0
        tool_call_count = 0

        for turn in range(1, _max_turns + 1):
            # 上下文自动压缩：估算 token 超限时把早期历史压缩为摘要，控制上下文增长
            compacted_messages, removed = await self._compact_context(
                working_messages, model, api_path
            )
            if removed:
                working_messages = compacted_messages
                compacted_count += 1
                logger.info(
                    "[AgentLoop] 上下文已自动压缩（turn=%d，移除 %d 条旧消息）",
                    turn,
                    removed,
                )

            body: dict[str, Any] = {
                "model": model,
                "messages": working_messages,
                "stream": False,
            }
            if deduped_tools:
                body["tools"] = deduped_tools

            # 通过 forward_request 发送请求，复用选 Key、协议转换、重试
            result = await forward_request(
                body,
                self._http_client,
                api_path=api_path,
                plugin_manager=self._plugin_manager,
            )

            if result.get("stream"):
                logger.warning("[AgentLoop] 内部调用意外返回了流式响应，跳过")
                return AgentResult(
                    ok=False,
                    messages=working_messages,
                    turns=turn,
                    error="内部调用返回了流式响应，Agent Loop 不支持内部流式",
                    usage=total_usage,
                    compacted=compacted_count,
                )

            status_code = int(result.get("status_code", 0) or 0)
            response_body = str(result.get("body", "") or "")

            if status_code < 200 or status_code >= 300:
                error_msg = str(result.get("error", f"上游返回 HTTP {status_code}") or "")
                logger.warning("[AgentLoop] LLM 调用失败 (turn=%d): %s", turn, error_msg)
                await self._try_audit(
                    result, model, body, response_body, status_code,
                    0, 0, 0, error=error_msg, source=source,
                )
                return AgentResult(
                    ok=False,
                    messages=working_messages,
                    turns=turn,
                    error=error_msg,
                    usage=total_usage,
                    compacted=compacted_count,
                )

            # 累加 token 用量
            prompt_tokens = 0
            completion_tokens = 0
            cached_tokens = 0
            cache_creation_tokens = 0
            try:
                resp_data = json.loads(response_body)
                usage = resp_data.get("usage", {})
                if isinstance(usage, dict):
                    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                    total_usage["prompt_tokens"] += prompt_tokens
                    total_usage["completion_tokens"] += completion_tokens
                    total_usage["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
                    cached_tokens, cache_creation_tokens = _extract_cached_tokens(usage)
            except json.JSONDecodeError:
                pass

            # 写入审计日志，来源标记为 chat（/v1/agent）或 task（定时任务）
            await self._try_audit(
                result, model, body, response_body, status_code,
                prompt_tokens, completion_tokens, prompt_tokens + completion_tokens,
                source=source,
                cached_tokens=cached_tokens,
                cache_creation_tokens=cache_creation_tokens,
            )

            # 提取 tool_calls
            tool_calls = _extract_tool_calls_from_response(response_body)

            if not tool_calls:
                # 没有工具调用 → Agent 完成
                text_content = _extract_text_content(response_body)
                final_message = {
                    "role": "assistant",
                    "content": text_content or "",
                }
                reasoning_content = _extract_reasoning_content(response_body)
                if reasoning_content:
                    final_message["reasoning_content"] = reasoning_content
                working_messages.append(final_message)
                logger.info(
                    "[AgentLoop] 完成，共 %d 轮，tokens=%d",
                    turn,
                    total_usage["total_tokens"],
                )
                return AgentResult(
                    ok=True,
                    final_message=final_message,
                    messages=working_messages,
                    turns=turn,
                    usage=total_usage,
                    compacted=compacted_count,
                )

            # 有工具调用 → 构建 assistant 消息并执行工具
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
            tool_results: list[dict] = []
            # 本轮工具失败信息（首个错误文本），用于自愈重试判断
            tool_error_text = ""
            # akm_ask_user 触发时记录需要向用户澄清的内容（question/options/multiple），
            # 非 None 表示中断编排
            ask_user_data: dict | None = None
            for tc in tool_calls:
                tc_id = tc["id"]
                tc_name = tc["name"]
                try:
                    tc_args = (
                        json.loads(tc["arguments"])
                        if isinstance(tc["arguments"], str)
                        else tc["arguments"]
                    )
                except (json.JSONDecodeError, TypeError):
                    tc_args = {}

                assistant_msg["tool_calls"].append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": tc_name,
                        "arguments": json.dumps(tc_args, ensure_ascii=False),
                    },
                })

                if tool_call_count >= self._max_tool_calls:
                    tool_result = json.dumps(
                        {"error": f"工具调用次数超过本次请求上限 ({self._max_tool_calls})"},
                        ensure_ascii=False,
                    )
                elif tc_name not in allowed_tool_names:
                    tool_call_count += 1
                    tool_result = json.dumps(
                        {"error": f"工具未获本次请求授权: {tc_name}"},
                        ensure_ascii=False,
                    )
                elif tc_name == _AGENT_ASK_USER_TOOL:
                    # akm_ask_user：AI 需要向用户澄清提问。中断本轮编排，把问题
                    # 及候选选项原样记入工具结果，返回给客户端等待用户回答后继续。
                    tool_call_count += 1
                    ask_question = str((tc_args or {}).get("question", "") or "").strip()
                    ask_options = (tc_args or {}).get("options") or []
                    ask_multiple = bool((tc_args or {}).get("multiple", False))
                    ask_user_data = {
                        "question": ask_question,
                        "options": [str(o) for o in ask_options] if ask_options else [],
                        "multiple": ask_multiple,
                    }
                    tool_result = json.dumps({
                        "status": "awaiting_user",
                        **ask_user_data,
                    }, ensure_ascii=False)
                else:
                    tool_call_count += 1
                    # 执行工具：上下文管理框架工具由 AgentLoop 内联处理，
                    # 其余委托 ToolRegistry 执行（期间注入请求级工作区覆盖）
                    tool_result, working_messages, compacted_count = (
                        await self._execute_context_tool(
                            tc_name,
                            tc_args,
                            working_messages,
                            model,
                            api_path,
                            compacted_count,
                        )
                    )
                    if tool_result is None:
                        tool_result = await self._execute_registered_tool(tc_name, tc_args, workspace_root)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result,
                })
                # 记录工具失败信息，供自愈重试注入修正提示（只保留第一个错误文本）
                if not tool_error_text:
                    tool_error_text = self._tool_error_text(tool_result)

                logger.info(
                    "[AgentLoop] turn=%d 执行工具 %s(%s) → %d chars",
                    turn,
                    tc_name,
                    json.dumps(tc_args, ensure_ascii=False)[:120],
                    len(tool_result),
                )

                # 已触发 akm_ask_user：不再执行本轮的其余工具调用
                if ask_user_data is not None:
                    break

            # assistant 消息中包含所有 tool_calls，tool 结果紧跟其后
            working_messages.append(assistant_msg)
            working_messages.extend(tool_results)

            # AI 需要向用户澄清：中断编排并返回，等待客户端把用户回答追加入
            # messages 后重新请求以继续。final_message 即模型想问的问题。
            if ask_user_data is not None:
                logger.info(
                    "[AgentLoop] AI 向用户提问澄清（turn=%d）: %s", turn, ask_user_data["question"]
                )
                return AgentResult(
                    ok=True,
                    final_message={"role": "assistant", "content": ask_user_data["question"]},
                    messages=working_messages,
                    turns=turn,
                    usage=total_usage,
                    compacted=compacted_count,
                    ask_user=ask_user_data,
                )

            # 自愈重试：本轮存在工具失败且未超过修正上限时，注入一条 system 修正提示，
            # 强制模型基于错误信息修正工具参数后继续（比让模型自行决定更可靠）
            if tool_error_text and self._tool_retry_max > 0 and retry_error_count < self._tool_retry_max:
                retry_error_count += 1
                working_messages.append({
                    "role": "system",
                    "content": (
                        f"上一步工具调用失败：{tool_error_text}\n"
                        "请修正工具参数后重新调用；若确认无法完成，请直接向用户说明原因。"
                    ),
                })
                logger.info(
                    "[AgentLoop] 工具失败，注入自愈重试提示（第 %d/%d 次）",
                    retry_error_count,
                    self._tool_retry_max,
                )
                continue

        # 达到最大轮次
        logger.warning("[AgentLoop] 达到最大轮次限制 %d", _max_turns)
        return AgentResult(
            ok=False,
            messages=working_messages,
            turns=_max_turns,
            error=f"达到最大轮次限制 ({_max_turns})",
            usage=total_usage,
            compacted=compacted_count,
        )

    async def run_stream(
        self,
        messages: list[dict],
        *,
        model: str = "",
        tools: list[dict] | None = None,
        tool_options: dict | None = None,
        instructions: str = "",
        max_turns: int = 0,
        api_path: str = "chat/completions",
        workspace_root: str = "",
        session_id: str = "",
        cancel_check: Callable[[], bool] | None = None,
        source: str = "chat",
    ) -> AsyncGenerator[str, None]:
        """运行 Agent Loop 并流式返回 SSE 事件

        事件类型：
        - ``reasoning_delta`` — LLM 思考（推理）过程片段，实时下发，先于同段正文
        - ``model_delta``     — 可见正文片段，实时下发（含工具轮过程性正文与最终主体内容）
        - ``turn_start``      — 新一轮开始（检测到工具调用时）
        - ``tool_call``       — 单个工具调用
        - ``tool_result``     — 单个工具执行结果
        - ``tool_retry``      — 工具调用失败触发的自愈重试（agent_tool_retry_max_retries>0 时），
                                data 含 turn / retry_count / max_retries / error；随后服务端注入
                                一条 system 修正提示并强制模型修正参数后重新调用
        - ``turn_pause``      — 自然停顿点：当前轮 LLM 输出（正文/思考）已完整下发，且其
                                working_messages 快照随事件提供。客户端若要在回复中途插入引导
                                （打断换方向或补充内容），应在此事件后从容中断并携带该快照续跑，
                                可避免半截内容（data 含 turn / messages / usage / compacted）
        - ``context_warning`` — 上下文占用接近上限（超过 agent_context_warning_ratio 比例），
                                data 含 estimated_tokens / max_tokens / remaining_tokens / ratio / compacted
        - ``ask_user``        — AI 需要向用户澄清提问（调用 akm_ask_user），data 含 question /
                                messages / turns / usage；随后本轮结束，等待客户端把用户回答
                                追加入 messages 后重新请求以继续
        - ``cancelled``       — 手动中断（客户端通过 AbortController 取消流式请求，或
                                cancel_check 回调返回 True），data 含 turns / usage；随后生成器退出
        - ``final``           — Agent 完成，含 final_message / usage / turns
        - ``error``           — 错误结束，含 error / turns / usage

        顺序约定：思考（reasoning_delta）与正文（model_delta）按模型输出顺序
        实时流式下发；工具调用轮在正文之后出现 turn_start / tool_call /
        tool_result 事件；最终主体内容同样实时下发，最后以 final 收尾。

        每轮 LLM 调用以 ``stream: True`` 发送。上游协议帧只在服务端解析，
        客户端始终收到统一的 Agent SSE 事件。

        手动中断：当 ``cancel_check`` 提供且返回 True（如客户端断开、前端点击
        停止按钮触发的 AbortController）时，在关键 await 点（读取上游流前、
        工具执行前、每轮开始）检查并提前退出，下发 ``cancelled`` 事件。这样
        服务端会停止继续消费上游 LLM 流，避免「客户端已停止但仍烧 token」。

        Args:
            与 ``run()`` 相同。
            session_id: 可选会话 ID。传入时复用同名会话文件做增量合并（按消息
                        内容去重追加），同一多轮对话只保留一份完整历史；留空则
                        每次请求新建一个独立会话文件（默认行为）。
            cancel_check: 可选回调，返回 True 表示本次请求应被中断。

        Yields:
            格式化为 ``data: {...}\\n\\n`` 的 Agent SSE 字符串
        """
        from akm.proxy import forward_request

        _max_turns = max_turns if max_turns > 0 else self._max_turns

        working_messages: list[dict] = list(messages or [])
        if instructions and working_messages:
            if working_messages[0].get("role") == "system":
                working_messages[0]["content"] = (
                    str(working_messages[0].get("content", "")) + "\n\n" + instructions
                )
            else:
                working_messages.insert(0, {"role": "system", "content": instructions})

        # 工具注入策略：调用方显式传 tools 时只注入调用方声明的工具
        # （未声明的内置工具不注入）；显式传空数组 [] 表示不注入任何工具；
        # Chat 可传 tool_options（search/image）切换搜索和图片生成/编辑；读图与
        # 普通工具始终以服务端当前注册状态为准。未传 tools（None）时注入除 _DEFAULT_EXCLUDED_TOOLS（联网搜索、图片生成/编辑）
        # 外的全部内置工具。上下文管理框架工具始终注入，除非显式传空数组 []。
        registered_tools = self._tool_registry.list_tools()
        if tools is not None:
            all_tools = []
            for declared in tools:
                if not isinstance(declared, dict):
                    continue
                name = (declared.get("function", {}) or {}).get("name", "")
                registered = self._tool_registry.get_definition(name)
                all_tools.append(registered.to_openai() if registered is not None else declared)
            if all_tools:
                all_tools.extend(_AGENT_CONTEXT_TOOLS)
        elif isinstance(tool_options, dict):
            search_enabled = bool(tool_options.get("search", False))
            image_enabled = bool(tool_options.get("image", False))
            all_tools = []
            for tool in registered_tools:
                name = (tool.get("function", {}) or {}).get("name", "")
                if name in _CHAT_OPTIONAL_TOOLS:
                    if name == "tavily_search" and not search_enabled:
                        continue
                    if name != "tavily_search" and not image_enabled:
                        continue
                all_tools.append(tool)
            all_tools.extend(_AGENT_CONTEXT_TOOLS)
        else:
            all_tools = [
                t
                for t in registered_tools
                if (t.get("function", {}) or {}).get("name", "")
                not in _DEFAULT_EXCLUDED_TOOLS
            ]
            all_tools.extend(_AGENT_CONTEXT_TOOLS)
        seen_names: set[str] = set()
        deduped_tools: list[dict] = []
        for t in all_tools:
            name = (t.get("function", {}) or {}).get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                deduped_tools.append(t)
        # 与非流式路径保持一致：未注入的工具绝不能仅凭模型返回的名称执行。
        allowed_tool_names = seen_names

        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        compacted_count = 0
        # 自愈重试计数：记录本次请求已注入的修正提示次数，必须为局部变量
        # （AgentLoop 是共享单例，不能把状态放到实例属性上，避免跨请求串扰）
        retry_error_count = 0
        tool_call_count = 0

        for turn in range(1, _max_turns + 1):
            # ── 每轮开始检查取消 ──
            if cancel_check is not None and cancel_check():
                yield _sse_event("cancelled", {
                    "turns": turn,
                    "usage": total_usage,
                    "compacted": compacted_count,
                })
                return

            # 上下文自动压缩：估算 token 超限时把早期历史压缩为摘要，控制上下文增长
            compacted_messages, removed = await self._compact_context(
                working_messages, model, api_path
            )
            if removed:
                working_messages = compacted_messages
                compacted_count += 1
                logger.info(
                    "[AgentLoop] 上下文已自动压缩（turn=%d，移除 %d 条旧消息）",
                    turn,
                    removed,
                )

            # 上下文占用警告：估算 token 超过上限的 warning_ratio 时，下发
            # context_warning 事件，供客户端提前感知并提示 AI 主动压缩
            if (
                self._max_context_tokens > 0
                and self._context_warning_ratio > 0
            ):
                estimated = _estimate_messages_tokens(working_messages)
                if estimated >= self._max_context_tokens * self._context_warning_ratio:
                    yield _sse_event("context_warning", {
                        "estimated_tokens": estimated,
                        "max_tokens": self._max_context_tokens,
                        "remaining_tokens": max(0, self._max_context_tokens - estimated),
                        "ratio": round(estimated / self._max_context_tokens, 4),
                        "compacted": compacted_count,
                    })

            body: dict[str, Any] = {
                "model": model,
                "messages": working_messages,
                "stream": True,
            }
            if deduped_tools:
                body["tools"] = deduped_tools

            result = await forward_request(
                body,
                self._http_client,
                api_path=api_path,
                plugin_manager=self._plugin_manager,
            )

            # 本轮正文与思考均实时下发：正文作为 model_delta，思考作为
            # reasoning_delta。工具轮正文也实时流出（模型输出的自然顺序）。
            if result.get("stream"):
                # Agent 对外协议独立于原始上游协议，不能通过 adapter 转换后再解析。
                resp = result["response"]
                status_code = int(result.get("status_code", 0) or 0)
                acc = _SSEStreamAccumulator()

                try:
                    if status_code < 200 or status_code >= 300:
                        response_body = (await resp.aread()).decode("utf-8", errors="replace")
                        try:
                            error_data = json.loads(response_body)
                            error_msg = str(error_data.get("error", "") or "")
                        except json.JSONDecodeError:
                            error_msg = ""
                        error_msg = str(result.get("error", error_msg) or error_msg or f"上游返回 HTTP {status_code}")
                        await self._try_audit(
                            result, model, body, response_body, status_code,
                            0, 0, 0, error=error_msg, source=source,
                        )
                        yield _sse_event("error", {"error": error_msg, "turns": turn, "usage": total_usage, "compacted": compacted_count})
                        return

                    # proxy 首字节预读后，上游响应流已被消费过首块。直接对 resp
                    # 二次 aiter_bytes() 会因 httpx「已消费」抛 StreamConsumed，必须
                    # 复用 proxy 预读时创建的 aiter 生成器继续读流，并先补发预读缓存。
                    prefetched = result.get("first_chunk") if isinstance(result, dict) else None
                    upstream_aiter = result.get("aiter")
                    if prefetched:
                        text = prefetched.decode("utf-8", errors="replace")
                        contents = acc.feed(text)
                        for reasoning in acc.drain_reasoning_deltas():
                            yield _sse_event("reasoning_delta", {"turn": turn, "content": reasoning})
                        for content in contents:
                            yield _sse_event("model_delta", {"turn": turn, "content": content})

                    stream_source = upstream_aiter if upstream_aiter is not None else resp.aiter_bytes()
                    # ── 读取上游流前检查取消：客户端已停止时不再继续消费
                    # LLM 流，避免「客户端已停止但仍烧 token」 ──
                    if cancel_check is not None and cancel_check():
                        yield _sse_event("cancelled", {
                            "turns": turn,
                            "usage": total_usage,
                            "compacted": compacted_count,
                        })
                        return
                    async for chunk in stream_source:
                        text = chunk.decode("utf-8", errors="replace")
                        contents = acc.feed(text)
                        # 思考（推理）增量先于正文下发，保证「先思考后正文」
                        for reasoning in acc.drain_reasoning_deltas():
                            yield _sse_event("reasoning_delta", {"turn": turn, "content": reasoning})
                        # 正文增量实时下发，保证最终主体逐字流式输出
                        for content in contents:
                            yield _sse_event("model_delta", {"turn": turn, "content": content})
                    contents = acc.finish()
                    for reasoning in acc.drain_reasoning_deltas():
                        yield _sse_event("reasoning_delta", {"turn": turn, "content": reasoning})
                    for content in contents:
                        yield _sse_event("model_delta", {"turn": turn, "content": content})
                finally:
                    await resp.aclose()

                response_body = acc.response_body
                prompt_tokens = acc.prompt_tokens
                completion_tokens = acc.completion_tokens
                total_tokens = acc.total_tokens
                cached_tokens = acc.cached_tokens
                cache_creation_tokens = acc.cache_creation_tokens
            else:
                # ── 非流式兜底（上游不支持流式或其他原因返回了普通 JSON 响应）──
                status_code = int(result.get("status_code", 0) or 0)
                response_body = str(result.get("body", "") or "")

                if status_code < 200 or status_code >= 300:
                    error_msg = str(result.get("error", f"上游返回 HTTP {status_code}") or "")
                    await self._try_audit(
                        result, model, body, response_body, status_code,
                        0, 0, 0, error=error_msg, source=source,
                    )
                    yield _sse_event("error", {"error": error_msg, "turns": turn, "usage": total_usage, "compacted": compacted_count})
                    return

                prompt_tokens = 0
                completion_tokens = 0
                total_tokens = 0
                cached_tokens = 0
                cache_creation_tokens = 0
                try:
                    resp_data = json.loads(response_body)
                    usage = resp_data.get("usage", {})
                    if isinstance(usage, dict):
                        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
                        cached_tokens, cache_creation_tokens = _extract_cached_tokens(usage)
                except json.JSONDecodeError:
                    pass

            # ── 累加 token 用量 ──
            total_usage["prompt_tokens"] += prompt_tokens
            total_usage["completion_tokens"] += completion_tokens
            total_usage["total_tokens"] += total_tokens

            # ── 审计日志 ──
            await self._try_audit(
                result, model, body, response_body, status_code,
                prompt_tokens, completion_tokens, total_tokens,
                source=source,
                cached_tokens=cached_tokens,
                cache_creation_tokens=cache_creation_tokens,
            )

            # ── 从累积/重建的响应中提取 tool_calls ──
            tool_calls = _extract_tool_calls_from_response(response_body)

            if not tool_calls:
                # 无工具调用：该轮正文已实时以 model_delta 下发，此处仅确认完成
                text_content = _extract_text_content(response_body)
                final_message = {
                    "role": "assistant",
                    "content": text_content or "",
                }
                reasoning_content = _extract_reasoning_content(response_body)
                if reasoning_content:
                    final_message["reasoning_content"] = reasoning_content
                working_messages.append(final_message)
                # 自动保存会话到磁盘：默认开启（agent_session_auto_save=true），
                # /v1/agent 请求结束后自动保存到 ~/.akm/agent_sessions/，供
                # akm_load_session 工具或客户端回顾恢复对话；设为 false 则跳过。
                # 保存失败不阻塞主流程（目录权限等问题不会导致请求异常）。
                if self._session_auto_save:
                    try:
                        from akm.agent_runtime.sessions import SessionStore

                        _store = SessionStore()
                        # 传了 session_id 时复用同名会话文件做增量合并：先读取
                        # 磁盘上已有的完整历史作为基线，再按消息内容去重追加
                        # 本次新增消息，避免同一逻辑会话被保存成多个内容重叠
                        # 的独立文件；未传 session_id 时维持原有行为，每次请求
                        # 新建一个独立会话文件。
                        _session_id = str(session_id or "").strip()
                        if _session_id:
                            existing = _store.load(_session_id) or {}
                            merged = list(existing.get("messages") or [])
                            seen = {
                                json.dumps(m, ensure_ascii=False, default=str)
                                for m in merged
                            }
                            for m in working_messages:
                                key = json.dumps(m, ensure_ascii=False, default=str)
                                if key not in seen:
                                    seen.add(key)
                                    merged.append(m)
                            _store.save({
                                "name": _session_id,
                                "model": model,
                                "messages": merged,
                                "workspace_root": workspace_root,
                                "api_path": api_path,
                                "instructions": instructions,
                            })
                        else:
                            _store.save({
                                "name": _store.next_name(),
                                "model": model,
                                "messages": working_messages,
                                "workspace_root": workspace_root,
                                "api_path": api_path,
                                "instructions": instructions,
                            })
                    except Exception:
                        pass
                # 自然停顿点：本轮输出（final_message 已入列）完整，即将下发 final。
                # 客户端若在回复中途输入了引导消息，可在此打断当前流、携带完整
                # working_messages 快照续跑，避免半截残话或不必要的后续轮次。
                yield _sse_event("turn_pause", {
                    "turn": turn,
                    "messages": working_messages,
                    "usage": total_usage,
                    "compacted": compacted_count,
                })
                yield _sse_event("final", {
                    "final_message": final_message,
                    "messages": working_messages,
                    "turns": turn,
                    "usage": total_usage,
                    "compacted": compacted_count,
                })
                return

            # 有工具调用：工具轮正文已实时以 model_delta 下发（模型输出自然顺序），
            # 此处接着下发工具事件
            yield _sse_event("turn_start", {"turn": turn})

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
            tool_result_msgs: list[dict] = []
            # 本轮工具失败信息（首个错误文本），用于自愈重试判断
            tool_error_text = ""
            # akm_ask_user 触发时记录需要向用户澄清的内容（question/options/multiple），
            # 非 None 表示中断编排
            ask_user_data: dict | None = None
            for tc in tool_calls:
                tc_id = tc["id"]
                tc_name = tc["name"]
                try:
                    tc_args = (
                        json.loads(tc["arguments"])
                        if isinstance(tc["arguments"], str)
                        else tc["arguments"]
                    )
                except (json.JSONDecodeError, TypeError):
                    tc_args = {}

                yield _sse_event("tool_call", {"name": tc_name, "arguments": tc_args})

                assistant_msg["tool_calls"].append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": tc_name,
                        "arguments": json.dumps(tc_args, ensure_ascii=False),
                    },
                })

                if tool_call_count >= self._max_tool_calls:
                    tool_result = json.dumps(
                        {"error": f"工具调用次数超过本次请求上限 ({self._max_tool_calls})"},
                        ensure_ascii=False,
                    )
                elif tc_name not in allowed_tool_names:
                    tool_call_count += 1
                    tool_result = json.dumps(
                        {"error": f"工具未获本次请求授权: {tc_name}"},
                        ensure_ascii=False,
                    )
                elif tc_name == _AGENT_ASK_USER_TOOL:
                    # akm_ask_user：AI 需要向用户澄清提问。中断本轮编排，把问题及
                    # 候选选项记入工具结果，下发 ask_user 事件并等待用户回答后继续。
                    tool_call_count += 1
                    ask_question = str((tc_args or {}).get("question", "") or "").strip()
                    ask_options = (tc_args or {}).get("options") or []
                    ask_multiple = bool((tc_args or {}).get("multiple", False))
                    ask_user_data = {
                        "question": ask_question,
                        "options": [str(o) for o in ask_options] if ask_options else [],
                        "multiple": ask_multiple,
                    }
                    tool_result = json.dumps({
                        "status": "awaiting_user",
                        **ask_user_data,
                    }, ensure_ascii=False)
                else:
                    tool_call_count += 1
                    # ── 工具执行前检查取消：客户端已停止时不再执行工具 ──
                    if cancel_check is not None and cancel_check():
                        yield _sse_event("cancelled", {
                            "turns": turn,
                            "usage": total_usage,
                            "compacted": compacted_count,
                        })
                        return
                    # 执行工具：上下文管理框架工具由 AgentLoop 内联处理，
                    # 其余委托 ToolRegistry 执行（期间注入请求级工作区覆盖）
                    tool_result, working_messages, compacted_count = (
                        await self._execute_context_tool(
                            tc_name,
                            tc_args,
                            working_messages,
                            model,
                            api_path,
                            compacted_count,
                        )
                    )
                    if tool_result is None:
                        tool_result = await self._execute_registered_tool(tc_name, tc_args, workspace_root)
                yield _sse_event("tool_result", {"name": tc_name, "result": tool_result})
                # 记录工具失败信息，供自愈重试注入修正提示（只保留第一个错误文本）
                if not tool_error_text:
                    tool_error_text = self._tool_error_text(tool_result)

                tool_result_msgs.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result,
                })

                # 已触发 akm_ask_user：不再执行本轮的其余工具调用
                if ask_user_data is not None:
                    break

            working_messages.append(assistant_msg)
            working_messages.extend(tool_result_msgs)

            # AI 需要向用户澄清：下发 ask_user 事件并结束本轮，等待客户端把用户
            # 回答追加入 messages 后重新请求以继续（携带 working_messages 供续跑）。
            if ask_user_data is not None:
                logger.info(
                    "[AgentLoop] AI 向用户提问澄清（turn=%d）: %s", turn, ask_user_data["question"]
                )
                yield _sse_event("ask_user", {
                    **ask_user_data,
                    "messages": working_messages,
                    "turns": turn,
                    "usage": total_usage,
                    "compacted": compacted_count,
                })
                return

            # 自愈重试：本轮存在工具失败且未超过修正上限时，下发 tool_retry 事件
            # 并注入一条 system 修正提示，强制模型修正工具参数后继续
            if tool_error_text and self._tool_retry_max > 0 and retry_error_count < self._tool_retry_max:
                retry_error_count += 1
                yield _sse_event("tool_retry", {
                    "turn": turn,
                    "retry_count": retry_error_count,
                    "max_retries": self._tool_retry_max,
                    "error": tool_error_text,
                })
                working_messages.append({
                    "role": "system",
                    "content": (
                        f"上一步工具调用失败：{tool_error_text}\n"
                        "请修正工具参数后重新调用；若确认无法完成，请直接向用户说明原因。"
                    ),
                })
                logger.info(
                    "[AgentLoop] 工具失败，注入自愈重试提示（第 %d/%d 次）",
                    retry_error_count,
                    self._tool_retry_max,
                )
                continue

            # 自然停顿点：本轮工具调用已全部执行完、即将进入下一轮 LLM 调用前。
            # 客户端若在回复中途输入了引导消息，可在此时打断当前流、携带完整
            # working_messages 快照续跑（ask_user 等待回答与自愈重试走自身分支，
            # 不会到达这里，保证停顿点只出现在「本轮完整收尾」之后）。
            yield _sse_event("turn_pause", {
                "turn": turn,
                "messages": working_messages,
                "usage": total_usage,
                "compacted": compacted_count,
            })

        logger.warning("[AgentLoop] 达到最大轮次限制 %d", _max_turns)
        yield _sse_event("error", {
            "error": f"达到最大轮次限制 ({_max_turns})",
            "turns": _max_turns,
            "usage": total_usage,
        })


def _sse_event(event: str, data: dict) -> str:
    """构造一条 SSE 格式的字符串"""
    payload = json.dumps({"event": event, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


# ── 全局 Agent Loop 实例引用，由 server.py 在 lifespan 中创建和设置 ──

_agent_loop: Optional[AgentLoop] = None


def get_agent_loop() -> Optional[AgentLoop]:
    """获取全局 Agent Loop 实例"""
    return _agent_loop


def set_agent_loop(loop: Optional[AgentLoop]) -> None:
    """设置全局 Agent Loop 实例"""
    global _agent_loop
    _agent_loop = loop
