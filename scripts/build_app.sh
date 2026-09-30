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

# py2app 已对应用签名，但后续资源精简和扩展补入会破坏资源封印；所有打包后处理
# 完成后重新签名，并在校验失败时阻止发布包生成。
#
# 签名身份优先级：AKM_SIGN_IDENTITY 环境变量 → 本机自签证书 "AKM Local Signing" →
# 回退 ad-hoc。ad-hoc 签名的 designated requirement 是 cdhash，每次重建都会变成
# 「新 App」，macOS 会重新询问桌面/文稿/下载等目录权限；换成同一张证书后，指定
# 要求只绑定 bundle id 与证书，重建不再改变身份，点过一次「允许」即长期有效。
# 证书可用 scripts/make_signing_cert.sh 生成（幂等，已存在则复用）。
SIGN_IDENTITY="${AKM_SIGN_IDENTITY:-}"
if [[ -z "$SIGN_IDENTITY" ]]; then
  # 先取回身份列表再原地匹配：`security ... | grep -q` 在 set -o pipefail 下可能因
  # grep 提前退出触发 SIGPIPE，把「有身份」误判成「没身份」而回退 ad-hoc。
  AVAILABLE_IDENTITIES="$(security find-identity -v -p codesigning 2>/dev/null || true)"
  if grep -qF '"AKM Local Signing"' <<<"$AVAILABLE_IDENTITIES"; then
    SIGN_IDENTITY="AKM Local Signing"
  fi
fi

if [[ -n "$SIGN_IDENTITY" ]]; then
  echo "签名身份: $SIGN_IDENTITY"
  codesign --force --deep --sign "$SIGN_IDENTITY" "dist/AI Key Manager.app"
else
  echo "WARN: 未找到稳定签名身份，回退 ad-hoc 签名。" >&2
  echo "WARN: ad-hoc 的指定要求是 cdhash，每次重建都会被 macOS 当成新 App，目录权限会反复弹窗。" >&2
  echo "WARN: 如需固化，先运行 scripts/make_signing_cert.sh，或设置 AKM_SIGN_IDENTITY=<身份名>。" >&2
  codesign --force --deep --sign - "dist/AI Key Manager.app"
fi
codesign --verify --deep --strict "dist/AI Key Manager.app"

echo "Build complete: $ROOT_DIR/dist/AI Key Manager.app"
