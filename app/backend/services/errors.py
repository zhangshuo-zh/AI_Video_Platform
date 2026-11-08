MAX_TAGS = 10


MAX_TAG_LEN = 20




class Conflict(Exception):
    """正文已经不是他看的那一版了。路由层转成 409，绝不静默覆盖。"""

    def __init__(self, detail: str, current_version: str = ""):
        super().__init__(detail)
        self.current_version = current_version




class LockHeld(Exception):
    """别人正拿着编辑锁。路由层转成 423（和"版本冲突"分开：
    一个是"你手上的太旧"，一个是"现在轮不到你写"，人要做的事不一样）。"""

    def __init__(self, detail: str, holder_name: str = "", expires_at: str = ""):
        super().__init__(detail)
        self.holder_name = holder_name
        self.expires_at = expires_at




class DocTooLarge(Exception):
    """正文超了单份上限（config.DOC_MAX_BYTES）。路由回 413。"""




class GitSaveFailed(Exception):
    """Git 提交失败、文件已经退回上一版。路由回 500，把 git 的原话带回去。"""


# ---------- 文档 AI 操作（W11） ----------
class DocAiJobMissing(Exception):
    """作业不存在，或不是这份文档的 AI 作业。路由回 404，不泄露别的项目。"""


# ---------- 文本抽取候选（W13） ----------
class ExtractMissing(Exception):
    """抽取运行或候选不存在，或不属于这份文档／这个项目。路由回 404。"""




class ExtractConflict(Exception):
    """原文版本已变、候选已被别人处理，或同名资产已存在。路由回 409。"""


# ---------- 审核中心（W14） ----------
class ReviewMissing(Exception):
    """还没有校验报告，或报告不属于这份文档。路由回 404／409。"""




class ReviewStale(Exception):
    """正文版本变了，旧报告不能当作新版本已通过。路由回 409。"""


# ---------- 产物审核（0025／裁定 D1）：关键帧与镜头视频的人工验收 ----------
class ReviewNotReviewable(Exception):
    """这条作业现在没法被人审（没成功，或没有产物）。路由回 409。"""




ARTIFACT_ANCHOR = "artifact"


# 整篇签字的锚点。它不在 gate 报告里，是文档那一档的"整份通过"。
WHOLE_DOC_ANCHOR = "document"


# ---------- 音频中心（W15） ----------
class AudioInvalid(Exception):
    """音频素材输入不成立，或当前没有可用的本地音频路线。路由回 422。"""


# ---------- Provider 路由（W06） ----------
class NoRoute(Exception):
    """这一类能力现在没有可用的执行路线。

    路由层把它转成 422/409 之类都行，关键是**绝不静默换一条轨**：
    默认走本地而本地没配，就直接报"本地没配"，不能替人切到云端——
    那会在账单上留下人没勾过的费用（规格 02 §8.3「不自动路由、不静默 fallback」）。
    """


# ---------- Job 提交（W07） ----------
class JobInvalid(Exception):
    """提交参数不成立（路由回 422）。和 NoRoute 分开：NoRoute 是"现在没有路线"，
    JobInvalid 是"这次请求本身不对"，前端能改的东西不一样。"""
