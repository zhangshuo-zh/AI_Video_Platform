"""独立生成 worker（W07 建壳，W08 补机时，W09 接真子进程）。

它是**另一个进程**，不挂在 FastAPI 里：请求只入队并立刻返回 Job ID，
模型跑几十分钟也不会把界面请求堵死（规格 01-4.4 / 02 §7.3）。

W09 起真适配器不再以 `adapter_not_ready` 失败：
  * FastMetal 1.3B/5B、mlx-video I2V、mflux 由 runners.py 构造 argv，
    这里用 `subprocess.Popen(list, shell=False)` 起子进程；
  * 子进程输出边跑边写进 logs/run.log，取消时先 SIGTERM、5 秒后再 SIGKILL；
  * 跑完验产物魔数与非空，登记相对路径；模型自己的 metrics.json 里挑
    几项进 resources_json；
  * 运行时缺解释器／入口／权重时以 `adapter_unavailable` 失败，并说清缺哪件。
  * 假适配器 `mock` 只在 `FASTVIDEO_ALLOW_MOCK=1` 时可用，用来把
    "排队 → 领取 → 成功/失败/取消 → 重试 → 崩溃恢复"整条链测穿。

一条本地队列同一时刻只跑一个作业：领取前先看有没有租约未过期的
starting/running，有就不领。跑作业时每 0.2 秒续租并看一眼取消，
模型内部再长也不会被恢复逻辑误杀。
"""
from __future__ import annotations

import argparse
import json
import os
import select
import resource
import signal
import socket
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import adapters
import jobfiles
import runners
from config import ALLOW_MOCK, WORKER_LEASE_SECONDS, WORKER_POLL
from repositories.jobs import jobs

STOP = False


class Cancelled(Exception):
    """执行中发现 cancel_requested。"""


