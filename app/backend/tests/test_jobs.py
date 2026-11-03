"""W07：Job 队列语义、串行执行、崩溃恢复、取消/重试与权限。

验收的四条硬指标在这里各有反证：
  * 同一个幂等键不会重复执行；
  * 一个本地任务在跑时不会领第二条；
  * 杀掉 worker 后作业不会永远挂在 running；
  * 重试新建一行，原行的日志/耗时/错误一个字段都不动。
"""
import threading
import time

import pytest

import adapters
import jobfiles
import services
import worker
from config import JOBS_DIR
from db import get_conn
from repositories.jobs import jobs


def _submit(c, project_id, **kw):
    body = {
        "project_id": project_id, "capability": "video", "prompt": "测试作业",
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.05},
    }
    body.update(kw)
    return c.post("/api/jobs", json=body)


def _wait(pred, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _put_video(c, local=None, cloud=None):
    """整批保存 provider 配置（PUT 是全量覆盖），只改视频那两行。"""
    rows = c.get("/api/providers").json()
    body = []
    for r in rows:
        item = {k: r[k] for k in ("id", "provider", "model", "enabled", "is_default")}
        item["key_env"] = r["key_env_custom"]
        item["notes"] = r["notes"]
        if r["capability"] == "video" and r["backend"] == "local" and local:
            item.update(local)
        if r["capability"] == "video" and r["backend"] == "cloud" and cloud:
            item.update(cloud)
        body.append(item)
    resp = c.put("/api/providers", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_管理员不带任何筛选也能列作业(as_role, seeded):
    """管理员看全部时 project_ids=None、状态/能力也都没选，条件是空的。
    W08 真浏览器第一轮就在这里撞到 "near ORDER: syntax error"。"""
    r = as_role("admin").get("/api/jobs")
    assert r.status_code == 200, r.text
    assert any(row["id"] == seeded["job_id"] for row in r.json())
    assert as_role("admin").get("/api/jobs?status=queued,running").status_code == 200


def test_提交立刻返回_202_并冻结快照(as_role, clean_queue, seeded):
    c = as_role("editor")
    r = _submit(c, seeded["project_id"], episode_id=seeded["episode_id"],
                shot_id=seeded["shot_id"], prompt="汴京的夜")
    assert r.status_code == 202, r.text
    data = r.json()
    assert data["status"] == "queued"
    assert data["kind"] == "video_local"
    assert data["execution_mode_label"] == "本地"

    detail = c.get(f"/api/jobs/{data['id']}").json()
    assert detail["prompt_snapshot"] == "汴京的夜"
    assert detail["params"] == {"sleep": 0.05}
    assert detail["adapter_version"] == "mock"
    # 改了原参数对象也不该影响已冻结的 Job
    detail["params"]["sleep"] = 99
    assert c.get(f"/api/jobs/{data['id']}").json()["params"] == {"sleep": 0.05}


def test_同一个幂等键只执行一次(as_role, clean_queue, seeded):
    c = as_role("editor")
    key = "idem-w07-001"
    first = _submit(c, seeded["project_id"], idempotency_key=key)
    second = _submit(c, seeded["project_id"], idempotency_key=key)
    assert first.status_code == 202
    assert second.status_code == 200          # 已存在：200，不再建第二行
    assert first.json()["id"] == second.json()["id"]
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM job WHERE idempotency_key = ?",
                         (key,)).fetchone()["n"]
    assert n == 1

    worker.run_once("w-idem")
    assert jobs.get(first.json()["id"])["status"] == "succeeded"
    # 再提交一次仍是同一条成功记录，不会再跑一遍
    third = _submit(c, seeded["project_id"], idempotency_key=key)
    assert third.status_code == 200
    assert third.json()["status"] == "succeeded"


def test_幂等键被别的项目占用时不泄露(as_role, clean_queue, seeded, fresh_project):
    owner = as_role("editor")
    key = "idem-w07-cross"
    _submit(owner, seeded["project_id"], idempotency_key=key)

    other_project = fresh_project("outsider")
    other = as_role("outsider")
    r = _submit(other, other_project, idempotency_key=key)
    assert r.status_code == 409
    assert "占用" in r.json()["detail"]
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM job WHERE idempotency_key = ?",
                         (key,)).fetchone()["n"]
    assert n == 1


