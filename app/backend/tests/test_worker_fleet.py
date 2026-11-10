"""多开、守护与通知（0049）。

这一组测的是**判定**，不是"接口返回 200"。三个功能各有一条最容易错的判据：

* 多开：`start(count=N)` 真的要起 N 个，且 **N 个都要写心跳**才算成功。
* 守护：`exposed` 必须在"配置开着但线程没跑"或"连续失败已放弃"时为真
  ——否则界面会一直显示"守护中"而实际没人守。
* 通知：收件人是按 `audience` 在查询期解析的，所以**删掉项目成员之后
  那条通知就该看不见了**（不展开成员的直接后果）。

三条真踩到的 bug 也钉在这里：
1. `stop()` 返回的是 `{messages: [...]}` 不是 `{message: str}`，
   路由读错字段直接 KeyError 500。
2. `deps.require_user` 返回 **sqlite3.Row** 不是 dict，
   用 `isinstance(user, dict)` 判会让收件箱在真实请求里永远是空的
   （而单测传 dict 又是好的）。
3. `_my_project_ids` 的 UNION 两边列名不同（`id` vs `project_id`），
   不显式起别名就要靠猜。
"""
from __future__ import annotations

import json
import os
import uuid
import signal
import sys
import time

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离 PID 目录与守护配置。

    **不改 `FASTVIDEO_DB`**：`conftest.py:23` 在任何项目 import 之前就把它设成了
    一个会话级的临时库，而且 `config.DB_PATH` 是 import 时读的常量——这里再
    `setenv` 完全无效，测试会打到真实库里（第一版就是这么错的：单独跑绿、
    一起跑 11 红）。按 conftest 的规矩：会改数据的用例自己造 uuid 数据。
    """
    import worker_supervisor as sup
    monkeypatch.setattr(sup, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(sup, "PID_DIR", tmp_path / "run" / "workers")
    monkeypatch.setattr(sup, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(sup, "WORKER_LOG", tmp_path / "logs" / "worker.log")
    sup._LAUNCHED.clear()

    import worker_daemon as daemon
    monkeypatch.setattr(daemon, "DAEMON_CONFIG", tmp_path / "run" / "daemon.json")

    yield {"sup": sup, "daemon": daemon, "tmp": tmp_path}

    # 共享库（conftest 只有一个库）：把自己造的行清掉，别让后续测试看见。
    try:
        from db import get_conn
        with get_conn() as conn:
            conn.execute("DELETE FROM job WHERE project_id IN "
                         "(SELECT id FROM project WHERE name LIKE ?)", (f"%-{_MARK}",))
            conn.execute("DELETE FROM project WHERE name LIKE ?", (f"%-{_MARK}",))
            conn.execute("DELETE FROM app_user WHERE username LIKE ?", (f"%-{_MARK}",))
            conn.execute("DELETE FROM notification")
            conn.execute("DELETE FROM notification_read")
    except Exception:
        pass

    for pid in list(sup._LAUNCHED):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    sup._LAUNCHED.clear()


def _fake_user(**kw):
    """造一个 dict 形态的用户。注意真实请求拿到的是 sqlite3.Row，
    两者都要认（见 `notifications._user_id`）。"""
    base = {"id": 1, "username": "u", "is_superuser": False}
    base.update(kw)
    return base


_MARK = uuid.uuid4().hex[:8]


@pytest.fixture()
def uniq():
    """本模块内唯一的 id 前缀，避免与共享库里的既有数据撞车。"""
    return uuid.uuid4().int % 10_000_000 + 1_000_000


def _ensure_user(user_id: int) -> int:
    """建一个用户并返回 id。`project.owner_id` 有外键，必须指向真实用户——
    拿一个随便的号当 owner 会 `FOREIGN KEY constraint failed`（真踩到）。"""
    from db import get_conn
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO app_user
                   (id, username, display_name, password_hash, is_active, is_superuser)
               VALUES (?, ?, ?, 'x', 1, 0)""",
            (user_id, f"u{user_id}-{_MARK}", f"U{user_id}"))
    return user_id


def _insert_project(project_id: int, owner_id: int):
    _ensure_user(owner_id)       # 先保证 owner 存在，否则外键不过
    from db import get_conn
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO project (id, name, status, owner_id)
               VALUES (?, ?, 'active', ?)""",
            (project_id, f"p{project_id}-{_MARK}", owner_id))


def _insert_queued_job(project_id: int, status: str = "queued"):
    from db import get_conn
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO job (project_id, capability, kind, execution_mode,
                   provider_code, model_code, status, queued_at, created_at, updated_at)
               VALUES (?, 'text', 'text', 'cloud', 'deepseek', 'm', ?,
                       datetime('now','localtime'), datetime('now','localtime'),
                       datetime('now','localtime'))""", (project_id, status))


class TestMultiWorker:
    """多开的返回值与状态汇总。"""

    def test_起多个时_pids_要凑够数(self, env):
        """`start(count=2)` 必须真的起 2 个并各写心跳。

        用假 worker（sleep）替身会验不出心跳，所以这里只验 `current()`
        的汇总逻辑本身——真起进程那条在 e2e 里验过（实测 count=2 得到两个 pid）。
        """
        sup = env["sup"]
        from db import get_conn
        # 手工登记两个"活着的"pid：用当前进程与它的子进程
        import subprocess
        p1 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                              start_new_session=True)
        p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                              start_new_session=True)
        try:
            sup.PID_DIR.mkdir(parents=True, exist_ok=True)
            (sup.PID_DIR / f"{p1.pid}.pid").write_text(str(p1.pid))
            (sup.PID_DIR / f"{p2.pid}.pid").write_text(str(p2.pid))
            state = sup.current()
            assert state["count"] == 2
            assert sorted(state["pids"]) == sorted([p1.pid, p2.pid])
        finally:
            for p in (p1, p2):
                p.kill(); p.wait()

    def test_超上限要拒(self, env):
        """防呆：一台机器开 8 个以上必然 OOM。这条挡的是误点。"""
        sup = env["sup"]
        # PID 目录是空的（fixture 隔离过），count 必然超过上限
        result = sup.start(count=99)
        assert result["ok"] is False
        assert "上限" in result["message"]

    def test_残留登记要如实报(self, env):
        """文件在、进程没了 → stale_pids 要有它。**不自动清**：
        自动清会让"上次崩过"这件事不可见，而它正是排查起点。"""
        sup = env["sup"]
        sup.PID_DIR.mkdir(parents=True, exist_ok=True)
        (sup.PID_DIR / "999999.pid").write_text("999999")
        state = sup.current()
        assert state["count"] == 0
        assert 999999 in state["stale_pids"]
        assert (sup.PID_DIR / "999999.pid").exists(), "读取时不该顺手删"

    def test_清理残留要动真格(self, env):
        sup = env["sup"]
        sup.PID_DIR.mkdir(parents=True, exist_ok=True)
        (sup.PID_DIR / "999999.pid").write_text("999999")
        removed = sup.clean_stale()
        assert removed == [999999]
        assert not (sup.PID_DIR / "999999.pid").exists()

    def test_stop_返回_messages_列表而不是单个_message(self, env):
        """这是真踩到的 500：`stop()` 改成返回 `{messages: [...]}` 之后，
        路由还读 `result["message"]`，`KeyError: 'message'` 直接 500。
        钉住返回结构，免得再有人改回去。"""
        sup = env["sup"]
        result = sup.stop()
        assert "messages" in result, f"返回体缺 messages：{sorted(result)}"
        assert isinstance(result["messages"], list)
        assert "stopped" in result


