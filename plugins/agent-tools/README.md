# `agent-tools` 插件

AKM 的纯前端离线工具箱（Offline Tools）。构建产物已打进本插件
`dist/` 目录，**启用即用，无需配置绝对路径**。

包含 JSON/YAML 格式化、时间戳转换、哈希、二维码生成、颜色工具、
正则测试、本机 IP 查询等开发者常用工具。

## 使用

1. 在管理台「插件」页启用 `agent-tools`（或在 `~/.akm/config.json` 的
   `plugin_configs.agent-tools` 中配置）。
2. 访问 `<访问路径>`（默认 `http://127.0.0.1:8800/tools`）开始使用。

该工具集为纯前端实现，除 IP 查询工具调用公共 API（ipapi.co /
ipify / ipwho.is）外，不依赖任何后端接口。

## 配置

| 键 | 类型 | 默认值 | 说明 |
|----|------|--------|------|
| `route_prefix` | string | `/tools` | 工具箱挂载路径。不能使用 `/`、`/api`、`/v1`、`/admin`、`/health`、`/debug`。修改后需重启服务 |
| `spa_fallback` | boolean | `true` | 将不存在的无扩展名路径回退到 `index.html`。工具箱为单页应用，建议保持开启 |

## 更新界面

工具箱源码在独立的 [`tools`](https://github.com/nkHub/tools) 项目。更新步骤：

```bash
cd tools
VITE_BASE_PATH='./' npm run build
rm -rf ../ccs/plugins/agent-tools/dist
cp -R dist/* ../ccs/plugins/agent-tools/dist/
```

`VITE_BASE_PATH='./'` 使产物资源引用为相对路径（可挂载到子路径）。
