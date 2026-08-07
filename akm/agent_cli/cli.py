"""`akm agent` 命令入口：交互式 Agent 会话。

用法::

    akm agent                 进入交互式会话
    akm agent "帮我写个脚本"   一次性（单轮）执行
    akm agent --resume 名称    从历史会话继续
    akm agent session list     列出历史会话
    akm agent session show 名称 查看会话
    akm agent session rm 名称  删除会话

一次性模式调用本地代理服务的 ``/v1/agent`` 非流式接口；交互模式使用
流式 SSE，并把多轮 messages 持久化到 ``~/.akm/agent_sessions/``。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import click
import httpx

import akm.config as config_module


def _base_url() -> str:
    """本地代理服务地址（与 akm/cli.py 保持一致）。"""
    cfg = config_module.load_config()
    port = int(cfg.get("server_port", config_module.DEFAULTS["server_port"]))
    return f"http://127.0.0.1:{port}"


def _token() -> str:
    """agent_api_token（可选鉴权）。"""
    cfg = config_module.load_config()
    return str(cfg.get("agent_api_token") or "").strip()


def _resolve_default_model(model: str) -> str:
    """未指定 --model 时，返回默认模型。

    默认使用 deepseek-v4-flash（经济快速）；显式传入 --model 时原样返回。
    """
    if model.strip():
        return model.strip()
    return "deepseek-v4-flash"


def _resolve_default_workspace(workspace_root: str) -> str:
    """未指定 --workspace-root 时，返回当前目录作为默认工作区。

    这样打开 agent 的文件夹即默认工作区；显式传入时原样返回。
    """
    if workspace_root.strip():
        return workspace_root.strip()
    return os.getcwd()


def _check_service() -> None:
    """检查本地服务是否运行，未运行则提示启动。"""
    try:
        with httpx.Client(timeout=1.5) as client:
            resp = client.get(f"{_base_url()}/health")
        if 200 <= resp.status_code < 300:
            return
    except Exception:
        pass
    raise click.ClickException(
        "本地代理服务未运行。请先执行 `akm serve` 启动服务后再使用 agent。"
    )


@click.group(name="agent", invoke_without_command=True)
@click.option("--model", default="", help="模型名称，缺省自动选择 deepseek-v4-flash")
@click.option("--resume", default="", help="从指定历史会话继续（会话名）")
@click.option(
    "--workspace-root",
    default="",
    help="工作区根目录（读/写/shell/git 工具的沙箱根），缺省为当前目录",
)
@click.option("--api-path", default="chat/completions", help="上游 API 路径，默认 chat/completions")
@click.option("--stream/--no-stream", default=True, help="交互模式是否使用流式输出（默认流式）")
@click.option("--name", default="", help="新会话名称，缺省自动生成")
@click.option("--color/--no-color", default=None, help="是否启用 ANSI 颜色（默认自动检测 TTY）")
@click.option(
    "--show-reasoning/--no-show-reasoning",
    default=False,
    help="是否展示模型的思考过程（默认折叠，避免 token 流刷屏）",
)
@click.pass_context
def agent(ctx, model, resume, workspace_root, api_path, stream, name, color, show_reasoning):
    """交互式 Agent 会话（基于本地 /v1/agent）。

    直接执行进入交互式会话；子命令 session 用于历史会话管理。
    """
    # 带子命令（如 session）时，把参数存到 ctx.obj 供子命令使用
    if ctx.invoked_subcommand is not None:
        ctx.obj = {
            "model": model,
            "workspace_root": workspace_root,
            "api_path": api_path,
            "name": name,
        }
        return
    # 无子命令时，本命令即启动会话
    _run_agent(
        model=model,
        resume=resume,
        workspace_root=workspace_root,
        api_path=api_path,
        stream=stream,
        name=name,
        color=color,
        show_reasoning=show_reasoning,
    )


def _run_agent(
    model="",
    resume="",
    workspace_root="",
    api_path="chat/completions",
    stream=True,
    name="",
    color=None,
    show_reasoning=False,
):
    """启动交互式 Agent 会话（多行智能输入 + rich 流式渲染）。"""
    _check_service()

    # --color 缺省时按是否终端自动决定；非 TTY（管道/重定向）自动关闭颜色
    if color is None:
        color = sys.stdout.isatty()

    # 未指定模型时自动选一个可用的默认模型，避免服务端无 wildcard key 报错
    model = _resolve_default_model(model)

    # 未指定工作区时，默认使用当前目录（打开 agent 的文件夹）
    workspace_root = _resolve_default_workspace(workspace_root)

    # 延迟导入，避免仅使用 --help 时加载整个 REPL 模块
    from akm.agent_cli.repl import AgentClient, build_session, run_agent_repl
    from akm.agent_cli.sessions import SessionStore

    store = SessionStore()
    try:
        session = build_session(
            store,
            name=name,
            resume=resume,
            model=model,
            workspace_root=workspace_root,
            api_path=api_path,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    client = AgentClient(_base_url(), token=_token())

    # TTY 下输入走 prompt_toolkit 多行智能输入（Enter 发送 / Alt+Enter 换行 /
    # 输入 / 弹命令菜单，命令名模糊匹配），非 TTY（管道 / 重定向）自动回退系统
    # input() 逐行输入；启动横幅（小猫 + 会话信息）由 REPL 主循环打印
    try:
        asyncio.run(
            run_agent_repl(
                store,
                client,
                session,
                stream=stream,
                color=color,
                show_reasoning=show_reasoning,
                # 三区 Live 渲染仅在交互终端且流式时启用（非 TTY 自动关闭）
                enable_live=bool(stream and color),
            )
        )
    except KeyboardInterrupt:
        click.echo("\n已退出。")


@agent.group("session")
def session():
    """管理历史会话。"""


@session.command("list")
def session_list():
    """列出全部历史会话。"""
    from akm.agent_cli.sessions import SessionStore

    store = SessionStore()
    sessions = store.list()
    if not sessions:
        click.echo("暂无历史会话")
        return
    for item in sessions:
        click.echo(
            f"{item['name']}  ({item['message_count']} 条消息, "
            f"更新于 {item['updated_at']})"
        )


@session.command("show")
@click.argument("name")
def session_show(name):
    """查看指定会话的摘要与最近消息。"""
    from akm.agent_cli.sessions import SessionStore

    store = SessionStore()
    data = store.load(name)
    if data is None:
        raise click.ClickException(f"会话不存在: {name}")
    click.echo(f"会话: {data.get('name')}")
    click.echo(f"创建: {data.get('created_at')}  更新: {data.get('updated_at')}")
    click.echo(f"模型: {data.get('model') or '(默认)'}")
    click.echo(f"工作区: {data.get('workspace_root') or '(未设置)'}")
    messages = data.get("messages") or []
    click.echo(f"消息数: {len(messages)}")
    if messages:
        click.echo("--- 最近消息 ---")
        for msg in messages[-5:]:
            role = msg.get("role")
            content = str(msg.get("content") or "")
            if len(content) > 200:
                content = content[:200] + "…"
            click.echo(f"[{role}] {content}")


@session.command("rm")
@click.argument("name")
def session_rm(name):
    """删除指定会话。"""
    from akm.agent_cli.sessions import SessionStore

    store = SessionStore()
    if not store.delete(name):
        raise click.ClickException(f"会话不存在: {name}")
    click.echo(f"已删除会话: {name}")