class TestDaemon:
    """守护的判定：`exposed`。"""

    def test_默认不守(self, env):
        """配置不存在时**默认关**——让人在不知情的情况下多了个会自动起进程的
        东西，比没有守护更糟。"""
        daemon = env["daemon"]
        cfg = daemon.load_config()
        assert cfg["enabled"] is False
        assert daemon.check()["exposed"] is True, "默认关着就该报'没在守'"

    def test_配置文件坏了也不默认开(self, env):
        """坏 JSON 当成关闭——宁可不管，也不要因为读不懂配置就乱起进程。"""
        daemon = env["daemon"]
        daemon.DAEMON_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        daemon.DAEMON_CONFIG.write_text("{ 这不是 json")
        assert daemon.load_config()["enabled"] is False

    def test_开了但线程没跑也算没守(self, env):
        """**这条是这个模块最该被读懂的一句**：`enabled=true` 不等于在守。
        线程没在跑时界面必须红着说，不然人会以为有兜底。"""
        daemon = env["daemon"]
        daemon.stop_thread()          # 确保没有别处起的线程在跑
        daemon.save_config(enabled=True, desired=2)
        status = daemon.check()
        assert status["enabled"] is True
        assert status["thread_alive"] is False
        assert status["exposed"] is True, "线程没跑却报'守护中'，界面就会骗人"

    def test_连续失败到上限就停手并升级为_需要人处理(self, env):
        """环境坏了（缺权重）时不能无限重试刷爆日志。"""
        daemon = env["daemon"]
        daemon._STATE["failures"] = daemon.MAX_FAILURES
        daemon.save_config(enabled=True, desired=1)
        status = daemon.check()
        assert status["exposed"] is True
        assert status["failures"] >= daemon.MAX_FAILURES

    def test_desired_为_0_是合法值(self, env):
        """0 表示"守着但不许有"——不能因为 `if not desired` 就退回默认 1。"""
        daemon = env["daemon"]
        cfg = daemon.save_config(enabled=True, desired=0)
        assert cfg["desired"] == 0
        assert daemon.load_config()["desired"] == 0


class TestNotifications:
    """通知的收件人解析与去重。"""

    def test_项目通知要能到成员手上(self, env, uniq):
        """需求原话："被影响的项目所属的用户和协作者可以收到消息提醒"。"""
        import notifications
        _insert_project(uniq, owner_id=uniq)
        notifications.emit(kind="worker_offline", level="bad",
                           title="worker 掉线", audience=f"project:{uniq}",
                           dedupe_key=f"d1-{_MARK}")
        box = notifications.inbox(_fake_user(id=uniq))
        assert len(box["items"]) == 1
        assert box["items"][0]["title"] == "worker 掉线"

    def test_收到的要能用_sqlite3_Row_形态的用户(self, env, uniq):
        """**真踩到的 bug**：`deps.require_user` 返回的是 sqlite3.Row，
        用 `isinstance(user, dict) or user.get("id")` 判会让收件箱在**真实
        请求里永远是空的**，而单测传 dict 又是好的——典型的"测试绿、界面空"。

        这条必须拿到**真正的 Row**（不是 dict），否则测的就不是那个 bug。
        """
        import notifications
        from db import get_conn
        _insert_project(uniq, owner_id=uniq)
        notifications.emit(kind="worker_offline", level="bad",
                           title="row-user", audience=f"project:{uniq}",
                           dedupe_key=f"row-{_MARK}")
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM app_user WHERE id = ?",
                               (uniq,)).fetchone()
        assert row is not None, "夹具没造出用户"
        assert not isinstance(row, dict), "这个测试的意义就在于 user 不是 dict"
        box = notifications.inbox(row)
        assert any(it["title"] == "row-user" for it in box["items"]), (
            "sqlite3.Row 形态的用户读不到收件箱——`_user_id` 退回了 isinstance(dict) 判断")

    def test_同一条不重复发(self, env, uniq):
        """worker 掉线每 15 秒检查一次，每轮发一条就是刷屏。"""
        import notifications
        _insert_project(uniq, owner_id=uniq)
        first = notifications.emit(kind="worker_offline", level="bad",
                                   title="t", audience=f"project:{uniq}",
                                   dedupe_key="same")
        second = notifications.emit(kind="worker_offline", level="bad",
                                    title="t", audience=f"project:{uniq}",
                                    dedupe_key="same")
        assert first is not None
        assert second is None, "同 dedupe_key 的没被去重"

    def test_结清之后还能再发(self, env, uniq):
        """**这条比去重更重要**：不结清的话，下次真掉线会被去重挡住——
        该响的时候不响，比重复提醒糟得多。"""
        import notifications
        _insert_project(uniq, owner_id=uniq)
        notifications.emit(kind="worker_offline", level="bad", title="t",
                           audience=f"project:{uniq}", dedupe_key="k")
        assert notifications.emit(kind="worker_offline", level="bad", title="t",
                                  audience=f"project:{uniq}", dedupe_key="k") is None
        notifications.resolve(kind="worker_offline", audience=f"project:{uniq}",
                              dedupe_key="k")
        again = notifications.emit(kind="worker_offline", level="bad", title="t",
                                   audience=f"project:{uniq}", dedupe_key="k")
        assert again is not None, "结清之后再发应该成功（否则真掉线时发不出来）"

    def test_已读按人记(self, env, uniq):
        """一条通知被两个成员看到，各自标已读互不影响——这是按人记的直接后果。

        原来这条只验了 a 标完变 0，b 那句注释着说"应该还是未读"却没断言，
        等于没测。现在两个人都造出来、都断言。
        """
        import notifications
        from db import get_conn
        member = uniq + 1
        _insert_project(uniq, owner_id=uniq)
        _ensure_user(member)
        with get_conn() as conn:
            conn.execute("INSERT INTO project_member (project_id, user_id) VALUES (?, ?)",
                         (uniq, member))

        nid = notifications.emit(kind="worker_offline", level="bad", title="t",
                                 audience=f"project:{uniq}",
                                 dedupe_key=f"rd-{_MARK}")
        assert nid is not None

        a = _fake_user(id=uniq)        # 项目所有者
        b = _fake_user(id=member)      # 协作者
        assert notifications.inbox(a)["unread"] == 1
        assert notifications.inbox(b)["unread"] == 1

        notifications.mark_read(a, [nid])
        assert notifications.inbox(a)["unread"] == 0, "a 标过就该是 0"
        assert notifications.inbox(b)["unread"] == 1, (
            "b 没标过，不该因为 a 标了就跟着变已读")

    def test_audience_格式不对要抛(self, env):
        """拼错不会静默忽略——一条发错人的通知比没发更糟。"""
        import notifications
        with pytest.raises(ValueError):
            notifications.emit(kind="worker_offline", level="bad", title="t",
                               audience="proejct:8")      # 拼错
        with pytest.raises(ValueError):
            notifications.emit(kind="nope", level="bad", title="t",
                               audience="project:8")

    def test_管理员的两个项目列表要合并对(self, env, uniq):
        """`_my_project_ids` 的 UNION 两边列名不同（id vs project_id），
        不显式起别名就要靠猜——这是真踩到的。"""
        import notifications
        _insert_project(uniq, owner_id=uniq)
        member_id = uniq + 1
        _ensure_user(member_id)
        from db import get_conn
        with get_conn() as conn:
            conn.execute("""INSERT INTO project_member (project_id, user_id)
                            VALUES (?, ?)""", (uniq, member_id))
        assert uniq in notifications._my_project_ids(uniq), "自己拥有的项目要算进去"
        assert uniq in notifications._my_project_ids(member_id), "作为成员加入的也要算"


