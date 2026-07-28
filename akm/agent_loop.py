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

logger = logging.getLogger("akm.agent_loop")

# Agent Loop 最大迭代次数，防止工具调用无限循环
MAX_AGENT_TURNS = 20


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

    插件在 register_tools() 中通过 register() 注册工具实现。
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
        self._max_turns = MAX_AGENT_TURNS
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
            resp_for_log = response_body
            if len(resp_for_log) > 64000:
                resp_for_log = resp_for_log[:32000] + f"\n...(截断，共 {len(resp_for_log)} 字符)" + resp_for_log[-32000:]
            await self._audit_submitter({
                "provider": str(result.get("provider", "") or ""),
                "key_alias": str(result.get("key_alias", "") or ""),
                "model": model,
                "request_body": json.dumps(request_body, ensure_ascii=False),
                "response_body": resp_for_log,
                "status_code": status_code,
                "latency_ms": int(result.get("latency_ms", 0) or 0),
                "error": error,
                "request_headers": json.dumps({"x-source": "agent"}),
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
        """运行 Agent Loop 并流式返回 SSE 事件（每轮 emit 一次事件）

        事件类型：
        - ``turn_start``    — 新一轮开始
        - ``tool_call``     — 单个工具调用
        - ``tool_result``   — 单个工具执行结果
        - ``final``         — Agent 完成，含 final_message / usage / turns
        - ``error``         — 错误结束，含 error / turns / usage

        Args:
            与 ``run()`` 相同。

        Yields:
            格式化为 ``data: {...}\\n\\n`` 的 SSE 字符串
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
                "stream": False,
            }
            if deduped_tools:
                body["tools"] = deduped_tools

            result = await forward_request(
                body,
                self._http_client,
                api_path=api_path,
                plugin_manager=self._plugin_manager,
            )

            if result.get("stream"):
                yield _sse_event("error", {"error": "内部调用返回了流式响应", "turns": turn, "usage": total_usage})
                return

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

            tool_calls = _extract_tool_calls_from_response(response_body)

            if not tool_calls:
                text_content = _extract_text_content(response_body)
                final_message = {
                    "role": "assistant",
                    "content": text_content or response_body,
                }
                working_messages.append(final_message)
                yield _sse_event("final", {
                    "final_message": final_message,
                    "messages": working_messages,
                    "turns": turn,
                    "usage": total_usage,
                })
                return

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
