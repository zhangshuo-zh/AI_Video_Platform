"""创作单元、分镜、资产的接口。

路径按层级走：/api/projects/{id}/episodes → /api/episodes/{id}/shots，
这样前端拿到的永远是"当前对象下面的东西"，不用自己拼过滤条件。
"""
import json
import uuid
from functools import wraps
from pathlib import Path

from fastapi import (APIRouter, Depends, File, HTTPException, UploadFile,
                     status as http_status)

import asset_studio
from access import ensure, ensure_asset, ensure_by, is_manager
from config import FILES_DIR
from db import transaction
from deps import actor_name, require_permission
from imagetypes import ALLOWED_TYPES, MAX_IMAGE_BYTES, is_heic, looks_like

from models import (ASSET_KINDS, asset_version, AssetCoverIn, AssetGenIn, AssetGenOut,
                    AssetGenPreviewOut, AssetImageImportIn, AssetImageImportOut,
                    AssetIn, AssetOut, AssetProfileIn, AssetProfileOut, AssetShotOut,
                    EpisodeIn, EpisodeOut, EpisodeStatusIn, profile_version, ShotIn,
                    ShotOut, VariantIn, VariantOut, VariantUpdateIn)
from paths import listdir_empty, resolve_within
from repositories.content import assets, episodes, shots
from repositories.content import profiles as profile_repo
from repositories.content import variants as variant_repo
from services import (JobInvalid, NoRoute, clean_tags, delete_episode_cascade,
                      shot_variant_ids, shots_view)

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
    # 走 services 的那一条而不是直接 episodes.delete()：文档行是 SQL 级联带走的，
    # 正文文件不在数据库里，行一删路径就查不回来了（详见 delete_episode_cascade）。
    if not delete_episode_cascade(episode_id, actor=actor_name(user)):
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


def _checked_variant_ids(project_id: int, raw: list[int],
                         asset_ids: list[int]) -> list[int]:
    """把镜头引用的形态收敛成一串服务端认可的 id，并**顺带补齐父资产引用**。

    三条纪律：
    1. 形态必须可见（父资产是本项目的或全局池的）——`variants.usable_ids` 判的就是这个；
    2. 引了形态就必须引它的父资产。这里不是"拒绝"而是**自动把父资产加进 asset_ids**：
       只勾"雨夜外套"没勾"掌灯人"的人并非少数，而漏掉父资产会让删除保护、
       引用计数、资产库那格"关联 N 镜"三处同时看不见这条引用——那是数据损坏，
       不是用户的错。补上之后两个清单永远一致，界面上也不用多解释一句。
    3. 去重保序，规则与 `_checked_asset_ids` 一样。
    """
    ids: list[int] = []
    for value in raw:
        try:
            variant_id = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="形态 id 必须是整数") from exc
        if variant_id <= 0:
            raise HTTPException(status_code=422, detail="形态 id 必须是正整数")
        if variant_id not in ids:
            ids.append(variant_id)
    if len(ids) > MAX_SHOT_ASSETS:
        raise HTTPException(status_code=422,
                            detail=f"一镜最多引用 {MAX_SHOT_ASSETS} 个形态")
    if ids and variant_repo.usable_ids(project_id, ids) != set(ids):
        raise HTTPException(
            status_code=422,
            detail="引用了不存在、或父资产不属于这个项目也不是全局资产的形态 id")
    for variant_id in ids:
        parent = variant_repo.parents_of([variant_id]).get(variant_id)
        if parent is not None and parent not in asset_ids:
            asset_ids.append(parent)
    return ids


def _variant_labels(rows: list[dict]) -> dict[int, dict[int, str]]:
    """一批镜头行 → {shot_id: {形态 id: 「资产名 · 形态名」}}，一次读齐。

    放在服务端而不是让分镜表逐个 id 回查：一集十几镜、每镜两三个形态，
    浏览器去查就是几十趟请求，而且它还得先知道形态属于哪条资产才拼得出名字。
    """
    all_ids: set[int] = set()
    per_shot: dict[int, list[int]] = {}
    for row in rows:
        ids = sorted(shot_variant_ids(row))
        per_shot[int(row["id"])] = ids
        all_ids |= set(ids)
    if not all_ids:
        return {}
    got = variant_repo.get_many(sorted(all_ids))
    owners = assets.get_many({row["asset_id"] for row in got.values()})
    out: dict[int, dict[int, str]] = {}
    for shot_id, ids in per_shot.items():
        labels = {}
        for variant_id in ids:
            variant = got.get(variant_id)
            if variant is None:
                # 形态被删了但镜头还指着它：标签留 id，界面上看得见"这里断过"，
                # 比凭空消失一条引用好查（同 `assetChip` 对悬空资产 id 的处理）。
                labels[variant_id] = f"#{variant_id}（形态已删）"
                continue
            owner = owners.get(variant["asset_id"])
            labels[variant_id] = (f"{owner['name']} · {variant['name']}" if owner
                                  else f"{variant['name']}")
        out[shot_id] = labels
    return out


