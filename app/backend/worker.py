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
import validations
from config import (ALLOW_MOCK, WORKER_LEASE_SECONDS, WORKER_MAX_RUNTIME_SECONDS,
                    WORKER_POLL)
from paths import describe as describe_path
from repositories.jobs import jobs

STOP = False

# 云端调用结果未知的那批错误码：供应商任务可能已经建立、甚至已经计费。
# 这些不能落 failed——failed 的默认建议是"重试"，而对一笔可能已扣钱的调用，
# 最坏的建议就是替人按下重试。
CLOUD_UNCERTAIN_CODES = {
    "adapter_failed", "empty_output", "run_timeout", "worker_stopped",
    "provider_timeout", "provider_network", "provider_empty_response",
    "provider_empty_output", "provider_error", "provider_unavailable",
    "cancelled_unconfirmed",
}


class Cancelled(Exception):
    """执行中发现 cancel_requested。"""


class JobFailure(Exception):
    def __init__(self, code: str, message: str, return_code: int | None = None,
                 provider_request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.return_code = return_code
        # 云端失败也要把请求 ID 带出去：账可以对不上，线索不能丢
        self.provider_request_id = provider_request_id


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


def _pump(proc, log, job, wid: str, deadline: float | None = None) -> None:
    """把子进程输出边跑边写进日志；每 0.2 秒续租并看一次取消。

    用 os.read + select 而不是 readline：模型输出里有 tqdm 的 \r 进度条，
    readline 会一直等换行，日志看起来像"卡住了"。

    `deadline` 是这条作业的存活上限。**租约只证明 worker 还活着，不证明模型还在前进**；
    没有这一位，子进程挂住时会每 0.2 秒成功续一次租，恢复逻辑永远不碰它。
    """
    fd = proc.stdout.fileno()
    last_beat = 0.0
    while True:
        if STOP:
            _terminate(proc)
            raise JobFailure("worker_stopped", "worker 收到停止信号，作业未完成；可以重试")
        if deadline is not None and time.monotonic() > deadline:
            _terminate(proc)
            raise JobFailure("run_timeout",
                             f"执行超过上限 {WORKER_MAX_RUNTIME_SECONDS} 秒，已终止；"
                             f"先看日志再决定是否重试")
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
              extra: dict) -> tuple[list[str], int, str | None]:
    """起真子进程。没有 shell，没有字符串拼接，argv 就是 argv。

    返回 (产物, 耗时, provider 请求 ID)。云端请求 ID 来自 cloud_run.py 的
    回执文件；本地作业没有这个东西，第三项就是 None。
    """
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
        _pump(proc, log, job, wid, deadline=started + WORKER_MAX_RUNTIME_SECONDS)
        return_code = proc.wait()
    except JobFailure as exc:
        # 超时/停止时云端可能已经把任务建起来了。`cloud_run.py` 在创建成功后
        # 会立刻落一份部分回执，这里把它读出来挂到异常上——
        # 账可以暂时对不上，追这笔钱的线索不能丢。
        if spec.meta.get("result_path"):
            early = runners.provider_receipt(spec)
            if early:
                extra["provider"] = early
                exc.provider_request_id = exc.provider_request_id or early.get("request_id")
        raise
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
    extra.update({k: v for k, v in spec.meta.items()
                  if k not in ("command", "request_path", "result_path")})
    extra["adapter_metrics"] = runners.metrics_summary(
        Path(spec.meta.get("metrics_path", ""))) if spec.meta.get("metrics_path") else {}
    # metrics_path 只给父进程读文件用，不进公开的 resources：
    # 界面要的是耗时，不是磁盘目录长什么样。
    extra.pop("metrics_path", None)
    extra["child_max_rss_kb"] = child_rss_kb

    # 云端回执：请求 ID 与实际用量。成功与失败都要读——失败时供应商也可能
    # 已经建过任务、计过费，这两个字段正是事后对账的凭据。
    receipt = runners.provider_receipt(spec) if spec.meta.get("result_path") else {}
    request_id = (receipt.get("request_id") or None) if receipt else None
    if receipt:
        extra["provider"] = receipt
        _write(log, "provider 回执：" + json.dumps({
            "request_id": receipt.get("request_id"),
            "http_status": receipt.get("http_status"),
            "usage": receipt.get("usage"),
            "ok": receipt.get("ok"),
            "error": receipt.get("error"),
        }, ensure_ascii=False))

    if return_code != 0:
        err = (receipt.get("error") or {}) if receipt else {}
        if err:
            code = "provider_" + str(err.get("code") or "error")
            raise JobFailure(code, str(err.get("message") or "供应商返回错误"),
                             return_code, provider_request_id=request_id)
        tail = _tail(Path(jobfiles.job_dir(job["id"])) / "logs" / "run.log")
        raise JobFailure(
            "adapter_failed",
            f"{spec.runner} 退出码 {return_code}；日志尾部：{tail[-400:] or '（空）'}",
            return_code, provider_request_id=request_id)
    try:
        paths = runners.verify(spec)
    except runners.RunnerUnavailable as exc:
        # 云端"退出码 0 却没有有效产物"是最需要人看的一种：
        # 供应商侧任务可能已经计过费。把请求 ID 一起带出去。
        raise JobFailure("empty_output", exc.message, return_code,
                         provider_request_id=request_id) from exc
    _write(log, "产物：" + "、".join(jobfiles.relative(p) for p in paths))
    return [jobfiles.relative(p) for p in paths], elapsed, request_id