class TestOfflineNotice:
    """掉线通知的触发条件。"""

    def test_有_worker_在线时不发(self, env, uniq):
        """有 worker 在跑就不算"队列停了"，发了就是噪音。"""
        import worker_notice
        _insert_project(uniq, owner_id=uniq)
        _insert_queued_job(uniq)
        assert worker_notice.notify_offline(online=1, queued_total=1) == []

    def test_队列空时不发(self, env, uniq):
        """没人被影响，不必打扰。"""
        import worker_notice
        _insert_project(uniq, owner_id=uniq)
        assert worker_notice.notify_offline(online=0, queued_total=0) == []

    def test_掉线且有排队时按项目发(self, env, uniq):
        import worker_notice
        _insert_project(uniq, owner_id=uniq)
        _insert_queued_job(uniq)
        sent = worker_notice.notify_offline(online=0, queued_total=1)
        assert uniq in sent

    def test_只数_queued_不数_running(self, env, uniq):
        """`running` 说明 worker 正在跑它，那条走"作业失败"通知，
        不是"队列停了"这条。混在一起会让同一个项目收到两条含义不同的红条。"""
        import worker_notice
        _insert_project(uniq, owner_id=uniq)
        _insert_queued_job(uniq, status="running")
        assert uniq not in worker_notice.queued_by_project()


class TestOptimizations:
    """审查后补的两条优化，各钉一个"不做就白费"的点。"""

    def test_没有掉线通知时不发_DELETE(self, env, uniq, monkeypatch):
        """`resolve_offline` 原来无条件 `DELETE FROM notification`，而它挂在
        守护线程里每 15 秒跑一次——worker 正常时永远是 0 行，等于每 15 秒
        拿一次写锁空转（`heartbeat` 里那条 prune 为同样的事被优化过）。

        **判据是"有没有走到那条 DELETE"**。前两版都没抓住变异：
          * v1 用 `total_changes()`：影响 0 行的 DELETE 不增加计数，测不出来；
          * v2 挂 `set_trace_callback`：`resolve_offline` 内部自己开连接，
            外部那条挂的 trace 抓不到它的语句。
        现在改成**在 `get_conn` 这一层包一层**：模块拿到的每条连接都装 trace，
        这样无论它在哪儿开连接都记录得到。
        """
        import contextlib
        import worker_notice
        import db as db_module
        from db import get_conn

        statements: list[str] = []

        @contextlib.contextmanager
        def traced_conn():
            with get_conn() as conn:
                conn.set_trace_callback(lambda sql: statements.append(sql))
                yield conn

        # 清干净，让判断真的走到"不该删"那一支
        with get_conn() as conn:
            conn.execute("DELETE FROM notification")

        monkeypatch.setattr(worker_notice, "get_conn", traced_conn)
        cleared = worker_notice.resolve_offline()

        writes = [q for q in statements
                  if q.lstrip().upper().startswith(("DELETE", "INSERT", "UPDATE"))]
        assert cleared == 0
        assert not writes, f"没有通知却执行了写语句：{writes}"

    def test_通知表有_audience_索引(self, env):
        """收件箱那条 SQL 的 WHERE 是 `audience = ?` 的 OR 组合，
        0049 的两条索引（created_at、kind 打头）都用不上。
        没有 (audience, id DESC) 就是每次拉通知全表扫，
        而这是每个登录者每 20 秒都打的接口。
        """
        from db import get_conn
        with get_conn() as conn:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='notification'")}
        assert "idx_notification_audience" in names, (
            f"缺 audience 索引，实有：{sorted(names)}")


class TestP0Regressions:
    """复核时发现的两条 P0。两条都是"注释写了正确意图、代码是另一回事"。"""

    def test_守护关着也要发掉线通知(self, env, uniq, monkeypatch):
        """**H1**：`_tick` 原来先 `if not cfg["enabled"]: return`，再调
        `_notify_state()`——而守护**默认关**（opt-in）。于是 worker 掉了、
        队列里还排着活、受影响的项目**一条通知都收不到**，除非有人先去开守护。
        等于"worker 掉线通知项目成员"这条需求在默认配置下完全不工作。

        讽刺的是我上一版把"无论守护开没开都要做"写进了注释，代码却把它放在
        早退之后——注释成了空话。实测确认过：修之前 `_tick()` 后通知表 0 条。
        """
        import worker_daemon as daemon
        import worker_notice
        from db import get_conn

        _insert_project(uniq, owner_id=uniq)
        _insert_queued_job(uniq)
        daemon.save_config(enabled=False, desired=1)     # 默认状态：守护关

        # 确保"当前没有 worker 在跑"（fixture 已隔离 PID 目录）
        assert env["sup"].current()["count"] == 0

        daemon._tick()

        box = __import__("notifications").inbox(_fake_user(id=uniq))
        offline = [x for x in box["items"] if x["kind"] == "worker_offline"]
        assert offline, "守护关着时没发掉线通知——通知链路被早退挡掉了"

    def test_stop_one_拒绝不是本平台登记的_pid(self, env, uniq):
        """**H2**：`stop_one(pid)` 原来拿到 pid 就 `os.kill`，而 pid 来自
        `POST /worker/process/stop-one` 的请求体。于是任意 pid 都能被
        SIGTERM、8 秒后 SIGKILL——实测拿一个无关进程试，它被直接强杀了。
        有 `system:config` 的人能借此打掉 API 自己。

        判据用 `PID_DIR` 下的登记文件，不是猜命令行长什么样。
        """
        sup = env["sup"]
        import subprocess
        # 起一个与本平台无关的进程
        victim = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            start_new_session=True)
        try:
            assert sup._pid_alive(victim.pid)
            result = sup.stop_one(victim.pid, timeout=1)
            assert result["ok"] is False
            assert "不是本机登记的 worker" in result["message"]
            # **关键**：进程必须还活着
            assert victim.poll() is None, "任意 pid 被杀了"
        finally:
            victim.kill()
            victim.wait()

    def test_stop_one_对自己的_worker_仍然有效(self, env, uniq):
        """上一条的配对：加了存在性校验之后，真 worker 还得停得掉。
        只验"拒外部"会让实现倾向于无脑全拒也能绿。"""
        sup = env["sup"]
        import subprocess
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            start_new_session=True)
        try:
            sup.PID_DIR.mkdir(parents=True, exist_ok=True)
            (sup.PID_DIR / f"{worker.pid}.pid").write_text(str(worker.pid))
            result = sup.stop_one(worker.pid, timeout=5)
            assert result["ok"] is True
            assert "worker 已停止" in result["message"]
            assert worker.poll() is not None, "自己的 worker 没停掉"
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.wait()


