"""资产图片上传与删除业务；不依赖 HTTP 上传对象。"""

import json

import jobfiles
import uuid
from pathlib import Path
import asset_studio
from config import FILES_DIR
from db import transaction
from imagetypes import ALLOWED_TYPES, MAX_IMAGE_BYTES, is_heic, looks_like
from models import AssetImagePurposeIn, AssetReferenceReplacementIn, AssetOut, VariantOut
from paths import listdir_empty, resolve_within
from repositories.content import _images_of, assets, shots
from repositories.content import variants as variant_repo
from repositories.jobs import jobs as job_repo
from repositories.reviews import reviews
from repositories.production import production

from .content_operations import (ContentOperationError)
from .jobs import shot_variant_ids
from typing import BinaryIO

def delete_asset(asset_id: int, refs) -> None:
    """删资产并清掉它登记过的图。被镜头引用就不给删。

    不提供 force 旁路：镜头里的悬空 asset_id 比"删不掉"难查得多，
    而且它会在创作中心静默显示成 #12 这种没有名字的东西。
    要删先去创作中心解除引用。
    """
    if refs:
        first = refs[0]
        raise ContentOperationError(
            status_code=409,
            detail=f"这个资产被 {len(refs)} 条镜头引用（如《{first['project_name']}》"
                   f"第 {first['episode_seq']} 集 镜 {first['seq']}），先去创作中心解除引用")
    with transaction():
        # 全量引用复查与删除收进同一事务：即使调用方看不到某些项目的镜头，
        # 也不允许删掉被它们引用的全局资产，且不泄露不可见项目的名称或镜头号。
        all_refs = production.referencing_shot_rows(asset_id)
        if all_refs:
            raise ContentOperationError(
                status_code=409,
                detail="这个资产正被其他项目的镜头引用，无法删除；请联系相关项目解除引用后再试",
            )
        # 先拿到图片清单再删行：删完就取不到了，文件会变成孤儿
        row = assets.get(asset_id)
        if row is None:
            raise ContentOperationError(status_code=404, detail="资产不存在")
        # 清单解析与"删文件只允许删 FILES_DIR 里面"这两件事都已有出处
        # （`_images_of` 认坏 JSON、`remove_imported_file` 走 `resolve_within`）。
        images = _images_of(row)
        assets.delete(asset_id)

    for rel in images:
        asset_studio.remove_imported_file(rel)
    folder = (FILES_DIR / str(asset_id)).resolve()
    if listdir_empty(folder):
        folder.rmdir()


def upload_asset_image(asset_id: int, stream: BinaryIO, content_type: str | None) -> AssetOut:
    if content_type not in ALLOWED_TYPES:
        raise ContentOperationError(status_code=415, detail="只收 PNG / JPEG / WebP")

    data = stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ContentOperationError(status_code=413, detail="图片不能超过 20MB")
    if is_heic(content_type, data[:32]):
        raise ContentOperationError(status_code=415,
                            detail="iPhone 的 HEIC 浏览器显示不了，先转成 JPG 或 PNG 再传")
    if not looks_like(content_type, data[:12]):
        raise ContentOperationError(status_code=415, detail="文件内容和类型对不上")

    # 每个资产一个子目录，文件名用随机串：避免覆盖，也不用处理原始文件名里的怪字符
    target_dir = FILES_DIR / str(asset_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ALLOWED_TYPES[content_type]}"
    (target_dir / filename).write_bytes(data)

    row = assets.add_image(asset_id, f"{asset_id}/{filename}")
    if row is None:
        # 资产不存在时把刚写的文件删掉，不留垃圾
        (target_dir / filename).unlink(missing_ok=True)
        raise ContentOperationError(status_code=404, detail="资产不存在")
    return AssetOut.from_row(row)


