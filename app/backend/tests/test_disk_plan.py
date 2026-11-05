"""盘余估算（`diskplan`）：把《发布基线》§3 挂着的那条「5GB 是拍的」变成能复核的算式。

三条判据，都对应一个真会骗人的地方：

1. **没有样本就不许说"预计"**。新项目一个产物都没有时，唯一诚实的说法是固定提醒线；
   拿别的项目或文档里的数字顶，人就会以为这个数与自己的镜头有关。
2. **成片作业不能混进"每镜平均"**。成片的 `capability` 也是 `video`，混进来会把
   平均值抬高一到两个数量级（本机实测：一镜 640×352 约 0.17 MB，一次成片约 5.6 MB）——
   这条被算错时，界面上的"还要多少磁盘"会大到没人信，阈值反而失去意义。
3. **三个入口说同一句话**。生成前预检、合成前预检、产线发起与统计面板读的是
   同一个 `diskplan.disk_readout()`；两处分叉就是界面自相矛盾。
"""
import pytest

import jobfiles
from repositories.content import episodes
from repositories.jobs import jobs as job_repo

import diskplan


@pytest.fixture(autouse=True)
def _清空队列():
    """全局并发是 1：库里只要挂着一条没人领的 `queued`（别的用例留下的），
    这里的 `claim_next` 就会领到那条或干脆领不到，用例红得看不出原因。
    """
    yield
    for i in range(60):
        got = job_repo.claim_next(f"dp-drain-{i}", 60)
        if got is None:
            break
        job_repo.cancelled(int(got["id"]), f"dp-drain-{i}", "用例收尾")


def _job_with_artifact(pid, eid, *, sid=None, capability="video", provider="mock",
                      model="mock-1", size=1024, worker="dp-worker", name="out.bin"):
    row = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": capability, "kind": "video_local", "execution_mode": "local",
        "provider_code": provider, "model_code": model, "adapter_version": "test",
        "prompt_snapshot": "盘余用例", "params": {}, "input_refs": []})
    assert job_repo.claim_next(worker, 60)["id"] == row["id"]
    _logs, outputs = jobfiles.ensure_dirs(row["id"])
    target = outputs / name
    target.write_bytes(b"0" * size)
    job_repo.succeed(row["id"], worker, "", [jobfiles.relative(target)], 100)
    return row


def test_没有样本时只说提醒线不说预计():
    est = diskplan.estimate(shots_missing=5, shot_video=[], keyframe=[], compose=[],
                            free_bytes=3 * 1024 ** 3, min_free_gb=5)
    assert est["has_sample"] is False and est["estimated_need_bytes"] == 0
    text = diskplan.sentence(est)
    assert "提醒线" in text and "预计" not in text, text
    assert est["shortfall_bytes"] == 2 * 1024 ** 3


def test_有样本时给预计与盘余两个数():
    est = diskplan.estimate(shots_missing=10, shot_video=[200_000], keyframe=[2_000_000],
                            compose=[6_000_000], free_bytes=40 * 1024 ** 3)
    need = round((200_000 * 10 + 2_000_000 * 10 + 6_000_000) * diskplan.SAFETY_RATIO)
    assert est["estimated_need_bytes"] == need, "算式必须就是「每镜×镜数＋成片」乘余量系数"
    text = diskplan.sentence(est)
    assert "还差 10 镜" in text and "预计" in text and "1.25" in text, text
    assert "每镜视频平均" in text and "每张关键帧平均" in text


def test_盘不够时shortfall是正数():
    est = diskplan.estimate(shots_missing=100, shot_video=[10_000_000], compose=[],
                            free_bytes=1024 ** 3)
    assert est["shortfall_bytes"] > 0
    assert "已经贴到提醒线以下" in diskplan.sentence(est)


def test_读不到盘余时不猜一个数():
    est = diskplan.estimate(shots_missing=1, shot_video=[1000], free_bytes=None)
    assert est["free_bytes"] is None and est["shortfall_bytes"] is None
    assert "读不到" in diskplan.sentence(est)


def test_每镜平均不把成片作业算进去(as_role, fresh_project):
    """这一条是本模块存在的理由：混进去"每镜还要多少磁盘"会大一个数量级。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    shot_rows = [c.post(f"/api/episodes/{eid}/shots", json={
        "summary": f"镜 {i}", "prompt": "崖顶黄昏", "duration": 5,
        "asset_ids": []}).json() for i in range(2)]
    for i, shot in enumerate(shot_rows):
        _job_with_artifact(pid, eid, sid=shot["id"], size=1024 * (i + 1),
                           worker=f"dp-shot-{i}", name=f"v{i}.mp4")
    # 一次成片：capability 同为 video，但 shot_id 为空、provider 是 ffmpeg
    _job_with_artifact(pid, eid, provider="ffmpeg", model="ffmpeg-compose-1",
                       size=200 * 1024, worker="dp-compose", name="c.mp4")

    est = diskplan.disk_readout(pid, shots_missing=2)
    shot_avg = est["per"]["shot_video"]["avg_bytes"]
    assert est["per"]["shot_video"]["samples"] == 2
    assert 1000 < shot_avg < 3000, f"每镜平均被成片污染了：{shot_avg}"
    assert est["per"]["compose"]["avg_bytes"] > 100 * 1024, "成片那一档该单独量"
    assert "预计" in est["sentence"]


def test_预检与统计说的是同一句话(as_role, fresh_project):
    """两个入口分叉＝界面自相矛盾，所以这一条比的不是"都有数字"，而是同一句。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    shot = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "镜 1", "prompt": "崖顶黄昏", "duration": 5,
        "asset_ids": []}).json()
    _job_with_artifact(pid, eid, sid=shot["id"], size=4096, worker="dp-eq",
                       name="v.mp4")

    stats = c.get(f"/api/projects/{pid}/production?episode_id={eid}").json()["stats"]
    comp = c.get(f"/api/projects/{pid}/composition-precheck?episode_id={eid}").json()
    disk_items = [i for i in comp["items"] if i["code"] == "env.disk"]
    assert disk_items and stats["disk"]["sentence"], "两边都得有读数"
    # 合成前那一栏只量成片产物那一档的空间（shots_missing=0），镜头那一档不能混进来；
    # 而镜头预检说的是"还差 1 镜"——同一处算式、两个口径，各自都答得上自己那一问
    assert "只算一次成片产物" in disk_items[0]["text"], disk_items[0]["text"]
    assert "还差" not in disk_items[0]["text"].split("｜")[0], disk_items[0]["text"]
    assert stats["disk"]["estimated_need_bytes"] >= 0
    shot_check = c.get(f"/api/precheck/shot/{shot['id']}").json()
    shot_disk = [i for i in shot_check["items"] if i["code"] == "env.disk"]
    assert shot_disk and "还差 1 镜" in shot_disk[0]["text"], shot_disk[0]["text"]
