"""Token/转换统计来源与诊断标记常量。"""

# usage 来源标记
FLAG_COUNT_TOKENS_FALLBACK = "count_tokens_fallback"
FLAG_USAGE_FALLBACK_ADAPTER = "usage_fallback_adapter"
FLAG_USAGE_ESTIMATED_LIGHT = "usage_estimated_light"

# 诊断标记
FLAG_MISSING_USAGE_UPSTREAM = "missing_usage_upstream"
FLAG_LOOP_GUARD_TRIGGERED = "loop_guard_triggered"

# 上游明确表示「缓存数据不可知」（如 new-api 系中转的 magpie_cache_unknown）。
# 与 FLAG_MISSING_USAGE_UPSTREAM 的区别：后者是「完全没有 usage」，
# 这里 usage 齐全、只有缓存字段缺失，因此不能退化成估算，只需在展示上标为未知。
FLAG_CACHE_UNKNOWN = "cache_unknown"
