#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo '请先按 README 创建虚拟环境并安装依赖。'
  exit 1
fi
.venv/bin/python -m app.bootstrap
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
