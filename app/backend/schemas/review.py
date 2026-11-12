from typing import Literal

from pydantic import BaseModel, Field


# ---------- 审核中心（W14） ----------

class ReviewSignIn(BaseModel):
    """按锚点签字。`anchor` 来自 gate 报告，同一版同一锚点改判会替换。"""
    # 文档签字必传所见正文版本；产物与 Agent 的既有调用可省略。
    base_version: str = Field(default="", max_length=100)
    anchor: str = Field(min_length=1, max_length=40)
    decision: Literal["approved", "rejected", "returned"]
    note: str = Field(default="", max_length=500)




class ReviewCheckItemIn(BaseModel):
    """人工清单里的一项结论。`code` 必须属于这一类能力的清单，服务端按 `reviewkit` 校验。"""
    code: str = Field(min_length=1, max_length=60)
    state: Literal["pass", "issue", "na"]
    note: str = Field(default="", max_length=300)




class ReviewCheckIn(BaseModel):
    """保存人工清单结论。一次最多 20 项（清单本身只有 4—5 项，多出来就是异常调用）。"""
    items: list[ReviewCheckItemIn] = Field(default_factory=list, max_length=20)




class ReviewBatchItemIn(BaseModel):
    job_id: int
    # 默认签整条作业的产物清单；一件一件签走原来的单条接口
    anchor: str = Field(default="artifact", min_length=1, max_length=40)




class ReviewBatchIn(BaseModel):
    """批量签字。

    `decision` 是**整批同一个结论**——批量通过与批量驳回是两种动作，
    混在一张表里传不同结论会让人数不清自己签了什么。
    上限 50：一次点几十条是真实需求（一镜一条），但更大批量该走产线的审核节点，
    而不是在审核中心一口气改 500 条。
    """
    items: list[ReviewBatchItemIn] = Field(min_length=1, max_length=50)
    decision: Literal["approved", "rejected", "returned"]
    note: str = Field(default="", max_length=500)




class ReviewGateOut(BaseModel):
    id: int | None = None
    project_id: int
    doc_id: int
    doc_title: str
    base_version: str
    current_version: str = ""
    stale: bool = False
    status: str
    status_label: str
    findings: list[dict] = []
    summary: dict = {}
    counts: dict = {}
    signatures: list[dict] = []
    can_sign: bool = False
    created_by_name: str = ""
    created_at: str = ""




class ReviewQueueItem(BaseModel):
    doc_id: int
    doc_title: str
    doc_kind: str
    kind_label: str
    episode_id: int | None = None
    episode_title: str = ""
    doc_status: str
    current_version: str
    gate_status: str = "none"
    gate_status_label: str = "未校验"
    gate_at: str = ""
    findings: int = 0
    errors: int = 0
    warnings: int = 0
    signed: int = 0
    approved: int = 0
    rejected: int = 0
    returned: int = 0
    stale: bool = False
