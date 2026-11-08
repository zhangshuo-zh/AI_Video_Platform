from pydantic import BaseModel, Field


# ---------- 项目看板 ----------
class DashboardBucket(BaseModel):
    """「按某列分组计数」的一行：状态 + 中文标签 + 数量。

    看板里 episodes / jobs / gates / signs / audio 五组都是这个形状，
    只有键名不同（status 或 decision）——所以 signs 单独一个类。
    """
    status: str
    label: str = ""
    count: int = 0




class DashboardSignBucket(BaseModel):
    """签字分档。键是 `decision` 而不是 `status`（表里就是这么存的）。"""
    decision: str
    label: str = ""
    count: int = 0




class DashboardCostRow(BaseModel):
    """成本账按币种聚合的一行。`micros` 是微单位（100 万分之一），
    不是元——这是全项目的金额口径，界面自己换算，后端不改精度。"""
    currency: str
    micros: int | None = None
    jobs: int = 0




class ProjectDashboardOut(BaseModel):
    episodes: list[DashboardBucket] = Field(default_factory=list)
    episode_total: int = 0
    shots: int = 0
    assets: int = 0
    documents: int = 0
    jobs: list[DashboardBucket] = Field(default_factory=list)
    job_total: int = 0
    costs: list[DashboardCostRow] = Field(default_factory=list)
    gates: list[DashboardBucket] = Field(default_factory=list)
    signs: list[DashboardSignBucket] = Field(default_factory=list)
    audio: list[DashboardBucket] = Field(default_factory=list)
