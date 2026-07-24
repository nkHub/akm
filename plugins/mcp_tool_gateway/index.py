"""MCP / 本地 HTTP 工具网关插件。

定位（与 ``tool_policy_guard`` 互补）：
- 维护一份「本地可调用工具」注册表；
- 可选把工具以 OpenAI function tools 形态注入模型请求；
- 提供受控 ``POST /call``，把 arguments 转发到配置的 HTTP 端点；
- **不**在代理内自动执行模型返回的 tool_calls 续接（仍由客户端/Agent 负责）。

安全边界：
- 仅允许 http/https；
- 可选 host 白名单（默认仅本机）；
- 参数体积与超时受限；
- 不能替代客户端本机工具沙箱。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Body, HTTPException

from akm.plugins import PluginBase


router = APIRouter()
_plugin_instance: "Plugin | None" = None


def _plugin() -> "Plugin":
    """取得由 PluginManager 注入上下文后的唯一插件实例。"""
    if _plugin_instance is None or not _plugin_instance.enabled:
        raise HTTPException(status_code=503, detail="mcp_tool_gateway 插件未启用")
    return _plugin_instance


@router.get("/status")
async def status():
    """返回网关启用状态与已注册工具摘要（不含 headers 密钥）。"""
    return _plugin().status()


@router.get("/list")
async def list_tools():
    """列出可注入/可调用的工具元数据。"""
    return _plugin().list_tools_public()


@router.post("/call")
async def call_tool(payload: dict = Body(default={})):
    """执行已注册工具：body = {name, arguments?}。"""
    return await _plugin().call_tool(payload if isinstance(payload, dict) else {})


class Plugin(PluginBase):
    """工具注册、请求注入与 HTTP 调用代理。"""

    router = router

    async def on_load(self):
        """登记全局实例。"""
        global _plugin_instance
        _plugin_instance = self

    async def on_unload(self):
        """卸载时摘掉路由实例引用。"""
        global _plugin_instance
        if _plugin_instance is self:
            _plugin_instance = None

    def _items(self, raw) -> list[str]:
        """兼容逗号与换行列表。"""
        return [
            item.strip()
            for item in str(raw or "").replace("\r", "\n").replace(",", "\n").split("\n")
            if item.strip()
        ]

    def _settings(self) -> dict:
        """热读配置。"""
        cfg = self.config or {}
        return {
            "enabled": cfg.get("enabled", True) is True,
            "inject_tools": cfg.get("inject_tools", False) is True,
            "strip_unlisted_tools": cfg.get("strip_unlisted_tools", False) is True,
            "tools_json": str(cfg.get("tools_json", "[]") or "[]"),
            "max_argument_bytes": max(
                256, min(1_048_576, int(cfg.get("max_argument_bytes", 32768) or 32768))
            ),
            "default_timeout_seconds": max(
                1, min(120, int(cfg.get("default_timeout_seconds", 15) or 15))
            ),
            "allow_call_api": cfg.get("allow_call_api", True) is True,
            "allowed_url_hosts": self._items(cfg.get("allowed_url_hosts", "127.0.0.1,localhost")),
        }

    def _parse_tools(self, cfg: dict) -> list[dict]:
        """解析 tools_json，过滤非法项并规范化字段。"""
        raw = cfg["tools_json"].strip() or "[]"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.logger.warning("[mcp_tool_gateway] tools_json 非法 JSON: %s", exc)
            return []
        if not isinstance(data, list):
            self.logger.warning("[mcp_tool_gateway] tools_json 必须是数组")
            return []

        tools: list[dict] = []
        seen: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "") or "").strip()
            url = str(item.get("url", "") or "").strip()
            if not name or not url or name in seen:
                continue
            if not self._url_allowed(url, cfg["allowed_url_hosts"]):
                self.logger.warning(
                    "[mcp_tool_gateway] 跳过工具 %s：URL 不被允许 (%s)", name, url
                )
                continue
            method = str(item.get("method", "POST") or "POST").upper()
            if method not in ("GET", "POST", "PUT", "PATCH"):
                method = "POST"
            try:
                timeout = float(item.get("timeout_seconds", cfg["default_timeout_seconds"]))
            except (TypeError, ValueError):
                timeout = float(cfg["default_timeout_seconds"])
            timeout = max(1.0, min(120.0, timeout))
            raw_headers = item.get("headers")
            headers = raw_headers if isinstance(raw_headers, dict) else {}
            # 仅保留字符串头，避免非预期类型
            safe_headers = {
                str(k): str(v)
                for k, v in headers.items()
                if str(k).strip() and v is not None
            }
            parameters = item.get("parameters")
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "properties": {}}
            tools.append(
                {
                    "name": name,
                    "description": str(item.get("description", "") or ""),
                    "parameters": parameters,
                    "url": url,
                    "method": method,
                    "timeout_seconds": timeout,
                    "headers": safe_headers,
                }
            )
            seen.add(name)
        return tools

    def _url_allowed(self, url: str, allowed_hosts: list[str]) -> bool:
        """校验工具 URL：协议必须是 http(s)，可选 host 白名单。"""
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.netloc:
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        if not allowed_hosts:
            return True
        return host in {h.lower() for h in allowed_hosts}

    def _as_openai_tools(self, tools: list[dict]) -> list[dict]:
        """转成 Chat Completions / 多数网关兼容的 tools 声明。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for tool in tools
        ]

    def _tool_name_from_declaration(self, tool: Any) -> str:
        """从 tools 数组元素提取名称。"""
        if not isinstance(tool, dict):
            return ""
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if isinstance(function, dict):
            return str(function.get("name", "") or "")
        return ""

    async def on_request(self, ctx) -> None:
        """按配置注入/剥离 tools 声明。"""
        request = ctx.request
        if not isinstance(request, dict):
            return
        cfg = self._settings()
        if not cfg["enabled"]:
            return
        if not cfg["inject_tools"] and not cfg["strip_unlisted_tools"]:
            return

        registry = self._parse_tools(cfg)
        registered_names = {t["name"] for t in registry}
        existing = request.get("tools")
        tools_list = list(existing) if isinstance(existing, list) else []

        if cfg["strip_unlisted_tools"] and tools_list:
            kept = []
            removed = []
            for tool in tools_list:
                name = self._tool_name_from_declaration(tool)
                if name and name in registered_names:
                    kept.append(tool)
                elif name:
                    removed.append(name)
                else:
                    # 无法识别名称的声明：剥离模式下直接丢掉，避免绕过
                    removed.append("?")
            tools_list = kept
            if removed:
                self.logger.info(
                    "[mcp_tool_gateway] 已剥离未注册工具声明: %s",
                    ", ".join(removed[:20]),
                )
                ctx.bag_set("mcp_tool_gateway.stripped", removed)

        if cfg["inject_tools"] and registry:
            existing_names = {
                self._tool_name_from_declaration(tool) for tool in tools_list
            }
            injected = []
            for decl in self._as_openai_tools(registry):
                name = self._tool_name_from_declaration(decl)
                if name and name not in existing_names:
                    tools_list.append(decl)
                    injected.append(name)
                    existing_names.add(name)
            if injected:
                ctx.bag_set("mcp_tool_gateway.injected", injected)

        if tools_list:
            request["tools"] = tools_list
        elif "tools" in request and (cfg["strip_unlisted_tools"] or cfg["inject_tools"]):
            # 剥离后为空则删除字段，避免上游收到空 tools
            request.pop("tools", None)


    def list_tools_public(self) -> dict:
        """对外暴露的工具列表（脱敏 headers）。"""
        cfg = self._settings()
        tools = self._parse_tools(cfg)
        return {
            "ok": True,
            "count": len(tools),
            "tools": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                    "method": t["method"],
                    "timeout_seconds": t["timeout_seconds"],
                    # 仅展示 host 与 path，降低 URL 敏感信息暴露
                    "endpoint_host": urlparse(t["url"]).hostname or "",
                    "has_headers": bool(t["headers"]),
                }
                for t in tools
            ],
        }

    def status(self) -> dict:
        """状态摘要。"""
        cfg = self._settings()
        tools = self._parse_tools(cfg)
        return {
            "ok": True,
            "enabled": cfg["enabled"],
            "inject_tools": cfg["inject_tools"],
            "strip_unlisted_tools": cfg["strip_unlisted_tools"],
            "allow_call_api": cfg["allow_call_api"],
            "tool_count": len(tools),
            "tool_names": [t["name"] for t in tools],
            "allowed_url_hosts": cfg["allowed_url_hosts"],
        }

    async def call_tool(self, payload: dict) -> dict:
        """按名称调用已注册工具。"""
        cfg = self._settings()
        if not cfg["enabled"]:
            raise HTTPException(status_code=503, detail="mcp_tool_gateway 未启用")
        if not cfg["allow_call_api"]:
            raise HTTPException(status_code=403, detail="调用 API 已关闭")

        name = str(payload.get("name", "") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="缺少 name")

        tools = {t["name"]: t for t in self._parse_tools(cfg)}
        tool = tools.get(name)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"未注册工具: {name}")

        arguments = payload.get("arguments", {})
        if arguments is None:
            arguments = {}
        # 允许 string 形式的 JSON arguments（兼容部分客户端）
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail=f"arguments 不是合法 JSON: {exc}") from exc
        if not isinstance(arguments, dict):
            raise HTTPException(status_code=400, detail="arguments 必须是 object")

        try:
            body_bytes = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"arguments 无法序列化: {exc}") from exc
        if len(body_bytes) > cfg["max_argument_bytes"]:
            raise HTTPException(
                status_code=413,
                detail=f"arguments 超过上限 {cfg['max_argument_bytes']} 字节",
            )

        # 二次校验 URL（配置热更新或恶意改写时兜底）
        if not self._url_allowed(tool["url"], cfg["allowed_url_hosts"]):
            raise HTTPException(status_code=400, detail="工具 URL 不被允许")

        timeout = float(tool["timeout_seconds"])
        method = tool["method"]
        headers = {
            "Content-Type": "application/json",
            **tool["headers"],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "GET":
                    # GET 将 arguments 作为 query 不合适复杂结构，改为 JSON body 不被所有服务支持；
                    # 这里用 query 扁平字符串 + 完整 JSON 放在 body 的兼容：仅传 params 的简单键值
                    params = {
                        str(k): v if isinstance(v, (str, int, float, bool)) or v is None else json.dumps(v, ensure_ascii=False)
                        for k, v in arguments.items()
                    }
                    resp = await client.get(tool["url"], params=params, headers=headers)
                else:
                    resp = await client.request(
                        method,
                        tool["url"],
                        content=body_bytes,
                        headers=headers,
                    )
        except httpx.TimeoutException as exc:
            self.logger.warning("[mcp_tool_gateway] 工具超时 %s: %s", name, exc)
            raise HTTPException(status_code=504, detail=f"工具超时: {name}") from exc
        except httpx.HTTPError as exc:
            self.logger.warning("[mcp_tool_gateway] 工具请求失败 %s: %s", name, exc)
            raise HTTPException(status_code=502, detail=f"工具请求失败: {exc}") from exc

        content_type = resp.headers.get("content-type", "")
        text = resp.text
        result_body: Any
        if "application/json" in content_type.lower():
            try:
                result_body = resp.json()
            except ValueError:
                result_body = text
        else:
            result_body = text

        return {
            "ok": 200 <= resp.status_code < 300,
            "name": name,
            "status_code": resp.status_code,
            "result": result_body,
        }
