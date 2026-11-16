"""作业服务 errors 域。"""




# ---------- P0：队列健康、人工核对与重试可见性 ----------

class JobMissing(Exception):
    """作业不存在或不属于可见项目。路由层转 404。"""





class JobStateConflict(Exception):
    """当前状态不允许这个动作（比如给一条 queued 做账单核对）。转 409。"""
