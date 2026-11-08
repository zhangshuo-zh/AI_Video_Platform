import json
import reviewgate

from repositories.documents import documents as docs
from repositories.reviews import reviews

from .documents import read_content
from .errors import ReviewMissing, ReviewStale, WHOLE_DOC_ANCHOR


_REVIEW_STATUS = {"passed": "通过", "failed": "未通过"}


_DECISION_LABELS = {"approved": "通过", "rejected": "驳回", "returned": "退回"}




def _review_signature_view(row) -> dict:
    return {
        "id": row["id"], "anchor": row["anchor"], "decision": row["decision"],
        "decision_label": _DECISION_LABELS.get(row["decision"], row["decision"]),
        "note": row["note"], "signed_by_name": row["signed_by_name"] or "",
        "signed_at": row["signed_at"],
    }




def review_gate(doc, user_id: int | None) -> dict:
    """跑一遍确定性校验并落报告。不写正文、不建 Job（规则检查不花钱）。"""
    data = read_content(doc)
    status, findings, summary = reviewgate.run(data["content"])
    gate = reviews.create_gate(
        project_id=doc["project_id"], doc_id=doc["id"],
        base_version=data["version"], status=status, findings=findings,
        summary=summary, created_by=user_id)
    return review_state(doc, can_sign=True, gate=gate)




def review_state(doc, can_sign: bool = False, gate=None) -> dict:
    """当前文档的审核面：最新报告 + 同版本签字 + 是否失配。"""
    data = read_content(doc)
    gate = gate or reviews.latest_gate(doc["id"])
    if gate is None:
        return {
            "id": None, "project_id": doc["project_id"], "doc_id": doc["id"],
            "doc_title": doc["title"], "base_version": "",
            "current_version": data["version"], "stale": False,
            "status": "none", "status_label": "未校验",
            "findings": [], "summary": {}, "counts": {}, "signatures": [],
            "can_sign": False, "created_by_name": "", "created_at": "",
        }
    try:
        findings = json.loads(gate["findings_json"] or "[]")
    except ValueError:
        findings = []
    try:
        summary = json.loads(gate["summary_json"] or "{}")
    except ValueError:
        summary = {}
    stale = data["version"] != gate["base_version"]
    signs = reviews.signs(reviews.DOCUMENT, doc["id"], gate["base_version"])
    return {
        "id": gate["id"], "project_id": doc["project_id"], "doc_id": doc["id"],
        "doc_title": doc["title"], "base_version": gate["base_version"],
        "current_version": data["version"], "stale": stale,
        "status": gate["status"],
        "status_label": _REVIEW_STATUS.get(gate["status"], gate["status"]),
        "findings": findings, "summary": summary,
        "counts": reviews.counts(reviews.DOCUMENT, doc["id"], gate["base_version"]),
        "signatures": [_review_signature_view(row) for row in signs],
        "can_sign": bool(can_sign and not stale),
        "created_by_name": "", "created_at": gate["created_at"],
    }




def review_sign(doc, payload, user) -> dict:
    """按锚点签字。报告必须存在、必须对应当前正文版本、锚点必须在报告里。"""
    data = read_content(doc)
    gate = reviews.latest_gate(doc["id"])
    if gate is None:
        raise ReviewMissing("这份文档还没有校验报告；先跑一次校验")
    if gate["base_version"] != data["version"]:
        raise ReviewStale("正文在报告之后被改过；旧报告的签字不能算到新版本上，先重新校验")
    try:
        findings = json.loads(gate["findings_json"] or "[]")
    except ValueError:
        findings = []
    anchors = {f.get("anchor") for f in findings}
    if payload.anchor not in anchors and payload.anchor != WHOLE_DOC_ANCHOR:
        raise reviewgate.ReviewInvalid("这个锚点不在本次报告里")
    reviews.upsert_sign(
        project_id=doc["project_id"], target_kind=reviews.DOCUMENT,
        target_id=doc["id"], gate_id=gate["id"],
        base_version=gate["base_version"], anchor=payload.anchor,
        decision=payload.decision, note=payload.note, user_id=user["id"])
    return review_state(doc, can_sign=True, gate=gate)




def review_queue(project_id: int) -> list[dict]:
    """项目里的文档审核队列。只读聚合：最新报告 + 当前版本是否失配。"""
    from models import DOC_KINDS

    out = []
    for doc in docs.list_by_project(project_id):
        try:
            data = read_content(doc)
        except Exception:
            continue
        gate = reviews.latest_gate(doc["id"])
        counts = reviews.counts(reviews.DOCUMENT, doc["id"], data["version"])
        if gate is None:
            out.append({
                "doc_id": doc["id"], "doc_title": doc["title"],
                "doc_kind": doc["kind"], "kind_label": DOC_KINDS.get(doc["kind"], doc["kind"]),
                "episode_id": doc["episode_id"], "episode_title": doc["episode_title"],
                "doc_status": doc["status"], "current_version": data["version"],
                "gate_status": "none", "gate_status_label": "未校验",
                "gate_at": "", "findings": 0, "errors": 0, "warnings": 0,
                "signed": sum(counts.values()), "approved": counts["approved"],
                "rejected": counts["rejected"], "returned": counts["returned"],
                "stale": False,
            })
            continue
        try:
            summary = json.loads(gate["summary_json"] or "{}")
        except ValueError:
            summary = {}
        stale = data["version"] != gate["base_version"]
        out.append({
            "doc_id": doc["id"], "doc_title": doc["title"],
            "doc_kind": doc["kind"], "kind_label": DOC_KINDS.get(doc["kind"], doc["kind"]),
            "episode_id": doc["episode_id"], "episode_title": doc["episode_title"],
            "doc_status": doc["status"], "current_version": data["version"],
            "gate_status": gate["status"] if not stale else "stale",
            "gate_status_label": ("已失配" if stale else
                                  _REVIEW_STATUS.get(gate["status"], gate["status"])),
            "gate_at": gate["created_at"],
            "findings": int(summary.get("errors", 0)) + int(summary.get("warnings", 0)),
            "errors": summary.get("errors", 0),
            "warnings": summary.get("warnings", 0),
            "signed": sum(counts.values()), "approved": counts["approved"],
            "rejected": counts["rejected"], "returned": counts["returned"],
            "stale": stale,
        })
    return out
