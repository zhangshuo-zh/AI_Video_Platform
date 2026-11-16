"""创作单元、分镜与资产元数据业务；权限由路由先行检查。"""

import asset_studio
from models import (ASSET_KINDS, asset_version, AssetCoverIn, AssetIn, AssetOut, AssetProfileIn, AssetProfileOut,
                    AssetShotOut, EpisodeIn, EpisodeOut, EpisodeStatusIn, profile_version,
                    ShotIn, ShotOut,
                    VariantOut)
from repositories.content import assets, episodes, shots
from repositories.content import profiles as profile_repo
from repositories.content import variants as variant_repo

from .content_operations import (ContentOperationError, _clean_aliases, _checked_asset_ids, _checked_variant_ids, _variant_labels, _shot_form_fields)
from .cascade import clean_tags, delete_episode_cascade
from .jobs import shots_view

_ASSET_SCOPES = ("project", "global", "all")


def list_episodes(project_id: int) -> list[EpisodeOut]:
    return [EpisodeOut.from_row(row) for row in episodes.list_by_project(project_id)]


def create_episode(project_id: int, payload: EpisodeIn) -> EpisodeOut:
    title = payload.title.strip()
    if not title:
        raise ContentOperationError(status_code=422, detail="标题不能为空")
    return EpisodeOut.from_row(episodes.create(project_id, title))


def set_episode_status(episode_id: int, payload: EpisodeStatusIn) -> EpisodeOut:
    """分集状态：draft|active|done。项目中心的"已定稿 N/M 集"就是它的聚合。

    权限走 doc:write——改集状态的东家是创作中心，项目中心只显示不改。
    """
    row = episodes.set_status(episode_id, payload.status)
    if row is None:
        raise ContentOperationError(status_code=404, detail="创作单元不存在")
    return EpisodeOut.from_row(row)


def delete_episode(episode_id: int, actor: str) -> None:
    # 走 services 的那一条而不是直接 episodes.delete()：文档行是 SQL 级联带走的，
    # 正文文件不在数据库里，行一删路径就查不回来了（详见 delete_episode_cascade）。
    if not delete_episode_cascade(episode_id, actor=actor):
        raise ContentOperationError(status_code=404, detail="创作单元不存在")


def list_shots(episode_id: int) -> list[ShotOut]:
    items = shots_view(episode_id)
    labels = _variant_labels([item["shot"] for item in items])
    return [ShotOut.from_row(item["shot"], source_stale=item["source_stale"],
                             variant_labels=labels.get(int(item["shot"]["id"]), {}))
            for item in items]


def create_shot(episode_id: int, payload: ShotIn, project_id: int) -> ShotOut:
    summary = payload.summary.strip()
    if not summary:
        raise ContentOperationError(status_code=422, detail="镜头描述不能为空")
    asset_ids = _checked_asset_ids(project_id, payload.asset_ids)
    # 顺序有讲究：先收敛形态（它会往 asset_ids 里补父资产），再落库。
    # 反过来的话"只勾了形态、没勾人"这条引用就进了库，而删除保护看不见它。
    variant_ids = _checked_variant_ids(project_id, payload.variant_ids, asset_ids)
    row = shots.create(episode_id, summary, payload.prompt.strip(),
                       payload.duration, asset_ids, variant_ids,
                       **_shot_form_fields(payload))
    # 回显带形态名字：新建那一行马上就会出现在分镜表里，界面不该先闪一下 #12
    return ShotOut.from_row(row, variant_labels=_variant_labels([dict(row)]).get(int(row["id"]), {}))


def update_shot(shot_id: int, payload: ShotIn, project_id: int) -> ShotOut:
    """整体改一镜：内容、提示词、时长、引用资产一次提交。

    和文档编辑同一条纪律——同一个字段不留两个入口；镜号与所属集不在
    可编辑范围里（挪镜等于删了重建，历史与作业归属都会跟着乱）。
    **已提交的作业不会跟着变**：Job 的提示词与参数是提交时冻住的快照。
    """
    summary = payload.summary.strip()
    if not summary:
        raise ContentOperationError(status_code=422, detail="镜头描述不能为空")
    asset_ids = _checked_asset_ids(project_id, payload.asset_ids)
    variant_ids = _checked_variant_ids(project_id, payload.variant_ids, asset_ids)
    row = shots.update(shot_id, summary, payload.prompt.strip(),
                       payload.duration, asset_ids, variant_ids,
                       **_shot_form_fields(payload))
    if row is None:
        raise ContentOperationError(status_code=404, detail="镜头不存在")
    return ShotOut.from_row(row, variant_labels=_variant_labels([dict(row)]).get(int(row["id"]), {}))


def delete_shot(shot_id: int) -> None:
    if not shots.delete(shot_id):
        raise ContentOperationError(status_code=404, detail="镜头不存在")


def list_assets(project_id: int, kind: str | None, scope: str) -> list[AssetOut]:
    """当前项目看得见的资产：自有 ＋ 全局池。scope 只要其中一半时传 project／global。"""
    if scope not in _ASSET_SCOPES:
        raise ContentOperationError(status_code=422,
                            detail=f"scope 只认 {'／'.join(_ASSET_SCOPES)}")
    return [AssetOut.from_row(row) for row in assets.list_visible(project_id, kind, scope)]


