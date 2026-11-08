import json
from typing import Literal

from pydantic import BaseModel, Field

from .enums import AssetGenOperation, VARIANT_KINDS, VariantType
from .jobs import JobEstimateIn


# ---------- 资产增强：人物档案、命名形态、资产生成与产物导入（迁移 0033） ----------

class AssetProfileIn(BaseModel):
    """人物文字档案，整体提交。

    和 `AssetIn` 一样不接受部分更新：同一个字段不留两个入口。
    长度上限比 `summary` 宽得多是有意的——档案就是要写成长文本，
    而 `summary` 在镜头选择器里当标题读，不该被三段小作文挤掉。
    """
    biography: str = Field(default="", max_length=4000)
    personality: str = Field(default="", max_length=4000)
    growth_arc: str = Field(default="", max_length=4000)
    # 与 asset.consistency_prompt 严格分开：这一条是"让文本模型怎么写这份设定"，
    # 那一条是"让图像模型画稳定"。合成一列，改履历就会顺手改掉定妆图的依据。
    text_prompt: str = Field(default="", max_length=2000)
    # 弱版本闸，同 AssetIn 的 `seen_version`：档案表没有自己的版本列，
    # 令牌由 `profile_version` 现算（资产时间打头＋档案四段）。
    seen_version: str = Field(default="", max_length=32)




class AssetProfileOut(BaseModel):
    asset_id: int
    # exists=False 表示"还没写过档案"，不是"数据缺失"：老人物升级后照样打得开。
    # 界面据此决定是提示"写一份"还是"编辑这份"，不弹错误条。
    exists: bool = False
    biography: str = ""
    personality: str = ""
    growth_arc: str = ""
    text_prompt: str = ""
    created_at: str = ""
    updated_at: str = ""
    # 弱版本令牌，由路由用 `profile_version` 现算填进来（档案表自己没有这一列）
    version: str = ""




class VariantIn(BaseModel):
    variant_type: VariantType
    name: str = Field(min_length=1, max_length=60)
    prompt: str = Field(default="", max_length=2000)
    # 不给就排到最后（仓储在同一事务里数 MAX+1）
    sort_order: int | None = Field(default=None, ge=0, le=9999)




class VariantUpdateIn(BaseModel):
    """整体更新。**没有 asset_id 字段**：形态挂在哪个资产不是可编辑项。"""
    variant_type: VariantType
    name: str = Field(min_length=1, max_length=60)
    prompt: str = Field(default="", max_length=2000)
    sort_order: int = Field(default=0, ge=0, le=9999)




class VariantOut(BaseModel):
    id: int
    asset_id: int
    variant_type: str
    variant_type_label: str
    name: str
    prompt: str
    ref_images: list[str] = []
    image_count: int = 0
    # 只用于追溯"这批图是哪次生成来的"：状态与成本回生成中心读，这里不存副本。
    # 作业被清掉时这一列会随外键置空，形态与图都还在。
    source_job_id: int | None = None
    sort_order: int
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "VariantOut":
        images = json.loads(row["ref_images"] or "[]") if row["ref_images"] else []
        return cls(
            id=row["id"], asset_id=row["asset_id"], variant_type=row["variant_type"],
            variant_type_label=VARIANT_KINDS.get(row["variant_type"], row["variant_type"]),
            name=row["name"], prompt=row["prompt"],
            ref_images=[p for p in images if isinstance(p, str)],
            image_count=len(images), source_job_id=row["source_job_id"],
            sort_order=row["sort_order"], created_at=row["created_at"],
            updated_at=row["updated_at"])




