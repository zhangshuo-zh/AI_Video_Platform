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

    # ① 首次心跳（_last_prune 初始 0）应当清掉超期行
    job_repo._last_prune = 0.0
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
