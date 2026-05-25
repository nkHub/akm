# AI Key Manager 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个本地 AI Key 管理代理服务，支持多供应商 key、优先级调度、自动故障切换、请求代理转发及完整审计日志。

**Architecture:** FastAPI HTTP 服务 + Click CLI 工具 + SQLite 存储。服务端接收 OpenAI 兼容的 `/v1/chat/completions` 请求，根据 model 匹配 key 池中优先级最高的可用 key 转发至上游，失败时自动切换和重试。CLI 独立操作数据库进行 key 和日志管理。

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, httpx, click, cryptography (Fernet), Pydantic, sqlite3

---

## 文件结构

```
ai-key-manager/
├── pyproject.toml
├── akm/
│   ├── __init__.py
│   ├── db.py              # SQLite 连接管理、建表
│   ├── models.py           # Pydantic 数据模型
│   ├── key_pool.py         # Key 加密存储、CRUD、状态管理
│   ├── proxy.py            # 请求转发、重试、故障切换
│   ├── audit.py            # 审计日志读写
│   ├── server.py           # FastAPI 应用
│   └── cli.py              # Click CLI 入口
├── tests/
│   ├── test_db.py
│   ├── test_key_pool.py
│   ├── test_proxy.py
│   ├── test_audit.py
│   └── test_server.py
```

`akm/__init__.py` — 空文件，仅标记 package。
`pyproject.toml` — 项目配置，声明依赖和 CLI 入口 `akm = "akm.cli:main"`。

---

### Task 1: 项目脚手架

**Files:**
- Create: `pyproject.toml`
- Create: `akm/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: 创建 pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"

[project]
name = "ai-key-manager"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "fastapi>=0.110.0",
    "uvicorn[standard]>=0.27.0",
    "httpx>=0.27.0",
    "click>=8.1.0",
    "cryptography>=42.0.0",
    "pydantic>=2.0.0",
]

[project.scripts]
akm = "akm.cli:main"

[tool.setuptools.packages.find]
where = ["."]
```

- [ ] **Step 2: 创建 akm/__init__.py 和 tests/__init__.py**

```bash
mkdir -p akm tests
touch akm/__init__.py tests/__init__.py
```

- [ ] **Step 3: 安装依赖并验证**

```bash
pip install -e .
python -c "import akm; print('ok')"
```

Expected: 输出 `ok`

- [ ] **Step 4: 提交**

```bash
git add pyproject.toml akm/__init__.py tests/__init__.py
git commit -m "feat: 初始化项目脚手架"
```

---

### Task 2: 数据库层

**Files:**
- Create: `akm/db.py`
- Create: `akm/models.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: 编写测试 — test_db.py**

```python
import os
import tempfile
import pytest
from akm.db import get_db_path, get_connection, init_db


