"""异常恢复目录（任务书 §14）。一句话职责：**把散在各处的错误码翻译成人能照着做的一张表**。

为什么要有这一层：错误码今天住在四个地方——`worker.py` 的 `JobFailure`、
`cloud_run.py` 的供应商错误、`pipeline.py` 的节点失败、`precheck.py` 的检查项——
每条都带一句 `error_message`，但那句话是**给当场排查写的**，不是给第二次遇到的人写的。
结果是同一个 `run_timeout` 有人看得懂、有人直接按重试；而云端那类"结果未知"如果按
普通失败建议重试，最坏情况是**替人再扣一次钱**。

这一层只翻译，不判命：判定规则仍在上面那四处，这里不新增任何"能不能重试"的事实，
只是把它们已经说过的话摆成同一张形状（§14 要求的七项：可懂的说明、技术详情、
影响范围、推荐做法、可否重试、是否可能额外收费、是否需要人工）。

两条纪律：

1. **认不出来的码必须老实说"未登记"**，不许套一个通用文案假装知道。`lookup()` 对未知码
   返回 `known=False`，界面照原话显示 `error_message` 并提示去看日志——
   这样新增错误码时"目录没跟上"是一件可见的事，而不是悄悄给出一个误导性建议。
2. **"可重试"是目录里最贵的一栏**，判据只从既有事实派生：
   云端且结果未知（`needs_attention`／`billable_status=pending_reconciliation`）一律
   `retryable=False`＋`needs_human=True`；被驳回的产物要的是重做与重签，不是原样重投。
"""
from __future__ import annotations

# 每种异常的条目形状。字段名与界面一一对应，改名要同步改 `frontend/js/views/generate.js`。
ENTRY_FIELDS = ("label", "explain", "technical", "impact", "action",
                "retryable", "may_cost", "needs_human")

