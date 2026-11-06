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