class TestNotificationPrune:
    """表只增不减的治理（2026-11-10 复核提出）。

    `worker_daemon` 类每次补起/失败各记一条，而只有 `worker_offline` 会被
    `resolve_offline` 清掉——worker 每天崩 5 次就是 1825 条/年，收件箱只取
    最近 30 条，旧行全是垃圾而且让未读 COUNT(*) 越来越慢。
    """

    def test_超期通知会被删(self, env, uniq):
        import notifications
        from db import get_conn
        with get_conn() as conn:
            conn.execute("DELETE FROM notification")
            conn.execute(
                """INSERT INTO notification (kind, level, title, audience, created_at)
                   VALUES ('worker_daemon','warn','旧','config_admins',
                           datetime('now','localtime','-10 days'))""")
            conn.execute(
                """INSERT INTO notification (kind, level, title, audience)
                   VALUES ('worker_daemon','warn','新','config_admins')""")
        removed = notifications.prune(force=True)
        assert removed == 1, f"该只删那条 10 天前的，实删 {removed}"
        with get_conn() as conn:
            titles = [r["title"] for r in conn.execute("SELECT title FROM notification")]
        assert titles == ["新"], f"留下的应是新的那条，实得 {titles}"

    def test_已读行跟着级联删(self, env, uniq):
        """`notification_read` 靠 0049 建的 `ON DELETE CASCADE` 跟着走，
        不用手动清——但得验它真的生效（外键约束在 SQLite 上要 `PRAGMA
        foreign_keys` 打开才管用）。"""
        import notifications
        from db import get_conn
        with get_conn() as conn:
            conn.execute("DELETE FROM notification")
            conn.execute(
                """INSERT INTO notification (kind, level, title, audience, created_at)
                   VALUES ('worker_daemon','warn','旧','config_admins',
                           datetime('now','localtime','-10 days'))""")
            nid = conn.execute("SELECT id FROM notification").fetchone()["id"]
            conn.execute("INSERT OR IGNORE INTO notification_read (notification_id,user_id) VALUES (?,999)", (nid,))
        notifications.prune(force=True)
        with get_conn() as conn:
            left = conn.execute(
                "SELECT COUNT(*) AS n FROM notification_read WHERE notification_id=?",
                (nid,)).fetchone()["n"]
        assert left == 0, "父行删了，已读行该被级联删掉"

    def test_不强制时不会每次都删(self, env, uniq):
        """每小时最多一次——放在每 15 秒的路径上不能变成写放大。
        `worker_node` 那条 prune 就是为同样的事被优化过。"""
        import notifications
        notifications.prune(force=True)          # 把计时器拨到现在
        from db import get_conn
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO notification (kind, level, title, audience, created_at)
                   VALUES ('worker_daemon','warn','旧','config_admins',
                           datetime('now','localtime','-10 days'))""")
        assert notifications.prune() == 0, "刚跑过就又不该再删"


class TestStartRace:
    """`start()` 的「读已有数 → fork」竞态（2026-11-10 复核）。

    判断分两步、中间没有原子性。FastAPI 的同步路由跑在线程池里，所以
    两个管理员同时点「启动」、或一个人点启动时守护正好在处理 `need`，
    两个调用会**各自读到同一个 already**，各自都通过"不会超限"检查：

        already=7，两个 start(count=1) 并发 → 都看到 7+1≤8 → 都起 → 9 个

    而 `MAX_WORKERS=8` 的存在理由就是"这台机器的内存撑不住那么多"。
    """

    def test_并发_start_不会突破上限(self, env, monkeypatch):
        sup = env["sup"]
        # 把"起进程"换成记账，不真 fork（真 fork 会要心跳，测试里没有 worker）
        forked: list[int] = []
        fake_next = iter(range(900001, 900999))

        def fake_fork(*, count, max_global, per):
            already = sup.current()["count"]
            if count > sup.MAX_WORKERS or already + count > sup.MAX_WORKERS:
                return {"ok": False, "pids": [], "failed": [],
                        "message": f"已在跑 {already} 个，超过上限"}
            out = []
            for i in range(count):
                pid = next(fake_next)
                (sup.PID_DIR / f"{pid}.pid").write_text(str(pid))
                # 假登记：让 current() 数得到它，但探测判"活"
                monkeypatch.setitem(sup._FAKE_ALIVE, pid, True)
                forked.append(pid)
                out.append((pid, i, None))   # 三元组：pid、序号、proc 对象
            # `since` 是真代码的必填返回（心跳新鲜度的基线）。少给它会
            # 在 `start()` 里 KeyError —— 而 KeyError 发生在线程里，
            # 只变成一条 warning，下面那两条断言会**平凡成立**。
            return {"procs": out, "failed": [],
                    "since": time.strftime("%Y-%m-%d %H:%M:%S")}

        sup.PID_DIR.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sup, "_fork_workers", fake_fork)
        # 等心跳也桩掉：否则每个"worker"都要白等 15 秒超时
        monkeypatch.setattr(sup, "_await_heartbeats",
                            lambda procs, failed, *, count, since="": {
                                "ok": True, "pids": [t[0] for t in procs],
                                "failed": failed, "message": "stub"})
        # 先占到 7（上限 8）
        sup._FAKE_ALIVE = {p: True for p in range(1, 8)}
        for p in range(1, 8):
            (sup.PID_DIR / f"{p}.pid").write_text(str(p))
        monkeypatch.setattr(sup, "_pid_alive",
                            lambda pid: sup._FAKE_ALIVE.get(pid, False))

        import threading
        results = []
        def go():
            results.append(sup.start(count=1))
        ts = [threading.Thread(target=go) for _ in range(2)]
        for t in ts: t.start()
        for t in ts: t.join()

        # **先确认两条线程真的跑完了**。少了这一句，本条是假绿的：
        # 2026-11-10 给 `start()` 加必填的 `since` 时，桩函数没返回它，
        # 两条线程都 KeyError 死在里面 —— `results` 是空的，于是
        # `total <= MAX_WORKERS` 与 `sum(ok) <= 1` 都平凡成立，测试照样绿。
        # 线程里的异常默认只成一条 warning，不失败；断言必须自己站住。
        assert len(results) == 2, (
            f"两次 start 只回来了 {len(results)} 个：线程里的异常被吞成 "
            f"warning 了，下面那两条上限断言会在空结果上假绿")
        assert not any(t.is_alive() for t in ts)

        total = sup.current()["count"]
        assert total <= sup.MAX_WORKERS, (
            f"并发 start 突破了上限：{total} > {sup.MAX_WORKERS}"
            f"（forked={forked}）")
        assert sum(1 for r in results if r["ok"]) <= 1, "两个并发都报成功"

    def test_限额判断本身要被抓到_边界不放水(self, env, monkeypatch):
        """**在 `_fork_workers` 内部下桩**，让真代码那句限额判断自己跑（2026-11-10 自查）。

        为什么非要有这一条：上面 `test_并发_start_不会突破上限` 把整个
        `_fork_workers` 换成了假函数，假函数里**自己重写了一遍**限额判断。
        于是它证明的是"`start()` 的锁是有效的"，而不是"真代码的上限算得对"。
        实测把真代码的 `already + count > MAX_WORKERS` 放水成 `> MAX_WORKERS + 1`，
        整个 `test_worker_fleet.py` **34 条全绿**——上限逻辑实际无人看守。
        `test_超上限要拒` 也抓不到：它用 `count=99` 远超上限，放水 1 个仍然超。

        做法：只把"真去 fork"这一步（`subprocess.Popen`）换成抛错，
        限额判断与 `current()` 全走真代码。到上限时应当在碰 Popen 之前就被拒；
        若放水了就会走到 Popen，被这里抓住。
        """
        sup = env["sup"]
        monkeypatch.setattr(sup, "_pid_alive", lambda pid: True)

        def boom(*a, **kw):
            raise AssertionError(
                "已经到上限了还走到 fork——限额判断放水了（MAX_WORKERS 形同虚设）")
        monkeypatch.setattr(sup.subprocess, "Popen", boom)

        sup.PID_DIR.mkdir(parents=True, exist_ok=True)
        for pid in range(1, sup.MAX_WORKERS + 1):
            (sup.PID_DIR / f"{pid}.pid").write_text(str(pid))
        assert sup.current()["count"] == sup.MAX_WORKERS

        result = sup.start(count=1)
        assert result["ok"] is False, (
            f"已有 {sup.MAX_WORKERS} 个还允许再起 1 个，"
            f"真起第 {sup.MAX_WORKERS + 1} 个会 OOM")
        assert "上限" in result["message"]


class TestReadLoop:
    """已读闭环：`mark_all_read` 必须真的标完**全部**可见未读。

    原实现是 `inbox(user, limit=100, unread_only=True)` 再逐条 mark——
    而 `inbox` 的 `limit` 被钳在 100，所以未读超过 100 条时只标掉前 100，
    剩下的永久留着。界面按钮叫「全部已读」，点完角标还在，人会以为按钮坏了。
    实测：150 条未读 → 标掉 100 → 还剩 50。
    """

    def _seed(self, uid: int, n: int, audience: str | None = None) -> None:
        from db import get_conn
        with get_conn() as conn:
            conn.execute("DELETE FROM notification")
            for i in range(n):
                conn.execute(
                    """INSERT INTO notification (kind, level, title, audience)
                       VALUES ('job_failed','warn',?,?)""",
                    (f"n{i}", audience or f"user:{uid}"))

    def test_超过一百条也全部标完(self, env, uniq):
        import notifications
        uid = uniq
        _ensure_user(uid)
        self._seed(uid, 150)
        user = {"id": uid, "username": f"u{uid}", "is_superuser": 0}

        assert notifications.inbox(user)["unread"] == 150
        marked = notifications.mark_all_read(user)
        assert marked == 150, f"该标 150 条，实标 {marked}"
        assert notifications.inbox(user)["unread"] == 0, "还剩未读，角标清不掉"

    def test_重复点全部已读是幂等的(self, env, uniq):
        import notifications
        uid = uniq
        _ensure_user(uid)
        self._seed(uid, 5)
        user = {"id": uid, "username": f"u{uid}", "is_superuser": 0}
        assert notifications.mark_all_read(user) == 5
        assert notifications.mark_all_read(user) == 0, "第二次不该再标"
        assert notifications.inbox(user)["unread"] == 0

    def test_看不见的通知不会被标掉(self, env, uniq):
        """范围必须与 `inbox` 一致：别人的通知不能被"全部已读"顺手标掉，
        否则那个人本该看到的未读会消失。"""
        import notifications
        from db import get_conn
        uid = uniq
        _ensure_user(uid)
        with get_conn() as conn:
            conn.execute("DELETE FROM notification")
            conn.execute(
                """INSERT INTO notification (kind, level, title, audience)
                   VALUES ('job_failed','warn','我的',?)""", (f"user:{uid}",))
            conn.execute(
                """INSERT INTO notification (kind, level, title, audience)
                   VALUES ('job_failed','warn','别人的','user:999999')""")
        user = {"id": uid, "username": f"u{uid}", "is_superuser": 0}
        assert notifications.mark_all_read(user) == 1
        assert notifications.inbox(user)["unread"] == 0
        # 别人那条仍然未读（用别人的身份看）
        other = {"id": 999999, "username": "other", "is_superuser": 0}
        assert notifications.inbox(other)["unread"] == 1, "把别人的通知标掉了"

    def test_不能盲标看不见的通知(self, env, uniq):
        """`mark_read` 必须校验可见性。

        原实现拿到 id 就直接插 `notification_read`，不校验——于是顺着 id
        遍历就能把**全站**通知标成已读（别人的项目、管理员类的）。

        最阴的后果：用户还看不到"项目 X 掉线"，却把它标成了已读；
        等他后来加入项目 X，那条通知带着"已读"出现、未读数不增加，
        **他永远不知道自己错过了什么**。
        """
        import notifications
        from db import get_conn
        uid = uniq
        other = uniq + 700000
        _ensure_user(uid)
        _ensure_user(other)
        proj = other + 1
        _insert_project(proj, other)      # 参数顺序是 (项目 id, 属主 id)
        with get_conn() as conn:
            conn.execute("DELETE FROM notification")
            conn.execute(
                """INSERT INTO notification (kind, level, title, audience)
                   VALUES ('worker_offline','warn','项目掉线',?)""",
                (f"project:{proj}",))
            nid = conn.execute("SELECT id FROM notification").fetchone()["id"]

        user = {"id": uid, "username": f"u{uid}", "is_superuser": 0}
        # 还不是成员 → 看不见 → 不许标
        assert notifications.inbox(user)["unread"] == 0
        assert notifications.mark_read(user, [nid]) == 0, "盲标不可见的通知成功了"

        # 加入项目后必须仍是未读——这是这个 bug 最要命的地方
        from db import get_conn as gc
        with gc() as conn:
            conn.execute(
                "INSERT INTO project_member (project_id, user_id) VALUES (?,?)",
                (proj, uid))
        assert notifications.inbox(user)["unread"] == 1, (
            "加入项目后那条通知已经变成已读——用户会永久错过它")
        assert notifications.mark_read(user, [nid]) == 1, "可见后应该能标"
        assert notifications.mark_read(user, [nid]) == 0, "重复标不该计数"


class TestAwaitHeartbeats:
    """`_await_heartbeats` 不能把"已经 fork 出来的 worker"从报告里弄丢。

    2026-11-10 审计修的真 bug。原来循环末尾是 `if not ok: break`，注释写
    "一个起不来就停手：环境问题多半对下一个也一样"——这句把时序搞反了：
    fork 全部在锁内的 `_fork_workers()` 做完（每个都已登记 `_LAUNCHED` +
    写 PID 文件），`break` 中断的是**确认**而不是**启动**。于是第 1 个失败后，
    第 2..N 个明明活着、心跳也写了，却既不进 pids 也不进 failed：报告查无此人。
    操作者看到只提第 1 个的 400 → 再点启动 → 又 fork 一批，无人认领的 worker
    一路累积到 MAX_WORKERS，而 `worker_daemon` 下一拍用 `current()["count"]`
    数得到它们，面板于是显示"在跑"，启动却从没确认过。
    """

    def _procs(self, sup, monkeypatch, heartbeat_by_pid, dead=()):
        """装好最小假环境并交出 `procs` 三元组；判定全走真代码。

        `heartbeat_by_pid` 的 True/False = 这个 pid 有没有写心跳；
        `dead` 里的 pid 其 proc.poll() 返回退出码 2（＝fork 之后秒退）。
        2026-11-10 起 `procs` 是 `(pid, 序号, proc)`：proc 对象由
        `_fork_workers` 直接带出来，确认阶段不再查 `_LAUNCHED`。
        """
        class FakeProc:
            def __init__(self, alive=True, code=None):
                self._alive, self.returncode = alive, code

            def poll(self):
                return None if self._alive else self.returncode

        sup._LAUNCHED.clear()
        procs = []
        for i, pid in enumerate(heartbeat_by_pid):
            proc = FakeProc(alive=pid not in dead,
                            code=None if pid not in dead else 2)
            sup._LAUNCHED[pid] = proc
            procs.append((pid, i, proc))
        monkeypatch.setattr(sup, "START_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(sup, "_heartbeat_written_since",
                            lambda pid, *, since: bool(heartbeat_by_pid.get(pid)))
        monkeypatch.setattr(sup, "_log_tail", lambda: "")
        return procs

    def test_第一个没心跳时后面起好的必须仍然报出来(self, env, monkeypatch):
        p1, p2 = 111111, 222222
        # 第 1 个永不写心跳；第 2 个写了
        sup = env["sup"]
        procs = self._procs(sup, monkeypatch, {p1: False, p2: True})

        out = sup._await_heartbeats(procs, [], count=2, since="")

        assert out["ok"] is False, "第 1 个没起来，整体不该报成功"
        assert out["pids"] == [p2], (
            f"第 2 个已 fork、心跳也写了，却没进 pids —— "
            f"它在报告里查无此人：{out}")
        assert "只起来 1 个" in out["message"], (
            f"报告没交代第 2 个去哪了：{out['message']}")

    def test_全部失败时逐个都要有交代(self, env, monkeypatch):
        """两个都没心跳 → failed 必须两条都在，而不是只报第一个就收工。"""
        p1, p2 = 311111, 322222
        sup = env["sup"]
        procs = self._procs(sup, monkeypatch, {p1: False, p2: False})

        out = sup._await_heartbeats(procs, [], count=2, since="")

        assert out["ok"] is False and out["pids"] == []
        assert len(out["failed"]) == 2, (
            f"两个都没心跳却只报了 {len(out['failed'])} 条：{out['failed']}")

    def test_立刻崩溃的也要逐个报出来(self, env, monkeypatch):
        """第 1 个秒退（fork 失败之外的另一种失败），第 2 个照样要被确认。"""
        p1, p2 = 411111, 422222
        sup = env["sup"]
        procs = self._procs(sup, monkeypatch, {p1: False, p2: True},
                            dead={p1})

        out = sup._await_heartbeats(procs, [], count=2, since="")

        assert "立刻退出" in out["failed"][0], out["failed"]
        assert out["pids"] == [p2], (
            f"第 1 个崩溃不该连累第 2 个被确认：{out}")
        sup._LAUNCHED.clear()

    def test_确认期间被并发收掉的进程仍要报退出码(self, env, monkeypatch):
        """`_await_heartbeats` 不能回头查 `_LAUNCHED`（2026-11-10 审计修的真 bug）。

        `_LOCK` 在 fork 结束就释放，等心跳在锁外。这中间任何一次并发的
        `current()`／守护线程一拍都会走 `_reap()`，把已退出的那条从
        `_LAUNCHED` 弹掉。旧代码是 `proc = _LAUNCHED.get(pid)`，于是：

        * `proc is None` → 「已退出」这条判据永不成立，报的是
          「起来了但 15 秒内没有写心跳；看 worker.log 尾部」——
          把人指向环境/迁移问题，而真相是进程 150 毫秒就退了、退出码 2；
        * `_remove_pid_file(pid)` 也不执行 → PID 文件永久留盘，
          `current()["stale_pids"]` 从此恒定多一个号，界面一直显示
          「未运行（残留 …）」，而且没有任何按钮能清它。

        这里把 proc 交到手上的同时**故意让 `_LAUNCHED` 是空的**，
        正是那个并发时序的事后状态。
        """
        sup = env["sup"]
        pid = 511111
        sup.PID_DIR.mkdir(parents=True, exist_ok=True)
        pid_file = sup.PID_DIR / f"{pid}.pid"
        pid_file.write_text(str(pid))

        dead = type("Dead", (), {"returncode": 2, "poll": lambda self: 2})()
        monkeypatch.setattr(sup, "START_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(sup, "_heartbeat_written_since",
                            lambda _p, *, since: False)
        monkeypatch.setattr(sup, "_log_tail", lambda: "")
        sup._LAUNCHED.clear()            # ← 模拟 _reap 已经把它收掉了

        out = sup._await_heartbeats([(pid, 0, dead)], [], count=1,
                                since="")

        assert len(out["failed"]) == 1, out
        assert "立刻退出（退出码 2）" in out["failed"][0], (
            f"报错了方向，人会被引去查环境：{out['failed']}")
        assert "没有写心跳" not in out["failed"][0]
        assert not pid_file.exists(), (
            f"PID 文件没删，会永久算成残留：{sorted(p.name for p in sup.PID_DIR.iterdir())}")
        sup._LAUNCHED.clear()


class TestForkFailureLeavesNoOrphan:
    """fork 成功了但登记失败 —— 绝不能留下一个界面上停不掉的野进程。

    2026-11-10 整库审计实测到的真 bug（`worker_supervisor._fork_workers`）。
    原来 `_write_pid_file(proc.pid)` 在 `try` 之外，而它内部
    `PID_DIR.mkdir(...)` + `path.write_text(...)` 没有任何保护。触发条件是
    `data/run/workers` 不可写／被同名普通文件占住／磁盘满。一条链四件事：

    1. 异常穿透 `_fork_workers` → 穿透 `start()`（`with _LOCK` 不吞异常）
       → 路由没有 try/except → **HTTP 500**，而子进程**已经真起来了**；
    2. 它不在 `list_workers()` 里（没有登记文件），`current()["count"]=0`，
       `stop_one` 以"不是本机登记的 worker"拒绝操作，`stop()` 报
       「没有 worker 在跑」→ **界面上没有任何入口能停掉它**；
    3. `MAX_WORKERS=8` 数的是 `current()["count"]` → 防呆对它完全不设防，
       每点一次启动多一个；
    4. 守护每 15 秒重复踩同一个异常，而 `_loop` 的 `except` 原来
       **只写 last_error、不计 failures** → 永远到不了 `MAX_FAILURES`，
       「放弃自动拉起」那条通知永远不发，`exposed=False`，
       界面渲染成绿色的「守护中 · 累计已补 0 次」。
    """

    def _sleep_proc(self):
        """真 Popen 一个会活着的假 worker（要的是真 pid 和真 kill）。"""
        import subprocess
        return subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"])

    def test_登记失败必须杀掉刚起的进程并如实报失败(self, env, monkeypatch):
        sup = env["sup"]
        proc = self._sleep_proc()
        monkeypatch.setattr(sup.subprocess, "Popen", lambda *a, **k: proc)
        # 用普通文件占住 PID_DIR：`PID_DIR.mkdir(exist_ok=True)` 会抛
        # FileExistsError（OSError 的子类）。fixture 里 PID_DIR 就是
        # `RUN_DIR/"workers"`，所以这里占的正是它。
        sup.RUN_DIR.mkdir(parents=True, exist_ok=True)
        sup.PID_DIR.write_text("我不是目录")

        out = sup.start(count=1)          # 关键：**不能抛异常**

        assert out["ok"] is False
        assert out["failed"], "登记失败必须写进 failed，不能只丢一句 500"
        assert "登记失败" in out["failed"][0], out["failed"]
        # 真的等它死（`kill()` 是异步的，立刻 poll 只会看到 None）。
        # 等不到就是**没杀**：这条正是那个孤儿进程的本体。
        import subprocess as sp
        try:
            proc.wait(timeout=5)
        except sp.TimeoutExpired:
            proc.kill(); proc.wait()
            pytest.fail(
                f"PID {proc.pid} 没被杀掉 —— 它不在 _LAUNCHED、没有 PID 文件，"
                f"`stop_one` 会拒绝操作、`stop()` 报『没有 worker 在跑』，"
                f"界面上没有任何入口能停掉它：{out}")

    def test_目录准备失败要给出原因而不是穿透成_500(self, env, monkeypatch):
        sup = env["sup"]
        sup.RUN_DIR.mkdir(parents=True, exist_ok=True)
        (sup.LOG_DIR.parent / "logs").write_text("我不是目录")
        called = []
        monkeypatch.setattr(sup.subprocess, "Popen",
                            lambda *a, **k: called.append(1))

        out = sup.start(count=1)

        assert out["ok"] is False and "准备失败" in out["message"], out
        assert not called, "目录都没准备好就不该 fork 任何进程"

    def test_日志打不开时不fork并报出路径(self, env, monkeypatch):
        """`open(WORKER_LOG, "ab")` 也得包起来——它和 `Popen` 一样会抛 OSError。

        这一条刻意**只**把 `WORKER_LOG` 造成目录（`LOG_DIR` 本身完好），
        否则先撞上上面那条"目录准备失败"，测不到这里。
        判据两条：一条进程都没 fork；失败信息里带得出问题的文件路径。
        """
        sup = env["sup"]
        sup.LOG_DIR.mkdir(parents=True, exist_ok=True)
        sup.WORKER_LOG.mkdir()                 # 目录不能以 "ab" 打开
        called = []
        monkeypatch.setattr(sup.subprocess, "Popen",
                            lambda *a, **k: called.append(1))

        out = sup.start(count=1)

        assert out["ok"] is False
        assert "日志打不开" in out["message"], out["message"]
        assert str(sup.WORKER_LOG) in out["message"], (
            f"报错没说是哪个文件打不开，人还得自己猜：{out['message']}")
        assert not called, "日志都没有就不该 fork（否则 worker 输出无处可去）"

    def test_守护反复抛异常也必须升级成需要人处理(self, env, monkeypatch):
        """`_loop` 的 `except` 不记 failures，就永远走不到放弃那条。

        这一条测的是"线程不能死"和"必须记账"能不能同时成立。
        """
        daemon = env["daemon"]
        import threading
        stop = threading.Event()
        monkeypatch.setattr(daemon, "_STOP", stop)

        ticks = {"n": 0}
        def boom() -> None:
            ticks["n"] += 1
            raise PermissionError("run/workers 不可写")
        monkeypatch.setattr(daemon, "_tick", boom)
        monkeypatch.setattr(daemon, "_record",
                            lambda action, detail, *, level="warn":
                            daemon._STATE["actions"].insert(
                                0, {"at": "", "action": action, "detail": detail}))
        waits = {"n": 0}
        def fake_wait(_seconds):
            waits["n"] += 1
            if waits["n"] >= daemon.MAX_FAILURES:
                stop.set()
        monkeypatch.setattr(daemon, "CHECK_INTERVAL", 0.001)
        monkeypatch.setattr(daemon._STOP, "wait", fake_wait)

        daemon._STATE["failures"] = 0
        daemon._STATE["actions"] = []
        daemon._loop()

        assert ticks["n"] == daemon.MAX_FAILURES, (
            f"线程要么死了、要么没继续重试：{ticks}")
        assert daemon._STATE["failures"] >= daemon.MAX_FAILURES, (
            "异常不计 failures → 永远发不出『放弃自动拉起』，"
            "界面会一直绿色显示『守护中』")
        actions = [a["action"] for a in daemon._STATE["actions"]]
        assert "放弃自动拉起" in actions, (
            f"到了上限必须升级为需要人处理，实际只记了：{actions}")


class TestHeartbeatMustBeFresh:
    """PID 被重用时，上一任的死心跳不能冒充"新 worker 起来了"。

    2026-11-10 整库审计修的真 bug：`_heartbeat_written_since` 名字里有
    `since`，实现里**没有任何时间判据**（`SELECT 1 FROM worker_node
    WHERE pid = ?`），而 `worker_node` 的行 7 天才清（`lease.py:233`）。
    长跑的 Linux 机默认 `pid_max=32768`，几天绕一圈，重用完全够撞。
    实测后果：插一行三天前的心跳，`_await_heartbeats` **0.00 秒**返回
    `ok=True`，而那个新进程一秒都没活过。谎报成功比报失败更坏——
    操作者以为在跑，守护下一拍数出 `online >= desired` 于是**连补都不补**，
    队列原地不动，而且没有任何地方显示异常。

    这一组**故意不桩 `_heartbeat_written_since`**：桩掉它就桩掉了本条
    唯一被测的东西（那句 SQL 的时间过滤）。
    """

    PID = 987654          # 不会真存在的 PID，纯当键用

    def _seed(self, *, age_days):
        from db import get_conn
        with get_conn() as conn:
            conn.execute("DELETE FROM worker_node WHERE pid = ?", (self.PID,))
            conn.execute(
                """INSERT INTO worker_node (worker_id, pid, host, started_at,
                                            heartbeat_at, version,
                                            limits_json, resources_json)
                   VALUES (?, ?, 'testhost',
                           datetime('now','localtime', ?),
                           datetime('now','localtime', ?),
                           'v1', '{}', '{}')""",
                (f"testhost:{self.PID}:stale", self.PID,
                 f"-{age_days} days", f"-{age_days} days"))

    def teardown_method(self):
        from db import get_conn
        with get_conn() as conn:
            conn.execute("DELETE FROM worker_node WHERE pid = ?", (self.PID,))

    def test_旧心跳不算本轮确认(self, env):
        sup = env["sup"]
        self._seed(age_days=3)
        since = time.strftime("%Y-%m-%d %H:%M:%S")
        assert sup._heartbeat_written_since(self.PID, since=since) is False, (
            "三天前的记录被当成'这个 PID 写过心跳'——PID 重用即谎报成功")

    def test_新心跳算确认(self, env):
        """配对用例：判据不是"一律不认"，窗口之内必须认，否则永远确认不了。"""
        sup = env["sup"]
        self._seed(age_days=0)      # heartbeat_at = 现在
        since = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(time.time() - 60))
        assert sup._heartbeat_written_since(self.PID, since=since) is True

    def test_since_是必填的(self, env):
        """不给"不过滤"的缺省值——忘了过滤就是上面那个 bug 本体。

        留一个默认＝把门开着，下一个人加调用点时不会想到要传。
        """
        sup = env["sup"]
        with pytest.raises(TypeError):
            sup._heartbeat_written_since(self.PID)

    def test_确认用的是传进来的基线而不是当下时间(self, env, monkeypatch):
        """`_await_heartbeats` 必须用 `_fork_workers` 交出来的那个基线。

        基线的正确取值点是**第一个 fork 之前**（见 `worker_supervisor` 里
        那段注释：先起的 worker 可能在 `_fork_workers` 返回后就写好了心跳，
        基线取晚会把真活着的它误判成死的）。"取点"本身这里测不了——
        `heartbeat_at` 是秒级串，两次 `strftime` 只差十几毫秒，
        任何断言都无法证伪，所以那条只写在注释里，不虚报成"已覆盖"。

        能测的是**判据用的是参数**：给一个足够老的基线，三天前的记录
        就该被认；如果确认阶段自己 `strftime(now)` 把参数遮住，
        同一条记录立刻变成"没写过心跳"，这里就红。
        """
        sup = env["sup"]
        self._seed(age_days=3)
        monkeypatch.setattr(sup, "START_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr(sup, "_log_tail", lambda: "")

        out = sup._await_heartbeats([(self.PID, 0, None)], [], count=1,
                                    since="1970-01-01 00:00:00")

        assert out["ok"] is True, (
            f"传进来的老基线没生效，确认阶段用的是当下时间：{out['message']}")


class TestDaemonPartialSuccess:
    """守护不能把"这轮真起来了几个"算成一次纯失败（2026-11-10 审计）。

    `start()` 的契约写明「部分成功时 `pids` 给出真的那几个」，
    而失败分支原来一次都不读它。于是最常见的真实故障——期望 3 个、
    每轮只起来 2 个——三个数字同时说谎：`recovered` 不增、
    `failures` 照增到 3 就**永久放弃**（那 1 个缺口再也不补），
    面板还红着报「守护失效」，其实守护稳定维持着 desired-1 个。
    """

    def _drive(self, env, monkeypatch, *, started, message):
        daemon, sup = env["daemon"], env["sup"]
        result = {"ok": False, "pids": list(started),
                  "failed": [message], "message": message}
        monkeypatch.setattr(sup, "current",
                            lambda: {"count": len(started), "running": bool(started),
                                     "pids": list(started), "pid": None,
                                     "stale_pids": []})
        monkeypatch.setattr(sup, "start", lambda **kw: dict(result))
        daemon.save_config(enabled=True, desired=3)
        daemon._STATE.update(failures=0, recovered=0, actions=[],
                            last_error="", exposed=False)
        return daemon

    def test_部分补起要算进展不能推进放弃计数(self, env, monkeypatch):
        daemon = self._drive(env, monkeypatch, started=[11, 22],
                             message="只起来 2 个；第 3 个启动后立刻退出")
        for _ in range(daemon.MAX_FAILURES):
            daemon._tick()
        st = daemon._STATE
        assert st["recovered"] >= daemon.MAX_FAILURES * 2, (
            f"每轮真起来 2 个却一次都不计 recovered：{st['recovered']}")
        assert st["failures"] == 0, (
            f"有净增量还累计 failures，三轮就到上限、缺口再也不补：{st['failures']}")
        assert not any(a["action"] == "放弃自动拉起" for a in st["actions"]), st["actions"]
        assert any(a["action"] == "部分补起" for a in st["actions"]), st["actions"]

    def test_一个都没起来仍然要按连续失败计(self, env, monkeypatch):
        """配对用例：判据是"有没有净增量"，不是"ok 是不是假"。"""
        daemon = self._drive(env, monkeypatch, started=[],
                             message="一个都没起来")
        for _ in range(daemon.MAX_FAILURES):
            daemon._tick()
        st = daemon._STATE
        assert st["failures"] == daemon.MAX_FAILURES, st["failures"]
        assert st["recovered"] == 0
        assert any(a["action"] == "放弃自动拉起" for a in st["actions"]), st["actions"]

    def test_成功记录不该按_warn_发出去(self, env, monkeypatch):
        """`_record` 原来靠 `action != "recovered"` 判 level，
        而全仓没有任何调用传 `"recovered"` → 连「已补起」都是 warn。"""
        daemon, sup = env["daemon"], env["sup"]
        monkeypatch.setattr(sup, "current",
                            lambda: {"count": 0, "running": False, "pids": [],
                                     "pid": None, "stale_pids": []})
        monkeypatch.setattr(sup, "start",
                            lambda **kw: {"ok": True, "pids": [77],
                                          "failed": [], "message": "已启动"})
        levels = {}
        import notifications
        real_emit = notifications.emit
        def spy(**kw):
            levels[kw["title"]] = kw["level"]
            return real_emit(**kw)
        monkeypatch.setattr(notifications, "emit", spy)
        daemon.save_config(enabled=True, desired=1)
        daemon._STATE.update(failures=0, recovered=0, actions=[])
        daemon._tick()
        assert levels.get("守护：已补起") == "info", (
            f"成功记录发成了需要人处理的级别：{levels}")