@pytest.fixture
def temp_db(monkeypatch):
    """使用临时目录隔离测试数据库"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    yield tmpdir


def test_get_db_path(temp_db):
    path = get_db_path()
    assert path.endswith("akm.db")
    assert path.startswith(temp_db)


def test_init_db_creates_tables(temp_db):
    conn = get_connection()
    init_db(conn)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert tables == {"keys", "audit_logs"}
    conn.close()


def test_keys_table_schema(temp_db):
    conn = get_connection()
    init_db(conn)
    cursor = conn.execute("PRAGMA table_info(keys)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}
    assert columns["alias"] == "TEXT"
    assert columns["provider"] == "TEXT"
    assert columns["api_key"] == "TEXT"
    assert columns["base_url"] == "TEXT"
    assert columns["models"] == "TEXT"
    assert columns["priority"] == "INTEGER"
    assert columns["status"] == "TEXT"
    conn.close()


def test_audit_logs_table_schema(temp_db):
    conn = get_connection()
    init_db(conn)
    cursor = conn.execute("PRAGMA table_info(audit_logs)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}
    assert columns["timestamp"] == "TEXT"
    assert columns["provider"] == "TEXT"
    assert columns["key_alias"] == "TEXT"
    assert columns["model"] == "TEXT"
    assert columns["request_body"] == "TEXT"
    assert columns["response_body"] == "TEXT"
    assert columns["status_code"] == "INTEGER"
    assert columns["latency_ms"] == "INTEGER"
    assert columns["error"] == "TEXT"
    conn.close()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_db.py -v
```

Expected: 全部 FAIL，`ModuleNotFoundError: No module named 'akm.db'`

- [ ] **Step 3: 创建 akm/models.py**

```python
"""数据模型定义"""

from pydantic import BaseModel


class KeyConfig(BaseModel):
    """Key 配置数据模型"""
    alias: str
    provider: str               # openai / deepseek / codex
    api_key: str
    base_url: str | None = None
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


# 供应商默认 base_url
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com",
    "deepseek": "https://api.deepseek.com",
    "codex": "https://api.openai.com",
}
```

- [ ] **Step 4: 创建 akm/db.py**

```python
"""SQLite 数据库连接和建表"""

import os
import sqlite3
from pathlib import Path

# 数据目录：~/.akm/
DB_DIR = os.path.expanduser("~/.akm")


def get_db_path() -> str:
    """返回数据库文件完整路径，并确保目录存在"""
    os.makedirs(DB_DIR, exist_ok=True)
    return os.path.join(DB_DIR, "akm.db")


def get_connection() -> sqlite3.Connection:
    """获取数据库连接，启用 WAL 模式和外键"""
    conn = sqlite3.connect(get_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """创建 keys 和 audit_logs 表（如果不存在）"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS keys (
            alias     TEXT PRIMARY KEY,
            provider  TEXT NOT NULL,
            api_key   TEXT NOT NULL,
            base_url  TEXT,
            models    TEXT DEFAULT '*',
            priority  INTEGER DEFAULT 0,
            status    TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT NOT NULL DEFAULT (datetime('now')),
            provider     TEXT DEFAULT '',
            key_alias    TEXT DEFAULT '',
            model        TEXT DEFAULT '',
            request_body TEXT DEFAULT '',
            response_body TEXT DEFAULT '',
            status_code  INTEGER DEFAULT 0,
            latency_ms   INTEGER DEFAULT 0,
            error        TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_audit_timestamp
            ON audit_logs(timestamp);
    """)
    conn.commit()
```

- [ ] **Step 5: 运行测试确认通过**

```bash
pytest tests/test_db.py -v
```

Expected: 全部 PASS (4 tests)

- [ ] **Step 6: 提交**

```bash
git add akm/db.py akm/models.py tests/test_db.py
git commit -m "feat: 添加数据库层和模型定义"
```

---

### Task 3: Key 池管理

**Files:**
- Create: `akm/key_pool.py`
- Create: `tests/test_key_pool.py`

- [ ] **Step 1: 编写测试 — test_key_pool.py**

```python
import os
import tempfile
import pytest
from akm.db import get_connection, init_db
from akm.key_pool import (
    _load_cipher,
    add_key,
    get_key,
    list_keys,
    remove_key,
    set_priority,
    set_status,
    mark_rate_limited,
    pick_key,
    clear_expired_rate_limits,
)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    """每个测试使用独立数据库和密钥目录"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    monkeypatch.setattr("akm.key_pool.SECRET_DIR", tmpdir)
    # 强制重新生成 cipher，确保使用新的 secret key
    monkeypatch.setattr("akm.key_pool._cipher", None)
    conn = get_connection()
    init_db(conn)
    yield conn
    conn.close()


def test_add_and_get_key(setup):
    add_key("test-key", "openai", "sk-abc123")
    key = get_key("test-key")
    assert key["alias"] == "test-key"
    assert key["provider"] == "openai"
    assert key["api_key"] == "sk-abc123"
    assert key["status"] == "active"
    assert key["priority"] == 0
    assert key["models"] == "*"


def test_add_duplicate_key_raises(setup):
    add_key("dup", "openai", "sk-xxx")
    with pytest.raises(ValueError, match="已存在"):
        add_key("dup", "openai", "sk-yyy")


def test_list_keys(setup):
    add_key("k1", "openai", "sk-a")
    add_key("k2", "deepseek", "sk-b", priority=5)
    keys = list_keys()
    assert len(keys) == 2
    assert keys[0]["alias"] == "k1"   # 按 priority 排序，k1=0 在前
    assert keys[1]["alias"] == "k2"


def test_list_keys_by_provider(setup):
    add_key("k1", "openai", "sk-a")
    add_key("k2", "deepseek", "sk-b")
    keys = list_keys(provider="deepseek")
    assert len(keys) == 1
    assert keys[0]["alias"] == "k2"


def test_remove_key(setup):
    add_key("rm", "openai", "sk-x")
    assert remove_key("rm") is True
    assert get_key("rm") is None


def test_remove_nonexistent_key(setup):
    assert remove_key("no-such") is False


def test_set_priority(setup):
    add_key("p", "openai", "sk-x")
    set_priority("p", 10)
    assert get_key("p")["priority"] == 10


def test_set_status(setup):
    add_key("s", "openai", "sk-x")
    set_status("s", "disabled")
    assert get_key("s")["status"] == "disabled"


def test_rate_limited_tracking(setup):
    """mark_rate_limited 记录冷却时间，pick_key 会跳过冷却中的 key"""
    add_key("rl", "openai", "sk-x")
    mark_rate_limited("rl")
    assert get_key("rl")["status"] == "rate_limited"
    # 冷却中不应被选中
    key = pick_key(model="gpt-4")
    assert key is None


def test_pick_key_by_model(setup):
    add_key("openai-k", "openai", "sk-a", models="gpt-4,gpt-3.5")
    add_key("deepseek-k", "deepseek", "sk-b", models="deepseek-chat")
    # 请求 gpt-4 应选中 openai key
    key = pick_key(model="gpt-4")
    assert key is not None
    assert key["alias"] == "openai-k"
    # 请求 deepseek-chat 应选中 deepseek key
    key = pick_key(model="deepseek-chat")
    assert key is not None
    assert key["alias"] == "deepseek-k"