@router.get("/episodes/{episode_id}/shots", response_model=list[ShotOut])
def list_shots(episode_id: int,
               user=Depends(require_permission("doc:read"))) -> list[ShotOut]:
    ensure_by("episode", episode_id, user, "read", "创作单元")
    items = shots_view(episode_id)
    labels = _variant_labels([item["shot"] for item in items])
    return [ShotOut.from_row(item["shot"], source_stale=item["source_stale"],
                             variant_labels=labels.get(int(item["shot"]["id"]), {}))
            for item in items]


@router.post("/episodes/{episode_id}/shots", response_model=ShotOut,
             status_code=http_status.HTTP_201_CREATED)
def create_shot(episode_id: int, payload: ShotIn,
                user=Depends(require_permission("doc:write"))) -> ShotOut:
    project = ensure_by("episode", episode_id, user, "write", "创作单元")
    summary = payload.summary.strip()
    if not summary:
        raise HTTPException(status_code=422, detail="镜头描述不能为空")
    asset_ids = _checked_asset_ids(project["id"], payload.asset_ids)
    # 顺序有讲究：先收敛形态（它会往 asset_ids 里补父资产），再落库。
    # 反过来的话"只勾了形态、没勾人"这条引用就进了库，而删除保护看不见它。
    variant_ids = _checked_variant_ids(project["id"], payload.variant_ids, asset_ids)
    row = shots.create(episode_id, summary, payload.prompt.strip(),
                       payload.duration, asset_ids, variant_ids)
    # 回显带形态名字：新建那一行马上就会出现在分镜表里，界面不该先闪一下 #12
    return ShotOut.from_row(row, variant_labels=_variant_labels([row]).get(int(row["id"]), {}))


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
    asset_ids = _checked_asset_ids(project["id"], payload.asset_ids)
    variant_ids = _checked_variant_ids(project["id"], payload.variant_ids, asset_ids)
    row = shots.update(shot_id, summary, payload.prompt.strip(),
                       payload.duration, asset_ids, variant_ids)
    if row is None:
        raise HTTPException(status_code=404, detail="镜头不存在")
    return ShotOut.from_row(row, variant_labels=_variant_labels([row]).get(int(row["id"]), {}))



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
    # 这里要的是**完整行**，不是 `ensure_asset` 那三列：弱版本闸要读 updated_at。
    # 多花一次 SELECT 比去改那个被九条路由共用的 SELECT 稳（同 `_asset_for` 的理由）。
    current = _asset_for(asset_id, user, "write")
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
    # 弱版本闸：界面带回"我看过的那一版"的令牌。只在这次带了才判
    # （脚本与别的调用方不带＝行为不变），冲突回 409 而不是覆盖。
    # 令牌为什么不用 `updated_at` 本身：那一列精度只到**秒**，两人同一秒内先后保存
    # 时它一模一样，闸就成了摆设。见 `models.asset_version`。
    seen = (payload.seen_version or "").strip()
    now = asset_version(current)
    if seen and seen != now:
        raise HTTPException(
            status_code=409,
            detail="这条资产在你打开之后又被改过（库里这一版是 " + now + "）。"
                   "这次没有保存，你写的字还在界面上；"
                   "先刷新对一遍再决定要不要覆盖——确认要顶掉就留空这一格再保存一次")
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
    # 这一镜用的是这条资产的哪个形态：一次查好再按 shot_id 分桶，
    # 不在上面那个循环里逐镜回库（引用清单最长可能几十条）。
    names: dict[int, list[str]] = {}
    for hit in variant_repo.referencing_shots(asset_id):
        names.setdefault(int(hit["shot_id"]), []).append(hit["variant_name"])
    out = []
    for row in rows:
        data = {k: row[k] for k in AssetShotOut.model_fields if k != "variant_names"}
        out.append(AssetShotOut(**data, variant_names=names.get(int(row["shot_id"]), [])))
    return out


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

# 类型表与文件头校验住在 imagetypes.py：从生成产物导入资产图那条路要的是反方向
# （只有字节、没有客户端声明的 content-type），两处各握一份魔数表迟早对不上。


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
    if is_heic(file.content_type, data[:32]):
        raise HTTPException(status_code=415,
                            detail="iPhone 的 HEIC 浏览器显示不了，先转成 JPG 或 PNG 再传")
    if not looks_like(file.content_type, data[:12]):
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


