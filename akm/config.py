"""配置管理 — 读写 ~/.akm/config.json"""

import json
import os

from akm.cost_estimate import DEFAULT_PRICING_TABLE

CONFIG_DIR = os.path.expanduser("~/.akm")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "auto_open_admin": True,  # 启动时自动打开管理台
    "log_retention_days": 30,  # 日志保留天数
    "server_port": 8800,       # 默认服务端口
    "log_request_body": False,  # 是否记录请求体（含完整对话内容，占用空间大）
    "log_response_body": False, # 是否记录响应体（占用空间大，关闭不影响统计）
    "stream_capture_max_bytes": 262144,  # 流式响应内存捕获上限（用于审计和 token 统计，默认 256KB）
    "stats_include_estimated_usage": False,  # 首页统计是否计入 estimated token，默认关闭更保守
    "cost_stats_enabled": False,  # 首页费用估算开关（不能替代供应商账单）
    "cost_pricing_table": DEFAULT_PRICING_TABLE,  # 模型单价表：model=输入/缓存/输出（每 1M tokens，固定美元）
    "json_viewer_max_text_length": 600000,  # JSON 查看器超长文本阈值（超过后仅允许下载原文）
    "image_supported_models": "gpt-image-2",  # 图片生成/编辑支持的模型列表（逗号分隔，首项作为默认值）
    "image_request_timeout_sec": 300,  # 图片生成/编辑请求超时（秒），默认比聊天接口更宽松
    "wake_recover_delay_sec": 8,  # 菜单栏应用在系统唤醒后等待网络/VPN恢复的秒数
    "use_native_user_agent": False,  # 是否透传客户端原始 User-Agent 并携带客户端业务头到上游
    # 出站 HTTP 代理：仅作用于 AKM 访问上游供应商的请求，不是系统 VPN
    "http_proxy_enabled": False,
    "http_proxy_url": "",  # 例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
    # macOS 原生功能
    "launch_at_login": False,  # 开机自启动（仅打包后的 .app 生效，通过 SMAppService 注册）
    "menu_bar_show_usage": False,  # 菜单栏显示今日用量（Token 数 / 费用交替展示）
    # 上游连接池：控制 AKM 访问上游供应商时的并发连接数
    "http_client_max_connections": 8,   # 每个上游路由的最大并发连接数
    "http_client_max_keepalive": 2,     # 每个上游路由的 keep-alive 连接数
    "http_client_max_pools": 64,        # 最大路由池数量（provider+key+model+path 组合数上限）
    "http_client_idle_ttl_sec": 120.0,  # 连接池空闲超时（秒），超时后该路由池被回收
    "http_client_connect_timeout_sec": 10.0,  # 连接建立超时（秒）
    # 代理转发：控制请求重试与 Key 选择策略
    "proxy_max_key_tries": 20,          # 选择 Key 的最大尝试次数，防止无限循环
    "proxy_max_retries_per_key": 2,     # 单个 Key 在 5xx/连接失败时的最大重试次数
    "proxy_retry_backoff_base_sec": 0.5,  # 重试退避基础等待秒数
    "proxy_default_timeout_sec": 120.0,  # 默认转发超时（秒），图片接口另有独立超时
    "proxy_first_byte_timeout_sec": 12.0,  # 流式首字节超时（秒）：上游 2xx 后迟迟不产出第一个响应体字节时判定“假成功”并切换 Key；0 表示关闭
    # Agent Loop
    "agent_max_turns": 100,              # Agent Loop 最大迭代轮次，防止工具调用无限循环
    "agent_max_context_tokens": 272000, # Agent Loop 上下文 token 估算上限，超过后自动压缩早期历史（0 表示不压缩）
    "agent_keep_recent_messages": 10,   # Agent Loop 压缩上下文时保留的最近消息条数（工具调用配对消息会自动完整保留）
    "agent_context_warning_ratio": 0.8, # Agent Loop 上下文占用量超上限该比例时，SSE 下发 context_warning 事件（0 关闭）
    "agent_upload_dir": "~/.akm/cache", # Agent 上传文件（图片）的保存目录，路径支持 ~ 展开
    "agent_workspace_root": "",         # Agent 工作区沙箱根目录（文件工具唯一可访问范围）；留空禁用全部文件工具
    "agent_write_tools_enabled": False, # 是否启用 Agent 写文件工具（write/edit/make_dir/delete/run_shell 中除 shell 外的全部）
    "agent_run_shell_enabled": False,   # 是否启用 Agent shell 执行工具（akm_run_shell，独立于写工具，默认关闭）
    "agent_git_enabled": False,         # 是否启用 Agent git 工具（akm_run_git，仅执行固定 operation）
    "agent_email_enabled": False,       # 是否启用 Agent 发邮件工具（akm_send_email）
    "agent_email_smtp_host": "",        # SMTP 服务器地址（如 smtp.qq.com）；留空表示未配置，工具不可用
    "agent_email_smtp_port": 465,       # SMTP 端口：465 走 SSL，587 走 STARTTLS（由 agent_email_smtp_ssl 决定是否启用 SSL）
    "agent_email_smtp_user": "",        # SMTP 登录账号
    "agent_email_smtp_password": "",    # SMTP 密码 / 授权码（敏感，akm_get_config 会脱敏）
    "agent_email_from": "",             # 默认发件人地址；留空则使用 SMTP 账号作为发件人
    "agent_email_smtp_ssl": True,       # 是否使用 SSL 加密连接（True=SMTP_SSL/465；False=STARTTLS/587）
    "agent_max_tool_calls": 30,         # 单次 Agent 请求最多执行的工具调用数，限制循环与资源消耗
    "agent_tool_retry_max_retries": 1,  # Agent 工具失败后的最大自愈修正轮次（0 关闭：失败结果照常回传，模型自主决定）
    "agent_api_token": "",              # /v1/agent 请求鉴权 token（Bearer）；留空表示不校验
    "agent_default_instructions": (
        "数学公式请使用 KaTeX 语法返回：行内公式用 \\(...\\)，"
        "独立公式用 \\[...\\]；公式内容请直接给出，不要用代码块包裹。"
    ),  # Agent 默认系统指令（客户端未传 instructions 时使用）
    "tavily_api_key": "",               # Tavily 联网搜索 API Key（Agent 内置 tavily_search 工具使用）
    # Key 管理
    "rate_limit_cooldown_sec": 60,      # 限流冷却秒数，被 429 后多久恢复可用
    # 审计队列
    "audit_queue_maxsize": 512,         # 审计日志异步队列上限，满后丢弃新增日志
    # 用量查询
    "usage_query_check_interval_sec": 60,  # 用量查询后台扫描间隔（秒）
    # 统计
    "stats_cache_ttl_sec": 60,          # 首页统计缓存有效期（秒）
}

