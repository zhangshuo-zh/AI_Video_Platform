"""worker 的韧性：锁冲突、进程组终止、派发中断。

这三条钉的是本轮改过的**行为语义**，不是实现细节：

* 心跳/续租撞上 `database is locked` 时，绝不能把正在跑的模型杀掉；
* 超时/取消要收掉整棵子进程树（FastMetal 与 mflux 都是"外层 python 再起算子"）；
* 两阶段派发在事务提交后、建 Job 前中断时，节点要明确落成可重试的失败，
  而不是永远停在 `running`。
"""
import io
import os
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

import worker
from db import get_conn


def test_锁冲突只跳过这一拍不抛异常():
    """`_db_guard` 的唯一职责：把 OperationalError 降级成"这一拍没读到"。

    过去 `_pump` 里三个调用是裸的，一次锁等待冒泡出去就会触发
    `_run_spec` 的 finally，把正常作业判成 internal_error。
    """
    log = io.StringIO()
    guard = worker._db_guard(log)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    assert guard(locked, 1, 2) is None          # 不抛，返回默认值
    assert "库锁" in log.getvalue()             # 但要在 run.log 里留痕
    assert guard(lambda: "ok") == "ok"          # 锁放开后恢复正常


def test_超时终止连孙进程一起收掉(tmp_path):
    """只 SIGTERM 直接子进程会留下孙子继续占显存，还可能回写已清理的 outputs/。"""
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        for _ in range(80):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        assert pidfile.read_text().strip(), "孙进程没起来，用例本身失效"
        grandchild = int(pidfile.read_text().strip())

        worker._terminate(proc, grace=1.0)
        assert proc.poll() is not None

        # 进程组信号是异步的：给内核一点时间收尸
        for _ in range(30):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(grandchild, signal.SIGKILL)
            pytest.fail("孙进程还活着：_terminate 只收掉了直接子进程")
    finally:
        if proc.poll() is None:
            proc.kill()