CATALOG: dict[str, dict] = {
    # ---------- 1 模型不可用 ----------
    "adapter_unavailable": {
        "label": "模型不可用",
        "explain": "这一档的运行时或权重在这台机器上不齐，作业根本没开始算，不是模型跑崩了。",
        "technical": "`runners.build()` 抛 RunnerUnavailable，worker 落 `adapter_unavailable`；"
                     "缺什么写在错误消息里（解释器、入口脚本或权重目录）。",
        "impact": "只有这一条作业受影响；同一槽位别的作业也会一直失败，直到运行时补齐。",
        "action": "按消息补运行时（模型中心 → 引擎管理看健康），或在配置里换一档已就绪的模型。",
        "retryable": True, "may_cost": False, "needs_human": False},
    "adapter_missing": {
        "label": "模型不可用（没有登记适配器）",
        "explain": "这个供应商与能力组合在清单里没有登记，平台不会替它猜一个执行方式。",
        "technical": "`adapters.find()` 返回 None；规格住代码，清单没有就是没有。",
        "impact": "这一档永远跑不了；不是偶发。",
        "action": "在模型中心登记这条槽位，或改选清单里已有的档。",
        "retryable": False, "may_cost": False, "needs_human": True},
    "adapter_not_ready": {
        "label": "模型不可用（这一档还没准备好）",
        "explain": "登记有，但当下不满足执行条件（例如假适配器没开允许开关）。",
        "technical": "`FASTVIDEO_ALLOW_MOCK` 未置 1 时提交 mock 作业，或健康闸没过。",
        "impact": "只影响这一条；条件补齐后可直接重试。",
        "action": "看清消息里的条件，补齐后重试；假适配器只用于自测，别当生产用。",
        "retryable": True, "may_cost": False, "needs_human": False},

    # ---------- 2 引擎异常 ----------
    "internal_error": {
        "label": "引擎异常（worker 自己出错）",
        "explain": "不是模型的问题，是执行器这条代码路径抛了异常。",
        "technical": "worker 捕获未分类异常落 `internal_error`；堆栈在作业日志文件里，"
                     "接口只给异常类型与一行消息（多行堆栈不进列表，见 worker 注释）。",
        "impact": "同一台机器上的同类作业都可能中招；产物没写出来。",
        "action": "先看日志（作业详情 → 日志），把类型与消息带上排查；不要连点重试刷队列。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "adapter_start_failed": {
        "label": "引擎异常（子进程没起来）",
        "explain": "命令构造出来了，但进程没启动成功。",
        "technical": "`Popen` 抛错，worker 落 `adapter_start_failed` 并清理半成品。",
        "impact": "这一条失败；通常伴随权限、可执行位或磁盘问题。",
        "action": "确认运行时可执行、目录可写，然后重试。",
        "retryable": True, "may_cost": False, "needs_human": True},

    # ---------- 3 Worker 离线（不是作业错误码，是状态）----------
    "worker_offline": {
        "label": "Worker 离线",
        "explain": "执行器进程不在线，作业会一直排队，不会被丢掉。",
        "technical": "心跳超过 `WORKER_ONLINE_SECONDS` 未更新；队列长度与最老排队时长在状态接口里。",
        "impact": "所有等待执行的本地作业都动不了；已在跑的不受影响。",
        "action": "启动 worker：`./run.sh worker`；预检里这一项是警告不是阻塞，提交仍然可以。",
        "retryable": False, "may_cost": False, "needs_human": True},

    # ---------- 4 本地资源不足 ----------
    "resource_low": {
        "label": "本地资源不足",
        "explain": "这台机器的盘或内存不够跑这一档。",
        "technical": "预检按 `jobfiles.volume_usage()` 报盘余；内存不足通常表现为 "
                     "`adapter_failed`／`empty_output`（子进程被系统杀掉）。",
        "impact": "同一档的作业都会反复失败；换小一档通常就能过。",
        "action": "清盘或降分辨率／帧数；16GB 机器按清单默认档走（FastMetal 1.3B 需要 fast-spatial）。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "env_disk": {
        "label": "本地资源不足（盘余偏低）",
        "explain": "作业目录所在盘的剩余空间低于提醒线。",
        "technical": "预检 `env.disk`，阈值是 `DISK_MIN_FREE_GB`（该值尚未按目标机标定，见发布基线）。",
        "impact": "写产物可能中途失败；不影响已经写好的文件。",
        "action": "清理旧产物或换盘；不要靠重试赌空间够。",
        "retryable": False, "may_cost": False, "needs_human": True},

    # ---------- 5 工作流依赖缺失 ----------
    "condition_image_missing": {
        "label": "依赖缺失（这一档要一张条件图）",
        "explain": "图生视频这类档必须有输入图，而上游关键帧没给出可用的。",
        "technical": "`pipeline._submit_job_node()` 先查白名单缺项，再试着从上游图像产物补；补不出就失败。",
        "impact": "只有这一格卡住；关键帧那一格修好后重试即可。",
        "action": "先重试关键帧那一格，或把默认轨换成不需要参考图的档（T2V）。",
        "retryable": True, "may_cost": False, "needs_human": False},
    "upstream_job_missing": {
        "label": "依赖缺失（上游作业不见了）",
        "explain": "节点要读的那条上游作业没有 ID 或已被删。",
        "technical": "`pipeline` 记 `upstream_job_missing`；产线不猜上游该是哪条。",
        "impact": "这一格与它的下游都停住；其它支线不受影响。",
        "action": "重跑上游那一格；确认没有被别的运行覆盖。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "submit_rejected": {
        "label": "依赖缺失（提交被规则挡住）",
        "explain": "节点该提交的東西不满足作业契约（默认轨没配、参数不合法、云端没确认计费）。",
        "technical": "`services.submit_job()` 抛 JobInvalid，节点记 `submit_rejected`，原话存错误消息。",
        "impact": "同一格每次推进都会同样失败；这是配置问题不是偶发。",
        "action": "照错误消息补配置（模型中心 → 能力轨），再重试这一格。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "asset.missing": {
        "label": "依赖缺失（引用的资产不在库里）",
        "explain": "这一镜引用的资产或形态已经不在了。",
        "technical": "预检 `asset.missing`／`variant.missing`（阻塞档）。",
        "impact": "这一镜生成出来也缺一致性依据；成片会把它算作问题镜头。",
        "action": "去资产中心补引用或重选形态，再提交。",
        "retryable": False, "may_cost": False, "needs_human": True},

    # ---------- 6 生成任务超时 ----------
    "run_timeout": {
        "label": "生成任务超时",
        "explain": "执行超过上限被终止。多半是这一档在这台机器上太重，不是模型坏了。",
        "technical": "worker 按 `WORKER_MAX_RUNTIME_SECONDS`（本地默认 7200s）到点 SIGTERM／SIGKILL。",
        "impact": "这一条没有产物；本地跑不额外花钱。",
        "action": "先看日志判断卡在哪一步；再决定是降档重跑还是提高上限。"
                  "云端作业不适用本条——云端超时见「云端结果未知」。",
        "retryable": True, "may_cost": False, "needs_human": False},

    # ---------- 7 云端任务状态未知 ----------
    "cloud_uncertain": {
        "label": "云端结果未知",
        "explain": "请求可能已经在供应商那边建成任务、甚至已计费，只是平台没拿到确定结果。",
        "technical": "错误码在 `CLOUD_UNCERTAIN_CODES` 内且带回执 request_id；"
                     "状态落 `needs_attention`、`billable_status=pending_reconciliation`。",
        "impact": "这一笔钱可能已经花掉。**直接重试等于可能再花一次**。",
        "action": "先去供应商后台按回执 ID 核对任务与账单，再决定补跑；核对动作在作业详情里做。",
        "retryable": False, "may_cost": True, "needs_human": True},
    "provider_timeout": {"label": "云端结果未知（请求超时）", "explain": "等供应商回话超时了，任务可能仍在跑。",
        "technical": "`cloud_run.py` 落 `timeout`，worker 归到 `provider_timeout`。",
        "impact": "不确定是否计费；不要连点重试。",
        "action": "按回执 ID 去供应商核对，再决定补跑。",
        "retryable": False, "may_cost": True, "needs_human": True},
    "provider_network": {"label": "云端结果未知（网络中断）", "explain": "送出去了但没收到完整回包。",
        "technical": "`cloud_run.py` 落 `network`。",
        "impact": "不确定是否计费。", "action": "先查本机网络与代理，再核对供应商任务。",
        "retryable": False, "may_cost": True, "needs_human": True},
    "provider_unavailable": {"label": "云端服务不可用", "explain": "供应商返回 5xx。",
        "technical": "带回执 ID 时判「结果未知」（待核对）；没带回执 ID 时按普通失败处理，"
                     "因为那次请求很可能根本没建成任务。",
        "impact": "有回执 ID 就可能已计费；没有则通常没花钱。",
        "action": "看这条作业有没有回执 ID：有就去核对，没有可以直接重跑。",
        "retryable": False, "may_cost": True, "needs_human": True},
    "provider_error": {"label": "云端返回错误", "explain": "供应商报了业务错误。",
        "technical": "`cloud_run.py` 落 `provider_error`（未归类的非 2xx 或任务 FAILED）。",
        "impact": "任务已建成的话可能计费。", "action": "照错误消息处理；不确定就先核对回执。",
        "retryable": False, "may_cost": True, "needs_human": True},
    "cancelled_unconfirmed": {"label": "取消时云端状态未知", "explain": "取消信号送到时子进程已把任务发出去了。",
        "technical": "worker 在取消分支读回执文件，命中 `CLOUD_UNCERTAIN_CODES` 就落待核对。",
        "impact": "可能已计费；这条作业不会自动重投。",
        "action": "去供应商核对任务与账单，确认没在跑之后再补。",
        "retryable": False, "may_cost": True, "needs_human": True},

    # ---------- 7b 云端明确的参数／账号问题（没建成任务，可放心修）----------
    "provider_auth": {"label": "云端密钥不对", "explain": "供应商不认这把密钥。",
        "technical": "401/403 → `auth`。密钥只存变量名，界面永不回显值。",
        "impact": "这一档所有云端作业都会失败；通常没花钱。",
        "action": "检查环境变量是否指向当前这把可用密钥（模型中心 → 能力轨）。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "provider_insufficient_balance": {"label": "云端余额不足", "explain": "账户没钱或额度用尽。",
        "technical": "402 → `insufficient_balance`。",
        "impact": "所有云端作业停摆；本地档不受影响。",
        "action": "充值或改走本地档；这属于账号决策，要人来定。",
        "retryable": False, "may_cost": False, "needs_human": True},
    "provider_rate_limit": {"label": "云端限流", "explain": "请求太密，供应商暂时不让过。",
        "technical": "429 → `rate_limit`。",
        "impact": "排队稍等即可；本平台的本地队列本来就串行。",
        "action": "等一会儿重试这一条；批量场景请放慢发起节奏。",
        "retryable": True, "may_cost": False, "needs_human": False},
    "provider_invalid_request": {"label": "云端拒绝了参数", "explain": "供应商认为这次请求不合法。",
        "technical": "4xx（非鉴权）→ `invalid_request`，任务一般没建成。",
        "impact": "同样参数重试还是失败。",
        "action": "对照清单改参数（模型中心 → 模型仓库看这一档允许的取值）。",
        "retryable": True, "may_cost": False, "needs_human": False},
    "provider_missing_key": {"label": "云端密钥没配", "explain": "这条槽位要求的密钥变量在本机不存在。",
        "technical": "`cloud_run.py` 落 `missing_key`。",
        "impact": "这一档跑不了。", "action": "在运行环境里补那个变量名（不要把值写进任何文档）。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "provider_missing_base_url": {"label": "云端地址没配", "explain": "这条槽位缺服务地址。",
        "technical": "`cloud_run.py` 落 `missing_base_url`（部分供应商必须显式给）。",
        "impact": "这一档跑不了。", "action": "补对应的地址变量后重试。",
        "retryable": True, "may_cost": False, "needs_human": True},

    # ---------- 8 文件写入失败／产物无效 ----------
    "empty_output": {"label": "产物是空的", "explain": "跑完了但没写出有效文件。",
        "technical": "`runners.verify()` 魔数或非空校验没过，worker 清理半成品。",
        "impact": "本地跑不额外花钱；云端这一条**可能已计费**，按回执核对。",
        "action": "看日志尾部判断是模型没出东西还是路径问题；云端先核对再重跑。",
        "retryable": True, "may_cost": True, "needs_human": False},
    "invalid_output": {"label": "产物不是有效媒体", "explain": "文件写出来了，但 ffprobe 读不出该读的东西。",
        "technical": "`composition.ffmpeg_runner.post_check` 抛 MediaInvalid，落 `invalid_output`。",
        "impact": "这条产物不会进成片（时间线取片时同样不认）。",
        "action": "重跑这一条；反复出现就检查编码参数与源文件。",
        "retryable": True, "may_cost": False, "needs_human": False},
    "write_failed": {"label": "文件写入失败", "explain": "目录或磁盘挡住了写。",
        "technical": "路径解析越界、只读盘或空间不足；作业目录由 `jobfiles` 统一管边界。",
        "impact": "这一条没有产物。", "action": "确认作业目录可写、盘有余，再重试。",
        "retryable": True, "may_cost": False, "needs_human": True},

    # ---------- 9 资产文件丢失 ----------
    "artifact_missing": {"label": "素材文件不见了", "explain": "记录还在，但文件已经不在盘上。",
        "technical": "产物清单里 `exists=false`；审核面的版本摘要随之变化，旧签字失配。",
        "impact": "这条不能进成片；引用它的镜头会被标问题。",
        "action": "重新生成这一条并重新签字，不要手改库里的路径。",
        "retryable": True, "may_cost": False, "needs_human": True},

    # ---------- 10 审核结果过期 ----------
    "review_stale": {"label": "审核签字已过期", "explain": "签的是那一版产物，现在产物换了。",
        "technical": "签字绑产物清单摘要（`base_version`）；版本不符时审核面显示失配，成片按未通过处理。",
        "impact": "这一镜不能进成片，直到新那一版重新签字。",
        "action": "去审核中心·产物审核对新版本重签；旧签字保留不改写。",
        "retryable": False, "may_cost": False, "needs_human": True},
    "review_rejected": {"label": "审核被驳回", "explain": "这条产物被签了驳回，不是没跑好，是不合格。",
        "technical": "`review_item.decision='rejected'`；正式产物的定义里含"
                     "\"签过通过\"，被驳回的不算。",
        "impact": "这一镜不参与成片；重试同一条作业不解决问题。",
        "action": "照审核意见改提示词或参数再重做，重做后重签。",
        "retryable": False, "may_cost": False, "needs_human": True},

    # ---------- 11 源版本变化 ----------
    "source_stale": {"label": "源内容已变，旧产物过期", "explain": "分镜或引用的资产在产物生成之后改过。",
        "technical": "`services.job_source_stale()` 比 `shot_source_version()` 指纹；"
                     "与时间线取片、分镜列表同一个出处。",
        "impact": "旧产物不能进成片；已签的字不会跟着新内容走。",
        "action": "重新生成这一镜并重新签字；旧产物与旧签字都保留。",
        "retryable": True, "may_cost": False, "needs_human": False},

    # ---------- 12 成片合成失败 ----------
    "composition_precheck": {"label": "合成前检查没过", "explain": "缺片、未签字或源过期，被当场拦下。",
        "technical": "`timeline.build(require_approved=True)` 抛错，接口回 422 并逐镜点名。",
        "impact": "没有新建版本；旧成片版本一个都不动。",
        "action": "照 422 消息逐镜处理；弹窗里的合成前检查就是同一份结论。",
        "retryable": True, "may_cost": False, "needs_human": False},
    "composition_conflict": {"label": "成片状态冲突", "explain": "这一集已经有一条在合成，或版本被同时改过。",
        "technical": "`composition.service` 的冲突判定；串行是本机的默认口径。",
        "impact": "这一条没发起；正在跑的那条不受影响。",
        "action": "等前一条出结果，或去成片中心看当前版本状态。",
        "retryable": True, "may_cost": False, "needs_human": False},
    "composition_missing": {"label": "成片记录不存在", "explain": "要看的成片版本没有。",
        "technical": "`GET /api/compositions/{id}` 找不到行。",
        "impact": "只看得到已存在的版本。", "action": "回成片中心重新选一个版本。",
        "retryable": False, "may_cost": False, "needs_human": False},

    # ---------- 其它契约类（不属 §12 种，但界面上一定会遇到）----------
    "job_succeeded": {"label": "这条作业没有异常", "explain": "作业已成功，产物清单在详情里。",
        "technical": "`for_job()` 对 `status='succeeded'` 直接给这一条，不套目录。",
        "impact": "无。", "action": "要接着往下走就在审核面签字，或去成片中心合成。",
        "retryable": False, "may_cost": False, "needs_human": False},
    "job_in_progress": {"label": "还在排队或正在执行", "explain": "这条作业还没跑完，谈不上异常。",
        "technical": "`for_job()` 对 queued／starting／running／cancel_requested 给这一条；"
                     "本地默认串行（全局并发 1），排队是正常状态不是故障。",
        "impact": "无。", "action": "等它跑完；worker 不在线时先起 worker（`./run.sh worker`）。",
        "retryable": False, "may_cost": False, "needs_human": False},
    "job_failed_generic": {"label": "失败了，但没有留下错误码", "explain": "历史行或异常路径没写错误码，只能看原话与日志。",
        "technical": "`error_code` 为空而状态是 failed——今天不该出现，出现就说明有路径漏写。",
        "impact": "无法按码归类；不影响别的作业。",
        "action": "先看日志全文定位，然后把这一码补进目录（这条提示本身就是待办信号）。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "bad_params": {"label": "参数不合法", "explain": "提交的参数不在这一档的白名单或取值范围里。",
        "technical": "`adapters.validate_params()` 挡在提交或开跑前；规格住代码。",
        "impact": "没开始算，不花钱。", "action": "照消息改参数（模型仓库里能看到这一档允许什么）。",
        "retryable": True, "may_cost": False, "needs_human": False},
    "run_cancelled": {"label": "已取消", "explain": "这条作业被人取消了。",
        "technical": "worker 在循环里看到 `cancel_requested` 就停手；本地取消不产生费用。",
        "impact": "没有产物。", "action": "需要结果就重投一条。",
        "retryable": True, "may_cost": False, "needs_human": False},
    "node_not_failed": {"label": "这一格没有异常", "explain": "这一格没有失败记录，目录不替它编原因。",
        "technical": "`error_code` 为空——产线节点只在失败或跳过时写码。",
        "impact": "无。", "action": "要看进度就等它往前走；worker 离线时先起 worker。",
        "retryable": False, "may_cost": False, "needs_human": False},
    "job_needs_attention": {"label": "需要人工处理", "explain": "这条作业停在\"需要处理\"，等平台自动判定解决不了。",
        "technical": "状态 `needs_attention`；`attention_reason` 存那一句人话原因。",
        "impact": "它不进成片、不自动重试。", "action": "在作业详情里核对并处理（云端先看回执 ID）。",
        "retryable": False, "may_cost": True, "needs_human": True},

    # ---------- 产线检查节点（`pipeline._NodeFail`，任务书 §14「映射到界面各处」）----------
    # 这一组的共同点：**没花一分钱**。检查节点只读库、只碰文件，不提交作业，
    # 所以 `may_cost` 全 False；`retryable` 全 True 的前提是"上游补好了再点重试"，
    # 真正能不能点仍由 `pipeline.retry_gate()` 说（`for_node()` 会把那道闸叠上）。
    "project_missing": {"label": "项目已经不在了", "explain": "这条运行要服务的项目被删了，节点没有可操作的对象。",
        "technical": "`run.project_id` 查不到行；删项目是级联的，但运行记录留在历史里。",
        "impact": "这一整条运行都跑不下去，不是某一个节点的问题。",
        "action": "结束这条运行（取消）；要重做就在新项目里重新发起一次生产。",
        "retryable": False, "may_cost": False, "needs_human": True},
    "episode_missing": {"label": "这一集已经不在了", "explain": "运行绑定的那一集被删了，镜头与稿件都跟着清掉了。",
        "technical": "`run.episode_id` 查不到行；删集时正文走 `git rm`，历史里还找得回。",
        "impact": "这一条运行到此为止。",
        "action": "取消这条运行；如果误删，先从 Git 历史把那一集找回再重跑。",
        "retryable": False, "may_cost": False, "needs_human": True},
    "document_missing": {"label": "这一集还没有剧本或小说正文", "explain": "解析镜头的节点没有输入——正文一份都没有。",
        "technical": "`export_document`／`parse_*` 类节点在 `document` 表里找不到这一集的行。",
        "impact": "下游全部不开始：解析、分镜、生成都不会排队。",
        "action": "去创作中心把这一集的正文建起来，再回运行页重试这一格。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "no_shots": {"label": "这一集一条镜头都没有", "explain": "分镜表是空的，后面的生成与合成没有可遍历的镜头。",
        "technical": "`shots.list_by_episode()` 返回空；通常紧跟在解析节点后面失败。",
        "impact": "整条运行停在起跑线。",
        "action": "去分镜节点或创作中心把镜头建起来（手动拆镜也行），再重试。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "shots_incomplete": {"label": "有镜头缺必要内容", "explain": "镜头行存在，但缺时长或提示词这类生成必需的字段。",
        "technical": "检查节点逐镜挑问题并把前 8 条拼进消息里，节点消息就是说人话的那份。",
        "impact": "缺内容的镜头会被跳过或直接失败；成片按顺序取片时会缺这一段。",
        "action": "照消息点名的镜号补齐内容（工作台里一镜一屏），补齐后重试这一格。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "assets_missing": {"label": "引用的资产不可用", "explain": "镜头引用的人物／场景／道具不属于本项目，也不在全局池——不是「没图」，是这条资产根本用不了。",
        "technical": "`services.shot_asset_ids()` 摊出来后，归属校验没过（跨项目引用与已删除的都会落到这一码）。",
        "impact": "一致性没有依据；生成节点提交时也会被同一规则拦住。",
        "action": "去资产中心确认这些引用指向本项目（或全局池）里的资产，或重新选择。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "upstream_job_missing": {"label": "上游作业找不到", "explain": "验收节点要查的那条作业不在库里。",
        "technical": "节点输出里记的 `job_id` 查不到行——多半是作业被清理或上游节点没真提交。",
        "impact": "这一格判不了，下游继续等。",
        "action": "回上游那一格看它有没有真的提交作业；没有就先重试上游。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "job_missing": {"label": "关联的作业已经不在了", "explain": "这一格记得一条作业号，但那条作业查不到了。",
        "technical": "`job_repo.get(node.job_id)` 返回 None。",
        "impact": "产物无法验收，下游拿不到输入。",
        "action": "确认作业是否被清理；需要结果就重跑上游那一格。",
        "retryable": True, "may_cost": False, "needs_human": False},
    "no_artifacts": {"label": "作业没有登记产物", "explain": "作业跑完了，但清单里一个文件都没有。",
        "technical": "`jobfiles.artifacts(job.result_paths_json)` 为空——执行器没写回路径，或写回了空数组。",
        "impact": "这一格验收不过；下游的关键帧／条件图会跟着断。",
        "action": "打开那条作业看日志确认执行器有没有真产出；必要时重跑。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "composition_not_ready": {"label": "合成作业还没跑完", "explain": "成片验收去问合成作业，得到的却是排队中／执行中／失败这类状态。",
        "technical": "`composition.service.status_view()` 给出的状态不是「已完成」；验收节点因此拒绝通过。",
        "impact": "成片没有验收，产线不会记这一格完成。",
        "action": "去成片中心等那条合成作业跑完，再重试这一格。",
        "retryable": True, "may_cost": False, "needs_human": False},
    "output_missing": {"label": "最终成片文件不在", "explain": "成片记录说产物在某个路径，但那个文件读不到。",
        "technical": "`output_path` 为空，或 `jobfiles.resolve_artifact()` 解析后文件不存在——路径被手工挪走过清理都算。",
        "impact": "这一版成片不能交付；签字也没有对象。",
        "action": "回成片中心重新合成一版；确认作业目录有没有被手工清理过。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "output_changed": {"label": "成片文件与记录不一致", "explain": "文件还在，但大小对不上当初记下的那个数。",
        "technical": "验收节点比对 `validators.inspect()` 的字节数与成片记录里的数字。",
        "impact": "这一版不能算验收通过——文件被替换过就不能保证是原来那条。",
        "action": "确认是不是手工改过或压缩过；不是就重新合成一版。",
        "retryable": False, "may_cost": False, "needs_human": True},
    "probe_failed": {"label": "成片探测读不出媒体信息", "explain": "ffprobe 在发起前预取结果时失败，成片还没法验收。",
        "technical": "`advance_run()` 在写事务外用 `validators.inspect()` 预取媒体信息；探测异常被带进事务由这一格明确失败。",
        "impact": "这一版成片不能验收；后续签字与导出按依赖等待。",
        "action": "看成片文件是不是还在、格式是不是被 ffprobe 支持；必要时重新合成一版。",
        "retryable": True, "may_cost": False, "needs_human": True},
    "unknown_node_type": {"label": "模板里有平台不认识的节点", "explain": "模板定义写了个当前执行器没有实现的节点类型。",
        "technical": "`_definition_of()` 的 `type` 不在 `JOB_NODES`／检查节点分支里；平台不猜它该干什么。",
        "impact": "这一格直接失败，下游按依赖等待或跳过。",
        "action": "回产线中心核对模板定义（自定义模板最容易写错这一栏）；换回内置模板可立刻恢复。",
        "retryable": False, "may_cost": False, "needs_human": True},
}

