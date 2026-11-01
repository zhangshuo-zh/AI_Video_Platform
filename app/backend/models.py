"""请求/响应用的数据结构。

ASSET_KINDS 是"资产类型 → 中文名"的唯一出处，前端不再自己判断。
"""
import json
from typing import Literal

from pydantic import BaseModel, Field

# 五类能力：固定，前端按这个顺序渲染，不靠库里查出来的顺序
CAPABILITIES: dict[str, str] = {
    "text": "文本",
    "image": "图像",
    "video": "视频",
    "tts": "配音",
    "music": "音乐",
}

BACKENDS: dict[str, str] = {"local": "本地", "cloud": "云端"}


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=500)
    tags: list[str] = []
    # 建项目时一并指定协作人：弹窗里一次填完，免去"建完再去加人"两步
    member_ids: list[int] = []


# 库里这两列都没有 CHECK 约束（加约束要重建表，而且取值到三期还会扩），
# 所以白名单写在这里：Literal 会让 FastAPI 直接挡掉表外的值，回 422。
ProjectStatus = Literal["active", "archived"]
EpisodeStatus = Literal["draft", "active", "done"]


class ProjectStatusIn(BaseModel):
    status: ProjectStatus


class ProjectBatchIn(BaseModel):
    """批量归档／恢复／加标签。ids 上限 200：一屏能勾出来的量级，
    再多就是拿批量接口当同步用，那不是这一页的用途。"""
    ids: list[int] = Field(min_length=1, max_length=200)
    action: Literal["archive", "restore", "tag"]
    tags: list[str] = []


class ProjectBatchDeleteIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)
    # 必须原样填 BATCH_DELETE_CONFIRM 那句话。单个删除靠输入项目名防手滑，
    # 一次删十几个没法逐个输入名字，就换成一句明确的批量确认词
    confirm: str = ""


class ProjectSkip(BaseModel):
    """批量操作里被跳过的那一项。不报错、但要报原因。"""
    id: int
    name: str = ""
    reason: str = ""


class ProjectBatchOut(BaseModel):
    done: list[int] = []
    skipped: list[ProjectSkip] = []


class ProjectTagsIn(BaseModel):
    tags: list[str] = []


class ProjectUpdateIn(BaseModel):
    """「修改」弹窗一次提交整张表单。

    不做部分更新（缺哪个字段就保留原值）：弹窗里三项都摆出来了，
    三样一起提交才是所见即所得；拆成"只传改了的"反而会出现
    界面上显示 A、库里存 B 的错位。
    """
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=500)
    tags: list[str] = []


class ProjectOut(BaseModel):
    id: int
    name: str
    status: str
    description: str = ""
    owner_id: int
    owner_name: str = ""
    created_at: str
    updated_at: str
    # 空串＝从没打开过。列表按它排序（见 repositories.projects._ORDER），
    # 归档状态走 status，两者都不影响"内容最后改动时间"
    last_opened_at: str = ""
    tags: list[str] = []
    # 只读摘要。集/镜/资产的东家是创作中心，项目中心只显示、不修改
    episode_count: int = 0
    episode_done: int = 0
    shot_count: int = 0
    asset_count: int = 0

    @classmethod
    def from_row(cls, row) -> "ProjectOut":
        keys = row.keys()
        return cls(id=row["id"], name=row["name"], status=row["status"],
                   description=row["description"],
                   owner_id=row["owner_id"],
                   owner_name=row["owner_name"] if "owner_name" in keys else "",
                   created_at=row["created_at"], updated_at=row["updated_at"],
                   last_opened_at=row["last_opened_at"] or "",
                   tags=json.loads(row["tags"] or "[]"),
                   episode_count=row["episode_count"] if "episode_count" in keys else 0,
                   episode_done=row["episode_done"] if "episode_done" in keys else 0,
                   shot_count=row["shot_count"] if "shot_count" in keys else 0,
                   asset_count=row["asset_count"] if "asset_count" in keys else 0)


class ProjectSummary(BaseModel):
    """删除前要给人看清楚会连带删掉什么。"""
    name: str
    episodes: int
    shots: int
    assets: int
    image_files: int


class ProviderOut(BaseModel):
    id: int
    capability: str
    capability_label: str
    backend: str
    backend_label: str
    provider: str
    model: str
    enabled: bool
    is_default: bool
    has_key: bool          # 只看环境变量里有没有，永远不回传密钥本身

    @classmethod
    def from_row(cls, row, has_key: bool) -> "ProviderOut":
        return cls(
            id=row["id"],
            capability=row["capability"],
            capability_label=CAPABILITIES.get(row["capability"], row["capability"]),
            backend=row["backend"],
            backend_label=BACKENDS.get(row["backend"], row["backend"]),
            provider=row["provider"],
            model=row["model"],
            enabled=bool(row["enabled"]),
            is_default=bool(row["is_default"]),
            has_key=has_key,
        )