# Agent 相关配置键（含仅 Agent tavily 搜索使用的 tavily_api_key）。
# config.json 中这些键归组在嵌套的 "agent_config" 对象下；内存层（load_config）
# 仍保持扁平展开，业务代码无感。归组仅影响磁盘文件布局。
AGENT_GROUP_KEYS: list[str] = [
    "agent_max_turns",
    "agent_max_context_tokens",
    "agent_keep_recent_messages",
    "agent_context_warning_ratio",
    "agent_upload_dir",
    "agent_workspace_root",
    "agent_write_tools_enabled",
    "agent_run_shell_enabled",
    "agent_git_enabled",
    "agent_email_enabled",
    "agent_email_smtp_host",
    "agent_email_smtp_port",
    "agent_email_smtp_user",
    "agent_email_smtp_password",
    "agent_email_from",
    "agent_email_smtp_ssl",
    "agent_max_tool_calls",
    "agent_tool_retry_max_retries",
    "agent_api_token",
    "agent_default_instructions",
    "tavily_api_key",
]


def normalize_http_proxy_url(raw: object) -> str:
    """规范化出站代理 URL：去空白；空串表示不使用代理。

    仅做轻量整理：host:port 自动补 http://；其余原样返回，由 httpx 在建连时校验。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    lower = text.lower()
    allowed = ("http://", "https://", "socks5://", "socks5h://", "socks4://")
    if lower.startswith(allowed):
        return text
    # 常见误填 host:port 时补默认协议，降低设置页录入成本
    if "://" not in text and text[0].isalnum():
        return f"http://{text}"
    return text


def _safe_int(raw: object, default: int) -> int:
    """将配置值转为整数；类型错误时回退默认值，避免手工配置阻断服务启动。"""
    try:
        return default if raw is None or raw == "" else int(raw)
    except (TypeError, ValueError):
        return default


def _safe_float(raw: object, default: float) -> float:
    """将配置值转为浮点数；类型错误时回退默认值，和整数配置保持一致。"""
    try:
        return default if raw is None or raw == "" else float(raw)
    except (TypeError, ValueError):
        return default


def resolve_http_proxy_url(cfg: dict | None = None) -> str | None:
    """根据配置返回生效的代理 URL；未启用或为空时返回 None。"""
    data = cfg if isinstance(cfg, dict) else load_config()
    if data.get("http_proxy_enabled") is not True:
        return None
    url = normalize_http_proxy_url(data.get("http_proxy_url", ""))
    return url or None



def _normalize_cost_pricing_table(raw: object) -> str:
    """将历史四段单价表转换为当前固定美元的三段格式。

    旧版本把币种写在每一行末尾；现在币种固定为美元符号，保留前三个
    价格字段即可。注释、空行及不符合旧格式的内容原样保留，让前端继续
    展示并由单价解析器统一决定其是否有效。
    """
    lines = []
    for line in str(raw or "").splitlines():
        if "=" not in line:
            lines.append(line)
            continue
        model, prices = line.split("=", 1)
        parts = [part.strip() for part in prices.split("/")]
        if len(parts) == 4:
            lines.append(f"{model}={'/'.join(parts[:3])}")
            continue
        lines.append(line)
    return "\n".join(lines)


def _ensure_dir() -> None:
    """确保配置目录存在"""
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_config() -> dict:
    """读取配置，缺失项用默认值补全"""
    _ensure_dir()
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {}
    # 合并默认值
    merged = dict(DEFAULTS)
    merged.update(data)
    # 兼容 config.json 的 agent_config 嵌套归组：展开到顶层（嵌套值优先于顶层旧键）
    _agent_group = data.get("agent_config") if isinstance(data, dict) else None
    if isinstance(_agent_group, dict):
        for _key in AGENT_GROUP_KEYS:
            if _key in _agent_group:
                merged[_key] = _agent_group[_key]
    # 内存层保持扁平，不把嵌套 agent_config 留在顶层
    merged.pop("agent_config", None)
    merged["cost_pricing_table"] = _normalize_cost_pricing_table(merged["cost_pricing_table"])
    merged["http_proxy_enabled"] = merged.get("http_proxy_enabled") is True
    merged["http_proxy_url"] = normalize_http_proxy_url(merged.get("http_proxy_url", ""))
    # 连接池参数：确保为合理整数，防止配置异常导致连接池初始化失败
    merged["http_client_max_connections"] = max(1, int(merged.get("http_client_max_connections", 8) or 8))
    merged["http_client_max_keepalive"] = max(0, int(merged.get("http_client_max_keepalive", 2) or 2))
    merged["http_client_max_pools"] = max(1, int(merged.get("http_client_max_pools", 64) or 64))
    merged["http_client_idle_ttl_sec"] = max(30.0, float(merged.get("http_client_idle_ttl_sec", 120.0) or 120.0))
    merged["http_client_connect_timeout_sec"] = max(1.0, float(merged.get("http_client_connect_timeout_sec", 10.0) or 10.0))
    # 代理转发参数
    merged["proxy_max_key_tries"] = max(1, int(merged.get("proxy_max_key_tries", 20) or 20))
    merged["proxy_max_retries_per_key"] = max(0, int(merged.get("proxy_max_retries_per_key", 2) or 2))
    merged["proxy_retry_backoff_base_sec"] = max(0.1, float(merged.get("proxy_retry_backoff_base_sec", 0.5) or 0.5))
    merged["proxy_default_timeout_sec"] = max(30.0, float(merged.get("proxy_default_timeout_sec", 120.0) or 120.0))
    merged["proxy_first_byte_timeout_sec"] = max(0.0, _safe_float(merged.get("proxy_first_byte_timeout_sec"), 12.0))
    # Agent Loop
    merged["agent_max_turns"] = max(1, _safe_int(merged.get("agent_max_turns"), 100))
    merged["agent_max_context_tokens"] = max(0, _safe_int(merged.get("agent_max_context_tokens"), 272000))
    merged["agent_keep_recent_messages"] = max(2, _safe_int(merged.get("agent_keep_recent_messages"), 10))
    merged["agent_context_warning_ratio"] = max(0.0, min(1.0, _safe_float(merged.get("agent_context_warning_ratio"), 0.8)))
    # Agent 文件工具开关：布尔归一化，防止配置为字符串/数字时误判
    merged["agent_write_tools_enabled"] = merged.get("agent_write_tools_enabled") is True
    merged["agent_run_shell_enabled"] = merged.get("agent_run_shell_enabled") is True
    merged["agent_git_enabled"] = merged.get("agent_git_enabled") is True
    merged["agent_email_enabled"] = merged.get("agent_email_enabled") is True
    merged["agent_email_smtp_ssl"] = merged.get("agent_email_smtp_ssl") is not False
    merged["agent_email_smtp_port"] = max(1, _safe_int(merged.get("agent_email_smtp_port"), 465))
    merged["agent_max_tool_calls"] = max(1, _safe_int(merged.get("agent_max_tool_calls"), 30))
    merged["agent_tool_retry_max_retries"] = max(0, _safe_int(merged.get("agent_tool_retry_max_retries"), 1))
    # Key 管理
    merged["rate_limit_cooldown_sec"] = max(1, int(merged.get("rate_limit_cooldown_sec", 60) or 60))
    # 审计队列
    merged["audit_queue_maxsize"] = max(1, int(merged.get("audit_queue_maxsize", 512) or 512))
    # 用量查询
    merged["usage_query_check_interval_sec"] = max(10, int(merged.get("usage_query_check_interval_sec", 60) or 60))
    # 统计
    merged["stats_cache_ttl_sec"] = max(1, int(merged.get("stats_cache_ttl_sec", 60) or 60))
    return merged


def save_config(data: dict) -> None:
    """保存配置（合并写入）"""
    _ensure_dir()
    # 先把调用方可能传入的 agent_config 嵌套对象展开进顶层，再走统一归一化，
    # 避免嵌套的 agent 配置在后续 update/打包中丢失。
    data = dict(data)
    _agent_group = data.pop("agent_config", None)
    if isinstance(_agent_group, dict):
        for _key in AGENT_GROUP_KEYS:
            # 嵌套值仅作为底层默认：调用方在 data 顶层显式给出的扁平键优先，避免覆盖
            if _key in _agent_group and _key not in data:
                data[_key] = _agent_group[_key]
    current = load_config()
    current.update(data)
    current["http_proxy_enabled"] = current.get("http_proxy_enabled") is True
    current["http_proxy_url"] = normalize_http_proxy_url(current.get("http_proxy_url", ""))
    # 连接池参数：确保为合理整数/浮点，防止配置异常导致连接池初始化失败
    current["http_client_max_connections"] = max(1, int(current.get("http_client_max_connections", 8) or 8))
    current["http_client_max_keepalive"] = max(0, int(current.get("http_client_max_keepalive", 2) or 2))
    current["http_client_max_pools"] = max(1, int(current.get("http_client_max_pools", 64) or 64))
    current["http_client_idle_ttl_sec"] = max(30.0, float(current.get("http_client_idle_ttl_sec", 120.0) or 120.0))
    current["http_client_connect_timeout_sec"] = max(1.0, float(current.get("http_client_connect_timeout_sec", 10.0) or 10.0))
    # 代理转发参数
    current["proxy_max_key_tries"] = max(1, int(current.get("proxy_max_key_tries", 20) or 20))
    current["proxy_max_retries_per_key"] = max(0, int(current.get("proxy_max_retries_per_key", 2) or 2))
    current["proxy_retry_backoff_base_sec"] = max(0.1, float(current.get("proxy_retry_backoff_base_sec", 0.5) or 0.5))
    current["proxy_default_timeout_sec"] = max(30.0, float(current.get("proxy_default_timeout_sec", 120.0) or 120.0))
    current["proxy_first_byte_timeout_sec"] = max(0.0, _safe_float(current.get("proxy_first_byte_timeout_sec"), 12.0))
    # Agent Loop
    current["agent_max_turns"] = max(1, _safe_int(current.get("agent_max_turns"), 100))
    current["agent_max_context_tokens"] = max(0, _safe_int(current.get("agent_max_context_tokens"), 272000))
    current["agent_keep_recent_messages"] = max(2, _safe_int(current.get("agent_keep_recent_messages"), 10))
    current["agent_context_warning_ratio"] = max(0.0, min(1.0, _safe_float(current.get("agent_context_warning_ratio"), 0.8)))
    current["agent_write_tools_enabled"] = current.get("agent_write_tools_enabled") is True
    current["agent_run_shell_enabled"] = current.get("agent_run_shell_enabled") is True
    current["agent_git_enabled"] = current.get("agent_git_enabled") is True
    current["agent_email_enabled"] = current.get("agent_email_enabled") is True
    current["agent_email_smtp_ssl"] = current.get("agent_email_smtp_ssl") is not False
    current["agent_email_smtp_port"] = max(1, _safe_int(current.get("agent_email_smtp_port"), 465))
    current["agent_max_tool_calls"] = max(1, _safe_int(current.get("agent_max_tool_calls"), 30))
    current["agent_tool_retry_max_retries"] = max(0, _safe_int(current.get("agent_tool_retry_max_retries"), 1))
    # Key 管理
    current["rate_limit_cooldown_sec"] = max(1, int(current.get("rate_limit_cooldown_sec", 60) or 60))
    # 审计队列
    current["audit_queue_maxsize"] = max(1, int(current.get("audit_queue_maxsize", 512) or 512))
    # 用量查询
    current["usage_query_check_interval_sec"] = max(10, int(current.get("usage_query_check_interval_sec", 60) or 60))
    # 统计
    current["stats_cache_ttl_sec"] = max(1, int(current.get("stats_cache_ttl_sec", 60) or 60))
    # Agent 配置归组：写入嵌套 agent_config 对象，磁盘顶层不再保留 agent_* 键
    current["agent_config"] = {_key: current.pop(_key) for _key in AGENT_GROUP_KEYS if _key in current}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)


def get(key: str, default=None):
    """读取单个配置项"""
    cfg = load_config()
    return cfg.get(key, default)
