"""调用方声明 JSON 输出时的最小结构化响应校验插件。"""

from __future__ import annotations

import json

from akm.plugins import PluginBase


class Plugin(PluginBase):
    """支持 json_object 与常用 JSON Schema required/type/enum/properties 校验。"""

    def _expected_schema(self, request: dict) -> dict | None:
        """从 Chat 或 Responses 请求中提取调用方声明的结构化输出约束。"""
        response_format = request.get("response_format") if isinstance(request.get("response_format"), dict) else {}
        if response_format.get("type") == "json_object":
            return {}
        if response_format.get("type") == "json_schema":
            schema = response_format.get("json_schema", {}).get("schema") if isinstance(response_format.get("json_schema"), dict) else None
            return schema if isinstance(schema, dict) else {}
        text_format = request.get("text", {}).get("format") if isinstance(request.get("text"), dict) else {}
        if isinstance(text_format, dict) and text_format.get("type") in ("json_schema", "json_object"):
            schema = text_format.get("schema") or text_format.get("json_schema", {}).get("schema")
            return schema if isinstance(schema, dict) else {}
        return None

    def _response_text(self, body: str) -> str:
        """从 Chat、Responses 或 Messages 非流式响应中提取可校验的文本。"""
        try:
            data = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            return ""
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message", {})
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
        output = data.get("output") if isinstance(data, dict) else None
        if isinstance(output, list):
            return "\n".join(
                part.get("text", "") for item in output if isinstance(item, dict)
                for part in (item.get("content") or []) if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        content = data.get("content") if isinstance(data, dict) else None
        if isinstance(content, list):
            return "\n".join(item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str))
        return ""

    def _validate(self, value, schema: dict, path: str = "$") -> str:
        """递归覆盖最常用 JSON Schema 子集，返回首条可读错误。"""
        expected_type = schema.get("type")
        type_checks = {"object": dict, "array": list, "string": str, "number": (int, float), "integer": int, "boolean": bool}
        if expected_type in type_checks and (not isinstance(value, type_checks[expected_type]) or (expected_type in ("number", "integer") and isinstance(value, bool))):
            return f"{path} 应为 {expected_type}"
        if "enum" in schema and value not in schema["enum"]:
            return f"{path} 不在 enum 中"
        if isinstance(value, dict):
            for name in schema.get("required", []) if isinstance(schema.get("required"), list) else []:
                if name not in value:
                    return f"{path}.{name} 为必填字段"
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            for name, child_schema in properties.items():
                if name in value and isinstance(child_schema, dict):
                    error = self._validate(value[name], child_schema, f"{path}.{name}")
                    if error:
                        return error
        if isinstance(value, list) and isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                error = self._validate(item, schema["items"], f"{path}[{index}]")
                if error:
                    return error
        return ""

    def _safe_body(self, api_path: str, message: str) -> str:
        """返回客户端当前协议可解析的结构化校验失败响应。"""
        if api_path == "responses":
            return json.dumps({"id":"resp_schema_guard","object":"response","status":"failed","error":{"message":message,"type":"invalid_response_schema"}}, ensure_ascii=False)
        if api_path == "messages":
            return json.dumps({"type":"error","error":{"type":"invalid_response_schema","message":message}}, ensure_ascii=False)
        return json.dumps({"error":{"message":message,"type":"invalid_response_schema"}}, ensure_ascii=False)

    async def on_response(self, request, response):
        """校验非流式成功响应；安全插件已改写的结果保持优先级，不再覆盖。"""
        cfg = self.config or {}
        if cfg.get("enabled", True) is not True or not isinstance(request, dict) or not isinstance(response, dict):
            return None
        if not response.get("ok") or response.get("stream") or response.get("security_action"):
            return response
        schema = self._expected_schema(request)
        if schema is None:
            return response
        try:
            value = json.loads(self._response_text(str(response.get("response_body", "") or "")))
            error = self._validate(value, schema)
        except json.JSONDecodeError:
            error = "输出不是合法 JSON"
        if not error:
            return response
        guarded = dict(response)
        guarded["schema_action"] = "warn" if cfg.get("mode", "block") == "warn" else "block"
        guarded["security_action"] = f"schema_{guarded['schema_action']}"
        guarded["security_reason"] = error
        if guarded["schema_action"] == "block":
            message = str(cfg.get("block_message", "模型响应不符合调用方声明的 JSON 格式。") or "模型响应不符合调用方声明的 JSON 格式。")
            guarded["response_body"] = self._safe_body(str(response.get("api_path", "chat/completions") or "chat/completions"), message)
        self.logger.warning("[response_schema_guard] %s", error)
        return guarded
