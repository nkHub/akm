"""CLI 管理工具：key 管理、服务启动、日志查看"""

import os
import asyncio
import webbrowser
import threading
import time
import json

import click
import httpx
from fastapi import FastAPI
from akm import __version__
import akm.config as config_module
from akm.agent import get_agent
from akm.db import get_connection, init_db, get_db_path, get_keys_log_path
from akm.key_pool import (
    add_key, list_keys, remove_key, set_priority, set_base_url, set_api_key, set_status, get_key,
    set_models, set_provider, set_auth_header,
    get_usage_query_config, set_usage_query_config,
)
from akm.proxy import test_key_connectivity
from akm.audit import list_logs, clean_logs, count_logs
from akm.markdown_kb_hook import markdown_kb_hook
from akm.plugins.plugin_manager import PluginManager


def _ensure_db():
    """确保数据库已初始化"""
    conn = get_connection()
    init_db(conn)
    conn.close()


def _format_config_value(value):
    """将 CLI 展示值格式化为稳定的字符串，便于脚本读取。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _parse_config_value(raw: str, current):
    """按现有配置值类型解析命令行输入，避免把数字/布尔都写成字符串。"""
    if isinstance(current, bool):
        value = raw.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        raise click.ClickException("布尔值仅支持 true/false/1/0/yes/no/on/off")
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(raw)
        except ValueError as exc:
            raise click.ClickException("该配置项需要整数值") from exc
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise click.ClickException("该配置项需要数字值") from exc
    if isinstance(current, (dict, list)):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"JSON 解析失败: {exc}") from exc
    return raw


def _get_service_health(base_url: str) -> tuple[bool, str]:
    """探测本地 HTTP 服务是否可达，status 命令只做轻量健康检查。"""
    health_url = f"{base_url.rstrip('/')}/health"
    try:
        with httpx.Client(timeout=1.5) as client:
            resp = client.get(health_url)
        if 200 <= resp.status_code < 300:
            return True, f"运行中 ({resp.status_code})"
        return False, f"异常响应 ({resp.status_code})"
    except Exception as exc:
        return False, f"未运行 ({exc})"


def _local_service_base_url() -> str:
    """统一生成本地代理服务地址，避免 CLI 内各命令各自拼接端口。"""
    cfg = config_module.load_config()
    port = int(cfg.get("server_port", config_module.DEFAULTS["server_port"]))
    return f"http://127.0.0.1:{port}"


def _format_non_json_service_error(resp: httpx.Response, action: str) -> str:
    """把非 JSON 错误响应压缩成一行，便于直接在终端定位上游问题。

    图片命令常见失败场景是上游网关直接返回 HTML 或纯文本错误页。原先 CLI
    只提示“非 JSON 响应”，实际排查时还需要再翻日志或抓包，信息不够。这里
    额外带上 content-type 与响应体前几百字符，尽量在不刷屏的前提下给出足够线索。
    """
    content_type = (resp.headers.get("content-type") or "").strip() or "unknown"
    server = (resp.headers.get("server") or "").strip() or "unknown"
    content_length = (resp.headers.get("content-length") or "").strip() or "unknown"
    body_preview = (resp.text or "").strip()
    if not body_preview:
        body_preview = "<empty>"
    body_preview = " ".join(body_preview.split())
    if len(body_preview) > 300:
        body_preview = body_preview[:300] + "..."
    return (
        f"{action}失败：服务返回了非 JSON 响应 "
        f"(HTTP {resp.status_code}, content-type={content_type}, server={server}, "
        f"content-length={content_length}, body={body_preview})"
    )


async def _generate_image_via_local_service(payload: dict, timeout: float = 120.0) -> dict:
    """通过本地代理调用图片生成接口，返回标准 JSON，便于 MCP 直接消费。

    这里显式复用本地 `/v1/images/generations` 路由，而不是在 CLI 内重复实现
    Key 选择、默认模型回填和审计逻辑，避免形成第二套图片请求链路。
    """
    base_url = _local_service_base_url()
    url = f"{base_url}/v1/images/generations"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
    try:
        body = resp.json()
    except json.JSONDecodeError as exc:
        raise click.ClickException(_format_non_json_service_error(resp, "图片生成")) from exc
    if resp.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else None
        message = str(detail or body or f"HTTP {resp.status_code}")
        raise click.ClickException(f"图片生成失败：{message}")
    return body


async def _edit_image_via_local_service(
    form_data: dict,
    file_specs: list[tuple[str, tuple[str, bytes, str]]],
    timeout: float = 120.0,
) -> dict:
    """通过本地代理调用图片编辑接口，返回标准 JSON，便于 MCP 直接消费。

    图片编辑必须走 multipart/form-data，这里统一在 CLI 内完成本地文件读取和
    表单拼装，避免让外部调用方自己处理 http multipart 细节。
    """
    base_url = _local_service_base_url()
    url = f"{base_url}/v1/images/edits"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, data=form_data, files=file_specs)
    try:
        body = resp.json()
    except json.JSONDecodeError as exc:
        raise click.ClickException(_format_non_json_service_error(resp, "图片编辑")) from exc
    if resp.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else None
        message = str(detail or body or f"HTTP {resp.status_code}")
        raise click.ClickException(f"图片编辑失败：{message}")
    return body


def _default_image_cli_timeout() -> float:
    """读取图片命令默认超时，和服务端图片请求链路保持一致。"""
    try:
        timeout = float(config_module.load_config().get("image_request_timeout_sec", 300) or 300)
    except (TypeError, ValueError):
        timeout = 300.0
    return max(30.0, timeout)


def _read_upload_file(path: str) -> tuple[str, bytes, str]:
    """读取本地上传文件，按扩展名推断 content-type，减少调用方样板代码。"""
    import mimetypes

    file_path = os.path.expanduser(path)
    if not os.path.exists(file_path):
        raise click.ClickException(f"文件不存在: {path}")
    if not os.path.isfile(file_path):
        raise click.ClickException(f"不是文件: {path}")
    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(file_path, "rb") as f:
        content = f.read()
    return filename, content, content_type


def _load_plugin_manager() -> PluginManager:
    """为 CLI 临时加载插件元数据，用于查询状态和切换启停。"""
    manager = PluginManager()
    asyncio.run(manager.load_all(FastAPI(), db=None))
    return manager


def _category_label(category: str) -> str:
    """把插件分类转成更直观的中文标签，便于终端阅读。"""
    labels = {
        "filter": "请求处理",
        "matcher": "模型匹配",
        "converter": "格式转换",
        "handler": "错误处理",
        "post": "响应处理",
        "app": "应用插件",
    }
    return labels.get(category or "", category or "未分类")


def _mask_api_key(api_key: str) -> str:
    """以稳定方式脱敏 key，避免 show/status 在终端直接暴露完整密钥。"""
    if not api_key:
        return "-"
    if len(api_key) <= 8:
        return api_key[:2] + "***"
    return api_key[:6] + "..." + api_key[-4:]


def _format_list(values: list[str]) -> str:
    """把列表转为逗号串，空列表时返回短横线，减少终端空白。"""
    if not values:
        return "-"
    return ", ".join(values)


def _collect_log_stats() -> dict:
    """聚合日志总量、成功失败数、供应商与模型分布，供 log stats 命令复用。"""
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    success = conn.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE status_code >= 200 AND status_code < 300"
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE status_code < 200 OR status_code >= 300"
    ).fetchone()[0]
    avg_latency = conn.execute(
        "SELECT AVG(latency_ms) FROM audit_logs WHERE latency_ms > 0"
    ).fetchone()[0]
    provider_rows = conn.execute(
        """
        SELECT provider, COUNT(*) AS count
        FROM audit_logs
        WHERE provider != ''
        GROUP BY provider
        ORDER BY count DESC, provider ASC
        LIMIT 5
        """
    ).fetchall()
    model_rows = conn.execute(
        """
        SELECT model, COUNT(*) AS count
        FROM audit_logs
        WHERE model != ''
        GROUP BY model
        ORDER BY count DESC, model ASC
        LIMIT 5
        """
    ).fetchall()
    conn.close()
    return {
        "total": total,
        "success": success,
        "failed": failed,
        "avg_latency_ms": round(float(avg_latency), 1) if avg_latency is not None else None,
        "providers": [dict(row) for row in provider_rows],
        "models": [dict(row) for row in model_rows],
    }


def _run_doctor_checks() -> list[tuple[str, str, str]]:
    """执行本地自检，尽量覆盖 CLI 可直接判断的问题，不依赖服务端额外接口。"""
    checks = []

    try:
        cfg = config_module.load_config()
        checks.append(("OK", "config", f"已加载 {config_module.CONFIG_PATH}"))
    except Exception as exc:
        return [("FAIL", "config", f"配置读取失败: {exc}")]

    try:
        conn = get_connection()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        checks.append(("OK", "database", f"可访问 {get_db_path()}"))
    except Exception as exc:
        checks.append(("FAIL", "database", f"数据库不可访问: {exc}"))

    keys_log_path = get_keys_log_path()
    if os.path.exists(keys_log_path):
        checks.append(("OK", "keys.log", f"审计文件存在: {keys_log_path}"))
    else:
        checks.append(("WARN", "keys.log", f"审计文件尚未生成: {keys_log_path}"))

    try:
        plugins = _load_plugin_manager().get_plugin_list()
        checks.append(("OK", "plugins", f"已识别 {len(plugins)} 个插件"))
    except Exception as exc:
        checks.append(("FAIL", "plugins", f"插件加载失败: {exc}"))

    port = int(cfg.get("server_port", config_module.DEFAULTS["server_port"]))
    service_ok, service_status = _get_service_health(f"http://127.0.0.1:{port}")
    checks.append(("OK" if service_ok else "WARN", "service", service_status))
    return checks


def _plugin_setting_defaults(meta: dict) -> dict:
    """从插件 settings schema 提取默认值，供 CLI 类型解析与补全使用。"""
    defaults = {}
    for item in meta.get("settings") or []:
        defaults[item.get("key")] = item.get("default")
    return defaults


def _get_plugin_meta(manager: PluginManager, name: str) -> dict | None:
    """从插件列表中按名称取元数据，避免 CLI 重复拼接查找逻辑。"""
    for item in manager.get_plugin_list():
        if item.get("name") == name:
            return item
    return None


async def _test_health_endpoint(key: dict) -> dict:
    """测试上游网关的 health 端点，并复用该 key 的认证头配置。"""
    agent = get_agent(key.get("provider", "openai"))
    base_url = key.get("base_url") or agent.default_base_url
    url = f"{base_url.rstrip('/')}/health"
    headers = agent.build_headers(key, "health")
    started_at = time.time()
    result = {
        "ok": False,
        "url": url,
        "status_code": 0,
        "latency_ms": 0,
        "error": "",
        "response_body": "",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=15)
        result["status_code"] = resp.status_code
        result["latency_ms"] = int((time.time() - started_at) * 1000)
        result["response_body"] = resp.text[:500]
        if 200 <= resp.status_code < 300:
            result["ok"] = True
            return result
        result["error"] = f"HTTP {resp.status_code}"
        return result
    except httpx.TimeoutException:
        result["error"] = "请求超时"
        return result
    except httpx.ConnectError as e:
        result["error"] = f"连接失败: {e}"
        return result
    except Exception as e:
        result["error"] = str(e)
        return result


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


@main.command("status")
def status():
    """查看当前配置、服务、Key、日志与插件总览"""
    cfg = config_module.load_config()
    keys = list_keys()
    log_total = count_logs()
    port = int(cfg.get("server_port", config_module.DEFAULTS["server_port"]))
    base_url = f"http://127.0.0.1:{port}"
    service_ok, service_status = _get_service_health(base_url)

    try:
        plugins = _load_plugin_manager().get_plugin_list()
    except Exception as exc:
        plugins = []
        plugin_note = f"插件加载失败: {exc}"
    else:
        plugin_note = ""

    active_keys = sum(1 for item in keys if item.get("status") == "active")
    disabled_keys = sum(1 for item in keys if item.get("status") != "active")
    enabled_plugins = sum(1 for item in plugins if item.get("enabled"))

    click.echo(f"AKM 版本      : {__version__}")
    click.echo(f"配置文件      : {config_module.CONFIG_PATH}")
    click.echo(f"服务地址      : {base_url}")
    click.echo(f"服务状态      : {'正常' if service_ok else '异常'}，{service_status}")
    click.echo(f"自动打开管理台: {_format_config_value(cfg.get('auto_open_admin'))}")
    click.echo(f"Key 概览       : 总数 {len(keys)}，启用 {active_keys}，禁用 {disabled_keys}")
    click.echo(f"审计日志      : 共 {log_total} 条")
    if plugin_note:
        click.echo(f"插件概览      : {plugin_note}")
    else:
        click.echo(f"插件概览      : 总数 {len(plugins)}，启用 {enabled_plugins}")


@main.group()
def config():
    """读取或修改 ~/.akm/config.json 配置"""
    pass


@config.command("get")
@click.argument("key", required=False)
def config_get(key):
    """读取全部配置，或读取单个配置项"""
    cfg = config_module.load_config()
    if not key:
        click.echo(json.dumps(cfg, ensure_ascii=False, indent=2))
        return
    if key not in cfg:
        raise click.ClickException(f"未知配置项: {key}")
    click.echo(_format_config_value(cfg[key]))


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """按当前类型写入单个配置项"""
    cfg = config_module.load_config()
    if key not in cfg:
        raise click.ClickException(f"未知配置项: {key}")
    parsed = _parse_config_value(value, cfg[key])
    config_module.save_config({key: parsed})
    click.echo(f"配置已更新: {key}={_format_config_value(parsed)}")


@main.group()
def plugin():
    """查看和切换插件启停状态"""
    pass


@main.group()
def image():
    """通过本地代理执行图片生成相关操作，便于脚本或 MCP 直接调用。"""
    pass


@image.command("generate")
@click.argument("prompt")
@click.option("--model", default=None, help="图片模型，默认由服务端自动回填 image_supported_models 首项")
@click.option("--size", default=None, help="图片尺寸，例如 1024x1024")
@click.option("--quality", default=None, help="图片质量，例如 low/medium/high")
@click.option("--background", default=None, help="背景模式，例如 transparent")
@click.option("--output-format", default=None, help="输出格式，例如 png/webp")
@click.option("--n", default=None, type=int, help="生成张数")
@click.option("--user", default=None, help="透传给上游的 user 字段")
@click.option("--timeout", default=None, type=float, help="请求超时时间（秒，默认读取 config.json 的 image_request_timeout_sec）")
def image_generate(prompt, model, size, quality, background, output_format, n, user, timeout):
    """调用本地代理生成图片，并把 JSON 结果直接输出到标准输出。

    该命令设计目标是“机器可消费优先”，因此成功时只输出 JSON，
    不额外拼接人类说明文字，方便 MCP/脚本直接解析 `data[].url` 或
    `data[].b64_json` 等字段。
    """
    payload = {
        "prompt": prompt,
    }
    if model:
        payload["model"] = model
    if size:
        payload["size"] = size
    if quality:
        payload["quality"] = quality
    if background:
        payload["background"] = background
    if output_format:
        payload["output_format"] = output_format
    if n is not None:
        payload["n"] = n
    if user:
        payload["user"] = user
    effective_timeout = float(timeout) if timeout is not None else _default_image_cli_timeout()
    result = asyncio.run(_generate_image_via_local_service(payload, timeout=effective_timeout))
    click.echo(json.dumps(result, ensure_ascii=False))


@image.command("edit")
@click.argument("image_path")
@click.option("--prompt", required=True, help="图片编辑提示词")
@click.option("--mask", default=None, help="可选 mask 图片路径")
@click.option("--model", default=None, help="图片模型，默认由服务端自动回填 image_supported_models 首项")
@click.option("--size", default=None, help="图片尺寸，例如 1024x1024")
@click.option("--quality", default=None, help="图片质量，例如 low/medium/high")
@click.option("--background", default=None, help="背景模式，例如 transparent")
@click.option("--output-format", default=None, help="输出格式，例如 png/webp")
@click.option("--n", default=None, type=int, help="生成张数")
@click.option("--user", default=None, help="透传给上游的 user 字段")
@click.option("--timeout", default=None, type=float, help="请求超时时间（秒，默认读取 config.json 的 image_request_timeout_sec）")
def image_edit(image_path, prompt, mask, model, size, quality, background, output_format, n, user, timeout):
    """调用本地代理编辑图片，并把 JSON 结果直接输出到标准输出。

    与 `image generate` 一样，成功时只输出 JSON，方便 MCP/脚本直接读取。
    图片文件通过 `image_path` 传入；如需局部重绘，可额外提供 `--mask`。
    """
    form_data = {
        "prompt": prompt,
    }
    if model:
        form_data["model"] = model
    if size:
        form_data["size"] = size
    if quality:
        form_data["quality"] = quality
    if background:
        form_data["background"] = background
    if output_format:
        form_data["output_format"] = output_format
    if n is not None:
        form_data["n"] = str(n)
    if user:
        form_data["user"] = user

    image_file = _read_upload_file(image_path)
    file_specs = [("image", image_file)]
    if mask:
        file_specs.append(("mask", _read_upload_file(mask)))

    effective_timeout = float(timeout) if timeout is not None else _default_image_cli_timeout()
    result = asyncio.run(_edit_image_via_local_service(form_data, file_specs, timeout=effective_timeout))
    click.echo(json.dumps(result, ensure_ascii=False))


@plugin.command("list")
def plugin_list():
    """列出全部插件及其状态"""
    manager = _load_plugin_manager()
    plugins = manager.get_plugin_list()
    if not plugins:
        click.echo("暂无插件")
        return
    for item in plugins:
        source = "内置" if item.get("builtin") else ("本地" if item.get("source") == "project" else "第三方")
        required = " 必需" if item.get("required") else ""
        click.echo(
            f"[{item['name']}] {_category_label(item.get('category'))} "
            f"状态={'启用' if item.get('enabled') else '禁用'} 来源={source}{required} "
            f"v{item.get('version', '-') }"
        )


def _toggle_plugin(name: str, enable: bool):
    """切换插件状态并输出统一提示，供 enable/disable 复用。

    优先打运行中的本地服务 API（热启停）；服务未运行时只写配置，下次启动生效。
    """
    base_url = _local_service_base_url()
    service_ok, _ = _get_service_health(base_url)
    if service_ok:
        action = "enable" if enable else "disable"
        url = f"{base_url.rstrip('/')}/api/plugins/{name}/{action}"
        try:
            with httpx.Client(timeout=8.0) as client:
                resp = client.post(url)
            try:
                result = resp.json()
            except json.JSONDecodeError as exc:
                raise click.ClickException(
                    _format_non_json_service_error(resp, f"插件{action}")
                ) from exc
            if not result.get("ok"):
                raise click.ClickException(result.get("error") or "插件状态切换失败")
            click.echo(result.get("message") or "状态已更新（服务热生效）")
            return
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(f"调用本地服务失败: {exc}") from exc

    manager = _load_plugin_manager()
    result = asyncio.run(manager.toggle_plugin(name, enable, hot=False))
    if not result.get("ok"):
        raise click.ClickException(result.get("error") or "插件状态切换失败")
    click.echo(result.get("message") or "状态已保存")


@plugin.command("enable")
@click.argument("name")
def plugin_enable(name):
    """启用指定插件"""
    _toggle_plugin(name, True)


@plugin.command("disable")
@click.argument("name")
def plugin_disable(name):
    """禁用指定插件"""
    _toggle_plugin(name, False)


@plugin.group("config")
def plugin_config():
    """读取或修改插件配置"""
    pass


@plugin_config.command("get")
@click.argument("name")
@click.argument("key", required=False)
def plugin_config_get(name, key):
    """读取插件全部配置，或读取单个配置项"""
    manager = _load_plugin_manager()
    meta = _get_plugin_meta(manager, name)
    if meta is None:
        raise click.ClickException(f"插件不存在: {name}")
    cfg = manager.get_config(name)
    if cfg is None:
        raise click.ClickException(f"插件不存在: {name}")
    if not key:
        click.echo(json.dumps(cfg, ensure_ascii=False, indent=2))
        return
    if key not in cfg:
        raise click.ClickException(f"未知插件配置项: {key}")
    click.echo(_format_config_value(cfg[key]))


@plugin_config.command("set")
@click.argument("name")
@click.argument("key")
@click.argument("value")
def plugin_config_set(name, key, value):
    """按插件 schema 类型修改单个配置项"""
    manager = _load_plugin_manager()
    meta = _get_plugin_meta(manager, name)
    if meta is None:
        raise click.ClickException(f"插件不存在: {name}")
    cfg = manager.get_config(name)
    if cfg is None:
        raise click.ClickException(f"插件不存在: {name}")
    defaults = _plugin_setting_defaults(meta)
    if key not in defaults and key not in cfg:
        raise click.ClickException(f"未知插件配置项: {key}")
    current = cfg[key] if key in cfg else defaults.get(key)
    parsed = _parse_config_value(value, current)
    cfg[key] = parsed
    result = manager.set_config(name, cfg)
    if not result.get("ok"):
        raise click.ClickException(result.get("error") or "插件配置保存失败")
    click.echo(f"插件配置已更新: {name}.{key}={_format_config_value(parsed)}")


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


@key.command("show")
@click.argument("alias")
def key_show(alias):
    """查看单个 key 的详细信息，便于排查具体配置。"""
    k = get_key(alias)
    if k is None:
        raise click.ClickException(f"Key '{alias}' 不存在")
    click.echo(f"别名           : {k['alias']}")
    click.echo(f"供应商         : {k['provider']}")
    click.echo(f"状态           : {k['status']}")
    click.echo(f"优先级         : {k['priority']}")
    click.echo(f"Base URL       : {k.get('base_url') or '-'}")
    click.echo(f"模型配置       : {k.get('models') or '-'}")
    click.echo(f"解析模型列表   : {_format_list(k.get('model_list') or [])}")
    click.echo(f"提供商模型列表 : {_format_list(k.get('provider_models') or [])}")
    click.echo(f"认证头模板     : {k.get('auth_header') or '-'}")
    click.echo(f"API Key        : {_mask_api_key(k.get('api_key') or '')}")
    click.echo(f"创建时间       : {k.get('created_at') or '-'}")


@key.command("edit")
@click.argument("alias")
@click.option("--provider", default=None, help="修改供应商名称")
@click.option("--models", default=None, help="修改模型配置，支持 * 或逗号分隔列表")
@click.option("--base-url", default=None, help="修改 API 地址")
@click.option("--auth-header", default=None, help="修改认证头模板")
@click.option("--priority", default=None, type=int, help="修改优先级")
@click.option("--status", default=None, type=click.Choice(["active", "disabled", "rate_limited"]), help="修改状态")
def key_edit(alias, provider, models, base_url, auth_header, priority, status):
    """统一编辑 key 常用字段，减少多个 set-* 命令来回切换。"""
    if get_key(alias) is None:
        raise click.ClickException(f"Key '{alias}' 不存在")
    changed = []
    if provider is not None:
        set_provider(alias, provider)
        changed.append("provider")
    if models is not None:
        set_models(alias, models)
        changed.append("models")
    if base_url is not None:
        set_base_url(alias, base_url)
        changed.append("base_url")
    if auth_header is not None:
        set_auth_header(alias, auth_header)
        changed.append("auth_header")
    if priority is not None:
        set_priority(alias, priority)
        changed.append("priority")
    if status is not None:
        set_status(alias, status)
        changed.append("status")
    if not changed:
        raise click.ClickException("请至少提供一个待修改选项")
    click.echo(f"Key '{alias}' 已更新: {', '.join(changed)}")


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
@click.option("--health", is_flag=True, help="改为请求 {base_url}/health，仅测试网关可达性")
@click.option("--fallback", is_flag=True, help="测试失败时允许按兼容接口继续尝试，默认关闭")
def key_test(alias, health, fallback):
    """测试 key 连通性"""
    k = get_key(alias)
    if k is None:
        click.echo(f"Key '{alias}' 不存在", err=True)
        return
    click.echo(f"测试 [{alias}] {k['provider']} → {k['base_url']} ...")
    if health:
        result = asyncio.run(_test_health_endpoint(k))
        click.echo(f"  请求 URL : {result['url']}")
        click.echo("  测试模式 : health")
    else:
        result = asyncio.run(test_key_connectivity(k, allow_fallback=fallback))
        click.echo(f"  请求 URL : {result['url']}")
        click.echo(f"  测试接口 : {result['api_path']}")
        click.echo(f"  请求模型 : {result['model']}")
        if result.get("fallback_used"):
            click.echo(f"  回退链路 : {' -> '.join(result.get('attempted_paths', []))}")
    if result["ok"]:
        click.echo(f"  结果     : ✅ 连接成功 ({result['latency_ms']}ms)")
    else:
        click.echo(f"  结果     : ❌ 失败")
        click.echo(f"  状态码   : {result['status_code']}")
        click.echo(f"  错误     : {result['error']}")
        if result["response_body"]:
            click.echo(f"  响应体   : {result['response_body']}")


@key.command("health")
@click.option("--provider", default=None, help="仅巡检指定供应商的 key")
@click.option("--health", is_flag=True, help="只检查 /health，可更快验证网关在线状态")
@click.option("--fallback", is_flag=True, help="测试业务接口时允许回退到兼容接口")
def key_health(provider, health, fallback):
    """批量巡检 key，可用于快速发现不可用 key 或高延迟网关。"""
    keys = list_keys(provider=provider)
    if not keys:
        click.echo("暂无 key")
        return
    ok_count = 0
    failed_count = 0
    for k in keys:
        if health:
            result = asyncio.run(_test_health_endpoint(k))
        else:
            result = asyncio.run(test_key_connectivity(k, allow_fallback=fallback))
        if result.get("ok"):
            ok_count += 1
            click.echo(
                f"[OK] {k['alias']} provider={k['provider']} latency={result.get('latency_ms', 0)}ms url={result.get('url', '-') }"
            )
        else:
            failed_count += 1
            click.echo(
                f"[FAIL] {k['alias']} provider={k['provider']} status={result.get('status_code', 0)} error={result.get('error') or '-'}"
            )
    click.echo(f"巡检完成：成功 {ok_count}，失败 {failed_count}，总计 {len(keys)}")


# ── 用量查询命令 ────────────────────────────────────────────

@key.command("usage-query")
@click.argument("alias")
def key_usage_query(alias):
    """手动查询指定 key 的用量/余额（使用已配置的查询脚本）"""
    k = get_key(alias)
    if k is None:
        raise click.ClickException(f"Key '{alias}' 不存在")

    config = get_usage_query_config(alias)
    if config is None or not config.get("script"):
        raise click.ClickException(f"Key '{alias}' 未配置用量查询脚本，请先用 set-usage-script 设置")

    script_raw = config.get("script", "")
    try:
        script_cfg = json.loads(script_raw)
    except json.JSONDecodeError:
        raise click.ClickException("用量查询脚本 JSON 解析失败")

    from akm.usage_query import execute_query_script

    async def _run():
        limits = httpx.Limits(max_keepalive_connections=2, max_connections=4)
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
            return await execute_query_script(k, script_cfg, client)

    click.echo(f"正在查询 [{alias}] {k.get('provider', '')} 用量...")
    result = asyncio.run(_run())

    if result["ok"]:
        click.echo(f"✅ 查询成功 ({result['status_code']}, {result['latency_ms']}ms)")
    else:
        click.echo(f"❌ 查询失败: {result.get('error', 'Unknown error')}")

    extracted = result.get("extracted", {}) or {}
    if extracted:
        click.echo("提取到的用量信息：")
        for field in ("isValid", "remaining", "unit", "total", "used", "planName", "invalidMessage", "extra"):
            val = extracted.get(field)
            if val is not None:
                click.echo(f"  {field}: {val}")


@key.command("set-usage-script")
@click.argument("alias")
def key_set_usage_script(alias):
    """为 key 设置用量查询脚本（交互式编辑 JSON 配置）"""
    k = get_key(alias)
    if k is None:
        raise click.ClickException(f"Key '{alias}' 不存在")

    config = get_usage_query_config(alias)
    current = config["script"] if config else ""

    click.echo("当前用量查询脚本：")
    click.echo(current or "(空)")
    click.echo("")
    click.echo("请输入新的 JSON 脚本配置（可粘贴 ccswitch 格式）")
    click.echo("输入空行并使用 Ctrl+D 或 Ctrl+Z 结束输入：")

    lines = []
    while True:
        try:
            line = input()
            lines.append(line)
        except EOFError:
            break

    new_script = "\n".join(lines).strip()
    if not new_script:
        raise click.ClickException("未提供脚本内容")

    # 验证 JSON 格式
    try:
        json.loads(new_script)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"JSON 格式错误: {e}")

    set_usage_query_config(alias, script=new_script)
    click.echo(f"Key '{alias}' 用量查询脚本已更新")


@key.command("set-usage-interval")
@click.argument("alias")
@click.argument("minutes", type=int)
def key_set_usage_interval(alias, minutes):
    """设置 key 的自动查询间隔（分钟，0 表示关闭）"""
    k = get_key(alias)
    if k is None:
        raise click.ClickException(f"Key '{alias}' 不存在")
    if minutes < 0:
        raise click.ClickException("间隔分钟数不能为负数")
    set_usage_query_config(alias, interval_m=minutes)
    if minutes > 0:
        click.echo(f"Key '{alias}' 用量自动查询间隔已设为 {minutes} 分钟")
    else:
        click.echo(f"Key '{alias}' 用量自动查询已关闭")


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


@log.command("stats")
def log_stats():
    """查看日志聚合统计，快速判断流量与错误分布。"""
    stats = _collect_log_stats()
    click.echo(f"总日志数       : {stats['total']}")
    click.echo(f"成功请求       : {stats['success']}")
    click.echo(f"失败请求       : {stats['failed']}")
    click.echo(
        f"平均延迟       : {stats['avg_latency_ms']}ms" if stats['avg_latency_ms'] is not None else "平均延迟       : -"
    )
    click.echo("供应商 Top5    :")
    if stats["providers"]:
        for row in stats["providers"]:
            click.echo(f"  - {row['provider']}: {row['count']}")
    else:
        click.echo("  - 无")
    click.echo("模型 Top5      :")
    if stats["models"]:
        for row in stats["models"]:
            click.echo(f"  - {row['model']}: {row['count']}")
    else:
        click.echo("  - 无")


@log.command("clean")
@click.option("--before", required=True, help="清理此日期之前的日志 (YYYY-MM-DD)")
def log_clean(before):
    """清理旧日志"""
    if not click.confirm(f"确认删除 {before} 之前的所有日志?"):
        click.echo("已取消")
        return
    count = clean_logs(before)
    click.echo(f"已清理 {count} 条日志")


@main.command("doctor")
def doctor():
    """执行本地环境自检，帮助快速定位配置、数据库、插件和服务问题。"""
    checks = _run_doctor_checks()
    worst = "OK"
    order = {"OK": 0, "WARN": 1, "FAIL": 2}
    for level, name, message in checks:
        if order[level] > order[worst]:
            worst = level
        click.echo(f"[{level}] {name:<10} {message}")
    if worst == "FAIL":
        raise click.ClickException("doctor 检查失败")


main.add_command(markdown_kb_hook)


if __name__ == "__main__":
    main()
