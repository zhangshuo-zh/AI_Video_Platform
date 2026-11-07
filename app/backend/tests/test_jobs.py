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
from repositories.jobs import JobOwnershipLost, jobs


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
    W08 真浏览器第一轮就在这里撞到 "near ORDER: syntax error"。

    原来这一条还顺手断言"seeded 那条作业在返回里"——那靠的是"整个会话跑到这里
    时作业总数没超过一页"，用例一加多就假红（测的是用例顺序，不是产品）。
    现在把两件事分开：空条件能不能出 200 与一页数据（验那条 SQL 拼得对），
    管理员能不能看见别人项目里具体某条作业（验权限）——后者用 project_id 定位，
    不受分页影响。
    """
    admin = as_role("admin")
    r = admin.get("/api/jobs")
    assert r.status_code == 200, r.text
    assert r.json(), "会话跑到这里库里必然有作业；空列表说明列表本身坏了"
    pinned = admin.get(f"/api/jobs?project_id={seeded['project_id']}")
    assert pinned.status_code == 200, pinned.text
    assert any(row["id"] == seeded["job_id"] for row in pinned.json())
    assert admin.get("/api/jobs?status=queued,running").status_code == 200


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

    # 旧 worker 只是被拖过租约、其实还活着：回来写终态必须被明确拒绝，
    # 不能静默地把库里那条 failed 盖成 succeeded（日志与库会各说一套）。
    with pytest.raises(JobOwnershipLost):
        jobs.succeed(job["id"], "w-dead", "", [], 10)
    assert jobs.get(job["id"])["status"] == "failed"

    # retry_count 是仓储算出来的派生列，sqlite3.Row 没有 .get()：
    # 模型层按 keys() 取值，重试行挂回原行后这一格必须是 1，不能恒为 0。
    listed = c.get(f"/api/jobs?project_id={seeded['project_id']}").json()
    assert any(r["id"] == job["id"] and r["retry_count"] == 1 for r in listed)


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

    # 冒名者：id 对、worker 不对 → 一个字段都不该动，而且要明确报「归属丢了」，
    # 不许静默返回一句「写好了」骗上层（旧 worker 被租约收走时同理）。
    with pytest.raises(JobOwnershipLost):
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


def _ffmpeg_ok() -> bool:
    import subprocess
    from config import FFMPEG_BIN
    try:
        subprocess.run([FFMPEG_BIN, "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ffmpeg_ok(), reason="本机没有 ffmpeg")
def test_mock的媒体分支产出真能播的mp4(as_role, clean_queue, seeded):
    """六场景要把"生成→签字→合成"一次跑穿，产物必须是真媒体。

    断言不看文件大小这种弱证据，直接用 ffprobe 读时长与视频流——
    读得出来才算这条轨真的能喂给时间线。
    """
    import subprocess
    from config import FFPROBE_BIN
    c = as_role("editor")
    job = _submit(c, seeded["project_id"],
                  params={"sleep": 0.05, "media": "mp4", "color": "blue",
                          "seconds": 0.5}).json()
    assert worker.run_once("w-mp4") == job["id"]

    arts = c.get(f"/api/jobs/{job['id']}/artifacts").json()
    assert len(arts) == 1 and arts[0]["exists"]
    path = JOBS_DIR / arts[0]["path"]
    assert arts[0]["path"].endswith(".mp4")

    probe = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "640,360", probe.stdout
    dur = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    assert 0.2 <= float(dur.stdout.strip()) <= 1.0, dur.stdout


def test_mock的媒体类型不在枚举里就挡在提交时(as_role, clean_queue, seeded):
    c = as_role("editor")
    r = _submit(c, seeded["project_id"], params={"sleep": 0.05, "media": "gif"})
    assert r.status_code == 422
    assert "media" in r.json()["detail"]


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


def test_显式指定不再被槽位绑死_同能力第二档也能选(as_role, provider_state,
                                          clean_queue, seeded, register_tier):
    """2026-11-07 放开的那条：显式指定的判据从"与 provider_config 槽位逐字相等"
    改成"已登记 ∧ 已启用 ∧ 清单有适配器"。

    改之前，`provider_config` 是十行固定（5 类能力 × 本地／云端），一格只装一个
    provider，所以同能力永远只能选一档——想对比 mflux 与 Draw Things 就得改默认轨
    再改回来，而改默认轨是有后果的动作（`is_default` 是 `resolve_provider()`
    的唯一输入）。这条钉的是：**槽位决定"不说的时候走哪条"，不再兼任允许清单**。
    """
    admin = as_role("admin")
    # 槽位配成 1.3B：不说的时候走它
    _put_video(admin, local={"provider": "fastmetal", "model": "fastmetal-1.3b-qad",
                             "enabled": True, "is_default": True})
    # 5B 也登记并启用：它**不在槽位里**，但平台上真有这一档。
    # 这正是改之前做不到的那件事——同一 provider 的两档以前只能二选一，
    # 因为显式那一支要求"与 provider_config 槽位逐字相等"。
    register_tier("video", "local", "fastmetal", "fastmetal-5b-qad")

    c = as_role("editor")
    r = c.post("/api/jobs", json={
        "project_id": seeded["project_id"], "capability": "video",
        "prompt": "换 5B 那一档试试", "execution_mode": "local",
        "provider_code": "fastmetal", "model_code": "fastmetal-5b-qad",
        "params": {},
    })
    assert r.status_code == 202, r.text
    assert r.json()["provider_code"] == "fastmetal"
    assert r.json()["model_code"] == "fastmetal-5b-qad"

    # 而不传这三件时，仍然只走槽位那一条（默认轨的唯一来源没变）
    d = c.post("/api/jobs", json={
        "project_id": seeded["project_id"], "capability": "video",
        "prompt": "不指定就走默认", "params": {},
    })
    assert d.status_code == 202, d.text
    assert d.json()["model_code"] == "fastmetal-1.3b-qad"


def test_提交下拉能看见登记过的第二档(as_role, provider_state, clean_queue,
                                  seeded, register_tier):
    """放开了提交侧，界面也必须跟得上（`GET /api/providers/available`）。

    这条钉的是"放开只对 curl 有效"那种半成品：判据换了而候选清单没换，
    生成中心的下拉还是那十条，同能力第二档照样选不到。

    同时钉两条**不放松**的底线：清单里没有适配器的档不列（参数白名单是硬前提），
    停用的登记不列（人可以用它把某一档关掉而不撤登记）。
    """
    admin = as_role("admin")
    _put_video(admin, local={"provider": "fastmetal", "model": "fastmetal-1.3b-qad",
                             "enabled": True, "is_default": True})
    register_tier("video", "local", "fastmetal", "fastmetal-5b-qad")
    # 清单外 provider：登记了但平台没有它的参数白名单，不许列
    register_tier("image", "local", "no-such-provider", "x1")

    c = as_role("editor")
    out = c.get("/api/providers/available").json()
    video = [o for o in out if o["capability"] == "video" and o["backend"] == "local"]
    models = {o["model"] for o in video}
    assert "fastmetal-5b-qad" in models, f"第二档没进候选：{models}"
    assert "fastmetal-1.3b-qad" in models, "槽位那一档别被挤掉"
    # 清单外 provider 不许出现
    assert not [o for o in out if o["provider"] == "no-such-provider"], \
        "清单里没有适配器的登记不该进候选——平台不会替它猜参数"


def test_显式指定要求这一档真的登记过(as_role, provider_state, clean_queue, seeded):
    """放开的反面：**没登记过就还是一片 422**。

    判据换的是"允许清单"这一环，不是"能不能跑"这一环——登记行回答的正是
    "这台机器上真有这一档"，没有它平台不该替一个没登记的东西建作业。
    """
    c = as_role("editor")
    r = c.post("/api/jobs", json={
        "project_id": seeded["project_id"], "capability": "image",
        "prompt": "没登记也想过", "execution_mode": "local",
        "provider_code": "mflux", "model_code": "flux1-schnell", "params": {},
    })
    assert r.status_code == 422
    assert "没有登记" in r.json()["detail"], r.json()["detail"]


def test_参数白名单在提交时挡掉(as_role, provider_state, clean_queue, seeded,
                             register_tier):
    admin = as_role("admin")
    _put_video(admin, local={"provider": "fastmetal", "model": "fastmetal-5b-qad",
                             "enabled": True})
    # 显式指定要求这一档在模型仓库登记过（2026-11-07 起）
    register_tier("video", "local", "fastmetal", "fastmetal-5b-qad")
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


def test_云端视频必须显式确认(as_role, provider_state, clean_queue, seeded,
                             register_tier):
    admin = as_role("admin")
    _put_video(admin, cloud={"provider": "wan", "model": "wan2.7-t2v", "enabled": True})
    register_tier("video", "cloud", "wan", "wan2.7-t2v")
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
        # 阶段 E 起在线判定看 worker_node（每个进程一行）；单行镜像只作兼容
        conn.execute("""UPDATE worker_node SET heartbeat_at =
                        datetime('now','localtime','-1 hour')""")
    offline = c.get("/api/worker/status").json()
    assert offline["online"] is False
    assert offline["current_job_id"] is None


def test_删项目留下作业与产物目录(as_role, clean_queue, fresh_project):
    """0039：`job.project_id` 是 ON DELETE SET NULL——钱花了，账与文件都要留着。

    原来这里断言的是相反的行为（作业行随项目 CASCADE 消失、目录被摘掉），
    而《系统使用说明书》§2.5 与 §3.2 承诺的是"作业与产物不会被删"。
    两边必须有一个让步，让步的是代码：删项目是既有流程里最重的动作，
    一次误删不该把成本账与产物路径一起抹掉。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    job = _submit(c, pid).json()
    worker.run_once("w-clean")
    folder = JOBS_DIR / str(job["id"])
    assert folder.is_dir()

    services.delete_project_cascade(pid, "测试")

    row = jobs.get(job["id"])
    assert row is not None                      # 行还在
    assert row["project_id"] is None            # 只是不再属于这个项目
    assert folder.is_dir()                      # 日志与产物目录一个字节都没动

    # 无主作业只归管理员：本人（项目已没了）与外人都拿 404，不泄露"这行存在"
    assert as_role("editor").get(f"/api/jobs/{job['id']}").status_code == 404
    assert as_role("outsider").get(f"/api/jobs/{job['id']}").status_code == 404
    admin = as_role("admin").get(f"/api/jobs/{job['id']}")
    assert admin.status_code == 200
    assert admin.json()["project_id"] is None

    # 管理员的「全部项目」视图能列出它；别人的视图按可见项目筛，自然筛不到
    assert job["id"] in [j["id"] for j in as_role("admin").get("/api/jobs").json()]
    assert job["id"] not in [j["id"] for j in as_role("outsider").get("/api/jobs").json()]


def test_日志路径越界读不到():
    assert jobfiles.read_log("/etc/passwd") == ""
    assert jobfiles.read_log("../platform.db") == ""
    assert jobfiles.read_log(f"{JOBS_DIR}/../../x") == ""