def test_本地串行_跑着的时候不领第二条(as_role, clean_queue, seeded):
    c = as_role("editor")
    a = _submit(c, seeded["project_id"]).json()
    b = _submit(c, seeded["project_id"]).json()

    first = jobs.claim_next("w-serial-1", 60)
    assert first is not None and first["id"] == a["id"]
    # 第一条租约还在，第二个 worker 也领不到——这是"同时最多一个本地任务"的闸
    assert jobs.claim_next("w-serial-2", 60) is None

    worker.process(first, "w-serial-1")
    assert jobs.get(a["id"])["status"] == "succeeded"

    second = jobs.claim_next("w-serial-2", 60)
    assert second is not None and second["id"] == b["id"]
    worker.process(second, "w-serial-2")
    assert jobs.get(b["id"])["status"] == "succeeded"


def test_worker_消失后作业不永挂_可重试(as_role, clean_queue, seeded):
    c = as_role("editor")
    job = _submit(c, seeded["project_id"]).json()
    claimed = jobs.claim_next("w-dead", 60)
    assert claimed is not None and claimed["id"] == job["id"]

    with get_conn() as conn:
        conn.execute("""UPDATE job SET lease_until = datetime('now','localtime','-1 seconds')
                         WHERE id = ?""", (job["id"],))
    recovered = jobs.recover_stale()
    assert recovered["failed"] >= 1

    row = jobs.get(job["id"])
    assert row["status"] == "failed"
    assert row["error_code"] == "worker_lost"
    assert "可以重试" in row["error_message"]

    retry = c.post(f"/api/jobs/{job['id']}/retry")
    assert retry.status_code == 202, retry.text
    new = retry.json()
    assert new["id"] != job["id"]
    assert new["retry_of_job_id"] == job["id"]
    assert new["status"] == "queued"
    # 原行不动
    assert jobs.get(job["id"])["status"] == "failed"
    assert jobs.get(job["id"])["error_code"] == "worker_lost"


def test_恢复不动租约未过期的活(as_role, clean_queue, seeded):
    """周期恢复是个双刃剑：活着的 worker 每 0.2 秒续租，不能被当成"死了"。"""
    c = as_role("editor")
    job = _submit(c, seeded["project_id"]).json()
    claimed = jobs.claim_next("w-alive", 60)
    assert claimed is not None and claimed["id"] == job["id"]
    jobs.recover_stale()
    assert jobs.get(job["id"])["status"] == "starting"


def test_别人的_worker_id_改不动一条作业的终态(as_role, clean_queue, seeded):
    """审计 R1：`cancelled()` 原来只按 id 写，谁拿着一个不相干的 worker_id
    都能覆盖"谁收的尾、跑了多久"这三样。补上 `AND worker_id = ?` 之后，
    四条终态写入（succeed／fail／needs_attention／cancelled）用同一条规则。

    反证就是这条用例本身：把守卫去掉，第一段断言会当场变红。
    """
    c = as_role("editor")
    job = _submit(c, seeded["project_id"]).json()
    claimed = jobs.claim_next("w-owner", 60)
    assert claimed is not None and claimed["id"] == job["id"]

    # 冒名者：id 对、worker 不对 → 一个字段都不该动
    jobs.cancelled(job["id"], "w-imposter", "别人来收尾")
    row = jobs.get(job["id"])
    assert row["status"] == "starting"
    assert row["worker_id"] == "w-owner"
    assert not row["error_message"]

    # 正主：同一行改得动
    jobs.cancelled(job["id"], "w-owner", "本人收尾")
    row = jobs.get(job["id"])
    assert row["status"] == "cancelled"
    assert row["error_message"] == "本人收尾"
    assert row["worker_id"] is None


