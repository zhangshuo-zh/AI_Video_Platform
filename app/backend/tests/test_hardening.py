"""安全收口那一轮（v0.85）的四条回归。

这四件事的共性是"看着无害、其实是门没关"：定妆图不登录就能取、
顶栏状态把别人项目的作业明细摊给所有人、初始口令没改也能干活、
权限判定里一个拼错的字符串会静默放行。所以每条都测"越界的那一面"，
而不是只测正常路径能通。
"""
import uuid
from pathlib import Path
from datetime import datetime, timedelta

import pytest

from access import ensure
from db import get_conn
from fastapi import HTTPException
from repositories.jobs import jobs as job_repo
from repositories.users import sessions as session_repo
from repositories.users import users as urepo
from security import hash_password, new_session_token

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")


def _client_for(app, user_id: int):
    """给任意账号铸一个会话客户端（conftest 的角色表里没有这些临时账号）。"""
    from fastapi.testclient import TestClient
    import main
    token, digest = new_session_token()
    session_repo.create(digest, user_id,
                        (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))
    return TestClient(main.app, cookies={"fv_session": token})


def _upload_image(app, as_role, people, client, pid) -> str:
    """建一个资产并传一张图，返回库里存的相对路径。"""
    a = client.post(f"/api/projects/{pid}/assets",
                    json={"kind": "character", "name": f"图主_{uuid.uuid4().hex[:6]}",
                          "summary": "", "consistency_prompt": "", "aliases": [], "tags": []})
    assert a.status_code == 201, a.text
    aid = a.json()["id"]
    up = client.post(f"/api/assets/{aid}/images",
                     files={"file": ("x.png", PNG, "image/png")})
    assert up.status_code == 200, up.text
    return up.json()["ref_images"][-1]


def test_定妆图必须登录且看得见资产才能取(app, as_role, people, client):
    # 用这条测试自己的项目：seeded 是 session 级的，往里塞图会把
    # test_projects 里"定妆图计数为 0"那条断言弄成看执行顺序
    owner = as_role("editor")
    with get_conn() as conn:
        pid = conn.execute("INSERT INTO project (name, owner_id) VALUES ('图权限用', ?)",
                           (people["editor"]["id"],)).lastrowid
    rel = _upload_image(app, as_role, None, owner, pid)

    assert owner.get(f"/files/{rel}").status_code == 200, "本人取自己项目的图要能成"
    outsider = as_role("outsider")
    r = outsider.get(f"/files/{rel}")
    assert r.status_code == 404, f"看不见那个项目的人应 404，实得 {r.status_code}"
    assert client.get(f"/files/{rel}").status_code == 401, "匿名不能取图（原来是静态挂载）"


def test_定妆图路径不能上跳(app, as_role, people):
    owner = as_role("editor")
    with get_conn() as conn:
        pid = conn.execute("INSERT INTO project (name, owner_id) VALUES ('图穿越用', ?)",
                           (people["editor"]["id"],)).lastrowid
    rel = _upload_image(app, as_role, None, owner, pid)
    # 把合法路径换成"回到上层拿数据库文件"：必须 404，不能把 SQLite 文件发出去
    for bad in ("../platform.db", "..%2Fplatform.db", f"{rel}/../../etc/passwd"):
        r = owner.get(f"/files/{bad}")
        assert r.status_code in (404, 400), f"{bad} → {r.status_code}"


def test_全局池的图登录者都能取(app, as_role, people):
    """全局资产（project_id 为 NULL）人人可读，图也一样——但登录还是要。"""
    admin = as_role("admin")
    with get_conn() as conn:
        pid = conn.execute("SELECT id FROM project LIMIT 1").fetchone()["id"]
    a = admin.post(f"/api/projects/{pid}/assets",
                   json={"kind": "character", "name": f"全局图_{uuid.uuid4().hex[:6]}",
                         "summary": "", "consistency_prompt": "",
                         "aliases": [], "tags": [], "scope": "global"})
    assert a.status_code == 201, a.text
    assert a.json()["project_id"] is None, a.json()
    aid = a.json()["id"]
    up = admin.post(f"/api/assets/{aid}/images", files={"file": ("x.png", PNG, "image/png")})
    assert up.status_code == 200, up.text
    rel = up.json()["ref_images"][-1]
    assert as_role("viewer").get(f"/files/{rel}").status_code == 200


