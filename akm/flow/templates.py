"""内置工作流模板（移植自 flow 项目的 templates.ts）。

模板中的 modelId 均为逻辑占位（mock-*），运行时由 find_model 按
strength 提示绑定真实模型（reason/code/review/fast）。
"""

import copy

from akm.flow import db


def _stamp() -> dict:
    """生成创建时间戳（version=1）。"""
    t = db.now_iso()
    return {"createdAt": t, "updatedAt": t, "version": 1}


def standard_dev_workflow() -> dict:
    """标准功能开发：规划 → 人工审批 → 编码 → 交叉审查 → 测试 → 修复 → 交付。"""
    intake = db.create_node_id()
    plan = db.create_node_id()
    human = db.create_node_id()
    code = db.create_node_id()
    review = db.create_node_id()
    test = db.create_node_id()
    fix = db.create_node_id()
    output = db.create_node_id()
    wf = {
        "id": db.create_workflow_id(),
        "name": "标准功能开发",
        "description": "规划 → 人工审批 → 编码 → 交叉审查 → 测试 → 修复 → 交付。每个阶段可绑定不同模型。",
        **_stamp(),
        "variables": {
            "projectPath": ".",
            "language": "TypeScript",
            # loop 边（fix→review）的重入预算；引擎默认 3，模板显式声明
            "maxNodeVisits": "3",
            "useWorktree": "false",
            "worktreeMode": "run",
            "keepWorktree": "false",
        },
        "nodes": [
            {
                "id": intake,
                "type": "intake",
                "position": {"x": 80, "y": 200},
                "data": {
                    "label": "需求输入",
                    "modelId": "mock-coder",
                    "executor": "llm",
                    "artifactKey": "intake",
                    "systemPrompt": "你是需求分析助手，提炼目标、约束与验收标准。",
                    "userPromptTemplate": "请结构化整理以下需求：\n\n{{input.prompt}}\n\n输出：目标 / 范围 / 验收标准 / 风险",
                },
            },
            {
                "id": plan,
                "type": "plan",
                "position": {"x": 300, "y": 200},
                "data": {
                    "label": "方案规划",
                    "modelId": "mock-reasoner",
                    "executor": "llm",
                    "artifactKey": "plan",
                    "systemPrompt": "你是资深架构师，输出可执行的技术方案，简洁有序。",
                    "userPromptTemplate": "项目路径: {{vars.projectPath}}\n语言: {{vars.language}}\n\n需求整理:\n{{artifacts.intake}}\n\n原始需求:\n{{input.prompt}}\n\n请输出技术方案（模块拆分、接口、步骤、风险）。",
                },
            },
            {
                "id": human,
                "type": "human",
                "position": {"x": 520, "y": 200},
                "data": {
                    "label": "方案审批",
                    "modelId": "mock-coder",
                    "executor": "human",
                    "humanGate": True,
                    "artifactKey": "approval",
                    "systemPrompt": "",
                    "userPromptTemplate": "请审阅方案产物（Artifacts · plan），确认 projectPath 与范围后批准继续编码。",
                },
            },
            {
                "id": code,
                "type": "code",
                "position": {"x": 740, "y": 200},
                "data": {
                    "label": "编码实现",
                    "modelId": "mock-coder",
                    "executor": "pi-agent",
                    "artifactKey": "code",
                    "systemPrompt": "你是资深工程师，根据方案写清晰可维护的代码。",
                    "userPromptTemplate": "按方案实现功能。\n\n## 方案\n{{artifacts.plan}}\n\n## 审批\n{{artifacts.approval}}\n\n## 需求\n{{input.prompt}}\n\n输出：关键代码与文件说明。",
                },
            },
            {
                "id": review,
                "type": "review",
                "position": {"x": 960, "y": 200},
                "data": {
                    "label": "交叉审查",
                    "modelId": "mock-reviewer",
                    "executor": "llm",
                    "artifactKey": "review",
                    "systemPrompt": "你是严格的代码审查员。必须给出结论行：`## 结论` 下一行为 `pass` 或 `fail`。",
                    "userPromptTemplate": "审查以下实现是否满足方案与需求。\n\n## 方案\n{{artifacts.plan}}\n\n## 实现\n{{artifacts.code}}\n\n输出审查报告，并明确 pass/fail。",
                },
            },
            {
                "id": test,
                "type": "test",
                "position": {"x": 1180, "y": 120},
                "data": {
                    "label": "测试验证",
                    "modelId": "mock-coder",
                    "executor": "pi-agent",
                    "artifactKey": "test",
                    "systemPrompt": "你是测试工程师，设计关键用例并推断风险。",
                    "userPromptTemplate": "基于实现设计测试用例与验证步骤。\n\n## 实现\n{{artifacts.code}}\n\n## 审查\n{{artifacts.review}}",
                },
            },
            {
                "id": fix,
                "type": "fix",
                "position": {"x": 1180, "y": 300},
                "data": {
                    "label": "修复",
                    "modelId": "mock-coder",
                    "executor": "pi-agent",
                    "artifactKey": "fix",
                    "systemPrompt": "你是修复专家，根据审查失败点最小改动修复。",
                    "userPromptTemplate": "审查未通过，请修复。\n\n## 审查\n{{artifacts.review}}\n\n## 原实现\n{{artifacts.code}}",
                },
            },
            {
                "id": output,
                "type": "output",
                "position": {"x": 1400, "y": 200},
                "data": {
                    "label": "交付汇总",
                    "modelId": "mock-coder",
                    "executor": "llm",
                    "artifactKey": "output",
                    "systemPrompt": "你是技术负责人，汇总交付说明。",
                    "userPromptTemplate": "汇总本次开发交付。\n\n## 方案\n{{artifacts.plan}}\n\n## 代码\n{{artifacts.code}}\n\n## 审查\n{{artifacts.review}}\n\n## 测试\n{{artifacts.test}}\n\n## 修复\n{{artifacts.fix}}",
                },
            },
        ],
        "edges": [
            {"id": db.create_edge_id(), "source": intake, "target": plan},
            {"id": db.create_edge_id(), "source": plan, "target": human},
            {"id": db.create_edge_id(), "source": human, "target": code},
            {"id": db.create_edge_id(), "source": code, "target": review},
            {"id": db.create_edge_id(), "source": review, "target": test, "condition": "pass", "label": "pass"},
            {"id": db.create_edge_id(), "source": review, "target": fix, "condition": "fail", "label": "fail"},
            {"id": db.create_edge_id(), "source": test, "target": output},
            # 修复后重入 review（受 maxNodeVisits 约束）；loop=true 使结构图保持 DAG
            {"id": db.create_edge_id(), "source": fix, "target": review, "loop": True, "label": "re-review"},
        ],
    }
    return copy.deepcopy(wf)


