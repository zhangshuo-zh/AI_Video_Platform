from models import JOB_STATUSES
from repositories.jobs import jobs as job_repo, retry_reason
from repositories.pipeline import NODE_FAILED

from .constants import JOB_NODES, NODE_STATUS_LABELS
from .graph import _deps_of




# 可重试作业的状态集合由 repositories.jobs 统一定义，上面直接 import 进来：
# 外部引用 `pipeline.RETRYABLE_JOB_STATUSES` 拿到的仍是同一个元组。


def retry_gate(node, run, job=None) -> str:
    """这一格现在能不能重试。空串＝可以；否则返回给人看的那句原话。

    **一处判定，两处用**：`retry_node` 拿它决定抛不抛 409，`node_view` 拿它决定
    界面上那枚「重试这一格」摆不摆。原来界面自己按 `status === 'failed'` 判，
    而被驳回的审核节点（`error_code == 'review_rejected'`，服务端一律拒）在页面上
    就永远挂着一颗点了必然报错的钮——按钮该不该在，是业务规则，不是前端样式。

    `job` 传进来是为了让详情页一次批量取回作业后不再逐格回库；不传就按 `node["job_id"]`
    自己读一次（单格重试那条路只有一条作业，不值得为它多写一层）。
    """
    if run["cancelled"]:
        return "这条运行已经取消；重新起一条运行"
    if node["status"] != NODE_FAILED:
        return ("只有失败的节点能重试；这一格现在是"
                f"「{NODE_STATUS_LABELS.get(node['status'], node['status'])}」")
    if node["error_code"] == "review_rejected":
        upstream = _deps_of(node)
        return ("这一格是被驳回的，重试它没有意义：先重试上游生成节点"
                f"{('（' + '、'.join(upstream) + '）') if upstream else ''}，"
                "产出新产物之后这里会重新等签字。")
    if node["node_type"] in JOB_NODES and node["job_id"]:
        if job is None:
            job = job_repo.get(node["job_id"])
        if job is None:
            return "关联的作业已经不在了，没法重试"
        reason = retry_reason(job)
        if reason:
            return (f"作业 #{job['id']} 现在是「{JOB_STATUSES.get(job['status'], job['status'])}」，"
                    f"只有失败、已取消或待核对的作业能重试；{reason}")
    return ""
