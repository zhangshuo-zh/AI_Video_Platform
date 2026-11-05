"""项目生产工作台（0037）：上下文、镜头设定五栏、九阶段进度与下一步任务。

要守住的三句话，逐条钉死：

1. **上下文不是权限**。`/api/context` 只存"上次站在哪一集"，读写都要过可见性；
   看不见的项目一律 404，集不属于项目是 422，项目被删/被移出后 GET 当场作废——
   不能让人顺着旧上下文读到别人的镜头计数。
2. **镜头设定五栏不进源版本指纹**。补填一个景别不该让这一镜已经签过字的产物判过期；
   这条一旦被改坏，代价是整集签字集体失效，所以必须有用例钉着。
3. **工作台的数字全部现算，且没接线的东西不给动作**。音频阶段可以显示"需要处理"，
   但不能进"下一步任务"——`runners.build()` 里没有 tts/music 分支，
   给一个点了必失败的按钮比不写这一阶段更糟。
"""
import json

import jobfiles
import production
import services
from repositories.content import episodes, shots
from repositories.reviews import reviews
from repositories.context import context
from repositories.jobs import jobs as job_repo
from repositories.projects import projects as project_repo

# ---------------------------------------------------------------- 建数据


def _episode(client, pid, title="第 1 集"):
    r = client.post(f"/api/projects/{pid}/episodes", json={"title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _shot(client, eid, summary="一镜", **extra):
    body = {"summary": summary, "prompt": "镜头提示词", "duration": 5,
            "asset_ids": [], **extra}
    r = client.post(f"/api/episodes/{eid}/shots", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _succeeded_video_job(pid, eid, sid, provider="fastmetal",
                         model="fastmetal-1.3b-qad"):
    """一条成功的本地视频作业，挂在具体镜头上——工作台数"几镜有片"就靠它。"""
    row = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "kind": "video_local", "execution_mode": "local",
        "provider_code": provider, "model_code": model,
        "adapter_version": "test", "prompt_snapshot": "工作台用", "params": {},
        "input_refs": []})
    claimed = job_repo.claim_next("pc-worker", 60)
    assert claimed is not None and claimed["id"] == row["id"]
    _logs, outputs = jobfiles.ensure_dirs(row["id"])
    target = outputs / "out.mp4"
    target.write_bytes(b"fake-video-bytes")
    job_repo.succeed(row["id"], "pc-worker", "", [jobfiles.relative(target)], 1)
    return job_repo.get(row["id"])


# ---------------------------------------------------------------- 0037 地基


def test_迁移0037_建上下文表并给镜头加五栏(app):
    from db import get_conn
    with get_conn() as conn:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(shot)")]
    assert "user_context" in tables
    for name in ("shot_size", "camera_move", "aspect", "style", "negative_prompt"):
        assert name in cols, f"shot 缺列 {name}"


def test_镜头设定五栏往返(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    created = _shot(c, eid, shot_size="特写", camera_move="推",
                    aspect="16:9", style="平涂赛璐璐",
                    negative_prompt="多余手指")
    assert created["shot_size"] == "特写"
    assert created["camera_move"] == "推"
    assert created["aspect"] == "16:9"
    assert created["style"] == "平涂赛璐璐"
    assert created["negative_prompt"] == "多余手指"
    again = c.put(f"/api/shots/{created['id']}", json={
        "summary": "一镜", "prompt": "镜头提示词", "duration": 5, "asset_ids": [],
        "shot_size": "中景", "camera_move": "", "aspect": "16:9",
        "style": "平涂赛璐璐", "negative_prompt": ""}).json()
    assert again["shot_size"] == "中景"
    assert again["camera_move"] == ""
    assert again["negative_prompt"] == ""


def test_补填景别不让已签产物判过期(as_role, fresh_project):
    """源版本指纹只认内容／提示词／时长／引用资产／形态。

    这条如果被改坏，代价是"填一个景别 → 整集签字失效"，
    所以钉死在指纹上，而不是靠界面记得别乱填。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    sid = _shot(c, eid)["id"]
    row = shots.get(sid)
    before = services.shot_source_version(row)
    c.put(f"/api/shots/{sid}", json={
        "summary": "一镜", "prompt": "镜头提示词", "duration": 5, "asset_ids": [],
        "shot_size": "全景", "camera_move": "摇", "aspect": "4:3",
        "style": "厚涂", "negative_prompt": "模糊"})
    after = services.shot_source_version(shots.get(sid))
    assert before == after, "镜头设定五栏不该进源版本指纹"
    # 但改了提示词就必须变——证明这不是"指纹坏了所以永远不变"
    c.put(f"/api/shots/{sid}", json={
        "summary": "一镜", "prompt": "换了提示词", "duration": 5, "asset_ids": []})
    assert services.shot_source_version(shots.get(sid)) != before


# ---------------------------------------------------------------- 上下文


def test_上下文往返与可见性闸(as_role, people, fresh_project):
    editor = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(editor, pid)
    assert editor.put("/api/context", json={"project_id": pid,
                                            "episode_id": eid}).status_code == 200
    got = editor.get("/api/context").json()
    assert got == {"project_id": pid, "episode_id": eid}

    outsider = as_role("outsider")
    # 别人的项目：连"它存在"都不说
    assert outsider.put("/api/context", json={"project_id": pid,
                                              "episode_id": None}).status_code == 404
    # 集不属于这个项目：422，说人话
    other = fresh_project("editor")
    r = editor.put("/api/context", json={"project_id": other, "episode_id": eid})
    assert r.status_code == 422 and "不属于该项目" in r.json()["detail"]


def test_项目没了上下文跟着作废(as_role, fresh_project):
    """删项目要管理员（`project:delete` 只授 admin），所以这条用 admin 的项目。

    上下文里留着一个看不见/已删除的项目 id 是最坏的一种"缓存"：
    工作台会拿着它去算数字，接口 404，界面就变成"打开工作台就报错"。
    """
    admin = as_role("admin")
    pid = fresh_project("admin")
    eid = _episode(admin, pid)
    admin.put("/api/context", json={"project_id": pid, "episode_id": eid})
    name = project_repo.get(pid)["name"]
    assert admin.delete(f"/api/projects/{pid}?confirm={name}").status_code == 204
    cleared = admin.get("/api/context").json()
    # `user_context.project_id` 是 ON DELETE SET NULL：项目没了，上下文自己就空了，
    # 不会留下一个"看得见 id、算不出数字"的悬空引用。集那一列同理。
    assert cleared["project_id"] is None and cleared["episode_id"] is None


def test_上下文清空是合法操作(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    c.put("/api/context", json={"project_id": pid, "episode_id": None})
    assert c.put("/api/context", json={"project_id": None,
                                       "episode_id": None}).status_code == 200
    assert c.get("/api/context").json()["project_id"] is None


# ---------------------------------------------------------------- 九阶段


def test_生产总览九阶段与数字一致(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    s1 = _shot(c, eid, "第一镜")
    s2 = _shot(c, eid, "第二镜")
    _succeeded_video_job(pid, eid, s1["id"])

    body = c.get(f"/api/projects/{pid}/production?episode_id={eid}").json()
    stages = {s["key"]: s for s in body["board"]["stages"]}
    assert [s["key"] for s in body["board"]["stages"]] == [
        "script", "parse", "assets", "storyboard", "video",
        "audio", "review", "compose", "deliver"], "九阶段顺序与键名是界面契约"
    assert stages["storyboard"]["total"] == 2
    assert stages["video"]["total"] == 2
    assert stages["video"]["done"] == 1
    # 有产物但没签字 → 审核阶段就是待办，数字要能对上
    assert stages["review"]["total"] == 1
    assert stages["review"]["pending"] == 1
    assert stages["compose"]["status"] == production.PENDING
    assert body["overview"]["episodes"] == 1
    # 接口把阶段定义也带出来，界面就不必在 JS 里再抄一份键名与顺序
    assert [s["key"] for s in body["stages"]] == [s["key"] for s in production.STAGES]


def test_下一步任务来自真实缺口且不含未接线的音频(as_role, fresh_project, clean_queue):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    sid = _shot(c, eid)["id"]

    tasks = c.get(f"/api/projects/{pid}/production?episode_id={eid}"
                  ).json()["board"]["next_tasks"]
    keys = {t["stage"] for t in tasks}
    assert "storyboard" in keys or "video" in keys, "有缺口就该给下一步"
    assert "audio" not in keys, "配音执行器没接线，不该把人往那儿指"

    # 全做完不该编出一条任务：签字＋成片都齐时，任务清单只该剩真实缺口
    _succeeded_video_job(pid, eid, sid)
    from repositories.reviews import reviews
    job = job_repo.latest_succeeded_videos_by_shot(eid)[sid]
    reviews.upsert_sign(project_id=pid, target_kind="job", target_id=job["id"],
                        gate_id=None, base_version="", anchor="x",
                        decision="approved", note="测试签字",
                        user_id=people_id(c))
    after = c.get(f"/api/projects/{pid}/production?episode_id={eid}"
                  ).json()["board"]["stages"]
    review = next(s for s in after if s["key"] == "review")
    assert review["pending"] == 0 and review["status"] == production.DONE


def people_id(client) -> int:
    """从会话里拿当前用户 id：签字要写 signed_by，测试不硬编 people 表。"""
    return client.get("/api/auth/me").json()["user"]["id"]


def test_工作台是只读的(as_role, fresh_project):
    """打开工作台不许改任何状态——它是"看"，不是"做"。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    _shot(c, eid)
    before = _snapshot()
    c.get(f"/api/projects/{pid}/production?episode_id={eid}")
    c.get("/api/context")
    assert _snapshot() == before


def _snapshot() -> dict:
    from db import get_conn
    with get_conn() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                for t in ("shot", "job", "review_item", "composition",
                          "audio_clip", "user_context")}


# ---------------------------------------------------------------- P2 生产统计


def _video_job(pid, eid, sid, elapsed_ms, encode_s=None, worker="stat-worker",
               fail_with=None):
    """一条本地视频作业，跑到终态；`encode_s` 写进 `adapter_metrics` 模拟冷/热提示词。"""
    row = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "kind": "video_local", "execution_mode": "local",
        "provider_code": "fastmetal", "model_code": "fastmetal-1.3b-qad",
        "adapter_version": "test", "prompt_snapshot": "统计用", "params": {},
        "input_refs": []})
    claimed = job_repo.claim_next(worker, 60)
    assert claimed is not None and claimed["id"] == row["id"]
    _logs, outputs = jobfiles.ensure_dirs(row["id"])
    target = outputs / "out.mp4"
    target.write_bytes(b"fake-video-bytes")
    if fail_with:
        job_repo.fail(row["id"], worker, fail_with, "验收用的失败", None)
        return job_repo.get(row["id"])
    resources = {"adapter_metrics": {"prompt_encode_s": encode_s}} if encode_s is not None else None
    job_repo.succeed(row["id"], worker, "", [jobfiles.relative(target)],
                     elapsed_ms, resources=resources)
    return job_repo.get(row["id"])


