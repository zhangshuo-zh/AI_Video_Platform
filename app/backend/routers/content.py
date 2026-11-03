"""创作单元、分镜、资产的接口。

路径按层级走：/api/projects/{id}/episodes → /api/episodes/{id}/shots，
这样前端拿到的永远是"当前对象下面的东西"，不用自己拼过滤条件。
"""
import json
import uuid
from pathlib import Path

from fastapi import (APIRouter, Depends, File, HTTPException, UploadFile,
                     status as http_status)

from access import ensure, ensure_asset, ensure_by, is_manager
from config import FILES_DIR
from deps import require_permission

from models import (AssetCoverIn, AssetIn, AssetOut, AssetShotOut, EpisodeIn, EpisodeOut,
                    EpisodeStatusIn, ShotIn, ShotOut)
from paths import listdir_empty, resolve_within
from repositories.content import assets, episodes, shots
from services import clean_tags, shots_view

router = APIRouter(prefix="/api", tags=["content"])

MAX_ALIASES = 10
MAX_ALIAS_LEN = 60          # 与资产名同宽：别名将来是拿去参与查重的，不该比名字长


def _clean_aliases(raw: list[str]) -> list[str]:
    """别名去空白、去重、限量。规则与 clean_tags 同形，但长度上限不同，
    所以不复用它——别名要跟名字一样长，标签只要 20 字。"""
    seen: list[str] = []
    for item in raw:
        alias = item.strip()[:MAX_ALIAS_LEN]
        if alias and alias not in seen:
            seen.append(alias)
    return seen[:MAX_ALIASES]


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

# 一镜最多挂多少个资产。不是业务上限，是"别让一条镜头把整页 chips 撑爆"的
# 界面上限：一镜里有三五个角色/场景已经很多，四十个足够。
MAX_SHOT_ASSETS = 40


def _checked_asset_ids(project_id: int, raw: list[int]) -> list[int]:
    """把前端交上来的引用收敛成服务端认可的一串 id。

    三条都要在写入口拦：
    1. 去重并保序——同一资产点两次不该在 chips 里出现两遍；
    2. 必须是本项目资产或全局资产——别家项目的 id 一律拒，否则会出现
       "看不见的引用"（删除保护算得到、界面上却找不到）；
    3. 镜头编辑与新建走同一套，不然从创建口仍然能塞进脏 id。
    """
    ids: list[int] = []
    for value in raw:
        try:
            asset_id = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="资产 id 必须是整数") from exc
        if asset_id <= 0:
            raise HTTPException(status_code=422, detail="资产 id 必须是正整数")
        if asset_id not in ids:
            ids.append(asset_id)
    if len(ids) > MAX_SHOT_ASSETS:
        raise HTTPException(status_code=422,
                            detail=f"一镜最多引用 {MAX_SHOT_ASSETS} 个资产")
    if ids and assets.usable_ids(project_id, ids) != set(ids):
        raise HTTPException(
            status_code=422,
            detail="引用了不存在、不属于这个项目，或不是全局资产的 id")
    return ids


@router.get("/episodes/{episode_id}/shots", response_model=list[ShotOut])
def list_shots(episode_id: int,
               user=Depends(require_permission("doc:read"))) -> list[ShotOut]:
    ensure_by("episode", episode_id, user, "read", "创作单元")
    return [ShotOut.from_row(item["shot"], source_stale=item["source_stale"])
            for item in shots_view(episode_id)]


@router.post("/episodes/{episode_id}/shots", response_model=ShotOut,
             status_code=http_status.HTTP_201_CREATED)
def create_shot(episode_id: int, payload: ShotIn,
                user=Depends(require_permission("doc:write"))) -> ShotOut:
    project = ensure_by("episode", episode_id, user, "write", "创作单元")
    summary = payload.summary.strip()
    if not summary:
        raise HTTPException(status_code=422, detail="镜头描述不能为空")
    row = shots.create(episode_id, summary, payload.prompt.strip(),
                       payload.duration,
                       _checked_asset_ids(project["id"], payload.asset_ids))
    return ShotOut.from_row(row)


