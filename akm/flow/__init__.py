"""工作流（Flow）模块：DAG 工作流编排引擎（并入 AKM，替代原 flow 独立服务）。

本模块把 flow 项目的服务端逻辑重写为 Python 实现：
- 工作流（workflow）增删改查与内置模板
- 运行（run）DAG 执行引擎（拓扑分层、并行调度、条件边、loop 重入）
- LLM 节点直接复用 AKM 的 proxy.forward_request（省去一层 HTTP 网关）
- /v1/flow/* 路由供 chat「工作流」页面与 /v1/agent 内置工具调用

一期只实现 LLM 节点（intake/plan/review/output 等）与流程控制；
pi-agent 编码节点、人工审批、git worktree 沙箱在二期接入。
"""

from akm.flow.router import router

__all__ = ["router"]