def test_生产统计从HTTP层到得了界面(as_role, fresh_project):
    """统计挂在工作台同一条接口上，所以这条断言必须走 HTTP——
    上一轮 `diagnosis` 就是在 `run_view()` 里在、出了 `response_model` 就没了。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    shot = _shot(c, eid)
    _video_job(pid, eid, shot["id"], 30_000, encode_s=0.007)
    data = c.get(f"/api/projects/{pid}/production?episode_id={eid}").json()
    assert "stats" in data, "界面读不到统计，后端算好也没用"
    stats = data["stats"]
    assert stats["jobs_total"] == 1
    assert stats["by_status"]["succeeded"] == 1
    video = stats["by_capability"]["video"]
    assert video["total"] == 1 and video["succeeded"] == 1
    assert video["avg_ms"] == 30_000, "均值只按量得出耗时的条数算"


def test_统计的金额与成本账同一条口径(as_role, fresh_project):
    """`money.by_currency` 必须等于 `jobs.cost_summary` 的和——
    在工作台里再 SUM 一次就是第二本账，两处迟早对不上。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    shot = _shot(c, eid)
    row = _video_job(pid, eid, shot["id"], 1000, encode_s=0.01)
    cost = job_repo.cost_summary([pid])
    expected = {}
    for item in cost["rows"]:
        key = item["currency"] or "—"
        expected[key] = round(expected.get(key, 0) + int(item["total"] or 0) / 1e6, 4)
    stats = c.get(f"/api/projects/{pid}/production?episode_id={eid}").json()["stats"]
    assert stats["money"]["by_currency"] == expected
    assert stats["money"]["cost_scope"].startswith("与成本账同一条")
    assert row["status"] == "succeeded"


