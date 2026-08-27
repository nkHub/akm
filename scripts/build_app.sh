#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# 打包前先移开 pyproject.toml：py2app 会读取其 [project] dependencies 作为
# install_requires 并逐个解析，纯 C 扩展单文件模块（tinyaes）会在该阶段报
# "setup script specifies an absolute path"。各依赖已装在当前 Python 环境，
# modulegraph 依靠代码 import 自行收集，无需 pyproject 参与。
PYPROJECT_MOVED=0
cleanup() {
  if [[ "$PYPROJECT_MOVED" -eq 1 && -f "pyproject.toml.bak" ]]; then
    mv "pyproject.toml.bak" "pyproject.toml"
  fi
}
trap cleanup EXIT

if [[ -f "pyproject.toml" && ! -f "pyproject.toml.bak" ]]; then
  mv "pyproject.toml" "pyproject.toml.bak"
  PYPROJECT_MOVED=1
fi

# 强制使用项目指定的 Python 版本（.python-version），避免在项目外目录打包时
# 误用 pyenv global（可能为 3.14.4）导致 sqlite 不支持动态加载、向量功能失效
if [[ -f ".python-version" ]]; then
  PYENV_VERSION="$(cat .python-version)" python setup.py py2app
else
  python setup.py py2app
fi

# 打包后精简 pygments，只保留常用语言 lexer，缩小 app 体积
if [[ -f "scripts/trim_pygments.py" ]]; then
  python "scripts/trim_pygments.py" "dist/AI Key Manager.app" || echo "WARN: trim_pygments 失败，忽略"
fi

# tinyaes 是私有命名 C 扩展（tinyaes.cpython-312-darwin.so），py2app/modulegraph 无法收集它
#（includes/data_files 均报绝对路径），故在 setup.py 的 excludes 里排除后，
# 直接从当前 Python 环境的 site-packages 拷贝进 app 的 lib/python3.12（运行时 sys.path）。
TINY_SO="$(PYENV_VERSION="$(cat .python-version 2>/dev/null || true)" python -c "
import sysconfig
from pathlib import Path
p = Path(sysconfig.get_paths().get('purelib', ''))
for n in ('tinyaes.cpython-312-darwin.so', 'tinyaes.so'):
    c = p / n
    if c.exists():
        print(c)
        break
")"
if [[ -n "$TINY_SO" && -f "$TINY_SO" ]]; then
  cp "$TINY_SO" "dist/AI Key Manager.app/Contents/Resources/lib/python3.12/"
  echo "tinyaes 扩展已打入 app"
else
  echo "WARN: tinyaes.so 未找到，跳过（运行时 import tinyaes 将失败）"
fi

echo "Build complete: $ROOT_DIR/dist/AI Key Manager.app"
