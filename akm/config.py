"""配置管理 — 读写 ~/.akm/config.json"""

import json
import os

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
    "json_viewer_max_text_length": 600000,  # JSON 查看器超长文本阈值（超过后仅允许下载原文）
    "image_supported_models": "gpt-image-2",  # 图片生成/编辑支持的模型列表（逗号分隔，首项作为默认值）
    "image_request_timeout_sec": 300,  # 图片生成/编辑请求超时（秒），默认比聊天接口更宽松
    "wake_recover_delay_sec": 8,  # 菜单栏应用在系统唤醒后等待网络/VPN恢复的秒数
}


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
    return merged


def save_config(data: dict) -> None:
    """保存配置（合并写入）"""
    _ensure_dir()
    current = load_config()
    current.update(data)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)


def get(key: str, default=None):
    """读取单个配置项"""
    cfg = load_config()
    return cfg.get(key, default)