def hotfix_workflow() -> dict:
    """快速热修：问题描述 → 人工确认 → 编码 → 测试 → 交付。"""
    intake = db.create_node_id()
    human = db.create_node_id()
    code = db.create_node_id()
    test = db.create_node_id()
    output = db.create_node_id()
    wf = {
        "id": db.create_workflow_id(),
        "name": "快速热修",
        "description": "问题描述 → 人工确认 → 编码 → 测试 → 交付。",
        **_stamp(),
        "variables": {
            "projectPath": ".",
            "useWorktree": "false",
            "worktreeMode": "run",
            "keepWorktree": "false",
        },
        "nodes": [
            {
                "id": intake,
                "type": "intake",
                "position": {"x": 80, "y": 180},
                "data": {
                    "label": "问题描述",
                    "modelId": "mock-coder",
                    "executor": "llm",
                    "artifactKey": "intake",
                    "systemPrompt": "提炼缺陷现象、复现与期望。",
                    "userPromptTemplate": "{{input.prompt}}",
                },
            },
            {
                "id": human,
                "type": "human",
                "position": {"x": 320, "y": 180},
                "data": {
                    "label": "修复确认",
                    "modelId": "mock-coder",
                    "executor": "human",
                    "humanGate": True,
                    "artifactKey": "approval",
                    "systemPrompt": "",
                    "userPromptTemplate": "请确认问题描述与影响范围（Artifacts · intake），批准后进入最小改动修复。",
                },
            },
            {
                "id": code,
                "type": "code",
                "position": {"x": 560, "y": 180},
                "data": {
                    "label": "修复编码",
                    "modelId": "mock-coder",
                    "executor": "pi-agent",
                    "artifactKey": "code",
                    "systemPrompt": "最小改动修复问题，说明原因。",
                    "userPromptTemplate": "## 问题\n{{artifacts.intake}}\n\n给出补丁与说明。",
                },
            },
            {
                "id": test,
                "type": "test",
                "position": {"x": 800, "y": 180},
                "data": {
                    "label": "回归验证",
                    "modelId": "mock-coder",
                    "executor": "pi-agent",
                    "artifactKey": "test",
                    "systemPrompt": "列出回归用例。",
                    "userPromptTemplate": "## 修复\n{{artifacts.code}}\n\n回归点？",
                },
            },
            {
                "id": output,
                "type": "output",
                "position": {"x": 1040, "y": 180},
                "data": {
                    "label": "交付",
                    "modelId": "mock-coder",
                    "executor": "llm",
                    "artifactKey": "output",
                    "systemPrompt": "写简短 release note。",
                    "userPromptTemplate": "问题: {{artifacts.intake}}\n修复: {{artifacts.code}}\n测试: {{artifacts.test}}",
                },
            },
        ],
        "edges": [
            {"id": db.create_edge_id(), "source": intake, "target": human},
            {"id": db.create_edge_id(), "source": human, "target": code},
            {"id": db.create_edge_id(), "source": code, "target": test},
            {"id": db.create_edge_id(), "source": test, "target": output},
        ],
    }
    return copy.deepcopy(wf)


