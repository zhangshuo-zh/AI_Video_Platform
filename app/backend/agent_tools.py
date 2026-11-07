"""智能体的**工具白名单**：Agent 能对平台做的事，全在这张表里（说明书 §3.4）。

一句话职责边界：**这里只有"能做"，没有"怎么做"**。每个工具都是既有服务层的一层
薄壳——判据、事务、权限、成本账一个都不在这里重写（与 `precheck.py` 第 3 条纪律
「不新造判据」同一条道理）。所以读这份文件之前先读它调的那几个模块，
那里才是事实的家：整集盘点住 `pipeline.preflight()`，提交作业住
`services.submit_job()`，登记资产住 `repositories/content.py`。

三条硬规矩，每条都对着说明书里的一句话：

1. **禁止任意 SQL／Shell／Python**（§2）。落法不是"过滤危险字符"，而是三条
   结构性约束：
   * 工具码必须是 `TOOLS` 的键——出现别的字符串就直接拒，不 `getattr`、不 `eval`、
     不把字符串当函数名用；
   * 参数只能填这张表声明过的那几个键，**多一个键就拒**。
     "顺手塞个 `sql`／`cmd`／`path` 参数"这条路从结构上不存在，而不是被正则挡住；
   * 每个工具绑的是一个**直接引用的函数对象**，不是名字。
     表里没写出来的动作，Agent 连"它存在"都不知道。
2. **风险档不是装饰**（§3.4「读写风险等级」）。四档的差别是"要不要人点头"：
   `read` 当场做完、不排队不花钱；`write` 必须先审批；`paid` 除审批外还要在
   审批页看见费用估算与数据发送范围；`formal` 批完之后还要拿真实业务行验收。
   档位写在表里，`agent_step.risk` 只是它的落库副本——改档位只有这一处。
3. **权限不在这里发明**。工具跑在 worker 进程里，没有"当前请求人"，
   所以每一步都以**发起人**的身份去过同一条判定：
   `users.permissions_for(发起人)` 查权限点，`access.ensure(发起人, 项目, 级别)`
   查项目归属（与 `routers/jobs.py` 那两行一模一样）。
   Agent 拿到的权限永远等于发起它的那个人，绝不因为"现在是平台在跑"就多给一格。
"""
from __future__ import annotations

import hashlib
import json

import access
import adapters
import pipeline
import workspace
from fastapi import HTTPException
from repositories.content import assets, episodes as episodes_repo
from repositories.documents import documents
from repositories.users import users

# 作业行状态里"已经跑完且成功"的那一个。不抄 JOB_STATUSES 的键名列表，
# 只在需要判成的这一处比对——多了就把 Job 的状态机抄进智能体第二遍
_JOB_SUCCEEDED = "succeeded"


class ToolUnknown(ValueError):
    """工具码不在白名单里。**默认拒绝**：宁可拒一个拼错的码，
    也不要"看起来跑了、其实什么都没做"——那是最难查的一种假成功。"""


class ToolArgsInvalid(ValueError):
    """参数不合法：缺必填、类型不对、长度超限，或者传了没声明的键。"""


class ToolExecutionError(RuntimeError):
    """工具自己失败了（业务拒绝、权限不够、对象不存在）。

    原话给人看，`code` 给统计用——与 `error_catalog` 那种"分类而不糊原文"同一口径。
    """

    def __init__(self, message: str, code: str = "tool_failed"):
        super().__init__(message)
        self.code = code


