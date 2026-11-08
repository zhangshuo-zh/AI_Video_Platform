import jobfiles
import json
import services

from repositories.documents import documents as docs
from repositories.jobs import jobs as job_repo
from repositories.reviews import reviews




def _node_map(nodes) -> dict[str, dict]:
    return {row["node_key"]: dict(row) for row in nodes}



def _find_approval(node: dict, node_input: dict):
    """这一格等的是谁的签字。文档确认看文档那一档，产物审核看作业那一档。"""
    if node["node_type"] == "human_gate":
        doc_id = node_input.get("doc_id")
        if not doc_id:
            return None
        doc = docs.get(doc_id)
        if doc is None:
            return None
        try:
            current = services.read_content(doc)["version"]
        except Exception:
            return None
        signs = reviews.signs(reviews.DOCUMENT, doc_id, current)
    else:
        job_id = node_input.get("job_id") or node["job_id"]
        if not job_id:
            return None
        job = job_repo.get(job_id)
        if job is None:
            return None
        version = services.artifact_version(job)
        signs = reviews.signs(reviews.JOB, job_id, version)
    for sign in signs:
        if sign["decision"] in ("approved", "rejected"):
            return dict(sign)
    return None




def _idempotency_key(run, node: dict) -> str:
    """确定性幂等键：同一条运行的同一个节点、同一次重试，键永远一样。

    这样即使有人绕过原子转态重放提交，`job.idempotency_key` 的 UNIQUE 也只会有
    一条作业——附件 §6.3 要的"不重复提交"由三层各自兜住，不靠其中任何一层单独成立。
    """
    return f"pl-{run['id']}-{node['node_key']}-a{int(node['retry_count'] or 0) + 1}"




def _upstream_image(node: dict, by_key: dict) -> str | None:
    """顺着依赖往上找一张关键帧，返回经过边界校验的绝对路径。

    直接依赖通常是审核节点（它自己不挂 Job），所以要往上多走几层；
    只认 `image_generate` 的产物——把音频或视频文件当参考图喂进去是另一种事故。
    """
    seen: set[str] = set()
    stack = _deps_of(node)
    while stack:
        key = stack.pop()
        if key in seen:
            continue
        seen.add(key)
        up = by_key.get(key)
        if not up:
            continue
        if up.get("node_type") == "image_generate" and up.get("job_id"):
            job = job_repo.get(up["job_id"])
            if job is not None and job["status"] == "succeeded":
                target = jobfiles.artifact_at(job["result_paths_json"], 0)
                if target is not None:
                    return str(target)
        stack += _deps_of(up)
    return None




def _deps_of(node) -> list[str]:
    try:
        return json.loads(node["depends_on_json"] or "[]")
    except ValueError:
        return []




def _shot_of_key(node_key: str) -> int | None:
    """`<模板key>#s<镜头id>` 里取出镜头 id；不是按镜头展开的节点返回 None。"""
    marker = "#s"
    if marker not in node_key:
        return None
    try:
        return int(node_key.rsplit(marker, 1)[1])
    except ValueError:
        return None




def _snapshot_of(run) -> dict:
    try:
        return json.loads(run["input_snapshot_json"] or "{}")
    except ValueError:
        return {}




def _upstream_job(node: dict, by_key: dict) -> int | None:
    """这个节点要认哪条作业：顺着依赖**逐层**往上找最近的一条。

    直接依赖常常是内部检查节点（`artifact_validate` 自己不挂 Job），
    只看一层就会找不到要审的作业。按层找（先看完这一层再上一层）保证拿到的是
    "离这一格最近的那条"；而按镜头展开的支线互不相连，所以不会串到别的镜头去。
    """
    seen: set[str] = set()
    frontier = _deps_of(node)
    while frontier:
        nxt: list[str] = []
        for key in frontier:
            if key in seen:
                continue
            seen.add(key)
            up = by_key.get(key)
            if not up:
                continue
            if up.get("job_id"):
                return int(up["job_id"])
            nxt += _deps_of(up)
        frontier = nxt
    return None
