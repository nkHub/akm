# Agent 对象重构计划

## 目标

将分散在 4 个文件中的 provider 逻辑统一到 `Agent` 对象中，支持链接管理、协议转换、认证构建，为后续接入 Anthropic/Responses 转换器提供架构基础。

---

## 一、当前问题清单

### 1.1 逻辑分散

| 逻辑 | 分散在 |
|------|--------|
| base_url 默认值 | `models.py`(DEFAULT_BASE_URLS) + `proxy.py`(2处兜底) + `key_pool.py`(add_key) |
| URL 拼接 | `proxy.py`(_build_upstream_url) + `proxy.py`(forward_request) + `proxy.py`(test_connectivity) |
| auth_header 构建 | `proxy.py`(forward_request:163) + `proxy.py`(test_connectivity:292) 完全相同 |
| `"Bearer {api_key}"` 默认值 | `server.py:171` + `key_pool.py:53` + `proxy.py:163` + `proxy.py:292` 共 4 处 |

### 1.2 硬编码

```python
# proxy.py:108
base_url = "https://api.openai.com"  # 兜底硬编码

# proxy.py:162
key.get("base_url") or "https://api.openai.com"  # 再次硬编码
```

### 1.3 模型不一致

`KeyConfig` Pydantic 模型缺少 `auth_header` 字段，但数据库表有，实际代码也在用。

### 1.4 无扩展点

新增供应商只需改 `DEFAULT_BASE_URLS`，但 URL/认证逻辑没有统一抽象，协议转换（Anthropic/Responses）无处挂载。

---

## 二、Agent 对象设计

### 2.1 核心类

```python
# akm/agent.py（新建）

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Agent:
    """上游 AI 供应商代理，封装 URL、认证、协议转换"""

    # ── 基本属性 ──
    name: str                          # "openai" / "deepseek" / "anthropic"
    default_base_url: str
    default_auth_header: str = "Bearer {api_key}"

    # ── 协议适配能力 ──
    supports_responses: bool = False    # 是否原生支持 Responses API
    supports_chat: bool = True          # 是否支持 Chat Completions
    supports_messages: bool = False     # 是否原生支持 Anthropic Messages API

    # ── 协议转换器（可选，按需注册）──
    chat_to_responses: Optional['BaseAdapter'] = None
    messages_to_chat: Optional['BaseAdapter'] = None
    chat_to_messages: Optional['BaseAdapter'] = None

    # ── URL 构建 ──
    def resolve_url(self, key: dict, api_path: str) -> str:
        """
        根据 Key 的配置解析最终上游 URL。
        优先级: key.base_url > agent.default_base_url > "https://api.openai.com"
        """
        base = (key.get("base_url") or "").rstrip("/") or self.default_base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/{api_path}"
        return f"{base}/v1/{api_path}"

    # ── 认证头构建 ──
    def build_headers(self, key: dict) -> dict:
        """
        根据 Key 配置构建请求头（含 Authorization）。
        模板 {api_key} 会被替换为解密后的 Key。
        """
        template = key.get("auth_header") or self.default_auth_header
        return {
            "Authorization": template.format(api_key=key["api_key"]),
            "Content-Type": "application/json",
        }

    # ── 协议转换判断 ──
    def needs_conversion(self, api_path: str) -> Optional[str]:
        """
        判断是否需要协议转换。
        返回 None 表示不需要，返回目标 api_path 表示需要转换。
        """
        if api_path == "responses" and not self.supports_responses:
            if self.chat_to_responses:
                return "chat/completions"  # 内部转为 chat 格式，响应再转回
        if api_path == "messages" and not self.supports_messages:
            if self.messages_to_chat:
                return "chat/completions"
        if api_path == "chat/completions" and not self.supports_chat:
            if self.chat_to_messages:
                return "messages"
        return None


# ── 全局注册表 ──
AGENT_REGISTRY: dict[str, Agent] = {
    "openai": Agent(
        name="openai",
        default_base_url="https://api.openai.com",
        supports_responses=True,
        supports_chat=True,
    ),
    "deepseek": Agent(
        name="deepseek",
        default_base_url="https://api.deepseek.com",
        supports_responses=False,
        supports_chat=True,
    ),
    "anthropic": Agent(
        name="anthropic",
        default_base_url="https://api.anthropic.com",
        supports_messages=True,
        supports_chat=False,
    ),
    # 未来扩展示例：
    # "gemini": Agent("gemini", "https://generativelanguage.googleapis.com", supports_chat=False),
}


def get_agent(provider: str) -> Agent:
    """根据 provider 名获取 Agent，未知的返回 openai 兜底"""
    return AGENT_REGISTRY.get(provider, AGENT_REGISTRY["openai"])
```