# 云端错误码里"结果未知"那一组的名字。与 `worker.CLOUD_UNCERTAIN_CODES` 同源，
# 但这里是**给人看的分组**，不是判定集合——判定仍只住 worker 一处。
UNCERTAIN_CODES = {
    "provider_timeout", "provider_network", "provider_empty_response",
    "provider_empty_output", "provider_error", "provider_unavailable",
    "cancelled_unconfirmed", "adapter_failed", "empty_output",
    "run_timeout", "worker_stopped",
}

UNKNOWN_TEMPLATE = {
    "label": "未登记的错误码",
    "explain": "平台没有这一码的现成解释。下面显示的是执行器当场的原话。",
    "technical": "错误码没进 `error_catalog.py` 的目录——新增码时目录没跟上，"
                 "这条提示本身就是个待办信号。",
    "impact": "影响范围不明，按原话判断最稳。",
    "action": "打开这条作业的日志看全文；确认原因后把这一码补进目录。",
    "retryable": False, "may_cost": False, "needs_human": True,
}


def lookup(code: str) -> dict:
    """按错误码取条目；认不出来就老实说认不出来。"""
    entry = CATALOG.get((code or "").strip())
    if entry is None:
        return {**UNKNOWN_TEMPLATE, "code": code or "", "known": False}
    return {**entry, "code": code, "known": True}


