#!/usr/bin/env bash
# 启动平台：静态前端和 /api 由同一个 uvicorn 进程托管。
#
#   ./run.sh                本机打开 http://127.0.0.1:8000
#   ./run.sh worker         另开一个终端跑生成队列 worker（独立进程）
#   ./run.sh --reload       改代码自动重启（开发时用）
#   ./run.sh stop           停掉 API 与 worker（按 PID 文件，不模糊匹配）
#   FASTVIDEO_HOST=0.0.0.0 ./run.sh   对局域网开放（一期还没做登录，先别开）
#   FASTVIDEO_PORT=8080 ./run.sh      换端口
set -euo pipefail

# 后端在 app/backend，必须先切过去：main.py 的 import 是顶层的，
# 在别的目录跑 uvicorn main:app 会直接 ModuleNotFoundError
cd "$(dirname "$0")/backend"

HOST="${FASTVIDEO_HOST:-127.0.0.1}"
PORT="${FASTVIDEO_PORT:-8000}"
RUN_DIR="data/run"
LOG_DIR="data/logs"
API_PID="$RUN_DIR/api.pid"
WORKER_PID="$RUN_DIR/worker.pid"

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

# 按 PID 停一个进程：先 SIGTERM，等不到再 SIGKILL。
# 不再用 `pkill -f "worker.py"`——那条命令会误杀编辑器、grep、别的项目里
# 任何命令行恰好含这串字符的进程，而且不确认进程是真的退出了。
stop_one() {
  local pidfile="$1" name="$2" pid i
  if [ ! -f "$pidfile" ]; then
    echo "  ${name}：没有 PID 文件（可能没在跑）"
    return 1
  fi
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  rm -f "$pidfile"
  if [ -z "$pid" ]; then
    echo "  ${name}：PID 文件是空的"
    return 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "  ${name}：进程 ${pid} 已经不在了"
    return 1
  fi
  kill "$pid" 2>/dev/null || true
  i=0
  while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 50 ]; do
    sleep 0.1
    i=$((i + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "  ${name}：5 秒内没退出，发 SIGKILL（${pid}）"
    kill -9 "$pid" 2>/dev/null || true
  else
    echo "  ${name}：已停止（${pid}）"
  fi
  return 0
}

if [ "${1:-}" = "stop" ]; then
  # worker 是独立进程，stop 必须两样一起收：只停 API 会让队列不再前进，
  # 看起来像"作业卡住了"，其实是没人干活。
  echo "停止平台："
  stop_one "$API_PID" "API" || true
  stop_one "$WORKER_PID" "worker" || true
  exit 0
fi

mkdir -p "$RUN_DIR" "$LOG_DIR"

# 依赖指纹：只有 requirements.txt 真的变了才重装。
# 原来只判断 .venv/bin/python 在不在——`uv pip install` 中途断网失败后，
# 下次启动直接跳过安装，表现是 ModuleNotFoundError: fastapi，看着像代码坏了。
hash_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    # 两样都没有就退回 mtime：宁可多装一次，也不要漏装
    stat -f %m "$1" 2>/dev/null || stat -c %Y "$1"
  fi
}
STAMP=".venv/.requirements.sha256"
# 指纹要覆盖**两个**文件（2026-11-08）：`constraints.txt` 是钉住确切版本的
# 那一份，只改它（比如回退某个包）同样必须触发重装。原来只算 requirements.txt，
# 改 constraints 会静默不生效——那种"我明明改了配置却没反应"最难查。
WANT="$(hash_file requirements.txt)-$(hash_file constraints.txt 2>/dev/null || echo none)"

if [ ! -x .venv/bin/python ] || [ "$(cat "$STAMP" 2>/dev/null || true)" != "$WANT" ]; then
  echo "准备 .venv（首次运行或 requirements/constraints 变了）..."
  # `-c constraints.txt` 把版本收窄到"验证过的那一代"；文件不存在时退回
  # 只按 requirements 的范围装（老检出没有这个文件也不该起不来）。
  if [ -f constraints.txt ]; then
    CONSTRAINT_ARGS="-c constraints.txt"
  else
    CONSTRAINT_ARGS=""
  fi
  if command -v uv >/dev/null 2>&1; then
    [ -x .venv/bin/python ] || uv venv --python 3.12 .venv
    # shellcheck disable=SC2086  # 这里的展开是故意的：要么空、要么两个词
    uv pip install --python .venv/bin/python $CONSTRAINT_ARGS -r requirements.txt
  else
    [ -x .venv/bin/python ] || python3 -m venv .venv
    # shellcheck disable=SC2086
    .venv/bin/python -m pip install -q $CONSTRAINT_ARGS -r requirements.txt
  fi
  # 装成功才写指纹：中途失败时下次还会重试
  printf '%s' "$WANT" > "$STAMP"
fi

# 端口预检：被占用时明确说清楚，不要等 uvicorn 抛一屏 log 才让人猜。
check_port() {
  .venv/bin/python - "$1" "$2" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
sock = socket.socket()
try:
    sock.bind((host, port))
except OSError as exc:
    print(f"端口 {port} 已被占用（{exc}）；换一个 FASTVIDEO_PORT 再试", file=sys.stderr)
    sys.exit(1)
finally:
    sock.close()
PY
}

# 日志同时进终端和文件：终端滚屏后还能回头查，随机初始口令也不会丢。
# `exec > >(tee ...)` 不改变当前进程号，所以 PID 文件写的就是真正的服务进程。
start_with_log() {
  local logfile="$1"
  shift
  exec > >(tee -a "$logfile") 2>&1
  exec "$@"
}

if [ "${1:-}" = "worker" ]; then
  shift
  if [ -f "$WORKER_PID" ] && kill -0 "$(cat "$WORKER_PID" 2>/dev/null || echo 0)" 2>/dev/null; then
    echo "worker 已经在跑（PID $(cat "$WORKER_PID")）；要重启先 ./run.sh stop" >&2
    exit 1
  fi
  echo $$ > "$WORKER_PID"
  echo "worker 日志：$LOG_DIR/worker.log"
  start_with_log "$LOG_DIR/worker.log" .venv/bin/python worker.py "$@"
fi

if [ -f "$API_PID" ] && kill -0 "$(cat "$API_PID" 2>/dev/null || echo 0)" 2>/dev/null; then
  echo "API 已经在跑（PID $(cat "$API_PID")）；要重启先 ./run.sh stop" >&2
  exit 1
fi
check_port "$HOST" "$PORT"
echo $$ > "$API_PID"
echo "→ http://${HOST}:${PORT}（日志：$LOG_DIR/api.log）"
start_with_log "$LOG_DIR/api.log" .venv/bin/python -m uvicorn main:app \
  --host "$HOST" --port "$PORT" "$@"
