"""插件元数据模型"""
from pydantic import BaseModel


class SettingDef(BaseModel):
    """单个配置项定义"""
    key: str
    label: str
    type: str = "text"          # number / boolean / select / text
    default: str | int | float | bool = ""
    description: str = ""
    options: list[str] = []      # select 类型时的选项列表
    options_source: str = ""     # select 动态数据源，例如 /v1/models
    allow_empty_option: bool = False
    empty_option_label: str = ""
    min: int | float | None = None
    max: int | float | None = None


class PluginMeta(BaseModel):
    """插件的 plugin.json 映射"""
    name: str
    version: str
    has_menu: bool = False
    category: str = ""           # filter / matcher / converter / handler / post / app
    description: str = ""
    builtin: bool = False        # 内置插件标记
    default_enabled: bool = True # 首次加载且无显式状态时是否默认启用
    required: bool = False       # 不可禁用
    priority: int = 100          # 同 hook 执行优先级，越小越先，0-999
    menu: dict = {}
    routes_prefix: str = ""
    settings_columns: int = 1   # 配置表单列数，默认单列；当前约定仅支持 1 或 2
    hooks: dict = {
        "on_request": False,
        "on_key_selected": False,
        "on_upstream_error": False,
        "on_response": False
    }
    settings: list[SettingDef] = []
    converts: list[dict] = []    # [{ "from": "responses", "to": "chat" }, ...]
