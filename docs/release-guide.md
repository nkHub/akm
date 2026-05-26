# 打包与更新控制指南

## 一、版本号统一

### 当前问题

版本号 `0.1.0` 硬编码在 5 个位置，改版本需要同步修改，容易遗漏：

| 文件 | 位置 | 作用 |
|------|------|------|
| `pyproject.toml` | `version = "0.1.0"` | pip 包版本 |
| `setup.py` | `CFBundleVersion` / `CFBundleShortVersionString` | macOS .app 版本 |
| `akm/cli.py` | `@click.version_option(version="0.1.0")` | CLI 版本号显示 |
| `akm/server.py` | `FastAPI(version="0.1.0")` | API 服务版本 |
| `akm/templates/about.html` | `<span>0.1.0</span>` | 关于页面展示 |

### 解决方案

在 `akm/__init__.py` 中定义唯一版本源，各处引用：

```python
# akm/__init__.py
__version__ = "0.1.0"
```

```python
# setup.py
from akm import __version__
# ...
"CFBundleVersion": __version__,
```

```python
# cli.py
from akm import __version__
@click.version_option(version=__version__, prog_name="akm")
```

服务器端可以新增一个 `/api/version` 端点供前端和外部检查使用，关于页面通过 API 动态获取。

---

## 二、打包流程

### 环境要求

- Python 3.12.13
- macOS（py2app 仅支持 macOS）

### 标准打包命令

```bash
# 1. 安装打包依赖
pip install py2app pillow setuptools

# 2. 临时移走 pyproject.toml（与 py2app 冲突）
mv pyproject.toml pyproject.toml.bak

# 3. 打包生成 dist/AI Key Manager.app
python setup.py py2app

# 4. 恢复 pyproject.toml
mv pyproject.toml.bak pyproject.toml

# 5. 如需清理旧构建缓存
rm -rf build dist && python setup.py py2app
```

### 构建产物结构

```
dist/AI Key Manager.app/
└── Contents/
    ├── Info.plist              # 应用元数据（版本号、LSUIElement 等）
    ├── MacOS/
    │   └── AI Key Manager      # 可执行入口
    ├── Resources/
    │   ├── logo.icns            # 应用图标
    │   ├── logo.png
    │   ├── templates/           # HTML 模板
    │   └── static/              # JS 静态资源
    └── Frameworks/
        └── libpython3.12.dylib  # 嵌入 Python 运行时
```

### 分发

打包完成后 `.app` 可以直接分发。如需分发，推荐 DMG 或 zip 压缩：

```bash
# 创建 zip 分发包
cd dist && zip -r "AI Key Manager-$(python -c 'from akm import __version__; print(__version__)').zip" "AI Key Manager.app"

# 或创建 DMG（需要 create-dmg 工具）
brew install create-dmg
create-dmg --volname "AI Key Manager" --volicon "../logo.icns" "AI Key Manager.dmg" "AI Key Manager.app/"
```

---

## 三、更新控制方案（选型）

### 方案 A：无需自动更新（当前状态）

**适用场景**：开发测试阶段，手动分发

**工作流**：
1. 修改 `akm/__init__.py` 中的版本号
2. 打包新版本
3. 手动传给用户替换

---

### 方案 B：Sparkle 框架（macOS 标准方案）

**适用场景**：正式发布，用户体验最好

**原理**：Sparkle 是 macOS 应用标准自动更新框架，`.app` 启动后定期检查 appcast XML，发现新版本弹窗提示下载安装。

**改造步骤**：

1. 集成 sparkle2 Python 绑定：
```bash
pip install sparkle2
```

2. `akm/menubar.py` 中添加更新检查：
```python
from sparkle2 import SparkleUpdater

updater = SparkleUpdater(
    appcast_url="https://your-server.com/appcast.xml",
    auto_check_interval=86400  # 每天检查一次（秒）
)
```

3. 托管 appcast.xml 和更新包。appcast 格式：
```xml
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
<channel>
  <title>AI Key Manager 更新</title>
  <item>
    <title>0.2.0</title>
    <sparkle:version>0.2.0</sparkle:version>
    <sparkle:shortVersionString>0.2.0</sparkle:shortVersionString>
    <description>新增功能 X，修复问题 Y</description>
    <enclosure url="https://your-server.com/AI%20Key%20Manager-0.2.0.zip"
               sparkle:version="0.2.0"
               length="12345678"
               type="application/octet-stream"/>
  </item>
</channel>
</rss>
```

4. 如果需要签名（macOS 要求），还需为 `.app` 进行代码签名和公证。

---

### 方案 C：GitHub Release 检查（轻量方案）

**适用场景**：托管在 GitHub，用户群体偏技术

**原理**：启动时请求 GitHub Releases API 获取最新版本号，与本地比对，有更新则在菜单栏添加「新版可用」提示。

**实现示例** (`akm/menubar.py`)：

```python
import httpx
from akm import __version__

GITHUB_REPO = "nkHub/akm"
CHECK_INTERVAL = 86400  # 每天检查一次

def check_update():
    """检查 GitHub Release 是否有新版本"""
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            timeout=10
        )
        if resp.status_code == 200:
            latest = resp.json()["tag_name"].lstrip("v")
            if latest != __version__:
                return {
                    "has_update": True,
                    "latest": latest,
                    "current": __version__,
                    "url": resp.json()["html_url"]
                }
    except Exception:
        pass
    return {"has_update": False}
```

然后在菜单栏动态添加「更新到 vX.X.X」菜单项，点击打开浏览器到 Release 页面。

**优点**：无需托管额外文件，完全免费  
**缺点**：用户需手动下载替换 `.app`

---

### 方案 D：自建服务器检查

**适用场景**：自有分发渠道，需要控制推送节奏

**原理**：定期请求自定义 `/api/update?version=xxx` 端点，服务器返回最新版本信息和下载链接。

**实现**：
1. 在 AKM 服务端新增 `/api/update` 端点（或使用独立服务）
2. `menubar.py` 启动时检查，返回 `{latest_version, download_url, changelog}`

**优点**：完全自主控制，支持灰度发布  
**缺点**：需要维护额外服务端逻辑

---

## 四、版本号规范

推荐 **[语义化版本 SemVer](https://semver.org/lang/zh-CN/)**：

| 类型 | 格式 | 示例 |
|------|------|------|
| 主版本号 | MAJOR.MINOR.PATCH | `0.1.0` → `0.2.0` |
| 预发布 | MAJOR.MINOR.PATCH-beta.N | `0.2.0-beta.1` |

更新策略：
- **PATCH** (`0.1.0` → `0.1.1`)：Bug 修复，向后兼容
- **MINOR** (`0.1.0` → `0.2.0`)：新功能，向后兼容
- **MAJOR** (`0.1.0` → `1.0.0`)：重大变更，不兼容旧版本

---

## 五、发布清单（每版发布前检查）

- [ ] `akm/__init__.py` 版本号已更新
- [ ] 功能开发完成，本地测试通过
- [ ] 清理构建缓存：`rm -rf build dist`
- [ ] 执行打包命令
- [ ] 验证 `.app` 可正常启动
- [ ] 创建分发包（zip / DMG）
- [ ] 上传到分发渠道
- [ ] 如需推送更新提示，更新 appcast.xml 或 GitHub Release
