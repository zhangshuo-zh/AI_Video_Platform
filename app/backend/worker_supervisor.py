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
import threading
import time
from pathlib import Path

from config import BASE_DIR

# 与 `run.sh:18-22` 同一套路径。改这里必须同步改那边，否则两个入口各管各的
# PID **目录**（0049，原来是单个 worker.pid）：一个 worker 一个文件。
#
# 为什么从单文件改成目录：多开时一个 grep 用的号放不下第二个进程。
# 文件名是 pid（`<pid>.pid`），内容也写 pid——两者一致，运维时
# `ls data/run/workers/` 就能数出有几个在跑，不用解析内容。
#
# 为兼容 `./run.sh stop`（它认的是 data/run/worker.pid），那边仍由
# `run.sh` 自己维护；这里只管网页起的。**两边不互相看不见**靠的不是共用
# 同一个文件，而是"都在同一台机器的进程表里"——`stop` 之外的操作都按
# 实际进程判活，不靠文件。
RUN_DIR = BASE_DIR / "data" / "run"
PID_DIR = RUN_DIR / "workers"
LOG_DIR = BASE_DIR / "data" / "logs"
# 保留旧名字指向第一个 PID 文件的位置，仅为兼容可能存在的引用与测试。
WORKER_PID = RUN_DIR / "worker.pid"
WORKER_LOG = LOG_DIR / "worker.log"

# 等 worker 真正写心跳的最长时间。worker 启动要跑 migrate + 建模板 + 收 stale，
# 首次（全新库）可能几秒；给到 15 秒，超时就如实说"起来了但没写心跳"，
# 不假装成功。这个数字不是拍的：`run.sh worker` 实测冷启动约 2—3 秒。
START_TIMEOUT_SECONDS = 15.0


# 本进程起过、还没 poll() 回收的 Popen。**不是缓存状态**，是回收句柄：
# 只有握住它才能在判活时用 poll() 顺带 waitpid——见 `_pid_alive` 的注释。
_LAUNCHED: dict[int, subprocess.Popen] = {}

# 起停串行化。**这是修一个真竞态**（2026-11-10 复核）：
#
# `start()` 的判断是「先读当前有几个，再逐个 fork」——两步之间没有原子性。
# FastAPI 的同步路由跑在线程池里，所以两个管理员同时点「启动」、或者
# 一个管理员点启动的同时守护线程正好在处理 `need`，两个调用会**各自读到
# 同一个 already**，各自都通过"不会超限"的检查，然后各起一批：
#
#     already=7，两个 start(count=1) 并发 → 都看到 7+1≤8 → 都起 → 实际 9 个
#
# 后果不只是数字难看：`MAX_WORKERS=8` 的存在理由就是"这台机器的内存撑不住
# 那么多"（见 start 的注释），突破上限正是它要防的事。
#
# 用一把普通 `Lock` 而不是 RLock：`start()` 内部会调 `current()`，
# 而 `current()` 不取这把锁（它只读文件与进程表），所以不会自锁。
# 锁只在**本进程**内有效——跨进程要靠 PID 目录，但起停本来就只从 API 走
# （`./run.sh worker` 那条路是人手工敲的，不会和界面点在同一毫秒上）。
_LOCK = threading.Lock()


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
    # **`kill -0` 成功不等于活着**：等父进程 `wait()` 之前它是僵尸（`ps stat=Z`），
    # 照样返回成功。本进程起的用上面的 `poll()` 就能回收；但 `stop_one` 面对的是
    # **别处起的** worker（`./run.sh worker`，或 API 重启前那一个），那些没有 Popen
    # 句柄，只能靠这条兜底。
    #
    # 不看这一条会怎样（实测）：`stop_one` 对别人的 worker 发完 SIGTERM，
    # 它 0.3 秒就退了、变成僵尸，而这里判"还活着"→ 等满 8 秒 → 走 SIGKILL 分支 →
    # 回执写成「8 秒内没退，已强杀」。一次干净的退出被报成强杀。
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3)
        return not out.stdout.strip().startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return True      # 查不到就按"活着"处理：宁可多等，不要误判成已退


