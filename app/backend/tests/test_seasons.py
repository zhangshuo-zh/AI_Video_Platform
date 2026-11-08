"""阶段 E-2：整季排产、并发闸门、继续失败集、暂停/取消、权限。

真实调度用的是单集产线那套（create_run／advance_run），这里只验"季"这一层：
  * 一次只按 max_active_runs 起几集，其余等；
  * 继续失败集只重试失败节点，已成功镜头的 Job 与产物不动；
  * 看不见的项目回 404，viewer 能读不能发。
"""
from __future__ import annotations

import uuid


import pipeline
import seasons
import services
import worker
from db import get_conn
from repositories.content import episodes, shots
from repositories.jobs import jobs as job_repo
from repositories.projects import projects as project_repo
from repositories.reviews import reviews
from test_pipeline import FAST, _template, mock_route  # noqa: F401 —— mock_route 是夹具

GATE = {"nodes": [
    {"key": "read_project", "type": "project_input", "depends_on": []},
    {"key": "read_episode", "type": "episode_input", "depends_on": ["read_project"]},
    {"key": "check_shots", "type": "shot_validate", "depends_on": ["read_episode"]},
    {"key": "gate", "type": "human_gate", "depends_on": ["check_shots"]},
]}

GEN = {"nodes": [
    {"key": "read_project", "type": "project_input", "depends_on": []},
    {"key": "read_episode", "type": "episode_input", "depends_on": ["read_project"]},
    {"key": "check_shots", "type": "shot_validate", "depends_on": ["read_episode"]},
    {"key": "keyframe", "type": "image_generate", "depends_on": ["check_shots"],
     "fan_out": "shot", "params": FAST},
    {"key": "check_frame", "type": "artifact_validate", "depends_on": ["keyframe"],
     "fan_out": "shot"},
]}


def _project(people, n_episodes: int = 3, n_shots: int = 1) -> dict:
    pid = project_repo.create(f"整季_{uuid.uuid4().hex[:6]}", "", [],
                              people["editor"]["id"])["id"]
    eps = []
    for index in range(n_episodes):
        eid = episodes.create(pid, f"第 {index + 1} 集")["id"]
        for s in range(n_shots):
            shots.create(eid, f"镜 {s + 1}", f"提示词 {s + 1}", 5.0, [])
        doc = services.create_document(pid, eid, "script", f"第{index + 1}集剧本",
                                       "燧人蹲在洞口。\n", "测试账号")["row"]
        eps.append({"episode_id": eid, "document": doc})
    return {"project_id": pid, "episodes": eps,
            "user_id": people["editor"]["id"]}


def _sign(pid: int, doc, people) -> None:
    version = services.read_content(doc)["version"]
    reviews.upsert_sign(
        project_id=pid, target_kind=reviews.DOCUMENT, target_id=doc["id"],
        gate_id=None, base_version=version, anchor="document",
        decision="approved", note="整季测试签字",
        user_id=people["editor"]["id"])


def _pump(run_id: int, rounds: int = 12) -> dict:
    for _ in range(rounds):
        pipeline.advance_run(run_id)
        while worker.run_once("w-season") is not None:
            pass
    return pipeline.run_view(run_id)


def test_按上限逐集推进并在签字后收口(mock_route, people, clean_queue):
    setup = _project(people, n_episodes=3)
    tid = _template(GATE)
    body = seasons.create(setup["project_id"], tid, setup["user_id"],
                          max_active_runs=1)
    assert body["max_active_runs"] == 1
    assert body["counts"] == {"total": 3, "completed": 0, "failed": 0,
                              "pending": 2, "running": 1}
    first = next(i for i in body["items"] if i["run_id"])
    others = [i for i in body["items"] if not i["run_id"]]
    assert all(i["status"] == "pending" for i in others)
    assert first["status"] == "waiting_review"

    # 再 tick 也不会多起一集：第一集还没结束
    seasons.tick()
    assert len(seasons.view(body["id"])["items"]) == 3
    assert seasons.view(body["id"])["counts"]["running"] == 1

    # 签掉第一集 → 收口 → tick 自动起第二集
    _sign(setup["project_id"], setup["episodes"][0]["document"], people)
    _pump(first["run_id"])
    seasons.tick()
    body = seasons.view(body["id"])
    assert body["items"][0]["status"] == "completed"
    assert sum(1 for i in body["items"] if i["run_id"]) == 2

    # 暂停：不再起新的；已经起的运行继续按自己的节拍（暂停季≠杀作业）
    second = next(i for i in body["items"] if i["run_id"] and i["status"] == "waiting_review")
    seasons.pause(body["id"])
    _sign(setup["project_id"], setup["episodes"][1]["document"], people)
    _pump(second["run_id"])
    seasons.tick()
    assert sum(1 for i in seasons.view(body["id"])["items"] if i["run_id"]) == 2

    # 恢复 → 起第三集；签完收口
    seasons.resume(body["id"])
    body = seasons.view(body["id"])
    assert sum(1 for i in body["items"] if i["run_id"]) == 3
    third = next(i for i in body["items"] if i["run_id"] and i["status"] == "waiting_review")
    _sign(setup["project_id"], setup["episodes"][2]["document"], people)
    _pump(third["run_id"])
    seasons.tick()
    body = seasons.view(body["id"])
    assert body["status"] == "completed", body["status_label"]
    assert body["finished"] is True
    assert body["progress"]["percent"] == 100.0


