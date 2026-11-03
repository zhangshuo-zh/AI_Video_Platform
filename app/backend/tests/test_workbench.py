"""首页工作台（跨模块待办聚合）：口径与各模块同源，只数看得见的项目。

为什么每条都只看"自己那个项目"的数字：测试共用一个库，别的用例留下的作业、
文档与运行会让全局 `counts` 飘。所以断言一律落在 `projects` 里自己那一行，
再单独验一条"合计＝各行之和"。
"""
import json

import services
import worker
from config import JOBS_DIR
from db import get_conn
from repositories.content import episodes
from repositories.documents import documents as docs
from repositories.jobs import jobs as job_repo
from repositories.pipeline import pipeline_repo
from repositories.projects import projects as project_repo


def _row(c, project_id: int) -> dict:
    data = c.get("/api/workbench").json()
    return next(p for p in data["projects"] if p["id"] == project_id)


def _submit(c, project_id: int, capability: str = "image", **kw):
    body = {
        "project_id": project_id, "capability": capability, "prompt": "工作台用例",
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.01, "text": "一帧"},
    }
    body.update(kw)
    return c.post("/api/jobs", json=body)


def test_只聚合自己看得见的项目(fresh_project, as_role):
    pid = fresh_project("editor")
    editor = as_role("editor")
    assert any(p["id"] == pid for p in editor.get("/api/workbench").json()["projects"])

    outsider = as_role("outsider")
    data = outsider.get("/api/workbench").json()
    assert all(p["id"] != pid for p in data["projects"])
    # 合计不能夹带别人项目的数字：它必须等于各自那几行的和
    for key in ("review_documents", "review_artifacts", "jobs_failed",
                "needs_attention", "runs_active"):
        assert data["counts"][key] == sum(p[key] for p in data["projects"])


def test_文档待签绑当前正文版本(fresh_project, as_role):
    pid = fresh_project("editor")
    c = as_role("editor")
    doc = services.create_document(pid, None, "novel", "工作台稿", "第一句。", "测试账号")
    did = doc["row"]["id"]

    # 没签过 → 待签
    assert _row(c, pid)["review_documents"] == 1

    assert c.post(f"/api/documents/{did}/review-gate").status_code == 201
    signed = c.post(f"/api/documents/{did}/review-sign",
                    json={"anchor": "document", "decision": "approved", "note": ""})
    assert signed.status_code == 200
    assert _row(c, pid)["review_documents"] == 0

    # 正文一改，旧签字落在旧版本上：这一份又回到待签
    version = services.read_content(docs.get(did))["version"]
    saved = c.put(f"/api/documents/{did}/content",
                  json={"content": "改成第二句。", "expected_version": version})
    assert saved.status_code == 200
    assert _row(c, pid)["review_documents"] == 1


def test_产物待签绑产物清单摘要(fresh_project, as_role, clean_queue):
    pid = fresh_project("editor")
    c = as_role("editor")
    job = _submit(c, pid).json()
    assert worker.run_once(f"w-wb-{job['id']}") == job["id"]
    assert _row(c, pid)["review_artifacts"] == 1

    signed = c.post(f"/api/jobs/{job['id']}/review-sign",
                    json={"anchor": "artifact", "decision": "approved"})
    assert signed.status_code == 200
    assert _row(c, pid)["review_artifacts"] == 0

    # 产物文件被清理：清单摘要变了，旧签字不再算到当前这版头上
    rel = json.loads(job_repo.get(job["id"])["result_paths_json"])[0]
    (JOBS_DIR / rel).unlink()
    assert _row(c, pid)["review_artifacts"] == 1


def test_失败与待核对分开计数(fresh_project, as_role, clean_queue):
    pid = fresh_project("editor")
    c = as_role("editor")
    failed_a = _submit(c, pid).json()
    failed_b = _submit(c, pid).json()
    attention = _submit(c, pid).json()
    # 直接改状态：这两个档位平时由 worker 的收尾路径写，不属于本用例要验的东西
    with get_conn() as conn:
        conn.execute("UPDATE job SET status = 'failed' WHERE id IN (?, ?)",
                     (failed_a["id"], failed_b["id"]))
        conn.execute("UPDATE job SET status = 'needs_attention' WHERE id = ?",
                     (attention["id"],))

    row = _row(c, pid)
    assert row["jobs_failed"] == 2
    assert row["needs_attention"] == 1


def test_归档项目不进待办(fresh_project, as_role):
    pid = fresh_project("editor")
    c = as_role("editor")
    services.create_document(pid, None, "novel", "归档前的稿", "一句话。", "测试账号")
    assert _row(c, pid)["review_documents"] == 1

    project_repo.set_status(pid, "archived")
    assert all(p["id"] != pid for p in c.get("/api/workbench").json()["projects"])


def test_进行中的运行只算没收尾的(fresh_project, as_role, people):
    pid = fresh_project("editor")
    c = as_role("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    template = pipeline_repo.latest_templates()[0]
    run = pipeline_repo.create_run(
        project_id=pid, episode_id=eid, template_id=template["id"],
        template_version=template["version"], input_snapshot={},
        idempotency_key=None, created_by=people["editor"]["id"],
        started_at="2026-11-03 10:00:00")

    assert _row(c, pid)["runs_active"] == 1
    pipeline_repo.finish(run["id"])
    assert _row(c, pid)["runs_active"] == 0
