#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

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

python setup.py py2app

echo "Build complete: $ROOT_DIR/dist/AI Key Manager.app"
