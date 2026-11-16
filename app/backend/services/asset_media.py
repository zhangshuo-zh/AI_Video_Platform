"""资产图片上传与删除业务；不依赖 HTTP 上传对象。"""

import uuid
import asset_studio
from config import FILES_DIR
from imagetypes import ALLOWED_TYPES, MAX_IMAGE_BYTES, is_heic, looks_like
from models import (AssetOut, VariantOut)
from paths import listdir_empty, resolve_within
from repositories.content import _images_of, assets
from repositories.content import variants as variant_repo

from .content_operations import (ContentOperationError)
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
    # 先拿到图片清单再删行：删完就取不到了，文件会变成孤儿
    row = assets.get(asset_id)
    if row is None:
        raise ContentOperationError(status_code=404, detail="资产不存在")
    # 清单解析与"删文件只允许删 FILES_DIR 里面"这两件事都已有出处
    # （`_images_of` 认坏 JSON、`remove_imported_file` 走 `resolve_within`）。
    # 原来这里手写 `json.loads(...)` 加一段 unlink 循环：一条脏 `ref_images`
    # （坏 JSON 或数组里混了非字符串）就会让资产行删掉、文件只清一半、
    # 界面报 500，而且第二条再也删不掉——同一个教训在 v0.96 已经收过一次。
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
