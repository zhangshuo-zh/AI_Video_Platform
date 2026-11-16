"""asset_studio images 业务域。"""
from __future__ import annotations
from config import FILES_DIR
from db import transaction
from imagetypes import MAX_IMAGE_BYTES, sniff
from paths import resolve_within
from repositories.content import assets as asset_repo
from repositories.content import variants as variant_repo
from repositories.jobs import jobs as job_repo
import asset_import_recovery
import jobfiles
import json
import shutil
import uuid
from .policy import IMAGE_CAPABILITY
from .policy import ImportArtifactBad
from .policy import ImportImageRejected
from .policy import ImportUnusable
from .policy import VariantMissing
from .generation import _status_label



# ---------- 从成功作业产物导入资产图 ----------

def _prepare_generated_image(asset, payload) -> dict:
    """校验并复制成功图像作业的第 N 个产物，返回待登记的图片意图。

    入参只有 `job_id + artifact_index`，**没有任何路径字段**：文件在哪由服务端
    从作业的产物清单里查。这一步是整个接口里最要紧的边界——
    一旦接受客户端给的路径，"导入一张图"就能变成"删一个服务器文件"。
    """
    job = job_repo.get(payload.job_id)
    if job is None:
        raise ImportUnusable("作业不存在")
    if job["capability"] != IMAGE_CAPABILITY:
        raise ImportUnusable(f"这条作业是{job['capability']}，不是图像生成；"
                             "资产参考图只能从图像作业的产物里存")
    if job["status"] != "succeeded":
        raise ImportUnusable(f"作业现在是「{_status_label(job['status'])}」，"
                             "只有成功结束的图像作业才有可存的产物")

    variant = None
    if payload.variant_id is not None:
        variant = variant_repo.get(payload.variant_id)
        if variant is None:
            raise VariantMissing("形态不存在")
        if variant["asset_id"] != asset["id"]:
            raise ImportUnusable("这个形态不属于这条资产")

    source = jobfiles.artifact_at(job["result_paths_json"], payload.artifact_index)
    # artifact_at 已经确认是边界内的现存文件；后面只取一次大小，不重复 is_file。
    if source is None:
        raise ImportArtifactBad("这个序号在作业产物清单里不存在，或文件已经被清理")

    size = source.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ImportImageRejected(f"产物有 {size // 1024 // 1024}MB，"
                                  f"超过参考图上限 {MAX_IMAGE_BYTES // 1024 // 1024}MB")
    with source.open("rb") as fh:
        head = fh.read(16)
    ext = sniff(head)
    if ext is None:
        raise ImportImageRejected("产物不是 PNG / JPEG / WebP，不能当参考图存")

    # 复制而不是移动：作业的产物是生成中心的事实，也是产物审核签字的对象
    # （artifact_version 是按"路径＋存在＋字节数"算的）。搬走它等于偷偷让
    # 已经签过字的产物清单变成空目录，那一签就说不清了。
    target_dir = FILES_DIR / str(asset["id"])
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    copied = target_dir / filename
    rel = f"{asset['id']}/{filename}"
    token = asset_import_recovery.begin(rel, job["id"])

    try:
        shutil.copyfile(source, copied)
        asset_import_recovery.sync_image(copied)
    except Exception:
        asset_import_recovery.discard(token)
        raise
    return {"_import_marker": token, "asset_id": asset["id"],
            "variant_id": variant["id"] if variant is not None else None,
            "source_job_id": job["id"], "path": rel}



def _register_generated_image(prepared: dict) -> dict:
    """只写数据库引用；调用方负责事务与文件补偿。"""
    rel = prepared["path"]
    if prepared["variant_id"] is None:
        row = asset_repo.add_image(prepared["asset_id"], rel)
        if row is None:
            raise ImportUnusable("资产不存在")
    else:
        row = variant_repo.add_image(prepared["variant_id"], rel,
                                    source_job_id=prepared["source_job_id"])
        if row is None:
            raise VariantMissing("形态不存在")
    images = json.loads(row["ref_images"] or "[]")
    return {**prepared, "ref_images": images, "image_count": len(images)}



def import_generated_image(asset, payload) -> dict:
    """兼容已有自行管理事务的调用方，返回未清理的恢复标记。"""
    prepared = _prepare_generated_image(asset, payload)
    try:
        return _register_generated_image(prepared)
    except Exception:
        asset_import_recovery.discard(prepared["_import_marker"])
        raise



def remove_variant_image_tx(variant_id: int, path: str) -> tuple[object | None, bool]:
    """在一个事务内从形态摘除图片引用，并在事务后清理盘上文件。"""
    with transaction():
        out, removed = variant_repo.remove_image(variant_id, path)
    if out is not None and removed:
        remove_imported_file(path)
    return out, removed



def import_generated_image_tx(asset, payload) -> dict:
    """文件准备与补偿均在数据库事务外，持锁协调恢复进程。"""
    with asset_import_recovery.import_lock():
        prepared = _prepare_generated_image(asset, payload)
        try:
            with transaction():
                out = _register_generated_image(prepared)
        except Exception:
            asset_import_recovery.discard(prepared["_import_marker"])
            raise
        asset_import_recovery.committed(out.pop("_import_marker"))
        return out



def remove_imported_file(rel_path: str) -> bool:
    """删一张参考图时顺带清文件：只允许清 FILES_DIR 里面的路径。

    `resolve_within` 而不是 `startswith`：后者会把 `data/files` 匹配给
    `data/files-evil`，一条脏记录就能把删除范围带出门外（批次②A 的同一课）。
    """
    target = resolve_within(FILES_DIR, rel_path)
    if target is not None and target.is_file():
        target.unlink()
        return True
    return False
