"""创作单元、分镜、资产的接口。

路径按层级走：/api/projects/{id}/episodes → /api/episodes/{id}/shots，
这样前端拿到的永远是"当前对象下面的东西"，不用自己拼过滤条件。
"""
from functools import wraps

from fastapi import (APIRouter, Depends, File, HTTPException, Query, Request, UploadFile,
                     status as http_status)

from services import asset_candidates, content_operations, content_units, asset_media, asset_random, asset_vision, asset_upscale
from schemas.assets import AssetRandomCharacterIn, AssetRandomCharacterOut, AssetImageUpscaleIn, AssetImageUpscaleOut

import asset_studio
import asset_import_recovery
import engine_launch
import production as production_mod
from access import ensure, ensure_asset, ensure_by, is_manager
from deps import actor_name, require_permission

from models import (AssetCandidateReviewIn, AssetImageBatchIn, AssetReferenceReplacementIn, EpisodeAssetPresetIn, AssetCoverIn, AssetGenIn, AssetGenOut,
                    AssetGenPreviewOut, AssetImageImportIn, AssetImageImportOut, AssetImagePurposeIn,
                    AssetImpactOut, AssetIn, AssetOut, AssetProfileIn, AssetProfileOut,
                    AssetReversePromptIn, AssetReversePromptOut, AssetShotOut, EpisodeIn, EpisodeOut,
                    EpisodeStatusIn, ShotExtractFrameIn, ShotExtractFrameOut, ShotIn, ShotOut,
                    VariantIn, VariantOut, VariantUpdateIn)
from repositories.content import variants as variant_repo
from services import (JobInvalid, NoRoute)

router = APIRouter(prefix="/api", tags=["content"])


def _content_call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except content_operations.ContentOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

MAX_ALIASES = content_operations.MAX_ALIASES
MAX_ALIAS_LEN = content_operations.MAX_ALIAS_LEN          # 与资产名同宽：别名将来是拿去参与查重的，不该比名字长


def _clean_aliases(raw: list[str]) -> list[str]:
    return content_operations._clean_aliases(raw)


# ---------- 创作单元 ----------

@router.get("/projects/{project_id}/episodes", response_model=list[EpisodeOut])
def list_episodes(project_id: int,
                  user=Depends(require_permission("doc:read"))) -> list[EpisodeOut]:
    ensure(user, project_id, "read")
    return _content_call(content_units.list_episodes, project_id)


@router.post("/projects/{project_id}/episodes", response_model=EpisodeOut,
             status_code=http_status.HTTP_201_CREATED)
def create_episode(project_id: int, payload: EpisodeIn,
                   user=Depends(require_permission("doc:write"))) -> EpisodeOut:
    ensure(user, project_id, "write")
    return _content_call(content_units.create_episode, project_id, payload)


@router.patch("/episodes/{episode_id}/status", response_model=EpisodeOut)
def set_episode_status(episode_id: int, payload: EpisodeStatusIn,
                       user=Depends(require_permission("doc:write"))) -> EpisodeOut:
    ensure_by("episode", episode_id, user, "write", "创作单元")
    return _content_call(content_units.set_episode_status, episode_id, payload)


@router.delete("/episodes/{episode_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_episode(episode_id: int,
                   user=Depends(require_permission("doc:write"))) -> None:
    ensure_by("episode", episode_id, user, "write", "创作单元")
    return _content_call(content_units.delete_episode, episode_id, actor_name(user))


# ---------- 分镜 ----------

# 一镜最多挂多少个资产。不是业务上限，是"别让一条镜头把整页 chips 撑爆"的
# 界面上限：一镜里有三五个角色/场景已经很多，四十个足够。
MAX_SHOT_ASSETS = content_operations.MAX_SHOT_ASSETS


