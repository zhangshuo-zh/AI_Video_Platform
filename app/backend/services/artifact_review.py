import hashlib
import jobfiles
import json
import reviewgate

from models import CAPABILITIES, JOB_STATUSES
from repositories.audio import clips as audio_clips
from repositories.content import shots
from repositories.jobs import jobs as job_repo, retry_reason
from repositories.reviews import reviews

from .errors import ARTIFACT_ANCHOR, JobInvalid, ReviewNotReviewable
from .jobs import job_source_stale
from .review import _review_signature_view


def artifact_version(job) -> str:
    """产物清单摘要：把"审的是哪一版产物"钉成一个可复算的令牌。

    与文档的 `base_version` 同一条道理——签字必须绑在被审的东西上，东西变了
    旧签字就不能算数。这里的"东西"是产物清单：相对路径 ＋ 是否还在 ＋ 字节数。
    产物被清理、被新一次生成顶掉，摘要就变，界面显示"已失配"，
    不会出现"文件早就没了、页面上还挂着通过"。
    """
    return artifact_version_of_manifest(jobfiles.artifacts(job["result_paths_json"]))


def artifact_version_of_manifest(manifest: list[dict]) -> str:
    """由**已算好的产物清单**求摘要。

    单独抽出来是为了让批量路径（首页工作台一次算 200 条）能共用同一套
    摘要口径：那边先用 `jobfiles.artifacts_many()` 一次列完所有目录，
    再逐条调本函数。两处口径若各写一遍，就会出现"工作台说没签、详情页说签了"
    这种最难查的分叉。
    """
    blob = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]




def _artifact_anchors(job) -> list[str]:
    """可签的锚点：整条作业，或某一件产物。下标越界的锚点一律拒。"""
    count = len(jobfiles.artifacts(job["result_paths_json"]))
    return [ARTIFACT_ANCHOR] + [f"{ARTIFACT_ANCHOR}:{i}" for i in range(count)]




def _job_id_of(job) -> int:
    """作业行的主键有两种写法：`job_repo.get()` 给的是 `id`，
    审核队列那条 SQL 为了跟 `shot_id`／`episode_id` 分得清，别成了 `job_id`。
    两个都认，省得调用方记着"这一行是哪儿来的"。"""
    keys = job.keys()
    return job["id"] if "id" in keys else job["job_id"]




def _job_reviewable(job) -> tuple[bool, str]:
    """只有"已成功且有产物"的作业能审。原因要能说出口，不是简单的真假。"""
    if job["status"] != "succeeded":
        return False, (f"这条作业的状态是「{JOB_STATUSES.get(job['status'], job['status'])}」；"
                       f"只有已成功的作业才有产物可审")
    if not jobfiles.artifacts(job["result_paths_json"]):
        return False, "这条作业没有登记任何产物，没有可审的对象"
    return True, ""




def job_review_state(job, can_sign: bool = False) -> dict:
    """一条作业的审核面：产物清单、版本摘要、当前版的签字与计数、是否失配。

    `shot_seq`／`episode_title` 这些是队列 SQL 顺手带出来的；单条详情走
    `job_repo.get()` 时没有这几列，所以按"有没有这个键"取，不给就留空——
    比为了两列去多跑一次创作中心的查询划算。
    """
    job_id = _job_id_of(job)
    keys = job.keys()          # 队列 SQL 多带的那两列，详情那条没有
    reviewable, reason = _job_reviewable(job)
    version = artifact_version(job) if reviewable else ""
    all_signs = reviews.signs(reviews.JOB, job_id) if reviewable else []
    signs = [row for row in all_signs if row["base_version"] == version]
    return {
        "job_id": job_id, "project_id": job["project_id"],
        "capability": job["capability"],
        "capability_label": CAPABILITIES.get(job["capability"], job["capability"]),
        "execution_mode": job["execution_mode"],
        "provider_code": job["provider_code"], "model_code": job["model_code"],
        "episode_id": job["episode_id"],
        "episode_title": (job["episode_title"] if "episode_title" in keys else "") or "",
        "shot_id": job["shot_id"],
        "shot_seq": job["shot_seq"] if "shot_seq" in keys else None,
        "status": job["status"],
        "status_label": JOB_STATUSES.get(job["status"], job["status"]),
        "finished_at": job["finished_at"] or "",
        "artifacts": jobfiles.artifacts(job["result_paths_json"]),
        "base_version": version,
        "reviewable": reviewable, "reason": reason,
        # 有签字落在别的版本上＝产物换过了，那些签字不能再算到这一版头上
        "stale": len(all_signs) != len(signs),
        "counts": reviews.counts(reviews.JOB, job_id, version),
        "signatures": [_review_signature_view(row) for row in signs],
        "anchors": _artifact_anchors(job) if reviewable else [],
        "can_sign": bool(can_sign and reviewable),
    }




