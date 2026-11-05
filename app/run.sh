#!/usr/bin/env bash
# 启动平台：静态前端和 /api 由同一个 uvicorn 进程托管。
#
#   ./run.sh                本机打开 http://127.0.0.1:8000
#   ./run.sh worker         另开一个终端跑生成队列 worker（独立进程）
#   ./run.sh --reload       改代码自动重启（开发时用）
#   ./run.sh stop           停掉 API 与 worker
#   FASTVIDEO_HOST=0.0.0.0 ./run.sh   对局域网开放（一期还没做登录，先别开）
#   FASTVIDEO_PORT=8080 ./run.sh      换端口
set -euo pipefail

# 后端在 app/backend，必须先切过去：main.py 的 import 是顶层的，
# 在别的目录跑 uvicorn main:app 会直接 ModuleNotFoundError
cd "$(dirname "$0")/backend"

HOST="${FASTVIDEO_HOST:-127.0.0.1}"
PORT="${FASTVIDEO_PORT:-8000}"

# 本机已有 FastVideo 仓库时给 W09 的 FastMetal adapter 一个默认运行时。
# 这不是写死路径：只是 $HOME/FastVideo 存在才取，换机器/换目录用环境变量覆盖。
# API 与 worker 都要带上，否则系统设置页和实际执行会看到两个答案。
if [ -z "${FASTVIDEO_FASTMETAL_HOME:-}" ] && [ -d "$HOME/FastVideo" ]; then
  export FASTVIDEO_FASTMETAL_HOME="$HOME/FastVideo"
fi

# mflux 由 `uv tool install` 装在 ~/.local/bin，那个目录默认不在 PATH 里。
# 只在可执行文件真的存在时补默认值，换机器/换安装位置仍用环境变量覆盖。
if [ -z "${FASTVIDEO_MFLUX_BIN:-}" ] && [ -x "$HOME/.local/bin/mflux-generate" ]; then
  export FASTVIDEO_MFLUX_BIN="$HOME/.local/bin/mflux-generate"
fi

if [ "${1:-}" = "stop" ]; then
  # worker 是独立进程，stop 必须两样一起收：只停 API 会让队列不再前进，
  # 看起来像"作业卡住了"，其实是没人干活。
  stopped=0
  pkill -f "uvicorn main:app" && stopped=1
  pkill -f "worker.py" && stopped=1
  if [ "$stopped" = 1 ]; then echo "已停止"; else echo "没有在跑的实例"; fi
  exit 0
fi

# 首次运行自动建环境和装依赖；之后这一步直接跳过
if [ ! -x .venv/bin/python ]; then
  echo "首次运行：创建 .venv 并安装依赖 ..."
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    python3 -m venv .venv
    .venv/bin/python -m pip install -q -r requirements.txt
  fi
fi

if [ "${1:-}" = "worker" ]; then
  shift
  exec .venv/bin/python worker.py "$@"
fi

echo "→ http://${HOST}:${PORT}"
# exec 让 uvicorn 顶替当前进程，Ctrl+C 能干净退出，不留孤儿进程
exec .venv/bin/python -m uvicorn main:app --host "$HOST" --port "$PORT" "$@"