def test_初始口令没改时后端只放行改密那几条(app, people):
    """原来这条规则只在前端，拿到旧会话就能绕过闸门干活。"""
    uid = urepo.create(f"t_newpwd_{uuid.uuid4().hex[:6]}", "没改密的人",
                       hash_password("initial-pw"), role_codes=["editor"],
                       must_change_password=True)["id"]
    c = _client_for(app, uid)
    assert c.get("/api/auth/me").status_code == 200, "看自己、改密、登出这三条要放行"
    for path in ("/api/projects", "/api/worker/status"):
        r = c.get(path)
        assert r.status_code == 403, f"{path} 在改密前就该 403，实得 {r.status_code}"
        assert "口令" in r.json()["detail"], r.json()
    r = c.post("/api/auth/password", json={"old_password": "initial-pw",
                                           "new_password": "brand-new-pw"})
    assert r.status_code == 200, r.text
    # 改密会作废所有会话（含刚才那条），所以用新口令重新登一次再确认业务接口通了
    from fastapi.testclient import TestClient
    import main
    fresh = TestClient(main.app)
    lr = fresh.post("/api/auth/login",
                    json={"username": urepo.get_by_id(uid)["username"],
                          "password": "brand-new-pw"})
    assert lr.status_code == 200, lr.text
    assert fresh.get("/api/projects").status_code == 200, "改完口令就该能正常干活"


def test_未知权限级别不默认放行():
    """need 拼错时以前会一路走到函数末尾被放行——权限判定里最坏的失败方式。"""
    with get_conn() as conn:
        pid = conn.execute("SELECT id FROM project LIMIT 1").fetchone()
    if pid is None:
        pytest.skip("库里没有项目")
    with get_conn() as conn:
        row = conn.execute("SELECT owner_id FROM project WHERE id = ?", (pid["id"],)).fetchone()
        uid = row["owner_id"]
    user = urepo.get_by_id(uid)
    # 所有人自己：未知级别也要报错，不能"因为没匹配上就放过"
    with pytest.raises(HTTPException) as bad:
        ensure(user, pid["id"], "wriite")
    assert bad.value.status_code == 500, bad.value
    # 合法级别照常
    assert ensure(user, pid["id"], "read")["id"] == pid["id"]


def test_顶栏状态不摊别人项目的作业明细(app, as_role, people, seeded, fresh_project):
    """running_jobs 前端根本不读，却把 job id／供应商／模型发给每个人。"""
    mine = seeded["project_id"]
    theirs = fresh_project("admin")

    def running(project_id):
        return job_repo.create({
            "project_id": project_id, "episode_id": None, "shot_id": None,
            "capability": "video", "kind": "video_local", "execution_mode": "local",
            "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
            "prompt_snapshot": "可见性用", "params": {}, "input_refs": [],
            "created_by": people["editor"]["id"],
        })["id"]

    j_mine, j_theirs = running(mine), running(theirs)
    with get_conn() as conn:
        for jid in (j_mine, j_theirs):
            conn.execute(
                """UPDATE job SET status = 'running', started_at = datetime('now','localtime'),
                          lease_until = datetime('now','localtime','+60 seconds')
                    WHERE id = ?""", (jid,))

    body = as_role("editor").get("/api/worker/status").json()
    ids = {r["id"] for r in body["running_jobs"]}
    assert ids == {j_mine}, f"editor 只该看到自己项目的作业，实得 {ids}"
    assert "log_path" not in body["running_jobs"][0], "日志路径不再往外给"
    # 队列长度与待核对数不裁：那是共享机器的客观状态，不是项目内容
    assert body["queue"]["running"] == 2, body["queue"]
    admin_ids = {r["id"] for r in as_role("admin").get("/api/worker/status").json()["running_jobs"]}
    assert admin_ids == {j_mine, j_theirs}, admin_ids

    with get_conn() as conn:
        conn.execute("UPDATE job SET status = 'cancelled' WHERE id IN (?, ?)", (j_mine, j_theirs))


