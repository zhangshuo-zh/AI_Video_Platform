"""W15：音频素材与同一条 Job 队列。

真实 VoxCPM2 / ACE-Step 运行时不在本机，测试不假装跑模型：
- 提交链路验的是"文件里落一条 job、素材关联到集与镜"；
- 产物回抄用一个手写 succeeded 的 job 模拟，证明路径能回到素材；
- 健康不过时明确拒绝，不静默换模型。
"""
import pytest

import services
from db import get_conn
from repositories.jobs import jobs as job_repo


@pytest.fixture
def audio_route(monkeypatch, provider_state, register_tier):
    monkeypatch.setattr(services.adapters, "health",
                        lambda row: {"state": "ok", "detail": "测试可达",
                                     "adapter": None, "key_env": ""})
    with get_conn() as conn:
        conn.execute("UPDATE provider_config SET is_default = 0 WHERE capability IN ('tts','music')")
        conn.execute(
            """UPDATE provider_config
                  SET provider = 'voxcpm2', model = 'voxcpm2',
                      enabled = 1, is_default = 1, key_env = ''
                WHERE capability = 'tts' AND backend = 'local'""")
        conn.execute(
            """UPDATE provider_config
                  SET provider = 'ace-step', model = 'ace-step-1.5',
                      enabled = 1, is_default = 1, key_env = ''
                WHERE capability = 'music' AND backend = 'local'""")
    # 显式提交要求这一档在模型仓库登记过（2026-11-07 起）：音频这两类都走显式
    register_tier("tts", "local", "voxcpm2", "voxcpm2")
    register_tier("music", "local", "ace-step", "ace-step-1.5")


def _make_clip(client, pid, ep, shot, kind="dialogue", text="你迟了三年。"):
    return client.post(f"/api/projects/{pid}/audio-clips", json={
        "episode_id": ep, "shot_id": shot, "kind": kind,
        "speaker": "掌灯人", "text": text, "notes": ""})


def test_建素材_提交同队列_产物回抄(as_role, seeded, audio_route, clean_queue):
    c = as_role("editor")
    pid, ep, sid = seeded["project_id"], seeded["episode_id"], seeded["shot_id"]
    made = _make_clip(c, pid, ep, sid)
    assert made.status_code == 201, made.text
    clip = made.json()
    assert clip["status"] == "draft" and clip["episode_seq"] == 1 and clip["shot_seq"] == 1

    sent = c.post(f"/api/audio-clips/{clip['id']}/submit")
    assert sent.status_code == 202, sent.text
    body = sent.json()
    assert body["status"] == "queued" and body["job_id"]
    job = job_repo.get(body["job_id"])
    assert job["capability"] == "tts" and job["kind"] == "tts"
    assert job["shot_id"] == sid and job["episode_id"] == ep
    assert job["input_refs_json"].find(str(clip["id"])) >= 0

    # 模拟 worker 跑完：只写一条 succeeded + 相对产物路径，不执行模型
    claimed = job_repo.claim_next("w15-test", 60)
    assert claimed is not None and claimed["id"] == job["id"]
    job_repo.succeed(job["id"], "w15-test", "", ["1/result.wav"], 12, return_code=0)

    got = c.get(f"/api/projects/{pid}/audio-clips").json()
    row = next(x for x in got if x["id"] == clip["id"])
    assert row["status"] == "ready" and row["path"] == "1/result.wav"

    # 已就绪不能再改文本，也不能重复提交
    assert c.patch(f"/api/audio-clips/{clip['id']}", json={
        "kind": "dialogue", "speaker": "x", "text": "改了", "notes": ""}).status_code == 409
    assert c.post(f"/api/audio-clips/{clip['id']}/submit").status_code == 409


def test_音乐走同一队列_不同能力(as_role, seeded, audio_route, clean_queue):
    c = as_role("editor")
    pid, ep, sid = seeded["project_id"], seeded["episode_id"], seeded["shot_id"]
    clip = _make_clip(c, pid, ep, sid, kind="music", text="雨夜长街，低音弦乐").json()
    sent = c.post(f"/api/audio-clips/{clip['id']}/submit")
    assert sent.status_code == 202, sent.text
    job = job_repo.get(sent.json()["job_id"])
    assert job["capability"] == "music" and job["provider_code"] == "ace-step"


def test_环境音明确标实验态_不提交(as_role, seeded, audio_route):
    c = as_role("editor")
    pid, ep, sid = seeded["project_id"], seeded["episode_id"], seeded["shot_id"]
    clip = _make_clip(c, pid, ep, sid, kind="sfx", text="雨声与灯芯爆响").json()
    bad = c.post(f"/api/audio-clips/{clip['id']}/submit")
    assert bad.status_code == 422 and "实验态" in bad.json()["detail"]


def test_关联校验与权限(as_role, seeded, audio_route, people, fresh_project):
    c = as_role("editor")
    pid, ep, sid = seeded["project_id"], seeded["episode_id"], seeded["shot_id"]
    other = fresh_project("editor")
    bad = c.post(f"/api/projects/{other}/audio-clips", json={
        "episode_id": ep, "shot_id": sid, "kind": "dialogue", "text": "x"})
    assert bad.status_code == 422

    assert as_role("viewer").get(f"/api/projects/{pid}/audio-clips").status_code == 404
    assert as_role("outsider").get(f"/api/projects/{pid}/audio-clips").status_code == 404

    clip = _make_clip(c, pid, ep, sid).json()
    assert c.delete(f"/api/audio-clips/{clip['id']}").status_code == 204


def test_作业成功但产物抄不到时不许写成已就绪(as_role, seeded, audio_route, clean_queue):
    """`audio_sync` 原来只看"作业成没成功"，不看**抄没抄到路径**。

    于是 `job_repo.get()` 回 None（作业行被收掉）或 `result_paths_json` 是空数组时，
    素材照样转成 `status=ready, path=null`——列表里出现一条"已就绪但点不开"的配音，
    而界面已经替平台说了"这条能用"。这一档必须落 failed 并让人重跑。
    """
    c = as_role("editor")
    pid, ep, sid = seeded["project_id"], seeded["episode_id"], seeded["shot_id"]
    clip = _make_clip(c, pid, ep, sid, text="这一条产物是空的。").json()
    sent = c.post(f"/api/audio-clips/{clip['id']}/submit").json()
    job_id = sent["job_id"]

    claimed = job_repo.claim_next("w15-empty", 60)
    assert claimed is not None and claimed["id"] == job_id
    job_repo.succeed(job_id, "w15-empty", "", [], 0, return_code=0)   # 成功，但没有产物

    rows = c.get(f"/api/projects/{pid}/audio-clips").json()
    row = next(x for x in rows if x["id"] == clip["id"])
    assert row["status"] == "failed", f"没有产物却报 ready：{row['status']}"
    assert row["path"] is None
    # failed 之后还能改文本重走一遍：这一档不是死状态
    assert c.patch(f"/api/audio-clips/{clip['id']}", json={
        "kind": "dialogue", "speaker": "掌灯人", "text": "换个说法再来一次",
        "notes": ""}).status_code == 200