class JobFailure(Exception):
    def __init__(self, code: str, message: str, return_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.return_code = return_code


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def worker_id() -> str:
    # 主机名 + pid + 短随机：同一台机器连开两个 worker 也分得清租约是谁的
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _write(log, message: str) -> None:
    log.write(f"[{_now()}] {message}\n")
    log.flush()


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
    out = {
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


def _params(job) -> dict:
    try:
        value = json.loads(job["params_json"] or "{}")
    except ValueError:
        raise JobFailure("bad_params", "参数快照不是合法 JSON")
    return value if isinstance(value, dict) else {}


def _validate(job, outputs: Path) -> tuple[dict, runners.RunSpec | None]:
    """开跑前的白名单校验与命令构造。

    返回 (参数, 命令)；mock 返回 (参数, None)。真适配器缺运行时
    （解释器/入口/权重）时抛 adapter_unavailable，并把缺哪件写进消息——
    这条失败是给人修环境用的，不是"模型跑崩了"。
    """
    params = _params(job)
    if job["provider_code"] == "mock":
        if not ALLOW_MOCK:
            raise JobFailure("adapter_not_ready",
                             "假适配器只在 FASTVIDEO_ALLOW_MOCK=1 时可用")
        errors = adapters.validate_mock_params(params)
        if job["model_code"] != adapters.MOCK_MODEL:
            errors.append(f"mock 的模型档只有 {adapters.MOCK_MODEL}")
        if errors:
            raise JobFailure("bad_params", "；".join(errors))
        return params, None

    adapter = adapters.find(job["capability"], job["execution_mode"], job["provider_code"])
    if adapter is None:
        raise JobFailure("adapter_missing",
                         f"provider「{job['provider_code']}」没有登记适配器")
    errors = adapters.validate_params(adapter, job["model_code"], params)
    if errors:
        raise JobFailure("bad_params", "；".join(errors))
    try:
        spec = runners.build(job, params, outputs)
    except runners.RunnerUnavailable as exc:
        raise JobFailure("adapter_unavailable", exc.message) from exc
    return params, spec


def _run_mock(job, params: dict, log, wid: str) -> tuple[list[str], int]:
    """假适配器：睡一会儿、可失败、写一个文本产物；每 0.2 秒看一眼取消。"""
    sleep_s = float(params.get("sleep", 0.6))
    should_fail = bool(params.get("fail", False))
    text = str(params.get("text", "mock output"))
    started = time.monotonic()
    remaining = sleep_s
    last_beat = 0.0
    _write(log, f"mock 开始：sleep={sleep_s}s fail={should_fail}")
    while remaining > 0:
        if STOP:
            raise JobFailure("worker_stopped", "worker 收到停止信号，作业未完成；可以重试")
        status = jobs.status_of(job["id"])
        if status == "cancel_requested":
            raise Cancelled("收到取消请求")
        step = min(0.2, remaining)
        time.sleep(step)
        remaining -= step
        jobs.renew(job["id"], wid, WORKER_LEASE_SECONDS)
        # 心跳另外按 2 秒一次刷：任务跑久了顶栏不能显示 worker 离线
        if time.monotonic() - last_beat >= 2:
            jobs.heartbeat(wid, os.getpid(), job["id"], adapters.MANIFEST_VERSION)
            last_beat = time.monotonic()
    if should_fail:
        raise JobFailure("mock_failed", "模拟失败：params.fail=true", 1)

    _, outputs = jobfiles.ensure_dirs(job["id"])
    target = outputs / f"result-{job['id']}.txt"
    target.write_text(text + "\n", encoding="utf-8")
    if target.stat().st_size <= 0:
        raise JobFailure("empty_output", "产物是空的")
    elapsed = int((time.monotonic() - started) * 1000)
    _write(log, f"mock 完成：{jobfiles.relative(target)}，{elapsed}ms")
    return [jobfiles.relative(target)], elapsed


def _tail(path: Path, limit: int = 800) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _pump(proc, log, job, wid: str) -> None:
    """把子进程输出边跑边写进日志；每 0.2 秒续租并看一次取消。

    用 os.read + select 而不是 readline：模型输出里有 tqdm 的 \r 进度条，
    readline 会一直等换行，日志看起来像"卡住了"。
    """
    fd = proc.stdout.fileno()
    last_beat = 0.0
    while True:
        if STOP:
            _terminate(proc)
            raise JobFailure("worker_stopped", "worker 收到停止信号，作业未完成；可以重试")
        status = jobs.status_of(job["id"])
        if status == "cancel_requested":
            _terminate(proc)
            raise Cancelled("收到取消请求")
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            log.write(chunk.decode("utf-8", errors="replace"))
            log.flush()
        # 续租与心跳不能挂在"没输出"上：模型连续刷进度条时管道一直 ready，
        # 如果只在 else 分支续租，租约到期就会被恢复逻辑误判成 worker 死了。
        jobs.renew(job["id"], wid, WORKER_LEASE_SECONDS)
        if time.monotonic() - last_beat >= 2:
            jobs.heartbeat(wid, os.getpid(), job["id"], adapters.MANIFEST_VERSION)
            last_beat = time.monotonic()
        if proc.poll() is not None:
            # 进程已退出，但管道里可能还有尾巴；再读一轮由 select 处理
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                break


def _terminate(proc, grace: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _run_spec(job, spec: runners.RunSpec, log, wid: str,
              extra: dict) -> tuple[list[str], int]:
    """起真子进程。没有 shell，没有字符串拼接，argv 就是 argv。"""
    started = time.monotonic()
    child_rss_kb = 0
    _write(log, "命令：" + json.dumps(spec.argv, ensure_ascii=False))
    _write(log, "工作目录：" + str(spec.cwd))
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    # 子进程继承当前环境：HF 缓存、代理、FASTVIDEO_* 都靠它；
    # 平台不额外注入任何密钥，日志里也只写 argv，不写 env。
    try:
        proc = subprocess.Popen(
            spec.argv, cwd=str(spec.cwd), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, shell=False,
        )
    except OSError as exc:
        raise JobFailure("adapter_start_failed",
                         f"起不了子进程：{exc.strerror or exc}") from exc
    try:
        _pump(proc, log, job, wid)
        return_code = proc.wait()
    finally:
        if proc.poll() is None:
            _terminate(proc)
        try:
            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            child_rss_kb = _rss_kb(usage.ru_maxrss)
        except Exception:
            child_rss_kb = 0
        # 管道里剩下的字节已经在 _pump 的 EOF 分支读完；这里再调
        # 阻塞式 .read() 只会在"子进程退出但孙子进程还握着管道"时挂住。
        try:
            proc.stdout.close()
        except Exception:
            pass

    elapsed = int((time.monotonic() - started) * 1000)
    extra["command"] = spec.argv
    extra["runner"] = spec.runner
    extra.update({k: v for k, v in spec.meta.items() if k not in ("command",)})
    extra["adapter_metrics"] = runners.metrics_summary(
        Path(spec.meta.get("metrics_path", ""))) if spec.meta.get("metrics_path") else {}
    # metrics_path 只给父进程读文件用，不进公开的 resources：
    # 界面要的是耗时，不是磁盘目录长什么样。
    extra.pop("metrics_path", None)
    extra["child_max_rss_kb"] = child_rss_kb
    if return_code != 0:
        tail = _tail(Path(jobfiles.job_dir(job["id"])) / "logs" / "run.log")
        raise JobFailure(
            "adapter_failed",
            f"{spec.runner} 退出码 {return_code}；日志尾部：{tail[-400:] or '（空）'}",
            return_code)
    try:
        paths = runners.verify(spec)
    except runners.RunnerUnavailable as exc:
        raise JobFailure("empty_output", exc.message, return_code) from exc
    _write(log, "产物：" + "、".join(jobfiles.relative(p) for p in paths))
    return [jobfiles.relative(p) for p in paths], elapsed


def process(job, wid: str) -> None:
    """跑一条已领取的作业：校验 → 执行 → 落状态。异常全部归一成失败。"""
    logs, outputs = jobfiles.ensure_dirs(job["id"])
    log_path = logs / "run.log"
    started = time.monotonic()
    cpu_start = _cpu_totals()[0]
    child_cpu_start = None
    spec = None
    extra: dict = {}
    with log_path.open("a", encoding="utf-8") as log:
        _write(log, f"job #{job['id']} {job['capability']}/{job['execution_mode']} "
                    f"{job['provider_code']}/{job['model_code']} worker={wid}")
        jobs.heartbeat(wid, os.getpid(), job["id"], adapters.MANIFEST_VERSION)
        try:
            jobs.set_running(job["id"], wid)
            if jobs.status_of(job["id"]) == "cancel_requested":
                raise Cancelled("领取后立刻收到取消请求")
            params, spec = _validate(job, outputs)
            _write(log, f"参数通过白名单：{json.dumps(params, ensure_ascii=False)}")
            if spec is None:
                paths, elapsed = _run_mock(job, params, log, wid)
            else:
                child_cpu_start = _cpu_totals()[1]
                paths, elapsed = _run_spec(job, spec, log, wid, extra)
            jobs.succeed(job["id"], wid, jobfiles.relative(log_path), paths,
                         elapsed, return_code=0,
                         resources=_resources(cpu_start, elapsed,
                                              child_cpu_start, extra=extra))
            _write(log, "状态：succeeded")
        except Cancelled as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            _write(log, f"状态：cancelled（{exc}）")
            jobs.cancelled(job["id"], wid, str(exc),
                           jobfiles.relative(log_path), elapsed,
                           resources=_resources(cpu_start, elapsed,
                                                child_cpu_start, extra=extra))
        except JobFailure as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            _write(log, f"状态：failed（{exc.code}）{exc.message}")
            if spec is not None and exc.code in ("adapter_start_failed",
                                                 "adapter_failed", "empty_output"):
                runners.cleanup_partial(spec)
            jobs.fail(job["id"], wid, exc.code, exc.message,
                      jobfiles.relative(log_path), exc.return_code, elapsed,
                      resources=_resources(cpu_start, elapsed,
                                           child_cpu_start, extra=extra))
        except Exception:
            elapsed = int((time.monotonic() - started) * 1000)
            detail = traceback.format_exc(limit=6)
            _write(log, "状态：failed（internal_error）\n" + detail)
            jobs.fail(job["id"], wid, "internal_error",
                      "worker 内部错误：" + traceback.format_exc(limit=1).strip(),
                      jobfiles.relative(log_path), None, elapsed,
                      resources=_resources(cpu_start, elapsed,
                                           child_cpu_start, extra=extra))
        finally:
            jobs.heartbeat(wid, os.getpid(), None, adapters.MANIFEST_VERSION)


def run_once(wid: str | None = None) -> int | None:
    """领取并跑完一条；没有可领的返回 None。测试直接调这个函数。"""
    wid = wid or worker_id()
    job = jobs.claim_next(wid, WORKER_LEASE_SECONDS)
    if job is None:
        return None
    process(job, wid)
    return job["id"]


def _install_signals() -> None:
    def _stop(signum, frame):
        global STOP
        STOP = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成队列 worker（独立进程）")
    parser.add_argument("--once", action="store_true", help="只跑一条就退出（测试用）")
    parser.add_argument("--poll", type=float, default=WORKER_POLL, help="空闲轮询秒数")
    args = parser.parse_args(argv)

    _install_signals()
    wid = worker_id()
    recovered = jobs.recover_stale()
    if recovered["failed"] or recovered["cancelled"]:
        print(f"启动恢复：failed={recovered['failed']} cancelled={recovered['cancelled']}")
    jobs.heartbeat(wid, os.getpid(), None, adapters.MANIFEST_VERSION)
    print(f"worker {wid} 启动；waiting for jobs ...")
    while not STOP:
        # 每轮都收一次"上一个 worker 没来得及收尾"的作业：只在启动时收的话，
        # 崩溃后立刻重启会看到租约还没过期，于是一条作业会永远挂在 running。
        # 正常跑着的作业每 0.2 秒续租，不会被这里误判。
        jobs.recover_stale()
        did = run_once(wid)
        jobs.heartbeat(wid, os.getpid(), None, adapters.MANIFEST_VERSION)
        if args.once:
            break
        if did is None:
            time.sleep(max(0.1, args.poll))
    print("worker 退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
