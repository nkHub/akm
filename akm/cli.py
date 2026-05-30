"""CLI 管理工具：key 管理、服务启动、日志查看"""

import os
import asyncio
import webbrowser
import threading
import click
from akm import __version__
from akm.db import get_connection, init_db
from akm.key_pool import (
    add_key, list_keys, remove_key, set_priority, set_base_url, set_api_key, set_status, get_key,
)
from akm.proxy import test_key_connectivity
from akm.audit import list_logs, clean_logs


def _ensure_db():
    """确保数据库已初始化"""
    conn = get_connection()
    init_db(conn)
    conn.close()


@click.group()
@click.version_option(version=__version__, prog_name="akm")
def main():
    """AI Key Manager — 本地 AI API key 管理代理

    集中管理多个 AI 供应商的 API key，启动本地代理服务，
    由程序自动根据优先级选择可用 key 并处理故障切换。

    快速开始:

      \b
      1. 添加 key:   akm key add my-key deepseek
      2. 查看列表:   akm key list
      3. 启动服务:   akm serve
      4. 查看日志:   akm log list

    服务启动后，将 opencode / cursor / 其他 OpenAI 兼容客户端的
    base_url 指向 http://127.0.0.1:8800/v1 即可。

    所有子命令均支持 --help，如: akm key add --help"""
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
@click.option("--auth-header", default="Bearer {api_key}", help="认证头模板，{api_key} 会被替换")
@click.option("--priority", default=0, type=int, help="优先级，越小越优先")
def key_add(alias, provider, models, base_url, auth_header, priority):
    """添加一个新的 API key"""
    api_key = click.prompt("请输入 API key", hide_input=True)
    try:
        add_key(alias, provider, api_key, base_url, models, auth_header, priority)
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
        base = k.get("base_url", "") or "-"
        click.echo(
            f"  [{k['alias']}] {k['provider']:<10} "
            f"优先级={k['priority']:<3} 状态={k['status']:<12} "
            f"模型={k['models']:<20} base_url={base}"
        )
        click.echo(f"          key={masked}")


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


@key.command("set-base-url")
@click.argument("alias")
@click.argument("base_url")
def key_set_base_url(alias, base_url):
    """修改 key 的 API 地址"""
    if get_key(alias) is None:
        click.echo(f"Key '{alias}' 不存在", err=True)
        return
    set_base_url(alias, base_url)
    click.echo(f"Key '{alias}' base_url 已更新")


@key.command("set-key")
@click.argument("alias")
def key_set_key(alias):
    """修改已存在 key 的 API key 值"""
    if get_key(alias) is None:
        click.echo(f"Key '{alias}' 不存在", err=True)
        return
    api_key = click.prompt("请输入新的 API key", hide_input=True)
    set_api_key(alias, api_key)
    click.echo(f"Key '{alias}' 的 API key 已更新")


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


@key.command("test")
@click.argument("alias")
def key_test(alias):
    """测试 key 连通性"""
    k = get_key(alias)
    if k is None:
        click.echo(f"Key '{alias}' 不存在", err=True)
        return
    click.echo(f"测试 [{alias}] {k['provider']} → {k['base_url']} ...")
    result = asyncio.run(test_key_connectivity(k))
    click.echo(f"  请求 URL : {result['url']}")
    click.echo(f"  请求模型 : {result['model']}")
    if result["ok"]:
        click.echo(f"  结果     : ✅ 连接成功 ({result['latency_ms']}ms)")
    else:
        click.echo(f"  结果     : ❌ 失败")
        click.echo(f"  状态码   : {result['status_code']}")
        click.echo(f"  错误     : {result['error']}")
        if result["response_body"]:
            click.echo(f"  响应体   : {result['response_body']}")


# ── serve 命令 ───────────────────────────────────────────

@main.command("serve")
@click.option("--port", default=8800, help="监听端口，默认 8800")
@click.option("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
@click.option("--no-open", is_flag=True, help="启动后不自动打开浏览器")
def serve(port, host, no_open):
    """启动代理服务"""
    import uvicorn
    url = f"http://{host}:{port}/admin"
    click.echo(f"AI Key Manager 启动中 → http://{host}:{port}")
    click.echo(f"后台管理 → {url}")

    if not no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

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
