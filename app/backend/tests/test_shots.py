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
from repositories.jobs import jobs as job_repo


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




# ---------- v0.99（迁移 0034）：镜头引用到"哪一套形态" ----------

def _variant(client, aid, name="雨夜外套", variant_type="outfit"):
    resp = client.post(f"/api/assets/{aid}/variants",
                       json={"variant_type": variant_type, "name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_镜头引用形态会自动补上父资产(as_role, fresh_project):
    """只勾衣服没勾人，是界面上最容易发生的手滑。

    这里选择**补**而不是**拒**：拒了会让人以为"形态不能单独选"，
    而补上的后果是删除保护、引用计数、资产库那格"关联 N 镜"三处永远一致。
    悬空的形态引用（父资产不在 asset_ids 里）才是真麻烦——它算得到、看不见。
    """
    pid, eid = _episode(fresh_project)
    c = as_role("editor")
    aid = _asset(c, pid, "燧人")
    vid = _variant(c, aid)

    created = _shot(c, eid, summary="燧人穿上外套", asset_ids=[], variant_ids=[vid])
    assert created["asset_ids"] == [aid]              # 父资产被补进来了
    assert created["variant_ids"] == [vid]
    assert created["variant_labels"][str(vid)] == "燧人 · 雨夜外套"

    # 再更新一次，只留形态、资产列表清空：同样要补回来（不能靠前端记得）
    again = _put(c, created["id"], asset_ids=[], variant_ids=[vid])
    assert again.status_code == 200, again.text
    assert again.json()["asset_ids"] == [aid]


def test_形态引用不能指到别人的形态(as_role, fresh_project, people):
    c = as_role("editor")
    pid_a, eid_a = _episode(fresh_project, "A集")
    aid = _asset(c, pid_a, "甲角色")
    vid = _variant(c, aid)
    pid_b = fresh_project("editor")
    eid_b = episode_repo.create(pid_b, "B集")["id"]
    resp = c.post(f"/api/episodes/{eid_b}/shots", json={
        "summary": "越界引用", "prompt": "", "duration": 5,
        "asset_ids": [], "variant_ids": [vid]})
    assert resp.status_code == 422, resp.text
    assert "形态" in resp.json()["detail"]


def test_只留形态时父资产永远被一起写回(as_role, fresh_project):
    """镜头编辑里"人没了、衣服还挂着"不该存在：服务端一次收敛，不靠前端自觉。"""
    pid, eid = _episode(fresh_project)
    c = as_role("editor")
    aid = _asset(c, pid, "掌灯人")
    vid = _variant(c, aid, "常服")
    sid = _shot(c, eid, asset_ids=[aid], variant_ids=[vid])["id"]
    body = _put(c, sid, asset_ids=[], variant_ids=[vid]).json()
    assert body["asset_ids"] == [aid] and body["variant_ids"] == [vid]
    cleared = _put(c, sid, asset_ids=[], variant_ids=[]).json()
    assert cleared["asset_ids"] == [] and cleared["variant_ids"] == []


def test_改形态会让这一镜标成源已变(as_role, fresh_project, clean_queue):
    """任务书 §11.1「资产变化应正确参与现有源版本判断」里形态那一半。

    反例是"改衣服不用重跑"：换衣服就是换画面依据，旧产物却还挂着已签，
    成片把它拼进时间线时没人知道这一集里同一个人穿了两套衣服。
    """
    from repositories.content import variants as variant_repo
    from services import job_source_stale

    pid, eid = _episode(fresh_project)
    c = as_role("editor")
    aid = _asset(c, pid, "林默")
    vid = _variant(c, aid, "常服")
    import worker
    sid = _shot(c, eid, summary="林默站在门口", asset_ids=[aid], variant_ids=[vid])["id"]
    job = _submit(c, pid, eid, sid).json()
    # 跑到成功才有"最新一条成功视频作业"可判——分镜列表那格只认这一条
    assert worker.run_once(f"w-stale-{job['id']}") == job["id"]
    assert not job_source_stale(job_repo.get(job["id"])), "刚生的不该就是过期的"

    variant_repo.update(vid, variant_type="outfit", name="常服",
                        prompt="袖口磨破了", sort_order=0)
    assert job_source_stale(job_repo.get(job["id"])) is True
    # 分镜列表那一格也要跟着说"源已变"，两处必须同一个答案
    rows = c.get(f"/api/episodes/{eid}/shots").json()
    assert [r["source_stale"] for r in rows if r["id"] == sid] == [True]


def test_已提交作业的指纹不随镜头后续修改被改写(as_role, fresh_project, clean_queue):
    """0034 加的这一列不许回头动历史作业：`source_version` 是提交那一刻冻住的字符串。"""
    pid, eid = _episode(fresh_project)
    c = as_role("editor")
    aid = _asset(c, pid, "燧人")
    vid = _variant(c, aid, "常服")
    sid = _shot(c, eid, asset_ids=[aid], variant_ids=[vid])["id"]
    job_id = _submit(c, pid, eid, sid).json()["id"]
    before = c.get(f"/api/jobs/{job_id}").json()
    assert before["source_version"]

    _put(c, sid, summary="改过的内容", asset_ids=[aid], variant_ids=[])
    after = c.get(f"/api/jobs/{job_id}").json()
    assert after["source_version"] == before["source_version"]


def test_老镜头没有形态列也照常读与判(as_role, fresh_project):
    """兼容性底线：0034 不回填任何一行，读不到就是空清单。"""
    import migrate
    from repositories.content import shots as shot_repo

    pid, eid = _episode(fresh_project)
    c = as_role("editor")
    sid = _shot(c, eid, summary="升级前那样的一镜")     # 请求体里根本没有 variant_ids
    assert sid["variant_ids"] == []
    assert sid["variant_labels"] == {}
    # 仓储直接建的一镜（绕过接口，走的是默认 '[]'）也一样读得回来
    raw = shot_repo.create(eid, "仓储建的", "提示词", 3.0, [])["id"]
    rows = c.get(f"/api/episodes/{eid}/shots").json()
    assert {r["id"] for r in rows} >= {sid["id"], raw}
    assert all(r["variant_ids"] == [] for r in rows)
    # 迁移可重跑且不会给老行塞出内容来
    assert migrate.migrate(verbose=False) == 0
    with get_conn() as conn:
        # 只数这一集：会话共用的库里别的用例留了形态引用，数全表就是测环境不是测行为
        n = conn.execute("SELECT COUNT(*) AS n FROM shot "
                         "WHERE episode_id = ? AND variant_ids <> '[]'",
                         (eid,)).fetchone()["n"]
    assert n == 0