class ProviderUpdate(BaseModel):
    id: int
    provider: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=120)
    enabled: bool = False
    is_default: bool = False


# ---------- 内容三件套 ----------

ASSET_KINDS: dict[str, str] = {"character": "角色", "scene": "场景", "prop": "物品"}
AssetKind = Literal["character", "scene", "prop"]


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


class ShotOut(BaseModel):
    id: int
    episode_id: int
    seq: int
    summary: str
    prompt: str
    duration: float
    status: str
    asset_ids: list[int]

    @classmethod
    def from_row(cls, row) -> "ShotOut":
        return cls(id=row["id"], episode_id=row["episode_id"], seq=row["seq"],
                   summary=row["summary"], prompt=row["prompt"],
                   duration=row["duration"], status=row["status"],
                   asset_ids=json.loads(row["asset_ids"] or "[]"))


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
    status: str
    # 详情页要摆"创建／最后修改"两个时间。第一次截图里它们是 undefined——
    # 模板用了字段而 schema 没带出来，这类错只有真渲染才会露出来
    created_at: str
    updated_at: str
    # 卡片那一格"关联 N 镜"与详情页的引用清单都靠它，列表 SQL 里一次带出，
    # 不给每个资产再跑一趟反查（N+1）
    ref_shot_count: int = 0

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
                   status=row["status"],
                   created_at=row["created_at"], updated_at=row["updated_at"],
                   ref_shot_count=(row["ref_shot_count"] or 0)
                   if "ref_shot_count" in keys else 0)


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


# ---------- 用户与角色（系统设置页） ----------

class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    is_superuser: bool
    is_active: bool
    must_change_password: bool
    created_at: str | None = None
    last_login_at: str | None = None
    roles: list[str] = []

    @classmethod
    def from_row(cls, row) -> "UserOut":
        return cls(id=row["id"], username=row["username"],
                   display_name=row["display_name"] or row["username"],
                   is_superuser=bool(row["is_superuser"]),
                   is_active=bool(row["is_active"]),
                   must_change_password=bool(row["must_change_password"]),
                   created_at=row["created_at"], last_login_at=row["last_login_at"],
                   roles=json.loads(row["roles"] or "[]"))


class UserCreateIn(BaseModel):
    # 用户名进 URL 与日志，限成 ASCII，省掉一半的转义问题
    username: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(default="", max_length=40)
    password: str = Field(min_length=6, max_length=64)
    roles: list[str] = []


class UserUpdateIn(BaseModel):
    """只更新传了的那几项。没传 = 不动，和项目那个"整张表单"的取舍不同：
    这里停用和改名是两个不同的动作，混在一次提交里更容易误触。
    角色不在这个包里——改角色是另一个权限点（user:assign），走单独的端点。
    """
    display_name: str | None = Field(default=None, max_length=40)
    is_active: bool | None = None


class PasswordResetIn(BaseModel):
    new_password: str = Field(min_length=6, max_length=64)


class UserRolesIn(BaseModel):
    roles: list[str] = []


class RoleOut(BaseModel):
    id: int
    code: str
    name: str
    description: str = ""
    permissions: list[str] = []


class PermissionOut(BaseModel):
    code: str
    name: str
    group_name: str


class RolePermIn(BaseModel):
    permissions: list[str] = []


# ---------- 项目协作人 ----------

class ProjectMemberOut(BaseModel):
    user_id: int
    username: str
    display_name: str
    created_at: str


class ProjectMemberIn(BaseModel):
    user_id: int


class ProjectMembersOut(BaseModel):
    """一页给齐：已受邀的人和可供挑选的人。

    候选也在这里出，是因为挑选名单要按"当前调用者能看见谁"过滤——
    让前端去调 /api/users 就等于绕开了这层过滤，而且只有 user:read
    的人才调得动，项目所有人（可能只有一个制作角色）反而调不动。
    """
    owner_id: int
    members: list[ProjectMemberOut] = []
    candidates: list[ProjectMemberOut] = []


class UserDeleteIn(BaseModel):
    """删用户必须表态：名下项目是转移还是跟着删。

    两个都不给就拒——静默把项目带进坟墓是这类操作最危险的方式。
    """
    transfer_to: int | None = None
    delete_projects: bool = False
