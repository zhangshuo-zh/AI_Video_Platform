import json
from typing import Literal

from pydantic import BaseModel, Field

from .enums import CAPABILITIES, VALIDATION_KINDS


# ---------- 成本账与参数模板（W08） ----------

class CostBucket(BaseModel):
    capability: str
    capability_label: str
    total_micros: int
    jobs: int




class CostCurrencyOut(BaseModel):
    currency: str
    total_micros: int
    jobs: int
    known: int
    pending: int
    by_capability: list[CostBucket] = []




class LocalCostOut(BaseModel):
    """本地作业没有金额，但有"用了多少机器"——
    它是对账的另一半：判断该不该把这批活挪到云端，看的就是这一块。"""
    jobs: int = 0
    elapsed_ms: int = 0
    cpu_ms: int = 0
    max_rss_kb: int = 0




class CostSummaryOut(BaseModel):
    # 一种币种一个桶，绝不跨币种相加；unpriced 是"云端但没记价"的条数，
    # 它必须显眼——有作业没进账比金额小更值得查
    currencies: list[CostCurrencyOut] = []
    unpriced_jobs: int = 0
    local: LocalCostOut = LocalCostOut()
    total_jobs: int = 0




class GenerationProfileIn(BaseModel):
    """参数模板。project_id 为空＝全局模板，只有管理员能写。"""
    project_id: int | None = None
    name: str = Field(min_length=1, max_length=60)
    capability: Literal["text", "image", "video", "tts", "music"]
    provider_code: str = Field(default="", max_length=80)
    model_code: str = Field(default="", max_length=120)
    params: dict = {}
    is_default: bool = False




class GenerationProfileUpdateIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    params: dict = {}
    is_default: bool = False




class GenerationProfileOut(BaseModel):
    id: int
    project_id: int | None
    project_name: str = ""
    name: str
    capability: str
    capability_label: str
    provider_code: str
    model_code: str
    params: dict
    is_default: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "GenerationProfileOut":
        try:
            params = json.loads(row["params_json"] or "{}")
        except ValueError:
            params = {}
        keys = row.keys()
        return cls(
            id=row["id"], project_id=row["project_id"],
            project_name=row["project_name"] if "project_name" in keys else "",
            name=row["name"], capability=row["capability"],
            capability_label=CAPABILITIES.get(row["capability"], row["capability"]),
            provider_code=row["provider_code"], model_code=row["model_code"],
            params=params if isinstance(params, dict) else {},
            is_default=bool(row["is_default"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )




class ValidationIn(BaseModel):
    """主动记一次验证。`real_call` 必须带 job_id——真调用的凭据就是那条作业。

    `artifact_review` 额外要求 `passed`：这一档记的是人看过产物之后的结论，
    通过与否必须明说，服务端不猜、界面不默认。

    **`backend` 收三档（local／cloud／remote），与 0045 的 DDL 一致**。
    原来这里只认两档，于是 0045 为远程自建扩的 CHECK `IN ('local','cloud','remote')`
    在 Pydantic 层就被 422 拒掉——DDL 放开的功能被表现层堵死，
    「远程自建也能留一条 real_call 凭据」那条裁定根本走不到仓储层。
    注意 `execution_mode`（作业执行分流）仍然是两档：0015/0039 的
    `job.execution_mode CHECK IN ('local','cloud')` 没放开，remote 登记
    实际跑起来仍折回本地子进程，两条轴不是一回事，别一起改。
    """
    capability: Literal["text", "image", "video", "tts", "music"]
    backend: Literal["local", "cloud", "remote"]
    provider: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=120)
    kind: Literal["env_check", "mock", "real_call", "target_machine", "artifact_review"]
    job_id: int | None = None
    passed: bool | None = None          # 只有 artifact_review 用：True=通过，False=不通过
    paid_confirmed: bool = False
    detail: str = Field(default="", max_length=500)




class ValidationOut(BaseModel):
    id: int
    capability: str
    capability_label: str
    backend: str
    provider: str
    model: str
    adapter_code: str
    manifest_version: str
    kind: str
    kind_label: str
    state: str
    detail: str
    checks: dict
    params: dict
    job_id: int | None
    provider_request_id: str | None
    http_status: int | None
    latency_ms: int | None
    artifact_path: str | None
    artifact_sha256: str | None
    cost_micros: int | None
    currency: str | None
    paid_confirmed: bool
    created_by_name: str = ""
    created_at: str

    @classmethod
    def from_row(cls, row) -> "ValidationOut":
        keys = row.keys()

        def load(text, fallback):
            try:
                value = json.loads(text or "")
            except ValueError:
                return fallback
            return value if isinstance(value, type(fallback)) else fallback

        return cls(
            id=row["id"], capability=row["capability"],
            capability_label=CAPABILITIES.get(row["capability"], row["capability"]),
            backend=row["backend"], provider=row["provider"], model=row["model"],
            adapter_code=row["adapter_code"], manifest_version=row["manifest_version"],
            kind=row["kind"], kind_label=VALIDATION_KINDS.get(row["kind"], row["kind"]),
            state=row["state"], detail=row["detail"],
            checks=load(row["checks_json"], {}), params=load(row["params_json"], {}),
            job_id=row["job_id"], provider_request_id=row["provider_request_id"],
            http_status=row["http_status"], latency_ms=row["latency_ms"],
            artifact_path=row["artifact_path"], artifact_sha256=row["artifact_sha256"],
            cost_micros=row["cost_micros"], currency=row["currency"],
            paid_confirmed=bool(row["paid_confirmed"]),
            created_by_name=(row["created_by_name"] or "") if "created_by_name" in keys else "",
            created_at=row["created_at"],
        )