def test_心跳不每次都清死节点(fresh_project, clean_queue):
    """第 11 条：`worker_node` 的清理按小时节流，但**清理能力本身还在**。

    心跳 2 秒一次，原来的实现每拍都跑一次
    `DELETE FROM worker_node WHERE heartbeat_at < ...-7 days`——
    而那张表里正常情况下就那么一两行、7 天前的永远是 0 行，
    等于每两秒拿一次写锁空转（实测 heartbeat 2.556 ms/次）。

    这里断言三件事，缺一条都不算修好：
    ① 第一次心跳就把超期行清掉（不能"节流"成"永不清理"）；
    ② 紧接着再心跳不清（节流窗口内不重复做无用功）；
    ③ 窗口过去之后再心跳，照旧清。
    """
    from repositories.jobs import jobs as job_repo

    def _dead(worker_id):
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO worker_node (worker_id, pid, host, heartbeat_at, version)
                   VALUES (?, 1, 'h', datetime('now','localtime','-8 days'), 'v1')""",
                (worker_id,))

    def _alive(worker_id) -> bool:
        with get_conn() as conn:
            return conn.execute("SELECT 1 FROM worker_node WHERE worker_id = ?",
                                (worker_id,)).fetchone() is not None

    # ① 首次心跳（本进程还没清过）应当清掉超期行。
    #
    # **`None` 而不是 `0.0`**（2026-11-11 修的真 bug）：`time.monotonic()` 是
    # 开机以来秒数，机器刚重启时可能远小于 3600；那时 `now - 0.0 >= 3600`
    # 为假，于是"第一次心跳必须清"这条永远不成立——用例会在刚重启的机器上红，
    # 而更糟的是**产品行为也错**：开机第一个小时内一次都不清死节点。
    # 哨兵改成 None 之后，两种情况都正确。
    job_repo._last_prune = None
    _dead("drill-dead-1")
    job_repo.heartbeat("w-alive", 4001, None, "v1")
    assert not _alive("drill-dead-1"), "第一次心跳必须清掉超期节点"

    # ② 节流窗口内：又冒出一条超期行，这一拍不该被清
    import time as _time
    _dead("drill-dead-2")
    job_repo._last_prune = _time.monotonic()
    job_repo.heartbeat("w-alive", 4001, None, "v1")
    assert _alive("drill-dead-2"), "一小时之内不该重复清"

    # ③ 越过窗口：照旧清
    job_repo._last_prune = _time.monotonic() - 3601
    job_repo.heartbeat("w-alive", 4001, None, "v1")
    assert not _alive("drill-dead-2"), "过了一小时必须恢复清理"


def test_mock分支的库调用也必须走锁兜底():
    """`_run_mock` 里的三处库调用原来是**裸的**（2026-11-10 整库审计修的）。

    同一个文件里 `_pump` 对 `status_of/renew/heartbeat` 这三个调用全都走
    `_db_guard(log)`，唯独 mock 分支没有。于是一条 mock 作业只要撞上
    `pipeline.advance_run` 的写锁，`OperationalError: database is locked`
    就冒泡出 `_run_mock`，被 `process()` 的 `except Exception` 写死成
    `failed / internal_error`——而产物一件都没坏。`_db_guard` 的文档
    承诺的正是这件事的反面。

    这里把三个调用**全部**换成会锁死的桩，逼它真的走一遍：
    作业必须正常跑完并交出产物，一句异常都不许冒出来。
    只在其中一个调用上加 `db(...)` 的半修，会在另外两处变红。
    """
    import jobfiles

    log = io.StringIO()
    guard = worker._db_guard(log)
    calls = {"status_of": 0, "renew": 0, "heartbeat": 0}

    def locked(name):
        def fn(*_args, **_kwargs):
            calls[name] += 1
            raise sqlite3.OperationalError("database is locked")
        return fn

    real = {n: getattr(worker.jobs, n) for n in calls}

    for name in calls:
        setattr(worker.jobs, name, locked(name))
    job = {"id": 990001, "capability": "text", "provider_code": "mock"}
    params = {"text": "锁也拦不住我", "sleep": 0.25, "fail": False,
              "media": "text"}
    try:
        paths, _elapsed = worker._run_mock(job, params, log, "w-test", guard)
    finally:
        for name, fn in real.items():
            setattr(worker.jobs, name, fn)

    assert calls["status_of"] and calls["renew"] and calls["heartbeat"], (
        f"三个调用没有都真的走到，这条测试盖不住全部裸调用点：{calls}")
    assert paths and paths[0].endswith("result-990001.txt"), (
        f"锁冲突把 mock 作业打断成没有产物了：{paths}")
    assert "库锁" in log.getvalue(), "降级要在 run.log 留痕，不能静默"
    target = jobfiles.JOBS_DIR / paths[0]
    assert target.exists() and "锁也拦不住我" in target.read_text(encoding="utf-8")


def test_刚重启的机器也立刻清死节点(fresh_project, clean_queue, monkeypatch):
    """**开机不到一小时时，第一次心跳照样要清死节点**（2026-11-11 真 bug）。

    这条是回归守卫，不是重复：`_last_prune` 曾用 `0.0` 当"从没清过"的哨兵，
    而判据是 `time.monotonic() - _last_prune >= 3600`。`monotonic()` 是**开机
    以来秒数**，机器刚重启时只有几百秒，于是差值小于 3600 ⇒ 开机后第一个
    小时内一次都不清理。product 行为与用例同时错，且只在"刚重启"时暴露
    ——正是 CI 冷启动最容易命中的情形。

    这里把 monotonic 压到 5 秒（模拟刚开机），断言第一拍仍然清理。
    """
    from repositories.jobs import jobs as job_repo
    import repositories.jobs_parts.lease as lease_mod

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO worker_node (worker_id, pid, host, heartbeat_at, version)
               VALUES ('boot-dead', 1, 'h', datetime('now','localtime','-8 days'), 'v1')""")

    class _FakeTime:
        @staticmethod
        def monotonic():
            return 5.0                    # 开机 5 秒

    monkeypatch.setattr(lease_mod, "time", _FakeTime)
    job_repo._last_prune = None
    job_repo.heartbeat("w-boot", 4002, None, "v1")

    with get_conn() as conn:
        alive = conn.execute(
            "SELECT 1 FROM worker_node WHERE worker_id = 'boot-dead'").fetchone()
    assert alive is None, (
        "开机 5 秒时第一次心跳没清死节点——说明哨兵又被写回 0.0 那类值了"
        "（monotonic 小于 3600 时 0.0 哨兵永远不触发清理）")