def _cloud_uncertain(job, code: str, request_id: str | None) -> bool:
    """云端调用结果未知：供应商任务可能已建立，不能按普通失败建议重试。"""
    return bool(job["execution_mode"] == "cloud" and request_id
                and code in CLOUD_UNCERTAIN_CODES)


def _internal_error_message(exc: BaseException) -> str:
    """worker 自己崩了时对外那一句：异常类型 + 一行消息，不带堆栈。

    为什么不再把 `traceback.format_exc()` 存进 `error_message`：这一列会随
    每一条作业列表接口一起回传（队列页一次拉几十行），也会进素材导出与成本
    CSV。多行堆栈铺在列表里既撑大响应、又让界面变成一坨，而它该待的地方
    本来就是日志文件——`/api/jobs/{id}/logs` 与作业详情是同一道 `job:read`，
    少写这一份并没有少给谁信息。
    异常消息本身可能含着绝对路径（`FileNotFoundError` 会把路径写进 `str(exc)`），
    所以再过一道 `describe_path`：部署机的家目录与安装路径不该跟着列表出门。
    """
    first = " ".join(str(exc).strip().split("\n")[:1])[:200]
    brief = describe_path(first)
    return (f"worker 内部错误：{type(exc).__name__}: {brief}" if brief
            else f"worker 内部错误：{type(exc).__name__}")


def process(job, wid: str) -> None:
    """跑一条已领取的作业：校验 → 执行 → 落状态。异常全部归一成失败或待核对。"""
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
            request_id = None
            if spec is None:
                paths, elapsed = _run_mock(job, params, log, wid)
            else:
                child_cpu_start = _cpu_totals()[1]
                paths, elapsed, request_id = _run_spec(job, spec, log, wid, extra)
            jobs.succeed(job["id"], wid, jobfiles.relative(log_path), paths,
                         elapsed, return_code=0,
                         resources=_resources(cpu_start, elapsed,
                                              child_cpu_start, extra=extra),
                         provider_request_id=request_id)
            _write(log, "状态：succeeded")
            # 成功的真实作业顺手成为该槽位的"真实调用"凭据。mock 会被 from_job 拒掉，
            # 记录失败也绝不能把已经成功的作业改回失败——所以整段吞掉只写日志。
            if job["execution_mode"] != "mock" and job["provider_code"] != "mock":
                try:
                    validations.from_job(job["id"], "real_call", job["created_by"],
                                         f"作业 #{job['id']} 跑通，产物校验通过。")
                    _write(log, "验证记录：真实调用已登记")
                except Exception as exc:
                    _write(log, f"验证记录未写入：{exc}")
        except Cancelled as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            # 云端任务可能在子进程被杀之前已经建好；回执文件若有一份，
            # 把它带进 resources 与 Job 列，方便去供应商后台追这次调用。
            receipt = runners.provider_receipt(spec) if (
                spec is not None and spec.meta.get("result_path")) else {}
            if receipt:
                extra["provider"] = receipt
            cancel_request_id = receipt.get("request_id")
            if _cloud_uncertain(job, "cancelled_unconfirmed", cancel_request_id):
                _write(log, f"状态：needs_attention（取消，但供应商任务可能已建立）")
                jobs.needs_attention(
                    job["id"], wid, "cancelled_unconfirmed",
                    f"{exc}；供应商任务可能已建立，请先核对账单再决定是否重试。",
                    "用户在云端调用已建立后取消",
                    jobfiles.relative(log_path), None, elapsed,
                    resources=_resources(cpu_start, elapsed, child_cpu_start, extra=extra),
                    provider_request_id=cancel_request_id)
            else:
                _write(log, f"状态：cancelled（{exc}）")
                jobs.cancelled(job["id"], wid, str(exc),
                               jobfiles.relative(log_path), elapsed,
                               resources=_resources(cpu_start, elapsed,
                                                    child_cpu_start, extra=extra),
                               provider_request_id=cancel_request_id)
        except JobFailure as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            if spec is not None and exc.code in ("adapter_start_failed",
                                                 "adapter_failed", "empty_output"):
                runners.cleanup_partial(spec)
            if _cloud_uncertain(job, exc.code, exc.provider_request_id):
                _write(log, f"状态：needs_attention（{exc.code}）{exc.message}")
                jobs.needs_attention(
                    job["id"], wid, exc.code, exc.message,
                    "云端调用结果未知：供应商可能已建立任务或已计费",
                    jobfiles.relative(log_path), exc.return_code, elapsed,
                    resources=_resources(cpu_start, elapsed,
                                         child_cpu_start, extra=extra),
                    provider_request_id=exc.provider_request_id)
            else:
                _write(log, f"状态：failed（{exc.code}）{exc.message}")
                jobs.fail(job["id"], wid, exc.code, exc.message,
                          jobfiles.relative(log_path), exc.return_code, elapsed,
                          resources=_resources(cpu_start, elapsed,
                                               child_cpu_start, extra=extra),
                          provider_request_id=exc.provider_request_id)
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            detail = traceback.format_exc(limit=6)
            _write(log, "状态：failed（internal_error）\n" + detail)
            jobs.fail(job["id"], wid, "internal_error",
                      _internal_error_message(exc),
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