def delete_asset_image(asset_id: int, path: str) -> AssetOut:
    with transaction():
        row, removed = assets.remove_image(asset_id, path)
    if row is None:
        raise ContentOperationError(status_code=404, detail="资产不存在")
    if not removed:
        raise ContentOperationError(status_code=404, detail="这张图不在该资产下")

    # 只删 FILES_DIR 里的文件：path 来自调用方，拼完解析后必须仍在上传目录内
    target = resolve_within(FILES_DIR, path)
    if target is not None and target.is_file():
        target.unlink()
    return AssetOut.from_row(row)


def delete_variant(variant_id: int) -> None:
    images = asset_studio.delete_variant(variant_id)
    # 文件在库提交之后再清：事务里只做库内写（db.transaction 的边界），
    # 反过来"文件没了、记录还在"会在画廊里留下一张打不开的图
    for rel in images:
        asset_studio.remove_imported_file(rel)


def upload_variant_image(variant_id: int, asset_id: int, stream: BinaryIO, content_type: str | None) -> VariantOut:
    """为某个资产形态上传一张本地参考图，复用资产图片安全校验并写入所属资产目录。"""
    data = stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ContentOperationError(
            status_code=413,
            detail=f"图片大小超过上限 {MAX_IMAGE_BYTES // 1024 // 1024}MB",
        )
    ctype = (content_type or "").lower()
    if is_heic(ctype, data[:32]):
        raise ContentOperationError(status_code=422, detail="暂不支持 HEIC/HEIF，请先转为 PNG/JPG/WebP")
    if ctype not in ALLOWED_TYPES:
        raise ContentOperationError(status_code=422, detail="仅支持 PNG、JPG、WebP 图片")
    if not looks_like(ctype, data[:32]):
        raise ContentOperationError(status_code=422, detail="文件内容与声明的图片格式不符")
    target_dir = FILES_DIR / str(asset_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ALLOWED_TYPES[ctype]}"
    target = target_dir / filename
    target.write_bytes(data)
    rel = f"{asset_id}/{filename}"
    updated = variant_repo.add_image(variant_id, rel, source_job_id=None)
    if updated is None:
        raise ContentOperationError(status_code=404, detail="形态不存在")
    return VariantOut.from_row(updated)


def delete_variant_image(variant_id: int, path: str) -> VariantOut:
    """从某个形态的图清单里摘掉一张，并清掉它在 `FILES_DIR` 里的那个文件。

    这一条是本轮补上的**出口**：0033 有 `VariantRepository.remove_image`，
    但没有任何接口能调它——结果形态的图只能进不能出，导错一张就得删整个形态
    （连带把别的图也清了）。基础参考图从来都有删除入口，两边不对称就是缺一条路。

    边界与资产删图同一条：`path` 来自调用方，但只有**已经登记在这个形态下**的路径
    才摘得掉（`remove_image` 判），落盘删除再走 `resolve_within(FILES_DIR, …)`，
    所以这里既删不掉别人的图，也删不掉目录外的文件。
    """
    out, removed = asset_studio.remove_variant_image_tx(variant_id, path)
    if out is None:
        raise asset_studio.VariantMissing("形态不存在")
    if not removed:
        # 与"删资产的参考图"同一条口径：路径不在这一份清单里就回 404，
        # 不能回一句"删好了"而实际什么都没动——那是最难查的一种假成功。
        raise ContentOperationError(status_code=404, detail="这张图不在该形态下")
    return VariantOut.from_row(out)


def set_image_purpose(asset_id: int, payload: AssetImagePurposeIn) -> None:
    variant_id = payload.variant_id
    with transaction() as conn:
        row = assets.get(asset_id) if variant_id is None else variant_repo.get(variant_id)
        if (row is None or (variant_id is not None and row["asset_id"] != asset_id)
                or payload.path not in _images_of(row)):
            raise ContentOperationError(status_code=404, detail="这张图不在指定的资产或形态下")
        purposes = json.loads(row["image_purposes"])
        purpose = payload.purpose.strip()
        if purpose:
            purposes[payload.path] = purpose
        else:
            purposes.pop(payload.path, None)
        if variant_id is None:
            conn.execute("UPDATE asset SET image_purposes = ? WHERE id = ?",
                         (json.dumps(purposes, ensure_ascii=False), asset_id))
        else:
            conn.execute("UPDATE asset_variant SET image_purposes = ? WHERE id = ?",
                         (json.dumps(purposes, ensure_ascii=False), variant_id))

