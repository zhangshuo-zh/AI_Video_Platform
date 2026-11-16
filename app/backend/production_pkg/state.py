"""production state 业务域。"""
from __future__ import annotations



# 对外五档状态（任务书 §12.3）。深层细节仍由作业状态原码给出，这里只做展示层。
PENDING = "待处理"

DOING = "进行中"

NEEDS = "需要处理"

DONE = "已完成"

FAILED = "失败"


# 对外五档的清单，接口随返回值带一份：界面拿它做筛选下拉，
# 而不是在 JS 里再抄一遍——抄一份就会有两套词（任务书 §12.3）。
PUBLIC_STATUSES = [PENDING, DOING, NEEDS, DONE, FAILED]


# 展示顺序＝推荐顺序，不是强制顺序。entry 是"点这一阶段去哪个页面"，
# 由 modules.js 的模块 id ＋二级页下标组成，前端直接用它拼 #module/sub。
STAGES: list[dict] = [
    {"key": "script", "label": "剧本创作", "entry": {"module": "writing", "sub": 0}},
    {"key": "parse", "label": "内容解析", "entry": {"module": "writing", "sub": 0}},
    {"key": "assets", "label": "资产准备", "entry": {"module": "assets", "sub": 0}},
    {"key": "storyboard", "label": "分镜制作", "entry": {"module": "writing", "sub": 2}},
    {"key": "video", "label": "视频生成", "entry": {"module": "generate", "sub": 0}},
    {"key": "audio", "label": "音频制作", "entry": {"module": "audio", "sub": 0}},
    {"key": "review", "label": "审核验收", "entry": {"module": "review", "sub": 1}},
    {"key": "compose", "label": "成片合成", "entry": {"module": "composition", "sub": 0}},
    {"key": "deliver", "label": "最终交付", "entry": {"module": "composition", "sub": 0}},
]



def _stage(key: str, **fields) -> dict:
    """把 STAGES 里的展示信息与算出来的数字合成一行阶段。"""
    meta = next(s for s in STAGES if s["key"] == key)
    out = {"key": key, "label": meta["label"], "entry": dict(meta["entry"]),
           "status": PENDING, "done": 0, "total": 0, "pending": 0, "failed": 0,
           "blocked": 0, "note": ""}
    out.update(fields)
    return out