def test_取消排队中的作业(as_role, clean_queue, seeded):
    c = as_role("editor")
    job = _submit(c, seeded["project_id"], params={"sleep": 2}).json()
    r = c.post(f"/api/jobs/{job['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert jobs.get(job["id"])["finished_at"]
    # 已经被取消的不会再被 worker 领走
    assert worker.run_once("w-after-cancel") is None


def test_取消运行中的作业(as_role, clean_queue, seeded):
    c = as_role("editor")
    job = _submit(c, seeded["project_id"], params={"sleep": 1.2}).json()
    thread = threading.Thread(target=lambda: worker.run_once("w-cancel"))
    thread.start()
    assert _wait(lambda: jobs.get(job["id"])["status"] == "running")
    r = c.post(f"/api/jobs/{job['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancel_requested"
    thread.join(timeout=8)
    row = jobs.get(job["id"])
    assert row["status"] == "cancelled", row["status"]
    logs = c.get(f"/api/jobs/{job['id']}/logs").json()
    assert "取消" in logs["text"]


def test_成功作业写日志与相对路径产物(as_role, clean_queue, seeded):
    c = as_role("editor")
    job = _submit(c, seeded["project_id"], params={"sleep": 0.05, "text": "hello mock"},
                  prompt="写一行就好").json()
    wid = worker.run_once("w-ok")
    assert wid == job["id"]

    detail = c.get(f"/api/jobs/{job['id']}").json()
    assert detail["status"] == "succeeded"
    assert detail["elapsed_ms"] is not None and detail["elapsed_ms"] >= 0
    assert detail["return_code"] == 0
    assert detail["log_path"].startswith(f"{job['id']}/logs/")
    assert not detail["log_path"].startswith("/")

    logs = c.get(f"/api/jobs/{job['id']}/logs").json()
    assert "succeeded" in logs["text"]
    assert str(JOBS_DIR) not in logs["text"]

    arts = c.get(f"/api/jobs/{job['id']}/artifacts").json()
    assert len(arts) == 1 and arts[0]["exists"] and arts[0]["size"] > 0
    assert arts[0]["path"].startswith(f"{job['id']}/outputs/")
    assert (JOBS_DIR / arts[0]["path"]).is_file()


def test_假适配器只在允许时可用(as_role, clean_queue, seeded, monkeypatch):
    c = as_role("editor")
    monkeypatch.setattr("services.ALLOW_MOCK", False)
    r = _submit(c, seeded["project_id"])
    assert r.status_code == 422
    assert "ALLOW_MOCK" in r.json()["detail"]


def test_真适配器缺运行时明确失败(as_role, provider_state, clean_queue, seeded,
                                  monkeypatch):
    admin = as_role("admin")
    _put_video(admin, local={"provider": "fastmetal", "model": "fastmetal-5b-qad",
                             "enabled": True, "is_default": True})
    # 测试环境不碰真实权重：把 home 指到一个空目录，worker 应该在起进程前
    # 就以 adapter_unavailable 失败，并说清缺什么，而不是假装跑了模型。
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", "/nonexistent/fastvideo-w09-test")
    c = as_role("editor")
    # 不传执行方式/供应商/模型 ⇒ 走默认轨
    r = _submit(c, seeded["project_id"], provider_code="", model_code="",
                execution_mode=None, params={"width": 704, "height": 1280,
                                             "frames": 121})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["provider_code"] == "fastmetal"

    worker.run_once("w-real")
    row = jobs.get(job["id"])
    assert row["status"] == "failed"
    assert row["error_code"] == "adapter_unavailable"
    assert "FASTVIDEO_FASTMETAL_HOME" in row["error_message"]
    logs = c.get(f"/api/jobs/{job['id']}/logs").json()
    assert "adapter_unavailable" in logs["text"]


def test_参数白名单在提交时挡掉(as_role, provider_state, clean_queue, seeded):
    admin = as_role("admin")
    _put_video(admin, local={"provider": "fastmetal", "model": "fastmetal-5b-qad",
                             "enabled": True})
    c = as_role("editor")
    base = {"provider_code": "fastmetal", "model_code": "fastmetal-5b-qad",
            "execution_mode": "local"}

    r = _submit(c, seeded["project_id"], params={"bogus": 1}, **base)
    assert r.status_code == 422
    assert "白名单" in r.json()["detail"]

    r = _submit(c, seeded["project_id"], params={"width": 700}, **base)
    assert r.status_code == 422
    assert "倍数" in r.json()["detail"]

    r = _submit(c, seeded["project_id"], params={"width": 704, "height": 1280}, **base)
    assert r.status_code == 202, r.text


def test_云端视频必须显式确认(as_role, provider_state, clean_queue, seeded):
    admin = as_role("admin")
    _put_video(admin, cloud={"provider": "wan", "model": "wan2.7-t2v", "enabled": True})
    c = as_role("editor")
    base = {"provider_code": "wan", "model_code": "wan2.7-t2v",
            "execution_mode": "cloud", "params": {}}

    r = _submit(c, seeded["project_id"], **base)
    assert r.status_code == 422
    assert "确认" in r.json()["detail"]

    # W08 起，确认要与一张具体的计价单一起给：只有勾、没有量价，仍然不落库
    r = _submit(c, seeded["project_id"], user_confirmed_cloud=True, **base)
    assert r.status_code == 422
    assert "预计成本" in r.json()["detail"]

    base["estimate"] = {"quantity": 5, "unit": "second",
                        "unit_price_micros": 1_200_000,
                        "currency": "CNY", "source": "供应商官网 2026-11-01"}
    r = _submit(c, seeded["project_id"], user_confirmed_cloud=True, **base)
    assert r.status_code == 202, r.text
    assert r.json()["kind"] == "video_cloud"
    assert r.json()["cost_micros"] == 6_000_000
    assert r.json()["billable_status"] == "pending_reconciliation"


def test_集与镜头必须对得上(as_role, clean_queue, seeded, fresh_project):
    c = as_role("editor")
    other = fresh_project("editor")
    ep = c.post(f"/api/projects/{other}/episodes", json={"title": "别的一集"})
    assert ep.status_code == 201, ep.text
    r = _submit(c, seeded["project_id"], episode_id=ep.json()["id"])
    assert r.status_code == 422
    assert "不属于" in r.json()["detail"]

    # 别的项目里的镜头，挂到本项目上同样要拒
    shot = c.post(f"/api/episodes/{ep.json()['id']}/shots", json={"summary": "别处的镜头"})
    assert shot.status_code == 201, shot.text
    r = _submit(c, seeded["project_id"], shot_id=shot.json()["id"])
    assert r.status_code == 422
    assert "不属于" in r.json()["detail"]


def test_权限与归属(as_role, clean_queue, seeded):
    viewer = as_role("viewer")
    r = _submit(viewer, seeded["project_id"])
    assert r.status_code == 403                 # viewer 没有 job:submit
    # 有 job:read 也不等于看得见别人的项目：归属那一层回 404
    assert viewer.get(f"/api/jobs/{seeded['job_id']}").status_code == 404

    outsider = as_role("outsider")
    assert outsider.get(f"/api/jobs/{seeded['job_id']}").status_code == 404
    assert outsider.get("/api/jobs").json() == []


def test_worker_心跳决定在线(as_role):
    c = as_role("viewer")
    jobs.heartbeat("w-status", 123, None, "test")
    online = c.get("/api/worker/status").json()
    assert online["online"] is True
    assert isinstance(online["queue"], dict)

    with get_conn() as conn:
        conn.execute("""UPDATE worker_state SET heartbeat_at =
                        datetime('now','localtime','-1 hour') WHERE id = 1""")
    offline = c.get("/api/worker/status").json()
    assert offline["online"] is False
    assert offline["current_job_id"] is None


def test_删项目带走作业目录(as_role, clean_queue, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    job = _submit(c, pid).json()
    worker.run_once("w-clean")
    folder = JOBS_DIR / str(job["id"])
    assert folder.is_dir()

    services.delete_project_cascade(pid, "测试")
    assert jobs.get(job["id"]) is None
    assert not folder.exists()


def test_日志路径越界读不到():
    assert jobfiles.read_log("/etc/passwd") == ""
    assert jobfiles.read_log("../platform.db") == ""
    assert jobfiles.read_log(f"{JOBS_DIR}/../../x") == ""
