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
    ):
        self.ok = ok
        self.final_message = final_message or {}
        self.messages = messages or []
        self.turns = turns
        self.error = error
        self.usage = usage or {}

    def to_dict(self) -> dict:
        """转为可序列化的 dict"""
        return {
            "ok": self.ok,
            "final_message": self.final_message,
            "messages": self.messages,
            "turns": self.turns,
            "error": self.error,
            "usage": self.usage,
        }


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
        self._max_turns = max(1, int(load_config().get("agent_max_turns", 20) or 20))
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
    ) -> None:
        """如果注册了审计回调，则写入审计日志（来源标记为 agent）"""
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
            if save_request_body:
                req_body_for_log = json.dumps(request_body, ensure_ascii=False)
            await self._audit_submitter({
                "provider": str(result.get("provider", "") or ""),
                "key_alias": str(result.get("key_alias", "") or ""),
                "model": model,
                "request_body": req_body_for_log,
                "response_body": resp_for_log,
                "status_code": status_code,
                "latency_ms": int(result.get("latency_ms", 0) or 0),
                "error": error,
                "request_headers": json.dumps({"user-agent": "agent/1.0"}),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            })
        except Exception:
            logger.warning("[AgentLoop] 审计日志写入失败", exc_info=True)

    async def run(
        self,
        messages: list[dict],
        *,
        model: str = "",
        tools: list[dict] | None = None,
        instructions: str = "",
        max_turns: int = 0,
        api_path: str = "chat/completions",
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

        # 合并调用方传入的工具定义和注册中心的工具定义
        registered_tools = self._tool_registry.list_tools()
        all_tools = list(tools or []) + registered_tools
        # 按 function name 去重，优先保留调用方传入的（允许覆盖注册中心）
        seen_names: set[str] = set()
        deduped_tools: list[dict] = []
        for t in all_tools:
            name = (t.get("function", {}) or {}).get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                deduped_tools.append(t)

        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        for turn in range(1, _max_turns + 1):
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
                )

            status_code = int(result.get("status_code", 0) or 0)
            response_body = str(result.get("body", "") or "")

            if status_code < 200 or status_code >= 300:
                error_msg = str(result.get("error", f"上游返回 HTTP {status_code}") or "")
                logger.warning("[AgentLoop] LLM 调用失败 (turn=%d): %s", turn, error_msg)
                await self._try_audit(
                    result, model, body, response_body, status_code,
                    0, 0, 0, error=error_msg,
                )
                return AgentResult(
                    ok=False,
                    messages=working_messages,
                    turns=turn,
                    error=error_msg,
                    usage=total_usage,
                )

            # 累加 token 用量
            prompt_tokens = 0
            completion_tokens = 0
            try:
                resp_data = json.loads(response_body)
                usage = resp_data.get("usage", {})
                if isinstance(usage, dict):
                    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                    total_usage["prompt_tokens"] += prompt_tokens
                    total_usage["completion_tokens"] += completion_tokens
                    total_usage["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
            except json.JSONDecodeError:
                pass

            # 写入审计日志，来源标记为 agent
            await self._try_audit(
                result, model, body, response_body, status_code,
                prompt_tokens, completion_tokens, prompt_tokens + completion_tokens,
            )

            # 提取 tool_calls
            tool_calls = _extract_tool_calls_from_response(response_body)

            if not tool_calls:
                # 没有工具调用 → Agent 完成
                text_content = _extract_text_content(response_body)
                final_message = {
                    "role": "assistant",
                    "content": text_content or response_body,
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
                )

            # 有工具调用 → 构建 assistant 消息并执行工具
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
            tool_results: list[dict] = []
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

                # 执行工具
                tool_result = await self._tool_registry.execute(tc_name, tc_args)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result,
                })

                logger.info(
                    "[AgentLoop] turn=%d 执行工具 %s(%s) → %d chars",
                    turn,
                    tc_name,
                    json.dumps(tc_args, ensure_ascii=False)[:120],
                    len(tool_result),
                )

            # assistant 消息中包含所有 tool_calls，tool 结果紧跟其后
            working_messages.append(assistant_msg)
            working_messages.extend(tool_results)

        # 达到最大轮次
        logger.warning("[AgentLoop] 达到最大轮次限制 %d", _max_turns)
        return AgentResult(
            ok=False,
            messages=working_messages,
            turns=_max_turns,
            error=f"达到最大轮次限制 ({_max_turns})",
            usage=total_usage,
        )

    async def run_stream(
        self,
        messages: list[dict],
        *,
        model: str = "",
        tools: list[dict] | None = None,
        instructions: str = "",
        max_turns: int = 0,
        api_path: str = "chat/completions",
    ) -> AsyncGenerator[str, None]:
        """运行 Agent Loop 并流式返回 SSE 事件

        事件类型：
        - ``reasoning_delta`` — LLM 思考（推理）过程片段，先于正文/工具实时下发
        - ``thinking``        — 工具调用轮产生的过程性正文（模型在发起工具前的说明文本），在工具前下发
        - ``model_delta``     — 最终主体内容的可见文本片段
        - ``turn_start``      — 新一轮开始（检测到工具调用时）
        - ``tool_call``       — 单个工具调用
        - ``tool_result``     — 单个工具执行结果
        - ``final``           — Agent 完成，含 final_message / usage / turns
        - ``error``           — 错误结束，含 error / turns / usage

        顺序约定：思考（reasoning_delta）→ 工具（thinking/tool_call/tool_result）→
        最终主体内容（model_delta），保证主体内容一定在工具/思考之后返回。

        每轮 LLM 调用以 ``stream: True`` 发送。上游协议帧只在服务端解析，
        客户端始终收到统一的 Agent SSE 事件。

        Args:
            与 ``run()`` 相同。

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

        registered_tools = self._tool_registry.list_tools()
        all_tools = list(tools or []) + registered_tools
        seen_names: set[str] = set()
        deduped_tools: list[dict] = []
        for t in all_tools:
            name = (t.get("function", {}) or {}).get("name", "")
            if name and name not in seen_names:
                seen_names.add(name)
                deduped_tools.append(t)

        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        for turn in range(1, _max_turns + 1):
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

            # 本轮正文增量先暂存，轮结束后判断：最终轮才作为主体
            # model_delta 下发；工具轮改以 thinking 事件在工具前下发。
            turn_content_parts: list[str] = []

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
                            0, 0, 0, error=error_msg,
                        )
                        yield _sse_event("error", {"error": error_msg, "turns": turn, "usage": total_usage})
                        return

                    async for chunk in resp.aiter_bytes():
                        for content in acc.feed(chunk.decode("utf-8", errors="replace")):
                            turn_content_parts.append(content)
                        # 思考（推理）内容独立成 reasoning_delta，先于正文/工具实时下发
                        for reasoning in acc.drain_reasoning_deltas():
                            yield _sse_event("reasoning_delta", {"turn": turn, "content": reasoning})
                    for content in acc.finish():
                        turn_content_parts.append(content)
                    for reasoning in acc.drain_reasoning_deltas():
                        yield _sse_event("reasoning_delta", {"turn": turn, "content": reasoning})
                finally:
                    await resp.aclose()

                response_body = acc.response_body
                prompt_tokens = acc.prompt_tokens
                completion_tokens = acc.completion_tokens
                total_tokens = acc.total_tokens
            else:
                # ── 非流式兜底（上游不支持流式或其他原因返回了普通 JSON 响应）──
                status_code = int(result.get("status_code", 0) or 0)
                response_body = str(result.get("body", "") or "")

                if status_code < 200 or status_code >= 300:
                    error_msg = str(result.get("error", f"上游返回 HTTP {status_code}") or "")
                    await self._try_audit(
                        result, model, body, response_body, status_code,
                        0, 0, 0, error=error_msg,
                    )
                    yield _sse_event("error", {"error": error_msg, "turns": turn, "usage": total_usage})
                    return

                prompt_tokens = 0
                completion_tokens = 0
                total_tokens = 0
                try:
                    resp_data = json.loads(response_body)
                    usage = resp_data.get("usage", {})
                    if isinstance(usage, dict):
                        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
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
            )

            # ── 从累积/重建的响应中提取 tool_calls ──
            tool_calls = _extract_tool_calls_from_response(response_body)

            if not tool_calls:
                # 没有工具调用 → 该轮正文即为最终主体内容，作为 model_delta 下发
                for content in turn_content_parts:
                    yield _sse_event("model_delta", {"turn": turn, "content": content})
                # 没有工具调用 → Agent 完成
                text_content = _extract_text_content(response_body)
                final_message = {
                    "role": "assistant",
                    "content": text_content or response_body,
                }
                reasoning_content = _extract_reasoning_content(response_body)
                if reasoning_content:
                    final_message["reasoning_content"] = reasoning_content
                working_messages.append(final_message)
                yield _sse_event("final", {
                    "final_message": final_message,
                    "messages": working_messages,
                    "turns": turn,
                    "usage": total_usage,
                })
                return

            # 有工具调用：本轮正文属于过程性说明，作为 thinking 事件在工具前下发
            if turn_content_parts:
                yield _sse_event("thinking", {"turn": turn, "content": "".join(turn_content_parts)})
            # 有工具调用 → emit 事件并执行
            yield _sse_event("turn_start", {"turn": turn})

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
            tool_result_msgs: list[dict] = []
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

                tool_result = await self._tool_registry.execute(tc_name, tc_args)
                yield _sse_event("tool_result", {"name": tc_name, "result": tool_result})

                tool_result_msgs.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result,
                })

            working_messages.append(assistant_msg)
            working_messages.extend(tool_result_msgs)

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
