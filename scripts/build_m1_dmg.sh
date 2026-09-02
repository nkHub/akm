#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

APP_NAME="AI Key Manager"
APP_PATH="dist/${APP_NAME}.app"
DMG_BG_PATH="/tmp/akm-dmg-background.png"

if ! command -v create-dmg >/dev/null 2>&1; then
  echo "错误: 未找到 create-dmg，请先执行: brew install create-dmg"
  exit 1
fi

VERSION="$(python -c 'from akm import __version__; print(__version__)')"
ARCH="$(uname -m)"

if [[ "$ARCH" != "arm64" ]]; then
  echo "警告: 当前机器架构为 ${ARCH}，该脚本用于 Apple Silicon(M1/M2) 构建。"
fi

echo "[1/7] 清理旧构建产物"
rm -rf build dist

echo "[2/7] 使用 py2app 打包 .app"
# 复用现有构建脚本：其中包含 pyproject.toml 临时挪走/恢复逻辑，避免 py2app 与 PEP 517 配置冲突。
"$ROOT_DIR/scripts/build_app.sh"

if [[ ! -d "$APP_PATH" ]]; then
  echo "错误: 未找到打包产物: $APP_PATH"
  exit 1
fi

BIN_PATH="${APP_PATH}/Contents/MacOS/${APP_NAME}"
if [[ -f "$BIN_PATH" ]]; then
  echo "[3/7] 校验可执行架构"
  file "$BIN_PATH"
fi

DMG_PATH="dist/${APP_NAME}-${VERSION}-arm64.dmg"

# 生成 DMG 背景图：这里使用 Python + Pillow 动态生成，避免仓库额外维护二进制图片资源。
# 背景设计目标：浅色渐变 + 轻提示文案，用户打开 DMG 后可以直接看到“把左侧应用拖到右侧 Applications”。
echo "[4/7] 生成 DMG 背景图"
python - <<'PY'
from PIL import Image, ImageDraw

w, h = 624, 399
img = Image.new("RGB", (w, h), "#F6F8FB")
draw = ImageDraw.Draw(img)

# 自上而下渐变，避免纯色背景太平。
for y in range(h):
    r = int(246 + (230 - 246) * y / h)
    g = int(248 + (236 - 248) * y / h)
    b = int(251 + (246 - 251) * y / h)
    draw.line([(0, y), (w, y)], fill=(r, g, b))

# 左右区域的柔和高亮，上一版 480×307 的 1.3 倍缩放。
draw.rounded_rectangle((78, 117, 247, 286), radius=16, fill=(255, 255, 255, 220), outline=(220, 226, 235), width=2)
draw.rounded_rectangle((377, 117, 546, 286), radius=16, fill=(255, 255, 255, 220), outline=(220, 226, 235), width=2)

# 中间箭头提示（不依赖字体，避免目标机器缺字库导致渲染异常）。
draw.polygon([(298, 195), (338, 195), (338, 182), (364, 208), (338, 234), (338, 221), (298, 221)], fill=(120, 130, 150))

img.save("/tmp/akm-dmg-background.png", "PNG")
PY

echo "[5/7] 生成 DMG: $DMG_PATH"
# create-dmg 1.3.0 起已修复 .VolumeIcon.icns / .background 的隐藏处理，
# 不再需要针对旧版（1.2.3）的 sed 修补。
# 历史教训：旧修补用行首锚点 ^SetFile 还原，但目标行以 Tab 缩进导致还原永不匹配，
# 每次构建都会向 create-dmg 累积追加 SetFile -a V（曾累积到 61 处、2536 字符），
# 被污染的 create-dmg 生成 DMG 时背景图失效。此处保持直接调用，不做任何修补。
create-dmg \
  --volname "$APP_NAME" \
  --volicon "logo.icns" \
  --window-size 624 399 \
  --icon-size 77 \
  --text-size 11 \
  --background "$DMG_BG_PATH" \
  --icon "$APP_NAME.app" 165 199 \
  --app-drop-link 459 199 \
  "$DMG_PATH" \
  "$APP_PATH"

echo "[6/7] 生成 zip 更新包"
# 更新程序通过 ditto -x -k 解压，zip 根目录需直接包含 .app；
# 这里用 ditto 打包以便与解压端保持一致（保留权限与符号链接）。
ZIP_PATH="dist/${APP_NAME}-${VERSION}-arm64.zip"
pushd "$ROOT_DIR/dist" >/dev/null
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "${APP_NAME}.app" "${APP_NAME}-${VERSION}-arm64.zip"
popd >/dev/null

echo "完成: $DMG_PATH"
echo "完成: $ZIP_PATH"
