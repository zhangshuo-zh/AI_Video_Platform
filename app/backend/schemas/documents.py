from typing import Literal

from pydantic import BaseModel, Field

from .enums import DOC_KINDS, DocAiOperation, DocKind, DocStatus
from .jobs import JobEstimateIn


class DocumentIn(BaseModel):
    kind: DocKind
    title: str = Field(min_length=1, max_length=80)
    # 可空：全项目共用的一份（总纲、人物小传）不属于任何一集（数据字典 §6.1）
    episode_id: int | None = None
    # 首版正文可以为空——"先立个标题占位，晚上再写"是真实用法。
    # 上限不在这里按字符数卡，统一由 config.DOC_MAX_BYTES 按字节判：
    # 两处各写一个数，迟早分叉。
    content: str = ""




class DocumentUpdateIn(BaseModel):
    """改元数据：标题与挂靠的分集。**不含路径**（改名不挪文件，见仓储层注释）。"""
    title: str = Field(min_length=1, max_length=80)
    episode_id: int | None = None




class DocStatusIn(BaseModel):
    status: DocStatus




class DocContentIn(BaseModel):
    """保存正文。`expected_version` 必填——

    没有它就没法判断"你看到的和现在盘上的是不是同一份"，
    而"第二个编辑者不会静默覆盖"是 W05 的验收项。
    所以不提供"不传就当没冲突"这条便道。
    """
    content: str
    expected_version: str = Field(min_length=1, max_length=120)




class DocumentOut(BaseModel):
    id: int
    project_id: int
    episode_id: int | None
    episode_seq: int = 0
    episode_title: str = ""
    kind: str
    kind_label: str
    title: str
    # 相对路径回给前端只为"看得懂"（编辑器标题栏、备份说明），
    # 所有读写都按 id 走，前端传路径进来的接口一个都没有。
    rel_path: str
    status: str
    git_commit: str | None
    created_at: str
    updated_at: str
    # 锁的三列合起来回答界面上那一句"现在谁在改"。NULL＝没人在改（或已过期）
    lock_holder_id: int | None = None
    lock_holder_name: str = ""
    lock_expires_at: str = ""

    @classmethod
    def from_row(cls, row) -> "DocumentOut":
        keys = row.keys()
        return cls(
            id=row["id"], project_id=row["project_id"], episode_id=row["episode_id"],
            episode_seq=row["episode_seq"] if "episode_seq" in keys else 0,
            episode_title=row["episode_title"] if "episode_title" in keys else "",
            kind=row["kind"], kind_label=DOC_KINDS.get(row["kind"], row["kind"]),
            title=row["title"], rel_path=row["rel_path"], status=row["status"],
            git_commit=row["git_commit"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            lock_holder_id=row["holder_user_id"] if "holder_user_id" in keys else None,
            lock_holder_name=(row["holder_name"] or "") if "holder_name" in keys else "",
            lock_expires_at=(row["expires_at"] or "") if "expires_at" in keys else "",
        )




class DocContentOut(BaseModel):
    """一次正文读取。`version` 就是下一次保存要带回去的 expected_version。"""
    id: int
    title: str
    content: str
    version: str
    git_commit: str | None
    lock_holder_id: int | None = None
    lock_holder_name: str = ""
    lock_expires_at: str = ""




class DocAiOptionsOut(BaseModel):
    """AI 操作弹窗的计价与路由：provider/模型/计价单位/单价/预计量/健康。

    单价来自 adapter 清单的官方价，不由前端决定；数量按选中字数 + 输出上限
    估算，前端只负责显示与让用户勾确认。
    """
    operation: str
    operation_label: str
    provider: str
    model: str
    unit: str
    unit_price_micros: int
    currency: str
    source: str
    max_tokens: int
    chars: int
    estimated_quantity: float
    # 这个数量有多可信（2026-11-10，F11）。`estimate_kind` 目前只有一个取值
    # `approximate`——它存在是为了让"这是估算"成为**接口事实**而不是前端措辞，
    # 哪天有了真实分词就能改成 `exact` 而不动前端。`estimate_note` 是给人看的那句。
    estimate_kind: str = "approximate"
    estimate_note: str = ""
    health_state: str
    health_detail: str
    hint: str




class DocAiSubmitIn(BaseModel):
    """提交一次文档 AI 操作。

    `start/end` 是前端选区偏移，`selection_text` 用来做第二重校验；
    服务端仍以盘上正文为准重新切片。金额必须带确认单与勾选——文本也是云端。
    """
    operation: DocAiOperation
    instruction: str = Field(default="", max_length=2000)
    scope: Literal["document", "selection"] = "document"
    start: int = Field(default=0, ge=0, le=2_000_000)
    end: int = Field(default=0, ge=0, le=2_000_000)
    selection_text: str = Field(default="", max_length=40000)
    estimate: JobEstimateIn
    user_confirmed_cloud: bool = False




class DocAiJobOut(BaseModel):
    job_id: int
    status: str
    status_label: str
    operation: str
    operation_label: str
    apply_mode: str
    base_version: str
    selection_start: int
    selection_end: int
    provider_code: str
    model_code: str
    error_code: str | None = None
    error_message: str | None = None




class DocAiPreviewOut(BaseModel):
    """AI 作业的预览。diff 是“盘上正文 → 应用后正文”的统一 diff。"""
    job_id: int
    status: str
    status_label: str
    operation: str
    operation_label: str
    apply_mode: str
    base_version: str
    current_version: str
    can_apply: bool
    conflict: bool
    diff: str = ""
    content: str = ""
    base_chars: int = 0
    result_chars: int = 0
    applied_chars: int = 0
    note: str = ""
    error_code: str | None = None
    error_message: str | None = None




class DocSaveOut(BaseModel):
    """保存结果。`changed=false` 表示写的和盘上那份一模一样，没有产生新提交。

    `updated_at` 一起回：保存成功后界面上那行"当前提交 / 改动"要当场换掉。
    只改提交号不改时间的话，刚存完一屏还写着旧的改动时间，看着像没存上。
    """
    id: int
    version: str
    git_commit: str | None
    changed: bool
    updated_at: str




class DocAiApplyOut(DocSaveOut):
    """应用 AI 结果后回的和保存同一份字段，另带来源作业与操作。"""
    job_id: int
    operation: str
    operation_label: str




class DocVersionOut(BaseModel):
    commit: str
    author: str
    at: str
    subject: str




class DocDiffOut(BaseModel):
    commit: str
    diff: str




class DocLockOut(BaseModel):
    """拿锁/看锁的结果。`state`：acquired 抢到 / renewed 续期 / held 别人拿着。"""
    doc_id: int
    state: str
    holder_user_id: int | None = None
    holder_name: str = ""
    expires_at: str = ""
