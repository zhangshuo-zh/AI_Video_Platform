"""独立生成 worker（W07 建壳，W08 补机时，W09 接真子进程）。

它是**另一个进程**，不挂在 FastAPI 里：请求只入队并立刻返回 Job ID，
模型跑几十分钟也不会把界面请求堵死（规格 01-4.4 / 02 §7.3）。

M1.4 起拆包重构：资源采样 / 子进程树监控迁入 `worker_pkg/rss.py`，参数校验 /
环境白名单 / mock / 通用辅助迁入 `worker_pkg/helpers.py`；本文件作为主执行门面
保留 `_pump`、`_run_spec`、`process`、`run_once`、`main` 并完整再导出全部符号，
确保所有测试与 CLI 入口 100% 兼容。
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import select
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path

import adapters
import agents
import cloud_run
import jobfiles
import pipeline
import runners
import seasons
import validations
from composition import ffmpeg_runner, interpolate
from composition.service import record_success as record_composition
from composition.validators import MediaInvalid
from config import (RESOURCE_CLASSES, WORKER_LEASE_SECONDS,
                    WORKER_MAX_GLOBAL, WORKER_MAX_PER_RESOURCE,
                    WORKER_MAX_RUNTIME_SECONDS, WORKER_POLL)
from log import get_logger
from migrate import migrate
from repositories.jobs import JobOwnershipLost, jobs
from worker_pkg.output import OutputLog, private_output_values
from worker_pkg.helpers import (
    _path_for_record,
    _ARGV_TEXT_PREVIEW,
    _CHILD_ENV_DENY,
    _CHILD_ENV_EXACT,
    _CHILD_ENV_PREFIXES,
    _PROMPT_ARGV_FLAGS,
    CLOUD_UNCERTAIN_CODES,
    Cancelled,
    JobFailure,
    _argv_for_record,
    _child_env,
    _cloud_uncertain,
    _db_guard,
    _internal_error_message,
    _limits_for_round,
    _limits_from_args,
    _mock_media,
    _now,
    _params,
    _run_mock,
    _tail,
    _validate,
    _write,
    worker_id,
)
from worker_pkg.rss import (
    _RssWatch,
    _cpu_totals,
    _node_resources,
    _ps_rows,
    _resources,
    _rss_kb,
    _signal_process_group,
    _terminate,
    descendant_peak,
)

__all__ = [
    "STOP",
    "CLOUD_UNCERTAIN_CODES",
    "Cancelled",
    "JobFailure",
    "_ARGV_TEXT_PREVIEW",
    "_CHILD_ENV_DENY",
    "_CHILD_ENV_EXACT",
    "_CHILD_ENV_PREFIXES",
    "_PROMPT_ARGV_FLAGS",
    "_RssWatch",
    "_argv_for_record",
    "_child_env",
    "_cloud_uncertain",
    "_cpu_totals",
    "_db_guard",
    "_install_signals",
    "_internal_error_message",
    "_limits_for_round",
    "_limits_from_args",
    "_mock_media",
    "_node_resources",
    "_now",
    "_params",
    "_ps_rows",
    "_pump",
    "_resources",
    "_rss_kb",
    "_run_mock",
    "_run_spec",
    "_signal_process_group",
    "_tail",
    "_terminate",
    "_validate",
    "_write",
    "descendant_peak",
    "main",
    "process",
    "run_once",
    "worker_id",
]

logger = get_logger("fastvideo.worker")

STOP = False


def _pump(proc: subprocess.Popen[bytes], log, job, wid: str, deadline: float | None = None,
          private_values: tuple[str, ...] = ()) -> None:
    """把子进程输出边跑边写进日志；每 0.2 秒续租并看一次取消。

    用 os.read + select 而不是 readline：模型输出里有 tqdm 的 \r 进度条，
    readline 会一直等换行，日志看起来像"卡住了"。

    `deadline` 是这条作业的存活上限。**租约只证明 worker 还活着，不证明模型还在前进**；
    没有这一位，子进程挂住时会每 0.2 秒成功续一次租，恢复逻辑永远不碰它。
    """
    stdout = proc.stdout
    if stdout is None:
        raise JobFailure("adapter_start_failed", "执行器没有提供标准输出流")
    fd = stdout.fileno()
    last_beat = 0.0
    db = _db_guard(log)
    output = OutputLog(log, private_output_values(job) + private_values)
    try:
        while True:
            if STOP:
                _terminate(proc)
                raise JobFailure("worker_stopped", "worker 收到停止信号，作业未完成；可以重试")
            if deadline is not None and time.monotonic() > deadline:
                _terminate(proc)
                raise JobFailure("run_timeout",
                                 f"执行超过上限 {WORKER_MAX_RUNTIME_SECONDS} 秒，已终止；"
                                 "先看日志再决定是否重试")
            status = db(jobs.status_of, job["id"], label="查取消状态")
            if status == "cancel_requested":
                _terminate(proc)
                raise Cancelled("收到取消请求")
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                chunk = os.read(fd, 8192)
                if not chunk:
                    break
                output.write(chunk)
            # 续租与心跳不能挂在"没输出"上：模型连续刷进度条时管道一直 ready，
            # 如果只在 else 分支续租，租约到期就会被恢复逻辑误判成 worker 死了。
            db(jobs.renew, job["id"], wid, WORKER_LEASE_SECONDS, label="续租")
            if time.monotonic() - last_beat >= 2:
                db(jobs.heartbeat, wid, os.getpid(), job["id"],
                   adapters.MANIFEST_VERSION, label="心跳")
                last_beat = time.monotonic()
            if proc.poll() is not None:
                # 进程已退出，但管道里可能还有尾巴；再读一轮由 select 处理
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    break
    finally:
        output.finish()


def _run_spec(job, spec: runners.RunSpec, log, wid: str,
              extra: dict) -> tuple[list[str], int, str | None]:
    """起真子进程。没有 shell，没有字符串拼接，argv 就是 argv。

    返回 (产物, 耗时, provider 请求 ID)。云端请求 ID 来自 cloud_run.py 的
    回执文件；本地作业没有这个东西，第三项就是 None。
    """
    started = time.monotonic()
    child_rss_kb = 0
    _write(log, "命令：" + json.dumps(_argv_for_record(spec.argv),
                                      ensure_ascii=False))
    _write(log, "工作目录：" + _path_for_record(spec.cwd))
    env = _child_env(spec)
    try:
        proc = subprocess.Popen(
            spec.argv, cwd=str(spec.cwd), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise JobFailure("adapter_start_failed",
                         f"起不了子进程：{exc.strerror or exc}") from exc
    watch = _RssWatch(proc.pid).start()
    try:
        _pump(proc, log, job, wid, deadline=started + WORKER_MAX_RUNTIME_SECONDS,
              private_values=tuple(env.get(key, "") for key in spec.pass_env))
        return_code = proc.wait()
    except JobFailure as exc:
        if spec.meta.get("result_path"):
            early = runners.provider_receipt(spec)
            if early:
                extra["provider"] = early
                exc.provider_request_id = exc.provider_request_id or early.get("request_id")
        raise
    finally:
        sampled_kb = watch.stop()
        if proc.poll() is None:
            _terminate(proc)
        try:
            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            child_rss_kb = _rss_kb(usage.ru_maxrss)
        except (OSError, ValueError):
            child_rss_kb = 0
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except OSError:
            pass

    elapsed = int((time.monotonic() - started) * 1000)
    extra["command"] = _argv_for_record(spec.argv)
    extra["runner"] = spec.runner
    extra.update({k: v for k, v in spec.meta.items()
                  if k not in ("command", "request_path", "result_path")})
    extra["adapter_metrics"] = runners.metrics_summary(
        Path(spec.meta.get("metrics_path", ""))) if spec.meta.get("metrics_path") else {}
    extra.pop("metrics_path", None)
    extra["child_max_rss_kb"] = child_rss_kb
    extra["child_peak_rss_sampled_kb"] = sampled_kb

    receipt = runners.provider_receipt(spec) if spec.meta.get("result_path") else {}
    request_id = (receipt.get("request_id") or None) if receipt else None
    if receipt:
        extra["provider"] = receipt
        receipt_log = {
            "request_id": receipt.get("request_id"),
            "http_status": receipt.get("http_status"),
            "usage": receipt.get("usage"),
            "ok": receipt.get("ok"),
            "error": receipt.get("error"),
        }
        if "checkpoint" in receipt:
            receipt_log["checkpoint"] = receipt["checkpoint"]
        if "checkpoints" in receipt:
            receipt_log["checkpoints"] = receipt["checkpoints"]
        _write(log, "provider 回执：" + json.dumps(receipt_log, ensure_ascii=False))

    if return_code != 0:
        err = (receipt.get("error") or {}) if receipt else {}
        if err:
            raw = str(err.get("code") or "error")
            if raw.startswith("provider_"):
                raw = raw[len("provider_"):]
            code = "provider_" + raw
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
        raise JobFailure("empty_output", exc.message, return_code,
                         provider_request_id=request_id) from exc
    if spec.runner == "ffmpeg":
        plan = spec.meta.get("interp")
        if plan:
            try:
                metrics = interpolate.interpolate(
                    spec.expected[0], spec.cwd, int(job["id"]),
                    float(plan["to_fps"]), log=log)
            except (interpolate.InterpolateUnavailable,
                    runners.RunnerUnavailable) as exc:
                raise JobFailure("interpolate_failed",
                                 getattr(exc, "message", None) or str(exc),
                                 return_code) from exc
            if metrics.get("interp_s") is not None:
                extra["interpolate"] = metrics
            _write(log, "补帧：%s → %s（%s 帧 → %s 帧）" % (
                metrics.get("source_fps"), metrics.get("target_fps"),
                metrics.get("source_frames"), metrics.get("target_frames")))
        try:
            ffmpeg_runner.post_check(spec)
        except MediaInvalid as exc:
            raise JobFailure("invalid_output", exc.message, return_code) from exc
    _write(log, "产物：" + "、".join(jobfiles.relative(p) for p in paths))
    return [jobfiles.relative(p) for p in paths], elapsed, request_id


def process(job, wid: str, limits: dict | None = None) -> None:
    """跑一条已领取的作业：校验 → 执行 → 落状态。异常全部归一成失败或待核对。"""
    logs, outputs = jobfiles.ensure_dirs(job["id"])
    log_path = logs / "run.log"
    started = time.monotonic()
    cpu_start = _cpu_totals()[0]
    child_cpu_start = None
    spec = None
    extra: dict = {}
    with log_path.open("a", encoding="utf-8") as log:
        db = _db_guard(log)
        _write(log, f"job #{job['id']} {job['capability']}/{job['execution_mode']} "
                    f"{job['provider_code']}/{job['model_code']} worker={wid}")
        db(jobs.heartbeat, wid, os.getpid(), job["id"], adapters.MANIFEST_VERSION,
           limits=limits, resources=_node_resources(), label="起始心跳")
        try:
            jobs.set_running(job["id"], wid)
            if jobs.status_of(job["id"]) == "cancel_requested":
                raise Cancelled("领取后立刻收到取消请求")
            params, spec = _validate(job, outputs)
            _write(log, f"参数通过白名单：{json.dumps(params, ensure_ascii=False)}")
            request_id = None
            if spec is None:
                paths, elapsed = _run_mock(job, params, log, wid, db)
            else:
                child_cpu_start = _cpu_totals()[1]
                paths, elapsed, request_id = _run_spec(job, spec, log, wid, extra)
            jobs.succeed(job["id"], wid, jobfiles.relative(log_path), paths,
                         elapsed, return_code=0,
                         resources=_resources(cpu_start, elapsed,
                                              child_cpu_start, extra=extra),
                         provider_request_id=request_id)
            _write(log, "状态：succeeded")
            if job["provider_code"] == "ffmpeg":
                try:
                    record_composition(job)
                    _write(log, "成片记录：最终产物与 sha256 已回填")
                except Exception as exc:
                    # 作业已成功：附加记录失败不能改写终态，单独记录以供修复。
                    logger.warning("composition_record_failed job=%s error=%s", job["id"], type(exc).__name__)
                    _write(log, f"成片回填未写入：{type(exc).__name__}")
            if job["execution_mode"] != "mock" and job["provider_code"] != "mock":
                try:
                    validations.from_job(job["id"], "real_call", job["created_by"],
                                         f"作业 #{job['id']} 跑通，产物校验通过。")
                    _write(log, "验证记录：真实调用已登记")
                except Exception as exc:
                    # 成功产物不因验证登记故障丢失；仅记录异常类型，不自动补签。
                    logger.warning("validation_record_failed job=%s error=%s", job["id"], type(exc).__name__)
                    _write(log, f"验证记录未写入：{type(exc).__name__}")
        except JobOwnershipLost as exc:
            _write(log, f"状态：本次结果作废，不写库（{exc}）")
            _write(log, "产物仍在 outputs/；请按库里的终态处理，不要自动重试")
        except Cancelled as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            receipt = runners.provider_receipt(spec) if (
                spec is not None and spec.meta.get("result_path")) else {}
            if receipt:
                extra["provider"] = receipt
            cancel_request_id = receipt.get("request_id")
            supplier_cancel: dict = {}
            if cancel_request_id and spec is not None:
                _key_envs = [e for e in (spec.pass_env or ())
                             if e.endswith(("_API_KEY", "_KEY", "_TOKEN"))]
                supplier_cancel = cloud_run.cancel_task(
                    provider=str(job["provider_code"] or ""),
                    request_id=str(cancel_request_id),
                    key_env=(_key_envs[0] if _key_envs else ""))
                extra["supplier_cancel"] = supplier_cancel
                _write(log, "供应商取消：" + json.dumps(
                    {k: supplier_cancel.get(k) for k in
                     ("cancelled", "reason", "http_status")},
                    ensure_ascii=False))
            supplier_cancelled = bool(supplier_cancel.get("cancelled"))
            if (not supplier_cancelled) and _cloud_uncertain(
                    job, "cancelled_unconfirmed", cancel_request_id,
                    submitted=spec is not None):
                reason = str(supplier_cancel.get("reason") or "")
                if reason == "running":
                    _write(log, "状态：needs_attention（取消，但供应商任务已在运行，取消不了）")
                    detail = ("供应商那边任务已经开跑，接口只允许取消排队中的任务；"
                              "这一笔很可能已经计费，请核对账单后再决定是否重试或补录。")
                elif reason:
                    _write(log, f"状态：needs_attention（取消，供应商取消没成功：{reason}）")
                    detail = ("供应商取消请求没成功（" + reason + "）；"
                              "任务结果未知，这一笔可能已经计费，请核对账单。")
                else:
                    _write(log, "状态：needs_attention（取消，但供应商任务可能已建立）")
                    detail = ("供应商任务可能已建立，请先核对账单再决定是否重试"
                              "（没有拿到可取消的任务号，无法代你撤销）。")
                jobs.needs_attention(
                    job["id"], wid, "cancelled_unconfirmed",
                    f"{exc}；{detail}",
                    "用户在云端调用已建立后取消",
                    jobfiles.relative(log_path), None, elapsed,
                    resources=_resources(cpu_start, elapsed, child_cpu_start, extra=extra),
                    provider_request_id=cancel_request_id)
            else:
                if supplier_cancelled:
                    _write(log, "状态：cancelled（已向供应商撤销，不会再计费）")
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
            # 单作业顶层：任意适配器故障都必须落终态，避免租约卡住或拖垮后续作业。
            elapsed = int((time.monotonic() - started) * 1000)
            detail = " → ".join(f"{frame.name}:{frame.lineno}"
                                  for frame in traceback.extract_tb(exc.__traceback__, limit=6))
            _write(log, f"状态：failed（internal_error）{type(exc).__name__}\n" + detail)
            logger.error("worker_internal_error job=%s error=%s", job["id"], type(exc).__name__)
            jobs.fail(job["id"], wid, "internal_error",
                      _internal_error_message(exc),
                      jobfiles.relative(log_path), None, elapsed,
                      resources=_resources(cpu_start, elapsed,
                                           child_cpu_start, extra=extra))
        finally:
            db(jobs.heartbeat, wid, os.getpid(), None, adapters.MANIFEST_VERSION,
               resources=_node_resources(), label="收尾心跳")


def run_once(wid: str | None = None, limits: dict | None = None) -> int | None:
    """领取并跑完一条；没有可领的返回 None。测试直接调这个函数。"""
    wid = wid or worker_id()
    job = jobs.claim_next(wid, WORKER_LEASE_SECONDS, limits=limits)
    if job is None:
        return None
    process(job, wid, limits=limits)
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
    parser.add_argument("--max-global", type=int, default=None,
                        help=f"本机同时在跑的作业上限（默认 {WORKER_MAX_GLOBAL}）")
    for name in RESOURCE_CLASSES:
        parser.add_argument(
            f"--max-{name}", type=int, default=None, dest=f"max_{name}",
            help=f"{name} 类同时在跑的上限"
                 f"（默认 {WORKER_MAX_PER_RESOURCE.get(name, 1)}）")
    args = parser.parse_args(argv)

    _install_signals()
    wid = worker_id()
    cli_limits, cli_explicit = _limits_from_args(args)
    migrate(verbose=False)
    pipeline.ensure_builtin_templates()
    limits = _limits_for_round(cli_limits, cli_explicit)
    recovered = jobs.recover_stale()
    if recovered["failed"] or recovered["cancelled"]:
        logger.info("worker_recover_stale failed=%s cancelled=%s",
                    recovered["failed"], recovered["cancelled"])
    jobs.heartbeat(wid, os.getpid(), None, adapters.MANIFEST_VERSION,
                   limits=limits, resources=_node_resources())
    cap = ", ".join(f"{k}={v}" for k, v in sorted(limits["per"].items())) or "默认"
    logger.info("worker_started wid=%s global=%s per=%s", wid, limits["global"], cap)
    # 每个调度子域独立兜底：某一域的编程错误不能停止其他域或心跳。
    # 以下捕获统一只记录类型；失败轮次保留现有状态，下一轮重试。
    while not STOP:
        try:
            jobs.recover_stale()
        except Exception as exc:
            logger.warning("worker_recover_stale_failed err=%s",
                           type(exc).__name__)
        try:
            seasons.tick()
        except Exception as exc:
            logger.warning("worker_seasons_tick_failed err=%s",
                           type(exc).__name__)
        try:
            pipeline.tick()
        except Exception as exc:
            logger.warning("worker_pipeline_tick_failed err=%s",
                           type(exc).__name__)
        try:
            agents.tick()
        except Exception as exc:
            logger.warning("worker_agents_tick_failed err=%s",
                           type(exc).__name__)
        try:
            limits = _limits_for_round(cli_limits, cli_explicit)
        except Exception as exc:
            logger.warning("worker_limits_read_failed err=%s",
                           type(exc).__name__)
        did = None
        try:
            did = run_once(wid, limits=limits)
        except Exception as exc:
            logger.warning("[作业] 这一轮跑砸了 worker_run_once_failed err=%s",
                           type(exc).__name__)
        try:
            jobs.heartbeat(wid, os.getpid(), None, adapters.MANIFEST_VERSION,
                           limits=limits, resources=_node_resources())
        except Exception as exc:
            logger.warning("worker_heartbeat_failed err=%s",
                           type(exc).__name__)
        if args.once:
            break
        if did is None:
            time.sleep(max(0.1, args.poll))
    try:
        jobs.mark_offline(worker_id=wid)
    except (sqlite3.Error, OSError) as exc:
        logger.debug("worker_mark_offline_failed err=%s", type(exc).__name__)
    logger.info("worker_stopped wid=%s", wid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
