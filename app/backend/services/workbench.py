import access
import jobfiles

from db import get_conn
from repositories.documents import documents as docs
from repositories.projects import projects as project_repo
from repositories.reviews import reviews
from repositories.workbench import workbench as workbench_repo

from .artifact_review import artifact_version_of_manifest
from .documents import read_content
from .errors import ARTIFACT_ANCHOR, WHOLE_DOC_ANCHOR


# ---------- 首页工作台：跨模块待办聚合 ----------

# 产物待签只看最近这一批可审作业：与审核中心产物队列同一个窗口
# （`reviews.job_queue` 的 limit 也是 200）。数字是"这 200 条里还剩几条没签"，
# 不是全库历史欠账——全量扫会把首页拖慢，而且几年前的作业也不该算进"今天该干什么"。
WORKBENCH_ARTIFACT_WINDOW = 200




def _pending_documents_for(project_ids: list[int]) -> dict[int, int]:
    """一组项目各自的"还没整篇签过的文档数"，三趟批量读完。

    判据与 `_pending_documents` 一字不差：版本令牌现算（`read_content`），
    读不到正文的不算待办。这里只解决**怎么读**——首页列的是全部在产项目，
    逐项目回库在规模基准里量到 25 个项目 159 次、8 个项目 57 次，是随项目数长的。
    """
    if not project_ids:
        return {}
    out = {int(pid): 0 for pid in project_ids}
    rows = docs.list_by_projects(project_ids)
    signs_by_doc = reviews.signs_by_targets(reviews.DOCUMENT,
                                            [int(r["id"]) for r in rows])
    for doc in rows:
        try:
            version = read_content(doc)["version"]
        except Exception:
            continue
        signed = any(s["anchor"] == WHOLE_DOC_ANCHOR and s["base_version"] == version
                     for s in signs_by_doc.get(int(doc["id"]), []))
        if not signed:
            out[int(doc["project_id"])] = out.get(int(doc["project_id"]), 0) + 1
    return out




def _pending_artifacts_for(project_ids: list[int]) -> dict[int, int]:
    """一组项目各自的"待审产物条数"，两趟批量读完（每项目仍只数最近那 200 条）。"""
    if not project_ids:
        return {}
    out = {int(pid): 0 for pid in project_ids}
    rows = reviews.job_queue_for_projects(project_ids,
                                          limit=WORKBENCH_ARTIFACT_WINDOW)
    if not rows:
        return out
    signs_by_job = reviews.signs_by_targets(
        reviews.JOB, [int(r["job_id"]) for r in rows])
    # **一次列完所有产物目录**（2026-11-08 基准测出的热点）。
    # 原来逐条调 `artifact_version(job)`，每条再对每个产物 stat 一次：
    # 1500 镜那一档实测单次打开首页 **18 000 次文件探测**。
    # 现在按作业目录分组、每目录一次 `scandir`，把 N 次 stat 压成 1 次目录读。
    # 摘要口径仍是同一个（`artifact_version_of_manifest`），只是清单来源换了。
    manifests = jobfiles.artifacts_many([r["result_paths_json"] for r in rows])
    for job, manifest in zip(rows, manifests):
        version = artifact_version_of_manifest(manifest)
        signed = any(s["anchor"] == ARTIFACT_ANCHOR and s["base_version"] == version
                     for s in signs_by_job.get(int(job["job_id"]), []))
        if not signed:
            out[int(job["project_id"])] = out.get(int(job["project_id"]), 0) + 1
    return out




