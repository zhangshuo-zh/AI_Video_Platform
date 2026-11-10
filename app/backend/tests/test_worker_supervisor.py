"""从网页启停 worker（0048）。

这个模块能出错的地方几乎都不是"代码写错了"，而是**判活判错**：进程明明退了，
却认为它还在；或者反过来。所以测试的重点是那几条判据，不是"接口返回 200"。

一条真实踩到的 bug 被钉在这里（`test_本进程起的_worker_退出后要及时判死`）：
worker 被 SIGTERM 后 0.5 秒就优雅退了，但因为父进程还没 `wait()`，
它是个**僵尸**——`kill(pid, 0)` 对僵尸照样返回成功，于是判活一直为真，
8 秒后走进强杀分支，回执写成"8 秒内没退，已强杀"。**一次干净的优雅退出
被报成了强杀**，而且会误导人去查"为什么 worker 不肯退"。
"""
from __future__ import annotations

import os
import signal
import sys
import time

import pytest


@pytest.fixture()
def sup(tmp_path, monkeypatch):
    """每个测试一个独立的 PID 文件与日志，互不干扰，也不碰现役 data/。"""
    monkeypatch.setenv("FASTVIDEO_DB", str(tmp_path / "t.db"))
    from migrate import migrate
    migrate(verbose=False)

    import worker_supervisor as module
    monkeypatch.setattr(module, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(module, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(module, "PID_DIR", tmp_path / "run" / "workers")
    monkeypatch.setattr(module, "WORKER_PID", tmp_path / "run" / "worker.pid")
    monkeypatch.setattr(module, "WORKER_LOG", tmp_path / "logs" / "worker.log")
    module._LAUNCHED.clear()
    yield module
    # 收拾：本测试起的进程一律杀掉，别留给下一个测试
    for pid in list(module._LAUNCHED):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    module._LAUNCHED.clear()


def _spawn_sleeper(sup, seconds: float = 30.0):
    """起一个"占着不放"的假 worker：不是真 worker.py，只为测判活与停止。

    用当前解释器跑 `sleep`，与真 worker 的进程形态一致（同一 Popen 语义）。
    """
    import subprocess
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    sup._LAUNCHED[proc.pid] = proc
    sup.PID_DIR.mkdir(parents=True, exist_ok=True)
    (sup.PID_DIR / f"{proc.pid}.pid").write_text(str(proc.pid))
    return proc


def _proc_stat(pid: int) -> str | None:
    """`ps -o stat=` 的第一列（`Z` 开头＝僵尸）。查不到返回 None。"""
    import subprocess
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    return text or None


def _kill0(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class TestLiveness:
    """判活：这是整个模块最容易错的地方。"""

    def test_僵尸进程不能被判成活着(self, sup):
        """**这是本模块最关键的一条**，钉的是一个真踩到的 bug。

        本进程起的 worker 退出后、父进程 poll() 之前是个**僵尸**：
        进程表里还占着那个号，`kill(pid, 0)` 照样返回成功（实测
        `ps -o stat` 显示 `Z` 而 kill -0 为 True）。于是：
        SIGTERM → worker 0.5 秒优雅退出 → 判活一直为真 → 8 秒后走强杀分支
        → 回执写成「8 秒内没退，已强杀」。**一次干净的退出被报成强杀**，
        还会误导人去查"worker 为什么不肯退"，而那件事根本没发生。

        **测试必须先造出僵尸状态再断言**：不能先调 `_reap()`/`poll()`
        ——那一步会把僵尸回收掉，之后 `kill -0` 自然返回 False，
        这条测试就变成永远绿的假绿（第一版就是这么写的，变异验红时没抓住）。
        """
        proc = _spawn_sleeper(sup, 30)
        assert sup._pid_alive(proc.pid) is True

        os.kill(proc.pid, signal.SIGTERM)
        # 等它真的死透——但**全程不碰 poll()**，让它保持僵尸
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stat = _proc_stat(proc.pid)
            if stat is None or stat.startswith("Z"):
                break
            time.sleep(0.05)

        stat = _proc_stat(proc.pid)
        if stat is None:
            pytest.skip("这台机器上进程已完全消失，造不出僵尸状态")
        assert stat.startswith("Z"), f"没造出僵尸，实际 stat={stat}"

        # 僵尸：kill -0 仍为 True，但 poll() 说它退了
        assert _kill0(proc.pid) is True, "沙箱行为变了：僵尸的 kill -0 居然为 False"
        assert sup._pid_alive(proc.pid) is False, (
            "僵尸被判成活着——判活退回了 kill -0，强杀误报的 bug 回来了")

    def test_stop_不把优雅退出报成强杀(self, sup):
        """端到端复现那个回执错误：0.5 秒退出的进程，回执里不许出现"已强杀"。"""
        _spawn_sleeper(sup, 30)
        started = time.monotonic()
        result = sup.stop()
        elapsed = time.monotonic() - started
        assert result["ok"] is True
        assert not any("已强杀" in m for m in result["messages"]), (
            f"优雅退出被报成强杀：{result['messages']}")
        assert elapsed < 5, f"停一个立刻响应的进程花了 {elapsed:.1f} 秒，说明判活没及时"

    def test_别人起的进程用_kill_判活(self, sup):
        """不是本进程起的（没有 Popen 句柄）走 kill -0。
        这条防的是"只认 _LAUNCHED，把 run.sh 起的 worker 判成死的"。"""
        proc = _spawn_sleeper(sup, 30)
        # 假装不是我们起的：把句柄摘掉
        sup._LAUNCHED.pop(proc.pid)
        try:
            assert sup._pid_alive(proc.pid) is True, (
                "没有 Popen 句柄时应退回 kill -0，而不是一律判死")
        finally:
            os.kill(proc.pid, signal.SIGKILL)

    def test_不存在的进程判死(self, sup):
        assert sup._pid_alive(0) is False
        assert sup._pid_alive(-1) is False
        assert sup._pid_alive(999999) is False


class TestPidFile:
    """PID 文件是与 `run.sh` 共用的唯一握手点。"""

    def test_没有文件时报未运行(self, sup):
        state = sup.current()
        assert state["running"] is False
        assert state["count"] == 0
        assert state["pids"] == []
        assert state["stale_pids"] == []

    def test_残留_PID_要如实报出来(self, sup):
        """上次强杀/断电留下的号不该被说成"从未启动"——那会掩盖异常。"""
        sup.PID_DIR.mkdir(parents=True, exist_ok=True)
        (sup.PID_DIR / "999999.pid").write_text("999999")
        state = sup.current()
        assert state["running"] is False
        assert 999999 in state["stale_pids"]

    def test_写坏的_PID_文件当没有(self, sup):
        """文件里是垃圾就返回 None，不要把 int() 异常抛到界面上。"""
        sup.PID_DIR.mkdir(parents=True, exist_ok=True)
        (sup.PID_DIR / "abc.pid").write_text("not-a-number")
        assert sup.read_pid() is None
        assert sup.current()["running"] is False


class TestStartStop:
    """启停语义。"""

    def test_超过上限才被拒(self, sup):
        """0049 起多开是合法需求（`claim_next` 靠 BEGIN IMMEDIATE 保证不重复领取），
        所以"已经有在跑"不再是错误；仍然要挡的是**开太多**——
        8 个以上的解释器加模型进程在 16 GB 机器上必然 OOM。"""
        result = sup.start(count=sup.MAX_WORKERS + 1)
        assert result["ok"] is False
        assert "上限" in result["message"]

    def test_停止是幂等的(self, sup):
        """停一个本来就没跑的不算错误：刷新后重复点不该报红。"""
        result = sup.stop()
        assert result["ok"] is True
        assert any("没有 worker 在跑" in m for m in result["messages"])

    def test_停止会清掉残留_PID(self, sup):
        sup.PID_DIR.mkdir(parents=True, exist_ok=True)
        (sup.PID_DIR / "999999.pid").write_text("999999")
        result = sup.stop()
        assert result["ok"] is True
        assert any("残留" in m for m in result["messages"])
        assert not (sup.PID_DIR / "999999.pid").exists(), "残留的 PID 文件该被清掉"

    def test_停止先_SIGTERM_不直接强杀(self, sup):
        """真 worker 装了信号处理，SIGTERM 会让它跑完当前这一轮再退。
        直接 SIGKILL 会留 running 状态给 recover_stale 收——能不用就不用。"""
        proc = _spawn_sleeper(sup, 30)
        result = sup.stop()
        assert result["ok"] is True
        assert not any("已强杀" in m for m in result["messages"]), (
            f"sleep 收到 SIGTERM 立刻退，不该走到强杀：{result['messages']}")
        assert proc.poll() is not None

    def test_起不来的_worker_要说原因(self, sup, monkeypatch):
        """进程起来了但没写心跳 → 报失败并带日志尾部，不报成功。
        "界面说成功而队列不动"是最坏的情况。"""
        monkeypatch.setattr(sup, "START_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(sup, "_heartbeat_written_since",
                            lambda pid, *, since: False)
        proc = _spawn_sleeper(sup, 30)
        # 把 PID 文件清掉，让 start() 认为可以起
        (sup.PID_DIR / f"{proc.pid}.pid").unlink()
        # 这次的 start 会真起 worker.py（它连的是临时库，能起来但没心跳判断被我们
        # 强制为 False）——所以必然走超时分支
        result = sup.start()
        try:
            assert result["ok"] is False
            assert "没有写心跳" in result["message"]
        finally:
            os.kill(proc.pid, signal.SIGKILL)


    def test_启动的_worker_自成进程组(self, sup):
            """孤儿防护的可验证形式：worker 的 pgid 必须等于它自己的 pid。

            `start_new_session=True` 让子进程 `setsid()`——它成为新会话与新进程组
            的组长。这样 API 重启/退出时不会连带杀掉 worker（它不在 API 的进程组里），
            也不会被 uvicorn reloader 那种壳进程带成孤儿。

            为什么不直接断言"API 重启后 worker 还活着"：那要真重启 API，
            测试里做不成也不该做。**进程组归属才是那个保证的机制本身**，
            验它比验一个模拟的重启更准。
            """
            import subprocess
            # 用真 worker.py 起（要真的走 start() 的那条 Popen 分支）
            sup.WORKER_PID.parent.mkdir(parents=True, exist_ok=True)
            if sup.WORKER_PID.exists():
                # 这里原来写的是 `(sup.PID_DIR / f"{proc.pid}.pid").unlink()`——
                # `proc` 在这个作用域**根本没有定义**（ruff F821 抓到的既有缺陷）。
                # 夹具每次都把 `RUN_DIR` 指到全新的 `tmp_path`，所以
                # `WORKER_PID.exists()` 恒为假、这行从未执行过，`NameError` 一直
                # 藏着。哪天有人复用这个夹具、或把 RUN_DIR 指到真实 `data/run`，
                # 这里就会以 NameError 失败，而失败信息指向一句"文件清理"，
                # 跟真正要测的进程组归属毫无关系。
                # 意图是"清掉可能残留的旧 worker 登记"，删的就是 `WORKER_PID`。
                sup.WORKER_PID.unlink()
            import worker_supervisor as module
            # 心跳判据在临时库里会等满超时；这里只关心进程组，把等待缩到最短
            original = module._heartbeat_written_since
            module._heartbeat_written_since = lambda pid, *, since: True
            module.START_TIMEOUT_SECONDS = 5.0
            try:
                result = sup.start()
            finally:
                module._heartbeat_written_since = original
            assert result["ok"] is True, result
            pid = result["pids"][0]
            try:
                out = subprocess.run(["ps", "-o", "pgid=,sess=", "-p", str(pid)],
                                     capture_output=True, text=True, timeout=5)
                parts = out.stdout.split()
                assert len(parts) >= 2, f"ps 输出不可解析：{out.stdout!r}"
                pgid = int(parts[0])
                # **只断言 pgid**：macOS 的 `ps -o sess=` 恒返回 0（实测），
                # 拿它当判据会让这条测试在 Mac 上永远红。而"自成进程组"已经是
                # setsid() 的直接后果——进程组组长必须是自己，这一条就够了。
                assert pgid == pid, (
                    f"worker 的进程组 {pgid} 不等于自己的 pid {pid}——"
                    f"没有 setsid，API 退出会把它带走或让它变孤儿")
            finally:
                os.kill(pid, signal.SIGKILL)
                sup._LAUNCHED.clear()


class TestEnv:
    """环境补齐：不补的话 worker 起得来但跑不了活。"""

    def test_补齐两个运行时变量(self, sup, tmp_path, monkeypatch):
        """`run.sh` 在起 uvicorn 前 export 了这两个（`run.sh:24-35`），
        注释原话「API 与 worker 都要带上，否则系统设置页和实际执行会看到两个答案」。
        API 若是直接 uvicorn 起的就没有，所以这里必须自己补。"""
        fake_home = tmp_path / "home"
        (fake_home / "FastVideo").mkdir(parents=True)
        mflux = fake_home / ".local" / "bin" / "mflux-generate"
        mflux.parent.mkdir(parents=True)
        mflux.write_text("#!/bin/sh\n")
        mflux.chmod(0o755)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.delenv("FASTVIDEO_FASTMETAL_HOME", raising=False)
        monkeypatch.delenv("FASTVIDEO_MFLUX_BIN", raising=False)

        env = sup._worker_env()
        assert env["FASTVIDEO_FASTMETAL_HOME"] == str(fake_home / "FastVideo")
        assert env["FASTVIDEO_MFLUX_BIN"] == str(mflux)

    def test_已有的值不被覆盖(self, sup, monkeypatch):
        """部署时显式配过的以那个为准——与 `run.sh` 的 `[ -z ... ]` 同义。"""
        monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", "/custom/where")
        env = sup._worker_env()
        assert env["FASTVIDEO_FASTMETAL_HOME"] == "/custom/where"