def _pid_files() -> list[Path]:
    """`PID_DIR` 下所有 `<pid>.pid`，按 pid 排序。目录不存在返回空表。"""
    try:
        return sorted(PID_DIR.glob("*.pid"),
                      key=lambda f: int(f.stem) if f.stem.isdigit() else 0)
    except OSError:
        return []


def read_pid() -> int | None:
    """第一个活着的 worker 的 pid（兼容只关心"有没有人干活"的调用方）。

    多开之后"那一个 pid"不再有唯一答案，所以这里返回**任意一个活着的**：
    真需要全部的地方用 `list_workers()`。返回 None 表示一个都没在跑。
    """
    for item in list_workers():
        if item["alive"]:
            return item["pid"]
    return None


def list_workers() -> list[dict]:
    """这台机器上所有登记过的 worker：`[{pid, alive, stale}]`。

    `stale=True` 表示 PID 文件还在但进程已经没了（上次强杀/崩溃留下的）。
    这种情况**如实返回而不自动清**——调用方（看板）要能看到"有个号没清"，
    悄悄删掉就等于把一次异常消失得无影无踪。
    """
    out = []
    for path in _pid_files():
        try:
            pid = int(path.stem)
        except ValueError:
            continue
        _reap(pid)
        # **只探一次**：原来 alive 与 stale 各调一次 `_pid_alive`，
        # 除了白跑（我给 `_pid_alive` 加的 `ps` 兜底是子进程，8 个 worker
        # 就是 16 次），还有个真的正确性问题——两次调用之间进程若退出了，
        # 会返回 `alive=True, stale=True` 这种自相矛盾的一行，
        # 而 `current()` 拿它同时算 live 和 stale，界面就会显示
        # "在跑 1 个"同时还"残留 1 个"，同一个 pid 出现两次。
        alive = _pid_alive(pid)
        out.append({"pid": pid, "alive": alive, "stale": not alive})
    return out


def current() -> dict:
    """汇总：有几个 worker 在跑、分别是哪些 pid、有没有残留。

    **以 PID 目录 + 进程探测为准**，不靠内存里的状态——API 重启后内存没了而
    worker 还活着，记内存会让界面显示"未启动"，人一按就起第二个。
    """
    workers = list_workers()
    live = [w for w in workers if w["alive"]]
    stale = [w["pid"] for w in workers if w["stale"]]
    return {
        "running": bool(live),
        "count": len(live),
        "pids": [w["pid"] for w in live],
        # 兼容单实例调用方（旧字段）
        "pid": live[0]["pid"] if live else None,
        "stale_pids": stale,
    }


def _write_pid_file(pid: int) -> Path:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    path = PID_DIR / f"{pid}.pid"
    path.write_text(str(pid))
    return path


def _remove_pid_file(pid: int) -> None:
    try:
        (PID_DIR / f"{pid}.pid").unlink()
    except OSError:
        pass


def clean_stale() -> list[int]:
    """清掉所有"文件在、进程没了"的登记。返回清掉的 pid 列表。

    只在**人明确要求**时调（界面上的"清理残留"），不在读取时自动清——
    自动清会让"上次崩过"这件事不可见，而它正是排查的起点。
    """
    removed = []
    for item in list_workers():
        if item["stale"]:
            _remove_pid_file(item["pid"])
            removed.append(item["pid"])
    return removed


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


# 单机 worker 个数上限。为什么要有：每个 worker 占一份 Python 解释器 + 模型
# 进程，开到 8 个在 16 GB 机器上必然 OOM。这不是业务限制，是防呆——
# 与 `concurrency_plan` 的内存评估互补：那个算"每条作业要多少"，这个挡"别开太多"。
MAX_WORKERS = 8