_FRAME_KEYS = {"first_frame_image": "image", "last_frame_image": "last_frame_image"}


def _asset_frame_path(value: str, asset_id: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    for prefix in ("/api/files/", "/files/", "files/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(FILES_DIR.resolve())
        except ValueError:
            return None
    if len(path.parts) != 2 or path.parts[0] != str(asset_id) or path.name == "..":
        return None
    return path.as_posix()


def _reference_uses(project_id: int, asset_id: int) -> list[dict]:
    uses = []
    for row in job_repo.video_reference_receipts(project_id):
        receipt = json.loads(row["resources_json"])
        for field, key in _FRAME_KEYS.items():
            path = _asset_frame_path(receipt.get(key, ""), asset_id)
            if path:
                current = row[field] or ""
                replaceable = not current or _asset_frame_path(current, asset_id) == path
                uses.append({**{name: row[name] for name in ("job_id", "shot_id", "seq",
                                                            "episode_id", "episode_seq")},
                             "path": path, "field": field, "current": current if replaceable else "",
                             "replaceable": replaceable})
    return uses


def asset_reference_usage(project_id: int, asset_id: int) -> list[dict]:
    uses = _reference_uses(project_id, asset_id)
    signs = reviews.signs_by_targets("job", {row["job_id"] for row in uses})
    return [{**row, "signed": any(sign["decision"] == "approved"
                                 for sign in signs.get(row["job_id"], []))}
            for row in uses]


def replace_asset_references(project_id: int, asset_id: int,
                             payload: AssetReferenceReplacementIn) -> int:
    if _asset_frame_path(payload.old_path, asset_id) != payload.old_path:
        raise ContentOperationError(422, "旧图路径不属于这条资产")
    if payload.new_path == payload.old_path:
        raise ContentOperationError(422, "请选择另一张图")
    with transaction():
        asset = assets.get(asset_id)
        if asset is None or asset["project_id"] not in (None, project_id):
            raise ContentOperationError(404, "资产不存在")
        registered = set(_images_of(asset))
        for variant in variant_repo.list_by_asset(asset_id):
            registered.update(_images_of(variant))
        target = resolve_within(FILES_DIR, payload.new_path)
        if (payload.new_path not in registered or target is None or not target.is_file()
                or _asset_frame_path(payload.new_path, asset_id) != payload.new_path):
            raise ContentOperationError(422, "新图必须是这条资产中已登记且仍可读取的图片")
        eligible = {(row["shot_id"], row["field"], row["current"])
                    for row in _reference_uses(project_id, asset_id)
                    if row["path"] == payload.old_path and row["replaceable"]}
        changes: dict[int, dict[str, str]] = {}
        for change in payload.changes:
            if (change.shot_id, change.field, change.expected) not in eligible:
                raise ContentOperationError(409, "镜头帧输入已变化，请刷新影响清单后重试")
            changes.setdefault(change.shot_id, {})[change.field] = payload.new_path
        for shot_id, fields in changes.items():
            shots.set_frame_image(shot_id, **fields)
        return len(changes)


def character_appearances(project_id: int, asset_id: int) -> list[dict]:
    rows = production.recent_character_appearances(project_id, asset_id, limit=8)
    variant_rows = {row["id"]: row for row in variant_repo.list_by_asset(asset_id)}
    items = []
    for row in rows:
        names = [variant_rows[vid]["name"] for vid in sorted(shot_variant_ids(row)) if vid in variant_rows]
        index = next((i for i, art in enumerate(jobfiles.artifacts(row["result_paths_json"]))
                      if art["exists"] and Path(art["path"]).suffix.lower() in {".mp4", ".webm", ".mov", ".m4v"}), None)
        items.append({**{key: row[key] for key in ("shot_id", "seq", "summary", "episode_id",
                                                "episode_seq", "job_id", "finished_at")},
                      "variant_names": names, "artifact_index": index})
    return items