@router.put("/shots/{shot_id}", response_model=ShotOut)
def update_shot(shot_id: int, payload: ShotIn,
                user=Depends(require_permission("doc:write"))) -> ShotOut:
    """整体改一镜：内容、提示词、时长、引用资产一次提交。

    和文档编辑同一条纪律——同一个字段不留两个入口；镜号与所属集不在
    可编辑范围里（挪镜等于删了重建，历史与作业归属都会跟着乱）。
    **已提交的作业不会跟着变**：Job 的提示词与参数是提交时冻住的快照。
    """
    project = ensure_by("shot", shot_id, user, "write", "镜头")
    summary = payload.summary.strip()
    if not summary:
        raise HTTPException(status_code=422, detail="镜头描述不能为空")
    row = shots.update(shot_id, summary, payload.prompt.strip(),
                       payload.duration,
                       _checked_asset_ids(project["id"], payload.asset_ids))
    if row is None:
        raise HTTPException(status_code=404, detail="镜头不存在")
    return ShotOut.from_row(row)



@router.delete("/shots/{shot_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_shot(shot_id: int,
                user=Depends(require_permission("doc:write"))) -> None:
    ensure_by("shot", shot_id, user, "write", "镜头")
    if not shots.delete(shot_id):
        raise HTTPException(status_code=404, detail="镜头不存在")


# ---------- 资产 ----------

# ---------- 资产 ----------
#
# 这一组端点是资产中心封版的主体。三条口径先写在这，免得读代码的人猜：
# 1. 全局资产（project_id 为 NULL）人人可读，写与删只有管理员 —— 见 access.ensure_asset。
# 2. 项目"引用"全局资产不复制一份：镜头的 asset_ids 里直接存那条全局资产的 id。
#    所以删项目不会动它（外键按 project_id = ? 匹配，NULL 比不中），
#    也不需要一张 asset_project_ref 中间表——引用关系本来就记在镜头上。
# 3. 被镜头引用的资产**不给删**（409），不提供 force 旁路。悬空引用比删不掉难查得多。

_ASSET_SCOPES = ("project", "global", "all")


def _shots_visible(rows, user) -> list[dict]:
    """引用清单里只留这个人看得见的。

    详情页的引用列表与删除保护共用这一份判断，不然会出现
    "界面上看不到任何引用、删除却被 409 拦住"这种说不通的情况。
    """
    out = []
    for row in rows:
        try:
            ensure(user, row["project_id"], "read")
        except HTTPException:
            continue
        out.append(dict(row))
    return out


@router.get("/projects/{project_id}/assets", response_model=list[AssetOut])
def list_assets(project_id: int, kind: str | None = None, scope: str = "all",
                user=Depends(require_permission("asset:read"))) -> list[AssetOut]:
    """当前项目看得见的资产：自有 ＋ 全局池。scope 只要其中一半时传 project／global。"""
    ensure(user, project_id, "read")
    if scope not in _ASSET_SCOPES:
        raise HTTPException(status_code=422,
                            detail=f"scope 只认 {'／'.join(_ASSET_SCOPES)}")
    return [AssetOut.from_row(row) for row in assets.list_visible(project_id, kind, scope)]


@router.post("/projects/{project_id}/assets", response_model=AssetOut,
             status_code=http_status.HTTP_201_CREATED)
def create_asset(project_id: int, payload: AssetIn,
                 user=Depends(require_permission("asset:write"))) -> AssetOut:
    """登记资产。scope=global 时那个 project_id 只是操作上下文，行不属于任何项目。"""
    ensure(user, project_id, "write")
    target = None if payload.scope == "global" else project_id
    if target is None and not is_manager(user):
        raise HTTPException(status_code=403, detail="全局资产只有管理员能新建")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="名称不能为空")
    if assets.exists_name(target, payload.kind, name):
        raise HTTPException(status_code=409,
                            detail="同一范围、同一类型里已经有这个名字，去合并而不是新建")
    row = assets.create(target, payload.kind, name, payload.summary.strip(),
                        payload.consistency_prompt.strip(),
                        _clean_aliases(payload.aliases), clean_tags(payload.tags))
    return AssetOut.from_row(row)


