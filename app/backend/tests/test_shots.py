"""W12：分镜表的编辑、资产引用与「一镜多条作业」。

这一层要守住三件事：
1. **一镜的四个字段能整体改**（内容／提示词／时长／引用），镜号与所属集不动；
2. **引用只认本项目资产与全局资产**——别家项目的 id 在写入口就拦掉，
   否则会出现界面看不见、删除保护却算得到的悬空引用；
3. **一镜可以对应多条作业，但改镜头不会改动作业快照**。
   作业里的提示词与参数是提交那一刻冻住的，编辑只动镜头表。
"""
from db import get_conn
from repositories.content import assets as asset_repo
from repositories.content import episodes as episode_repo


def _episode(fresh_project, title="第 1 集"):
    pid = fresh_project("editor")
    return pid, episode_repo.create(pid, title)["id"]


def _asset(client, pid, name, kind="character", scope="project"):
    body = {"kind": kind, "name": name, "summary": "", "consistency_prompt": "",
            "aliases": [], "tags": [], "scope": scope}
    resp = client.post(f"/api/projects/{pid}/assets", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _shot(client, eid, **overrides):
    body = {"summary": "开场", "prompt": "", "duration": 5, "asset_ids": []}
    body.update(overrides)
    resp = client.post(f"/api/episodes/{eid}/shots", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put(client, shot_id, **overrides):
    body = {"summary": "改过的内容", "prompt": "改过的提示词",
            "duration": 7.5, "asset_ids": []}
    body.update(overrides)
    return client.put(f"/api/shots/{shot_id}", json=body)


def _submit(client, pid, eid, sid, prompt="一镜一条"):
    return client.post("/api/jobs", json={
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "prompt": prompt,
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.0},
    })


# ---------- 创建与整体更新 ----------

def test_新建与更新一镜的四个字段(as_role, fresh_project):
    pid, eid = _episode(fresh_project)
    c = as_role("editor")
    a = _asset(c, pid, "燧人")
    b = _asset(c, pid, "洞口", kind="scene")

    created = _shot(c, eid, summary="燧人蹲在洞口", prompt="雨夜，岩壁",
                    duration=6, asset_ids=[a, b, a])
    assert created["seq"] == 1
    # 重复 id 在写入口就去掉，chips 不会出现两遍
    assert created["asset_ids"] == [a, b]

    resp = _put(c, created["id"], summary="燧人抬头看雨", prompt="冷雨，近景",
                duration=8.5, asset_ids=[b])
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["summary"] == "燧人抬头看雨"
    assert row["prompt"] == "冷雨，近景"
    assert row["duration"] == 8.5
    assert row["asset_ids"] == [b]
    assert row["episode_id"] == eid and row["seq"] == 1

    listed = c.get(f"/api/episodes/{eid}/shots").json()
    assert listed[0]["summary"] == "燧人抬头看雨"
    assert listed[0]["asset_ids"] == [b]


def test_空内容与超时长被拒(as_role, fresh_project):
    _, eid = _episode(fresh_project)
    c = as_role("editor")
    sid = _shot(c, eid)["id"]
    assert _put(c, sid, summary="   ").status_code == 422
    assert _put(c, sid, duration=0).status_code == 422
    assert _put(c, sid, duration=61).status_code == 422


def test_资产引用只认本项目与全局(as_role, fresh_project, people):
    pid, eid = _episode(fresh_project)
    c = as_role("editor")
    own = _asset(c, pid, "本项目的角色")

    # 另一个项目里的资产：id 存在，但这条镜头不该引用它
    other_pid = fresh_project("editor")
    other = _asset(c, other_pid, "别人家的角色")
    resp = _put(c, _shot(c, eid)["id"], asset_ids=[other])
    assert resp.status_code == 422
    assert "不存在" in resp.json()["detail"]

    # 全局资产人人可引用
    global_id = asset_repo.create(None, "scene", "全局的雨夜", "", "")["id"]
    ok = _put(c, _shot(c, eid)["id"], asset_ids=[own, global_id])
    assert ok.status_code == 200, ok.text
    assert ok.json()["asset_ids"] == [own, global_id]

    # 不存在、0、负数都拒
    sid = _shot(c, eid)["id"]
    for bad in ([999999], [0], [-1]):
        assert _put(c, sid, asset_ids=bad).status_code == 422
    # 超过上限
    too_many = list(range(1, 42))
    assert _put(c, sid, asset_ids=too_many).status_code == 422


def test_跨项目看不见就404_只读角色403(as_role, fresh_project):
    pid, eid = _episode(fresh_project)
    sid = _shot(as_role("editor"), eid)["id"]
    assert _put(as_role("outsider"), sid).status_code == 404
    assert _put(as_role("viewer"), sid).status_code == 403


def test_更新会推动项目的最后修改时间(as_role, fresh_project):
    pid, eid = _episode(fresh_project)
    c = as_role("editor")
    sid = _shot(c, eid)["id"]
    with get_conn() as conn:
        conn.execute("UPDATE project SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
                     (pid,))
    assert _put(c, sid).status_code == 200
    with get_conn() as conn:
        after = conn.execute("SELECT updated_at FROM project WHERE id = ?",
                             (pid,)).fetchone()["updated_at"]
    assert after != "2000-01-01 00:00:00"


# ---------- 一镜多条作业与快照 ----------

def test_一镜可以有多条作业_按集与按镜筛选(as_role, clean_queue, fresh_project):
    pid, eid = _episode(fresh_project)
    c = as_role("editor")
    a = _shot(c, eid, summary="A")["id"]
    b = _shot(c, eid, summary="B")["id"]

    for prompt in ("第一版", "第二版"):
        assert _submit(c, pid, eid, a, prompt).status_code == 202
    assert _submit(c, pid, eid, b, "另一镜").status_code == 202

    by_shot = c.get(f"/api/jobs?shot_id={a}").json()
    assert len(by_shot) == 2
    assert {row["shot_id"] for row in by_shot} == {a}
    assert [row["prompt_preview"] for row in by_shot] == ["第二版", "第一版"]

    by_ep = c.get(f"/api/jobs?episode_id={eid}").json()
    assert len(by_ep) == 3
    assert {row["episode_id"] for row in by_ep} == {eid}

    # 看不见这个项目的人，用集或镜查询都是 404，不是空列表
    outsider = as_role("outsider")
    assert outsider.get(f"/api/jobs?shot_id={a}").status_code == 404
    assert outsider.get(f"/api/jobs?episode_id={eid}").status_code == 404


def test_改镜头不会改已提交作业的输入快照(as_role, clean_queue, fresh_project):
    pid, eid = _episode(fresh_project)
    c = as_role("editor")
    created = _shot(c, eid, summary="原来的内容", prompt="原来的提示词")
    submitted = _submit(c, pid, eid, created["id"], "冻结在作业里的提示词")
    assert submitted.status_code == 202
    job_id = submitted.json()["id"]

    before = c.get(f"/api/jobs/{job_id}").json()
    assert before["prompt_snapshot"] == "冻结在作业里的提示词"
    assert before["shot_id"] == created["id"]
    snapshot_params = dict(before["params"])

    resp = _put(c, created["id"], summary="改完的内容", prompt="改完的提示词",
                duration=9, asset_ids=[])
    assert resp.status_code == 200
    assert resp.json()["summary"] == "改完的内容"

    after = c.get(f"/api/jobs/{job_id}").json()
    assert after["prompt_snapshot"] == before["prompt_snapshot"]
    assert after["params"] == snapshot_params
    assert after["shot_id"] == created["id"]
    assert after["queued_at"] == before["queued_at"]

