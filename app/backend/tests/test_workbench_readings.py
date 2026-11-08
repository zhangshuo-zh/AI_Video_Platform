"""首页两个只读口径：已签字待合成、本机产能读数。

它们都是"现算不建表"的数字，所以最怕两件事：
一是口径漂（工作台、成片、首页各算各的），二是没样本时给个 0 冒充读数。
这里把两条口径直接钉在服务函数上，绕过接口的全局项目集合，避免相互污染。
"""

import jobfiles
import services
from db import get_conn
from repositories.jobs import jobs as job_repo
from repositories.reviews import reviews


def _video_job(pid, eid, sid, elapsed_ms=41_700, duration=5.0):
    row = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "kind": "video_local", "execution_mode": "local",
        "provider_code": "fastmetal", "model_code": "fastmetal-1.3b-qad",
        "adapter_version": "test", "prompt_snapshot": "读数", "params": {},
        "input_refs": [], "source_version": ""})
    claimed = job_repo.claim_next("reading-worker", 60)
    assert claimed is not None and claimed["id"] == row["id"]
    _logs, outputs = jobfiles.ensure_dirs(row["id"])
    target = outputs / "out.mp4"
    target.write_bytes(b"fake-video-bytes")
    job_repo.succeed(row["id"], "reading-worker", "", [jobfiles.relative(target)], 1)
    with get_conn() as conn:
        conn.execute("UPDATE job SET elapsed_ms = ? WHERE id = ?",
                     (elapsed_ms, row["id"]))
    return job_repo.get(row["id"])


def _project_with_shot(client, n_shots=1, duration=5.0):
    pid = client.post("/api/projects", json={
        "name": "读数项目", "description": "", "tags": []}).json()["id"]
    eid = client.post(f"/api/projects/{pid}/episodes",
                      json={"title": "第 1 集"}).json()["id"]
    sids = [client.post(f"/api/episodes/{eid}/shots", json={
        "summary": f"第 {i + 1} 镜", "prompt": "提示词",
        "duration": duration, "asset_ids": []}).json()["id"] for i in range(n_shots)]
    return pid, eid, sids


def test_签字后没出成片才算待合成(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid, eid, (sid,) = _project_with_shot(c)
    job = _video_job(pid, eid, sid)
    me = c.get("/api/auth/me").json()["user"]["id"]

    assert services._signed_ready_for([pid]) == {}   # 没签，不算

    reviews.upsert_sign(project_id=pid, target_kind="job", target_id=job["id"],
                        gate_id=None, base_version="", anchor="sha",
                        decision="approved", note="测试", user_id=me)
    assert services._signed_ready_for([pid]) == {pid: 1}

    # 出一版成片，且晚于签字——这一镜已经进了成片，不再等
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO composition
                 (project_id, episode_id, version, timeline_json, params_json,
                  created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?, datetime('now','localtime','+2 seconds'),
                       datetime('now','localtime','+2 seconds'))""",
            (pid, eid, 1, "{}", "{}", me))
    assert services._signed_ready_for([pid]) == {}


def test_签的是打回就不算待合成(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid, eid, (sid,) = _project_with_shot(c)
    job = _video_job(pid, eid, sid)
    me = c.get("/api/auth/me").json()["user"]["id"]
    reviews.upsert_sign(project_id=pid, target_kind="job", target_id=job["id"],
                        gate_id=None, base_version="", anchor="sha",
                        decision="rejected", note="打回", user_id=me)
    assert services._signed_ready_for([pid]) == {}


def test_产能读数按机时算并给出样本数(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid, eid, (sid,) = _project_with_shot(c, duration=5.0)
    _video_job(pid, eid, sid, elapsed_ms=41_700)

    out = services._throughput_for([pid])
    assert out["samples"] == 1
    assert out["video_avg_sec"] == 41.7
    # 60 秒 ÷ 41.7 秒一镜 ≈ 1.44 镜/分钟，每镜 5 秒 → 约 7.2 秒成片/分钟
    assert out["per_minute_sec"] == 7.2
    assert out["image_avg_sec"] is None
    assert out["caliber"]


def test_没有作业样本时不给数字(as_role, fresh_project):
    c = as_role("editor")
    pid, _eid, _sids = _project_with_shot(c)
    out = services._throughput_for([pid])
    assert out["samples"] == 0
    assert out["video_avg_sec"] is None
    assert out["per_minute_sec"] is None
