"""协议转换告警码常量。

集中管理 warning 字符串，避免多处硬编码导致拼写漂移。
"""

# Responses 请求字段降级/未完整映射告警
RESPONSES_INCLUDE_NOT_FULLY_MAPPED = "responses_include_not_fully_mapped"
RESPONSES_STORE_NOT_MAPPED = "responses_store_not_mapped"
RESPONSES_REASONING_SUMMARY_NOT_MAPPED = "responses_reasoning_summary_not_mapped"
RESPONSES_PARALLEL_TOOL_CALLS_NOT_MAPPED = "responses_parallel_tool_calls_not_mapped"