def test_原因分布用的是异常目录的中文名(as_role, fresh_project):
    """「驳回都集中在哪一类」是质量分析最有用的一栏，但码表不能再抄一遍：
    标签只从 `error_catalog` 取。"""
    import error_catalog

    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    shot = _shot(c, eid)
    _video_job(pid, eid, shot["id"], 500, fail_with="run_timeout")
    stats = c.get(f"/api/projects/{pid}/production?episode_id={eid}").json()["stats"]
    codes = {row["error_code"]: row for row in stats["top_error_codes"]}
    assert "run_timeout" in codes
    assert codes["run_timeout"]["n"] == 1
    assert codes["run_timeout"]["label"] == error_catalog.lookup("run_timeout")["label"]
    assert stats["by_status"]["failed"] == 1


def test_冷热带提示词的耗时分开算(as_role, fresh_project):
    """本机实测：同一条提示词第二次编码 0.007s，全新的提示词 82s（UMT5 那一次）。
    均值把这个差吞掉，排产就会低估一集新提示词的真实耗时——所以两边分开给。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    s1, s2, s3 = (_shot(c, eid, "一"), _shot(c, eid, "二"), _shot(c, eid, "三"))
    _video_job(pid, eid, s1["id"], 112_000, encode_s=82.0)
    _video_job(pid, eid, s2["id"], 31_000, encode_s=0.007)
    _video_job(pid, eid, s3["id"], 33_000, encode_s=0.007)
    stats = c.get(f"/api/projects/{pid}/production?episode_id={eid}").json()["stats"]
    est = stats["estimate"]
    assert est["cold_prompt_count"] == 1 and est["warm_prompt_count"] == 2
    assert est["cold_extra_ms"] > 70_000, "新提示词多出来的编码时间要量得出量级"
    assert est["sample_jobs"] == 3 and est["remaining_shots"] == 0
    assert est["minutes_if_all_new_prompts"] is None, "没有待跑镜头时不给上界"


def test_没有已完成作业时不给推算值(as_role, fresh_project):
    """这一条是防"编一个数"：没有样本就不能显示「还要 45 分钟」这种话。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    _shot(c, eid)
    _shot(c, eid)
    stats = c.get(f"/api/projects/{pid}/production?episode_id={eid}").json()["stats"]
    est = stats["estimate"]
    assert est["remaining_shots"] == 2
    assert est["minutes"] is None and est["sample_jobs"] == 0
    assert "还没有已完成的视频作业" in est["basis"]
    # 算不出来的两件事必须自己说清楚，而不是缺省不显示
    assert any("质量标准" in item for item in stats["not_computed"])
    assert any("不补位" in item or "MC1" in item for item in stats["not_computed"])


