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
def audio_route(monkeypatch, provider_state):
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