def for_job(job) -> dict:
    """一条作业的完整诊断视图。

    派生而不新造事实：`retryable` 在目录基础上再叠两道既有闸门——
    结果未知的云端作业永远不给"可直接重试"，被驳回的产物要的是重做不是原样重投。
    """
    status = job["status"]
    raw_code = job["error_code"] or ""
    if status == "succeeded":
        # 成功的作业没有"异常"可诊断。硬套一张目录会显示成"未登记的错误码"，
        # 那是把界面变成假警报——所以这一档单独给一句实话。
        code = "job_succeeded"
    elif status in ("queued", "starting", "running", "cancel_requested"):
        # 还没跑完的作业同样没有异常：过去这里会拿空的 error_code 去查目录，
        # 于是排队中的作业显示"未登记的错误码"，界面变成狼来了。
        code = "job_in_progress"
    elif status == "needs_attention":
        code = raw_code or "job_needs_attention"
    else:
        code = raw_code or "job_failed_generic"
    view = lookup(code)
    cloud = job["execution_mode"] == "cloud"
    uncertain = cloud and (job["status"] == "needs_attention"
                           or job["billable_status"] == "pending_reconciliation")
    if uncertain:
        view = {**lookup("cloud_uncertain" if code in UNCERTAIN_CODES or not view["known"]
                         else code),
                "code": code or "cloud_uncertain"}
    return {
        "job_id": int(job["id"]),
        "status": job["status"],
        "error_code": code,
        "error_message": job["error_message"] or "",
        "provider_request_id": job["provider_request_id"] or "",
        "billable_status": job["billable_status"] or "",
        "cloud": cloud,
        "uncertain": bool(uncertain),
        "diagnosis": {**view,
                      "retryable": bool(view["retryable"]) and not uncertain
                                   and code != "review_rejected"},
    }