# ---------------------------------------------------------------- P2 协作读数


def _authored_job(pid, eid, sid, *, author, elapsed_ms=1000):
    """一条指定提交人的成功作业（`created_by` 是这张表唯一的归属事实）。"""
    row = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "kind": "video_local", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "adapter_version": "test",
        "prompt_snapshot": "协作读数", "params": {}, "input_refs": [],
        "created_by": author})
    worker = f"team-{author}"
    assert job_repo.claim_next(worker, 60)["id"] == row["id"]
    _logs, outputs = jobfiles.ensure_dirs(row["id"])
    target = outputs / "out.mp4"
    target.write_bytes(b"fake-video-bytes")
    job_repo.succeed(row["id"], worker, "", [jobfiles.relative(target)], elapsed_ms)
    return job_repo.get(row["id"])


def test_协作读数只数提交与签字不排产量(as_role, people, fresh_project):
    """两个限制必须跟着数字一起出现：作业条数≠产量，签得少≠质量差。

    平台能查到的只有 `job.created_by` 与 `review_item.signed_by` 这类弱引用
    （设计稿 §8 明写不做操作审计），所以这里既不给"完成率"也不给排名。
    """
    import pytest  # noqa: F401  （与文件其余部分一致：不需要 client 也能跑）

    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    s1, s2 = _shot(c, eid, "一"), _shot(c, eid, "二")
    editor = people["editor"]["id"]
    admin = people["admin"]["id"]
    j1 = _authored_job(pid, eid, s1["id"], author=editor, elapsed_ms=30_000)
    _authored_job(pid, eid, s2["id"], author=admin, elapsed_ms=60_000)

    admin_client = as_role("admin")
    anchors = admin_client.get(f"/api/jobs/{j1['id']}/review").json()["anchors"]
    # `anchors` 是锚点字符串清单（`artifact` 或 `artifact:<下标>`），不是对象清单
    admin_client.post(f"/api/jobs/{j1['id']}/review-sign", json={
        "anchor": anchors[0] if anchors else "artifact",
        "decision": "approved", "note": "协作读数用例"})

    stats = c.get(f"/api/projects/{pid}/production?episode_id={eid}").json()["stats"]
    team = stats["team"]
    by_user = {m["user_id"]: m for m in team["members"]}
    assert by_user[editor]["submitted"] == 1 and by_user[editor]["succeeded"] == 1
    assert by_user[editor]["busy_ms"] == 30_000
    assert by_user[admin]["submitted"] == 1
    assert by_user[admin]["signs"].get("approved") == 1
    assert by_user[editor]["signs"] == {}
    # 边界：这一栏不合成比率、不排名，也不给"绩效"字段
    joined = str(team)
    assert "rate" not in joined and "rank" not in joined
    assert any("不等于产量" in x for x in team["caveats"])
    assert any("操作审计" in x for x in team["caveats"])


