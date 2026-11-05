"""审核质量清单与批量签字（任务书 §8.2／§12.1，P1 第 4、5 项；迁移 0038）。

这一层最容易被写成"看起来能用"，所以每条断言都盯一件**能变红**的事：

* 没看完清单能不能签通过——不能，而且要说清缺哪几项；
* 一眼看出不行时能不能立刻驳回——能，且不需要先填表；
* 批量里混进一条看不见的项目——整批按 404 抛出，可见的那条也不许被签掉
  （批量不许变成探测他项目内容的尺子，这是守卫用例抓出来的真缺陷）；
* 界面传来的项不属于这一类能力——422，不往枚举里掺野 code。
"""
import subprocess

import pytest

import jobfiles
import reviewkit
import services
from config import FFMPEG_BIN
from repositories.jobs import jobs as job_repo
from repositories.reviews import reviews
from repositories.review_checks import review_checks

WID = "check-worker"


def _has_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG_BIN, "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _has_ffmpeg(), reason="本机没有 ffmpeg：造不出 ffprobe 读得出的真产物")


def _video_job(client, pid, eid, sid, *, capability="video", stale_source=False):
    """一条成功、有真产物、可选"按旧版源生成"的作业。"""
    shot = client.get(f"/api/episodes/{eid}/shots").json()
    shot = next((s for s in shot if s["id"] == sid), shot[0])
    source = "" if stale_source else services.shot_source_version(
        _shot_row(sid))
    row = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": capability, "kind": "video_local" if capability == "video" else "image",
        "execution_mode": "local", "provider_code": "fastmetal" if capability == "video" else "mflux",
        "model_code": "fastmetal-1.3b-qad" if capability == "video" else "flux1-schnell",
        "adapter_version": "test", "prompt_snapshot": "提示词", "params": {},
        "input_refs": [], "source_version": source})
    assert job_repo.claim_next(WID, 60)["id"] == row["id"]
    _logs, outputs = jobfiles.ensure_dirs(row["id"])
    # 必须是**真媒体**：清单里 `media.readable` 用 ffprobe 判，
    # 手搓的文件头能骗过魔数检查、骗不过 ffprobe——那条 block 是有意的，
    # 因为成片取片时同样要 ffprobe，审核签通过的不该在合成当天才不认。
    target = outputs / ("out.mp4" if capability == "video" else "out.png")
    if capability == "video":
        subprocess.run([FFMPEG_BIN, "-hide_banner", "-y", "-f", "lavfi",
                        "-i", "color=c=red:s=320x180:d=1", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-r", "24", str(target)],
                       check=True, capture_output=True)
    else:
        subprocess.run([FFMPEG_BIN, "-hide_banner", "-y", "-f", "lavfi",
                        "-i", "color=c=red:s=320x180", "-frames:v", "1", str(target)],
                       check=True, capture_output=True)
    job_repo.succeed(row["id"], WID, "", [jobfiles.relative(target)], 1)
    return job_repo.get(row["id"])


def _shot_row(sid):
    from repositories.content import shots
    return shots.get(sid)


def _setup(client, n=1):
    pid = client.post("/api/projects", json={"name": "审核清单", "description": "",
                                             "tags": []}).json()["id"]
    eid = client.post(f"/api/projects/{pid}/episodes",
                      json={"title": "第 1 集"}).json()["id"]
    sids = [client.post(f"/api/episodes/{eid}/shots", json={
        "summary": f"第 {i + 1} 镜", "prompt": "崖顶黄昏", "duration": 5,
        "asset_ids": []}).json()["id"] for i in range(n)]
    return pid, eid, sids


def _fill_all(client, job_id, capability="video", state="pass"):
    codes = [i["code"] for i in reviewkit.HUMAN_ITEMS[capability] if i.get("required")]
    return client.post(f"/api/jobs/{job_id}/review-checklist",
                       json={"items": [{"code": c, "state": state, "note": ""}
                                       for c in codes]})


# ---------------------------------------------------------------- 清单本体

def test_视频清单有四到五项必选且自动项现算(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "一镜", "prompt": "提示词", "duration": 5, "asset_ids": []}).json()["id"]
    job = _video_job(c, pid, eid, sid)

    got = c.get(f"/api/jobs/{job['id']}/review-checklist").json()
    assert got["capability"] == "video"
    required = [h for h in got["human"] if h["required"]]
    assert len(required) >= 4, required
    assert all(h["state_label"] == "未看" for h in required)
    codes = {a["code"] for a in got["auto"]}
    assert {"artifact.exists", "source.current"} <= codes
    assert got["can_approve"] is False
    assert "还差" in got["hint"]


