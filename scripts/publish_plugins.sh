#!/usr/bin/env bash
set -euo pipefail

# 插件市场发布脚本
#
# 职责：把仓库 plugins/ 下的每个插件（含 plugin.json 的目录）打包为
#   {name}-{version}.zip，上传到 GitHub Release 固定 tag（默认 plugin-market），
#   再生成并提交 plugins/plugins.json 索引（记录版本、下载地址、sha256 与大小）。
# 运行时插件市场改为拉取该索引与 Release zip，不再走 git/trees API，
# 避免 api.github.com 未认证 60 次/小时的限流（403）。
#
# 用法：
#   ./scripts/publish_plugins.sh             # 正常发布（打包 + 上传 + 生成索引 + 提交）
#   ./scripts/publish_plugins.sh --no-upload # 只打包 + 生成索引，不上传、不提交（验证用）
#   ./scripts/publish_plugins.sh --tag vX    # 指定 Release tag（默认 plugin-market）

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

REPO="nkHub/akm"
TAG="plugin-market"
INDEX_FILE="plugins/plugins.json"
UPLOAD=1
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# ── 参数解析 ──
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-upload) UPLOAD=0; shift ;;
    --tag) TAG="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

# ── 前置检查 ──
if ! command -v gh >/dev/null 2>&1; then
  echo "错误: 未找到 gh，请先执行: brew install gh && gh auth login"
  exit 1
fi

if [[ "$UPLOAD" == "1" ]] && ! gh auth status >/dev/null 2>&1; then
  echo "错误: 请先登录 GitHub CLI: gh auth login"
  exit 1
fi

# ── 1. 扫描插件目录 ──
PLUGINS=()
for dir in plugins/*/; do
  name="$(basename "$dir")"
  [[ "$name" == __pycache__ ]] && continue
  [[ -f "$dir/plugin.json" ]] || continue
  PLUGINS+=("$name")
done

if [[ ${#PLUGINS[@]} -eq 0 ]]; then
  echo "错误: plugins/ 下未找到任何带 plugin.json 的插件"
  exit 1
fi
echo "[1/4] 发现 ${#PLUGINS[@]} 个插件: ${PLUGINS[*]}"

# ── 2. 确保 Release 存在 ──
if [[ "$UPLOAD" == "1" ]]; then
  if ! gh release view "$TAG" >/dev/null 2>&1; then
    echo "[2/4] 创建 Release: $TAG"
    gh release create "$TAG" --title "AKM 插件市场" \
      --notes "AKM 插件市场发布通道，插件 zip 由 scripts/publish_plugins.sh 上传"
  fi
fi

# ── 3. 逐个打包 + 上传 ──
# 打包用 Python zipfile（与运行时解压端一致）：zip 根目录直接含 {name}/，
# 排除 .DS_Store / __pycache__ 等残留；同时计算 sha256 与大小。
echo "[3/4] 打包并生成索引"
python3 - "$WORK_DIR" "$REPO" "$TAG" "${PLUGINS[@]}" <<'PY'
import hashlib
import json
import sys
import time
import zipfile
from pathlib import Path

work_dir = Path(sys.argv[1])
repo = sys.argv[2]
tag = sys.argv[3]
names = sys.argv[4:]
exclude_parts = (".DS_Store", "__pycache__")

zip_paths = []
for name in names:
    plugin_dir = Path("plugins") / name
    version = json.loads((plugin_dir / "plugin.json").read_text("utf-8"))["version"]
    zip_path = work_dir / f"{name}-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 以 {name}/ 为 zip 根目录，保证解压后即插件目录
        for p in sorted(plugin_dir.rglob("*")):
            if p.is_dir() or any(part in exclude_parts for part in p.parts):
                continue
            zf.write(p, p.relative_to(plugin_dir.parent))
    zip_paths.append((name, version, zip_path))
    print(f"      打包 {name} (v{version}) -> {zip_path.name}")

entries = {}
for name, version, zip_path in zip_paths:
    meta = json.loads(Path(f"plugins/{name}/plugin.json").read_text("utf-8"))
    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    entries[name] = {
        "version": version,
        "description": meta.get("description", ""),
        "category": meta.get("category", ""),
        "has_menu": bool(meta.get("has_menu", False)),
        "zip_url": f"https://github.com/{repo}/releases/download/{tag}/{zip_path.name}",
        "sha256": sha256,
        "size": zip_path.stat().st_size,
    }

data = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "plugins": entries,
}
Path("plugins/plugins.json").write_text(
    json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8"
)
print(f"      已写入 {len(entries)} 个插件索引")
PY

# 上传到 Release（--clobber 覆盖同名资产，支持迭代发布）
if [[ "$UPLOAD" == "1" ]]; then
  for zip_path in "$WORK_DIR"/*.zip; do
    echo "      上传 ${zip_path##*/} 到 release $TAG"
    gh release upload "$TAG" "$zip_path" --clobber
  done
fi

if [[ "$UPLOAD" == "0" ]]; then
  echo "完成(--no-upload，未上传/未提交): 索引已生成 $INDEX_FILE"
  exit 0
fi

# ── 提交索引（仅 add 该文件，避免夹带无关改动）──
echo "提交索引: $INDEX_FILE"
git add "$INDEX_FILE"
if git diff --cached --quiet; then
  echo "索引无变化，跳过提交"
else
  git commit -m "chore(plugins): 更新插件市场索引（${TAG}）"
fi

echo "完成: 请 push 使索引生效 → git push origin main"