def _signed_ready_for(project_ids: list[int]) -> dict[int, int]:
    """各项目「已签字、但还没有更新成片」的镜头数。

    口径写死在这里，前端只读：
    * 镜头要有**成功的视频作业**（`job.status='succeeded'`）；
    * 那条作业被签过（`review_item.target_kind='job'`，决策 approved）；
    * 这一集还没有一版成片是在**签字之后**建的（`composition.created_at >= signed_at`）。

    按镜头算、一条只算一次：同一镜重跑多次，只要最新那版被签且没进成片，就记 1。
    不解析 `timeline_json`——那会把「成片里到底收了哪几镜」变成第二套口径。
    """
    if not project_ids:
        return {}
    marks = ",".join("?" * len(project_ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT e.project_id AS project_id, COUNT(DISTINCT s.id) AS n
                  FROM shot s
                  JOIN episode e ON e.id = s.episode_id
                  JOIN job j ON j.shot_id = s.id
                            AND j.capability = 'video'
                            AND j.status = 'succeeded'
                  JOIN review_item ri ON ri.target_kind = 'job'
                                     AND ri.target_id = j.id
                                     AND ri.decision = 'approved'
                                     AND ri.signed_at IS NOT NULL
                 WHERE e.project_id IN ({marks})
                   AND NOT EXISTS (
                        SELECT 1 FROM composition c
                         WHERE c.episode_id = s.episode_id
                           AND c.created_at >= ri.signed_at)
                 GROUP BY e.project_id""",
            project_ids).fetchall()
    return {int(r["project_id"]): int(r["n"]) for r in rows}




def _throughput_for(project_ids: list[int]) -> dict:
    """本机出片速度：单镜视频耗时、每分钟能出多少秒成片。

    只统计**已成功且有机时**的作业；排队与重试不算进来（它们不是产线速度）。
    `shot_sec` 用的是镜头设定时长——成片实际时长要看 `composition.duration_ms`，
    但那个只对出过成片的集有值，做不出"单镜"口径，所以这里标明折算方式。
    """
    empty = {"samples": 0, "video_avg_sec": None, "image_avg_sec": None,
             "per_minute_sec": None,
             "caliber": "本机已成功作业；排队与重试不计；样本为 0 时不给数"}
    if not project_ids:
        return empty
    marks = ",".join("?" * len(project_ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT j.capability AS capability,
                       COUNT(*) AS n,
                       SUM(j.elapsed_ms) AS total_ms,
                       SUM(COALESCE(s.duration, 0)) AS shot_sec
                  FROM job j LEFT JOIN shot s ON s.id = j.shot_id
                 WHERE j.status = 'succeeded'
                   AND j.elapsed_ms IS NOT NULL AND j.elapsed_ms > 0
                   AND j.project_id IN ({marks})
                 GROUP BY j.capability""",
            project_ids).fetchall()
    agg = {r["capability"]: dict(r) for r in rows}
    video = agg.get("video") or {}
    image = agg.get("image") or {}
    samples = sum(int(r["n"]) for r in rows)
    if not samples:
        return empty
    per_minute = None
    if video.get("total_ms") and video.get("shot_sec"):
        per_minute = round(60 * float(video["shot_sec"]) / (float(video["total_ms"]) / 1000), 1)
    return {
        "samples": samples,
        "video_avg_sec": (round(float(video["total_ms"]) / int(video["n"]) / 1000, 1)
                          if video.get("total_ms") else None),
        "image_avg_sec": (round(float(image["total_ms"]) / int(image["n"]) / 1000, 1)
                          if image.get("total_ms") else None),
        "per_minute_sec": per_minute,
        "caliber": "本机已成功作业的机时；成片秒数按镜头设定时长折算；不含排队与重试",
    }




def _pending_documents(project_id: int) -> int:
    """当前正文版本上还没整篇签字的文档数。

    版本令牌用 `read_content` 现算，和审核中心同一处定义——不在库里另存一份
    正文哈希，否则"文件被手改过、库没跟上"就成了两套事实。
    读不到正文（文件被人手工删了）不算待办：宁可少报一条，
    也不拿一个点进去就 404 的数字催人。
    """
    return _pending_documents_for([project_id]).get(int(project_id), 0)




def _pending_artifacts(project_id: int) -> int:
    """最近这批可审作业里，当前产物清单上还没整条签字的条数。

    产物摘要（`artifact_version`）每次现算：文件被清理或被顶掉，摘要就变，
    旧签字自然不算数——首页不会把"产物早就没了、页面上还挂着通过"算成已签。
    """
    return _pending_artifacts_for([project_id]).get(int(project_id), 0)




def workbench(user) -> dict:
    """首页工作台：按"我看得见的在产项目"聚合待办。只读，不写任何状态。

    每个数字只有一个定义处：
    - `review_documents`／`review_artifacts`：见上面两个函数（版本对不上就不算签过）；
    - `jobs_failed`／`needs_attention`：`job.status` 原值，不做窗口裁剪；
    - `runs_active`：还没收尾、也没被取消的产线运行。
    归档项目不计：归档的意思是"先不挡在眼前"，它不该出现在待办里。
    """
    rows = project_repo.list_visible(user["id"], access.is_manager(user), "active")
    project_ids = [int(r["id"]) for r in rows]
    status_counts = workbench_repo.job_status_counts(project_ids)
    run_counts = workbench_repo.active_run_counts(project_ids)
    pending_docs = _pending_documents_for(project_ids)
    pending_arts = _pending_artifacts_for(project_ids)
    signed_ready = _signed_ready_for(project_ids)
    projects = []
    for row in rows:
        pid = int(row["id"])
        by_status = status_counts.get(pid, {})
        projects.append({
            "id": pid, "name": row["name"],
            "review_documents": pending_docs.get(pid, 0),
            "review_artifacts": pending_arts.get(pid, 0),
            "jobs_failed": by_status.get("failed", 0),
            "needs_attention": by_status.get("needs_attention", 0),
            "runs_active": run_counts.get(pid, 0),
            "signed_ready": signed_ready.get(pid, 0),
        })
    keys = ("review_documents", "review_artifacts", "jobs_failed",
            "needs_attention", "runs_active", "signed_ready")
    return {
        "counts": {key: sum(p[key] for p in projects) for key in keys},
        "projects": projects,
        "artifact_window": WORKBENCH_ARTIFACT_WINDOW,
        # 产能读数与待办同一个接口给：首页只拉一次，不会出现"待办说 12 条、
        # 产能说 3 条样本"这种两个时刻的口径。
        "throughput": _throughput_for(project_ids),
    }
