
# 一轮 tick 最多派发多少个节点。正常一集也就几十个，这一位是防呆：
# 模板写错成"每个节点都依赖自己之外的所有节点"时，别把队列一次灌爆。
MAX_DISPATCH_PER_TICK = 60



# 一次推进最多走多少"层"。层数上限只是防呆：模板最长也就十几层，
# 真跑到这个数说明依赖图有问题（比如每层都在放新节点），停下来比空转好。
MAX_LAYERS = 40



NODE_TYPE_LABELS = {
    "project_input": "读取项目",
    "episode_input": "读取集",
    "document_input": "读取剧本",
    "shot_validate": "分镜检查",
    "asset_validate": "资产检查",
    "human_gate": "人工确认",
    "image_generate": "关键帧生成",
    "image_review": "关键帧验收",
    "video_generate": "镜头视频生成",
    "artifact_validate": "产物检查",
    "shot_review": "镜头审核",
    "audio_prepare": "音频准备检查",
    "export_prepare": "成片导出准备",
    "export_compose": "成片合成",
    "export_validate": "成片验收",
    "export_review": "成片审核",
}



# 哪些节点要提交 Job（其余是内部检查或人工审核）
JOB_NODES = {"image_generate": "image", "video_generate": "video",
             # 成片合成也走 Job，但它由 composition 服务建单，不经过
             # `submit_job` 的模型白名单；`_execute_node` 里有专门分支。
             "export_compose": "video"}


# 哪些节点要等人签字
REVIEW_NODES = {"human_gate", "image_review", "shot_review", "export_review"}


# 哪些节点按镜头展开（一镜一个节点，互不影响：镜头 2 失败不该重跑镜头 1 和 3）
FANOUT_TYPES = {"image_generate", "image_review", "video_generate",
                "artifact_validate", "shot_review"}


# 内部检查节点：调度器当场做完，不排队
INTERNAL_NODES = {"project_input", "episode_input", "document_input",
                  "shot_validate", "asset_validate", "artifact_validate",
                  "audio_prepare", "export_prepare", "export_validate"}



NODE_STATUS_LABELS = {
    "blocked": "等待前置", "pending": "待执行", "ready": "可执行",
    "running": "执行中", "waiting_review": "等待审核", "attention": "待人工核对",
    "completed": "已完成", "failed": "失败", "skipped": "已跳过",
    "cancelled": "已取消",
}





# 产线事件的中文名。**与智能体事件同一类决定**（见 `agents.EVENT_LABEL` 上面那段）：
# 原来这份字典只住在 `views/pipeline.js`，与库里的 CHECK 各写一份，中间没有守卫——
# 0026 的 CHECK 收 15 档，界面少说一档就会变成事件码。
# 现在收在服务端，随 `GET /pipeline/runs/{id}` 下发 `event_labels`；
# `tests/test_pipeline.py::test_产线事件的每一档都有中文名` 比 0026 的 CHECK 与这本字典。
#
# 注意它**不是**智能体那 23 档：两张表各有一套事件，名字也不同（产线叫「节点完成」，
# 智能体叫「步骤成功」）。共用一本字典会逼着两边叫同一个名字，那是另一种错。
EVENT_LABELS = {
    "run_created": "发起运行", "run_started": "开始生产",
    "run_paused": "暂停", "run_resumed": "恢复",
    "run_cancelled": "取消", "run_completed": "全部完成",
    "run_failed": "运行失败",
    "preflight_warning": "生产前检查提醒",
    "job_submitted": "提交作业",
    "node_ready": "节点就绪", "node_started": "开始执行",
    "node_completed": "节点完成", "node_failed": "节点失败",
    "node_skipped": "节点跳过", "node_retried": "重试这一格",
    "node_waiting_review": "等待签字", "node_attention": "待人工核对",
}


RUN_STATUS_LABELS = {
    "running": "生产中", "waiting_review": "待审核", "attention": "待人工核对",
    "paused": "已暂停", "cancelled": "已取消", "completed": "已完成",
    "completed_with_errors": "完成（有跳过或重试）", "failed": "失败",
}




class PipelineInvalid(Exception):
    """模板或请求本身不成立（路由回 422）。"""




class PipelineMissing(Exception):
    """运行或节点不存在、或者不属于可见项目（路由回 404）。"""




class PipelineConflict(Exception):
    """当前状态不允许这个动作（路由回 409）。"""
