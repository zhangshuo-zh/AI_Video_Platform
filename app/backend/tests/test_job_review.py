"""产物审核（0025／裁定 D1）：给"已成功作业的产物"签字。

为什么单开一份测试：这一档与文档审核共用一张表、一套签字语义，但**版本令牌完全不同**
——文档是正文 sha256，产物是清单摘要（相对路径＋是否还在＋字节数）。
最容易出错的地方正是"产物没了／换了，旧签字还算不算数"，所以这里逐条反证。

作业一律用假适配器真跑一遍（`worker.run_once`）：产物是真的落在临时目录里的文件，
不是往库里塞一个假路径——塞假路径的话，"产物被清理后签字失配"这条根本测不到。
"""
import json

import services
import worker
from config import JOBS_DIR
from db import get_conn
from repositories.content import shots
from repositories.documents import documents as docs
from repositories.jobs import jobs as job_repo
from repositories.projects import projects as project_repo
from repositories.reviews import reviews


def _submit(c, project_id, capability="image", **kw):
    body = {
        "project_id": project_id, "capability": capability, "prompt": "关键帧测试",
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.02, "text": "一帧"},
    }
    body.update(kw)
    return c.post("/api/jobs", json=body)


def _run_one(c, project_id, capability="image", **kw):
    """提交一条假适配器作业并真跑完，返回作业 id。"""
    job = _submit(c, project_id, capability, **kw).json()
    assert worker.run_once(f"w-review-{job['id']}") == job["id"]
    return job["id"]


def test_只有已成功的图像视频作业进队列(as_role, clean_queue, seeded):
    c = as_role("editor")
    pid = seeded["project_id"]
    image_job = _run_one(c, pid, "image", shot_id=seeded["shot_id"],
                         episode_id=seeded["episode_id"])
    video_job = _run_one(c, pid, "video")
    # 文本作业即使成功也不进产物审核队列：它审的是正文，归文档那一档
    _run_one(c, pid, "text")
    # 失败的作业没有产物，不该出现在"可以审"的列表里
    failed = _submit(c, pid, "image", params={"sleep": 0.0, "fail": True}).json()
    worker.run_once(f"w-review-{failed['id']}")
    assert job_repo.get(failed["id"])["status"] == "failed"

    queue = c.get(f"/api/projects/{pid}/job-reviews").json()
    by_id = {row["job_id"]: row for row in queue}
    assert image_job in by_id and video_job in by_id
    assert failed["id"] not in by_id
    assert "text" not in {row["capability"] for row in queue}

    row = by_id[image_job]
    assert row["capability_label"] == "图像" and row["reviewable"] is True
    assert len(row["artifacts"]) == 1 and row["artifacts"][0]["exists"]
    # 镜头号与集号由队列 SQL 一次带出，界面不必再问一次创作中心
    assert row["shot_seq"] == shots.get(seeded["shot_id"])["seq"]
    assert row["episode_title"] == "第 1 集"
    # 锚点清单＝整条 ＋ 每一件产物；界面按它给选项，不让前端自己编下标
    assert row["anchors"] == ["artifact", "artifact:0"]


def test_签字_改判_计数(as_role, clean_queue, seeded):
    c = as_role("editor")
    jid = _run_one(c, seeded["project_id"], "image")

    got = c.post(f"/api/jobs/{jid}/review-sign",
                 json={"anchor": "artifact", "decision": "approved",
                       "note": "构图可以"}).json()
    assert got["counts"] == {"approved": 1, "rejected": 0, "returned": 0}
    assert got["signatures"][0]["decision"] == "approved"
    assert got["signatures"][0]["note"] == "构图可以"
    assert got["stale"] is False

    # 同一版同一锚点重签＝改判，不叠第二条历史
    again = c.post(f"/api/jobs/{jid}/review-sign",
                   json={"anchor": "artifact", "decision": "rejected",
                         "note": "手画歪了"}).json()
    assert again["counts"] == {"approved": 0, "rejected": 1, "returned": 0}
    assert len(again["signatures"]) == 1
    rows = reviews.signs(reviews.JOB, jid)
    assert len(rows) == 1 and rows[0]["decision"] == "rejected"

    # 换一个锚点（审某一件产物）是另一条签字，两者并存
    per_artifact = c.post(f"/api/jobs/{jid}/review-sign",
                          json={"anchor": "artifact:0", "decision": "approved"}).json()
    assert per_artifact["counts"] == {"approved": 1, "rejected": 1, "returned": 0}
    assert len(reviews.signs(reviews.JOB, jid)) == 2