def dual_model_workflow() -> dict:
    """双模型竞赛：同一方案由两个编码模型实现，裁判模型择优。"""
    plan = db.create_node_id()
    code_a = db.create_node_id()
    code_b = db.create_node_id()
    judge = db.create_node_id()
    output = db.create_node_id()
    wf = {
        "id": db.create_workflow_id(),
        "name": "双模型竞赛",
        "description": "同一方案由两个编码模型实现，裁判模型择优。默认 useWorktree=per-coding 隔离，可并行写；关闭 worktree 时同 projectPath 会串行加锁。",
        **_stamp(),
        "variables": {
            "projectPath": ".",
            "maxNodeVisits": "3",
            "useWorktree": "true",
            "worktreeMode": "per-coding",
            "keepWorktree": "false",
        },
        "nodes": [
            {
                "id": plan,
                "type": "plan",
                "position": {"x": 120, "y": 220},
                "data": {
                    "label": "统一方案",
                    "modelId": "mock-reasoner",
                    "executor": "llm",
                    "artifactKey": "plan",
                    "systemPrompt": "输出统一实现规格，便于两模型同题竞技。",
                    "userPromptTemplate": "{{input.prompt}}",
                },
            },
            {
                "id": code_a,
                "type": "code",
                "position": {"x": 400, "y": 120},
                "data": {
                    "label": "实现 A",
                    "modelId": "mock-coder",
                    "executor": "pi-agent",
                    "artifactKey": "codeA",
                    "systemPrompt": "实现规格，风格偏简洁。",
                    "userPromptTemplate": "## 规格\n{{artifacts.plan}}",
                },
            },
            {
                "id": code_b,
                "type": "code",
                "position": {"x": 400, "y": 320},
                "data": {
                    "label": "实现 B",
                    "modelId": "mock-coder",
                    "executor": "pi-agent",
                    "artifactKey": "codeB",
                    "systemPrompt": "实现规格，风格偏健壮与注释。",
                    "userPromptTemplate": "## 规格\n{{artifacts.plan}}",
                },
            },
            {
                "id": judge,
                "type": "review",
                "position": {"x": 680, "y": 220},
                "data": {
                    "label": "裁判择优",
                    "modelId": "mock-reviewer",
                    "executor": "llm",
                    "artifactKey": "judge",
                    "systemPrompt": "对比两份实现，选出更优并说明理由。",
                    "userPromptTemplate": "## 规格\n{{artifacts.plan}}\n\n## A\n{{artifacts.codeA}}\n\n## B\n{{artifacts.codeB}}",
                },
            },
            {
                "id": output,
                "type": "output",
                "position": {"x": 940, "y": 220},
                "data": {
                    "label": "结果",
                    "modelId": "mock-coder",
                    "executor": "llm",
                    "artifactKey": "output",
                    "systemPrompt": "输出最终选用方案。",
                    "userPromptTemplate": "{{artifacts.judge}}",
                },
            },
        ],
        "edges": [
            {"id": db.create_edge_id(), "source": plan, "target": code_a},
            {"id": db.create_edge_id(), "source": plan, "target": code_b},
            {"id": db.create_edge_id(), "source": code_a, "target": judge},
            {"id": db.create_edge_id(), "source": code_b, "target": judge},
            {"id": db.create_edge_id(), "source": judge, "target": output},
        ],
    }
    return copy.deepcopy(wf)


TEMPLATES: list[dict] = [
    standard_dev_workflow(),
    hotfix_workflow(),
    dual_model_workflow(),
]