def job_review_queue(project_id: int) -> list[dict]:
    """项目里的产物审核队列。签字批量取一次，不为每条作业各查一趟。"""
    rows = reviews.job_queue(project_id)
    signs = reviews.signs_for_jobs(project_id, [r["job_id"] for r in rows])
    by_job: dict[int, list] = {}
    for row in signs:
        by_job.setdefault(row["target_id"], []).append(row)
    out = []
    for row in rows:
        state = job_review_state(row)
        mine = by_job.get(row["job_id"], [])
        # 队列里的 job 一定是 succeeded 且有产物（SQL 已经筛过），
        # 所以这里的 stale 只看"签字绑的是不是当前这版产物"
        state["stale"] = any(s["base_version"] != state["base_version"] for s in mine)
        state["signatures"] = [_review_signature_view(s) for s in mine
                               if s["base_version"] == state["base_version"]]
        state["counts"] = reviews.counts(reviews.JOB, row["job_id"], state["base_version"])
        out.append(state)
    return out




def job_checklist(job) -> dict:
    """一条作业的审核清单：自动项现算，人工项从 `review_check` 读。

    三个来源都是别人的事实，这里只拼不判：
    镜头行与素材来自仓储、过期判定走 `job_source_stale`（与取片同一条口径）、
    媒体读探测复用 `composition.validators.inspect`。
    """
    import reviewkit
    from repositories.review_checks import review_checks

    # `clips` / `shots` 是模块级已经导进来的（见文件头 `from repositories.audio
    # import clips`），这里不再重复 import。原来这两行用了 `as clip_repo` /
    # `as shot_repo` 的别名，理由是这个函数里有个局部变量叫 `clips`
    # （下面装的是"这一镜的音频片段"，与仓储同名不同物）——所以这里显式用
    # `audio_clips` 这个名字，既不遮蔽局部变量、也不再从别的层重复导入。
    shot = shots.get(int(job["shot_id"])) if job["shot_id"] else None
    clips: list[dict] = []
    if job["project_id"] and job["episode_id"] and job["shot_id"]:
        clips = [c for c in audio_clips.list(int(job["project_id"]),
                                             episode_id=int(job["episode_id"]))
                 if int(c["shot_id"] or 0) == int(job["shot_id"])]
    stale = bool(shot is not None and job_source_stale(job, shot=shot))
    stored = review_checks.get(review_checks.TARGET_KIND, int(job["id"]))
    return reviewkit.checklist(job, shot=shot, clips=clips, stale=stale, checks=stored)




def job_checklist_save(job, payload, user) -> dict:
    """保存人工清单结论。项不属于这一类能力的清单就整批拒——
    这个口子写的是 `review_check.code`，放过野 code 等于往枚举里掺脏值。"""
    import reviewkit
    from repositories.review_checks import review_checks

    reviewable, reason = _job_reviewable(job)
    if not reviewable:
        raise ReviewNotReviewable(reason)
    codes = [i.code for i in payload.items]
    bad = reviewkit.validate_codes(job["capability"], codes)
    if bad:
        raise reviewgate.ReviewInvalid(
            f"这些项不在 {job['capability']} 的清单里：" + "、".join(bad))
    version = artifact_version(job)
    for item in payload.items:
        review_checks.upsert(
            project_id=int(job["project_id"]), target_kind=review_checks.TARGET_KIND,
            target_id=int(job["id"]), code=item.code, state=item.state,
            note=item.note, base_version=version, user_id=user["id"])
    return job_checklist(job)




def job_retry_batch(project_id: int, episode_id: int, *, user_id: int | None = None,
                    capability: str = "video", limit: int = 50) -> dict:
    """把这一集里**卡在失败／已取消**的那些镜头一次重投（任务书 §15 的 P2「批量制作」）。

    四条规矩，都是把已经付过代价的教训写死：

    1. **只重投本机的失败与取消。** 云端逐次确认计费是硬约束（§3）：批量入口一次点下去
       可能再扣 N 笔钱，所以云端那一档在这里**逐条拒**并指回单条重试那一路；
       `needs_attention` 同理不自动重投（结果未知时重试就是可能再扣一次钱，
       与 `worker.CLOUD_UNCERTAIN_CODES` 同一条判断）；还在队列里的也不动——
       排两条同样的活是纯浪费。
    2. **一条上限 50。** 与批量审核同一口径，超了直接拒，不做"自动截断"：
       截断会让人以为整集都重投了。
    3. **逐条落地、逐条报原因**，用的是 `jobs.retry()` 那**同一个**函数
       ——新行复制参数快照、`attempt` 加一、原行的日志与错误一个字段都不动。
       批量入口不许另写一套复制逻辑，否则两条路会走出两种历史。
    4. **看不见的集走 404**：归属判断由路由的 `access.ensure` 做，
       这一层不吞异常（P1 批量审核那一轮撞过的坑：把 404 咽成逐条原因，
       批量入口就成了探测别人项目的尺子）。
    """
    if not 1 <= int(limit) <= 50:
        raise JobInvalid("一次最多重投 50 个镜头；再多就分批")
    latest = job_repo.latest_jobs_by_shot(int(episode_id), capability)
    retried: list[dict] = []
    skipped: list[dict] = []
    for shot_id in sorted(latest):
        row = latest[shot_id]
        # 能不能重投只有 `repositories.jobs.retry_reason()` 一处说了算。
        # 批量额外收紧两档（结果未知、云端计费）用开关传进去，不再手抄状态集合。
        reason = retry_reason(row, allow_cloud=False, allow_attention=False)
        if reason:
            skipped.append({"shot_id": shot_id, "job_id": int(row["id"]),
                            "reason": reason})
            continue
        if len(retried) >= int(limit):
            skipped.append({"shot_id": shot_id, "job_id": int(row["id"]),
                            "reason": "已到一次 50 条的上限，剩下的请再点一次"})
            continue
        fresh = job_repo.retry(row, user_id)
        retried.append({"shot_id": shot_id, "job_id": int(row["id"]),
                        "new_job_id": int(fresh["id"]),
                        "attempt": int(fresh["attempt"] or 1)})
    return {"project_id": int(project_id), "episode_id": int(episode_id),
            "capability": capability,
            "counts": {"shots": len(latest), "retried": len(retried),
                       "skipped": len(skipped)},
            "retried": retried, "skipped": skipped,
            "rule": "只重投本机 failed／cancelled；云端与待核对逐条拒并写明原因，上限 50 条"}