def test_worker_一轮撞库锁不关掉整条队列(monkeypatch, seeded):
    """`run_once` 与 `heartbeat` 原来没有 try，而这条链最容易撞 SQLITE_BUSY。

    实测路径：`advance_run` 在 `BEGIN IMMEDIATE` 事务里要跑 ffprobe 验成片
    （最长 30 秒），另一条连接按 db.py 的 timeout=5 等 5 秒就抛
    "database is locked"。`run.sh worker` 是 `exec` 起来的独立进程、没人重启它，
    一句锁冲突于是把整条队列关掉——界面只剩"worker 离线"，
    而在跑的那条作业下一轮被 recover_stale 收成 failed（云端可能已经计了费）。
    兜住的后果与"让它崩"相同（作业靠租约回收、下轮重来），区别只是队列不停。
    """
    import sqlite3

    import worker

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker, "run_once", boom)
    monkeypatch.setattr(worker.jobs, "heartbeat", lambda *a, **k: None)

    assert worker.main(["--once"]) == 0, "撞一次锁就把 worker 进程退出＝队列整个停摆"
    src = (Path(__file__).resolve().parents[1] / "worker.py").read_text()
    assert "did = run_once(wid, limits=limits)" in src
    assert '[作业] 这一轮跑砸了' in src, "退出路径又变回没有兜底的那一版了"


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/api/worker/process", None),
    ("POST", "/api/worker/process/start", {}),
    ("POST", "/api/worker/process/start-many", {"count": 2}),
    ("POST", "/api/worker/process/stop", None),
    ("POST", "/api/worker/process/stop-one", {"pid": 0}),
])
def test_worker进程接口不泄露宿主日志路径(as_role, monkeypatch, tmp_path, method, path, payload):
    import worker_supervisor as sup
    monkeypatch.setattr(sup, "WORKER_LOG", tmp_path / "private-host" / "worker.log")
    monkeypatch.setattr(sup, "current", lambda: {"running": False, "count": 0, "pids": [], "stale_pids": []})
    monkeypatch.setattr(sup, "start", lambda **kwargs: {"ok": True, "message": "started"})
    monkeypatch.setattr(sup, "stop", lambda **kwargs: {"ok": True, "messages": ["stopped"]})
    response = as_role("admin").request(method, path, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["log_path"] == "data/logs/worker.log"
    assert str(tmp_path) not in response.text


def test_并发配置局部修改与审计记录真实操作人(as_role):
    from repositories import worker_limit, config_log
    saved = worker_limit.get()
    try:
        client = as_role("admin")
        first = client.put("/api/worker/concurrency", json={"global_max": 2, "per": {"text": 1}, "acknowledge_risk": True})
        assert first.status_code == 200, first.text
        second = client.put("/api/worker/concurrency", json={"note": "只改备注", "acknowledge_risk": True})
        assert second.status_code == 200, second.text
        assert second.json()["global_max"] == 2 and second.json()["per"] == {"text": 1}
        records = config_log.of_subject("worker_limit", "singleton")
        assert len(records) >= 2
        assert records[0]["created_by"] == as_role.id("admin")
        assert second.json()["updated_by"] == as_role.id("admin")
    finally:
        if saved is None:
            worker_limit.forget()
        else:
            worker_limit.save(global_max=saved["global"], per=saved["per"],
                              updated_by=saved["updated_by"], note=saved["note"])


def test_worker进程卡片的日志尾巴脱敏且不倒序错乱(as_role, monkeypatch, tmp_path):
    """0049：卡片下半区显示 worker.log 尾巴，这块最容易把宿主路径漏出去。

    日志里天然有 traceback，traceback 里天然有 `File "/Users/某人/项目/..."`。
    这个接口以前只回一个固定的相对标签（`data/logs/worker.log`），所以安全；
    现在真的把日志文本往外给了，脱敏就从"顺手"变成"必须"——这条测试
    就是钉住它：写一条带绝对路径的假日志，接口回来的每行都不许含它。

    同时钉住顺序：接口与页面都按时间正序，保留多行堆栈上下文。
    """
    import worker_supervisor as sup
    host_path = str(tmp_path / "secret-host" / "worker.log")
    monkeypatch.setattr(sup, "WORKER_LOG", tmp_path / "secret-host" / "worker.log")
    monkeypatch.setattr(sup, "current", lambda: {"running": False, "count": 0,
                                                "pids": [], "stale_pids": []})
    log = tmp_path / "secret-host" / "worker.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "第一行：启动\n"
        f'  File "{host_path}", line 12, in main\n'
        f"Traceback 里还有 {tmp_path}/models/weights.safetensors\n"
        "最后一行：退出\n", encoding="utf-8")

    body = as_role("admin").get("/api/worker/process").json()
    tail = body["log_tail"]
    assert tail, "日志存在时应该读到尾巴"
    assert tail[-1] == "最后一行：退出", "接口保留正序"
    assert body["log_size"] > 0 and body["log_mtime"]
    blob = "\n".join(tail)
    for leak in (str(tmp_path), "secret-host", "models/weights"):
        assert leak not in blob, f"日志尾巴泄露了本机路径片段：{leak}"
    # 内容不能因为脱敏被整段删掉——那样卡片就成空白了
    assert any("启动" in ln for ln in tail)