def start(*, max_global: int | None = None, per: dict | None = None,
          count: int = 1) -> dict:
    """起 `count` 个 worker（默认 1 个）。**逐个等心跳确认**，不是批量 fork 了事。

    0049 起允许多开：原来"已经在跑就拒绝"是单实例时代的规矩，现在多开是
    正常需求（`claim_next` 本来就靠 `BEGIN IMMEDIATE` 保证不重复领取）。
    仍然拒绝的情况只剩两种：超过 `MAX_WORKERS`（防呆）、以及单个 worker
    起不来（环境问题）。

    返回 `{ok, pids, message, failed}`：`ok` 只在**全部**起来了才是真。
    部分成功时 `pids` 给出真的那几个，`failed` 说清哪一步没成。

    **锁只包住"读已有数 → 检查上限 → 逐个 fork"，不包等心跳。**
    等心跳最长 `START_TIMEOUT_SECONDS`（15 秒）× 个数，多开时能到两分钟，
    把这期间所有并发启动全堵死是不可接受的；而锁真正要防的是"两个调用
    各自读到同一个 already、各起一批"——那个窗口到 fork 结束就关闭了。
    """
    count = int(count or 1)
    if count < 1:
        return {"ok": False, "pids": [], "failed": [],
                "message": f"数量必须 ≥ 1，给的是 {count}"}

    with _LOCK:
        begun = _fork_workers(count=count, max_global=max_global, per=per)
    if "message" in begun:                  # 被上限拒绝，一个都没起
        return begun
    return _await_heartbeats(begun["procs"], begun["failed"],
                             count=count, since=begun["since"])