def job_review_batch(items, decision: str, note: str, user, sign_one) -> dict:
    """批量签字：**每条一个自己的事务，逐条报结果**。

    为什么不做成全批一个事务：一条被拦（产物换了版、清单没看完、状态不是已成功）
    是常态而不是意外。整批回滚的话，人会为了"把剩下的签完"反复点，
    而每次点都可能改判已经签过的那几条；逐条落地才是可数的事实。
    为什么不允许批内混结论：`decision` 是整批同一个——批量通过与批量驳回是两种动作。

    逐条报的只有**业务性拒绝**；归属判断（看不见／没权限）原样抛 403/404，
    批量不许变成探测他项目内容的尺子。

    `sign_one` 是调用方给的"签一条"回调（路由传的是带可见性判断的那一个），
    这样批量入口用的权限与归属判断和单条**完全同一条路径**，不开第二个写口子。
    """
    results = []
    for one in items:
        entry = {"job_id": int(one.job_id), "ok": False, "reason": ""}
        try:
            state = sign_one(one)
            counts = state.get("counts") or {}
            entry.update(ok=True, decision=decision,
                         signed=bool(counts.get("approved")) or decision == "approved")
            results.append(entry)
        # 只接住**业务性拒绝**（清单没看完、这条不可审、版本失配）——那些是逐条报的内容。
        # 其余异常一律原样抛出：`access.ensure` 抛的 403/404 如果被塞进结果里，
        # 接口就变成 200 ＋ 一句"作业不存在"，批量入口于是成了一把
        # 探测别人项目里存在什么的尺子——"看不见就是不存在"靠状态码兑现，不靠文案。
        # 这一寸是 `tests/test_guards.py` 的越权用例当场抓出来的。
        except (ReviewNotReviewable, reviewgate.ReviewInvalid, JobInvalid) as exc:
            # 业务性拒绝（清单没看完、这条不可审、版本失配）才是逐条报的东西
            entry["reason"] = str(exc)
            results.append(entry)
    signed = sum(1 for r in results if r["ok"])
    return {"total": len(results), "signed": signed, "refused": len(results) - signed,
            "decision": decision, "results": results}




def job_review_sign(job, payload, user) -> dict:
    """给一条作业的产物签字。锚点必须在当前产物清单里，版本必须对得上。

    产线的 `image_review`／`shot_review` 节点就调这里——**审核结论只有这一个
    写入口**，产线只拿 `review_item.id` 当引用，不复制结论（不变量 3）。
    """
    import reviewkit

    reviewable, reason = _job_reviewable(job)
    if not reviewable:
        raise ReviewNotReviewable(reason)
    # 清单要求默认只在批量与界面（见 `reviewkit.ENFORCE_CHECKLIST_ON_SINGLE_SIGN`）。
    # 开关一翻，这里就是唯一需要改的地方：所有签通过都先过清单，
    # 包括产线人工关卡与验收脚本——所以翻之前要把那两处一起改。
    if (payload.decision == "approved" and reviewkit.ENFORCE_CHECKLIST_ON_SINGLE_SIGN
            and job["capability"] in reviewkit.HUMAN_ITEMS):
        checklist = job_checklist(job)
        if not checklist["can_approve"]:
            raise reviewgate.ReviewInvalid(
                f"人工清单还没看完，不能签通过：{checklist['hint']}")
    version = artifact_version(job)
    if payload.anchor not in _artifact_anchors(job):
        raise reviewgate.ReviewInvalid("这个锚点不在本次产物清单里")
    reviews.upsert_sign(
        project_id=job["project_id"], target_kind=reviews.JOB,
        target_id=_job_id_of(job),
        gate_id=None, base_version=version, anchor=payload.anchor,
        decision=payload.decision, note=payload.note, user_id=user["id"])
    return job_review_state(job, can_sign=True)