def test_锚点必须在产物清单里(as_role, clean_queue, seeded):
    c = as_role("editor")
    jid = _run_one(c, seeded["project_id"], "image")
    r = c.post(f"/api/jobs/{jid}/review-sign",
               json={"anchor": "artifact:7", "decision": "approved"})
    assert r.status_code == 422 and "锚点" in r.json()["detail"]
    # 文档那一套的字符偏移锚点（c12）在产物这里不成立
    r2 = c.post(f"/api/jobs/{jid}/review-sign",
                json={"anchor": "c12", "decision": "approved"})
    assert r2.status_code == 422


def test_没成功或没产物的作业不能签(as_role, clean_queue, seeded):
    c = as_role("editor")
    pid = seeded["project_id"]

    queued = _submit(c, pid, "image").json()          # 只入队，不执行
    r = c.post(f"/api/jobs/{queued['id']}/review-sign",
               json={"anchor": "artifact", "decision": "approved"})
    assert r.status_code == 409 and "状态" in r.json()["detail"]

    state = c.get(f"/api/jobs/{queued['id']}/review").json()
    assert state["reviewable"] is False and state["reason"]
    assert state["base_version"] == "" and state["anchors"] == []
    assert state["can_sign"] is False

    # 成功但产物清单为空：一样不给签，不留一条"看起来通过了"的空签字
    empty = _submit(c, pid, "image").json()
    worker.run_once(f"w-review-{empty['id']}")
    with get_conn() as conn:
        conn.execute("UPDATE job SET result_paths_json = '[]' WHERE id = ?",
                     (empty["id"],))
    r2 = c.post(f"/api/jobs/{empty['id']}/review-sign",
                json={"anchor": "artifact", "decision": "approved"})
    assert r2.status_code == 409 and "产物" in r2.json()["detail"]
    # 清单空了就不在队列里，界面不会摆一条点不动的行
    queue = c.get(f"/api/projects/{pid}/job-reviews").json()
    assert empty["id"] not in {row["job_id"] for row in queue}


def test_产物被清理后旧签字失配(as_role, clean_queue, seeded):
    """产物审核与文档审核同一条道理的落点：被审的东西变了，签字就不能算数。"""
    c = as_role("editor")
    jid = _run_one(c, seeded["project_id"], "image")
    signed = c.post(f"/api/jobs/{jid}/review-sign",
                    json={"anchor": "artifact", "decision": "approved"}).json()
    version_before = signed["base_version"]
    assert version_before and signed["stale"] is False

    rel = json.loads(job_repo.get(jid)["result_paths_json"])[0]
    (JOBS_DIR / rel).unlink()

    after = c.get(f"/api/jobs/{jid}/review").json()
    assert after["base_version"] != version_before      # 摘要随"文件还在不在"变
    assert after["artifacts"][0]["exists"] is False
    assert after["stale"] is True                        # 旧签字落在旧版上
    assert after["counts"] == {"approved": 0, "rejected": 0, "returned": 0}
    assert after["signatures"] == []
    # 失配不等于不能审：产物清单还在（只是文件没了），人要能重新判一次
    assert after["reviewable"] is True and after["can_sign"] is True
    queue = c.get(f"/api/projects/{seeded['project_id']}/job-reviews").json()
    mine = next(row for row in queue if row["job_id"] == jid)
    assert mine["stale"] is True and mine["signatures"] == []