def test_没人做过任何事时不给空表格(as_role, fresh_project):
    """零成员比摆一张空表更诚实：统计那一栏没有读数就说明白。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    stats = c.get(f"/api/projects/{pid}/production?episode_id={eid}").json()["stats"]
    assert stats["team"]["members"] == []
    assert stats["jobs_total"] == 0


def test_资产影响按批量读_回库次数不随引用镜头数增长(as_role, fresh_project, monkeypatch):
    """规模基准（600 资产／1 500 镜头那一档）量到的一条真缺陷，在这儿钉住。

    `asset_impact` 原来在循环里逐条 `shots.get()` 与 `reviews.signs()`：
    引用镜头从 25 涨到 75 时，同一条读路径的回库从 18 涨到 54。
    改成"循环外两次 IN 批量"之后，这条用例不许再看见逐条调用——
    断言写的是**调用次数**而不是结果对不对，因为结果两种写法都对，
    只有次数能证明这条路径在几百镜时还站得住。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = _episode(c, pid)
    aid = c.post(f"/api/projects/{pid}/assets", json={
        "kind": "character", "name": "阿昭", "summary": "",
        "consistency_prompt": "黑发、灰袍", "aliases": [], "tags": [],
        "scope": "project"}).json()["id"]
    shot_ids = [_shot(c, eid, f"第 {i + 1} 镜", asset_ids=[aid])["id"]
                for i in range(20)]
    for sid in shot_ids[:10]:          # 一半有产物：签字那一档也要真被批量读到
        _succeeded_video_job(pid, eid, sid)

    seen = {"per_get": 0, "per_signs": 0, "batch_shots": 0, "batch_signs": 0}
    orig_get, orig_signs = shots.get, reviews.signs
    orig_many, orig_sb = shots.get_many, reviews.signs_by_targets
    monkeypatch.setattr(shots, "get", lambda i: (
        seen.__setitem__("per_get", seen["per_get"] + 1), orig_get(i))[1])
    monkeypatch.setattr(reviews, "signs", lambda *a, **k: (
        seen.__setitem__("per_signs", seen["per_signs"] + 1), orig_signs(*a, **k))[1])
    monkeypatch.setattr(shots, "get_many", lambda ids: (
        seen.__setitem__("batch_shots", seen["batch_shots"] + 1), orig_many(ids))[1])
    monkeypatch.setattr(reviews, "signs_by_targets", lambda kind, ids: (
        seen.__setitem__("batch_signs", seen["batch_signs"] + 1),
        orig_sb(kind, ids))[1])

    out = production.asset_impact(aid)
    assert out["counts"]["referencing"] == 20
    assert out["counts"]["never_generated"] == 10
    assert seen["batch_shots"] == 1 and seen["batch_signs"] == 1, seen
    assert seen["per_get"] == 0 and seen["per_signs"] == 0, (
        f"循环里又逐条回库了：{seen}")