def test_守护配置写入留痕且不启动真实线程(as_role, monkeypatch, tmp_path):
    import worker_daemon as daemon
    from repositories import config_log
    monkeypatch.setattr(daemon, "DAEMON_CONFIG", tmp_path / "daemon.json")
    monkeypatch.setattr(daemon, "start_thread", lambda: True)
    response = as_role("admin").put("/api/worker/daemon", json={"enabled": True, "desired": 2})
    assert response.status_code == 200, response.text
    records = config_log.of_subject("worker_daemon", "singleton")
    assert records and records[0]["created_by"] == as_role.id("admin")


@pytest.mark.parametrize("per", [{"text": -1}, {"text": 65}, {"not-a-class": 1}])
def test_进程启动拒绝非法资源类与上限(as_role, monkeypatch, per):
    import worker_supervisor as sup
    monkeypatch.setattr(sup, "start", lambda **kwargs: {"ok": True, "message": "stub"})
    response = as_role("admin").post("/api/worker/process/start-many", json={"per": per})
    assert response.status_code == 422, response.text


def test_并发局部配置修改不丢另一位管理员的字段(as_role, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from repositories import worker_limit
    import services
    from models import WorkerLimitIn
    from threading import Barrier, local
    import db
    barrier, seen = Barrier(2), local()
    original_get = worker_limit.get
    saved = original_get()
    def synchronized_get():
        row = original_get()
        if db._HELD.get() is None and not getattr(seen, "waited", False):
            seen.waited = True
            barrier.wait(timeout=5)
        return row
    try:
        worker_limit.save(global_max=2, per={"text": 1})
        payloads = [WorkerLimitIn(global_max=3, acknowledge_risk=True),
                    WorkerLimitIn(per={"image": 1}, acknowledge_risk=True)]
        with monkeypatch.context() as patch:
            patch.setattr(worker_limit, "get", synchronized_get)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(services.set_concurrency, payloads))
        assert len(results) == 2
        current = worker_limit.get()
        assert current["global"] == 3 and current["per"] == {"image": 1}
    finally:
        if saved is None:
            worker_limit.forget()
        else:
            worker_limit.save(global_max=saved["global"], per=saved["per"],
                              updated_by=saved["updated_by"], note=saved["note"])


