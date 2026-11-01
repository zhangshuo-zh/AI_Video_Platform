"""独立生成 worker（W07）。

它是**另一个进程**，不挂在 FastAPI 里：请求只入队并立刻返回 Job ID，
模型跑几十分钟也不会把界面请求堵死（规格 01-4.4 / 02 §7.3）。

W07 只做队列语义与执行壳：
  * 真适配器（FastMetal / mlx-video / mflux / VoxCPM2 / ACE-Step）在这里
    只做白名单校验，然后以 `adapter_not_ready` 失败——执行接线是 W09，
    现在假装成功比不跑更糟；
  * 假适配器 `mock` 只在 `FASTVIDEO_ALLOW_MOCK=1` 时可用，用来把
    "排队 → 领取 → 成功/失败/取消 → 重试 → 崩溃恢复"整条链测穿。

一条本地队列同一时刻只跑一个作业：领取前先看有没有租约未过期的
starting/running，有就不领。租约每 0.2 秒续一次；进程被杀后租约到期，
下次启动由 recover_stale() 收成 failed(worker_lost)，不会永远挂在 running。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
import traceback
import uuid
from datetime import datetime

import adapters
import jobfiles
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


def _params(job) -> dict:
    try:
        value = json.loads(job["params_json"] or "{}")
    except ValueError:
        raise JobFailure("bad_params", "参数快照不是合法 JSON")
    return value if isinstance(value, dict) else {}


def _validate(job) -> tuple[dict, str]:
    """开跑前的白名单校验；返回 (参数, 失败时为空)。"""
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
        return params, "mock"

    adapter = adapters.find(job["capability"], job["execution_mode"], job["provider_code"])
    if adapter is None:
        raise JobFailure("adapter_missing",
                         f"provider「{job['provider_code']}」没有登记适配器")
    errors = adapters.validate_params(adapter, job["model_code"], params)
    if errors:
        raise JobFailure("bad_params", "；".join(errors))
    raise JobFailure(
        "adapter_not_ready",
        f"{adapter.label} 的执行接线在 W09；W07 只验证队列与参数，本次不运行模型")


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


def process(job, wid: str) -> None:
    """跑一条已领取的作业：校验 → 执行 → 落状态。异常全部归一成失败。"""
    logs, _ = jobfiles.ensure_dirs(job["id"])
    log_path = logs / "run.log"
    started = time.monotonic()
    with log_path.open("a", encoding="utf-8") as log:
        _write(log, f"job #{job['id']} {job['capability']}/{job['execution_mode']} "
                    f"{job['provider_code']}/{job['model_code']} worker={wid}")
        jobs.heartbeat(wid, os.getpid(), job["id"], adapters.MANIFEST_VERSION)
        try:
            jobs.set_running(job["id"], wid)
            if jobs.status_of(job["id"]) == "cancel_requested":
                raise Cancelled("领取后立刻收到取消请求")
            params, runner = _validate(job)
            _write(log, f"参数通过白名单：{json.dumps(params, ensure_ascii=False)}")
            if runner == "mock":
                paths, elapsed = _run_mock(job, params, log, wid)
            else:  # 目前 _validate 只返回 mock；真适配器会抛 adapter_not_ready
                raise JobFailure("adapter_not_ready", "没有可执行的反适配器")
            jobs.succeed(job["id"], wid, jobfiles.relative(log_path), paths,
                         elapsed, return_code=0)
            _write(log, "状态：succeeded")
        except Cancelled as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            _write(log, f"状态：cancelled（{exc}）")
            jobs.cancelled(job["id"], wid, str(exc),
                           jobfiles.relative(log_path), elapsed)
        except JobFailure as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            _write(log, f"状态：failed（{exc.code}）{exc.message}")
            jobs.fail(job["id"], wid, exc.code, exc.message,
                      jobfiles.relative(log_path), exc.return_code, elapsed)
        except Exception:
            elapsed = int((time.monotonic() - started) * 1000)
            detail = traceback.format_exc(limit=6)
            _write(log, "状态：failed（internal_error）\n" + detail)
            jobs.fail(job["id"], wid, "internal_error",
                      "worker 内部错误：" + traceback.format_exc(limit=1).strip(),
                      jobfiles.relative(log_path), None, elapsed)
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