### 2.2 协议转换适配器接口

```python
# akm/adapter.py（新建，之前设计的两个转换器实现在此）

from abc import ABC, abstractmethod


class BaseAdapter(ABC):
    """协议转换适配器基类"""

    @abstractmethod
    def convert_request(self, body: dict) -> dict:
        """转换请求体"""
        ...

    @abstractmethod
    def convert_response(self, body: str) -> str:
        """转换非流式响应体"""
        ...

    @abstractmethod
    async def convert_sse_stream(self, upstream_stream):
        """转换流式 SSE 响应（生成器）"""
        ...


# 具体适配器在独立模块中实现：
# akm/adapters/chat_to_responses.py
# akm/adapters/anthropic_messages.py
```

### 2.3 文件结构变更

```
akm/
├── agent.py              ← 新增：Agent + AGENT_REGISTRY
├── adapter.py            ← 新增：BaseAdapter 接口
├── adapters/             ← 新增：转换器实现目录
│   ├── __init__.py
│   ├── chat_to_responses.py
│   └── anthropic_messages.py
├── server.py             ← 删除 _build_upstream_url 逻辑，改用 agent
├── proxy.py              ← 删除 _build_upstream_url、auth 重复逻辑
├── key_pool.py           ← add_key 改用 agent.default_base_url
├── models.py             ← 删除 DEFAULT_BASE_URLS、补齐 auth_header
├── db.py                 ← 不变
├── audit.py              ← 不变
└── config.py             ← 不变
```

---

## 三、分步重构计划

### 阶段 1：补齐模型 + 创建 Agent（不改变行为）

**文件：** `models.py`

```diff
- DEFAULT_BASE_URLS = {"openai": "...", "deepseek": "..."}

 class KeyConfig(BaseModel):
     alias: str
     provider: str
     api_key: str
     base_url: str | None = None
+    auth_header: str = "Bearer {api_key}"    # 补齐
     models: str = "*"
     priority: int = 0
     status: str = "active"
```

**文件：** `agent.py`（新建）→ 实现 `Agent` + `AGENT_REGISTRY` + `get_agent()`

**验证：** 单元测试确保 `resolve_url`、`build_headers` 与旧逻辑一致

---

### 阶段 2：替换 proxy.py 中的散落逻辑

**文件：** `proxy.py`

```diff
- def _build_upstream_url(base_url, api_path):
-     if not base_url:
-         base_url = "https://api.openai.com"  # 兜底
-     base = base_url.rstrip("/")
-     if base.endswith("/v1"):
-         return f"{base}/{api_path}"
-     return f"{base}/v1/{api_path}"

+ from akm.agent import get_agent

  async def forward_request(body, client, ...):
      ...
-     url = _build_upstream_url(key.get("base_url") or "https://api.openai.com", api_path)
+     agent = get_agent(key["provider"])
+     url = agent.resolve_url(key, api_path)

-     auth_template = key.get("auth_header", "Bearer {api_key}")
-     headers = {
-         "Authorization": auth_template.format(api_key=key["api_key"]),
-         "Content-Type": "application/json",
-     }
+     headers = agent.build_headers(key)
```

同样修改 `test_key_connectivity` 中的重复逻辑。

**验证：** 现有测试全部通过

---

### 阶段 3：替换 key_pool 和 server 中的默认值

**文件：** `key_pool.py`

```diff
  def add_key(alias, provider, api_key, base_url=None, models="*",
-             auth_header="Bearer {api_key}", priority=0):
+             auth_header=None, priority=0):
+     agent = get_agent(provider)
+     if auth_header is None:
+         auth_header = agent.default_auth_header
      if base_url is None:
-         base_url = DEFAULT_BASE_URLS.get(provider, "")
+         base_url = agent.default_base_url
```

**文件：** `server.py`

```diff
  # add_key API
- auth_header=body.get("auth_header", "Bearer {api_key}")
+ auth_header=body.get("auth_header")  # None → Agent 内部处理
```

**验证：** E2E 测试通过

---

### 阶段 4：集成 Chat→Responses 转换器

**文件：** `adapters/chat_to_responses.py` → 实现 `ChatToResponsesAdapter(BaseAdapter)`

**文件：** `proxy.py`

```diff
+ from akm.adapters.chat_to_responses import ChatToResponsesAdapter

  async def forward_request(body, client, ...):
      ...
+     # 协议转换判断
+     target_path = agent.needs_conversion(api_path)
+     if target_path:
+         adapter = agent.chat_to_responses
+         upstream_body = adapter.convert_request(body)
+         upstream_path = target_path
+     else:
+         upstream_body = body
+         upstream_path = api_path

      url = agent.resolve_url(key, upstream_path)
      ...
```

