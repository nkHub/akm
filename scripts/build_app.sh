#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# 强制使用项目指定的 Python 版本（.python-version），避免在项目外目录打包时
# 误用 pyenv global（可能为 3.14.4）导致 sqlite 不支持动态加载、向量功能失效
if [[ -f ".python-version" ]]; then
  PYENV_VERSION="$(cat .python-version)" python setup.py py2app
else
  python setup.py py2app
fi

PYPROJECT_MOVED=0
cleanup() {
  if [[ "$PYPROJECT_MOVED" -eq 1 && -f "pyproject.toml.bak" ]]; then
    mv "pyproject.toml.bak" "pyproject.toml"
  fi
}
trap cleanup EXIT

if [[ -f "pyproject.toml" ]]; then
  mv "pyproject.toml" "pyproject.toml.bak"
  PYPROJECT_MOVED=1
fi

# 打包后精简 pygments，只保留常用语言 lexer，缩小 app 体积
if [[ -f "scripts/trim_pygments.py" ]]; then
  python "scripts/trim_pygments.py" "dist/AI Key Manager.app" || echo "WARN: trim_pygments 失败，忽略"
fi

echo "Build complete: $ROOT_DIR/dist/AI Key Manager.app"
