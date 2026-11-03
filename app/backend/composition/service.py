"""成片中心的服务层：建时间线、开合成 Job、读视图、回收最终产物（阶段 D）。

一次合成＝一条 `composition` 记录 ＋ 一条普通 `job`（provider=ffmpeg）。两步在
同一个事务里落库：只建了一半就会留下"有片子没作业"或"有作业没人认领"的中间态，
而那正是裁定 D2 统一提交协议时要消灭的形状。

状态不单独存：`view()` 从关联 Job 现算。Job 被删（外键 SET NULL）时显示
"关联作业已不在"，而不是凭空回到草稿。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

import jobfiles
from config import (COMPOSE_FPS, COMPOSE_HEIGHT, COMPOSE_WIDTH)
from db import get_conn, transaction
from repositories.compositions import compositions
from repositories.content import episodes
from repositories.jobs import jobs as job_repo

from composition import timeline as timeline_mod
from composition import validators


class CompositionMissing(Exception):
    pass


class CompositionConflict(Exception):
    pass


class CompositionInvalid(Exception):
    def __init__(self, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.reasons = reasons or [message]


JOB_STATUS_LABELS = {
    "queued": "排队中", "starting": "启动中", "running": "合成中",
    "succeeded": "已完成", "failed": "失败", "cancelled": "已取消",
    "cancel_requested": "取消中", "needs_attention": "待人工核对",
}


def _params(raw: dict | None) -> dict:
    raw = raw or {}
    width = int(raw.get("width") or COMPOSE_WIDTH)
    height = int(raw.get("height") or COMPOSE_HEIGHT)
    fps = int(raw.get("fps") or COMPOSE_FPS)
    if width % 2 or height % 2:
        raise CompositionInvalid(f"分辨率 {width}×{height} 不合法：宽高必须是偶数")
    if not (320 <= width <= 3840 and 320 <= height <= 2160):
        raise CompositionInvalid(f"分辨率 {width}×{height} 不合法：超出 320—3840 范围")
    if not (1 <= fps <= 60):
        raise CompositionInvalid(f"帧率 {fps} 不合法：只接受 1—60")
    return {"width": width, "height": height, "fps": fps,
            "video_codec": "h264", "container": "mp4"}


def create(project_id: int, episode_id: int, created_by: int | None, *,
           params: dict | None = None, idempotency_key: str | None = None,
           pipeline_run_id: int | None = None,
           subtitle_path: str | None = None) -> dict:
    """建一条成片记录并发起合成。返回成片视图（含 job_id）。"""
    key = (idempotency_key or "").strip() or None
    if key:
        existing = job_repo.by_key(key)
        if existing is not None:
            row = compositions.by_job(existing["id"])
            if row is not None:
                return view(row["id"])
    episode = episodes.get(episode_id)
    if episode is None or episode["project_id"] != project_id:
        raise CompositionInvalid("这一集不属于该项目")
    frozen = _params(params)
    try:
        timeline = timeline_mod.build(
            project_id, episode_id, require_approved=True,
            subtitle_path=subtitle_path)
    except timeline_mod.CompositionInvalid as exc:
        raise CompositionInvalid(exc.message, exc.reasons) from exc

    key = key or f"compose-{uuid.uuid4().hex}"
    try:
        with transaction():
            version = compositions.next_version(episode_id)
            job = job_repo.create({
                "project_id": project_id,
                "episode_id": episode_id,
                "shot_id": None,
                "capability": "video",
                "kind": "video_local",
                "execution_mode": "local",
                "provider_code": "ffmpeg",
                "model_code": "ffmpeg-compose-1",
                "adapter_version": "ffmpeg",
                "prompt_snapshot": (f"第 {episode['seq']} 集成片 v{version}："
                                    f"{len(timeline['shots'])} 镜"),
                "params": {"composition_id": 0, **frozen},
                "input_refs": [s["video"]["path"] for s in timeline["shots"]],
                "priority": 0,
                "idempotency_key": key,
                "billable_status": "not_billable",
                "created_by": created_by,
            })
            row = compositions.create(
                project_id=project_id, episode_id=episode_id,
                pipeline_run_id=pipeline_run_id, version=version,
                timeline=timeline, params=frozen, job_id=job["id"],
                created_by=created_by)
            # 参数里的 composition_id 要等行建出来才知道；回填到同一事务里
            with get_conn() as conn:
                conn.execute(
                    """UPDATE job SET params_json = ? WHERE id = ?""",
                    (json.dumps({"composition_id": row["id"], **frozen},
                                ensure_ascii=False, sort_keys=True), job["id"]))
    except sqlite3.IntegrityError as exc:
        raise CompositionConflict(
            f"这一集的成片版本刚被另一次请求占用（{exc}）；刷新后再点一次") from exc
    return view(row["id"])


def view(composition_id: int, *, include_timeline: bool = True) -> dict:
    row = compositions.get(composition_id)
    if row is None:
        raise CompositionMissing("成片记录不存在")
    job = job_repo.get(row["job_id"]) if row["job_id"] else None
    # 崩在"Job 成功"与"回填成片"之间时，读的时候补一次；补不出来就如实空着
    if job is not None and job["status"] == "succeeded" and not row["output_path"]:
        try:
            record_success(job)
            row = compositions.get(composition_id) or row
        except Exception:
            pass
    status = _status(job, row)
    return _view(row, job, status, include_timeline=include_timeline)


def list_for_project(project_id: int, episode_id: int | None = None) -> list[dict]:
    return [view(row["id"], include_timeline=False)
            for row in compositions.list_for_project(project_id, episode_id)]


def record_success(job) -> None:
    """Job 成功后把最终文件、sha256 与媒体信息抄回成片行。

    只抄一次：重复调用是幂等的，已经填过就不覆盖——成片版本不可变。
    """
    row = compositions.by_job(job["id"])
    if row is None or row["output_path"]:
        return
    try:
        paths = json.loads(job["result_paths_json"] or "[]")
    except ValueError:
        paths = []
    rel = str(paths[0]) if paths else ""
    target = jobfiles.resolve_artifact(rel) if rel else None
    if target is None:
        return
    receipt = jobfiles.job_dir(job["id"]) / "outputs" / f"{job['id']}-compose.json"
    info: dict = {}
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        info = payload.get("observed") or {}
    except (OSError, ValueError):
        info = {}
    if not info:
        info = validators.inspect(target)
    compositions.record_output(
        row["id"], output_path=rel, sha256=_sha256(target),
        bytes_=target.stat().st_size, duration_ms=info.get("duration_ms"),
        width=info.get("width"), height=info.get("height"))


def _status(job, row) -> dict:
    if row["job_id"] is None:
        return {"code": "draft", "label": "未发起"}
    if job is None:
        return {"code": "job_missing", "label": "关联作业已不在"}
    code = job["status"]
    if code == "needs_attention":
        return {"code": "attention", "label": JOB_STATUS_LABELS[code]}
    return {"code": code, "label": JOB_STATUS_LABELS.get(code, code)}


def _view(row, job, status, *, include_timeline: bool) -> dict:
    try:
        timeline = json.loads(row["timeline_json"] or "{}")
    except ValueError:
        timeline = {}
    try:
        params = json.loads(row["params_json"] or "{}")
    except ValueError:
        params = {}
    out = {
        "id": row["id"], "project_id": row["project_id"],
        "episode_id": row["episode_id"], "episode_seq": row["episode_seq"],
        "episode_title": row["episode_title"], "version": row["version"],
        "job_id": row["job_id"], "status": status["code"],
        "status_label": status["label"],
        "job_status": job["status"] if job is not None else "",
        "error_code": (job["error_code"] if job is not None else "") or "",
        "error_message": (job["error_message"] if job is not None else "") or "",
        "params": params,
        "shot_count": len(timeline.get("shots") or []),
        "audio_count": len(timeline.get("audio") or []),
        "total_duration_ms": int(timeline.get("total_duration_ms") or 0),
        "output_path": row["output_path"] or "",
        "sha256": row["sha256"] or "",
        "bytes": row["bytes"] or 0,
        "duration_ms": row["duration_ms"] or 0,
        "width": row["width"] or params.get("width"),
        "height": row["height"] or params.get("height"),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "artifact_url": (f"/api/jobs/{row['job_id']}/artifacts/0"
                         if row["job_id"] and row["output_path"] else ""),
    }
    if include_timeline:
        out["timeline"] = timeline
        out["audio"] = timeline.get("audio") or []
        out["subtitles"] = timeline.get("subtitles") or []
    return out


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