def _fork_workers(*, count: int, max_global: int | None,
                  per: dict | None) -> dict:
    """锁内部分：检查上限并 fork。**调用方必须已持有 `_LOCK`**。

    返回 `{"procs": [(pid, 序号, Popen)], "failed": [...]}`；被上限拒绝时
    返回带 `message` 的失败 dict（一个进程都没起）。等心跳在锁外做。

    **proc 对象要一起带出来**，别让锁外的确认阶段回头查 `_LAUNCHED`：
    `_LOCK` 在 fork 结束就释放（见 `start()` 的注释），而 `_reap()`
    会被并发的 `current()`／守护线程调用，从 `:355` 等到第 N 个之前，
    第 N 个完全可能已经退出并被弹掉。那时 `_LAUNCHED.get(pid)` 是 None，
    2026-11-10 审计实测的后果是两条：死进程被报成「起来了但没写心跳」
    （把人指向环境/迁移问题），而且 PID 文件不删、永久留在盘上。
    """
    already = current()["count"]
    if count > MAX_WORKERS or already + count > MAX_WORKERS:
        return {"ok": False, "pids": [], "failed": [],
                "message": f"已在跑 {already} 个、要求再起 {count} 个，"
                           f"超过上限 {MAX_WORKERS}；这台机器的内存撑不住那么多"}

    # 2026-11-10 审计修的真 bug：这两句原来裸着。`data/run`／`data/logs`
    # 被同名普通文件占住、权限被改、磁盘满 → `OSError` 一路穿透
    # `_fork_workers` → 穿透 `start()`（`with _LOCK` 不吞异常）→
    # 路由 `routers/jobs.py` 没有 try/except → **HTTP 500**，
    # 而且那句"没起来"的原因被丢进 traceback，界面只看得到 500。
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "pids": [], "failed": [],
                "message": f"worker 的运行目录／日志目录准备失败：{exc}"
                           f"（RUN_DIR={RUN_DIR}，LOG_DIR={LOG_DIR}）"}

    argv = [_python_executable(), str(BASE_DIR / "worker.py")]
    if max_global is not None:
        argv += ["--max-global", str(int(max_global))]
    for name, value in (per or {}).items():
        argv += [f"--max-{name}", str(int(value))]

    procs: list[tuple[int, int, object]] = []
    failed: list[str] = []
    # **本轮的绝对基线**，取在第一个 fork 之前。锁外的确认阶段用它筛掉
    # `worker_node` 里属于上一任的死记录（见 `_heartbeat_written_since`）。
    # 只能是"fork 之前"：先起的那个 worker 完全可能在 `_fork_workers` 返回
    # 之后、`_await_heartbeats` 开头之前就已经写好心跳——基线取晚了会把
    # **真活着**的它误判成死的，那比原来的 bug 还糟。
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    for index in range(count):
        # 日志追加写：与 `run.sh` 的 `tee -a` 同义，重启不丢上一次的输出。
        # `open` 只在这一个循环体内驻留，避免多开时句柄堆积。
        try:
            log = open(WORKER_LOG, "ab")
        except OSError as exc:
            failed.append(f"第 {index + 1} 个的日志打不开（{WORKER_LOG}）：{exc}")
            break
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(BASE_DIR),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=_worker_env(),
                # **孤儿防护**：新会话＝worker 成为独立进程组的组长。
                # API 重启/退出时不会连带杀掉它；也不会挂在 API 的进程组下面
                # 被 `run.sh --reload` 那种壳进程带成孤儿。
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            failed.append(f"第 {index + 1} 个没能 fork：{exc}")
            break
        finally:
            log.close()      # 父进程不必握着这个句柄；子进程已经继承了一份

        # 2026-11-10 审计修的真 bug：`_write_pid_file` 原来在 try 之外，
        # 而它的 `PID_DIR.mkdir` + `write_text` 没有任何保护。fork 已经成功、
        # 进程**真的活着**了，这时登记失败会留下一个谁都管不到的孤儿：
        # 它不在 `list_workers()` 里（没有登记文件），`current()["count"]=0`，
        # `stop_one` 以"不是本机登记的 worker"拒绝操作，`stop()` 报"没有 worker 在跑"
        # → **界面上没有任何入口能停掉它**；而 `MAX_WORKERS` 数的正是
        # `current()["count"]`，于是防它对它完全不设防，每点一次启动多一个。
        # 宁可这一个算失败，也绝不能留野进程：先杀再报。
        try:
            _write_pid_file(proc.pid)
        except OSError as exc:
            # 不 pop `_LAUNCHED`：登记（`:380`）在这之后才做，此刻表里没有它。
            try:
                proc.kill()
            except OSError:
                pass
            failed.append(
                f"第 {index + 1} 个已经 fork（PID {proc.pid}）但登记失败，"
                f"已把它杀掉以免变成无人认领的孤儿：{exc}")
            break
        _LAUNCHED[proc.pid] = proc
        procs.append((proc.pid, index, proc))

    return {"procs": procs, "failed": failed, "since": since}


