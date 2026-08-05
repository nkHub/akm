"""Agent 交互式 CLI 子包：会话持久化、SSE 消费、输出渲染、交互循环。

服务端 `/v1/agent` 是无状态的（messages 每次请求全量上传），因此本子包在
客户端负责保存多轮对话历史（~/.akm/agent_sessions/*.json），并在每次请求时
把完整 messages 回传，从而为 `akm agent` 提供会话 resume 能力。

模块划分与 akm/agent_runtime（服务端）对称：
- sessions.py   — 会话持久化层
- sse.py        — /v1/agent 流式 SSE 事件消费
- render.py     — 终端输出渲染（零依赖 ANSI）
- repl.py       — 交互循环 + 内建斜杠命令
- cli.py        — click 命令入口（akm agent / akm agent session）
"""

from akm.agent_cli.cli import agent
from akm.agent_cli.cli import session

__all__ = ["agent", "session"]
