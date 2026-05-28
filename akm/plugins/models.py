"""插件元数据模型"""
from pydantic import BaseModel


class SettingDef(BaseModel):
    """单个配置项定义"""
    key: str
    label: str
    type: str = "text"          # number / boolean / select / text
    default: str | int | bool = ""
    description: str = ""
    options: list[str] = []      # select 类型时的选项列表
    min: int | None = None
    max: int | None = None


class PluginMeta(BaseModel):
    """插件的 plugin.json 映射"""
    name: str
    version: str
    has_menu: bool = False
    category: str = ""           # filter / matcher / converter / handler / post / app
    description: str = ""
    builtin: bool = False        # 内置插件标记
    required: bool = False       # 不可禁用
    priority: int = 100          # 同 hook 执行优先级，越小越先，0-999
    menu: dict = {}
    routes_prefix: str = ""
    hooks: dict = {
        "on_request": False,
        "on_key_selected": False,
        "on_upstream_error": False,
        "on_response": False
    }
    settings: list[SettingDef] = []
    converts: list[dict] = []    # [{ "from": "responses", "to": "chat" }, ...]