def _await_heartbeats(procs: list[tuple[int, int, object]], failed: list[str],
                      *, count: int, since: str) -> dict:
    """锁外部分：逐个等 worker 写第一份心跳。

    worker 起来会先 migrate 再 heartbeat，这段等待是"确认真的在干活"
    而不是"确认 fork 成功"。
    """
    pids: list[int] = []
    for pid, index, proc in procs:
        # `proc` 由 `_fork_workers` 直接交到手上看，**不再回头查 `_LAUNCHED`**
        # （那行 `proc = _LAUNCHED.get(pid)` 是本条的真 bug：并发的 `_reap()`
        # 能把还没轮到确认的那一条弹掉，于是 `proc is None`，
        # 「已退出」这条判据永远不成立，报错文案指错方向，PID 文件也不删。
        # 详见 `_fork_workers` 的文档。）
        ok = False
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if proc is not None and proc.poll() is not None:
                code = proc.returncode
                _remove_pid_file(pid)
                tail = _log_tail()
                failed.append(f"第 {index + 1} 个启动后立刻退出（退出码 {code}）"
                              + (f"：{tail}" if tail else ""))
                break
            if _heartbeat_written_since(pid, since=since):
                ok = True
                pids.append(pid)
                break
            time.sleep(0.25)
        else:
            # 进程活着但没心跳：起来了却没登记，多半是环境/迁移问题。
            # **不报成功**——那样人会以为在跑，而队列其实不动。
            failed.append(
                f"第 {index + 1} 个（PID {pid}）进程起来了但 "
                f"{START_TIMEOUT_SECONDS:.0f} 秒内没有写心跳；看 worker.log 尾部")
        # **这里不能 break**（2026-11-10 审计修的真 bug）。
        #
        # 原来这句是 `if not ok: break`，理由写着"环境问题多半对下一个也一样"。
        # 那个理由把时序搞反了：fork 全部发生在锁内的 `_fork_workers()`
        # （`:322-339`，每个都已经 `_LAUNCHED[pid]` 登记并写了 PID 文件），
        # 这里中断的是**确认**，不是**启动**。于是第 1 个失败之后，
        # 第 2..N 个明明已经起来、心跳也写了，却既不进 `pids`（说"起来了"）
        # 也不进 `failed`（说"没起来"）——报告里查无此人。
        #
        # 后果是一条链：操作者看到的 HTTP 400 只提第 1 个 → 以为什么都没起 →
        # 再点一次启动 → 又 fork 一批 → 那些无人认领的 worker 一直累积到
        # `MAX_WORKERS`；更糟的是 `worker_daemon` 下一拍用 `current()["count"]`
        # 数出它们，于是面板显示"在跑 N 个"，而启动从来没人确认过它们。
        #
        # 代价是等待时长仍是文档里那句「15 秒 × 个数」，本来就没省下来。

    if pids and not failed:
        return {"ok": True, "pids": pids, "failed": [],
                "message": (f"已启动 {len(pids)} 个 worker"
                            + (f"（PID {'、'.join(map(str, pids))}）" if count > 1
                               else f"（PID {pids[0]}）"))}
    if pids:
        return {"ok": False, "pids": pids, "failed": failed,
                "message": f"只起来 {len(pids)} 个（PID {'、'.join(map(str, pids))}）；"
                           + "；".join(failed)}
    return {"ok": False, "pids": [], "failed": failed,
            "message": "；".join(failed) or "一个都没起来"}