# ---------- 声明 ----------
# 每个工具七件事：label（人看的名）、risk（档位）、perms（发起人必须有这些权限点）、
# args（参数声明）、handler（函数对象，在文件末尾装配）、data_out（往外送什么）、
# verify（执行后拿什么证明"真的做到了"，说明书 §5 步 7）。
#
# `data_out` 是给审批页念的那句"数据发送范围"（说明书 §3.2）。它不是自动判断，
# 而是**登记时就得填**的一句话：一个工具会送出什么，写代码的人此刻就知道；
# 留到审批时再让程序去猜，就得到一个会说谎的隐私提示。
TOOLS: dict[str, dict] = {
    "episode_preflight": {
        "label": "盘点这一集缺什么、什么会拦",
        "risk": "read",
        "perms": ("project:read",),
        "args": {"episode_id": {"type": "int", "required": True}},
        "data_out": "只读库里已有的数字与句子；不外发任何内容",
        "verify": "读到的就是产线发起弹窗将要显示的同一份清单（同一个函数、同一份判据）",
    },
    "document_outline": {
        "label": "读一篇文档的结构读数",
        "risk": "read",
        "perms": ("doc:read",),
        "args": {"document_id": {"type": "int", "required": True}},
        # 这条不是形式（说明书 §2「云端发送内容需最小化」）：读数只有标题、
        # 段数、字数、版本号——**正文一个字都不进结果**。
        # 所以这一步的产物被云端模型读走时，泄露面是零，而不是"大概不多"。
        "data_out": "只回标题／段落数／字数／版本，不回正文",
        "verify": "版本令牌就是 document.version 那一列，正文一改读数就跟着变",
    },
    "text_job_submit": {
        "label": "提交一条文本生成作业",
        # 按最严的一档登记：今天文本只有云端一条轨（deepseek），要花钱、要出本机。
        # 等本地文本腿（ollama）实测通过，档位仍留 paid 也不亏——
        # 参数里带的是正文素材，"不花钱"不等于"可以不给人看"。
        "risk": "paid",
        "perms": ("job:submit",),
        "args": {
            "capability": {"type": "enum", "choices": ("text",), "required": True},
            "prompt": {"type": "str", "required": True, "max": 8000},
            "episode_id": {"type": "int", "required": False},
            "idempotency_key": {"type": "str", "required": False, "max": 120},
        },
        "data_out": "把 prompt 那一格的内容**逐字**交给所选供应商，不裁剪不摘要",
        # 验收拿的是 Job 的真实终态与产物文件，不是 Agent 自己说"我做完了"
        "verify": "作业终态为成功、且产物清单里有一个能读的文本文件才算成",
    },
    "asset_register": {
        "label": "登记一条正式资产",
        "risk": "formal",       # 写进业务事实，下游（分镜引用、成片一致性）跟着变
        "perms": ("asset:write",),
        "args": {
            "kind": {"type": "enum",
                     "choices": ("character", "scene", "prop"), "required": True},
            "name": {"type": "str", "required": True, "max": 80},
            "summary": {"type": "str", "required": False, "max": 2000},
            "consistency_prompt": {"type": "str", "required": False, "max": 2000},
        },
        "data_out": "只写库，不外发任何内容",
        "verify": "回读那一行：名称与类型必须与批准时逐字一致",
    },
}

# 有限枚举：写在这里是为了界面与测试都有一处可问，而不是各自硬写四个字符串。
RISK_LABEL = {"read": "只读", "write": "写入", "paid": "花钱的写入",
              "formal": "改正式业务事实"}


# ---------- 参数校验 ----------
def _coerce(name: str, spec: dict, value):
    kind = spec["type"]
    if kind == "int":
        # bool 是 int 的子类，但不许当整数用：`episode_id=True` 是 bug，不是 1
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ToolArgsInvalid(f"{name} 要一个整数，现在给的是 {type(value).__name__}")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ToolArgsInvalid(f"{name} 读不出整数：{value!r}") from exc
    if kind == "bool":
        return bool(value)
    if kind == "str":
        text = "" if value is None else str(value)
        limit = int(spec.get("max", 2000))
        if len(text) > limit:
            raise ToolArgsInvalid(f"{name} 超过 {limit} 字（现在 {len(text)} 字）")
        return text.strip()
    if kind == "enum":
        choices = tuple(spec.get("choices") or ())
        text = "" if value is None else str(value)
        if text not in choices:
            raise ToolArgsInvalid(
                f"{name} 只能是 {'、'.join(choices)}，不是「{text}」")
        return text
    # 声明写错是开发期 bug，不该在运行时被当成"用户的参数不对"报给人
    raise ToolArgsInvalid(f"内部：{name} 声明了平台不认识的参数类型 {kind}")


