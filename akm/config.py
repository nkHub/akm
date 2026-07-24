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
    "use_native_user_agent": False,  # 是否透传客户端原始 User-Agent；默认继续使用 akm/<version>
    # 出站 HTTP 代理：仅作用于 AKM 访问上游供应商的请求，不是系统 VPN
    "http_proxy_enabled": False,
    "http_proxy_url": "",  # 例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
}


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
    merged["cost_pricing_table"] = _normalize_cost_pricing_table(merged["cost_pricing_table"])
    merged["http_proxy_enabled"] = merged.get("http_proxy_enabled") is True
    merged["http_proxy_url"] = normalize_http_proxy_url(merged.get("http_proxy_url", ""))
    return merged


def save_config(data: dict) -> None:
    """保存配置（合并写入）"""
    _ensure_dir()
    current = load_config()
    current.update(data)
    current["http_proxy_enabled"] = current.get("http_proxy_enabled") is True
    current["http_proxy_url"] = normalize_http_proxy_url(current.get("http_proxy_url", ""))
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)


def get(key: str, default=None):
    """读取单个配置项"""
    cfg = load_config()
    return cfg.get(key, default)