def test_配置审计迁移保留旧记录与自增序号(tmp_path):
    import os
    import subprocess
    import sys
    code = '''
import sqlite3, migrate
from config import DB_PATH
original=migrate._discover
migrate._discover=lambda:[(v,p) for v,p in original() if v<=56]
migrate.migrate(verbose=False)
with sqlite3.connect(DB_PATH) as conn:
    conn.execute("INSERT INTO config_change_log(id,subject,subject_key,action,summary) VALUES(10,'model_policy','old','update','old record')")
    conn.execute("INSERT INTO config_change_log(id,subject,subject_key,action) VALUES(20,'model_policy','deleted','update')")
    conn.execute('DELETE FROM config_change_log WHERE id=20')
    before=conn.execute('SELECT * FROM config_change_log').fetchall()
migrate._discover=lambda:[(v,p) for v,p in original() if v<=57]
assert migrate.migrate(verbose=False)==1
assert migrate.migrate(verbose=False)==0
with sqlite3.connect(DB_PATH) as conn:
    assert conn.execute('SELECT * FROM config_change_log').fetchall()==before
    conn.execute("INSERT INTO config_change_log(subject,subject_key,action) VALUES('worker_daemon','singleton','update')")
    assert conn.execute("SELECT id FROM config_change_log WHERE subject='worker_daemon'").fetchone()[0]>20
    assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
'''
    env = {**os.environ, "FASTVIDEO_DB": str(tmp_path / "old.db")}
    result = subprocess.run([sys.executable, "-c", code], env=env,
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("path,payload", [
    ("/api/worker/process/stop", {}),
    ("/api/worker/process/stop-one", {"pid": 123}),
])
def test_停止可先暂停守护且保留旧契约(as_role, monkeypatch, tmp_path, path, payload):
    import worker_daemon as daemon
    import worker_supervisor as sup
    from repositories import config_log
    monkeypatch.setattr(daemon, "DAEMON_CONFIG", tmp_path / "daemon.json")
    monkeypatch.setattr(sup, "current", lambda: {"running": False, "count": 0, "pids": [], "stale_pids": []})
    observed = []
    monkeypatch.setattr(sup, "stop", lambda **kwargs: observed.append(daemon.load_config()["enabled"]) or {"ok": True, "messages": ["stopped"]})
    client = as_role("admin")
    daemon.save_config(enabled=True, desired=2)
    assert client.post(path, json=payload).status_code == 200
    assert observed == [True], "默认兼容旧调用，不改变守护"
    response = client.post(path + "?pause_daemon=true", json=payload)
    assert response.status_code == 200, response.text
    assert observed == [True, False]
    assert daemon.load_config() == {"enabled": False, "desired": 2}
    assert "守护已暂停" in response.json()["last_action"]
    assert any(row["summary"] == "停止前暂停守护" for row in config_log.of_subject("worker_daemon", "singleton"))


def test_暂停守护失败不得继续停止(as_role, monkeypatch):
    import worker_supervisor as sup
    def fail():
        raise OSError("private path")
    monkeypatch.setattr(sup, "pause_daemon_recovery", fail)
    monkeypatch.setattr(sup, "stop", lambda **kwargs: pytest.fail("暂停失败不能停止"))
    response = as_role("admin").post("/api/worker/process/stop?pause_daemon=true")
    assert response.status_code == 400
    assert "未执行停止" in response.text
    assert "private path" not in response.text


@pytest.mark.parametrize("mode,expected", [("missing", "missing"), ("empty", "empty"),
                                           ("content", "ok"), ("unreadable", "unreadable")])
def test_worker日志读取状态不猜启动历史(as_role, monkeypatch, tmp_path, mode, expected):
    import builtins
    import worker_supervisor as sup
    path = tmp_path / "worker.log"
    monkeypatch.setattr(sup, "WORKER_LOG", path)
    monkeypatch.setattr(sup, "current", lambda: {"count": 0})
    if mode != "missing":
        path.write_text("Traceback\n  source line\nRuntimeError: failed\n" if mode == "content" else "")
    if mode == "content":
        import io
        original = builtins.open
        content = path.read_bytes()
        class GrowingLog(io.BytesIO):
            def read(self, size=-1):
                assert 0 < size <= 16384, "日志可能在 stat 后持续增长，读取必须有字节上限"
                return super().read(size)
        def bounded(file, *args, **kwargs):
            if str(file) == str(path):
                return GrowingLog(content)
            return original(file, *args, **kwargs)
        monkeypatch.setattr(builtins, "open", bounded)
    if mode == "unreadable":
        original = builtins.open
        def denied(file, *args, **kwargs):
            if str(file) == str(path):
                raise PermissionError("private host directory")
            return original(file, *args, **kwargs)
        monkeypatch.setattr(builtins, "open", denied)
    response = as_role("admin").get("/api/worker/process")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["log_status"] == expected
    assert "private host directory" not in response.text
    if mode == "content":
        assert body["log_tail"] == ["Traceback", "  source line", "RuntimeError: failed"]
    else:
        assert body["log_tail"] == []