def test_没看完清单签通过被拒且缺项点名(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "一镜", "prompt": "提示词", "duration": 5, "asset_ids": []}).json()["id"]
    job = _video_job(c, pid, eid, sid)

    # 只勾一项就想批量签通过 → 逐条拒，并说清还差几项
    c.post(f"/api/jobs/{job['id']}/review-checklist",
           json={"items": [{"code": "content.matches_prompt", "state": "pass"}]})
    r = c.post("/api/jobs/review-batch", json={
        "items": [{"job_id": job["id"]}], "decision": "approved", "note": "看着行"})
    assert r.status_code == 200 and r.json()["signed"] == 0
    reason = r.json()["results"][0]["reason"]
    assert "还不能签通过" in reason and "还差" in reason, reason
    assert reviews.signs("job", int(job["id"])) == [], "被拒的批量不许留下签字"


def test_清单看齐后批量签通过落一条签字(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sids = [c.post(f"/api/episodes/{eid}/shots", json={
        "summary": f"第 {i} 镜", "prompt": "提示词", "duration": 5,
        "asset_ids": []}).json()["id"] for i in (1, 2)]
    jobs = [_video_job(c, pid, eid, sid) for sid in sids]

    for j in jobs:
        assert _fill_all(c, j["id"]).status_code == 200
    r = c.post("/api/jobs/review-batch", json={
        "items": [{"job_id": j["id"]} for j in jobs],
        "decision": "approved", "note": "整批看过"})
    body = r.json()
    assert body["signed"] == 2 and body["refused"] == 0, body
    for j in jobs:
        signs = reviews.signs("job", int(j["id"]))
        assert len(signs) == 1 and signs[0]["decision"] == "approved"
        assert signs[0]["base_version"] == services.artifact_version(j), "签的是当前产物那一版"


def test_驳回不需要先填清单(as_role, fresh_project, clean_queue):
    """这个不对称是有意的：一眼看出不行就该能立刻驳回，不能被表单卡住。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "一镜", "prompt": "提示词", "duration": 5, "asset_ids": []}).json()["id"]
    job = _video_job(c, pid, eid, sid)

    r = c.post("/api/jobs/review-batch", json={
        "items": [{"job_id": job["id"]}], "decision": "rejected", "note": "跑偏了"})
    assert r.json()["signed"] == 1, r.json()
    assert reviews.signs("job", int(job["id"]))[0]["decision"] == "rejected"


def test_清单勾了不合格也算看完但结论要人自己下(as_role, fresh_project, clean_queue):
    """勾完 ≠ 合格：`issues` 单独报数，界面得让人看见"这项我判了不合格"。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "一镜", "prompt": "提示词", "duration": 5, "asset_ids": []}).json()["id"]
    job = _video_job(c, pid, eid, sid)
    codes = [i["code"] for i in reviewkit.HUMAN_ITEMS["video"] if i.get("required")]
    c.post(f"/api/jobs/{job['id']}/review-checklist",
           json={"items": [{"code": codes[0], "state": "issue", "note": "手多了一根"},
                           *({"code": x, "state": "pass", "note": ""} for x in codes[1:])]})

    got = c.get(f"/api/jobs/{job['id']}/review-checklist").json()
    assert got["counts"]["human_missing"] == 0
    assert got["counts"]["human_issues"] == 1
    # 勾了不合格仍允许签通过（人可以判"瑕不掩瑜"），但这条必须被显示出来——
    # 清单的作用是"看过什么看得见"，不是替人做决定
    assert got["can_approve"] is True


# ---------------------------------------------------------------- 边界与拒写

def test_野code被拒且不落库(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "一镜", "prompt": "提示词", "duration": 5, "asset_ids": []}).json()["id"]
    job = _video_job(c, pid, eid, sid)

    r = c.post(f"/api/jobs/{job['id']}/review-checklist",
               json={"items": [{"code": "content.whatever", "state": "pass"}]})
    assert r.status_code == 422, r.text
    assert review_checks.get("job", int(job["id"])) == {}


