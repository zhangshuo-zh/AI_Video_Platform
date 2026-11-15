"""worker 进程资源与子进程树监控辅助。"""
from __future__ import annotations

import os
import resource
import signal
import subprocess
import sys
import threading
import time

import jobfiles

# `_node_resources()` 的差分基准。初值取进程启动这一刻，所以第一次心跳给的是
# "从启动到现在"的均值，之后每次只算这一段——不然跑完一条大作业后，
# 面板上的占用会拖着整个生命周期的平均值不放，看着像还在忙。
_NODE_SAMPLE = (0.0, time.monotonic())


def _rss_kb(value: int) -> int:
    # ru_maxrss 的单位随系统走：Linux 给 KB，macOS 给字节。不归一就写进库，
    # 界面上会得到"21344 MB"这种荒唐数字（W08 真截图抓到的）。
    return int(value / 1024) if sys.platform == "darwin" else int(value)


def _cpu_totals() -> tuple[float, float]:
    self_usage = resource.getrusage(resource.RUSAGE_SELF)
    child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (self_usage.ru_utime + self_usage.ru_stime,
            child_usage.ru_utime + child_usage.ru_stime)


def _resources(cpu_start: float, elapsed_ms: int,
               child_cpu_start: float | None = None,
               child_rss_kb: int = 0,
               extra: dict | None = None) -> dict:
    """本地作业的机时证据。金额是 0，但"占了多久机器"不能是空白——
    将来要不要把这批活挪到云端，靠的就是这一栏。

    W09 起模型在子进程里，`cpu_ms` 是 worker 自己的、`child_cpu_ms` 才是
    模型那一边；`max_rss_kb` 是 worker 生命周期峰值，`child_max_rss_kb`
    取自 RUSAGE_CHILDREN。两个数字分开写，不拿父进程的峰值冒充模型占用。
    """
    self_usage = resource.getrusage(resource.RUSAGE_SELF)
    child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_now, child_now = _cpu_totals()
    out: dict = {
        "wall_ms": int(elapsed_ms),
        "cpu_ms": int(max(0.0, cpu_now - cpu_start) * 1000),
        "max_rss_kb": _rss_kb(self_usage.ru_maxrss),
        "measure": "getrusage(RUSAGE_SELF)+RUSAGE_CHILDREN",
    }
    if child_cpu_start is not None:
        out["child_cpu_ms"] = int(max(0.0, child_now - child_cpu_start) * 1000)
        out["child_max_rss_kb"] = max(int(child_rss_kb),
                                      _rss_kb(child_usage.ru_maxrss))
    if extra:
        out.update(extra)
    return out


def _node_resources() -> dict:
    """这个 worker 进程现在占了多少机器——写进 `worker_node.resources_json`。

    为什么要有这一栏：阶段 E 允许一个进程领一条视频、另一个领一条合成，
    而"这台机器扛不扛得住再多开一个"只有当场读数能回答。上限是人声明的意愿，
    占用是机器的实况——**面板必须两个都给**，否则出问题时人只会去调上限。

    口径三条，都是踩过坑才定死的：
      * `cpu_pct` 是**两次心跳之间**的均值（不是进程启动至今的均值），
        所以长时间空转后刚跑完一条作业，读数会立刻掉下来，不会拖着一个旧高值；
      * 内存取 `ru_maxrss`（生命周期峰值），系统没有"当前 RSS"的标准库读法，
        拿峰值不撒谎，但字段名写清是 peak；
      * 磁盘读 `JOBS_DIR` 所在卷——产物全落那儿，那里满了合成与生成都会失败。

    只用标准库，不引 psutil（§不引入未经确认的新依赖）。
    """
    global _NODE_SAMPLE
    usage = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_now = (usage.ru_utime + usage.ru_stime + child.ru_utime + child.ru_stime)
    wall_now = time.monotonic()
    prev_cpu, prev_wall = _NODE_SAMPLE
    interval = wall_now - prev_wall
    cpu_pct = round((cpu_now - prev_cpu) / interval * 100, 1) if interval > 0.5 else 0.0
    _NODE_SAMPLE = (cpu_now, wall_now)
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:          # 极老的 Unix/容器不给负载；给空而不是整个心跳失败
        load1 = load5 = load15 = 0.0
    try:
        usage_disk = jobfiles.volume_usage()
        free, total = ((usage_disk.free, usage_disk.total) if usage_disk else (0, 0))
    except OSError:          # 问不到就报 0，不拿一个猜的数冒充读数
        free = total = 0
    return {
        "cores": os.cpu_count() or 0,
        "load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2),
        "cpu_pct": cpu_pct,
        "cpu_ms_since_last": int(max(0.0, cpu_now - prev_cpu) * 1000),
        "rss_peak_mb": round(_rss_kb(usage.ru_maxrss) / 1024, 1),
        "child_rss_peak_mb": round(_rss_kb(child.ru_maxrss) / 1024, 1),
        "disk_free_gb": round(free / 1024 ** 3, 2),
        "disk_total_gb": round(total / 1024 ** 3, 2),
        "measure": "getrusage+self+children/getloadavg/jobfiles.volume_usage",
    }


