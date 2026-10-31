"""创作单元、分镜、资产的接口。

路径按层级走：/api/projects/{id}/episodes → /api/episodes/{id}/shots，
这样前端拿到的永远是"当前对象下面的东西"，不用自己拼过滤条件。
"""
import json
import uuid
from pathlib import Path

from fastapi import (APIRouter, Depends, File, HTTPException, UploadFile,
                     status as http_status)

from access import ensure, ensure_by
from config import FILES_DIR
from deps import require_permission

from models import (AssetIn, AssetOut, EpisodeIn, EpisodeOut, EpisodeStatusIn,
                    ShotIn, ShotOut)
from repositories.content import assets, episodes, shots

router = APIRouter(prefix="/api", tags=["content"])


# ---------- 创作单元 ----------

@router.get("/projects/{project_id}/episodes", response_model=list[EpisodeOut])
def list_episodes(project_id: int,
                  user=Depends(require_permission("doc:read"))) -> list[EpisodeOut]:
    ensure(user, project_id, "read")
    return [EpisodeOut.from_row(row) for row in episodes.list_by_project(project_id)]


@router.post("/projects/{project_id}/episodes", response_model=EpisodeOut,
             status_code=http_status.HTTP_201_CREATED)
def create_episode(project_id: int, payload: EpisodeIn,
                   user=Depends(require_permission("doc:write"))) -> EpisodeOut:
    ensure(user, project_id, "write")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="标题不能为空")
    return EpisodeOut.from_row(episodes.create(project_id, title))


@router.patch("/episodes/{episode_id}/status", response_model=EpisodeOut)
def set_episode_status(episode_id: int, payload: EpisodeStatusIn,
                       user=Depends(require_permission("doc:write"))) -> EpisodeOut:
    """分集状态：draft|active|done。项目中心的"已定稿 N/M 集"就是它的聚合。

    权限走 doc:write——改集状态的东家是创作中心，项目中心只显示不改。
    """
    ensure_by("episode", episode_id, user, "write", "创作单元")
    row = episodes.set_status(episode_id, payload.status)
    if row is None:
        raise HTTPException(status_code=404, detail="创作单元不存在")
    return EpisodeOut.from_row(row)


@router.delete("/episodes/{episode_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_episode(episode_id: int,
                   user=Depends(require_permission("doc:write"))) -> None:
    ensure_by("episode", episode_id, user, "write", "创作单元")
    if not episodes.delete(episode_id):
        raise HTTPException(status_code=404, detail="创作单元不存在")


# ---------- 分镜 ----------

@router.get("/episodes/{episode_id}/shots", response_model=list[ShotOut])
def list_shots(episode_id: int,
               user=Depends(require_permission("doc:read"))) -> list[ShotOut]:
    ensure_by("episode", episode_id, user, "read", "创作单元")
    return [ShotOut.from_row(row) for row in shots.list_by_episode(episode_id)]


@router.post("/episodes/{episode_id}/shots", response_model=ShotOut,
             status_code=http_status.HTTP_201_CREATED)
def create_shot(episode_id: int, payload: ShotIn,
                user=Depends(require_permission("doc:write"))) -> ShotOut:
    ensure_by("episode", episode_id, user, "write", "创作单元")
    summary = payload.summary.strip()
    if not summary:
        raise HTTPException(status_code=422, detail="镜头描述不能为空")
    row = shots.create(episode_id, summary, payload.prompt.strip(),
                       payload.duration, payload.asset_ids)
    return ShotOut.from_row(row)


@router.delete("/shots/{shot_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_shot(shot_id: int,
                user=Depends(require_permission("doc:write"))) -> None:
    ensure_by("shot", shot_id, user, "write", "镜头")
    if not shots.delete(shot_id):
        raise HTTPException(status_code=404, detail="镜头不存在")


# ---------- 资产 ----------

@router.get("/projects/{project_id}/assets", response_model=list[AssetOut])
def list_assets(project_id: int, kind: str | None = None,
                user=Depends(require_permission("asset:read"))) -> list[AssetOut]:
    ensure(user, project_id, "read")
    return [AssetOut.from_row(row) for row in assets.list_by_project(project_id, kind)]