def _checked_asset_ids(project_id: int, raw: list[int]) -> list[int]:
    try:
        return content_operations._checked_asset_ids(project_id, raw)
    except content_operations.ContentOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _checked_variant_ids(project_id: int, raw: list[int],
                         asset_ids: list[int]) -> list[int]:
    try:
        return content_operations._checked_variant_ids(project_id, raw, asset_ids)
    except content_operations.ContentOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _variant_labels(rows: list[dict]) -> dict[int, dict[int, str]]:
    return content_operations._variant_labels(rows)


@router.get("/episodes/{episode_id}/shots", response_model=list[ShotOut])
def list_shots(episode_id: int,
               user=Depends(require_permission("doc:read"))) -> list[ShotOut]:
    ensure_by("episode", episode_id, user, "read", "创作单元")
    return _content_call(content_units.list_shots, episode_id)


@router.get("/episodes/{episode_id}/asset-preparation",
            dependencies=[Depends(require_permission("asset:read"))])
def episode_asset_preparation(episode_id: int,
                              user=Depends(require_permission("doc:read"))) -> dict:
    project = ensure_by("episode", episode_id, user, "read", "创作单元")
    return content_units.asset_preparation(episode_id, int(project["id"]))


@router.get("/episodes/{episode_id}/asset-presets")
def episode_asset_presets(episode_id: int,
                          user=Depends(require_permission("doc:read"))) -> list[dict]:
    project = ensure_by("episode", episode_id, user, "read", "创作单元")
    return _content_call(content_units.episode_asset_presets, episode_id, int(project["id"]))


@router.post("/episodes/{episode_id}/asset-presets", status_code=http_status.HTTP_204_NO_CONTENT)
def save_episode_asset_preset(episode_id: int, payload: EpisodeAssetPresetIn,
                              user=Depends(require_permission("doc:write"))) -> None:
    project = ensure_by("episode", episode_id, user, "write", "创作单元")
    _content_call(content_units.save_episode_asset_preset, episode_id, int(project["id"]), payload)