def validate(tool_code: str, raw_args) -> dict:
    """规范化参数：**未知的键直接拒**。

    这一句是整个模块最硬的一条防线。如果参数是"透传给下游"的，
    Agent（或伪造请求的人）就能往里塞 `sql`／`cmd`／`path`，
    而这些键迟早被某个下游当真。所以这里不是过滤，是白名单。
    """
    tool = TOOLS.get(tool_code)
    if tool is None:
        raise ToolUnknown(
            f"工具「{tool_code}」不在白名单里，平台不执行"
            "（智能体只能用登记过的工具，不能自己发明动作）")
    declared = tool["args"]
    args = raw_args if isinstance(raw_args, dict) else {}
    unknown = [k for k in args if k not in declared]
    if unknown:
        raise ToolArgsInvalid(
            f"工具「{tool_code}」不接受这些参数：{'、'.join(sorted(unknown))}。"
            "多出来的键一律按拒处理——这就是「禁止任意 SQL／Shell／Python」"
            "在参数层的落法")
    out: dict = {}
    for name, spec in declared.items():
        if name not in args:
            if spec.get("required"):
                raise ToolArgsInvalid(f"工具「{tool_code}」缺必填参数 {name}")
            continue
        out[name] = _coerce(name, spec, args[name])
    return out