@router.post("/projects/{project_id}/assets", response_model=AssetOut,
             status_code=http_status.HTTP_201_CREATED)
def create_asset(project_id: int, payload: AssetIn,
                 user=Depends(require_permission("asset:write"))) -> AssetOut:
    ensure(user, project_id, "write")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="名称不能为空")
    if assets.exists(project_id, payload.kind, name):
        raise HTTPException(status_code=409, detail="同类资产里已经有这个名字，去合并而不是新建")
    row = assets.create(project_id, payload.kind, name, payload.summary.strip(),
                        payload.consistency_prompt.strip(), payload.ref_images)
    return AssetOut.from_row(row)


@router.delete("/assets/{asset_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_asset(asset_id: int,
                 user=Depends(require_permission("asset:write"))) -> None:
    ensure_by("asset", asset_id, user, "write", "资产")
    # 先拿到图片清单再删行：删完就取不到了，文件会变成孤儿
    row = assets.get(asset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    images = json.loads(row["ref_images"] or "[]")
    assets.delete(asset_id)

    for rel in images:
        target = (FILES_DIR / rel).resolve()
        if str(target).startswith(str(FILES_DIR.resolve())) and target.is_file():
            target.unlink()
    folder = (FILES_DIR / str(asset_id)).resolve()
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()


# ---------- 定妆图 ----------

# 只收这三种。扩展名由服务端按类型定，不信客户端给的文件名
ALLOWED_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAGIC = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
}


def _is_heic(content_type: str, head: bytes) -> bool:
    """iPhone 默认拍 HEIC，浏览器自己也显示不了，提前给人一句人话。"""
    return content_type in ("image/heic", "image/heif") or b"ftypheic" in head or b"ftypheif" in head


def _looks_like(content_type: str, head: bytes) -> bool:
    """看文件头。content-type 是客户端嘴上说的，文件头才是它到底是什么。"""
    if content_type == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    magic = MAGIC.get(content_type)
    return bool(magic) and head.startswith(magic)


@router.post("/assets/{asset_id}/images", response_model=AssetOut)
async def upload_asset_image(asset_id: int, file: UploadFile = File(...),
                            user=Depends(require_permission("asset:write"))) -> AssetOut:
    ensure_by("asset", asset_id, user, "write", "资产")
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="只收 PNG / JPEG / WebP")

    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="图片不能超过 20MB")
    if _is_heic(file.content_type, data[:32]):
        raise HTTPException(status_code=415,
                            detail="iPhone 的 HEIC 浏览器显示不了，先转成 JPG 或 PNG 再传")
    if not _looks_like(file.content_type, data[:12]):
        raise HTTPException(status_code=415, detail="文件内容和类型对不上")

    # 每个资产一个子目录，文件名用随机串：避免覆盖，也不用处理原始文件名里的怪字符
    target_dir = FILES_DIR / str(asset_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ALLOWED_TYPES[file.content_type]}"
    (target_dir / filename).write_bytes(data)

    row = assets.add_image(asset_id, f"{asset_id}/{filename}")
    if row is None:
        # 资产不存在时把刚写的文件删掉，不留垃圾
        (target_dir / filename).unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail="资产不存在")
    return AssetOut.from_row(row)


@router.delete("/assets/{asset_id}/images", response_model=AssetOut)
def delete_asset_image(asset_id: int, path: str,
                       user=Depends(require_permission("asset:write"))) -> AssetOut:
    ensure_by("asset", asset_id, user, "write", "资产")
    row, removed = assets.remove_image(asset_id, path)
    if row is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    if not removed:
        raise HTTPException(status_code=404, detail="这张图不在该资产下")

    # 只删 FILES_DIR 里的文件：拼出来的路径必须仍在上传目录内
    target = (FILES_DIR / path).resolve()
    if str(target).startswith(str(FILES_DIR.resolve())) and target.is_file():
        target.unlink()
    return AssetOut.from_row(row)
