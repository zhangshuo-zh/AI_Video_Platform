from pydantic import BaseModel


# ---------- 作业诊断（error_catalog 的七字段） ----------
class JobDiagnosisBody(BaseModel):
    """`error_catalog` 的七字段诊断。**这一层不新造判据**，只是给它一个类型。

    为什么值得收成模型：`CATALOG` 里 55 条 + `UNKNOWN_TEMPLATE` 的键**完全一致**
    （已核对：只有一种键组合），也就是说这是个真固定的形状，`response_model`
    不会静默剔掉任何字段——这一点必须先确认再加，否则等于悄悄改接口。
    """
    label: str = ""
    explain: str = ""
    technical: str = ""
    impact: str = ""
    action: str = ""
    retryable: bool = False
    may_cost: bool = False
    needs_human: bool = False
    code: str = ""
    known: bool = True




class JobDiagnosisOut(BaseModel):
    """`GET /api/jobs/{id}/diagnosis` 的响应。

    `diagnosis` 里多出的 `retryable` 是**派生**的（在目录基础上再叠两道闸门：
    结果未知的云端作业不给"可直接重试"、被驳回的产物要重做不是原样重投），
    这是 `error_catalog.for_job()` 的既有口径，这里只是给它一个类型。
    """
    job_id: int
    status: str
    error_code: str = ""
    error_message: str = ""
    provider_request_id: str = ""
    billable_status: str = ""
    cloud: bool = False
    uncertain: bool = False
    diagnosis: JobDiagnosisBody