def for_node(node, *, retry_hint: str = "") -> dict:
    """一个产线节点的诊断视图（任务书 §14 要求同一套七字段出现在界面每一处）。

    为什么节点也要走这张表：节点原来的失败文案是 `_NodeFail` 当场写的那一句，
    只说"是什么"不说"接下来怎么办"；而人在产线上撞到的异常，与在生成中心撞到的
    是同一家族的麻烦，就该拿到同一个形状的判断。这里仍然**不新造判据**：
    这一格现在到底能不能点重试，只有 `pipeline.retry_gate()` 说了算，
    所以由调用方（`pipeline._node_view`）把那道口试原样传进来叠上——
    目录只说"这类失败通常可重试"，闸门说"这条现在别点"，界面按后者摆。

    判"有没有异常"用的是 `error_code` 而不是状态字：节点只有失败或跳过才写码，
    拿状态字来判就得把 `pipeline` 的状态表再抄一份到这儿（两处抄表迟早对不上）。
    """
    code = (node["error_code"] or "").strip()
    view = lookup(code) if code else lookup("node_not_failed")
    return {"node_id": int(node["id"]),
            "node_type": node["node_type"],
            "label": node["label"],
            "error_code": code,
            "error_message": node["error_message"] or "",
            "diagnosis": {**view,
                          "retryable": bool(view["retryable"]) and not retry_hint,
                          "retry_hint": retry_hint}}
