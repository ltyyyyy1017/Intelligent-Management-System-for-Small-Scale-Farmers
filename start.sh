#!/usr/bin/env bash
# 乡村财务智管家 · Linux / macOS 启动脚本
# 村委电脑多数是 Windows，这个脚本主要给答辩演示机、开发机与国产化环境用。
set -euo pipefail
cd "$(dirname "$0")"

PY=""
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "没找到 Python 3.11+，请先安装。" >&2
  exit 1
fi

"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 11):
    print("需要 Python 3.11 或更高版本，当前：%s" % sys.version)
    raise SystemExit(1)
EOF

if [ ! -f "data/village.db" ]; then
  echo "第一次使用：先建账。"
  "$PY" -m app.cli init
fi

exec "$PY" -m app.cli serve "$@"