def test_上限两集时同时起两条运行(mock_route, people, clean_queue):
    setup = _project(people, n_episodes=2)
    body = seasons.create(setup["project_id"], _template(GATE), setup["user_id"],
                          max_active_runs=2)
    runs = [i for i in body["items"] if i["run_id"]]
    assert len(runs) == 2
    assert body["counts"]["running"] == 2


def test_继续失败集只重试失败镜头(mock_route, people, clean_queue):
    """两镜同时排队，第二镜先失败；继续失败集应只补这一镜的新作业，
    第一镜的 Job／产物原样不动。"""
    setup = _project(people, n_episodes=1, n_shots=2)
    tid = _template(GEN)
    body = seasons.create(setup["project_id"], tid, setup["user_id"])
    item = body["items"][0]
    sids = pipeline_repo_shots(setup)
    # 建季时已经派了两条关键帧作业，还没跑；把第二镜那条改成会失败
    run = pipeline.run_view(item["run_id"])
    nodes = {n["node_key"]: n for n in run["nodes"]}
    bad_job = nodes[f"keyframe#s{sids[1]}"]["job_id"]
    good_job = nodes[f"keyframe#s{sids[0]}"]["job_id"]
    with get_conn() as conn:
        conn.execute("UPDATE job SET params_json = ? WHERE id = ?",
                     ('{"sleep": 0.01, "fail": true}', bad_job))
    _pump(item["run_id"])
    run = pipeline.run_view(item["run_id"])
    nodes = {n["node_key"]: n for n in run["nodes"]}
    assert nodes[f"keyframe#s{sids[0]}"]["status"] == "completed"
    assert nodes[f"keyframe#s{sids[1]}"]["status"] == "failed"
    assert nodes[f"check_frame#s{sids[1]}"]["status"] == "blocked"
    before_good = job_repo.get(good_job)
    assert before_good["status"] == "succeeded"
    good_paths = before_good["result_paths_json"]

    seasons.tick()
    body = seasons.view(body["id"])
    assert body["items"][0]["status"] == "failed"
    assert "模拟失败" in body["items"][0]["error_message"]

    # 人把原因修掉（这里改回不失败的参数），继续失败集
    with get_conn() as conn:
        conn.execute("UPDATE job SET params_json = ? WHERE id = ?",
                     ('{"sleep": 0.01}', bad_job))
    out = seasons.retry_failed(body["id"], setup["user_id"])
    assert out["retry"]["nodes"] == 1
    assert out["retry"]["started_runs"] == 0
    # 第一镜没被重做：Job 还是原来那条，产物清单原样
    node1 = pipeline.pipeline_repo.get_node(nodes[f"keyframe#s{sids[0]}"]["id"])
    assert node1["job_id"] == good_job
    assert job_repo.get(good_job)["result_paths_json"] == good_paths
    # 第二镜确实领到了一条新作业（旧作业作为历史保留）
    node2 = pipeline.pipeline_repo.get_node(nodes[f"keyframe#s{sids[1]}"]["id"])
    assert node2["job_id"] != bad_job
    assert job_repo.get(bad_job)["status"] == "failed"
    _pump(item["run_id"])
    run = pipeline.run_view(item["run_id"])
    # 重试过一镜 ⇒ 运行标"完成（有跳过或重试）"，季照同一口径
    assert run["status"] == "completed_with_errors", run["status_label"]
    seasons.tick()
    assert seasons.view(body["id"])["status"] == "completed_with_errors"


def pipeline_repo_shots(setup):
    return [int(row["id"])
            for row in shots.list_by_episode(setup["episodes"][0]["episode_id"])]


def test_取消整季会取消未结束的单集运行(mock_route, people, clean_queue):
    setup = _project(people, n_episodes=2)
    body = seasons.create(setup["project_id"], _template(GATE), setup["user_id"])
    run_id = next(i["run_id"] for i in body["items"] if i["run_id"])
    body = seasons.cancel(body["id"], "验收取消")
    assert body["status"] == "cancelled"
    assert pipeline.run_view(run_id)["status"] == "cancelled"
    seasons.tick()
    assert seasons.view(body["id"])["status"] == "cancelled"


def test_权限与归属(as_role, people, fresh_project, mock_route):  # noqa: F811
    setup = _project(people, n_episodes=1)
    tid = _template(GATE)
    editor = as_role("editor")
    created = editor.post(f"/api/projects/{setup['project_id']}/seasons",
                          json={"template_id": tid})
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert editor.get(f"/api/projects/{setup['project_id']}/seasons").status_code == 200
    assert editor.get(f"/api/seasons/{sid}").status_code == 200

    project_repo.add_member(setup["project_id"], people["viewer"]["id"],
                            people["editor"]["id"])
    viewer = as_role("viewer")
    assert viewer.get(f"/api/seasons/{sid}").status_code == 200
    assert viewer.post(f"/api/seasons/{sid}/pause").status_code == 403

    outsider = as_role("outsider")
    assert outsider.get(f"/api/seasons/{sid}").status_code == 404
    assert outsider.get(f"/api/projects/{setup['project_id']}/seasons").status_code == 404
    assert outsider.post(f"/api/seasons/{sid}/cancel",
                         json={"reason": "x"}).status_code == 404

    # 模板不存在 ⇒ 404；不是自己的集 ⇒ 422
    assert editor.post(f"/api/projects/{setup['project_id']}/seasons",
                       json={"template_id": 999999}).status_code == 404
