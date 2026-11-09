"""worker 守护：API 进程里的一个看门狗线程，按需把掉的 worker 拉回来（0049）。

**为什么放在 API 进程里而不是 launchd/systemd**：网页上要能开关它。系统级守护
装一次就固定了，没法从界面「今天先别守」；而且跨平台要维护两套配置文件。
代价要说清楚：**API 挂了，守护也停**——这是这一层的已知边界，不是疏忽。

三条纪律，与这个模块想解决的问题绑在一起：

1. **只在"确实该守"时守**。守护是 opt-in（配置存在且 `enabled`），默认关。
   让人在不知情的情况下多了个会自动起进程的东西，比没有守护更糟。

2. **守的是"数量"，不是"某个 pid"**。人可能开了 3 个 worker，其中一个挂了，
   该做的是补回 1 个，而不是把剩下 2 个也重启。所以目标是"在线数 ≥ 期望数"。

3. **每次动作留痕**。补起来、放弃补、因为停了作业而不敢补——三种都要记，
   否则"守护到底干了什么"只能靠猜。记录进 `notifications`（0049 的表），
   与第 3 个需求（通知用户）共用同一条链路。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from config import BASE_DIR

# 守护配置存哪：`data/run/daemon.json`。
# 为什么不进库：它是**这台机器**的运行态配置（开不开守护、几个 worker），
# 与 `worker_limit` 那种"全局设置"不是一个层级；而且守护线程本身要在库可用
# 之前就能读到它（迁移期间也要工作），文件比表少一层依赖。
DAEMON_CONFIG = BASE_DIR / "data" / "run" / "daemon.json"

# 检查间隔。为什么是 15 秒：worker 心跳 2 秒一次，`WORKER_ONLINE_SECONDS`
# 是 20 秒判离线。取 15 秒意味着"判离线之前不会重复检查"，又不会让人等太久。
# 这个数字与 `config.WORKER_ONLINE_SECONDS` 绑在一起，改了那边要复核这里。
CHECK_INTERVAL = 15.0

# 连续失败多少次后停下来不再硬试。为什么要有：环境坏了（缺权重、缺解释器）
# 时守护会每 15 秒起一个又崩一个，日志瞬间被刷满，而且掩盖了真正的原因。
# 停下来并把原因记进通知，比无限重试诚实。
MAX_FAILURES = 3

_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_STATE = {"started_at": None, "last_check": None, "last_error": "",
          "failures": 0, "recovered": 0, "actions": []}


def load_config() -> dict:
    """读守护配置。文件不存在／坏了都返回"关闭"，绝不默认开启。"""
    try:
        raw = DAEMON_CONFIG.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {"enabled": False, "desired": 1}
    if not isinstance(data, dict):
        return {"enabled": False, "desired": 1}
    return {
        "enabled": bool(data.get("enabled")),
        # 期望在线几个。0 是合法值（"守着但不许有"），所以不能 `or 1`。
        "desired": max(0, int(data.get("desired", 1) or 0)),
    }


def save_config(*, enabled: bool, desired: int) -> dict:
    DAEMON_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    cfg = {"enabled": bool(enabled), "desired": max(0, min(int(desired), 8))}
    DAEMON_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return cfg


def _record(action: str, detail: str) -> None:
    """记一条守护动作。进内存环形缓冲（给接口看）**并**尝试写通知表。

    写通知失败不能影响守护本身——一句日志写不进去不该让自动拉起停摆。
    """
    _STATE["actions"] = ([{"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "action": action, "detail": detail}]
                         + _STATE["actions"])[:30]
    try:
        import notifications
        notifications.emit(
            kind="worker_daemon",
            level="warn" if action != "recovered" else "info",
            title=f"守护：{action}",
            body=detail,
            # 守护动的是整台机器的进程，不是某个项目，所以收件人是
            # "有 system:config 的人"——他们才是该知道的人。
            audience="config_admins",
        )
    except Exception:
        pass


def _notify_state(online: int) -> None:
    """按当前在线数发/清掉线通知。三态：

    * `online > 0` → 清掉旧的掉线通知（worker 回来了）
    * `online == 0` 且队列有活 → 通知受影响的项目（需求原话那句）
    * `online == 0` 且队列空 → 什么都不做（没人被影响，不打扰）

    读队列失败时宁可**不发**：发一条"可能有人受影响"的模糊提醒，
    比漏发更消耗信任——人点进去发现没事，下次就不看了。
    """
    try:
        import worker_notice
        if online > 0:
            cleared = worker_notice.resolve_offline()
            if cleared:
                _STATE["last_check"] = _STATE["last_check"] or ""
            return
        queued = sum(worker_notice.queued_by_project().values())
        sent = worker_notice.notify_offline(online=online, queued_total=queued)
        if sent:
            _record("通知掉线", f"队列有 {queued} 条在等，已通知 {len(sent)} 个项目")
    except Exception as exc:
        _STATE["last_error"] = f"通知失败：{type(exc).__name__}: {exc}"


def _tick() -> None:
    """一轮检查。**只有一处会起进程**，其余分支都只记录。"""
    import worker_supervisor as sup

    cfg = load_config()
    _STATE["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if not cfg["enabled"]:
        return

    state = sup.current()
    online = state["count"]
    desired = cfg["desired"]

    # 通知受影响的项目：**无论守护开没开都要做**。守护只管"要不要自动拉起"，
    # 而"队列停了要告诉人"是另一件事——守护关着的时候更需要这条提醒。
    _notify_state(online)

    if online >= desired:
        _STATE["failures"] = 0      # 恢复正常就清零，否则一次故障会永久卡住
        return

    if _STATE["failures"] >= MAX_FAILURES:
        return                       # 已经放弃，等人处理（见 check() 的 exposed）

    need = desired - online
    result = sup.start(count=need)
    if result["ok"]:
        _STATE["failures"] = 0
        _STATE["recovered"] += len(result.get("pids") or [])
        _record("已补起", f"在线 {online} → {desired}，"
                          f"新起 PID {'、'.join(map(str, result['pids']))}")
    else:
        _STATE["failures"] += 1
        _STATE["last_error"] = result["message"]
        # 第 MAX_FAILURES 次失败才报"放弃"，前几次只记 warn——
        # 偶发失败（迁移正好在跑）不该立刻升级成"需要人处理"。
        if _STATE["failures"] >= MAX_FAILURES:
            _record("放弃自动拉起",
                    f"连续 {MAX_FAILURES} 次没起来，停止重试以免刷爆日志。"
                    f"最后一次：{result['message']}")
        else:
            _record("补起失败", result["message"])


def _loop() -> None:
    while not _STOP.is_set():
        try:
            _tick()
        except Exception as exc:            # 守护线程绝不能因为一句异常就死
            _STATE["last_error"] = f"{type(exc).__name__}: {exc}"
        _STOP.wait(CHECK_INTERVAL)


def start_thread() -> bool:
    """起守护线程（幂等：已在跑就返回 False）。API 启动时调一次。"""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return False
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="worker-daemon", daemon=True)
    _THREAD.start()
    _STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return True


def stop_thread() -> None:
    _STOP.set()


def check() -> dict:
    """给界面看的守护状态。**不改任何东西**——改是 `_tick` 的事。

    `exposed` 是这里最该被读懂的一句：守护停了、或已放弃重试时，
    "有守护"这件事就不成立了。界面必须据此把话说清楚，
    不能因为配置文件里 enabled=true 就一直显示"守护中"。
    """
    import worker_supervisor as sup
    cfg = load_config()
    state = sup.current()
    running = _THREAD is not None and _THREAD.is_alive()
    return {
        "enabled": cfg["enabled"],
        "desired": cfg["desired"],
        "online": state["count"],
        "pids": state["pids"],
        "thread_alive": running,
        "failures": _STATE["failures"],
        "recovered": _STATE["recovered"],
        "last_check": _STATE["last_check"],
        "last_error": _STATE["last_error"],
        "started_at": _STATE["started_at"],
        "actions": _STATE["actions"],
        # 关键判定：真的在守吗
        "exposed": (not cfg["enabled"]) or (not running)
                   or (_STATE["failures"] >= MAX_FAILURES),
        "check_interval_seconds": CHECK_INTERVAL,
    }
