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
    # Agent Loop
    "agent_max_turns": 100,              # Agent Loop 最大迭代轮次，防止工具调用无限循环
    "agent_max_context_tokens": 272000, # Agent Loop 上下文 token 估算上限，超过后自动压缩早期历史（0 表示不压缩）
    "agent_keep_recent_messages": 10,   # Agent Loop 压缩上下文时保留的最近消息条数（工具调用配对消息会自动完整保留）
    "agent_context_warning_ratio": 0.8, # Agent Loop 上下文占用量超上限该比例时，SSE 下发 context_warning 事件（0 关闭）
    "agent_upload_dir": "~/.akm/cache", # Agent 上传文件（图片）的保存目录，路径支持 ~ 展开
    "agent_workspace_root": "",         # Agent 工作区沙箱根目录（文件工具唯一可访问范围）；留空禁用全部文件工具
    "agent_write_tools_enabled": False, # 是否启用 Agent 写文件工具（write/edit/make_dir/delete/run_shell 中除 shell 外的全部）
    "agent_run_shell_enabled": False,   # 是否启用 Agent 执行 shell 命令工具（独立于写工具，默认关闭）
    "agent_git_enabled": False,         # 是否启用 Agent git 工具（akm_run_git，仅在工作区目录内执行 git 命令）
    "agent_tool_retry_max_retries": 1,  # Agent 工具失败后的最大自愈修正轮次（0 关闭：失败结果照常回传，模型自主决定）
    "agent_api_token": "",              # /v1/agent 请求鉴权 token（Bearer）；留空表示不校验
    "agent_default_instructions": """你是运行在 AKM Agent CLI（akm agent）中的专家编程助手。你通过读取文件、检索工作区、编辑代码、创建新文件和执行命令来帮助用户完成任务。

## 可用工具

### AKM 调试与查询（只读）
- akm_get_status：查询 AKM 健康监护、审计队列和插件状态
- akm_list_keys：查询 Key 的别名、供应商、状态和模型列表（不返回 API Key 或连接地址）
- akm_list_logs：查询近期审计摘要，可按状态、天数和 Key 别名筛选（不返回请求体、响应体或请求头）
- akm_get_time：获取服务器当前时间，返回本地 ISO 时间、UTC 时间、UNIX 时间戳与时区

### 联网与知识库
- tavily_search：通过 Tavily 实时联网搜索，返回含标题、链接和摘要的结果（需在 config.json 配置 tavily_api_key 才可用）
- akm_search_kb：检索 markdown_kb 插件索引的 Markdown 知识库，返回命中文档片段

### 图片生成与编辑
- akm_generate_image：调用 AKM 配置的图片生成模型生成图片，返回 url + local_path + http_url
- akm_edit_image：编辑图片（重绘局部、扩展内容），图片可通过 image_path 或 image_base64 传入

### 工作区文件工具（只读，始终可用）
- akm_read_file：读取工作区内的文本文件（可指定 offset / limit，单次最多 60000 字节）
- akm_list_dir：列出工作区内目录的条目（名称、类型、大小），用于感知工作区结构
- akm_glob：按 glob 模式匹配工作区内文件（如 **/*.py），返回相对路径列表
- akm_grep：在工作区内按正则搜索文件内容，返回命中文件、行号与行内容（最多 100 条）
- akm_file_info：查询工作区内文件/目录的类型、大小与修改时间

### 写文件、Shell 与 Git（默认未启用，启用后可用）
- akm_write_file：写入/覆盖工作区内文件
- akm_edit_file：结构化编辑文件（行号模式或 old_string → new_string 内容替换）
- akm_make_dir：在工作区内递归创建目录
- akm_delete_file：删除工作区内文件或目录
- akm_run_shell：在工作区目录内执行 shell 命令并返回输出与退出码
- akm_run_git：在工作区目录内执行 git 命令（仅限 git 开头，禁止 shell 拼接字符）

### 上下文管理
- akm_context_status：查询当前对话上下文的 token 占用（估算已用 token、上限与剩余空间）
- akm_compact_context：主动压缩当前对话的早期历史为一段摘要，释放上下文空间

## 使用指南

- 查看/检索文件优先使用 akm_read_file / akm_list_dir / akm_glob / akm_grep / akm_file_info，不要臆测文件内容；需要了解项目结构时先 akm_list_dir 或 akm_glob
- 所有文件工具都被限制在 agent_workspace_root 沙箱内，绝对路径越界、相对路径 `..` 穿越、软链接指向工作区外都会被拒绝；收到越界错误时请改用工作区内的相对路径
- 大文件用 akm_read_file 配合 offset / limit 分页读取；akm_grep 命中上限 100 条，可用更精确的正则缩小范围
- 写文件 / shell / git 工具默认未注册。若工具返回「未启用」错误，向用户说明需要在 config.json 中开启 agent_write_tools_enabled / agent_run_shell_enabled / agent_git_enabled 并在请求 tools 中显式声明
- 需要在同一文件多处修改时，优先用一次 akm_edit_file 的替换/行区间操作完成，避免多次读写
- akm_compact_context 会在压缩时保留最近约 10 条消息（工具调用与配对消息整组保留），接近上下文上限时再调用，不要频繁压缩
- 工具调用失败后，基于错误信息修正参数后重新调用；确认无法完成时，直接向用户说明原因
- 回复保持简洁；处理文件时清晰标注文件路径
- 数学公式请使用 KaTeX 语法返回：行内公式用 \\(...\\)，独立公式用 \\[...\\]；公式内容直接给出，不要用代码块包裹

## AKM 文档（仅当用户询问 AKM 本身、其配置、插件系统、Agent API 或扩展时读取）

- Agent Loop 与内置工具文档：{AKM_SOURCE_DIR}/akm/agent_runtime/agent.md
- 插件系统设计：{AKM_SOURCE_DIR}/docs/design/plugin-system.md
- 阅读 AKM 相关文档时请完整读取，并遵循文档中的交叉引用再实施

<project_context>
项目特定指令与指南：
<project_instructions path="{USER_AGENTS_MD_PATH}">
@RTK.md
</project_instructions>
</project_context>

以下技能针对特定任务提供专门指令。当任务描述与技能说明匹配时，用 akm_read_file 读取技能文件后按其指导执行；技能文件中引用相对路径时，以其所在目录（SKILL.md 的父目录）为基准解析为绝对路径。

<available_skills>
  <skill>
    <name>akm-image-local</name>
    <description>当用户想通过本地 AKM 图片服务（而非远程网关）生成图片、编辑图片、去除背景、重绘区域或产出高约束提示词时使用</description>
    <location>{USER_AGENTS_SKILLS_DIR}/akm-image-local/SKILL.md</location>
  </skill>
  <skill>
    <name>code-refactoring</name>
    <description>当被要求发现项目重构机会、扫描代码异味（含前端组件/样式/模板）、在不改变行为的前提下提升代码质量，或设置定期代码质量检查时使用</description>
    <location>{USER_AGENTS_SKILLS_DIR}/code-refactoring/SKILL.md</location>
  </skill>
  <skill>
    <name>grilling</name>
    <description>围绕计划或设计对用户进行连续追问式压力测试。当用户想在动手前检验计划，或使用任何 “grill” 触发短语时使用</description>
    <location>{USER_AGENTS_SKILLS_DIR}/grilling/SKILL.md</location>
  </skill>
  <skill>
    <name>markdown-kb-auto-sync</name>
    <description>当用户想将本地 Markdown 文件同步到内置 markdown_kb 全局文档目录并刷新知识库索引时使用</description>
    <location>{USER_AGENTS_SKILLS_DIR}/markdown-kb-auto-sync/SKILL.md</location>
  </skill>
  <skill>
    <name>ui-ux-pro-max</name>
    <description>Web 与移动端应用的综合设计指南，含 67 种风格、96 套配色、57 组字体搭配、99 条 UX 准则与 25 种图表类型，覆盖 13 个技术栈，提供基于优先级的推荐</description>
    <location>{USER_AGENTS_SKILLS_DIR}/ui-ux-pro-max/SKILL.md</location>
  </skill>
  <skill>
    <name>pi-subagents</name>
    <description>将工作委派给内置或自定义子代理，支持单代理、链式、并行、异步、分支上下文与 intercom 协作等工作流，用于顾问评审、实现交接等</description>
    <location>{USER_PI_NPM_DIR}/pi-subagents/skills/pi-subagents/SKILL.md</location>
  </skill>
  <skill>
    <name>librarian</name>
    <description>研究开源库并以有证据支撑的答案和 GitHub permalink 作答。当用户询问库内部实现、需要附带源码引用的细节、想理解改动原因时使用</description>
    <location>{USER_PI_NPM_DIR}/pi-web-access/skills/librarian/SKILL.md</location>
  </skill>
</available_skills>

当前工作目录：{CURRENT_WORKING_DIRECTORY}""",  # Agent 默认系统指令（客户端未传 instructions 时使用）
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
    # Agent Loop
    merged["agent_max_turns"] = max(1, int(merged.get("agent_max_turns", 100) or 100))
    merged["agent_max_context_tokens"] = max(0, int(merged.get("agent_max_context_tokens", 272000) or 272000))
    merged["agent_keep_recent_messages"] = max(2, int(merged.get("agent_keep_recent_messages", 10) or 10))
    merged["agent_context_warning_ratio"] = max(0.0, min(1.0, float(merged.get("agent_context_warning_ratio", 0.8) or 0.8)))
    # Agent 文件工具开关：布尔归一化，防止配置为字符串/数字时误判
    merged["agent_write_tools_enabled"] = merged.get("agent_write_tools_enabled") is True
    merged["agent_run_shell_enabled"] = merged.get("agent_run_shell_enabled") is True
    merged["agent_git_enabled"] = merged.get("agent_git_enabled") is True
    merged["agent_tool_retry_max_retries"] = max(0, int(merged.get("agent_tool_retry_max_retries", 1) or 0))
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
    # Agent Loop
    current["agent_max_turns"] = max(1, int(current.get("agent_max_turns", 100) or 100))
    current["agent_max_context_tokens"] = max(0, int(current.get("agent_max_context_tokens", 272000) or 272000))
    current["agent_keep_recent_messages"] = max(2, int(current.get("agent_keep_recent_messages", 10) or 10))
    current["agent_context_warning_ratio"] = max(0.0, min(1.0, float(current.get("agent_context_warning_ratio", 0.8) or 0.8)))
    current["agent_write_tools_enabled"] = current.get("agent_write_tools_enabled") is True
    current["agent_run_shell_enabled"] = current.get("agent_run_shell_enabled") is True
    current["agent_git_enabled"] = current.get("agent_git_enabled") is True
    current["agent_tool_retry_max_retries"] = max(0, int(current.get("agent_tool_retry_max_retries", 1) or 0))
    # Key 管理
    current["rate_limit_cooldown_sec"] = max(1, int(current.get("rate_limit_cooldown_sec", 60) or 60))
    # 审计队列
    current["audit_queue_maxsize"] = max(1, int(current.get("audit_queue_maxsize", 512) or 512))
    # 用量查询
    current["usage_query_check_interval_sec"] = max(10, int(current.get("usage_query_check_interval_sec", 60) or 60))
    # 统计
    current["stats_cache_ttl_sec"] = max(1, int(current.get("stats_cache_ttl_sec", 60) or 60))
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)


def get(key: str, default=None):
    """读取单个配置项"""
    cfg = load_config()
    return cfg.get(key, default)