**流式转换接入：**

```diff
  if client_wants_stream:
+     if target_path:
+         # 转换后的流式响应需要再转回来
+         resp_stream = ...  # 原始 SSE 流
+         converted_stream = adapter.convert_sse_stream(resp_stream)
+         return {"stream": True, "response": 包装后的转换流, ...}
      return {"stream": True, "response": resp, ...}
```

**验证：** Codex + DeepSeek 端到端可用

---

## 四、重构前后对比

### 4.1 代码量变化

| 文件 | 改前 | 改后 | 变化 |
|------|------|------|------|
| `models.py` | 5 行默认值 | 删除 | -5 |
| `agent.py` | 不存在 | ~60 行 | +60 |
| `adapter.py` | 不存在 | ~20 行 | +20 |
| `proxy.py` | ~338 行 | ~300 行 | -38 |
| `key_pool.py` | ~252 行 | ~248 行 | -4 |
| `server.py` | ~629 行 | ~627 行 | -2 |

净增约 30 行，但消除了 4 处硬编码重复和 2 处逻辑重复。

### 4.2 扩展性

```
改前：新增供应商
  1. models.py 改 DEFAULT_BASE_URLS
  2. proxy.py 改兜底值
  3. key_pool.py 改默认值

改后：新增供应商
  1. agent.py 加一行 AGENT_REGISTRY["xxx"] = Agent(...)
```

---

## 五、Agent 与转换器的关系图

```
┌──────────────────────────────────────────────────────────┐
│                     forward_request                       │
│                                                           │
│  1. pick_key → key dict                                   │
│  2. agent = get_agent(key["provider"])                    │
│                                                           │
│  3. agent.needs_conversion(api_path)                      │
│     ├─ None → 直接转发                                     │
│     └─ "chat/completions" →                               │
│        ├─ adapter.convert_request(body)                   │
│        ├─ 发到 /v1/chat/completions                       │
│        └─ adapter.convert_sse_stream(resp) → 流式返回     │
│                                                           │
│  4. agent.build_headers(key) → {"Authorization": "..."}   │
│  5. agent.resolve_url(key, api_path) → "https://..."      │
└──────────────────────────────────────────────────────────┘

                    Agent 实例
                    ┌──────────┐
                    │ "openai" │── supports_responses=True
                    │          │── chat_to_responses=None
                    └──────────┘

                    ┌──────────┐
                    │"deepseek"│── supports_responses=False
                    │          │── chat_to_responses=ChatToResponsesAdapter()
                    └──────────┘

                    ┌──────────┐
                    │"anthropic│── supports_messages=True
                    │          │── messages_to_chat=MessagesToChatAdapter() ← 未来
                    │          │── chat_to_messages=ChatToMessagesAdapter() ← 未来
                    └──────────┘
```

---

## 六、风险与注意事项

| 风险 | 应对 |
|------|------|
| `resolve_url` 行为差异 | 先写单测覆盖现有行为，再替换 |
| agent 切换时机 | 只在 `pick_key` 之后获取 agent，不会出现 key 和 agent 不匹配 |
| 第三方中转站 provider 未知 | `get_agent()` 兜底返回 `openai` Agent |
| 并发安全 | Agent 是无状态对象（dataclass），天然线程安全 |
| 转换器性能开销 | SSE 逐行转换，无缓冲，延迟增加 < 5ms |

---

## 七、实施顺序

```
Phase 1: 基础架构（30min）
  ├─ 创建 agent.py（Agent + 注册表）
  ├─ 创建 adapter.py（BaseAdapter 接口）
  └─ 修改 models.py（删除 DEFAULT_BASE_URLS，补齐 auth_header）

Phase 2: 替换散落逻辑（20min）
  ├─ proxy.py：_build_upstream_url → agent.resolve_url
  ├─ proxy.py：auth 构建 → agent.build_headers
  ├─ key_pool.py：DEFAULT_BASE_URLS → agent.default_base_url
  └─ server.py：auth_header 默认值 → agent.default_auth_header

Phase 3: Chat→Responses 转换器（2h）
  ├─ 实现 ChatToResponsesAdapter
  ├─ 注册到 deepseek Agent
  └─ proxy.py 集成转换逻辑

Phase 4: 测试验证（30min）
  ├─ 单元测试：Agent URL/Headers
  ├─ 集成测试：Chat→Responses 双向转换
  └─ E2E 测试：Codex + DeepSeek 可用
```