def args_hash(tool_code: str, args: dict) -> str:
    """参数指纹。审批绑它、执行前再核一次它（说明书 §3.4「审批与参数绑定」）。

    规范化用 `sort_keys` ＋ 紧凑分隔符：同一组参数在任何顺序、任何一次生成里
    必须得到同一个串。否则"人批的参数"与"执行的参数"会因为字典顺序不同而哈希不同，
    审批就永远对不上——那比没有审批更糟，因为每一步都会卡在"审批失配"。
    """
    payload = json.dumps({"tool": tool_code, "args": args},
                         sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def risk_of(tool_code: str) -> str:
    return TOOLS[_check_code(tool_code)]["risk"]


def labels() -> dict:
    """给界面的一张小表：码 → （人看的名字，风险档，风险档的人话）。

    一次性给全，是为了让"工具叫什么"只有一个出处。视图里再抄一份
    `{"episode_preflight": "资产盘点"}` 那种字典，就是下一轮要修的第二个答案。
    """
    return {code: {"label": tool["label"], "risk": tool["risk"],
                   "risk_label": RISK_LABEL[tool["risk"]],
                   "data_out": tool["data_out"], "verify": tool["verify"]}
            for code, tool in TOOLS.items()}


def _check_code(tool_code: str) -> str:
    if tool_code not in TOOLS:
        raise ToolUnknown(f"工具「{tool_code}」不在白名单里")
    return tool_code


def cost_note(capability: str, provider: str, model: str, backend: str) -> str:
    """审批页那句费用／计价的话——**从适配器清单现读，不在这里写数字**。

    为什么不调 `services._cost_of()`：那是要落账时的金额换算，它信的是"提交人
    勾了确认"这件事；审批页此刻既没提交也没确认单，能说的只有"这一档按什么计价"。
    清单里那张 `prices` 表就是这句话的唯一出处；把金额抄进代码就成了第二份价格，
    价格会漂，漂了审批页还在念旧价，比不念更糟。
    """
    adapter = adapters.find(capability, backend, provider)
    if adapter is None:
        return "这一档不在适配器清单里：参数不猜、价格也不猜，提交侧会被拒"
    spec = next((m for m in adapter.models if m.code == model), None)
    if spec is None:
        return f"清单里有 {adapter.label}，但没有 {model} 这一档：先核模型名"
    if not spec.prices:
        return (f"{adapter.label} / {model}：清单里没有计价条目"
                "（本地轨不计费，或这一档的价格尚未核验）")
    # 单位中文名从 `models.UNITS` 那一份取（前端 enums.js 的 UNITS 就是它的副本）。
    # 在这里再写一遍 `{"million_token": "百万 token"}` 就是界面文案第三次漂移的起点。
    from models import UNITS
    parts = [f"{p.label}：{p.amount_micros / 1_000_000:g} "
             f"{p.currency}／{UNITS.get(p.unit, p.unit)}" for p in spec.prices]
    return "；".join(parts)


# ---------- 身份与权限 ----------
def _actor(ctx: dict):
    """发起人那一行。查不到就 fail-closed：账号被删了，
    他发起的运行不该以"平台"的名义继续写东西。"""
    actor_id = ctx.get("actor_id")
    if not actor_id:
        raise ToolExecutionError("这次运行没有可认的发起人，平台不代其执行写操作",
                                 code="no_actor")
    user = users.get_by_id(int(actor_id))
    if user is None:
        raise ToolExecutionError(f"发起人账号 #{actor_id} 已不存在；"
                                 "请取消这条运行，或由还在的人重新发起", code="actor_gone")
    return user


def _require(user, codes, project_id: int, need: str) -> None:
    """权限点＋项目归属两层，与 `deps.require_permission` ＋ 路由里那句
    `access.ensure()` 同一条组合。少任何一层都可能变成"Agent 比人权限大"。"""
    granted = users.permissions_for(user)
    missing = [code for code in codes if code not in granted]
    if missing:
        raise ToolExecutionError(
            f"发起人缺少权限：{'、'.join(missing)}。智能体不越过人——"
            "它只能做发起人本来就能做的事", code="forbidden")
    try:
        access.ensure(user, int(project_id), need)
    except HTTPException as exc:
        # 把 HTTP 语义翻成步骤语义：这一步是在 worker 里跑的，
        # 403／404 对人来说都只是"这件事没做成，原因是这句"
        raise ToolExecutionError(str(exc.detail), code="not_visible") from exc


# ---------- 执行 ----------
def _episode_preflight(args: dict, ctx: dict) -> dict:
    """整集盘点：直接调 `pipeline.preflight()`。

    产线发起弹窗（`pipeline.create_run`）、`precheck.episode_precheck`、这里，
    三个入口读的是同一份结论。说明书 §9 阶段 4「从资产缺失盘点开始」
    就落在这一个函数调用上——Agent 看到的是人看到的同一张清单，
    不是为它另写的一套口径。
    """
    row = episodes_repo.get(args["episode_id"])
    if row is None:
        raise ToolExecutionError(f"没有这一集：#{args['episode_id']}", code="not_found")
    _require(_actor(ctx), TOOLS["episode_preflight"]["perms"],
             row["project_id"], "read")
    out = pipeline.preflight(int(row["project_id"]), int(args["episode_id"]))
    return {
        "episode_id": int(args["episode_id"]),
        "seq": row["seq"],
        "blocking": list(out.get("blocking") or []),
        "warnings": list(out.get("warnings") or []),
        "numbers": dict(out.get("info") or {}),
        # 跳转给界面用：Agent 说"缺资产"，人能一键跳到那一页，而不是自己去列表里找
        "locate": {"module": "assets", "sub": 0},
        # 这句话是给人看的收尾说明，也是这条工具结果的"验收口径"自述
        "note": "与产线发起弹窗同一份清单（同一个函数），平台没有第二套盘点",
    }


def _document_outline(args: dict, ctx: dict) -> dict:
    row = documents.get(args["document_id"])
    if row is None:
        raise ToolExecutionError(f"没有这篇文档：#{args['document_id']}",
                                 code="not_found")
    _require(_actor(ctx), TOOLS["document_outline"]["perms"],
             row["project_id"], "read")
    # 正文只数字数与段数，一个字都不回传——见上面那句 data_out。
    # 读不到文件不报错：文档元数据在库里、正文在盘上，这两件事本来就可能不同步
    # （工作区被挪走、盘还没挂上）。回 `has_body=false` 让人自己去创作中心看那一页。
    body = ""
    read_error = ""
    try:
        body = workspace.read(row["rel_path"])
    except Exception as exc:                      # PathError / FileNotFoundError / OSError
        read_error = f"正文读不到：{type(exc).__name__}"
    paragraphs = [p for p in body.split("\n\n") if p.strip()] if body else []
    return {
        "document_id": int(row["id"]),
        "title": row["title"],
        "doc_type": row["doc_type"],
        "version": row["version"],
        "chars": len(body),
        "paragraphs": len(paragraphs),
        "has_body": bool(body.strip()),
        "read_error": read_error,
        "locate": {"module": "writing", "sub": 0},
    }


def _text_job_submit(args: dict, ctx: dict) -> dict:
    """提交一条文本作业：走唯一那条提交路，不在这里 resolve、也不在这里建作业行。

    幂等键缺省时由运行与步序**确定性地**算出来（`ag-<run_id>-s<seq>`），
    与产线节点 `pl-<run_id>-<node_key>-a<n>` 同一个思路：worker 重启、
    同一请求重发都拿同一个键，于是"提交两次扣两次费"这条路是结构上走不通，
    而不是靠调用方记得传。
    """
    from models import JobSubmitIn
    import services

    project_id = int(ctx["project_id"])
    user = _actor(ctx)
    _require(user, TOOLS["text_job_submit"]["perms"], project_id, "read")

    episode_id = args.get("episode_id")
    if episode_id:
        ep = episodes_repo.get(int(episode_id))
        if ep is None or int(ep["project_id"]) != project_id:
            raise ToolExecutionError(
                f"第 {episode_id} 集不属于这个项目：跨项目的引用一律不接",
                code="bad_link")
        episode_id = int(episode_id)
    key = (args.get("idempotency_key")
           or f"ag-{ctx['run_id']}-s{ctx['seq']}")

    payload = JobSubmitIn(
        project_id=project_id, episode_id=episode_id, shot_id=None,
        capability=args["capability"],
        # 不填执行方式＝走这一类的默认轨（`provider_config.is_default`）。
        # 这里刻意不给 Agent 传 execution_mode 的参数：
        # "换成云端那一轨"必须是一次看得见的选择，不是模型顺手加的一个字段。
        prompt=args["prompt"], params={}, input_refs=[],
        idempotency_key=key,
        # 这个字段不是"Agent 替人做决定"，而是**转述人已经做过的那个决定**：
        # `_execute` 只在步骤状态是「已批准」时才把 `cloud_confirmed` 置真，
        # 而那意味着审核中心里有一条绑着本步骤参数指纹的批准记录。
        #
        # 上一版这里恒传 False，理由是"授权不由 Agent 代答"——那句话对，但结论错了：
        # 它让**默认轨指向云端时这类步骤注定失败**，而这不是保护，是让这条路
        # 永远走不通（《系统设计说明书》§16 第 25 条如实登记过）。
        # 现在授权由签字提供，Agent 仍然不代答——它只是把签字这件事带进来。
        user_confirmed_cloud=bool(ctx.get("cloud_confirmed")))
    try:
        job = services.submit_job(payload, user["id"])
    except services.JobInvalid as exc:
        raise ToolExecutionError(str(exc), code="submit_rejected") from exc
    mode = job["execution_mode"]
    return {
        "job_id": int(job["id"]),
        "status": job["status"],
        "execution_mode": mode,
        "provider_code": job["provider_code"],
        "model_code": job["model_code"],
        "idempotency_key": key,
        # 走到这里说明这一枪发得出去。云端那一轨能发出去，靠的是审核中心那条
        # 绑着参数指纹的批准记录（见上面 `user_confirmed_cloud` 那段）——
        # 不是 Agent 自己勾了一个框。
        "cloud_confirmed": bool(ctx.get("cloud_confirmed")),
        "note": ("作业已排进生成中心那一个队列；状态、日志、成本与产物都在那里看，"
                 "智能体表里不复制这些事实"),
    }


def _asset_register(args: dict, ctx: dict) -> dict:
    """登记一条正式资产。名称查重交给仓储层之外先做一遍，
    为的是给人一句"已经有这一条了"，而不是让库里长出两条同名资产。
    """
    project_id = int(ctx["project_id"])
    _require(_actor(ctx), TOOLS["asset_register"]["perms"], project_id, "write")
    name = args["name"]
    # 查重按 (项目范围, 类型, 名称) 三条一起比——这就是 `asset` 表那条唯一索引的口径
    # （0012 把它拆成"项目内"与"全局"两条，用 `exists_name` 而不是自己拼 SQL，
    # 免得又长出第二份查重规则）。先查一次是为了给人一句"已经有这一条了"，
    # 而不是让库里撞索引撞出一条 500。
    if assets.exists_name(project_id, args["kind"], name):
        raise ToolExecutionError(
            f"项目里已经有一条同类型的「{name}」；"
            "要改它请去资产中心，智能体不重复登记同名条目", code="duplicate_name")
    import sqlite3
    try:
        row = assets.create(project_id, args["kind"], name,
                            args.get("summary", ""),
                            args.get("consistency_prompt", ""))
    except sqlite3.IntegrityError as exc:
        # 两个人（或两条运行）同时登记同一条：先查再写挡不住并发，
        # 索引才是最终答案。这里把它翻成人话，不翻成"内部错误"
        raise ToolExecutionError(
            f"这一条资产刚好在这一步之前被登记了（{exc}）；"
            "请到资产中心确认已有那一条，不必再来一次",
            code="duplicate_name") from exc
    # 验收（说明书 §5 步 7「验证真实状态和产物」）：回读那一行，
    # 逐字对名称与类型。对不上就明说失败——不拿"INSERT 没报错"当成功
    check = assets.get_many([int(row["id"])]) or {}
    got = check.get(int(row["id"]))
    if got is None or got["name"] != name or got["kind"] != args["kind"]:
        raise ToolExecutionError(
            f"资产行写完但回读对不上（#{row['id']}）：这一步按失败处理，"
            "请人工核对资产中心", code="verify_failed")
    return {
        "asset_id": int(row["id"]),
        "kind": row["kind"],
        "name": row["name"],
        "verified": True,
        "locate": {"module": "assets", "sub": 0},
        "note": "已登记为正式资产；定妆图与一致性提示词仍在资产中心那一页补",
    }


# handler 与声明分开装配，是为了让上面那张表能一口气读完——
# 读代码的人最想知道的是"允许哪些事"，不是每个函数体。
TOOLS["episode_preflight"]["handler"] = _episode_preflight
TOOLS["document_outline"]["handler"] = _document_outline
TOOLS["text_job_submit"]["handler"] = _text_job_submit
TOOLS["asset_register"]["handler"] = _asset_register


def execute(tool_code: str, args: dict, ctx: dict) -> dict:
    """执行一个白名单工具。参数先按声明规范化再过 handler。

    `ctx` 必须有 project_id／run_id／seq／actor_id 四样（步骤归属与幂等键都要用）。
    这里**不再判一次风险**：风险是登记时就定死的档位，由调用方（`agents.py`）
    决定"这一步需不需要先有人批"。工具自己不知道审批状态，
    所以也就不可能被绕过——绕过审批的那条路要在 `agents.py` 里找，不在这里。
    """
    code = _check_code(tool_code)
    tool = TOOLS[code]
    clean = validate(code, args)
    handler = tool["handler"]
    if handler is None:
        # 表里声明了却没有绑实现：这是开发期漏装配，必须响，不能退化成"什么都没做"
        raise ToolExecutionError(f"内部：工具「{code}」没有绑定实现",
                                 code="not_wired")
    return handler(clean, ctx)
