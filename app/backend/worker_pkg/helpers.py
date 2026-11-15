"""worker 参数校验、环境白名单、mock 执行与辅助函数。"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

import adapters
import jobfiles
from composition import ffmpeg_runner, interpolate
from composition.validators import MediaInvalid
import runners
from config import (ALLOW_MOCK, FFMPEG_BIN, RESOURCE_CLASSES,
                    WORKER_LEASE_SECONDS, WORKER_MAX_GLOBAL)
from paths import describe as describe_path

# 云端调用结果未知的那批错误码：供应商任务可能已经建立、甚至已经计费。
# 这些不能落 failed——failed 的默认建议是"重试"，而对一笔可能已扣钱的调用，
# 最坏的建议就是替人按下重试。
CLOUD_UNCERTAIN_CODES = {
    "adapter_failed", "empty_output", "run_timeout", "worker_stopped",
    "provider_timeout", "provider_network", "provider_empty_response",
    "provider_empty_output", "provider_error", "provider_unavailable",
    # 受理后断线或响应 JSON 损坏也可能没有 request_id。
    "provider_cloud_run_error",
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
            continue
        # 环境里没有 → 从库里的密文**现解**，这一次调用现用（2026-11-11）。
        #
        # 为什么在这里解而不是让 cloud_run.py 自己查库：cloud_run 是独立子进程，
        # 模块头写死"不碰数据库、不认识 Job"，那条边界是它可测试、可重放的前提
        # （只认一份请求 JSON 快照）。破它比"多解一次密"的代价大得多。
        # 也不缓存到 os.environ：那是**跨作业残留**，会让 A 作业用的密钥
        # 悄悄留在进程里给 B 作业看见。
        try:
            import secret_store
            value, _source = secret_store.resolve(extra)
            if value:
                env[extra] = value
        except Exception:
            # 解不开（KEK 换了/库被改）：**这里不加变量，也不打印原因**。
            # 为什么不打日志：本函数没有 log 句柄，而为了打一行字去改它的
            # 签名会把 12 个调用点全牵动；更关键的是**解密失败的证据在别处
            # 更准确**——`runners._cloud` 的 `key_present` 已能认出"库里有"，
            # 于是作业会带着"缺密钥"的信息失败，`secret_store.resolve`
            # 在 API 侧的探测会报 `InvalidTag`。两处都能查，不必在这里留第三份。
            # 静默的只是"本函数没补这个变量"，不是"把故障吞掉"。
            continue
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
    import worker as _w

    sleep_s = float(params.get("sleep", 0.6))
    should_fail = bool(params.get("fail", False))
    text = str(params.get("text", "mock output"))
    started = time.monotonic()
    remaining = sleep_s
    last_beat = 0.0
    _write(log, f"mock 开始：sleep={sleep_s}s fail={should_fail}")
    while remaining > 0:
        if _w.STOP:
            raise JobFailure("worker_stopped", "worker 收到停止信号，作业未完成；可以重试")
        # 锁冲突时这一拍读到 None：当作"没有取消请求"继续跑，下一拍再问。
        # 与 `_pump` 同一套语义，不能因为没读到就把作业判死。
        status = db(_w.jobs.status_of, job["id"], label="查取消状态")
        if status == "cancel_requested":
            raise Cancelled("收到取消请求")
        step = min(0.2, remaining)
        time.sleep(step)
        remaining -= step
        db(_w.jobs.renew, job["id"], wid, WORKER_LEASE_SECONDS, label="续租")
        # 心跳另外按 2 秒一次刷：任务跑久了顶栏不能显示 worker 离线
        if time.monotonic() - last_beat >= 2:
            db(_w.jobs.heartbeat, wid, os.getpid(), job["id"],
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


def _cloud_uncertain(job, code: str, request_id: str | None,
                     *, submitted: bool = False) -> bool:
    """无请求 ID 也可能已受理；执行前取消仍可直接收尾。"""
    evidence = request_id or submitted or code != "cancelled_unconfirmed"
    return bool(job["execution_mode"] == "cloud" and evidence
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
    except (sqlite3.Error, ValueError, RuntimeError):
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
