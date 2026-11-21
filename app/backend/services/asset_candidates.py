"""候选审选状态与批量导入。"""
import json
from contextlib import ExitStack

import asset_import_recovery
from asset_generation_refs import parse_refs
from asset_studio_pkg.images import _prepare_generated_image, _register_generated_image
from asset_studio_pkg.policy import ImportArtifactBad, ImportUnusable, VariantMissing
from db import get_conn, transaction
from models import AssetCandidateReviewIn, AssetImageBatchIn, AssetImageImportIn
from repositories.content import _images_of, assets, variants
from repositories.jobs import jobs


def _candidate_job(asset, job_id: int):
    job = jobs.get(job_id)
    if job is None or job["capability"] != "image" or job["status"] != "succeeded":
        raise ImportUnusable("仅可处理成功的图像作业")
    ref = parse_refs(job)
    if (ref["operation"] != "asset_image" or ref["asset_id"] not in (None, asset["id"])
            or asset["project_id"] not in (None, job["project_id"])):
        raise ImportUnusable("作业不属于这条资产的候选")
    return job


def asset_candidate_review(asset, job_id: int) -> dict[int, str]:
    _candidate_job(asset, job_id)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT artifact_index, state FROM asset_candidate_review WHERE asset_id = ? AND job_id = ?",
            (asset["id"], job_id),
        )
        return {row["artifact_index"]: row["state"] for row in rows}


def update_asset_candidate_review(asset, job_id: int, payload: AssetCandidateReviewIn) -> dict[int, str]:
    with transaction() as conn:
        job = _candidate_job(asset, job_id)
        try:
            manifest = json.loads(job["result_paths_json"] or "[]")
        except (TypeError, ValueError):
            manifest = []
        count = len(manifest) if isinstance(manifest, list) else 0
        for index in set(payload.indexes):
            if not 0 <= index < min(count, 64):
                raise ImportArtifactBad("候选序号不在作业清单内")
            key = (asset["id"], job_id, index)
            if payload.state == "pending":
                conn.execute("DELETE FROM asset_candidate_review WHERE asset_id = ? AND job_id = ? AND artifact_index = ?", key)
            else:
                conn.execute(
                    """INSERT INTO asset_candidate_review(asset_id, job_id, artifact_index, state)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(asset_id, job_id, artifact_index) DO UPDATE SET state = excluded.state""",
                    (*key, payload.state),
                )
        return asset_candidate_review(asset, job_id)


def import_asset_candidates(asset, payload: AssetImageBatchIn) -> dict:
    indexes = list(dict.fromkeys(payload.indexes))
    if any(not 0 <= index <= 63 for index in indexes):
        raise ImportArtifactBad("候选序号超出范围")
    variant_key = payload.variant_id or 0
    asset_id = int(asset["id"])
    prepared: dict[int, dict] = {}
    with asset_import_recovery.import_lock():
        _candidate_job(asset, payload.job_id)
        with get_conn() as conn:
            group = assets.get(asset_id) if not variant_key else variants.get(variant_key)
            if group is None or (variant_key and group["asset_id"] != asset_id):
                raise VariantMissing("形态不存在或不属于这条资产")
            registered = set(_images_of(group))
            prior = {row["artifact_index"]: row["path"] for row in conn.execute(
                """SELECT artifact_index, path FROM asset_candidate_import
                   WHERE asset_id = ? AND job_id = ? AND variant_key = ?""",
                (asset_id, payload.job_id, variant_key),
            ) if row["path"] in registered}
        with ExitStack() as rollback:
            for index in indexes:
                if index not in prior:
                    prepared[index] = _prepare_generated_image(asset, AssetImageImportIn(
                        job_id=payload.job_id, artifact_index=index, variant_id=payload.variant_id))
                    rollback.callback(asset_import_recovery.discard, prepared[index]["_import_marker"])
            with transaction() as conn:
                _candidate_job(asset, payload.job_id)
                group = assets.get(asset_id) if not variant_key else variants.get(variant_key)
                if group is None or (variant_key and group["asset_id"] != asset_id):
                    raise VariantMissing("形态不存在或不属于这条资产")
                if any(prior[index] not in _images_of(group) for index in indexes if index in prior):
                    raise ImportUnusable("目标参考图已变化，请重试")
                archived = {row["artifact_index"] for row in conn.execute(
                    """SELECT artifact_index FROM asset_candidate_review
                       WHERE asset_id = ? AND job_id = ? AND state = 'archived'""",
                    (asset_id, payload.job_id),
                )}
                if archived.intersection(indexes):
                    raise ImportUnusable("请先恢复已淘汰的候选")
                for index, item in prepared.items():
                    _register_generated_image(item)
                    conn.execute(
                        """INSERT INTO asset_candidate_import(asset_id, job_id, artifact_index, variant_key, path)
                           VALUES (?, ?, ?, ?, ?) ON CONFLICT(asset_id, job_id, artifact_index, variant_key)
                           DO UPDATE SET path = excluded.path""",
                        (asset_id, payload.job_id, index, variant_key, item["path"]),
                    )
            rollback.pop_all()
        for item in prepared.values():
            asset_import_recovery.committed(item["_import_marker"])
    return {"imported": len(prepared), "reused": len(indexes) - len(prepared)}
