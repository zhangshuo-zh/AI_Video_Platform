"""W14：校验器 gate、版本绑定与按锚点签字。"""
import pytest

import reviewgate
import services
from repositories.projects import projects as project_repo
from repositories.reviews import reviews


BAD = ("第一句还留着 TODO。\n"
       "第二句没有收尾也没有标点\n"
       + "很长" * 70 + "。\n")


def _make_doc(fresh_project, content=BAD, title="W14 审核稿"):
    pid = fresh_project("editor")
    made = services.create_document(pid, None, "novel", title, content, "测试账号")
    return made["row"], services.read_content(made["row"]), pid


def test_规则只报三类坏味道并给字符锚点():
    status, findings, summary = reviewgate.run(BAD)
    assert status == "failed"
    rules = {f["rule"] for f in findings}
    assert {"placeholder", "unterminated", "long_sentence"} <= rules
    for f in findings:
        assert f["anchor"].startswith("c")
        assert BAD[int(f["anchor"][1:]):].startswith(f["quote"][:12])
    assert summary["errors"] >= 2 and summary["warnings"] >= 1


def test_空稿直接未通过():
    status, findings, _summary = reviewgate.run("   ")
    assert status == "failed"
    assert findings[0]["anchor"] == "document"


def test_跑校验_签字_改判_队列(as_role, fresh_project):
    doc, data, pid = _make_doc(fresh_project)
    c = as_role("editor")

    ran = c.post(f"/api/documents/{doc['id']}/review-gate")
    assert ran.status_code == 201, ran.text
    gate = ran.json()
    assert gate["status"] == "failed" and gate["base_version"] == data["version"]
    assert gate["can_sign"] is True
    anchor = gate["findings"][0]["anchor"]

    signed = c.post(f"/api/documents/{doc['id']}/review-sign",
                    json={"anchor": anchor, "decision": "returned",
                          "note": "这句先改掉"})
    assert signed.status_code == 200, signed.text
    state = signed.json()
    assert state["counts"]["returned"] == 1
    assert state["signatures"][0]["signed_by_name"]
    assert state["signatures"][0]["signed_at"]

    # 同一锚点改判：只留一条，不叠历史
    again = c.post(f"/api/documents/{doc['id']}/review-sign",
                   json={"anchor": anchor, "decision": "approved", "note": "改过了"})
    assert again.status_code == 200
    assert again.json()["counts"] == {"approved": 1, "rejected": 0, "returned": 0}

    queue = c.get(f"/api/projects/{pid}/reviews")
    assert queue.status_code == 200
    row = next(x for x in queue.json() if x["doc_id"] == doc["id"])
    assert row["gate_status"] == "failed" and row["signed"] == 1
    assert row["stale"] is False


def test_锚点必须来自报告(as_role, fresh_project):
    doc, _data, _pid = _make_doc(fresh_project)
    c = as_role("editor")
    c.post(f"/api/documents/{doc['id']}/review-gate")
    bad = c.post(f"/api/documents/{doc['id']}/review-sign",
                 json={"anchor": "c999999", "decision": "approved"})
    assert bad.status_code == 422


def test_正文改过之后旧报告与旧签字都不算新版本(as_role, fresh_project):
    doc, data, _pid = _make_doc(fresh_project)
    c = as_role("editor")
    gate = c.post(f"/api/documents/{doc['id']}/review-gate").json()
    anchor = gate["findings"][0]["anchor"]
    c.post(f"/api/documents/{doc['id']}/review-sign",
           json={"anchor": anchor, "decision": "approved"})

    services.save_content(doc, "改过的新正文。\n", data["version"], "测试账号",
                          as_role.id("editor"))
    got = c.get(f"/api/documents/{doc['id']}/review-gate").json()
    assert got["stale"] is True and got["can_sign"] is False
    blocked = c.post(f"/api/documents/{doc['id']}/review-sign",
                     json={"anchor": anchor, "decision": "approved"})
    assert blocked.status_code == 409
    # 新版本没有签字：计数按当前版本查，旧签字不会混进来
    assert reviews.counts(doc["id"], got["current_version"]) == {
        "approved": 0, "rejected": 0, "returned": 0}


def test_没有签字权限不能签(as_role, fresh_project, people):
    doc, _data, pid = _make_doc(fresh_project)
    project_repo.add_member(pid, people["viewer"]["id"], people["editor"]["id"])
    viewer = as_role("viewer")
    assert viewer.get(f"/api/documents/{doc['id']}/review-gate").status_code == 200
    assert viewer.post(f"/api/documents/{doc['id']}/review-gate").status_code == 403
    assert viewer.post(f"/api/documents/{doc['id']}/review-sign",
                       json={"anchor": "document", "decision": "approved"}).status_code == 403
    assert viewer.get(f"/api/projects/{pid}/reviews").status_code == 200