def test_pick_key_skips_disabled(setup):
    add_key("a", "openai", "sk-a", priority=0)
    add_key("b", "openai", "sk-b", priority=1)
    set_status("a", "disabled")
    key = pick_key(model="gpt-4")
    assert key["alias"] == "b"


def test_pick_key_wildcard_models(setup):
    add_key("w", "openai", "sk-w", models="*")
    key = pick_key(model="any-model")
    assert key is not None
    assert key["alias"] == "w"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_key_pool.py -v
```

Expected: 全部 FAIL

- [ ] **Step 3: 创建 akm/key_pool.py**

```python
"""Key 池管理：加密存储、CRUD、状态管理、选择逻辑"""

import os
import time
from cryptography.fernet import Fernet
from akm.db import get_connection
from akm.models import DEFAULT_BASE_URLS

SECRET_DIR = os.path.expanduser("~/.akm")
RATE_LIMIT_COOLDOWN = 60  # 限流冷却秒数
_cipher: Fernet | None = None


def _get_secret_path() -> str:
    """密钥文件路径"""
    os.makedirs(SECRET_DIR, exist_ok=True)
    return os.path.join(SECRET_DIR, "secret.key")


def _load_cipher() -> Fernet:
    """加载 Fernet 加密器，首次使用时自动生成密钥"""
    global _cipher
    if _cipher is not None:
        return _cipher
    key_path = _get_secret_path()
    if not os.path.exists(key_path):
        key = Fernet.generate_key()
        with open(key_path, "wb") as f:
            f.write(key)
    with open(key_path, "rb") as f:
        key = f.read()
    _cipher = Fernet(key)
    return _cipher


def _encrypt(plain: str) -> str:
    """加密明文，返回 base64 编码的密文字符串"""
    return _load_cipher().encrypt(plain.encode()).decode()


def _decrypt(cipher_text: str) -> str:
    """解密 base64 编码的密文，返回明文"""
    return _load_cipher().decrypt(cipher_text.encode()).decode()


def add_key(
    alias: str,
    provider: str,
    api_key: str,
    base_url: str | None = None,
    models: str = "*",
    priority: int = 0,
) -> None:
    """添加一个新的 API key"""
    if base_url is None:
        base_url = DEFAULT_BASE_URLS.get(provider, "")
    enc_key = _encrypt(api_key)
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO keys (alias, provider, api_key, base_url, models, priority)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (alias, provider, enc_key, base_url, models, priority),
        )
        conn.commit()
    except Exception:
        raise ValueError(f"Key 别名 '{alias}' 已存在")
    finally:
        conn.close()


def get_key(alias: str) -> dict | None:
    """获取指定 key 的完整信息（解密后）"""
    conn = get_connection()
    row = conn.execute("SELECT * FROM keys WHERE alias = ?", (alias,)).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    d["api_key"] = _decrypt(d["api_key"])
    return d