def test_权限与归属(as_role, clean_queue, seeded, fresh_project, people):
    pid = seeded["project_id"]
    editor = as_role("editor")
    jid = _run_one(editor, pid, "image")
    assert editor.get(f"/api/jobs/{jid}/review").json()["can_sign"] is True

    # viewer 能看队列与详情，不能签。成员挂在**另一个项目**上：seeded 是 session 级
    # 共用的，把它改成"有 viewer 成员"会让后面 test_jobs 那条"viewer 看不见＝404"
    # 变成 200——那不是它要测的东西被改坏了，是我把公共夹具弄脏了。
    other = fresh_project("editor")
    project_repo.add_member(other, people["viewer"]["id"], people["editor"]["id"])
    viewer = as_role("viewer")
    assert viewer.get(f"/api/projects/{other}/job-reviews").status_code == 200
    # 看得见的项目里能读；看不见的项目一律 404，不泄露作业存在
    assert viewer.get(f"/api/projects/{pid}/job-reviews").status_code == 404
    got = viewer.get(f"/api/jobs/{jid}/review")
    assert got.status_code == 404
    # 签字这里是 403 而不是 404：权限点在依赖里先判，项目归属那一道在函数体里后判。
    # 全仓都是这个顺序（viewer 连"这类事能不能做"都不满足，先告诉他不能做），
    # 不改口径，只把期望写对。
    assert viewer.post(f"/api/jobs/{jid}/review-sign",
                       json={"anchor": "artifact", "decision": "approved"}
                       ).status_code == 403
    # 同一个 viewer 在自己参与的项目里能读能看，但签字仍要 review:sign
    vjid = _run_one(editor, other, "image")
    vgot = viewer.get(f"/api/jobs/{vjid}/review")
    assert vgot.status_code == 200 and vgot.json()["can_sign"] is False
    assert viewer.post(f"/api/jobs/{vjid}/review-sign",
                       json={"anchor": "artifact", "decision": "approved"}
                       ).status_code == 403

    # 外人（另一个 editor，不是这个项目的人）：一律 404，不泄露作业存在
    outsider = as_role("outsider")
    assert outsider.get(f"/api/projects/{pid}/job-reviews").status_code == 404
    assert outsider.get(f"/api/jobs/{jid}/review").status_code == 404
    assert outsider.post(f"/api/jobs/{jid}/review-sign",
                         json={"anchor": "artifact", "decision": "approved"}
                         ).status_code == 404


def test_删文档连带清掉它的签字(as_role, fresh_project, people):
    """0025 把 doc_id 换成多态列之后，级联不再由外键白送，必须代码里补。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = services.create_document(pid, None, "novel", "签字级联测试",
                                   "燧人蹲在洞口。\n", "测试账号")["row"]
    data = services.read_content(doc)
    reviews.upsert_sign(project_id=pid, target_kind=reviews.DOCUMENT,
                        target_id=doc["id"], gate_id=None,
                        base_version=data["version"], anchor="document",
                        decision="approved", note="级联测试",
                        user_id=people["editor"]["id"])
    assert len(reviews.signs(reviews.DOCUMENT, doc["id"])) == 1

    assert c.delete(f"/api/documents/{doc['id']}").status_code == 204
    assert docs.get(doc["id"]) is None
    # 文档没了，签字也不能留着指向空气
    assert reviews.signs(reviews.DOCUMENT, doc["id"]) == []


def test_文档签字与产物签字互不串档(as_role, clean_queue, fresh_project, people):
    """同一个数字 id 在两档里是两个不同对象：文档 N 与作业 N 不能互相认签字。

    用自己的项目：seeded 是公共夹具，往里加一份不删的文档，
    项目列表那列"文档数"就会被这条测试改掉（test_projects 会红）。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    jid = _run_one(c, pid, "image")
    doc = services.create_document(pid, None, "novel", "串档测试",
                                   "第二份稿子。\n", "测试账号")["row"]
    data = services.read_content(doc)
    reviews.upsert_sign(project_id=pid, target_kind=reviews.DOCUMENT,
                        target_id=jid,        # 故意拿作业的 id 当文档 id 用
                        gate_id=None, base_version=data["version"],
                        anchor="document", decision="rejected", note="串档探针",
                        user_id=people["editor"]["id"])

    state = c.get(f"/api/jobs/{jid}/review").json()
    assert state["signatures"] == [], "文档那一档的签字串到作业上了"
    assert state["counts"] == {"approved": 0, "rejected": 0, "returned": 0}
    # 反过来也成立：那条探针只在 document 档里读得到，作业档一条也没有
    assert len(reviews.signs(reviews.DOCUMENT, jid)) == 1
    assert reviews.signs(reviews.JOB, jid) == []
