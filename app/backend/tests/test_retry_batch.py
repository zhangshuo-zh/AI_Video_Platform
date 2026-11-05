"""批量重投卡住的镜头（任务书 §15 的 P2「批量制作」里那一半能安全自动化的部分）。

判据只有一条主线：**批量不是第二条写口子**。
每一条重投都走与单条重试完全相同的 `jobs.retry()`，而云端与待核对一律逐条拒——
一次点击不该悄悄再扣 N 笔钱（§3 的硬约束），也不该把结果未知的那条重投一次
（`worker.CLOUD_UNCERTAIN_CODES` 同一条判断）。
"""
import pytest

import jobfiles
from repositories.content import episodes
from repositories.jobs import jobs as job_repo


@pytest.fixture(autouse=True)
def _drain_queue():
    """用例结束后把队列清空。

    全局并发是 1：只要有一条 `queued` 没人领，下一个用例的 `claim_next` 就永远领不到
    自己刚建的那条（本文件第一版就是这么红的——而且红得看不出原因）。
    重投出来的新行本来就是 `queued`，所以必须由这里收尾。
    """
    yield
    for i in range(40):
        got = job_repo.claim_next(f"drain-{i}", 60)
        if got is None:
            break
        job_repo.cancelled(int(got["id"]), f"drain-{i}", "用例收尾")


def _job(client, pid, eid, sid, *, mode="local", status="failed",
         worker="rb-worker", provider="mock", model="mock-1", priority=999):
    row = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "kind": "video_local" if mode == "local" else "video_cloud",
        "execution_mode": mode, "provider_code": provider, "model_code": model,
        "adapter_version": "test", "prompt_snapshot": "批量重投用例",
        "params": {"sleep": 0.01, "fail": True}, "input_refs": [],
        # `claim_next` 按 `priority DESC, queued_at, id` 取活：库是跨用例复用的，
        # 不给这条抬优先级，就会领到别的用例留下的一条排队作业（第一版就是这么红的）。
        "priority": priority})
    if status == "queued":
        return row
    claimed = job_repo.claim_next(worker, 60)
    assert claimed is not None and int(claimed["id"]) == int(row["id"]), \
        "领不到自己建的那条（先看本文件的 autouse 收尾有没有把队列清干净）"
    if status == "failed":
        job_repo.fail(row["id"], worker, "run_timeout", "执行超过上限，已终止", None)
    elif status == "needs_attention":
        job_repo.needs_attention(row["id"], worker, "provider_timeout",
                                 "云端结果未知", "租约丢失且结果不确定")
    elif status == "succeeded":
        _logs, outputs = jobfiles.ensure_dirs(row["id"])
        target = outputs / "out.mp4"
        target.write_bytes(b"x")
        job_repo.succeed(row["id"], worker, "", [jobfiles.relative(target)], 1000)
    return job_repo.get(row["id"])


def _shots(client, pid, eid, n):
    out = []
    for i in range(n):
        r = client.post(f"/api/episodes/{eid}/shots", json={
            "summary": f"第 {i + 1} 镜", "prompt": "崖顶黄昏", "duration": 5,
            "asset_ids": []})
        assert r.status_code == 201, r.text
        out.append(r.json())
    return out


def test_只重投本机失败的那几镜(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    s1, s2, s3 = _shots(c, pid, eid, 3)
    failed = _job(c, pid, eid, s1["id"], status="failed")
    _job(c, pid, eid, s2["id"], status="succeeded")
    _job(c, pid, eid, s3["id"], status="queued", priority=0)

    got = c.post("/api/jobs/retry-batch", json={"episode_id": eid})
    assert got.status_code in (200, 201, 202), got.text
    data = got.json()
    assert [r["shot_id"] for r in data["retried"]] == [s1["id"]]
    reasons = {x["shot_id"]: x["reason"] for x in data["skipped"]}
    assert s2["id"] in reasons and "已经成功" in reasons[s2["id"]]
    assert s3["id"] in reasons and "在跑或排队" in reasons[s3["id"]]
    assert data["counts"] == {"shots": 3, "retried": 1, "skipped": 2}


def test_新行继承参数原行不动(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    shot = _shots(c, pid, eid, 1)[0]
    old = _job(c, pid, eid, shot["id"], status="failed")
    data = c.post("/api/jobs/retry-batch", json={"episode_id": eid}).json()
    new_id = data["retried"][0]["new_job_id"]

    fresh = job_repo.get(new_id)
    assert fresh["attempt"] == 2 and fresh["params_json"] == old["params_json"]
    assert fresh["retry_of_job_id"] == old["id"] and fresh["status"] == "queued"
    kept = job_repo.get(old["id"])
    assert kept["status"] == "failed" and kept["error_code"] == "run_timeout"
    assert kept["elapsed_ms"] == old["elapsed_ms"] and kept["superseded_by_job_id"] == new_id


def test_云端与待核对逐条拒且不多排队(as_role, fresh_project):
    """批量入口最贵的风险是一次点下去再扣 N 笔钱，或把结果未知的那条重投。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    s_cloud, s_att = _shots(c, pid, eid, 2)
    _job(c, pid, eid, s_cloud["id"], mode="cloud", status="failed",
         provider="deepseek", model="deepseek-chat")
    _job(c, pid, eid, s_att["id"], status="needs_attention")

    total_before = job_repo.cost_summary([pid])["total_jobs"]
    data = c.post("/api/jobs/retry-batch", json={"episode_id": eid}).json()
    assert data["retried"] == []
    reasons = {x["shot_id"]: x["reason"] for x in data["skipped"]}
    assert "逐次确认计费" in reasons[s_cloud["id"]]
    assert "待核对" in reasons[s_att["id"]]
    assert job_repo.cost_summary([pid])["total_jobs"] == total_before, "拒掉的不该留下新作业"


def test_上限五十一条直接被拒_批量不静默截断(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    r = c.post("/api/jobs/retry-batch", json={"episode_id": eid, "limit": 51})
    assert r.status_code in (422, 400), r.text
    assert "50" in str(r.json()["detail"])


def test_到了上限就明说而不是默默少做(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    a, b = _shots(c, pid, eid, 2)
    _job(c, pid, eid, a["id"], status="failed")
    _job(c, pid, eid, b["id"], status="failed")
    data = c.post("/api/jobs/retry-batch", json={"episode_id": eid, "limit": 1}).json()
    assert len(data["retried"]) == 1 and len(data["skipped"]) == 1
    assert "上限" in data["skipped"][0]["reason"]


def test_看不见这一集就404(as_role, fresh_project):
    """归属判断不许被咽成逐条原因——那会把批量入口变成探测别人项目的尺子。"""
    c_admin = as_role("admin")
    pid = c_admin.post("/api/projects", json={"name": "别人的批量项目",
                                             "description": "", "tags": []}).json()["id"]
    eid = c_admin.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    outsider = as_role("outsider")
    r = outsider.post("/api/jobs/retry-batch", json={"episode_id": eid})
    assert r.status_code == 404, r.text


def test_响应字段真的出得来(as_role, fresh_project):
    """P1 那一轮的复发预防：`response_model` 会丢字段，必须走 HTTP 量一次。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    shot = _shots(c, pid, eid, 1)[0]
    _job(c, pid, eid, shot["id"], status="failed")
    data = c.post("/api/jobs/retry-batch", json={"episode_id": eid}).json()
    for key in ("project_id", "episode_id", "capability", "counts",
                "retried", "skipped", "rule"):
        assert key in data, key
    assert data["retried"][0].keys() >= {"shot_id", "job_id", "new_job_id", "attempt"}