def _heartbeat_written_since(pid: int, *, since: str) -> bool:
    """这个 PID 有没有在 `since`（本地时间串）**之后**写过心跳。
    读库失败一律当"还没有"——宁可等满超时说"没确认"，也不要谎报成功。

    2026-11-10 审计修的真 bug：函数名里的 `since` 在实现里**根本不存在**，
    查询只有 `WHERE pid = ?`，没有任何时间判据。而 `worker_node` 的行
    7 天才清（`lease.py:233`），于是 PID 一旦被重用，上一任的死记录会直接
    冒充"新 worker 起来了"。实测（长跑的 Linux 机默认 `pid_max=32768`，
    几天绕一圈，完全够撞）：插一行三天前的心跳，`_await_heartbeats`
    **0.00 秒**返回 `ok=True`，而那个新进程一秒都没活过。
    谎报成功的代价是整条链都反了——操作者以为在跑、守护那一拍数出
    `online >= desired` 于是**连补都不补**、队列原地不动。

    `since` 是**必填**的关键字参数，不给缺省值：这里"忘了过滤"就是上面那个 bug，
    留一个"不过滤"的默认等于把门开着。判据必须是"本轮 fork 之后"这个
    **绝对基线**，不能是"最近 15 秒"这种滚动窗口——多开时确认第 N 个之前，
    前 N-1 个各可能等满 15 秒，滚动窗口会把**真的活着**的第 N 个误判成死的
    （它的心跳是在本轮基线之后、但已超出 15 秒写的）。

    字符串比较在这里等于时间比较：`heartbeat_at` 与 `since` 都是
    `datetime('now','localtime')` 那个定宽格式 `YYYY-MM-DD HH:MM:SS`。
    """
    try:
        from db import get_conn
        with get_conn() as conn:
            row = conn.execute(
                """SELECT 1 FROM worker_node
                    WHERE pid = ? AND heartbeat_at >= ? LIMIT 1""",
                (pid, since)
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
    """删掉这个 pid 的登记文件。文件已不在也是成功（幂等）。"""
    _remove_pid_file(pid)


def stop_one(pid: int, *, timeout: float = 8.0) -> dict:
    """停一个 worker：先 SIGTERM（它自己会收尾、把当前作业状态写回去），
    超时再 SIGKILL。与 `run.sh:39-68` 的 `stop_one` 同一套顺序与理由。

    worker 装了信号处理（`worker.py` 的 STOP 标志），SIGTERM 会让它跑完当前
    这一轮再退——所以给它 8 秒，不是 1 秒就强杀。
    """
    # **只允许停"我们自己登记过的 worker"**（2026-11-10 复核修）。
    #
    # 原来这里拿到 pid 就 `os.kill`——`stop_one` 是 `POST /worker/process/stop-one`
    # 的实现，那个接口的入参 `pid` 来自请求体。于是**任意 pid 都能被 SIGTERM、
    # 8 秒后 SIGKILL**：实测拿一个与本平台无关的 Python 进程试，它被直接强杀了。
    # 有 `system:config` 的人能借此打掉 API 自己、数据库、别的服务；
    # 叠加一个跨站请求（如果 CSRF 防线有缺口）危害更大。
    #
    # 判据用 `PID_DIR` 下的登记文件，而不是"这个 pid 看起来像 worker"：
    # 登记文件是本平台起进程时才写的，是唯一可信的"这是我们的人"的凭据。
    # 我不去读 /proc 的 cmdline 之类——那要靠猜命令行长什么样。
    registered = {item["pid"] for item in list_workers()}
    if pid not in registered:
        return {"ok": False, "pid": pid,
                "message": f"PID {pid} 不是本机登记的 worker，拒绝操作"}

    _reap(pid)
    if not _pid_alive(pid):
        _cleanup_pid_if(pid)
        return {"ok": True, "pid": pid, "message": f"PID {pid} 已不在，清掉了登记"}

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        _cleanup_pid_if(pid)
        return {"ok": False, "pid": pid, "message": f"给 PID {pid} 发 SIGTERM 失败：{exc}"}

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
            "message": f"PID {pid} {timeout:.0f} 秒内没退，已强杀；"
                       f"它手上的作业会被下一轮 recover_stale 收掉"}


def stop(*, timeout: float = 8.0, pid: int | None = None) -> dict:
    """停 worker。给 `pid` 只停那一个；不给就停**全部**。

    返回 `{ok, stopped, messages}`：`stopped` 是真的停掉的 pid 列表。
    没有 worker 在跑也返回 ok=True（幂等）——界面刷新后重复点、两个人同时点，
    都不该报红。
    """
    if pid is not None:
        result = stop_one(pid, timeout=timeout)
        return {"ok": result["ok"],
                "stopped": [pid] if result["ok"] else [],
                "messages": [result["message"]]}

    workers = [w for w in list_workers() if w["alive"]]
    stale = [w["pid"] for w in list_workers() if w["stale"]]
    stopped: list[int] = []
    messages: list[str] = []
    for item in workers:
        result = stop_one(item["pid"], timeout=timeout)
        if result["ok"]:
            stopped.append(item["pid"])
        messages.append(result["message"])
    for pid_ in stale:
        _cleanup_pid_if(pid_)
        messages.append(f"清掉了残留登记（PID {pid_}）")
    if not messages:
        messages.append("没有 worker 在跑")
    return {"ok": True, "stopped": stopped, "messages": messages}
