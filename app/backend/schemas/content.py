import json
from typing import Literal

from pydantic import BaseModel, Field

from .enums import ASSET_KINDS, AssetKind, EpisodeStatus
from .tokens import asset_version


class EpisodeIn(BaseModel):
    title: str = Field(min_length=1, max_length=60)




class EpisodeStatusIn(BaseModel):
    status: EpisodeStatus




class EpisodeOut(BaseModel):
    id: int
    project_id: int
    seq: int
    title: str
    status: str
    shot_count: int = 0

    @classmethod
    def from_row(cls, row) -> "EpisodeOut":
        return cls(id=row["id"], project_id=row["project_id"], seq=row["seq"],
                   title=row["title"], status=row["status"],
                   shot_count=row["shot_count"] if "shot_count" in row.keys() else 0)




class ShotIn(BaseModel):
    summary: str = Field(min_length=1, max_length=500)
    prompt: str = Field(default="", max_length=2000)
    duration: float = Field(default=5, gt=0, le=60)
    asset_ids: list[int] = []
    # 0034：这一镜用的是这个资产的**哪一个形态**（人物的哪套衣服）。
    # 服务端会保证父资产同时出现在 asset_ids 里，见 routers/content._checked_variant_ids。
    variant_ids: list[int] = []
    # 0037：镜头设定五栏（任务书 §6.1 第一阶段）。全部可留空——它们是给人看的
    # 生产语言，今天没有任何执行器读它们，所以**不进源版本指纹**：
    # 补填一个景别不该把这一镜已经签过字的产物判成过期。
    shot_size: str = Field(default="", max_length=40)
    camera_move: str = Field(default="", max_length=40)
    aspect: str = Field(default="", max_length=20)
    style: str = Field(default="", max_length=60)
    negative_prompt: str = Field(default="", max_length=2000)




class ContextIn(BaseModel):
    """当前上下文：项目 × 集。两个都可为空（清空上下文是合法操作）。"""
    project_id: int | None = None
    episode_id: int | None = None




class ShotOut(BaseModel):
    id: int
    episode_id: int
    seq: int
    summary: str
    prompt: str
    duration: float
    asset_ids: list[int]
    variant_ids: list[int] = []
    shot_size: str = ""
    camera_move: str = ""
    aspect: str = ""
    style: str = ""
    negative_prompt: str = ""
    # 形态 id → 「父资产名 · 形态名」。分镜表要把 #57 翻成人话，
    # 让浏览器再为一串 id 逐个回查形态就是 N+1；列表接口一次带出来。
    variant_labels: dict[int, str] = {}
    # 这一镜最新的成功作业是不是基于旧版分镜／资产生成的。只读标记，
    # 不参与编辑回写：PUT 只认四个可编辑字段，带上它也会被忽略。
    source_stale: bool = False

    @classmethod
    def from_row(cls, row, source_stale: bool = False,
                 variant_labels: dict[int, str] | None = None) -> "ShotOut":
        keys = row.keys()
        return cls(id=row["id"], episode_id=row["episode_id"], seq=row["seq"],
                   summary=row["summary"], prompt=row["prompt"],
                   duration=row["duration"],
                   asset_ids=json.loads(row["asset_ids"] or "[]"),
                   variant_ids=(json.loads(row["variant_ids"] or "[]")
                                if "variant_ids" in keys else []),
                   # 0037 的镜头设定五栏：老库/老夹具可能还没有这些列，
                   # 读不到就按空串给——它们是给人看的，不是判据，缺了不该报错。
                   shot_size=(row["shot_size"] if "shot_size" in keys else ""),
                   camera_move=(row["camera_move"] if "camera_move" in keys else ""),
                   aspect=(row["aspect"] if "aspect" in keys else ""),
                   style=(row["style"] if "style" in keys else ""),
                   negative_prompt=(row["negative_prompt"]
                                    if "negative_prompt" in keys else ""),
                   variant_labels=variant_labels or {},
                   source_stale=bool(source_stale))