def create_asset(project_id: int, payload: AssetIn) -> AssetOut:
    """登记资产。scope=global 时那个 project_id 只是操作上下文，行不属于任何项目。"""
    target = None if payload.scope == "global" else project_id
    name = payload.name.strip()
    if not name:
        raise ContentOperationError(status_code=422, detail="名称不能为空")
    if assets.exists_name(target, payload.kind, name):
        raise ContentOperationError(status_code=409,
                            detail="同一范围、同一类型里已经有这个名字，去合并而不是新建")
    row = assets.create(target, payload.kind, name, payload.summary.strip(),
                        payload.consistency_prompt.strip(),
                        _clean_aliases(payload.aliases), clean_tags(payload.tags))
    return AssetOut.from_row(row)


def get_asset(asset_id: int) -> AssetOut:
    return AssetOut.from_row(assets.get(asset_id))


def update_asset(asset_id: int, payload: AssetIn, current) -> AssetOut:
    """整体提交：名称／类型／简介／提示词／别名／标签／范围一次改完。

    同一个字段不留第二个入口（指令 §4.1），所以这里不接受部分更新。
    **全局资产不能收回成某个项目的资产**：它可能正被好几个项目的镜头引用，
    挪进一个项目等于让其他引用悬空——那是数据损坏，不是"改归属"。
    项目 → 全局是放行的（管理员）：id 不变，原有引用照旧有效。
    """
    # 这里要的是**完整行**，不是 `ensure_asset` 那三列：弱版本闸要读 updated_at。
    # 多花一次 SELECT 比去改那个被九条路由共用的 SELECT 稳（同 `_asset_for` 的理由）。
    if payload.scope != "global" and current["project_id"] is None:
        raise ContentOperationError(status_code=409, detail="全局资产不能收回成单个项目的资产："
                                                          "它可能正被多个项目引用，收回会让引用悬空")
    name = payload.name.strip()
    if not name:
        raise ContentOperationError(status_code=422, detail="名称不能为空")
    # 弱版本闸：界面带回"我看过的那一版"的令牌。只在这次带了才判
    # （脚本与别的调用方不带＝行为不变），冲突回 409 而不是覆盖。
    # 令牌为什么不用 `updated_at` 本身：那一列精度只到**秒**，两人同一秒内先后保存
    # 时它一模一样，闸就成了摆设。见 `models.asset_version`。
    seen = (payload.seen_version or "").strip()
    now = asset_version(current)
    if seen and seen != now:
        raise ContentOperationError(
            status_code=409,
            detail="这条资产在你打开之后又被改过（库里这一版是 " + now + "）。"
                   "这次没有保存，你写的字还在界面上；"
                   "先刷新对一遍再决定要不要覆盖——确认要顶掉就留空这一格再保存一次")
    target = None if payload.scope == "global" else current["project_id"]
    if assets.exists_name(target, payload.kind, name, exclude_id=asset_id):
        raise ContentOperationError(status_code=409,
                            detail="同一范围、同一类型里已经有这个名字，去合并而不是新建")
    row = assets.update(asset_id, kind=payload.kind, name=name,
                        summary=payload.summary.strip(),
                        consistency_prompt=payload.consistency_prompt.strip(),
                        aliases=_clean_aliases(payload.aliases),
                        tags=clean_tags(payload.tags), project_id=target)
    return AssetOut.from_row(row)


def set_asset_cover(asset_id: int, payload: AssetCoverIn) -> AssetOut:
    """设封面＝把那张图挪到数组第一位。不加 is_cover 列，顺序本身就是真相。"""
    row, moved = assets.set_cover(asset_id, payload.path)
    if row is None:
        raise ContentOperationError(status_code=404, detail="资产不存在")
    if not moved:
        raise ContentOperationError(status_code=404, detail="这张图不在该资产下")
    return AssetOut.from_row(row)


def asset_shots(asset_id: int, rows) -> list[AssetShotOut]:
    """哪些镜头引用了它，跨项目查得到——所以只回这个人看得见的那部分。

    不可见的直接过滤掉，不计数也不列（规格 01-2.2-R1：看不见就是不存在）。
    """
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


def get_character_profile(row) -> AssetProfileOut:
    asset_id = row["id"]
    if row["kind"] != "character":
        # 明确说"这类资产没有档案这一层"，而不是回一份空档案让人以为数据丢了
        raise asset_studio.ProfileInvalid(
            f"只有人物资产有文字档案；这条是{ASSET_KINDS.get(row['kind'], row['kind'])}")
    data = profile_repo.get(asset_id)
    return AssetProfileOut(**data, version=profile_version(data))


def put_character_profile(asset_id: int, payload: AssetProfileIn) -> AssetProfileOut:
    # 令牌压的是"档案时间＋档案四段"（见 `models.profile_version`，那里写了为什么
    # 不含资产时间）。判在界面保存服务而不是 `save_profile` 里：save_profile 还被别处复用，
    # 把它改成"必须带令牌才能调"会连带挡住那些没有编辑界面的调用方。
    seen = (payload.seen_version or "").strip()
    current = profile_repo.get(asset_id)
    if seen and seen != profile_version(current):
        raise ContentOperationError(
            status_code=409,
            detail="这份人物档案在你打开之后又被改过。这次没有保存，"
                   "你写的字还在界面上；先刷新把最新的一份读进来对一遍，"
                   "确认要顶掉就留空这一格再保存一次")
    out = asset_studio.save_profile(asset_id, payload)
    return AssetProfileOut(**out, version=profile_version(out))


def list_variants(asset_id: int) -> list[VariantOut]:
    return [VariantOut.from_row(row) for row in variant_repo.list_by_asset(asset_id)]


def asset_row(asset_id: int):
    row = assets.get(asset_id)
    if row is None:
        raise ContentOperationError(404, "资产不存在")
    return row


def asset_reference_rows(asset_id: int):
    return assets.referencing_shots(asset_id)
