from pydantic import BaseModel, Field

from .jobs import JobEstimateIn


class ExtractSubmitIn(BaseModel):
    """提交一次抽取。正文永远从文档 id 读，前端不传路径、也不传全文。

    和 W11 的文档 AI 同一条纪律：单价来自清单，数量按“正文 + 输出上限”估算；
    必须带确认单并勾云端确认，否则不落 Job、不花钱。
    """
    project_id: int
    doc_id: int
    instruction: str = Field(default="", max_length=2000)
    estimate: JobEstimateIn
    user_confirmed_cloud: bool = False




class ExtractRunOut(BaseModel):
    id: int
    project_id: int
    doc_id: int | None
    doc_title: str
    job_id: int
    status: str
    status_label: str
    model_code: str
    prompt_version: str
    base_version: str
    current_version: str = ""
    stale: bool = False
    note: str = ""
    counts: dict = {}
    cost_micros: int | None = None
    currency: str | None = None
    billable_status: str = ""
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    deduped: bool = False




class ExtractItemOut(BaseModel):
    id: int
    run_id: int
    kind: str
    kind_label: str
    payload: dict
    confidence: float | None
    evidence: dict
    status: str
    status_label: str
    asset_id: int | None = None
    decided_by_name: str = ""
    decided_at: str | None = None




class ExtractAcceptIn(BaseModel):
    """确认入库时的覆盖值。只开放资产编辑面上真有意义的几项。"""
    name: str | None = Field(default=None, max_length=60)
    summary: str | None = Field(default=None, max_length=500)
    consistency_prompt: str | None = Field(default=None, max_length=1000)
    aliases: list[str] | None = None
    tags: list[str] | None = None




class ExtractMergeIn(BaseModel):
    asset_id: int




class ExtractRejectIn(BaseModel):
    reason: str = Field(default="", max_length=500)
