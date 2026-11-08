"""单镜头生产工作台与资产影响分析（任务书 §6.1、§6.3、场景二与场景三）。

要钉住的三件事：

1. **正式产物只有一个定义**：最新一条"成功且签过通过"的作业。
   工作台不许自己另挑一条"看起来更新"的产物显示给用户——那会出现
   "工作台说 v3 是正式的、成片拼的是 v2"。
2. **没接线的音频不能给动作**：音频段只报状态与原因，不出现"提交"这条路。
3. **影响分析必须把形态引用算进来**：改一套衣服也是改动；
   只查直接引用会漏掉最常见的那一类。
"""
import json

import production
import services
from repositories.content import assets as asset_repo, shots

# ---------------------------------------------------------------- 建数据


def _setup(client, n_shots=1):
    pid = client.post("/api/projects", json={"name": "镜头工作台", "description": "",
                                             "tags": []}).json()["id"]
    eid = client.post(f"/api/projects/{pid}/episodes",
                      json={"title": "第 1 集"}).json()["id"]
    sids = [client.post(f"/api/episodes/{eid}/shots", json={
        "summary": f"第 {i + 1} 镜", "prompt": "提示词", "duration": 5,
        "asset_ids": []}).json()["id"] for i in range(n_shots)]
    return pid, eid, sids


def _video_job(pid, eid, sid, client, source_version: str = ""):
    """一条成功但没签字的本地视频作业，产物在盘上。

    `source_version` 由调用方在**建作业之前**算好传进来——它就是"这条产物
    是按哪一版源内容生的"那份快照，真提交路径上由 `submit_job` 填。
    """
    from repositories.jobs import jobs as job_repo
    import jobfiles
    row = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "kind": "video_local", "execution_mode": "local",
        "provider_code": "fastmetal", "model_code": "fastmetal-1.3b-qad",
        "adapter_version": "test", "prompt_snapshot": "工作台", "params": {},
        "input_refs": [], "source_version": source_version})
    claimed = job_repo.claim_next("wb-worker", 60)
    assert claimed is not None and claimed["id"] == row["id"]
    _logs, outputs = jobfiles.ensure_dirs(row["id"])
    target = outputs / "out.mp4"
    target.write_bytes(b"fake-video-bytes")
    job_repo.succeed(row["id"], "wb-worker", "", [jobfiles.relative(target)], 1)
    return job_repo.get(row["id"])


def _sign(pid, job_id, client, decision="approved"):
    from repositories.reviews import reviews
    me = client.get("/api/auth/me").json()["user"]["id"]
    return reviews.upsert_sign(project_id=pid, target_kind="job", target_id=job_id,
                               gate_id=None, base_version="", anchor="sha",
                               decision=decision, note="测试", user_id=me)


# ---------------------------------------------------------------- 八段


def test_工作台八段齐全且键名是界面契约(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "一镜", "prompt": "提示词", "duration": 5, "asset_ids": [],
        "shot_size": "特写"}).json()["id"]
    out = c.get(f"/api/shots/{sid}/workbench").json()
    for key in ("setting", "refs", "keyframes", "videos", "audio",
                "preview", "review", "compose_ready", "precheck"):
        assert key in out, f"工作台缺段：{key}"
    assert out["setting"]["shot_size"] == "特写"
    assert out["setting"]["revision"] >= 1
    assert out["videos"]["formal_job_id"] is None
    assert out["keyframes"]["jobs"] == []


