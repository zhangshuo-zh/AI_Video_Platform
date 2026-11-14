from typing import Literal

import job_state


# ---------- 数据标准核心代码集（CodeSet Type Aliases） ----------
# 对应《数据字典.md》§4.0.1 核心数据元码值标准目录，应用层 Pydantic 强类型防守基线
Capability = Literal["text", "image", "video", "tts", "music"]
DispatchMode = Literal["local", "cloud"]
HostingSource = Literal["local", "cloud", "remote"]
PolicyPreference = Literal["local", "cloud", "remote", "any"]
ConnectionType = Literal["local_http", "grpc", "cli"]
SupportedConnectionType = Literal["local_http"]
ValidationKind = Literal["env_check", "mock", "real_call", "target_machine", "artifact_review"]
ValidationState = Literal["ok", "missing", "unknown", "unconfigured", "unregistered", "failed"]
ProbeState = Literal["ok", "missing", "unknown"]


# 五类能力：固定，前端按这个顺序渲染，不靠库里查出来的顺序
CAPABILITIES: dict[str, str] = {
    "text": "文本",
    "image": "图像",
    "video": "视频",
    "tts": "配音",
    "music": "音乐",
}



BACKENDS: dict[str, str] = {"local": "本地", "cloud": "云端"}



# 排序轴比展示轴宽一档：`BACKENDS` 是"作业执行分流"（job.execution_mode，0015 的
# CHECK 只有 local／cloud），而 `provider_config`／`model_registration` 这两张表
# 的 `backend` 列收 remote（0035／0045）。`backend_order()` 拿它生成 CASE，
# 少写 remote 的后果是远程自建那几行在列表里落到 `ELSE`、沉到最后并被当成脏值。
BACKEND_ORDER: tuple[str, ...] = ("local", "cloud", "remote")



# 计费单位白名单。不做自由文本：单位是给人对账的，
# "sec"／"秒"／"seconds" 混着存，汇总时就成了三个口径。
UNITS: dict[str, str] = {
    "second": "秒",
    "minute": "分钟",
    "request": "次",
    "token": "Token",
    # 文本供应商按"百万 token"报价；单 token 单价会写成 0.000002 这种小数，
    # 既难核对也容易在前端被四舍五入吃掉。单独留一档，预填就是官方口径。
    "million_token": "百万 Token",
    "image": "张",
    "character": "字符",
}



BILLABLE_LABELS: dict[str, str] = {
    "known": "已确认",
    "pending_reconciliation": "待核对",
    "not_billable": "不计费",
}



# 一条"能用"的结论凭什么成立。五类互不升级——外部评审点名的就是这一刀：
# mock 跑通不等于真实供应商可用，环境齐备不等于真产出过文件，
# 真实调用成功也只证明"链路产出过"，文件好不好要人看过（产物验收）才算数。
VALIDATION_KINDS: dict[str, str] = {
    "env_check": "环境检查",
    "mock": "假适配器",
    "real_call": "真实调用",
    "target_machine": "目标机基准",
    "artifact_review": "产物验收",
}



# 错误码 → 人话分类。不追求穷尽：没登记的原样显示，至少不会把未知伪装成已知。
# 这一位的用途很窄：让非开发用户知道"下一步该找配置、找参数、找机器，还是去核对账单"。
ERROR_LABELS: dict[str, str] = {
    "bad_params": "参数不合法",
    "adapter_not_ready": "适配器尚未接线",
    "adapter_missing": "缺少运行时",
    "adapter_unavailable": "运行时条件不足",
    "adapter_start_failed": "模型进程起不来",
    "adapter_failed": "模型进程异常退出",
    "empty_output": "返回成功但没有有效产物",
    "run_timeout": "执行超过时间上限",
    "worker_lost": "执行进程中断",
    "worker_stopped": "worker 被停止",
    "cancelled": "已取消",
    "cancelled_unconfirmed": "取消时供应商任务可能已建立",
    "internal_error": "平台内部错误",
    "mock_failed": "假适配器模拟失败",
    "provider_missing_key": "缺少密钥",
    "provider_missing_base_url": "缺少供应商地址",
    "provider_unsupported_provider": "供应商未登记",
    "provider_auth": "供应商拒绝鉴权（密钥不对或过期）",
    "provider_insufficient_balance": "供应商余额不足",
    "provider_rate_limit": "供应商限流",
    "provider_invalid_request": "供应商拒绝请求参数",
    "provider_unavailable": "供应商暂时不可用",
    "provider_error": "供应商返回错误",
    "provider_empty_response": "供应商返回空响应",
    "provider_timeout": "供应商轮询超时",
    "provider_empty_output": "供应商任务成功但没有产物",
    "provider_network": "与供应商的网络中断",
    "provider_cloud_run_error": "云端调用进程异常",
}




# 库里这两列都没有 CHECK 约束（加约束要重建表，而且取值到三期还会扩），
# 所以白名单写在这里：Literal 会让 FastAPI 直接挡掉表外的值，回 422。
ProjectStatus = Literal["active", "archived"]


EpisodeStatus = Literal["draft", "active", "done"]


# ---------- 生成作业（W07） ----------

JOB_STATUSES: dict[str, str] = {
    "queued": "排队中",
    "starting": "启动中",
    "running": "运行中",
    "succeeded": "成功",
    "failed": "失败",
    "cancel_requested": "取消中",
    "cancelled": "已取消",
    "needs_attention": "待核对",
}



JOB_RUNNING = job_state.IN_FLIGHT


JOB_FINISHED = job_state.FINISHED


# ---------- 内容三件套 ----------

ASSET_KINDS: dict[str, str] = {"character": "角色", "scene": "场景", "prop": "道具"}


AssetKind = Literal["character", "scene", "prop"]




VARIANT_KINDS: dict[str, str] = {
    "outfit": "服饰", "scene_view": "视角", "prop_form": "形态", "other": "备选方案",
}


VariantType = Literal["outfit", "scene_view", "prop_form", "other"]




# 四种操作。前三种是 text 能力、产出的是"能填进表单的草稿"；最后一种是 image 能力。
ASSET_GEN_OPERATIONS: dict[str, str] = {
    "character_profile": "人物文字设定",
    "scene_setting": "场景说明与提示词",
    "prop_setting": "道具说明与提示词",
    "asset_image": "资产图像候选",
}


AssetGenOperation = Literal["character_profile", "scene_setting", "prop_setting", "asset_image"]


# ---------- 文档（正文在文件系统，库里只有元数据） ----------

DOC_KINDS: dict[str, str] = {"novel": "小说", "script": "剧本", "storyboard": "分镜稿"}


# storyboard 在枚举里但界面上不给建：分镜的真相是 shot 表（一镜一行、要挂生成作业），
# 再存一份 Markdown 就是第二个事实来源。留这一档是为了将来真要出"人读的分镜稿"时不改表。
DocKind = Literal["novel", "script", "storyboard"]


DocStatus = Literal["draft", "polishing", "finalized"]




DocAiOperation = Literal["draft", "expand", "continue", "polish", "scriptify", "prompt"]


# ---------- 文本抽取候选（W13） ----------

ExtractItemKind = Literal["character", "scene", "prop", "shot"]


# ---------- 音频中心（W15） ----------

AudioKind = Literal["dialogue", "narration", "music", "sfx"]
