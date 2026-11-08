from pydantic import BaseModel, Field


# ---------- 首页工作台（跨模块待办聚合） ----------
#
# 这一组模型是**先测绘形状、后建模**的：`services.workbench()` 原来直接返回 dict，
# 键随三个子视图拼出来。给聚合视图加 `response_model` 之所以危险，是因为
# FastAPI 会**静默剔除**未声明的字段（接口照样 200、测试可能仍绿，前端却少了键）。
# 所以这组类是从实测响应逐键抄下来的，不是凭印象写的；`tests/` 里有一条
# 逐键比对的回归守着"以后别偷偷加字段不加模型"。


class WorkbenchCounts(BaseModel):
    """六格待办的合计。键与 `WorkbenchProject` 里的计数一一对应。"""
    review_documents: int = 0
    review_artifacts: int = 0
    jobs_failed: int = 0
    needs_attention: int = 0
    runs_active: int = 0
    signed_ready: int = 0




class WorkbenchProject(BaseModel):
    """一行一个在产项目。归档项目不出现在这个列表里（归档＝先不挡在眼前）。"""
    id: int
    name: str
    review_documents: int = 0
    review_artifacts: int = 0
    jobs_failed: int = 0
    needs_attention: int = 0
    runs_active: int = 0
    signed_ready: int = 0




class WorkbenchThroughput(BaseModel):
    """本机出片速度。**样本为 0 时四项都是 None，不给编出来的 0**——
    界面据此显示「暂无样本」，而不是显示成"每分钟出 0 秒"。

    `caliber` 是折算口径的人话，必须一起下发：`per_minute_sec` 是按
    "镜头设定时长"折算的，不是成片实际时长，不写明就会被当成另一个数。
    """
    samples: int = 0
    video_avg_sec: float | None = None
    image_avg_sec: float | None = None
    per_minute_sec: float | None = None
    caliber: str = ""




class WorkbenchOut(BaseModel):
    counts: WorkbenchCounts
    projects: list[WorkbenchProject] = Field(default_factory=list)
    # 产物待签只看最近这么多条作业（`services.WORKBENCH_ARTIFACT_WINDOW`）。
    # 这个数必须下发：界面要说"只看了最近 200 条"，否则人以为全库都算过了。
    artifact_window: int = 0
    throughput: WorkbenchThroughput
