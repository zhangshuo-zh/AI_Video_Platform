"""阶段 E-1：资源类并发与多 worker 心跳。

三条必须成立的事：
  * 默认仍是"全局 1／每类 1"——升级不会让 16GB 机器同时跑两条视频；
  * 抬了上限以后，**不同**资源类可以并行，同一类仍按格数封顶；
  * 两个 worker 同时领，只能有一条拿到（原子领取不会重复执行）。

心跳用临时 worker id，结束前把这两个进程标成很久以前，不留给后面的用例。
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import worker
from db import get_conn
from repositories.jobs import jobs as job_repo, resource_class


def _job(project_id: int, user_id: int, *, capability="text",
         provider="mock", priority=0) -> dict:
    # kind 是库里的稳定枚举，不是随便写的标签：文本/图片直接用 capability，
    # 视频与合成都落 video_local（合成也是本地视频作业）。
    kind = {"text": "text", "image": "image"}.get(capability, "video_local")
    return job_repo.create({
        "project_id": project_id, "episode_id": None, "shot_id": None,
        "capability": capability, "kind": kind, "execution_mode": "local",
        "provider_code": provider, "model_code": "mock-1",
        "adapter_version": "mock", "prompt_snapshot": f"并发测试 {capability}",
        "params": {"sleep": 0.01}, "input_refs": [], "priority": priority,
        "created_by": user_id,
    })


@pytest.fixture
def queue(fresh_project, people, clean_queue):
    pid = fresh_project("editor")
    return {"pid": pid, "uid": people["editor"]["id"]}


def _finish(job_id: int, wid: str) -> None:
    job_repo.fail(job_id, wid, "test_done", "测试收尾", None, None, 1)


def test_资源类映射():
    assert resource_class("video", "mock") == "video"
    assert resource_class("tts") == "audio"
    assert resource_class("music", "mock") == "audio"
    assert resource_class("video", "ffmpeg") == "compose"


def test_默认全局一_第二条要等第一条收尾(queue):
    first = _job(queue["pid"], queue["uid"], capability="text")
    second = _job(queue["pid"], queue["uid"], capability="image")
    got = job_repo.claim_next("e1-default", 60)
    assert got is not None and got["id"] == first["id"]
    assert job_repo.claim_next("e1-default", 60) is None      # 全局 1，另一类也不领
    _finish(first["id"], "e1-default")
    got = job_repo.claim_next("e1-default", 60)
    assert got is not None and got["id"] == second["id"]
    _finish(second["id"], "e1-default")


def test_抬了全局上限后不同资源类可以并行(queue):
    limits = {"global": 3, "per": {"text": 1, "image": 1, "video": 1,
                                   "audio": 1, "compose": 1}}
    a = _job(queue["pid"], queue["uid"], capability="text")
    b = _job(queue["pid"], queue["uid"], capability="image")
    c = _job(queue["pid"], queue["uid"], capability="text")
    one = job_repo.claim_next("e1-a", 60, limits=limits)
    two = job_repo.claim_next("e1-b", 60, limits=limits)
    assert {one["id"], two["id"]} == {a["id"], b["id"]}       # 两类各走一条
    assert job_repo.claim_next("e1-c", 60, limits=limits) is None   # text 已满
    for row, wid in ((one, "e1-a"), (two, "e1-b")):
        _finish(row["id"], wid)
    got = job_repo.claim_next("e1-d", 60, limits=limits)
    assert got is not None and got["id"] == c["id"]
    _finish(got["id"], "e1-d")


def test_视频类仍按格数封顶(queue):
    limits = {"global": 9, "per": {"video": 1}}
    a = _job(queue["pid"], queue["uid"], capability="video")
    _job(queue["pid"], queue["uid"], capability="video")
    one = job_repo.claim_next("e1-v1", 60, limits=limits)
    assert one["id"] == a["id"]
    assert job_repo.claim_next("e1-v2", 60, limits=limits) is None
    _finish(one["id"], "e1-v1")


def test_成片单独占一类不与视频抢(queue):
    limits = {"global": 9, "per": {"video": 1, "compose": 1}}
    v = _job(queue["pid"], queue["uid"], capability="video")
    f = _job(queue["pid"], queue["uid"], capability="video",
             provider="ffmpeg")
    one = job_repo.claim_next("e1-v", 60, limits=limits)
    two = job_repo.claim_next("e1-f", 60, limits=limits)
    assert (one["id"], two["id"]) == (v["id"], f["id"])
    # 终态写入带 `AND worker_id = ?` 归属守卫：谁领的就得由谁收尾，
    # 两行都用同一个临时 wid 现在会被明确判成「归属丢了」。
    for row, wid in ((one, "e1-v"), (two, "e1-f")):
        _finish(row["id"], wid)


def test_两个worker同时领只有一条拿到(queue):
    job = _job(queue["pid"], queue["uid"], capability="text")
    limits = {"global": 4, "per": {"text": 1}}

    def grab(wid: str):
        return job_repo.claim_next(wid, 60, limits=limits)

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(grab, ["e1-race-1", "e1-race-2"]))
    got = [r["id"] for r in rows if r is not None]
    assert got == [job["id"]]
    _finish(job["id"], "e1-race-1")


def test_心跳带资源占用():
    """`worker_node.resources_json` 不是装饰：面板要靠它回答"还要不要再开一个 worker"。

    读数只保证**形状与口径**，不保证具体数值——数值随机器走，写死反而会让
    测试在别的机器上假红。
    """
    sample = worker._node_resources()
    for key in ("cores", "load1", "cpu_pct", "rss_peak_mb",
                "disk_free_gb", "disk_total_gb"):
        assert key in sample, key
    assert isinstance(sample["cpu_pct"], (int, float))
    assert sample["rss_peak_mb"] > 0            # 进程自己总要占点内存；0 说明单位搞错了
    assert sample["disk_total_gb"] > 0

    # 第二次采样是"这两次心跳之间"的占用，不能把整个生命周期的均值拖回来
    time.sleep(0.6)
    again = worker._node_resources()
    assert 0.0 <= again["cpu_pct"] < 100.0 * (os.cpu_count() or 1)

    job_repo.heartbeat("e1-res-1", 999, None, "test",
                       limits={"global": 2, "per": {}}, resources=sample)
    rows = {w["worker_id"]: w for w in job_repo.workers(online_seconds=60)}
    assert rows["e1-res-1"]["resources"]["disk_total_gb"] == sample["disk_total_gb"]
    with get_conn() as conn:
        conn.execute("UPDATE worker_node SET heartbeat_at = "
                     "datetime('now','localtime','-1 day') "
                     "WHERE worker_id = 'e1-res-1'")


def test_心跳能列出多个worker与各自上限(as_role):
    limits = {"global": 2, "per": {"video": 1, "image": 1}}
    job_repo.heartbeat("e1-node-a", 111, None, "test", limits=limits)
    job_repo.heartbeat("e1-node-b", 222, None, "test",
                       limits={"global": 1, "per": {}})
    body = as_role("viewer").get("/api/worker/status").json()
    assert body["online"] is True
    ids = {w["worker_id"]: w for w in body["workers"]}
    assert {"e1-node-a", "e1-node-b"} <= set(ids)
    assert ids["e1-node-a"]["limits"]["global"] == 2
    assert ids["e1-node-a"]["pid"] == 111
    assert body["queue"]["by_resource"]["video"] == {"queued": 0, "running": 0}
    # 收尾：把这两个临时进程标旧，后面"离线"类断言不被它们影响
    with get_conn() as conn:
        conn.execute("UPDATE worker_node SET heartbeat_at = "
                     "datetime('now','localtime','-1 day') "
                     "WHERE worker_id LIKE 'e1-node-%'")
