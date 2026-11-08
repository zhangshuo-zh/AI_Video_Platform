"""首页工作台：跨模块待办聚合。

为什么单开一个端点而不是让首页挨个模块问：待办是"跨模块"的概念——
审核中心知道哪几份没签，生成中心知道哪几条失败，产线知道哪几条还在跑。
让浏览器分别去问，要么拼出 N 个项目 × 3 个模块的请求，要么前端自己加总，
两样都会长出一份"前端版口径"。这里把加总放在服务端，口径与各模块同一处定义。

只读：不写任何状态，也不改任何作业。
"""
from fastapi import APIRouter, Depends

import services
from deps import require_permission
from models import WorkbenchOut

router = APIRouter(prefix="/api", tags=["workbench"])


@router.get("/workbench", response_model=WorkbenchOut)
def get_workbench(user=Depends(require_permission("project:read"))) -> WorkbenchOut:
    """看得见的在产项目的待办计数 + 分项目明细。

    单独一个 `project:read` 就够：它数的是"我本来就看得见的东西"。
    看不见的项目在这里既不出现，也不计入任何数字（服务端按可见性先筛）。
    """
    return WorkbenchOut(**services.workbench(user))