class AssetGenIn(BaseModel):
    """提交一次资产生成。走 `services.submit_job` 那唯一一条提交路。

    目标资产可以还没有（新建时在页上先生成再保存）：这时用 draft_* 三样拼提示词，
    作业只记项目、不记资产 id。等真要保存时再由 `asset:write` 那道门二次守卫，
    "草稿没绑资产"不等于"绕过保存权限"。
    """
    operation: AssetGenOperation
    target_asset_id: int | None = None
    target_variant_id: int | None = None
    # 任务模式（asset_studio.TEXT_MODES）。图像作业不看这一列，服务端会忽略。
    # 为什么默认 full：那是 0033 那一轮唯一的行为，老调用方不必跟着改。
    text_mode: str = Field(default="full", max_length=20)
    # text_mode=expand 时要扩写哪一段（biography / personality / growth_arc /
    # summary / consistency_prompt，按操作而定，服务端对着 OUTPUT_SPEC 判）。
    target_field: str = Field(default="", max_length=40)
    # text_mode=from_source 时的原文片段。上限比 instruction 宽一档：
    # 这一格装的是"依据"，不是"要求"；再长就不该靠它写设定，那是一整篇文档，
    # 归创作中心的抽取那条路（本轮边界见 §10）。
    source_excerpt: str = Field(default="", max_length=6000)
    instruction: str = Field(default="", max_length=2000)
    image_prompt: str = Field(default="", max_length=4000)
    draft_name: str = Field(default="", max_length=60)
    draft_summary: str = Field(default="", max_length=500)
    draft_consistency_prompt: str = Field(default="", max_length=1000)
    execution_mode: Literal["local", "cloud"] | None = None
    provider_code: str = Field(default="", max_length=80)
    model_code: str = Field(default="", max_length=120)
    params: dict = {}
    estimate: JobEstimateIn | None = None
    user_confirmed_cloud: bool = False
    idempotency_key: str | None = Field(default=None, max_length=120,
                                        pattern=r"^[A-Za-z0-9._:-]+$")




class AssetGenOut(BaseModel):
    """提交后的回执：界面拿 job_id 去查状态，不在本地演进度。"""
    job_id: int
    status: str
    status_label: str
    operation: str
    operation_label: str
    text_mode: str = ""
    text_mode_label: str = ""
    # 这一次实际请求模型输出哪几段（中文段名）。界面回显它，人才知道自己点的
    # 是"补三格"还是"重写全部"——模式选了但请求了几段看不见，就等于没说。
    sections: list[str] = []
    # 幂等命中：这条回执指向的是**已经在队列里的那条作业**，没有新建。
    reused: bool = False
    capability: str
    execution_mode: str
    provider_code: str
    model_code: str
    cost_micros: int
    currency: str
    billable_status: str
    target_asset_id: int | None = None
    target_variant_id: int | None = None
    note: str = ""




class AssetGenPreviewOut(BaseModel):
    """作业结果 → 表单草稿。

    `can_fill` 由服务端判（作业成功＋产物读得到＋解析出至少一个字段），
    浏览器不自己判定——把模型输出直接写进表单再"看起来已保存"是最容易骗到人的一种失败。
    """
    job_id: int
    status: str
    status_label: str
    operation: str
    operation_label: str
    # 这一条作业当时是按哪种模式跑的（从 input_refs 读回来，不是界面现在选的那个）。
    text_mode: str = ""
    text_mode_label: str = ""
    can_fill: bool = False
    fields: dict[str, str] = {}
    raw: str = ""
    note: str = ""
    error_code: str = ""
    error_message: str = ""
    target_asset_id: int | None = None
    target_variant_id: int | None = None




class AssetImageImportIn(BaseModel):
    """把某条已成功图像作业的第 N 个产物存进资产。

    只收 `job_id + artifact_index`，**不收任何路径字段**：文件在哪由服务端从
    作业产物清单里查，客户端连文件名都决定不了，也就决定不了删除范围。
    `variant_id` 给了就存进那个形态的图清单，不给就存进资产的基础参考图。
    """
    job_id: int
    artifact_index: int = Field(default=0, ge=0, le=63)
    variant_id: int | None = None




class AssetImageImportOut(BaseModel):
    asset_id: int
    variant_id: int | None = None
    path: str
    ref_images: list[str] = []
    image_count: int = 0
    source_job_id: int | None = None
