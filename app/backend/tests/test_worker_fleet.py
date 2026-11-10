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
                out.append((pid, i))
            return {"procs": out, "failed": []}

        sup.PID_DIR.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sup, "_fork_workers", fake_fork)
        # 等心跳也桩掉：否则每个"worker"都要白等 15 秒超时
        monkeypatch.setattr(sup, "_await_heartbeats",
                            lambda procs, failed, *, count: {
                                "ok": True, "pids": [p for p, _ in procs],
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

        total = sup.current()["count"]
        assert total <= sup.MAX_WORKERS, (
            f"并发 start 突破了上限：{total} > {sup.MAX_WORKERS}"
            f"（forked={forked}）")
        assert sum(1 for r in results if r["ok"]) <= 1, "两个并发都报成功"