def test_正式视频只认签过通过的那条(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid, eid, (sid,) = _setup(c)
    job = _video_job(pid, eid, sid, c)
    out = production.shot_workbench(sid)
    assert out["videos"]["jobs"][0]["status"] == "succeeded"
    # 没签字就没有"正式视频"：这条口径与 composition/timeline.build 同源
    assert out["videos"]["formal_job_id"] is None
    assert out["compose_ready"]["video_ok"] is False

    _sign(pid, int(job["id"]), c)
    out = production.shot_workbench(sid)
    assert out["videos"]["formal_job_id"] == int(job["id"])
    assert out["compose_ready"]["video_ok"] is True
    assert out["compose_ready"]["signed_ok"] is True
    assert out["review"]["pending"] == []


def test_引用形态的工作台不能因为父资产是Row就炸(as_role, fresh_project, clean_queue):
    """六场景真跑时炸出来的那条：`get_many` 回的是 sqlite3.Row，没有 `.get()`。

    过去的用例都只建"引用资产、不引用形态"的镜头，所以这一段从来没被执行到；
    而"分镜必须引用到具体形态"正是任务书 §5.3 的硬要求，是真实主路径。
    这条用例钉的是：**任何引用了形态的镜头都要能打开工作台，并且形态行带上父资产名**。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    aid = c.post(f"/api/projects/{pid}/assets", json={
        "kind": "character", "name": "掌灯人", "summary": "", "consistency_prompt": "",
        "aliases": [], "tags": [], "scope": "project"}).json()["id"]
    vid = c.post(f"/api/assets/{aid}/variants", json={
        "variant_type": "outfit", "name": "旧袍", "prompt": "灰布旧袍"}).json()["id"]
    sid = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "举灯", "prompt": "夜", "duration": 5,
        "asset_ids": [aid], "variant_ids": [vid]}).json()["id"]

    resp = c.get(f"/api/shots/{sid}/workbench")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert [a["name"] for a in out["refs"]["assets"]] == ["掌灯人"]
    variant = out["refs"]["variants"][0]
    assert variant["name"] == "旧袍" and variant["parent_name"] == "掌灯人"
    assert out["refs"]["missing_assets"] == [] and out["refs"]["missing_variants"] == []

    # 同一条链再走一次"有成功但未签字的视频作业"的状态：
    # `review.pending` 过去直接把 sqlite3.Row 放进响应，FastAPI 序列化时才炸
    # （PydanticSerializationError: Unable to serialize unknown type: sqlite3.Row）。
    # 只有走 HTTP 才量得到这一格，所以这里坚持用 client 而不是直接调函数。
    _video_job(pid, eid, sid, c)
    again = c.get(f"/api/shots/{sid}/workbench")
    assert again.status_code == 200, again.text
    pending = again.json()["review"]["pending"]
    assert len(pending) == 1 and set(pending[0]) >= {"job_id", "prompt", "finished_at"}


def test_驳回的签字不算正式产物(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid, eid, (sid,) = _setup(c)
    job = _video_job(pid, eid, sid, c)
    _sign(pid, int(job["id"]), c, decision="rejected")
    out = production.shot_workbench(sid)
    assert out["videos"]["formal_job_id"] is None
    assert out["review"]["pending"], "被驳回的镜头要回到待办，而不是消失"


def test_音频段说清没接线(as_role, fresh_project, clean_queue):
    """今天 `runners.build()` 里没有 tts／music 分支，工作台必须照实说。"""
    c = as_role("editor")
    pid, eid, (sid,) = _setup(c)
    out = production.shot_workbench(sid)
    assert out["audio"]["engines"]["tts"]["runner_wired"] is False
    assert out["audio"]["engines"]["music"]["runner_wired"] is False
    assert "尚未接入" in out["compose_ready"]["engine_note"]


def test_能不能合成用的是同一份预检清单(as_role, fresh_project, clean_queue,
                                        monkeypatch):
    """`can_compose` 的阻塞项来自 `precheck.shot_precheck`，不是工作台自己数一遍。

    这里把预检临时换成"没有阻塞"，只验**接线**：阻塞清单为空时 can_compose 才为真。
    规则本身在 `tests/test_precheck.py` 里逐条钉。
    """
    c = as_role("editor")
    pid, eid, (sid,) = _setup(c)
    job = _video_job(pid, eid, sid, c)
    _sign(pid, int(job["id"]), c)
    import precheck
    monkeypatch.setattr(precheck, "shot_precheck",
                        lambda *a, **k: {"status": precheck.PASS, "items": [],
                                         "total": 0, "blocking": 0,
                                         "warnings": 0, "passed": 0})
    out = production.shot_workbench(sid)
    assert out["compose_ready"]["can_compose"] is True
    assert out["compose_ready"]["blockers"] == []


# ---------------------------------------------------------------- 影响分析


def test_改资产会点名引用它的镜头(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid, eid, (sid,) = _setup(c)
    aid = c.post(f"/api/projects/{pid}/assets", json={
        "kind": "character", "name": "阿昭", "summary": "", "aliases": [],
        "tags": [], "scope": "project"}).json()["id"]
    c.put(f"/api/shots/{sid}", json={"summary": "一镜", "prompt": "提示词",
                                     "duration": 5, "asset_ids": [aid]})
    out = c.get(f"/api/assets/{aid}/impact").json()
    assert out["counts"]["referencing"] == 1
    assert out["shots"][0]["shot_id"] == sid
    assert out["shots"][0]["recommend"] == "还没生成过"


def test_改了资产之后已生成的视频被标过期(as_role, fresh_project, clean_queue):
    """场景三的正题：动一下资产，要能看见"哪一镜的哪条产物不再作数"。"""
    c = as_role("editor")
    pid, eid, (sid,) = _setup(c)
    aid = c.post(f"/api/projects/{pid}/assets", json={
        "kind": "character", "name": "阿昭", "summary": "", "aliases": [],
        "tags": [], "scope": "project"}).json()["id"]
    c.put(f"/api/shots/{sid}", json={"summary": "一镜", "prompt": "提示词",
                                     "duration": 5, "asset_ids": [aid]})
    # 快照在改资产之前算：真提交路径上就是 submit_job 里那一刻的指纹
    snap = services.shot_source_version(shots.get(sid),
                                       loaded_assets={aid: asset_repo.get(aid)})
    job = _video_job(pid, eid, sid, c, source_version=snap)
    _sign(pid, int(job["id"]), c)
    c.put(f"/api/assets/{aid}", json={"kind": "character", "name": "阿昭",
                                      "summary": "换了描述",
                                      "consistency_prompt": "新的提示词",
                                      "aliases": [], "tags": [], "scope": "project",
                                      "seen_version": ""})
    out = c.get(f"/api/assets/{aid}/impact").json()
    assert out["counts"]["stale"] == 1
    assert "源已变" in out["shots"][0]["recommend"]


def test_形态引用也算影响范围(as_role, fresh_project, clean_queue):
    """只查直接引用会漏掉"改了一套衣服"——那正是最常见的改动。"""
    c = as_role("editor")
    pid, eid, (sid,) = _setup(c)
    aid = c.post(f"/api/projects/{pid}/assets", json={
        "kind": "character", "name": "阿昭", "summary": "", "aliases": [],
        "tags": [], "scope": "project"}).json()["id"]
    vid = c.post(f"/api/assets/{aid}/variants", json={
        "variant_type": "outfit", "name": "常服", "prompt": ""}).json()["id"]
    c.put(f"/api/shots/{sid}", json={"summary": "一镜", "prompt": "提示词",
                                     "duration": 5, "asset_ids": [aid],
                                     "variant_ids": [vid]})
    out = c.get(f"/api/assets/{aid}/impact").json()
    assert out["counts"]["referencing"] == 1
    rows = production.referencing_shots(aid)
    assert rows and int(rows[0]["shot_id"]) == sid
    # 形态引用这条分支单独验一次：`_checked_variant_ids` 会把父资产一起写进
    # asset_ids，所以界面上造不出"只指形态不指人"的行——那就直接查仓储那条
    # EXISTS 分支能不能把这一镜捞回来（它捞的是 variant_ids，不是 asset_ids）。
    from repositories.production import production as prod_repo
    only_variant = shots.update(sid, "一镜", "提示词", 5, [], [vid])
    assert json.loads(only_variant["asset_ids"]) == []
    assert [int(r["shot_id"]) for r in prod_repo.referencing_shot_rows(aid)] == [sid]


def test_工作台与影响分析都不越权(as_role, people, fresh_project, clean_queue):
    owner = as_role("editor")
    pid, eid, (sid,) = _setup(owner)
    aid = owner.post(f"/api/projects/{pid}/assets", json={
        "kind": "scene", "name": "崖顶", "summary": "", "aliases": [], "tags": [],
        "scope": "project"}).json()["id"]
    outsider = as_role("outsider")
    assert outsider.get(f"/api/shots/{sid}/workbench").status_code == 404
    assert outsider.get(f"/api/assets/{aid}/impact").status_code == 404
