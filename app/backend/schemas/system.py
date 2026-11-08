from pydantic import BaseModel


# ---------- 系统状态 ----------
class HealthOut(BaseModel):
    """`GET /api/health` 的响应。故意只有两个字段。

    这一条是**未登录也开放**的（唯一两个之一，另一个是 login），
    所以它只回"服务活着"这一件事；项目数是因为它对内网排障有用、
    对外不构成情报（不区分项目名、不区分状态）。

    worker / 队列 / 内存那几项还没接，**不返回假值**——
    前端据此显示"未接入"，而不是显示一个编出来的 0。
    """
    ok: bool
    project_count: int