@router.get("/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: int, user=Depends(require_permission("asset:read"))) -> AssetOut:
    ensure_asset(asset_id, user, "read")
    return AssetOut.from_row(assets.get(asset_id))


@router.put("/assets/{asset_id}", response_model=AssetOut)
def update_asset(asset_id: int, payload: AssetIn,
                 user=Depends(require_permission("asset:write"))) -> AssetOut:
    """整体提交：名称／类型／简介／提示词／别名／标签／范围一次改完。

    同一个字段不留第二个入口（指令 §4.1），所以这里不接受部分更新。
    **全局资产不能收回成某个项目的资产**：它可能正被好几个项目的镜头引用，
    挪进一个项目等于让其他引用悬空——那是数据损坏，不是"改归属"。
    项目 → 全局是放行的（管理员）：id 不变，原有引用照旧有效。
    """
    current = ensure_asset(asset_id, user, "write")
    if payload.scope == "global" and current["project_id"] is None:
        pass                                   # 本来就是全局，范围没变
    elif payload.scope == "global":
        if not is_manager(user):
            raise HTTPException(status_code=403, detail="把项目资产升为全局资产只有管理员能做")
    elif current["project_id"] is None:
        raise HTTPException(status_code=409, detail="全局资产不能收回成单个项目的资产："
                                                    "它可能正被多个项目引用，收回会让引用悬空")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="名称不能为空")
    target = None if payload.scope == "global" else current["project_id"]
    if assets.exists_name(target, payload.kind, name, exclude_id=asset_id):
        raise HTTPException(status_code=409,
                            detail="同一范围、同一类型里已经有这个名字，去合并而不是新建")
    row = assets.update(asset_id, kind=payload.kind, name=name,
                        summary=payload.summary.strip(),
                        consistency_prompt=payload.consistency_prompt.strip(),
                        aliases=_clean_aliases(payload.aliases),
                        tags=clean_tags(payload.tags), project_id=target)
    return AssetOut.from_row(row)


@router.put("/assets/{asset_id}/cover", response_model=AssetOut)
def set_asset_cover(asset_id: int, payload: AssetCoverIn,
                    user=Depends(require_permission("asset:write"))) -> AssetOut:
    """设封面＝把那张图挪到数组第一位。不加 is_cover 列，顺序本身就是真相。"""
    ensure_asset(asset_id, user, "write")
    row, moved = assets.set_cover(asset_id, payload.path)
    if row is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    if not moved:
        raise HTTPException(status_code=404, detail="这张图不在该资产下")
    return AssetOut.from_row(row)


@router.get("/assets/{asset_id}/shots", response_model=list[AssetShotOut])
def asset_shots(asset_id: int,
                user=Depends(require_permission("asset:read"))) -> list[AssetShotOut]:
    """哪些镜头引用了它，跨项目查得到——所以只回这个人看得见的那部分。

    不可见的直接过滤掉，不计数也不列（规格 01-2.2-R1：看不见就是不存在）。
    """
    ensure_asset(asset_id, user, "read")
    rows = _shots_visible(assets.referencing_shots(asset_id), user)
    return [AssetShotOut(**{k: r[k] for k in AssetShotOut.model_fields}) for r in rows]


@router.delete("/assets/{asset_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_asset(asset_id: int,
                 user=Depends(require_permission("asset:write"))) -> None:
    """删资产并清掉它登记过的图。被镜头引用就不给删。

    不提供 force 旁路：镜头里的悬空 asset_id 比"删不掉"难查得多，
    而且它会在创作中心静默显示成 #12 这种没有名字的东西。
    要删先去创作中心解除引用。
    """
    ensure_asset(asset_id, user, "write")
    refs = _shots_visible(assets.referencing_shots(asset_id), user)
    if refs:
        first = refs[0]
        raise HTTPException(
            status_code=409,
            detail=f"这个资产被 {len(refs)} 条镜头引用（如《{first['project_name']}》"
                   f"第 {first['episode_seq']} 集 镜 {first['seq']}），先去创作中心解除引用")
    # 先拿到图片清单再删行：删完就取不到了，文件会变成孤儿
    row = assets.get(asset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    images = json.loads(row["ref_images"] or "[]")
    assets.delete(asset_id)

    for rel in images:
        target = resolve_within(FILES_DIR, rel)
        if target is not None and target.is_file():
            target.unlink()
    folder = (FILES_DIR / str(asset_id)).resolve()
    if listdir_empty(folder):
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
    # 全局资产也要能传图／删图，所以走 ensure_asset 而不是 ensure_by("asset")：
    # 后者要绕回项目，而全局资产的 project_id 是 NULL，绕不回去会被错判成 404
    ensure_asset(asset_id, user, "write")
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
    # 全局资产也要能传图／删图，所以走 ensure_asset 而不是 ensure_by("asset")：
    # 后者要绕回项目，而全局资产的 project_id 是 NULL，绕不回去会被错判成 404
    ensure_asset(asset_id, user, "write")
    row, removed = assets.remove_image(asset_id, path)
    if row is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    if not removed:
        raise HTTPException(status_code=404, detail="这张图不在该资产下")

    # 只删 FILES_DIR 里的文件：path 来自调用方，拼完解析后必须仍在上传目录内
    target = resolve_within(FILES_DIR, path)
    if target is not None and target.is_file():
        target.unlink()
    return AssetOut.from_row(row)