def list_keys(provider: str | None = None) -> list[dict]:
    """列出所有 key，按 priority 升序排列，可筛选供应商"""
    conn = get_connection()
    if provider:
        rows = conn.execute(
            "SELECT * FROM keys WHERE provider = ? ORDER BY priority ASC",
            (provider,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM keys ORDER BY priority ASC"
        ).fetchall()
    conn.close()
    result = []
    for row in rows:
        d = dict(row)
        d["api_key"] = _decrypt(d["api_key"])
        result.append(d)
    return result


def remove_key(alias: str) -> bool:
    """删除指定 key，返回是否删除成功"""
    conn = get_connection()
    cursor = conn.execute("DELETE FROM keys WHERE alias = ?", (alias,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def set_priority(alias: str, priority: int) -> None:
    """设置 key 优先级"""
    conn = get_connection()
    conn.execute(
        "UPDATE keys SET priority = ? WHERE alias = ?", (priority, alias)
    )
    conn.commit()
    conn.close()


def set_status(alias: str, status: str) -> None:
    """设置 key 状态：active / disabled / rate_limited"""
    conn = get_connection()
    conn.execute(
        "UPDATE keys SET status = ? WHERE alias = ?", (status, alias)
    )
    conn.commit()
    conn.close()


# 限流冷却记录：{alias: 冷却截止时间戳}
_rate_limit_timers: dict[str, float] = {}


def mark_rate_limited(alias: str) -> None:
    """标记 key 为限流状态，60 秒后自动恢复"""
    set_status(alias, "rate_limited")
    _rate_limit_timers[alias] = time.time() + RATE_LIMIT_COOLDOWN


def clear_expired_rate_limits() -> None:
    """恢复冷却到期的 key 为 active 状态"""
    now = time.time()
    expired = [
        alias for alias, deadline in _rate_limit_timers.items()
        if now >= deadline
    ]
    for alias in expired:
        set_status(alias, "active")
        del _rate_limit_timers[alias]


def pick_key(model: str) -> dict | None:
    """根据 model 名选择优先级最高的可用 key

    匹配规则：
    1. key 的 models 字段包含该 model（精确匹配），或 models 为 '*'
    2. status 为 'active'
    3. 按 priority ASC 排序，取第一个
    """
    clear_expired_rate_limits()
    conn = get_connection()
    # 先选 models='*' 通配的 + models 包含指定 model 的 active key
    rows = conn.execute(
        """SELECT * FROM keys
           WHERE status = 'active'
             AND (models = '*' OR ',' || models || ',' LIKE '%,' || ? || ',%')
           ORDER BY priority ASC""",
        (model,),
    ).fetchall()
    conn.close()
    if not rows:
        return None
    d = dict(rows[0])
    d["api_key"] = _decrypt(d["api_key"])
    return d
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_key_pool.py -v
```

Expected: 全部 PASS (12 tests)

- [ ] **Step 5: 提交**

```bash
git add akm/key_pool.py tests/test_key_pool.py
git commit -m "feat: 添加 Key 池管理（加密存储、CRUD、优先级选择）"
```

---

### Task 4: 代理转发 + 故障切换

**Files:**
- Create: `akm/proxy.py`
- Create: `tests/test_proxy.py`

- [ ] **Step 1: 编写测试 — test_proxy.py**

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
from akm.proxy import forward_request, _build_upstream_url


class FakeResponse:
    """模拟 httpx.Response"""
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=MagicMock(), response=self)


@pytest.mark.asyncio
async def test_build_upstream_url():
    assert _build_upstream_url("https://api.openai.com") == \
        "https://api.openai.com/v1/chat/completions"
    assert _build_upstream_url("https://api.deepseek.com/v1") == \
        "https://api.deepseek.com/v1/v1/chat/completions"


@pytest.mark.asyncio
async def test_forward_success(monkeypatch):
    """正常转发成功返回"""
    monkeypatch.setattr("akm.proxy.pick_key", lambda model: {
        "alias": "ok", "provider": "openai", "api_key": "sk-xxx",
        "base_url": "https://api.openai.com",
    })
    mock_client = AsyncMock()
    mock_client.post.return_value = FakeResponse(200, {"choices": [{"message": {"content": "hi"}}]})

    with patch("akm.proxy.httpx.AsyncClient", return_value=mock_client):
        result = await forward_request(
            body={"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
            client=mock_client,
            log_callback=None,
        )
    assert result["status_code"] == 200
    assert result["key_alias"] == "ok"


@pytest.mark.asyncio
async def test_forward_429_switches_key(monkeypatch):
    """429 限流后切换下一个 key"""
    keys_called = []

    def pick_key_mock(model):
        keys_called.append(model)
        if len(keys_called) == 1:
            return {"alias": "k1", "provider": "openai", "api_key": "sk-a",
                    "base_url": "https://api.openai.com"}
        else:
            return {"alias": "k2", "provider": "openai", "api_key": "sk-b",
                    "base_url": "https://api.openai.com"}

    monkeypatch.setattr("akm.proxy.pick_key", pick_key_mock)

    # mark_rate_limited 真实调用，但需要 patch 数据库操作
    monkeypatch.setattr("akm.proxy.mark_rate_limited", lambda alias: None)

    mock_client = AsyncMock()
    mock_client.post.side_effect = [
        FakeResponse(429),
        FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
    ]

    with patch("akm.proxy.httpx.AsyncClient", return_value=mock_client):
        result = await forward_request(
            body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
            client=mock_client,
            log_callback=None,
        )
    assert result["status_code"] == 200
    assert result["key_alias"] == "k2"
    assert len(keys_called) >= 2


@pytest.mark.asyncio
async def test_forward_402_disables_key(monkeypatch):
    """402 余额不足后禁用 key 并切换"""
    keys_called = []

    def pick_key_mock(model):
        keys_called.append(model)
        if len(keys_called) == 1:
            return {"alias": "k1", "provider": "openai", "api_key": "sk-a",
                    "base_url": "https://api.openai.com"}
        else:
            return {"alias": "k2", "provider": "openai", "api_key": "sk-b",
                    "base_url": "https://api.openai.com"}

    monkeypatch.setattr("akm.proxy.pick_key", pick_key_mock)
    monkeypatch.setattr("akm.proxy.set_status", lambda alias, status: None)

    mock_client = AsyncMock()
    mock_client.post.side_effect = [
        FakeResponse(402),
        FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
    ]

    with patch("akm.proxy.httpx.AsyncClient", return_value=mock_client):
        result = await forward_request(
            body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
            client=mock_client,
            log_callback=None,
        )
    assert result["status_code"] == 200
    assert result["key_alias"] == "k2"


@pytest.mark.asyncio
async def test_forward_all_keys_exhausted(monkeypatch):
    """所有 key 都不可用时返回 503"""
    monkeypatch.setattr("akm.proxy.pick_key", lambda model: None)

    result = await forward_request(
        body={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
        client=AsyncMock(),
        log_callback=None,
    )
    assert result["status_code"] == 503
    assert "没有可用" in result["error"]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_proxy.py -v
```

Expected: 全部 FAIL

- [ ] **Step 3: 创建 akm/proxy.py**

```python
"""代理转发：将请求转发到上游 AI API，含重试和故障切换逻辑"""

import time
import json
import httpx
from akm.key_pool import pick_key, mark_rate_limited, set_status


# 最大尝试 key 数量，防止无限循环
MAX_KEY_TRIES = 20
# 5xx 最大重试次数（单个 key）
MAX_RETRIES_PER_KEY = 2


def _build_upstream_url(base_url: str) -> str:
    """从供应商 base_url 拼接 chat/completions 路径"""
    base = base_url.rstrip("/")
    # 如果 base_url 已经包含 /v1，则直接追加
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


async def forward_request(
    body: dict,
    client: httpx.AsyncClient,
    log_callback=None,
) -> dict:
    """转发请求到上游 AI API，自动处理故障切换

    返回: {"status_code": int, "body": str, "key_alias": str,
           "provider": str, "model": str, "error": str, "latency_ms": int}
    """
    model = body.get("model", "")
    tries = 0

    while tries < MAX_KEY_TRIES:
        key = pick_key(model)
        if key is None:
            return {
                "status_code": 503,
                "body": "",
                "key_alias": "",
                "provider": "",
                "model": model,
                "error": "没有可用的 API key",
                "latency_ms": 0,
            }

        tries += 1
        url = _build_upstream_url(key["base_url"])
        headers = {
            "Authorization": f"Bearer {key['api_key']}",
            "Content-Type": "application/json",
        }

        last_error = ""
        for attempt in range(1 + MAX_RETRIES_PER_KEY):
            t0 = time.time()
            try:
                resp = await client.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=120,
                )
                latency = int((time.time() - t0) * 1000)
                resp_body = resp.text

                if resp.status_code == 429:
                    mark_rate_limited(key["alias"])
                    last_error = f"429 Too Many Requests (key: {key['alias']})"
                    break  # 跳出重试循环，换 key

                if resp.status_code == 402:
                    set_status(key["alias"], "disabled")
                    last_error = f"402 Payment Required (key: {key['alias']} 已禁用)"
                    break

                if 500 <= resp.status_code < 600:
                    last_error = f"{resp.status_code} Server Error"
                    if attempt < MAX_RETRIES_PER_KEY:
                        continue  # 重试同一 key
                    else:
                        break  # 重试耗尽，换 key

                # 成功
                return {
                    "status_code": resp.status_code,
                    "body": resp_body,
                    "key_alias": key["alias"],
                    "provider": key["provider"],
                    "model": model,
                    "error": "",
                    "latency_ms": latency,
                }

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = str(e)
                if attempt >= MAX_RETRIES_PER_KEY:
                    break

            except Exception as e:
                last_error = str(e)
                break

        # 当前 key 彻底失败，日志回调记录失败尝试
        if log_callback:
            log_callback({
                "provider": key["provider"],
                "key_alias": key["alias"],
                "model": model,
                "request_body": json.dumps(body, ensure_ascii=False),
                "response_body": "",
                "status_code": 0,
                "latency_ms": 0,
                "error": last_error,
            })

    return {
        "status_code": 502,
        "body": "",
        "key_alias": "",
        "provider": "",
        "model": model,
        "error": "所有 key 均已尝试但均失败",
        "latency_ms": 0,
    }
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_proxy.py -v
```

Expected: 全部 PASS (5 tests)

- [ ] **Step 5: 提交**

```bash
git add akm/proxy.py tests/test_proxy.py
git commit -m "feat: 添加代理转发和自动故障切换逻辑"
```

---

### Task 5: 审计日志

**Files:**
- Create: `akm/audit.py`
- Create: `tests/test_audit.py`

- [ ] **Step 1: 编写测试 — test_audit.py**

```python
import tempfile
import pytest
from akm.db import get_connection, init_db
from akm.audit import write_log, list_logs, clean_logs


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    conn = get_connection()
    init_db(conn)
    yield conn
    conn.close()


def test_write_and_list_log(setup):
    write_log({
        "provider": "openai",
        "key_alias": "my-key",
        "model": "gpt-4",
        "request_body": '{"model":"gpt-4"}',
        "response_body": '{"choices":[]}',
        "status_code": 200,
        "latency_ms": 350,
        "error": "",
    })
    logs = list_logs(limit=10)
    assert len(logs) == 1
    log = logs[0]
    assert log["provider"] == "openai"
    assert log["key_alias"] == "my-key"
    assert log["status_code"] == 200
    assert log["latency_ms"] == 350


def test_write_log_error(setup):
    write_log({
        "provider": "openai",
        "key_alias": "bad-key",
        "model": "gpt-4",
        "request_body": "{}",
        "response_body": "",
        "status_code": 0,
        "latency_ms": 0,
        "error": "Connection timeout",
    })
    logs = list_logs()
    assert logs[0]["error"] == "Connection timeout"


def test_list_logs_by_provider(setup):
    write_log({"provider": "openai", "key_alias": "a", "model": "g", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    write_log({"provider": "deepseek", "key_alias": "b", "model": "d", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    logs = list_logs(provider="deepseek", limit=10)
    assert len(logs) == 1
    assert logs[0]["key_alias"] == "b"


def test_clean_logs(setup):
    write_log({"provider": "o", "key_alias": "k", "model": "m", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    assert len(list_logs()) == 1
    # 清理未来日期的日志不会删除任何内容
    count = clean_logs("2099-01-01")
    assert count == 1
    assert len(list_logs()) == 0


def test_clean_logs_partial(setup):
    import time
    write_log({"provider": "o", "key_alias": "k1", "model": "m", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    time.sleep(0.01)
    write_log({"provider": "o", "key_alias": "k2", "model": "m", "request_body": "", "response_body": "", "status_code": 200, "latency_ms": 0, "error": ""})
    # 清理很旧的数据不影响
    count = clean_logs("2000-01-01")
    assert count == 0
    assert len(list_logs()) == 2
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_audit.py -v
```

Expected: 全部 FAIL

- [ ] **Step 3: 创建 akm/audit.py**

```python
"""审计日志：写入、查询、清理"""

from datetime import datetime
from akm.db import get_connection


def write_log(data: dict) -> None:
    """写入一条审计日志

    data 应包含: provider, key_alias, model, request_body,
                response_body, status_code, latency_ms, error
    """
    conn = get_connection()
    conn.execute(
        """INSERT INTO audit_logs
           (timestamp, provider, key_alias, model, request_body,
            response_body, status_code, latency_ms, error)
           VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get("provider", ""),
            data.get("key_alias", ""),
            data.get("model", ""),
            data.get("request_body", ""),
            data.get("response_body", ""),
            data.get("status_code", 0),
            data.get("latency_ms", 0),
            data.get("error", ""),
        ),
    )
    conn.commit()
    conn.close()


def list_logs(
    provider: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """查询最近日志，可按供应商筛选"""
    conn = get_connection()
    if provider:
        rows = conn.execute(
            """SELECT * FROM audit_logs
               WHERE provider = ?
               ORDER BY id DESC LIMIT ?""",
            (provider, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clean_logs(before: str) -> int:
    """清理指定日期之前的日志，返回删除条数

    before: YYYY-MM-DD 格式的日期字符串
    """
    try:
        datetime.strptime(before, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"日期格式错误: {before}，需要 YYYY-MM-DD")
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM audit_logs WHERE timestamp < ?", (before,)
    )
    conn.commit()
    count = cursor.rowcount
    conn.close()
    return count
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_audit.py -v
```

Expected: 全部 PASS (5 tests)

- [ ] **Step 5: 提交**

```bash
git add akm/audit.py tests/test_audit.py
git commit -m "feat: 添加审计日志模块"
```

---

### Task 6: FastAPI 服务

**Files:**
- Create: `akm/server.py`
- Create: `tests/test_server.py`

- [ ] **Step 1: 编写测试 — test_server.py**

```python
import tempfile
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import ASGITransport, AsyncClient
from akm.db import get_connection, init_db
from akm.server import app


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr("akm.db.DB_DIR", tmpdir)
    conn = get_connection()
    init_db(conn)
    conn.close()
    yield


@pytest.mark.asyncio
async def test_chat_completions_success(monkeypatch):
    """正常请求返回上游响应"""
    async def mock_forward(body, client, log_callback=None):
        return {
            "status_code": 200,
            "body": '{"choices":[{"message":{"content":"hello"}}]}',
            "key_alias": "test-key",
            "provider": "openai",
            "model": "gpt-4",
            "error": "",
            "latency_ms": 100,
        }
    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log", lambda x: None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "hello"


@pytest.mark.asyncio
async def test_chat_completions_no_keys(monkeypatch):
    """没有可用 key 时返回 503"""
    async def mock_forward(body, client, log_callback=None):
        return {
            "status_code": 503,
            "body": "",
            "key_alias": "",
            "provider": "",
            "model": "gpt-4",
            "error": "没有可用的 API key",
            "latency_ms": 0,
        }
    monkeypatch.setattr("akm.server.forward_request", mock_forward)
    monkeypatch.setattr("akm.server.write_log", lambda x: None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 503
    data = resp.json()
    assert "没有可用" in data["detail"]


@pytest.mark.asyncio
async def test_health_endpoint():
    """健康检查端点"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_server.py -v
```

Expected: 全部 FAIL

- [ ] **Step 3: 创建 akm/server.py**

```python
"""FastAPI 服务：接收 OpenAI 兼容请求并代理转发"""

import json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from akm.proxy import forward_request
from akm.audit import write_log

app = FastAPI(title="AI Key Manager", version="0.1.0")


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok"}


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 兼容的 chat completions 端点"""
    body = await request.json()

    async with httpx.AsyncClient() as client:
        result = await forward_request(body, client)

    # 写入审计日志
    write_log({
        "provider": result["provider"],
        "key_alias": result["key_alias"],
        "model": result["model"],
        "request_body": json.dumps(body, ensure_ascii=False),
        "response_body": result["body"],
        "status_code": result["status_code"],
        "latency_ms": result["latency_ms"],
        "error": result["error"],
    })

    if result["status_code"] == 503:
        return JSONResponse(
            status_code=503,
            content={"detail": result["error"]},
        )

    if result["status_code"] == 502:
        return JSONResponse(
            status_code=502,
            content={"detail": result["error"]},
        )

    # 透传上游响应
    try:
        upstream_data = json.loads(result["body"])
    except json.JSONDecodeError:
        upstream_data = {"raw": result["body"]}

    return Response(
        content=result["body"],
        status_code=result["status_code"],
        media_type="application/json",
    )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_server.py -v
```

Expected: 全部 PASS (3 tests)

- [ ] **Step 5: 提交**

```bash
git add akm/server.py tests/test_server.py
git commit -m "feat: 添加 FastAPI 代理服务"
```

---

### Task 7: CLI 工具

**Files:**
- Create: `akm/cli.py`

> **注意：** CLI 命令不单独编写单元测试（Click 集成测试复杂度高、收益低），改为在 Task 8 中通过端到端脚本验证 CLI 功能。

- [ ] **Step 1: 创建 akm/cli.py**

```python
"""CLI 管理工具：key 管理、服务启动、日志查看"""

import os
import click
from akm.db import get_connection, init_db
from akm.key_pool import (
    add_key, list_keys, remove_key, set_priority, set_status, get_key,
)
from akm.audit import list_logs, clean_logs


def _ensure_db():
    """确保数据库已初始化"""
    conn = get_connection()
    init_db(conn)
    conn.close()


@click.group()
def main():
    """AI Key Manager — 本地 AI API key 管理代理"""
    _ensure_db()


# ── key 子命令 ──────────────────────────────────────────

@main.group()
def key():
    """管理 API key"""
    pass


@key.command("add")
@click.argument("alias")
@click.argument("provider")
@click.option("--models", default="*", help="支持的模型，逗号分隔，默认 * 表示全部")
@click.option("--base-url", default=None, help="自定义 API 地址")
@click.option("--priority", default=0, type=int, help="优先级，越小越优先")
def key_add(alias, provider, models, base_url, priority):
    """添加一个新的 API key"""
    api_key = click.prompt("请输入 API key", hide_input=True)
    try:
        add_key(alias, provider, api_key, base_url, models, priority)
        click.echo(f"Key '{alias}' 添加成功")
    except ValueError as e:
        click.echo(f"错误: {e}", err=True)


@key.command("list")
@click.option("--provider", default=None, help="按供应商筛选")
def key_list(provider):
    """列出所有 key"""
    keys = list_keys(provider=provider)
    if not keys:
        click.echo("暂无 key")
        return
    for k in keys:
        masked = k["api_key"][:8] + "..." if k["api_key"] else ""
        click.echo(
            f"  [{k['alias']}] {k['provider']:<10} "
            f"优先级={k['priority']:<3} 状态={k['status']:<12} "
            f"模型={k['models']:<20} key={masked}"
        )


@key.command("remove")
@click.argument("alias")
def key_remove(alias):
    """删除指定 key"""
    if remove_key(alias):
        click.echo(f"Key '{alias}' 已删除")
    else:
        click.echo(f"Key '{alias}' 不存在", err=True)


@key.command("set-priority")
@click.argument("alias")
@click.argument("priority", type=int)
def key_set_priority(alias, priority):
    """设置 key 优先级"""
    if get_key(alias) is None:
        click.echo(f"Key '{alias}' 不存在", err=True)
        return
    set_priority(alias, priority)
    click.echo(f"Key '{alias}' 优先级已设为 {priority}")


@key.command("disable")
@click.argument("alias")
def key_disable(alias):
    """禁用指定 key"""
    if get_key(alias) is None:
        click.echo(f"Key '{alias}' 不存在", err=True)
        return
    set_status(alias, "disabled")
    click.echo(f"Key '{alias}' 已禁用")


@key.command("enable")
@click.argument("alias")
def key_enable(alias):
    """启用指定 key"""
    if get_key(alias) is None:
        click.echo(f"Key '{alias}' 不存在", err=True)
        return
    set_status(alias, "active")
    click.echo(f"Key '{alias}' 已启用")


# ── serve 命令 ───────────────────────────────────────────

@main.command("serve")
@click.option("--port", default=8800, help="监听端口，默认 8800")
@click.option("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
def serve(port, host):
    """启动代理服务"""
    import uvicorn
    click.echo(f"AI Key Manager 启动中 → http://{host}:{port}")
    uvicorn.run("akm.server:app", host=host, port=port, log_level="info")


# ── log 子命令 ───────────────────────────────────────────

@main.group()
def log():
    """管理审计日志"""
    pass


@log.command("list")
@click.option("--provider", default=None, help="按供应商筛选")
@click.option("--limit", default=20, type=int, help="返回条数，默认 20")
def log_list(provider, limit):
    """查看最近日志"""
    logs = list_logs(provider=provider, limit=limit)
    if not logs:
        click.echo("暂无日志")
        return
    for entry in logs:
        click.echo(
            f"  [{entry['timestamp']}] {entry['provider']}/{entry['key_alias']} "
            f"model={entry['model']} status={entry['status_code']} "
            f"延迟={entry['latency_ms']}ms"
        )
        if entry["error"]:
            click.echo(f"    错误: {entry['error']}")


@log.command("clean")
@click.option("--before", required=True, help="清理此日期之前的日志 (YYYY-MM-DD)")
def log_clean(before):
    """清理旧日志"""
    if not click.confirm(f"确认删除 {before} 之前的所有日志?"):
        click.echo("已取消")
        return
    count = clean_logs(before)
    click.echo(f"已清理 {count} 条日志")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 验证 CLI 可执行**

```bash
python -m akm.cli --help
```

Expected: 显示帮助信息，包含 `key`, `serve`, `log` 三个子命令组

- [ ] **Step 3: 提交**

```bash
git add akm/cli.py
git commit -m "feat: 添加 CLI 管理工具"
```

---

### Task 8: 端到端集成验证

**Files:**
- Create: `tests/test_e2e.py`

- [ ] **Step 1: 编写端到端测试 — test_e2e.py**

```python
"""端到端测试：验证 CLI 操作 + 服务请求的完整流程"""

import tempfile
import subprocess
import time
import json
import pytest
import httpx


@pytest.fixture(scope="module")
def akm_env(monkeypatch):
    """设置隔离环境并初始化数据库"""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setenv("HOME", tmpdir)
    # 安装 akm 包（editable），确保 akm 命令可用
    subprocess.run(["pip", "install", "-e", "."], capture_output=True, cwd=".")
    yield tmpdir


def test_cli_key_add_list_remove():
    """测试 key 的增删查完整流程"""
    # 添加 key
    import os
    tmpdir = tempfile.mkdtemp()
    os.environ["HOME"] = tmpdir
    subprocess.run(["pip", "install", "-e", "."], capture_output=True, cwd=".")
    subprocess.run(
        ["akm", "key", "add", "e2e-test", "openai", "--models", "gpt-4"],
        input="sk-test123\n",
        text=True,
        capture_output=True,
    )
    # 列出
    result = subprocess.run(["akm", "key", "list"], capture_output=True, text=True)
    assert "e2e-test" in result.stdout
    assert "openai" in result.stdout
    # 删除
    result = subprocess.run(["akm", "key", "remove", "e2e-test"], capture_output=True, text=True)
    assert "已删除" in result.stdout
    # 确认已删
    result = subprocess.run(["akm", "key", "list"], capture_output=True, text=True)
    assert "暂无" in result.stdout


def test_cli_key_priority_and_status():
    """测试优先级设置和启用/禁用"""
    import os
    tmpdir = tempfile.mkdtemp()
    os.environ["HOME"] = tmpdir
    subprocess.run(["pip", "install", "-e", "."], capture_output=True, cwd=".")
    subprocess.run(
        ["akm", "key", "add", "prio", "openai"],
        input="sk-prio\n",
        text=True,
        capture_output=True,
    )
    # 设置优先级
    subprocess.run(["akm", "key", "set-priority", "prio", "99"], capture_output=True, text=True)
    result = subprocess.run(["akm", "key", "list"], capture_output=True, text=True)
    assert "优先级=99" in result.stdout
    # 禁用
    subprocess.run(["akm", "key", "disable", "prio"], capture_output=True, text=True)
    result = subprocess.run(["akm", "key", "list"], capture_output=True, text=True)
    assert "状态=disabled" in result.stdout
    # 启用
    subprocess.run(["akm", "key", "enable", "prio"], capture_output=True, text=True)
    result = subprocess.run(["akm", "key", "list"], capture_output=True, text=True)
    assert "状态=active" in result.stdout


def test_server_startup_and_health():
    """测试服务启动和健康检查"""
    import os
    import signal
    tmpdir = tempfile.mkdtemp()
    os.environ["HOME"] = tmpdir
    subprocess.run(["pip", "install", "-e", "."], capture_output=True, cwd=".")
    # 启动服务（后台）
    proc = subprocess.Popen(
        ["akm", "serve", "--port", "18800"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(2)  # 等待服务启动
    try:
        import requests
        resp = requests.get("http://127.0.0.1:18800/health", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        proc.terminate()
        proc.wait()


def test_proxy_request_no_keys():
    """无 key 时代理请求返回 503"""
    import os
    import signal
    tmpdir = tempfile.mkdtemp()
    os.environ["HOME"] = tmpdir
    subprocess.run(["pip", "install", "-e", "."], capture_output=True, cwd=".")
    proc = subprocess.Popen(
        ["akm", "serve", "--port", "18801"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(2)
    try:
        import requests
        resp = requests.post(
            "http://127.0.0.1:18801/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
            timeout=5,
        )
        assert resp.status_code == 503
    finally:
        proc.terminate()
        proc.wait()
```

- [ ] **Step 2: 运行端到端测试**

```bash
pytest tests/test_e2e.py -v
```

Expected: 全部 PASS (4 tests)

- [ ] **Step 3: 运行全部测试确认**

```bash
pytest tests/ -v
```

Expected: 所有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add tests/test_e2e.py
git commit -m "test: 添加端到端集成测试"
```

---

## 实施顺序

```
Task 1 (脚手架) → Task 2 (数据库) → Task 3 (Key池) → Task 4 (代理)
                                                      ↘
                                            Task 5 (审计日志)
                                                      ↙
Task 6 (FastAPI服务) → Task 7 (CLI) → Task 8 (E2E验证)
```

Task 4 和 Task 5 可并行实施（它们互不依赖）。