class AssetIn(BaseModel):
    kind: AssetKind
    name: str = Field(min_length=1, max_length=60)
    summary: str = Field(default="", max_length=500)
    consistency_prompt: str = Field(default="", max_length=1000)
    aliases: list[str] = []
    tags: list[str] = []
    # 归属范围：project = 当前项目，global = 全局资产池。
    # 写全局要管理员（routers/content.py 里判），读不要——指令 §5.3「能查看不等于能修改」。
    scope: Literal["project", "global"] = "project"
    # 弱版本闸：界面把"我看过的那一版"的令牌原样带回来（见 `asset_version`）。
    # 空＝不判，脚本与别的调用方不会因为多了这道闸就突然 409。
    # 为什么在 `AssetIn` 里：新建用不上它，但 create 与 update 共用一个入参模型
    # 是既有做法（同一个字段不留两个入口），拆两个模型反而会让 PUT 少一个字段。
    # 名字不敢叫 base_version——那个名字在本文件里已经是"产物清单摘要"了
    # （artifact review 用的 sha256），两个含义共用一个字段名是接口最难懂的那种事。
    seen_version: str = Field(default="", max_length=32)




class AssetOut(BaseModel):
    id: int
    project_id: int | None
    kind: str
    kind_label: str
    name: str
    summary: str
    consistency_prompt: str
    aliases: list[str] = []
    tags: list[str] = []
    ref_images: list[str]
    # 这里没有 status：资产那一列（0004 带来的 `draft|ready`）从来没有写入方，
    # 0023 把它从表里摘掉了。"这个资产算不算齐了"在界面上由引用数与有没有定妆图现算，
    # 留一列没人写的状态只会让人以为它有含义。
    # 详情页要摆"创建／最后修改"两个时间。第一次截图里它们是 undefined——
    # 模板用了字段而 schema 没带出来，这类错只有真渲染才会露出来
    created_at: str
    updated_at: str
    # 卡片那一格"关联 N 镜"与详情页的引用清单都靠它，列表 SQL 里一次带出，
    # 不给每个资产再跑一趟反查（N+1）
    ref_shot_count: int = 0
    # 给编辑界面的弱版本令牌（见 `asset_version`）。它不是业务字段，
    # 也不需要单独存一列：内容本身就能算出它。
    version: str = ""

    @classmethod
    def from_row(cls, row) -> "AssetOut":
        keys = row.keys()
        return cls(id=row["id"], project_id=row["project_id"], kind=row["kind"],
                   kind_label=ASSET_KINDS.get(row["kind"], row["kind"]),
                   name=row["name"], summary=row["summary"],
                   consistency_prompt=row["consistency_prompt"],
                   aliases=json.loads(row["aliases"] or "[]") if "aliases" in keys else [],
                   tags=json.loads(row["tags"] or "[]") if "tags" in keys else [],
                   ref_images=json.loads(row["ref_images"] or "[]"),
                   created_at=row["created_at"], updated_at=row["updated_at"],
                   ref_shot_count=(row["ref_shot_count"] or 0)
                   if "ref_shot_count" in keys else 0,
                   version=asset_version(row))




class AssetCoverIn(BaseModel):
    """设封面：把这条路径挪到 ref_images 的第一位。

    只收已登记在该资产下的路径，收不到别的——所以它不能用来把任意文件指成图片。
    """
    path: str = Field(min_length=1, max_length=300)




class AssetShotOut(BaseModel):
    """引用这个资产的镜头。详情页与删除保护共用这一份清单。"""
    shot_id: int
    seq: int
    summary: str
    episode_id: int
    episode_seq: int
    episode_title: str
    project_id: int
    project_name: str
    # 0034：这一镜点开了这个资产的**哪个形态**（只列属于本资产的，别的资产的形态
    # 不该出现在这条资产的引用清单里）。详情页据此说清"第 3 镜要的是她那套雨夜外套"。
    variant_names: list[str] = []
