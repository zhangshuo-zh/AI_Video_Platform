import json
from typing import Literal

from pydantic import BaseModel, Field

from .enums import BACKENDS, CAPABILITIES, ProjectStatus
from .tokens import provider_slot_version


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=500)
    tags: list[str] = []
    # 建项目时一并指定协作人：弹窗里一次填完，免去"建完再去加人"两步
    member_ids: list[int] = []




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




class ProjectNote(BaseModel):
    """批量操作里"做成了，但有话要说"的那一项。

    和 ProjectSkip 分开：skipped 是没做（权限不够），note 是做了但没全做到——
    比如项目标签已经 10 个，新标签一个也加不进去。混在一起报，
    人就会以为"已加标签 3 个项目"里那 3 个都加上了。
    """
    id: int
    name: str = ""
    note: str = ""




class ProjectBatchOut(BaseModel):
    done: list[int] = []
    skipped: list[ProjectSkip] = []
    notes: list[ProjectNote] = []




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




class ProjectCostOut(BaseModel):
    """项目中心那一格只读累计。pending 是"其中待核对"的条数，不是金额。"""
    currency: str
    total_micros: int
    jobs: int = 0
    pending: int = 0




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
    # 这两个计数只服务于"批量删除前清点影响范围"：列表顺手带出来，
    # 那里就不必对每个项目各打一次 /summary
    document_count: int = 0
    image_count: int = 0
    # 成本账的东家是生成中心，项目中心只读这一份累计（按币种分开，不加总）。
    # 不在这里放明细：明细只属于生成中心那一页（设计稿 §3.2）。
    costs: list["ProjectCostOut"] = []

    @classmethod
    def from_row(cls, row) -> "ProjectOut":
        keys = row.keys()

        def costs():
            try:
                raw = json.loads(row["costs"] or "[]")
            except (ValueError, KeyError, TypeError):
                return []
            return [ProjectCostOut(**item) for item in raw if isinstance(item, dict)]

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
                   asset_count=row["asset_count"] if "asset_count" in keys else 0,
                   document_count=row["document_count"] if "document_count" in keys else 0,
                   image_count=row["image_count"] if "image_count" in keys else 0,
                   costs=costs())




class ProjectSummary(BaseModel):
    """删除前要给人看清楚会连带删掉什么。"""
    name: str
    episodes: int
    shots: int
    assets: int
    image_files: int
    # 文档元数据会被删、正文会从工作区摘掉，但那一稿在 Git 历史里还在——
    # 数量还是要报，人要知道的是"这个项目带走了什么"
    documents: int = 0




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
    # key_env 是"实际去哪个变量找"（用户填的名字 > 清单建议 > FV_KEY_<PROVIDER>）；
    # key_env_custom 是库里那一列的原值，界面要能把两者分开显示。
    key_env: str
    key_env_custom: str
    notes: str
    # 健康是每次现查的：state 四档 + 一句人话 + 命中的 adapter（没登记就是 null）
    health: dict
    # P0：把"凭什么认为能用"单独摆出来。health 只回答环境齐不齐，
    # validation 回答"最近一次真调用／假适配器／目标机基准是什么时候、结果如何"。
    validation: dict = {}
    # 弱版本令牌，由路由用 `provider_slot_version` 现算填进来（表里没有这一列）。
    # 界面存着它，保存时原样带回；对不上就是有人在中间改过这一行。
    version: str = ""

    @classmethod
    def from_row(cls, row, has_key: bool, key_env: str, health: dict,
                 validation: dict | None = None) -> "ProviderOut":
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
            key_env=key_env,
            key_env_custom=row["key_env"],
            notes=row["notes"],
            health=health,
            validation=validation or {},
            # 令牌现算：这一行"打开时是什么样"由内容决定，不另存一列。
            version=provider_slot_version(row),
        )




class ProviderUpdate(BaseModel):
    id: int
    provider: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=120)
    enabled: bool = False
    is_default: bool = False
    key_env: str = Field(default="", max_length=64)
    notes: str = Field(default="", max_length=500)
    # 弱版本闸：界面把"我打开时看到的那一版"的令牌原样带回来（见 `provider_slot_version`）。
    # 空＝不判，脚本与命令行调用不会因为多了这道闸就突然 409——与 `AssetIn.seen_version`
    # 同一条边界：闸只拦"界面里两个人撞车"，不改变所有调用方的契约。
    seen_version: str = Field(default="", max_length=32)




class ProviderOptionOut(BaseModel):
    """给提交表单看的可用槽位：只有 provider/模型与参数白名单，没有密钥名与备注。

    生成中心的"本地／云端"选择靠它——普通编辑者没有 system:config，
    不该为了选一条云端视频去读整张配置页。
    """
    capability: str
    capability_label: str
    backend: str
    backend_label: str
    provider: str
    model: str
    adapter_label: str
    health_state: str
    health_detail: str
    # 提交表单也要能看出"这一槽位从没真跑过"。不带 detail，避免把内部路径泄露给普通编辑者。
    validation_state: str = "never"
    validation_at: str = ""
    models: list[dict] = []




class CurrentProviderOut(BaseModel):
    """给其他模块只读的"当前默认"。

    不带 notes、不带密钥名——别的页面只需要知道"这一类现在走谁、状态如何"。
    """

    capability: str
    capability_label: str
    backend: str
    backend_label: str
    provider: str
    model: str
    adapter: str
    health_state: str
    health_detail: str
    validation_state: str = "never"
    validation_at: str = ""
