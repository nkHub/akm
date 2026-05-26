"""数据模型定义"""

from pydantic import BaseModel


class KeyConfig(BaseModel):
    """Key 配置数据模型"""
    alias: str
    provider: str               # openai / deepseek
    api_key: str
    base_url: str | None = None
    auth_header: str = "Bearer {api_key}"  # 认证头模板，{api_key} 会被替换
    models: str = "*"           # 支持的模型，逗号分隔，* 表示全部
    priority: int = 0
    status: str = "active"      # active / disabled / rate_limited


class AuditRecord(BaseModel):
    """审计日志数据模型"""
    id: int | None = None
    timestamp: str = ""
    provider: str = ""
    key_alias: str = ""
    model: str = ""
    request_body: str = ""
    response_body: str = ""
    status_code: int = 0
    latency_ms: int = 0
    error: str = ""
