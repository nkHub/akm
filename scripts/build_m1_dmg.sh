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

echo "[1/4] 清理旧构建产物"
rm -rf build dist

echo "[2/4] 使用 py2app 打包 .app"
# 复用现有构建脚本：其中包含 pyproject.toml 临时挪走/恢复逻辑，避免 py2app 与 PEP 517 配置冲突。
"$ROOT_DIR/scripts/build_app.sh"

if [[ ! -d "$APP_PATH" ]]; then
  echo "错误: 未找到打包产物: $APP_PATH"
  exit 1
fi

BIN_PATH="${APP_PATH}/Contents/MacOS/${APP_NAME}"
if [[ -f "$BIN_PATH" ]]; then
  echo "[3/4] 校验可执行架构"
  file "$BIN_PATH"
fi

DMG_PATH="dist/${APP_NAME}-${VERSION}-arm64.dmg"

# 生成 DMG 背景图：这里使用 Python + Pillow 动态生成，避免仓库额外维护二进制图片资源。
# 背景设计目标：浅色渐变 + 轻提示文案，用户打开 DMG 后可以直接看到“把左侧应用拖到右侧 Applications”。
echo "[4/5] 生成 DMG 背景图"
python - <<'PY'
from PIL import Image, ImageDraw

w, h = 960, 600
img = Image.new("RGB", (w, h), "#F6F8FB")
draw = ImageDraw.Draw(img)

# 自上而下渐变，避免纯色背景太平。
for y in range(h):
    r = int(246 + (230 - 246) * y / h)
    g = int(248 + (236 - 248) * y / h)
    b = int(251 + (246 - 251) * y / h)
    draw.line([(0, y), (w, y)], fill=(r, g, b))

# 左右区域的柔和高亮，缩小框体避免视觉压迫。
draw.rounded_rectangle((120, 180, 380, 440), radius=24, fill=(255, 255, 255, 220), outline=(220, 226, 235), width=2)
draw.rounded_rectangle((580, 180, 840, 440), radius=24, fill=(255, 255, 255, 220), outline=(220, 226, 235), width=2)

# 中间箭头提示（不依赖字体，避免目标机器缺字库导致渲染异常）。
draw.polygon([(458, 300), (520, 300), (520, 280), (560, 320), (520, 360), (520, 340), (458, 340)], fill=(120, 130, 150))

img.save("/tmp/akm-dmg-background.png", "PNG")
PY

echo "[5/5] 生成 DMG: $DMG_PATH"
create-dmg \
  --volname "$APP_NAME" \
  --volicon "logo.icns" \
  --window-size 960 600 \
  --icon-size 128 \
  --background "$DMG_BG_PATH" \
  --icon "$APP_NAME.app" 250 315 \
  --app-drop-link 710 315 \
  "$DMG_PATH" \
  "$APP_PATH"

echo "完成: $DMG_PATH"
