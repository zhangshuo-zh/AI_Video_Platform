"""从网页启停 worker：把「开一个终端敲 run.sh」收进界面，并守住三件事。

**这不是守护进程**（没有 supervise、没有自动拉起），是**手动启停**。
界面必须把这句话说出来——按钮存在会让人以为"崩了会自动恢复"，而事实不是，
这个错觉比没有按钮更危险。

为什么要按现在这套写法，而不是简单 `Popen(["python", "worker.py"])`：

1. **孤儿防护**：`start_new_session=True` 让 worker 自己成为一个新会话/进程组
   的组长。API 重启（或崩溃）时，worker 不会被连带杀掉、也不会挂在 API 的
   进程组下面变成没人认领的孤儿。`run.sh` 里 `--reload` 那段注释已经记过这个
   坑的另一种形态（uvicorn reloader 壳进程把 worker 带成孤儿）。

2. **PID 文件必须与 `run.sh` 共用同一个**（`data/run/worker.pid`）。否则
   网页起的 worker，`./run.sh stop` 找不到它；反过来 `run.sh` 起的，
   网页也停不掉。两个入口一份 PID，才谈得上"同一台机器只有一个 worker"。

3. **环境变量要显式补齐**：`run.sh` 在起 uvicorn **之前** export 了
   `FASTVIDEO_FASTMETAL_HOME` 与 `FASTVIDEO_MFLUX_BIN`（`run.sh:24-35`，
   注释原话「API 与 worker 都要带上，否则系统设置页和实际执行会看到两个答案」）。
   如果 API 进程自己是用 `run.sh` 起的，这两个值会继承给子进程；但若有人
   直接 `uvicorn main:app` 起的 API，子进程就没有——于是 worker 起得来却
   报 `adapter_unavailable`，而且报的位置离"网页上点了个按钮"很远。
   所以这里把 `run.sh` 那两条推导**照抄一份**（只抄条件与路径，不抄导出时机）。

为什么不干脆调用 `./run.sh worker`：那样会走 `exec`，把 API 进程**替换**掉
（`start_with_log()` 里的 `exec`）。必须直接起 `worker.py`。
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from config import BASE_DIR

# 与 `run.sh:18-22` 同一套路径。改这里必须同步改那边，否则两个入口各管各的
# PID 文件，`./run.sh stop` 与网页按钮会互相看不见对方起的进程。
RUN_DIR = BASE_DIR / "data" / "run"
LOG_DIR = BASE_DIR / "data" / "logs"
WORKER_PID = RUN_DIR / "worker.pid"
WORKER_LOG = LOG_DIR / "worker.log"

# 等 worker 真正写心跳的最长时间。worker 启动要跑 migrate + 建模板 + 收 stale，
# 首次（全新库）可能几秒；给到 15 秒，超时就如实说"起来了但没写心跳"，
# 不假装成功。这个数字不是拍的：`run.sh worker` 实测冷启动约 2—3 秒。
START_TIMEOUT_SECONDS = 15.0


# 本进程起过、还没 poll() 回收的 Popen。**不是缓存状态**，是回收句柄：
# 只有握住它才能在判活时用 poll() 顺带 waitpid——见 `_pid_alive` 的注释。
_LAUNCHED: dict[int, subprocess.Popen] = {}


def _reap(pid: int) -> None:
    """把本进程起的那一条收掉（非阻塞）。不是我起的就不动——别人家的孩子
    由它自己的父进程管，替它 wait 反而会出错。"""
    proc = _LAUNCHED.get(pid)
    if proc is None:
        return
    if proc.poll() is not None:
        _LAUNCHED.pop(pid, None)


def _pid_alive(pid: int) -> bool:
    """进程还在不在。

    **本进程起的用 `poll()`，别的一律 `kill -0`**（2026-11-09 实测踩到）：
    本进程起的 worker 退出后，在父进程 `wait()` 之前是个**僵尸**——
    `kill(pid, 0)` 对僵尸照样返回成功（进程表里还占着那个号），于是
    「SIGTERM 发了、进程其实 0.5 秒就退了、这里却判它活着」，8 秒后
    走强杀分支，回执写成"没退，已强杀"——**把一次干净的优雅退出
    报成了强杀**。`poll()` 会做 `waitpid(WNOHANG)`，僵尸当场被回收，
    判断就准了（实测同一场景：kill -0 判活 12 秒不退，poll 0.6 秒）。

    别人的进程没有 Popen 句柄可用，只能 kill -0；那种情况下僵尸问题不存在
    （僵尸的父进程是别人，轮不到我们判决）。
    """
    if pid <= 0:
        return False
    proc = _LAUNCHED.get(pid)
    if proc is not None:
        return proc.poll() is None
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid() -> int | None:
    """读 PID 文件。文件不存在／空／写坏都返回 None（不猜）。"""
    try:
        raw = WORKER_PID.read_text().strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def current() -> dict:
    """现在有没有 worker 在跑。**以 PID 文件 + kill -0 为准**，不靠内存里的状态。

    为什么不记"我起过"：API 重启后内存就没了，而 worker 还活着。那种情况下
    按钮会显示"未启动"，人一按就起第二个——两个进程抢同一份显存。
    """
    pid = read_pid()
    alive = _pid_alive(pid) if pid else False
    return {
        "running": alive,
        "pid": pid if alive else None,
        # PID 文件存在但进程没了＝上次退出没清理，如实报出来，别当成"从未启动"
        "stale_pid": pid if (pid and not alive) else None,
    }


def _worker_env() -> dict[str, str]:
    """给 worker 的环境：继承当前进程，并补齐 `run.sh` 那两条推导。

    `setdefault` 而不是直接赋值：API 进程里若已有（比如它是 `run.sh` 起的，
    或部署时显式配了），以那个为准——与 `run.sh` 的 `[ -z ... ]` 判断同义。
    """
    env = dict(os.environ)
    home = Path.home()
    if not env.get("FASTVIDEO_FASTMETAL_HOME") and (home / "FastVideo").is_dir():
        env["FASTVIDEO_FASTMETAL_HOME"] = str(home / "FastVideo")
    mflux = home / ".local" / "bin" / "mflux-generate"
    if not env.get("FASTVIDEO_MFLUX_BIN") and mflux.exists() and os.access(mflux, os.X_OK):
        env["FASTVIDEO_MFLUX_BIN"] = str(mflux)
    return env


def _python_executable() -> str:
    """用当前进程这一个解释器起 worker。

    不用 `shutil.which("python")`：那会拿到系统 Python，可能没装依赖。
    也不用 `.venv/bin/python` 的路径字符串——它是符号链接，直接交给
    `sys.executable` 更准（本机实测指向 pyenv 里的同一个 3.12）。
    """
    import sys
    return sys.executable


def start(*, max_global: int | None = None, per: dict | None = None) -> dict:
    """起一个 worker。已经在跑就拒绝（不重复起），起完等心跳确认。

    返回 `{ok, pid, message, ...}`：**`ok` 只在"进程活着"时为真**。
    进程起来了但没写心跳，也返回 ok=False 并说清楚——那是环境问题
    （缺运行时、迁移失败），不该显示成"启动成功"让人以为在干活。
    """
    state = current()
    if state["running"]:
        return {"ok": False, "pid": state["pid"],
                "message": f"worker 已经在跑（PID {state['pid']}）；要重启请先停止"}

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    argv = [_python_executable(), str(BASE_DIR / "worker.py")]
    if max_global is not None:
        argv += ["--max-global", str(int(max_global))]
    for name, value in (per or {}).items():
        argv += [f"--max-{name}", str(int(value))]

    # 日志追加写：与 `run.sh` 的 `tee -a` 同义，重启不丢上一次的输出。
    log = open(WORKER_LOG, "ab")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(BASE_DIR),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_worker_env(),
            # **孤儿防护**：新会话＝worker 成为独立进程组的组长。
            # API 重启/退出时不会连带杀掉它；它也不会挂在 API 的进程组下面
            # 被 `run.sh --reload` 那种壳进程带成孤儿。
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()      # 父进程不必握着这个句柄；子进程已经继承了一份

    _LAUNCHED[proc.pid] = proc
    WORKER_PID.write_text(str(proc.pid))

    # 等它写第一份心跳。worker 起来会先 migrate 再 heartbeat，
    # 这段等待是"确认真的在干活"而不是"确认 fork 成功"。
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            code = proc.returncode
            _cleanup_pid_if(proc.pid)
            tail = _log_tail()
            return {"ok": False, "pid": None,
                    "message": f"worker 启动后立刻退出（退出码 {code}）"
                               + (f"：{tail}" if tail else "")}
        if _heartbeat_written_since(proc.pid):
            return {"ok": True, "pid": proc.pid,
                    "message": f"worker 已启动（PID {proc.pid}）"}
        time.sleep(0.25)

    # 进程活着但没心跳：起来了却没登记，多半是环境/迁移问题。
    # **不报成功**——那样人会以为在跑，而队列其实不动。
    return {"ok": False, "pid": proc.pid,
            "message": f"worker 进程起来了（PID {proc.pid}）但 {START_TIMEOUT_SECONDS:.0f} 秒内"
                       f"没有写心跳，可能环境有问题；看 {WORKER_LOG.name} 的尾部"}


def _heartbeat_written_since(pid: int) -> bool:
    """这个 PID 有没有在 `worker_node` 里登记过。读库失败一律当"还没有"——
    宁可等满超时说"没确认"，也不要谎报成功。"""
    try:
        from db import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM worker_node WHERE pid = ? LIMIT 1", (pid,)
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _log_tail(lines: int = 6) -> str:
    """日志尾部，给"起不来"那句解释用。读不到就返回空，不抛。"""
    try:
        with open(WORKER_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    tail = [ln for ln in chunk.splitlines() if ln.strip()][-lines:]
    return " / ".join(tail)[:400]


def _cleanup_pid_if(pid: int) -> None:
    """PID 文件里是 `pid` 才删——避免误删别人（另一次启动）写进去的号。"""
    if read_pid() == pid:
        try:
            WORKER_PID.unlink()
        except OSError:
            pass


def stop(*, timeout: float = 8.0) -> dict:
    """停 worker：先 SIGTERM（它自己会收尾、把当前作业状态写回去），
    超时再 SIGKILL。与 `run.sh:39-68` 的 `stop_one` 同一套顺序与理由。

    worker 装了信号处理（`worker.py:925-927` 的 STOP 标志），SIGTERM 会让它
    跑完当前这一轮再退——所以给它 8 秒，不是 1 秒就强杀。
    """
    state = current()
    if not state["running"]:
        # PID 文件残留（上次强杀/断电）也顺手清掉，别让它一直红着
        if state["stale_pid"]:
            _cleanup_pid_if(state["stale_pid"])
            return {"ok": True, "pid": None,
                    "message": f"没有 worker 在跑（清掉了上次残留的 PID {state['stale_pid']}）"}
        return {"ok": True, "pid": None, "message": "没有 worker 在跑"}

    pid = state["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        _cleanup_pid_if(pid)
        return {"ok": False, "pid": pid, "message": f"发 SIGTERM 失败：{exc}"}

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _reap(pid)
        if not _pid_alive(pid):
            _cleanup_pid_if(pid)
            return {"ok": True, "pid": pid, "message": f"worker 已停止（PID {pid}）"}
        time.sleep(0.1)

    # 8 秒没退：跑着长作业时它可能正在收尾。强杀会留下 running 状态，
    # 由下一轮 `recover_stale` 收成 failed——这是既有机制，这里只如实说。
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    _cleanup_pid_if(pid)
    return {"ok": True, "pid": pid,
            "message": f"worker {timeout:.0f} 秒内没退，已强杀（PID {pid}）；"
                       f"它手上的作业会被下一轮 recover_stale 收掉"}