# ---------- 资产增强：人物档案、命名形态、生成与产物导入（迁移 0033） ----------

def studio_errors(fn):
    """把 asset_studio 的领域异常换成 HTTP 状态码，映射只写这一处。

    九条新路由各写一份 try/except 的话，第七个拷贝一定会和第一个不一致——
    同一个错误在两个端点上回不同状态码，是接口最难查的那类漂移。
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except asset_studio.VariantMissing as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except asset_studio.ImportImageRejected as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except (asset_studio.VariantConflict, asset_studio.ImportUnusable) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (asset_studio.AssetGenInvalid, asset_studio.ProfileInvalid,
                asset_studio.ImportArtifactBad) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # 提交作业那条路的校验异常必须在这一层接住：JobInvalid 一路冒到 FastAPI
        # 就是 500 + "Internal Server Error"，人看不出是自己哪一步没填对，
        # 日志里也只剩一条堆栈。它是"输入不成立"，不是"服务器坏了"。
        except (JobInvalid, NoRoute) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return wrapper


def _asset_for(asset_id: int, user, need: str):
    """过 `access` 那道门，再把**完整行**取回来。

    `ensure_asset` 只 SELECT id/project_id/name——够它自己判归属，不够我们读 `kind`。
    多取一次行比去改那个函数的 SELECT 更稳：它被九条既有路由共用，加一列等于
    给那九条各做一次回归。
    """
    ensure_asset(asset_id, user, need)
    row = assets.get(asset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    return row


@router.get("/assets/{asset_id}/character-profile", response_model=AssetProfileOut)
@studio_errors
def get_character_profile(asset_id: int,
                          user=Depends(require_permission("asset:read"))) -> AssetProfileOut:
    row = _asset_for(asset_id, user, "read")
    if row["kind"] != "character":
        # 明确说"这类资产没有档案这一层"，而不是回一份空档案让人以为数据丢了
        raise asset_studio.ProfileInvalid(
            f"只有人物资产有文字档案；这条是{ASSET_KINDS.get(row['kind'], row['kind'])}")
    data = profile_repo.get(asset_id)
    return AssetProfileOut(**data, version=profile_version(data))


@router.put("/assets/{asset_id}/character-profile", response_model=AssetProfileOut)
@studio_errors
def put_character_profile(asset_id: int, payload: AssetProfileIn,
                          user=Depends(require_permission("asset:write"))) -> AssetProfileOut:
    _asset_for(asset_id, user, "write")     # 权限与"这条资产存在"在这一道判
    # 令牌压的是"档案时间＋档案四段"（见 `models.profile_version`，那里写了为什么
    # 不含资产时间）。判在路由而不是 `save_profile` 里：save_profile 还被别处复用，
    # 把它改成"必须带令牌才能调"会连带挡住那些没有编辑界面的调用方。
    seen = (payload.seen_version or "").strip()
    current = profile_repo.get(asset_id)
    if seen and seen != profile_version(current):
        raise HTTPException(
            status_code=409,
            detail="这份人物档案在你打开之后又被改过。这次没有保存，"
                   "你写的字还在界面上；先刷新把最新的一份读进来对一遍，"
                   "确认要顶掉就留空这一格再保存一次")
    out = asset_studio.save_profile(asset_id, payload)
    return AssetProfileOut(**out, version=profile_version(out))


@router.get("/assets/{asset_id}/variants", response_model=list[VariantOut])
@studio_errors
def list_variants(asset_id: int,
                  user=Depends(require_permission("asset:read"))) -> list[VariantOut]:
    # 可见性从资产继承：只校验"形态 id 查得到"等于任何人都能读别人的形态
    _asset_for(asset_id, user, "read")
    return [VariantOut.from_row(row) for row in variant_repo.list_by_asset(asset_id)]


@router.post("/assets/{asset_id}/variants", response_model=VariantOut,
             status_code=http_status.HTTP_201_CREATED)
@studio_errors
def create_variant(asset_id: int, payload: VariantIn,
                   user=Depends(require_permission("asset:write"))) -> VariantOut:
    asset = _asset_for(asset_id, user, "write")
    return VariantOut.from_row(asset_studio.create_variant(asset, payload))


@router.put("/asset-variants/{variant_id}", response_model=VariantOut)
@studio_errors
def update_variant(variant_id: int, payload: VariantUpdateIn,
                   user=Depends(require_permission("asset:write"))) -> VariantOut:
    row = variant_repo.get(variant_id)
    if row is None:
        raise asset_studio.VariantMissing("形态不存在")
    ensure_asset(row["asset_id"], user, "write")
    return VariantOut.from_row(asset_studio.update_variant(variant_id, payload))


@router.delete("/asset-variants/{variant_id}", status_code=http_status.HTTP_204_NO_CONTENT)
@studio_errors
def delete_variant(variant_id: int,
                   user=Depends(require_permission("asset:write"))) -> None:
    row = variant_repo.get(variant_id)
    if row is None:
        raise asset_studio.VariantMissing("形态不存在")
    ensure_asset(row["asset_id"], user, "write")
    images = asset_studio.delete_variant(variant_id)
    # 文件在库提交之后再清：事务里只做库内写（db.transaction 的边界），
    # 反过来"文件没了、记录还在"会在画廊里留下一张打不开的图
    for rel in images:
        asset_studio.remove_imported_file(rel)


@router.delete("/asset-variants/{variant_id}/images", response_model=VariantOut)
@studio_errors
def delete_variant_image(variant_id: int, path: str,
                         user=Depends(require_permission("asset:write"))) -> VariantOut:
    """从某个形态的图清单里摘掉一张，并清掉它在 `FILES_DIR` 里的那个文件。

    这一条是本轮补上的**出口**：0033 有 `VariantRepository.remove_image`，
    但没有任何接口能调它——结果形态的图只能进不能出，导错一张就得删整个形态
    （连带把别的图也清了）。基础参考图从来都有删除入口，两边不对称就是缺一条路。

    边界与资产删图同一条：`path` 来自调用方，但只有**已经登记在这个形态下**的路径
    才摘得掉（`remove_image` 判），落盘删除再走 `resolve_within(FILES_DIR, …)`，
    所以这里既删不掉别人的图，也删不掉目录外的文件。
    """
    row = variant_repo.get(variant_id)
    if row is None:
        raise asset_studio.VariantMissing("形态不存在")
    ensure_asset(row["asset_id"], user, "write")
    with transaction():
        out, removed = variant_repo.remove_image(variant_id, path)
    if out is None:
        raise asset_studio.VariantMissing("形态不存在")
    if not removed:
        # 与"删资产的参考图"同一条口径：路径不在这一份清单里就回 404，
        # 不能回一句"删好了"而实际什么都没动——那是最难查的一种假成功。
        raise HTTPException(status_code=404, detail="这张图不在该形态下")
    asset_studio.remove_imported_file(path)
    return VariantOut.from_row(out)


@router.post("/projects/{project_id}/asset-generations", response_model=AssetGenOut,
             status_code=http_status.HTTP_202_ACCEPTED)
@studio_errors
def create_asset_generation(project_id: int, payload: AssetGenIn,
                            user=Depends(require_permission("job:submit"))) -> AssetGenOut:
    # 与 POST /api/jobs 同一条门：能读这个项目 + 有 job:submit 才能派活。
    # 花钱的闸门不在这里，在 submit_job 里的云端确认与清单价格核对。
    ensure(user, project_id, "read")
    if payload.target_asset_id is not None:
        ensure_asset(payload.target_asset_id, user, "read")
    return AssetGenOut(**asset_studio.submit_generation(project_id, payload, user["id"]))


@router.get("/projects/{project_id}/asset-generations", response_model=list[AssetGenOut])
@studio_errors
def list_asset_generations(project_id: int, target_asset_id: int | None = None,
                           limit: int = 12,
                           user=Depends(require_permission("job:read"))) -> list[AssetGenOut]:
    """这个项目（可指定某条资产）最近的资产生成作业。

    界面刷新后还要看得见"我刚生成的那批候选"——状态本来就在 job 表里，
    读回来比在浏览器里攒一份副本诚实。
    """
    ensure(user, project_id, "read")
    rows = asset_studio.recent_generations(
        project_id, target_asset_id, min(max(int(limit), 1), 50))
    return [AssetGenOut(**row) for row in rows]


@router.get("/projects/{project_id}/asset-generations/{job_id}",
            response_model=AssetGenPreviewOut)
@studio_errors
def get_asset_generation(project_id: int, job_id: int,
                         user=Depends(require_permission("job:read"))) -> AssetGenPreviewOut:
    ensure(user, project_id, "read")
    return AssetGenPreviewOut(**asset_studio.generation_view(project_id, job_id))


@router.post("/assets/{asset_id}/generated-images", response_model=AssetImageImportOut)
@studio_errors
def import_generated_image(asset_id: int, payload: AssetImageImportIn,
                           user=Depends(require_permission("asset:write"))
                           ) -> AssetImageImportOut:
    asset = _asset_for(asset_id, user, "write")
    # 一个事务：形态的图清单与资产的 ref_images 都是读-改-写，
    # BEGIN IMMEDIATE 保证两个人同时存图不会互相覆盖那份 JSON
    with transaction():
        out = asset_studio.import_generated_image(asset, payload)
    return AssetImageImportOut(**out)