def _ps_rows() -> list[tuple[int, int, int]]:
    """`ps -eo pid,ppid,rss` 三列。只用标准库＋系统自带的 ps，不引 psutil
    （§不引入未经确认的新依赖）。读不到就回空列表——采样失败不该让一条作业失败。
    """
    try:
        out = subprocess.run(["ps", "-eo", "pid,ppid,rss"],
                             capture_output=True, text=True, timeout=2).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return rows


def descendant_peak(rows: list[tuple[int, int, int]], root_pid: int) -> int:
    """沿 ppid 链把 `root_pid` 的整棵子进程树找出来，返回树里最大的 RSS（KB）。

    为什么要走整棵树而不是只看 `proc.pid`：FastMetal 与 mflux 都是**外面一层 python
    再起算子进程**，只看直接子进程量到的那点壳常驻，真正的模型占用在孙子那一层。
    """
    kids = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid, _rss in rows:
            if ppid in kids and pid not in kids:
                kids.add(pid)
                changed = True
    return max((rss for pid, _ppid, rss in rows if pid in kids), default=0)


class _RssWatch:
    """一条作业一个采样线程：每 `every` 秒记一次这棵子进程树的 RSS 峰值。

    为什么要新加这一栏，`child_max_rss_kb` 不够吗：那一栏来自
    `getrusage(RUSAGE_CHILDREN)`，是**worker 进程生命周期内的最大值**——同一个 worker
    跑完一条 8.75GB 的视频作业之后，接下来任何一条作业的 `child_max_rss_kb` 都会写着
    8.75GB（本机 2026-11-05 实测：一条 1024×1024 的图像作业被记成 8.75GB，
    而它自己那棵树的采样峰值是 3.13GB）。排产要看的是**这一条**占多少，
    所以两个数分开存、语义写清：
      * `child_max_rss_kb`＝worker 见过的最大子进程（生命周期，不是本条）；
      * `child_peak_rss_sampled_kb`＝本条作业运行期间的采样峰值（按秒采，
        **可能低于真实峰值**，但不会把上一条的峰值算到这一条头上）。

    采样本身会 fork 一个 `ps`，它的 CPU 时间计入 `child_cpu_ms`——量级是每条作业
    一秒的零头（本机实测一条 33 秒的作业多算约 30ms），换来的是每条作业自己的内存读数。
    """

    def __init__(self, root_pid: int, every: float = 1.0):
        self.root = root_pid
        self.every = every
        self.peak_kb = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            peak = descendant_peak(_ps_rows(), self.root)
            if peak > self.peak_kb:
                self.peak_kb = peak
            self._stop.wait(self.every)

    def start(self) -> "_RssWatch":
        self._thread.start()
        return self

    def stop(self) -> int:
        self._stop.set()
        self._thread.join(timeout=3)
        return self.peak_kb


def _signal_process_group(proc, sig: int) -> None:
    """给整棵子进程树发信号，而不是只发给最外层那个 python。

    `_run_spec` 用 `start_new_session=True` 起子进程，于是 pgid == pid；
    FastMetal / mflux 都是「外层 python 再起算子进程」，只 terminate 外层
    会留下孙进程继续占资源、甚至把文件写回已经清理过的 outputs/。
    老路径（没有独立会话的 Popen，例如测试里的替身）退回单进程信号：
    绝不能对着 worker 自己所在的进程组发 SIGTERM。
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    if pgid is not None and pgid == proc.pid:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _terminate(proc, grace: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    _signal_process_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