@router.delete("/episodes/{episode_id}/asset-presets", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_episode_asset_preset(episode_id: int, name: str = Query(min_length=1, max_length=60),
                                user=Depends(require_permission("doc:write"))) -> None:
    ensure_by("episode", episode_id, user, "write", "创作单元")
    _content_call(content_units.delete_episode_asset_preset, episode_id, name)


def _shot_form_fields(payload) -> dict:
    return content_operations._shot_form_fields(payload)


@router.post("/episodes/{episode_id}/shots", response_model=ShotOut,
             status_code=http_status.HTTP_201_CREATED)
def create_shot(episode_id: int, payload: ShotIn,
                user=Depends(require_permission("doc:write"))) -> ShotOut:
    project = ensure_by("episode", episode_id, user, "write", "创作单元")
    return _content_call(content_units.create_shot, episode_id, payload, int(project["id"]))


@router.put("/shots/{shot_id}", response_model=ShotOut)
def update_shot(shot_id: int, payload: ShotIn,
                user=Depends(require_permission("doc:write"))) -> ShotOut:
    project = ensure_by("shot", shot_id, user, "write", "镜头")
    return _content_call(content_units.update_shot, shot_id, payload, int(project["id"]))



@router.delete("/shots/{shot_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_shot(shot_id: int,
                user=Depends(require_permission("doc:write"))) -> None:
    ensure_by("shot", shot_id, user, "write", "镜头")
    return _content_call(content_units.delete_shot, shot_id)


@router.post("/shots/{shot_id}/extract-frame", response_model=ShotExtractFrameOut)
def extract_shot_frame(shot_id: int, payload: ShotExtractFrameIn | None = None,
                       user=Depends(require_permission("doc:write"))) -> ShotExtractFrameOut:
    project = ensure_by("shot", shot_id, user, "write", "镜头")
    try:
        return content_operations.extract_shot_frame(shot_id, payload, int(project["id"]))
    except content_operations.ContentOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


# ---------- 资产 ----------

# ---------- 资产 ----------
#
# 这一组端点是资产中心封版的主体。三条口径先写在这，免得读代码的人猜：
# 1. 全局资产（project_id 为 NULL）人人可读，写与删只有管理员 —— 见 access.ensure_asset。
# 2. 项目"引用"全局资产不复制一份：镜头的 asset_ids 里直接存那条全局资产的 id。
#    所以删项目不会动它（外键按 project_id = ? 匹配，NULL 比不中），
#    也不需要一张 asset_project_ref 中间表——引用关系本来就记在镜头上。
# 3. 被镜头引用的资产**不给删**（409），不提供 force 旁路。悬空引用比删不掉难查得多。

_ASSET_SCOPES = content_units._ASSET_SCOPES


def _shots_visible(rows, user) -> list[dict]:
    """引用清单里只留这个人看得见的。

    详情页的引用列表与删除保护共用这一份判断，不然会出现
    "界面上看不到任何引用、删除却被 409 拦住"这种说不通的情况。

    **判可见性按项目去重**：这条引用清单是按资产反查出来的，一条被 50 镜引用的资产
    就有 50 行，而它们大概率只来自两三的项目。原来每行各调一次 `ensure`，
    而 `ensure` 自己开一条连接、打两条 SELECT——于是"打开一条资产的详情页"
    变成几十次建连（实测含建连约 0.2ms／行，看着不大，乘上引用数就不小了）。
    """
    allowed: set[int] = set()
    denied: set[int] = set()
    out = []
    for row in rows:
        pid = row["project_id"]
        if pid in denied:
            continue
        if pid not in allowed:
            try:
                ensure(user, pid, "read")
            except HTTPException:
                denied.add(pid)
                continue
            allowed.add(pid)
        out.append(dict(row))
    return out


@router.get("/projects/{project_id}/assets", response_model=list[AssetOut])
def list_assets(project_id: int, kind: str | None = None, scope: str = "all",
                user=Depends(require_permission("asset:read"))) -> list[AssetOut]:
    ensure(user, project_id, "read")
    return _content_call(content_units.list_assets, project_id, kind, scope)


@router.post("/projects/{project_id}/assets", response_model=AssetOut,
             status_code=http_status.HTTP_201_CREATED)
def create_asset(project_id: int, payload: AssetIn,
                 user=Depends(require_permission("asset:write"))) -> AssetOut:
    ensure(user, project_id, "write")
    if payload.scope == "global" and not is_manager(user):
        raise HTTPException(status_code=403, detail="全局资产只有管理员能新建")
    return _content_call(content_units.create_asset, project_id, payload)


@router.post("/projects/{project_id}/assets/random-character",
             response_model=AssetRandomCharacterOut)
def random_character_asset(project_id: int, payload: AssetRandomCharacterIn,
                           request: Request,
                           user=Depends(require_permission("asset:write"))) -> AssetRandomCharacterOut:
    ensure(user, project_id, "write")
    client = request.client.host if request.client else ""
    allow_auto_start = client in engine_launch.LOOPBACK and is_manager(user)
    return _content_call(
        asset_random.random_character, project_id, int(user["id"]), payload,
        allow_auto_start=allow_auto_start,
    )


@router.get("/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: int, user=Depends(require_permission("asset:read"))) -> AssetOut:
    ensure_asset(asset_id, user, "read")
    return _content_call(content_units.get_asset, asset_id)


@router.put("/assets/{asset_id}", response_model=AssetOut)
def update_asset(asset_id: int, payload: AssetIn,
                 user=Depends(require_permission("asset:write"))) -> AssetOut:
    current = _asset_for(asset_id, user, "write")
    if payload.scope == "global" and current["project_id"] is not None and not is_manager(user):
        raise HTTPException(status_code=403, detail="把项目资产升为全局资产只有管理员能做")
    return _content_call(content_units.update_asset, asset_id, payload, current)


@router.put("/assets/{asset_id}/cover", response_model=AssetOut)
def set_asset_cover(asset_id: int, payload: AssetCoverIn,
                    user=Depends(require_permission("asset:write"))) -> AssetOut:
    ensure_asset(asset_id, user, "write")
    return _content_call(content_units.set_asset_cover, asset_id, payload)


@router.put("/assets/{asset_id}/image-purpose", status_code=http_status.HTTP_204_NO_CONTENT)
def set_asset_image_purpose(asset_id: int, payload: AssetImagePurposeIn,
                            user=Depends(require_permission("asset:write"))) -> None:
    ensure_asset(asset_id, user, "write")
    _content_call(asset_media.set_image_purpose, asset_id, payload)


@router.post("/assets/{asset_id}/reverse-prompt", response_model=AssetReversePromptOut)
def reverse_asset_prompt(asset_id: int, payload: AssetReversePromptIn,
                         user=Depends(require_permission("asset:write"))) -> AssetReversePromptOut:
    ensure_asset(asset_id, user, "write")
    return _content_call(asset_vision.reverse_prompt, asset_id, int(user["id"]), payload)


@router.get("/assets/{asset_id}/shots", response_model=list[AssetShotOut])
def asset_shots(asset_id: int,
                user=Depends(require_permission("asset:read"))) -> list[AssetShotOut]:
    ensure_asset(asset_id, user, "read")
    rows = _shots_visible(content_units.asset_reference_rows(asset_id), user)
    return _content_call(content_units.asset_shots, asset_id, rows)


def _project_reference_asset(project_id: int, asset_id: int, user) -> None:
    asset = ensure_asset(asset_id, user, "read")
    if asset["project_id"] not in (None, project_id):
        raise HTTPException(status_code=404, detail="资产不存在")


@router.get("/projects/{project_id}/assets/{asset_id}/reference-usage",
            dependencies=[Depends(require_permission("asset:read"))])
def asset_reference_usage(project_id: int, asset_id: int,
                          user=Depends(require_permission("job:read"))) -> list[dict]:
    ensure(user, project_id, "read")
    _project_reference_asset(project_id, asset_id, user)
    return asset_media.asset_reference_usage(project_id, asset_id)


@router.post("/projects/{project_id}/assets/{asset_id}/reference-usage",
             dependencies=[Depends(require_permission("asset:read")), Depends(require_permission("job:read"))])
def replace_asset_references(project_id: int, asset_id: int, payload: AssetReferenceReplacementIn,
                             user=Depends(require_permission("doc:write"))) -> dict:
    ensure(user, project_id, "write")
    _project_reference_asset(project_id, asset_id, user)
    return {"updated": _content_call(asset_media.replace_asset_references, project_id, asset_id, payload)}


@router.get("/projects/{project_id}/assets/{asset_id}/appearances",
            dependencies=[Depends(require_permission("asset:read"))])
def recent_asset_appearances(project_id: int, asset_id: int,
                             user=Depends(require_permission("job:read"))) -> list[dict]:
    ensure(user, project_id, "read")
    _project_reference_asset(project_id, asset_id, user)
    return asset_media.character_appearances(project_id, asset_id)


@router.get("/assets/{asset_id}/impact", response_model=AssetImpactOut)
def asset_impact(asset_id: int,
                 user=Depends(require_permission("asset:read"))) -> dict:
    """改了这条资产，哪些镜头与哪些已生成产物会受影响（任务书 §6.3、场景三）。

    与 `/api/assets/{id}/shots` 的分工：那条只列引用关系，这条在引用关系上
    再判"最新那条成功视频的源版本还对不对、签过字没有、建议做什么"。
    可见性沿用同一个出口 `_shots_visible`——看不见就是不存在，
    多一个聚合接口不该多一条泄露通道。
    """
    ensure_asset(asset_id, user, "read")
    rows = production_mod.referencing_shots(asset_id)
    visible = {int(row["shot_id"]) for row in _shots_visible(rows, user)}
    return production_mod.asset_impact(asset_id, only_shot_ids=visible)


@router.delete("/assets/{asset_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_asset(asset_id: int,
                 user=Depends(require_permission("asset:write"))) -> None:
    ensure_asset(asset_id, user, "write")
    refs = _shots_visible(content_units.asset_reference_rows(asset_id), user)
    return _content_call(asset_media.delete_asset, asset_id, refs)


# ---------- 定妆图 ----------

# 类型表与文件头校验住在 imagetypes.py：从生成产物导入资产图那条路要的是反方向
# （只有字节、没有客户端声明的 content-type），两处各握一份魔数表迟早对不上。


@router.post("/assets/{asset_id}/images", response_model=AssetOut)
def upload_asset_image(asset_id: int, file: UploadFile = File(...),
                       user=Depends(require_permission("asset:write"))) -> AssetOut:
    # 这里是 `def` 而不是 `async def`（2026-11-08，P1）：函数体里除了读上传流，
    # 还要**同步**写盘（20MB 的 write_bytes）与一次同步 DB 写（add_image，
    # 最坏要等 5 秒的写锁）。写在 `async def` 里就是把这些阻塞直接压在
    # 唯一的事件循环上——一次上传期间 `/api/worker/status`、健康检查、
    # 其它任何 async 请求全在排队。声明成 `def` 之后 FastAPI 会把这个
    # 端点整体丢进线程池，与 platform 里其余 190+ 个 `def` 端点同一条路。
    # 代价只是 `await file.read(...)` 变成同步的 `file.file.read(...)`。
    # 全局资产也要能传图／删图，所以走 ensure_asset 而不是 ensure_by("asset")：
    # 后者要绕回项目，而全局资产的 project_id 是 NULL，绕不回去会被错判成 404
    ensure_asset(asset_id, user, "write")
    return _content_call(asset_media.upload_asset_image, asset_id, file.file, file.content_type)


@router.post("/assets/{asset_id}/images/upscale", response_model=AssetImageUpscaleOut)
def upscale_asset_image(asset_id: int, payload: AssetImageUpscaleIn,
                        user=Depends(require_permission("asset:write"))) -> AssetImageUpscaleOut:
    ensure_asset(asset_id, user, "write")
    return _content_call(asset_upscale.upscale_asset_image, asset_id, payload)


@router.delete("/assets/{asset_id}/images", response_model=AssetOut)
def delete_asset_image(asset_id: int, path: str,
                       user=Depends(require_permission("asset:write"))) -> AssetOut:
    # 全局资产也要能传图／删图，所以走 ensure_asset 而不是 ensure_by("asset")：
    # 后者要绕回项目，而全局资产的 project_id 是 NULL，绕不回去会被错判成 404
    ensure_asset(asset_id, user, "write")
    return _content_call(asset_media.delete_asset_image, asset_id, path)


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
        except asset_import_recovery.ImportRecoveryPending as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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
    return _content_call(content_units.asset_row, asset_id)


@router.get("/assets/{asset_id}/character-profile", response_model=AssetProfileOut)
@studio_errors
def get_character_profile(asset_id: int,
                          user=Depends(require_permission("asset:read"))) -> AssetProfileOut:
    row = _asset_for(asset_id, user, "read")
    return _content_call(content_units.get_character_profile, row)


@router.put("/assets/{asset_id}/character-profile", response_model=AssetProfileOut)
@studio_errors
def put_character_profile(asset_id: int, payload: AssetProfileIn,
                          user=Depends(require_permission("asset:write"))) -> AssetProfileOut:
    _asset_for(asset_id, user, "write")
    return _content_call(content_units.put_character_profile, asset_id, payload)


@router.get("/assets/{asset_id}/variants", response_model=list[VariantOut])
@studio_errors
def list_variants(asset_id: int,
                  user=Depends(require_permission("asset:read"))) -> list[VariantOut]:
    # 可见性从资产继承：只校验"形态 id 查得到"等于任何人都能读别人的形态
    _asset_for(asset_id, user, "read")
    return _content_call(content_units.list_variants, asset_id)


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
    return _content_call(asset_media.delete_variant, variant_id)


@router.post("/asset-variants/{variant_id}/images", response_model=VariantOut)
@studio_errors
def upload_variant_image(variant_id: int, file: UploadFile = File(...),
                         user=Depends(require_permission("asset:write"))) -> VariantOut:
    row = variant_repo.get(variant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="形态不存在")
    asset = ensure_asset(int(row["asset_id"]), user, "write")
    return _content_call(asset_media.upload_variant_image, variant_id, int(asset["id"]), file.file, file.content_type)


@router.delete("/asset-variants/{variant_id}/images", response_model=VariantOut)
@studio_errors
def delete_variant_image(variant_id: int, path: str,
                         user=Depends(require_permission("asset:write"))) -> VariantOut:
    row = variant_repo.get(variant_id)
    if row is None:
        raise asset_studio.VariantMissing("形态不存在")
    ensure_asset(row["asset_id"], user, "write")
    return _content_call(asset_media.delete_variant_image, variant_id, path)


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
                           limit: int = Query(12, ge=1),
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
    # 作业也必须是自己**看得见**的（2026-11-10 审计修的真 bug）。
    #
    # 原来这里只判了"资产归你"，没判 `payload.job_id` 那条作业归谁：
    # 于是拿别人项目里一条成功图像作业的 id，就能把它的产物字节复制进自己的
    # 资产，再用 `/files/...` 下载出来——跨项目读取他人产物（实测两份字节完全相同）。
    # 同一模块的 `generation_view`（`asset_studio.py:646-648`）与
    # `services/doc_ai.py` 的 `_ai_job_for_doc` 都有这道门，唯独这条导入路径漏了。
    #
    # 口径与 `routers/jobs.py:33-39` 的 `_load` 等价：作业不存在 → 404「作业不存在」；
    # 存在但项目不可见 → 404「项目不存在」。看不见一律 404，不因为行还在就告诉
    # 对方"这行存在，只是你不配看"。
    ensure_by("job", payload.job_id, user, "read", "作业")
    # 一个事务：形态的图清单与资产的 ref_images 都是读-改-写，
    # BEGIN IMMEDIATE 保证两个人同时存图不会互相覆盖那份 JSON
    out = asset_studio.import_generated_image_tx(asset, payload)
    return AssetImageImportOut(**out)

@router.get("/assets/{asset_id}/candidate-review/{job_id}",
            dependencies=[Depends(require_permission("asset:read"))])
@studio_errors
def get_candidate_review(asset_id: int, job_id: int,
                         user=Depends(require_permission("job:read"))) -> dict[int, str]:
    asset = _asset_for(asset_id, user, "read")
    ensure_by("job", job_id, user, "read", "作业")
    return asset_candidates.asset_candidate_review(asset, job_id)


@router.put("/assets/{asset_id}/candidate-review/{job_id}")
@studio_errors
def put_candidate_review(asset_id: int, job_id: int, payload: AssetCandidateReviewIn,
                         user=Depends(require_permission("asset:write"))) -> dict[int, str]:
    asset = _asset_for(asset_id, user, "write")
    ensure_by("job", job_id, user, "read", "作业")
    return asset_candidates.update_asset_candidate_review(asset, job_id, payload)


@router.post("/assets/{asset_id}/generated-images/batch")
@studio_errors
def batch_import_candidates(asset_id: int, payload: AssetImageBatchIn,
                            user=Depends(require_permission("asset:write"))) -> dict:
    asset = _asset_for(asset_id, user, "write")
    ensure_by("job", payload.job_id, user, "read", "作业")
    return asset_candidates.import_asset_candidates(asset, payload)
