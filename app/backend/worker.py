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

自动化漫剧产线的调度节拍也住在这个进程里（每轮循环调一次
`pipeline.tick()`）。理由：本进程已经是常驻循环、已经在每轮做
`recover_stale()`，也是唯一会写 Job 状态的地方；调度器放这儿，
"派活"与"收活"就是同一个进程里的先后两步，不用再造回调通道。
API 那边的 `POST /advance` 调的是同一个 `advance_run()`，两边都幂等，
谁先跑都不会把同一个节点派两次。
"""
from __future__ import annotations

import argparse
import json
import os
import select
import resource
import signal
import socket
import sqlite3
import subprocess
import threading
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import adapters
import jobfiles
import agents
from migrate import migrate
import pipeline
import seasons
from composition import ffmpeg_runner, interpolate
from composition.service import record_success as record_composition
from composition.validators import MediaInvalid
import runners
import validations
from config import (ALLOW_MOCK, FFMPEG_BIN, RESOURCE_CLASSES, WORKER_LEASE_SECONDS,
                    WORKER_MAX_GLOBAL, WORKER_MAX_PER_RESOURCE,
                    WORKER_MAX_RUNTIME_SECONDS, WORKER_POLL)
from paths import describe as describe_path
from repositories.jobs import JobOwnershipLost, jobs

STOP = False

# `_node_resources()` 的差分基准。初值取进程启动这一刻，所以第一次心跳给的是
# "从启动到现在"的均值，之后每次只算这一段——不然跑完一条大作业后，
# 面板上的占用会拖着整个生命周期的平均值不放，看着像还在忙。
_NODE_SAMPLE = (0.0, time.monotonic())

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


# 允许传给模型子进程的环境变量：**白名单**，不是黑名单。
#
# 为什么白名单：黑名单永远漏。这里是第三方脚本，我们不知道它会读哪个变量，
# 只能反过来明确"它确实需要哪些"。加新变量必须走这里，这本身就是一道review。
_CHILD_ENV_PREFIXES = (
    "FASTVIDEO_",      # 平台自己的开关：适配器路径、并发上限、缓存目录…
    "HF_",             # huggingface 缓存/镜像/token 之外的都是路径类
    "TRANSFORMERS_", "TORCH_", "CUDA_", "PYTORCH_", "MLX_", "OMP_", "MKL_",
    "XDG_", "TMPDIR", "TEMP", "TMP",
)
_CHILD_ENV_EXACT = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME", "SHELL",
    "PYTHONUNBUFFERED", "PYTHONPATH", "PYTHONHASHSEED",
    "TZ", "TERM", "PWD",
    # 代理：模型权重下载几乎一定走它们，漏了就是"内网根本拉不下来"
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)
# 这些名字虽然前缀匹配，但**是密钥**，必须单独挡掉。
# `HF_TOKEN` 与 `HF_*` 同前缀，不单独处理就会被放行。
_CHILD_ENV_DENY = (
    "HF_TOKEN", "HF_HUB_TOKEN", "HUGGING_FACE_HUB_TOKEN",
    "FASTVIDEO_ADMIN_PASSWORD", "FASTVIDEO_SECRET_KEY",
    "FASTVIDEO_CLOUD_KEY",
)


def _child_env(spec) -> dict:
    """给模型子进程准备环境变量：白名单 + 必要的放行。

    第三方脚本需要 PATH/HOME/HF 缓存/代理这些**非密钥**信息才能跑；
    它们不需要（也不该拿到）平台自己的口令与供应商密钥。
    """
    env = {}
    for name, value in os.environ.items():
        if name in _CHILD_ENV_DENY:
            continue
        if name in _CHILD_ENV_EXACT or name.startswith(_CHILD_ENV_PREFIXES):
            env[name] = value
    env.setdefault("PYTHONUNBUFFERED", "1")
    # 云端 runner 需要**一个**指定名字的密钥变量（`key_env` 是请求里给的变量名）。
    # 这是唯一按需放行的通道：只给它点名的那一个，其余一律不给。
    for extra in (getattr(spec, "pass_env", None) or ()):
        if extra in _CHILD_ENV_DENY:
            continue
        if extra in os.environ:
            env[extra] = os.environ[extra]
    return env


def _write(log, message: str) -> None:
    log.write(f"[{_now()}] {message}\n")
    log.flush()


# 这些 argv 键后面跟的那一格是**用户创作正文**（提示词/负向提示词/文本）。
# 它们既进 `run.log`，也随 `extra["command"]` 进 `resources_json` 落库，
# 于是"日志里躺着完整提示词"——排查时没用（要复现看的是参数与种子），
# 转发日志给外部时却是白送出去的创作内容。
_PROMPT_ARGV_FLAGS = frozenset({"--prompt", "--negative-prompt", "-p", "--text",
                                "--caption", "--negative"})
# 截断长度：留够"这是在讲什么"的判断量，又不至于把整段正文抄走。
_ARGV_TEXT_PREVIEW = 40


def _argv_for_record(argv: list[str]) -> list[str]:
    """把要落盘/进日志的 argv 里那一格正文换成摘要。

    **只动记录，不动真跑的 argv**：`_run_spec` 用原始 `spec.argv` 拉起子进程，
    这里返回的是副本，两个用途必须分开——写进日志的东西和交给内核的东西
    不该是同一个对象，否则以后有人图省事在这里原地改就会改坏执行。

    形状：`--prompt 一只猫…(共 812 字)`。字数照实给，因为排查时"提示词多长"
    常常就是线索（比如撞到 77 token 上限）；内容本身不给。
    """
    out = list(argv)
    for i, item in enumerate(out[:-1]):
        if item not in _PROMPT_ARGV_FLAGS:
            continue
        text = out[i + 1]
        # 已经短到没必要截的就原样留：把 "1girl, solo" 换成摘要反而更难读。
        if len(text) <= _ARGV_TEXT_PREVIEW:
            continue
        out[i + 1] = f"{text[:_ARGV_TEXT_PREVIEW]}…(共 {len(text)} 字)"
    return out


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

    # 成片合成不是模型适配器：provider=ffmpeg 的作业由 composition 包构造命令。
    # 它同样走这一条 Job 链路（领取、租约、取消、日志、产物），只是 argv 不同。
    if job["provider_code"] == "ffmpeg":
        try:
            return params, ffmpeg_runner.build(job, params, outputs)
        except ffmpeg_runner.CompositionJobInvalid as exc:
            raise JobFailure("bad_params", str(exc)) from exc
        except interpolate.InterpolateUnavailable as exc:
            # 补帧这道开着而运行时不齐：这是环境问题，不是参数不成立，
            # 也不做"那就复制帧"的静默退让——点名缺什么，人决定补还是关。
            raise JobFailure("interpolate_unavailable", str(exc)) from exc
        except MediaInvalid as exc:
            raise JobFailure("bad_params", exc.message) from exc

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


def _run_mock(job, params: dict, log, wid: str,
              db=lambda fn, *a, **k: fn(*a, **k)) -> tuple[list[str], int]:
    """假适配器：睡一会儿、可失败、写一个文本产物；每 0.2 秒看一眼取消。

    `db` 是 `_db_guard(log)` 那个包装，由 `process()` 传进来。
    2026-11-10 整库审计修的真 bug：这里三处库调用原来是裸的
    （`jobs.status_of/renew/heartbeat`），而同一个文件里 `_pump` 对
    **同样这三个调用**全都走 `db(...)`。后果是 mock 作业撞上
    `pipeline.advance_run` 的写锁（`get_conn` 只有 5 秒 timeout）时，
    `OperationalError: database is locked` 直接冒泡出 `_run_mock` →
    `process()` 的 `except Exception` 把一条产物一件没坏的作业写死成
    `failed / internal_error`；`renew` 那处更糟——正常跑动的作业会被
    判死。这正是 `_db_guard` 文档里承诺的反面：「锁冲突绝不能变成
    『杀死正在跑的模型』」。同一个 worker 上真适配器的作业不受影响，
    只有 mock 分支这一条路径行为不一致，所以平时完全看不出来。

    缺省值是恒等包装，为了让单元测试可以不构造 log 直接调它。
    """
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
        # 锁冲突时这一拍读到 None：当作"没有取消请求"继续跑，下一拍再问。
        # 与 `_pump` 同一套语义，不能因为没读到就把作业判死。
        status = db(jobs.status_of, job["id"], label="查取消状态")
        if status == "cancel_requested":
            raise Cancelled("收到取消请求")
        step = min(0.2, remaining)
        time.sleep(step)
        remaining -= step
        db(jobs.renew, job["id"], wid, WORKER_LEASE_SECONDS, label="续租")
        # 心跳另外按 2 秒一次刷：任务跑久了顶栏不能显示 worker 离线
        if time.monotonic() - last_beat >= 2:
            db(jobs.heartbeat, wid, os.getpid(), job["id"],
               adapters.MANIFEST_VERSION, label="心跳")
            last_beat = time.monotonic()
    if should_fail:
        raise JobFailure("mock_failed", "模拟失败：params.fail=true", 1)

    _, outputs = jobfiles.ensure_dirs(job["id"])
    media = str(params.get("media", "text"))
    if media == "text":
        target = outputs / f"result-{job['id']}.txt"
        target.write_text(text + "\n", encoding="utf-8")
    else:
        # 用本机 ffmpeg 现造一件真媒体（纯色，1 秒／单帧），
        # 让审核签字之后的时间线与合成能真的取到片、ffprobe 也真读得出视频流。
        target = _mock_media(job, params, outputs, media)
    if target.stat().st_size <= 0:
        raise JobFailure("empty_output", "产物是空的")
    elapsed = int((time.monotonic() - started) * 1000)
    _write(log, f"mock 完成：{jobfiles.relative(target)}，{elapsed}ms")
    return [jobfiles.relative(target)], elapsed


def _mock_media(job, params: dict, outputs: Path, media: str) -> Path:
    """假适配器的媒体分支：lavfi 纯色源 → 真 mp4 或真 png。

    为什么走 ffmpeg 而不是手写字节：合成链要求产物能被 ffprobe 解析出时长与流，
    自造一个"看起来像 mp4"的头只会把失败推到更远的地方。这里用与
    `tests/test_composition.py` 同一条命令口径，成败都当场见分晓。
    缺 ffmpeg 时以 `mock_media_unavailable` 失败并说清缺什么——
    这条消息是给人修环境用的，不是"模型跑崩了"。
    """
    color = str(params.get("color", "red"))
    seconds = float(params.get("seconds", 1.0))
    target = outputs / f"result-{job['id']}.{'mp4' if media == 'mp4' else 'png'}"
    if media == "mp4":
        argv = [FFMPEG_BIN, "-hide_banner", "-y", "-f", "lavfi",
                "-i", f"color=c={color}:s=640x360:d={seconds}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "24", str(target)]
    else:
        argv = [FFMPEG_BIN, "-hide_banner", "-y", "-f", "lavfi",
                "-i", f"color=c={color}:s=640x360",
                "-frames:v", "1", str(target)]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise JobFailure("mock_media_unavailable",
                         f"mock 的媒体产物需要可执行的 {FFMPEG_BIN}"
                         f"（{type(exc).__name__}）；或用 media=text 只测队列") from exc
    if proc.returncode != 0 or not target.exists():
        err = (proc.stderr or b"").decode("utf-8", "ignore").strip()[-300:]
        raise JobFailure("mock_media_failed",
                         f"ffmpeg 造媒体失败（退出码 {proc.returncode}）：{err}")
    return target


def _tail(path: Path, limit: int = 800) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _db_guard(log):
    """心跳/续租/查状态的库访问兜底：锁冲突绝不能变成「杀死正在跑的模型」。

    `pipeline.advance_run` 的长事务会持有 SQLite 写锁，而 `db.get_conn` 的
    timeout 只有 5 秒。过去 `_pump` 里这三个调用是裸的：一次
    `sqlite3.OperationalError: database is locked` 就冒泡出 `_pump`，
    `_run_spec` 的 finally 无条件 `_terminate(proc)`，于是一条本来好好的
    作业被一次锁等待判成 internal_error。这里把锁冲突降级为「这一拍没读到」，
    作业继续跑；锁一释放，下一拍自动续上租约。
    """
    state = {"misses": 0}

    def call(fn, *args, default=None, label="", **kwargs):
        try:
            value = fn(*args, **kwargs)
            state["misses"] = 0
            return value
        except sqlite3.OperationalError as exc:
            state["misses"] += 1
            misses = state["misses"]
            if misses == 1 or misses % 25 == 0:
                _write(log, f"[库锁] {label or getattr(fn, '__name__', 'db')} "
                            f"这一拍没拿到库（连续 {misses} 拍）：{exc}；"
                            "作业继续跑，不因此终止子进程")
            return default

    return call


def _pump(proc, log, job, wid: str, deadline: float | None = None) -> None:
    """把子进程输出边跑边写进日志；每 0.2 秒续租并看一次取消。

    用 os.read + select 而不是 readline：模型输出里有 tqdm 的 \r 进度条，
    readline 会一直等换行，日志看起来像"卡住了"。

    `deadline` 是这条作业的存活上限。**租约只证明 worker 还活着，不证明模型还在前进**；
    没有这一位，子进程挂住时会每 0.2 秒成功续一次租，恢复逻辑永远不碰它。
    """
    fd = proc.stdout.fileno()
    last_beat = 0.0
    db = _db_guard(log)
    while True:
        if STOP:
            _terminate(proc)
            raise JobFailure("worker_stopped", "worker 收到停止信号，作业未完成；可以重试")
        if deadline is not None and time.monotonic() > deadline:
            _terminate(proc)
            raise JobFailure("run_timeout",
                             f"执行超过上限 {WORKER_MAX_RUNTIME_SECONDS} 秒，已终止；"
                             f"先看日志再决定是否重试")
        status = db(jobs.status_of, job["id"], label="查取消状态")
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


def _ps_rows() -> list[tuple[int, int, int]]:
    """`ps -eo pid,ppid,rss` 三列。只用标准库＋系统自带的 ps，不引 psutil
    （§不引入未经确认的新依赖）。读不到就回空列表——采样失败不该让一条作业失败。
    """
    try:
        out = subprocess.run(["ps", "-eo", "pid,ppid,rss"],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
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


def _run_spec(job, spec: runners.RunSpec, log, wid: str,
              extra: dict) -> tuple[list[str], int, str | None]:
    """起真子进程。没有 shell，没有字符串拼接，argv 就是 argv。

    返回 (产物, 耗时, provider 请求 ID)。云端请求 ID 来自 cloud_run.py 的
    回执文件；本地作业没有这个东西，第三项就是 None。
    """
    started = time.monotonic()
    child_rss_kb = 0
    # 落日志前把提示词换成摘要：日志会被贴进工单、发给外部排查，
    # 而"当时提示词写了什么"不是排查要的信息，"多长、什么模型、什么种子"才是。
    _write(log, "命令：" + json.dumps(_argv_for_record(spec.argv),
                                      ensure_ascii=False))
    _write(log, "工作目录：" + str(spec.cwd))
    env = _child_env(spec)
    # 子进程**只拿它该拿的**（2026-11-09 外部审查第 6 条）。
    #
    # 原来这里是 `env = dict(os.environ)`，注释还写着"平台不额外注入任何密钥"——
    # 事实正相反：`FASTVIDEO_ADMIN_PASSWORD`、`DASHSCOPE_API_KEY`、`HF_TOKEN`
    # 全都会被这批第三方模型脚本继承。它们大多是 `huggingface_hub` 拉下来的
    # 社区代码，任何一条 `print(os.environ)` 或异常回传都能把密钥带走，
    # 而 `cloud_run.py` 那边花了很大力气定"密钥只按变量名读、值只放进程环境"
    # 的边界——子进程全量继承把这一条整个抵消掉。日志里不写 env 救不了这个
    # 漏洞：泄漏点在子进程自己的行为，不在我们的日志。
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
        sampled_kb = watch.stop()
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
    # 同一份摘要进 resources：它跟着 Job 落库并回给界面，
    # 与日志同一口径——两处各写各的迟早一处漏、一处不漏。
    extra["command"] = _argv_for_record(spec.argv)
    extra["runner"] = spec.runner
    extra.update({k: v for k, v in spec.meta.items()
                  if k not in ("command", "request_path", "result_path")})
    extra["adapter_metrics"] = runners.metrics_summary(
        Path(spec.meta.get("metrics_path", ""))) if spec.meta.get("metrics_path") else {}
    # metrics_path 只给父进程读文件用，不进公开的 resources：
    # 界面要的是耗时，不是磁盘目录长什么样。
    extra.pop("metrics_path", None)
    extra["child_max_rss_kb"] = child_rss_kb
    # 本条作业自己的采样峰值（语义见 `_RssWatch` 的注释）。
    extra["child_peak_rss_sampled_kb"] = sampled_kb

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
            # `cloud_run.py` 已经把 5xx 这类错误命名成 `provider_unavailable`／
            # `provider_error` 了，这里再无条件拼一次 `provider_` 就会变成
            # `provider_provider_unavailable`——而 `CLOUD_UNCERTAIN_CODES` 里存的是
            # 单前缀的那批名字。后果不是难看，是判错：供应商返回 500 时任务很可能
            # 已经建立、甚至已经计费，这种作业必须落 `needs_attention`（待核对），
            # 双前缀让它躲过这条规则，于是账上是"待对账"、状态上却是"失败，可重试"。
            # 六场景真跑（场景四·云端状态未知）把这一寸不一致照了出来。
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
        # 云端"退出码 0 却没有有效产物"是最需要人看的一种：
        # 供应商侧任务可能已经计过费。把请求 ID 一起带出去。
        raise JobFailure("empty_output", exc.message, return_code,
                         provider_request_id=request_id) from exc
    if spec.runner == "ffmpeg":
        # 补帧跑在 post_check 之前：验收要看的是最终那个文件的真实帧率，
        # 不是补帧之前那一版的。没排到这一道（meta 里 interp 为空）时整段跳过，
        # 行为与加这道之前逐字一致。
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
        # 退出码 0 只说明进程没报错，不说明文件能播：再用 ffprobe 复核
        # 分辨率、编码、时长、帧率与音轨；不通过就按失败收尾，不写 succeeded。
        try:
            ffmpeg_runner.post_check(spec)
        except MediaInvalid as exc:
            raise JobFailure("invalid_output", exc.message, return_code) from exc
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
            # 合成作业成功后把最终文件、sha256 与媒体信息抄回成片行。
            # 抄写失败不能把已经成功的作业改回失败——整段吞掉只写日志，
            # 读取成片时会再补一次（composition/service.view 里那条懒回填）。
            if job["provider_code"] == "ffmpeg":
                try:
                    record_composition(job)
                    _write(log, "成片记录：最终产物与 sha256 已回填")
                except Exception as exc:
                    _write(log, f"成片回填未写入：{exc}")
            # 成功的真实作业顺手成为该槽位的"真实调用"凭据。mock 会被 from_job 拒掉，
            # 记录失败也绝不能把已经成功的作业改回失败——所以整段吞掉只写日志。
            if job["execution_mode"] != "mock" and job["provider_code"] != "mock":
                try:
                    validations.from_job(job["id"], "real_call", job["created_by"],
                                         f"作业 #{job['id']} 跑通，产物校验通过。")
                    _write(log, "验证记录：真实调用已登记")
                except Exception as exc:
                    _write(log, f"验证记录未写入：{exc}")
        except JobOwnershipLost as exc:
            # 终态已经被租约恢复或另一个收尾动作拿走：库里那一行就是真相，
            # 这里绝不能再写一次把它盖掉，也不该再演成 internal_error。
            _write(log, f"状态：本次结果作废，不写库（{exc}）")
            _write(log, "产物仍在 outputs/；请按库里的终态处理，不要自动重试")
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
                _write(log, "状态：needs_attention（取消，但供应商任务可能已建立）")
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
            db(jobs.heartbeat, wid, os.getpid(), None, adapters.MANIFEST_VERSION,
               resources=_node_resources(), label="收尾心跳")


def run_once(wid: str | None = None, limits: dict | None = None) -> int | None:
    """领取并跑完一条；没有可领的返回 None。测试直接调这个函数。

    `limits` 只影响"现在能不能领"，不影响作业本身；一个进程仍是一次一条。
    """
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


def _limits_from_args(args) -> tuple[dict, dict]:
    """CLI 覆盖项 → (领取用的上限, 哪些项是命令行显式给的)。

    默认仍是全局 1／每类 1：并发是 operator 明确抬上限并多开进程才发生的事，
    不会因为升级代码就悄悄在 16GB 机器上同时跑两条视频。

    **`explicit` 单独返回，不塞进 limits 里**（2026-11-09 实测踩到）：
    `_limits_for_round` 的结果会被 `jobs.heartbeat(limits=...)` 用
    `json.dumps` 写进 `worker_node.limits_json`，而 `explicit["per"]` 是个
    set——"Object of type set is not JSON serializable"，心跳当场炸。
    它是"这次怎么定的"过程信息，不该混进"上限是多少"这个结果里。
    """
    per = {}
    explicit_per: set[str] = set()
    for name in RESOURCE_CLASSES:
        value = getattr(args, f"max_{name}", None)
        if value is not None:
            per[name] = max(0, int(value))
            explicit_per.add(name)
    global_max = (args.max_global if args.max_global is not None
                  else WORKER_MAX_GLOBAL)
    limits = {"global": max(0, int(global_max)), "per": per}
    explicit = {"global": args.max_global is not None, "per": explicit_per}
    return limits, explicit


def _limits_for_round(cli_limits: dict, explicit: dict | None = None) -> dict:
    """这一轮领取该用哪套上限：命令行显式给的 > 网页设的 > 进程默认。

    为什么要每轮读一次库而不是启动时读一次：网页上改完期望"下一轮就生效"，
    而不是"等你重启 worker"。`claim_next` 本来就每轮查库（它要数活跃槽位），
    多读一行单例配置不构成新的读放大；改完立刻生效也省掉"改了没反应"的排查。

    **命令行优先**是硬规矩：`./run.sh worker --max-global 2` 是 operator 当下
    对着这台机器下的指令，不该被网页上一周前设的旧值悄悄盖掉。反过来若命令行
    没给，网页设过就用网页的——这正是"能在网页上改"这件事本身。

    返回值**只含 global／per 两个键**，能直接 `json.dumps`——心跳要把它写进
    `worker_node.limits_json`。`explicit` 是入参不是出参（see `_limits_from_args`）。
    """
    try:
        from repositories import worker_limit
        saved = worker_limit.limits_or_none()
    except Exception:
        # 表还没建（老库没跑 0047）或库暂时读不了：退回命令行/默认值。
        # 这里绝不能抛——一句配置读不到不该让队列停摆。
        saved = None
    if not saved:
        return {"global": cli_limits.get("global"),
                "per": dict(cli_limits.get("per") or {})}

    explicit = explicit or {}
    out_global = cli_limits.get("global")
    if not explicit.get("global") and saved.get("global") is not None:
        out_global = max(0, int(saved["global"]))
    per = dict(cli_limits.get("per") or {})
    explicit_per = explicit.get("per") or set()
    for name, value in (saved.get("per") or {}).items():
        if name in RESOURCE_CLASSES and name not in explicit_per:
            per[name] = max(0, int(value))
    return {"global": out_global, "per": per}


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
    # **库要先补到最新**（2026-11-08，P1）。
    # 原来只有 `main.create_app()` 里调 `migrate()`，于是"只跑 worker 不跑 API"
    # （`./run.sh worker`，或批处理机）在全新库上会直接崩：下面那句
    # `ensure_builtin_templates()` 要写 `pipeline_template` 表，表还不存在。
    # 而且这个异常发生在 while 循环之前、没有任何 try 兜着，进程起不来。
    # 重复执行是安全的（`migrate()` 自己按 schema_version 幂等）。
    migrate(verbose=False)
    # 模板表补齐：worker 可能比 API 进程先起来（比如只跑 worker 做批处理），
    # 那时候产线运行照样要能推进，所以两边都调一次；重复调用是空操作。
    pipeline.ensure_builtin_templates()
    # 并发上限放在 migrate 之后读：0047 的 `worker_limit` 表要等迁移建出来，
    # 在它之前读会撞"no such table"（虽然 `_limits_for_round` 兜住了不抛，
    # 但那样启动横幅会显示成默认值，与人设的值不符——横幅是第一眼证据）。
    limits = _limits_for_round(cli_limits, cli_explicit)
    recovered = jobs.recover_stale()
    if recovered["failed"] or recovered["cancelled"]:
        print(f"启动恢复：failed={recovered['failed']} cancelled={recovered['cancelled']}")
    jobs.heartbeat(wid, os.getpid(), None, adapters.MANIFEST_VERSION,
                   limits=limits, resources=_node_resources())
    cap = ", ".join(f"{k}={v}" for k, v in sorted(limits["per"].items())) or "默认"
    print(f"worker {wid} 启动；并发 全局={limits['global']} {cap}；"
          f"waiting for jobs ...")
    while not STOP:
        # 每轮都收一次"上一个 worker 没来得及收尾"的作业：只在启动时收的话，
        # 崩溃后立刻重启会看到租约还没过期，于是一条作业会永远挂在 running。
        # 正常跑着的作业每 0.2 秒续租，不会被这里误判。
        try:
            jobs.recover_stale()
        except Exception as exc:
            print(f"[队列] 收 stale 失败，本轮跳过：{type(exc).__name__}: {exc}")
        # 产线节拍：先把该派的节点派成 Job，再往下走 run_once 领取。
        # 顺序反过来也能工作（下一轮补上），只是白等一个轮询周期。
        # 这里出异常不能让 worker 整个退出——一条产线的定义有问题，
        # 不该把队列里其它正常作业一起拖死。
        # 整季排产：先把到点的下一集立成单集运行，再由 pipeline.tick 推进它。
        # 顺序反过来只是白等一个轮询周期；两个 try 分开，一季的配置有问题
        # 不该把已存在的单集运行也停掉。
        try:
            seasons.tick()
        except Exception as exc:
            print(f"[整季] 排产失败，本轮跳过：{type(exc).__name__}: {exc}")
        try:
            pipeline.tick()
        except Exception as exc:
            print(f"[产线] 推进失败，本轮跳过：{type(exc).__name__}: {exc}")
        # 智能体运行的推进节拍也住在这里（同裁定 D3 的理由：本进程是唯一会写
        # Job 状态的地方，"派活"与"收活"是同一进程里的先后两步）。
        # 单独一个 try：一条运行的计划有问题不该把产线的推进也停掉。
        try:
            agents.tick()
        except Exception as exc:
            print(f"[智能体] 推进失败，本轮跳过：{type(exc).__name__}: {exc}")
        # 下面两步原来是没有 try 的，而这条链上恰恰最容易撞库锁：
        # `advance_run` 在 BEGIN IMMEDIATE 事务里要跑 ffprobe 验成片（最长 30 秒），
        # 另一条连接等 5 秒（db.py 的 timeout）就抛 "database is locked"。
        # 一句锁冲突不该把整个队列关掉——`run.sh worker` 是 `exec` 起来的独立进程，
        # 没有 supervise 重启，一旦退出就没人领作业了，界面上只剩"worker 离线"，
        # 而那一条正在跑的作业会被下一轮的 recover_stale 收成 failed（云端可能已计费）。
        # 兜住的后果和"让它崩"是一样的（作业靠租约回收、下一轮重来），
        # 区别只是队列不停。
        # 每轮重新算一次上限：网页上改完，下一轮就该按新值领活。
        # 放在 try 里是因为它要读库——读不到就用上一轮的值，不该让队列停。
        try:
            limits = _limits_for_round(cli_limits, cli_explicit)
        except Exception as exc:
            print(f"[并发] 这一轮读配置失败，沿用上一轮：{type(exc).__name__}: {exc}")
        did = None
        try:
            did = run_once(wid, limits=limits)
        except Exception as exc:
            print(f"[作业] 这一轮跑砸了，下一轮继续：{type(exc).__name__}: {exc}")
        try:
            jobs.heartbeat(wid, os.getpid(), None, adapters.MANIFEST_VERSION,
                           limits=limits, resources=_node_resources())
        except Exception as exc:
            print(f"[心跳] 这一轮没写上：{type(exc).__name__}: {exc}")
        if args.once:
            break
        if did is None:
            time.sleep(max(0.1, args.poll))
    print("worker 退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
