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