def test_图像作业不吃视频清单的项(as_role, fresh_project, clean_queue):
    """两类能力的清单是两套码：拿视频的项去勾图像必须被拒。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "一镜", "prompt": "提示词", "duration": 5, "asset_ids": []}).json()["id"]
    job = _video_job(c, pid, eid, sid, capability="image")

    got = c.get(f"/api/jobs/{job['id']}/review-checklist").json()
    assert {i["code"] for i in got["human"]} == {
        i["code"] for i in reviewkit.HUMAN_ITEMS["image"]}
    r = c.post(f"/api/jobs/{job['id']}/review-checklist",
               json={"items": [{"code": "content.motion_ok", "state": "pass"}]})
    assert r.status_code == 422, r.text


def test_批量对看不见的项目整批按403或404且不部分签(as_role, fresh_project,
                                          clean_queue):
    """批量的归属判断必须与单条同一条路。

    写第一版时这里错了：`except Exception` 把 `access.ensure` 的 404 也咽了下去，
    于是接口回 200 ＋ 一句"作业不存在"——批量入口成了一把探测别人项目内容的尺子。
    守卫用例（越权那条）抓出之后改成只接业务性拒绝。
    """
    editor = as_role("editor")
    outsider = as_role("outsider")
    pid = fresh_project("editor")
    eid = editor.post(f"/api/projects/{pid}/episodes",
                      json={"title": "第 1 集"}).json()["id"]
    sid = editor.post(f"/api/episodes/{eid}/shots", json={
        "summary": "一镜", "prompt": "提示词", "duration": 5,
        "asset_ids": []}).json()["id"]
    # 两条清单都齐了——如果批量把归属判断降级成逐条报，第二条就会被"顺手签掉"
    sid2 = editor.post(f"/api/episodes/{eid}/shots", json={
        "summary": "第二镜", "prompt": "提示词", "duration": 5,
        "asset_ids": []}).json()["id"]
    mine = _video_job(editor, pid, eid, sid)
    mine2 = _video_job(editor, pid, eid, sid2)
    _fill_all(editor, mine["id"])
    _fill_all(editor, mine2["id"])

    r = outsider.post("/api/jobs/review-batch", json={
        "items": [{"job_id": mine["id"]}, {"job_id": mine2["id"]}],
        "decision": "approved"})
    assert r.status_code in (403, 404), r.text
    # 整批不许部分落地：两条都没有签字
    assert reviews.signs("job", int(mine["id"])) == []
    assert reviews.signs("job", int(mine2["id"])) == []


# ------------------------------------------------ 待裁开关：清单要不要管住单条签字

def test_现行为_单条签通过不要求清单(as_role, fresh_project, clean_queue):
    """开关默认 False：要求只住在批量接口与审核中心界面。

    这条用例钉的是「现状」，不是「应该」——翻开关时它会红，那时要连带改的是
    产线人工关卡与六场景脚本，所以不许顺手把它删掉。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "一镜", "prompt": "提示词", "duration": 5, "asset_ids": []}).json()["id"]
    job = _video_job(c, pid, eid, sid)
    got = c.get(f"/api/jobs/{job['id']}/review-checklist").json()
    assert got["can_approve"] is False          # 清单没看
    r = c.post(f"/api/jobs/{job['id']}/review-sign",
               json={"anchor": "artifact", "decision": "approved", "note": "单条直接签"})
    assert r.status_code == 200, r.text         # 现行为：签得掉


def test_开关翻起来后单条签通过也被清单管住(as_role, fresh_project, clean_queue, monkeypatch):
    """翻一下开关就得真的生效——不然这个开关只是个装饰。"""
    import reviewkit
    monkeypatch.setattr(reviewkit, "ENFORCE_CHECKLIST_ON_SINGLE_SIGN", True)
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "一镜", "prompt": "提示词", "duration": 5, "asset_ids": []}).json()["id"]
    job = _video_job(c, pid, eid, sid)

    r = c.post(f"/api/jobs/{job['id']}/review-sign",
               json={"anchor": "artifact", "decision": "approved", "note": "还是想直接签"})
    assert r.status_code == 422, r.text
    assert "还没看完" in r.json()["detail"], r.text
    assert reviews.signs("job", int(job["id"])) == []

    # 驳回不受这条约束（不对称是有意的）
    assert c.post(f"/api/jobs/{job['id']}/review-sign",
                  json={"anchor": "artifact", "decision": "rejected",
                        "note": "跑偏"}).status_code == 200

    # 勾完就能签
    assert _fill_all(c, job["id"]).status_code == 200
    assert c.post(f"/api/jobs/{job['id']}/review-sign",
                  json={"anchor": "artifact", "decision": "approved",
                        "note": "看完了"}).status_code == 200
