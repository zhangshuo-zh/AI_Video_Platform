"""模型配置：五类能力 × 本地/云端 两轨。

PUT 是整批覆盖而不是逐行改：配置页一次保存十行，
整批提交能在一个事务里校验"每类能力恰好一个默认"，不会出现中间态。

W06 起这一页还负责两件规格里点名的事：密钥变量名（只存名字）与备注；
允许参数和健康状态不落库，前者来自 adapter 清单、后者是打开页面时现查的。

P0 又加了一件事：**把"环境齐"和"真跑过"分开显示**。健康状态每次现查，
但"这一槽位最近一次真实调用是什么时候、结果如何"是历史事实，落在
`model_validation` 表里——它不能跟着配置一起被整批覆盖掉。
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response

import adapters
import engines
import validations
from config import ALLOW_MOCK
from deps import require_permission, require_user
from models import (BACKENDS, CAPABILITIES, CurrentProviderOut, ProviderOptionOut,
                    ProviderOut, ProviderUpdate, ValidationIn, ValidationOut,
                    provider_slot_version)
from repositories.config_log import record as config_log_record
from repositories.model_center import model_center as registration_repo
from repositories.providers import has_key, providers
from repositories.validation import validations as validation_repo

router = APIRouter(prefix="/api", tags=["settings"])

# 变量名只允许环境变量的合法写法；它不是值，但拼错会让"已配置"永远亮不起来，
# 所以宁可在保存时挡掉，也不让人对着一个不存在的名字猜。
KEY_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROVIDER_RE = re.compile(r"^[A-Za-z0-9._-]*$")


def _slot_key(row) -> tuple[str, str, str]:
    return (row["capability"], row["backend"], row["provider"])


def _slot_model_key(row) -> tuple[str, str, str, str]:
    """0043：验证矩阵按档分区后的键。`row` 是 provider_config 的一行，
    它的 `model` 就是这一槽位当前选的那一档——记录里不是这个 model 的
    根本不会进这一格：能力轨的"生产可用"只认当前档自己的记录。"""
    return (row["capability"], row["backend"], row["provider"], row["model"])


def _row_out(row, matrix: dict | None = None) -> ProviderOut:
    # 超时用列表档（0.8 秒）：这一条是"打开一页"的路径，服务型引擎那一格只是顺带，
    # 不该因为某台服务半死就让人等满 5 秒。要问到底请点引擎实例那一行的「探测」
    # 或模型中心的「检查引擎连接」——那两条是人明确要求的现查。
    health = adapters.health(row, probe_service=False,
                             probe_timeout=engines.LIST_TIMEOUT_SECONDS)
    env = health["key_env"] if row["backend"] == "cloud" else ""
    # matrix 给了就从里取（一整页一次批量读）；这一格不在矩阵里＝这个槽位从没验证过，
    # 要传**空字典**而不是 None——传 None 会让 summary_for 自己再打五趟查询去确认"没有"。
    latest = matrix.get(_slot_model_key(row), {}) if matrix is not None else None
    summary = validations.summary_for(row, latest)
    # 指纹只给 cloud 行；本地轨没有密钥这个概念，给空 dict（不是 None——
    # None 会让 schema 那一侧再判一次"是不是没算"）。
    fp = adapters.key_fingerprint(env) if row["backend"] == "cloud" else {}
    return ProviderOut.from_row(row, has_key(row, env), env, health, summary,
                                secret_fingerprint=fp)


@router.get("/providers", response_model=list[ProviderOut])
def list_providers(_=Depends(require_permission("system:config"))) -> list[ProviderOut]:
    # 十个槽位 × 五档 = 原来五十趟回库；现在一条窗口函数读完（模型与密钥页每次进都走这）。
    # 传 slots 是**定向**：provider_config 只有十行且键固定，把十个槽位给它，
    # 窗口函数就只在十组里挑最新，不必先把整张 model_validation 算一遍再丢弃。
    # 0043 起键带 model：窗口按 (槽位, model, kind) 分区，"生产可用"只认
    # 这一槽位当前那一档自己的记录，与模型仓库同一把尺子。
    rows = providers.all()   # 一次查询两处用；原来矩阵和返回各打一遍同一张表
    slots = {(r["capability"], r["backend"], r["provider"], r["model"])
             for r in rows}
    matrix = validation_repo.latest_matrix(slots, by_model=True)
    return [_row_out(row, matrix) for row in rows]


@router.get("/providers/manifest")
def provider_manifest(_=Depends(require_permission("system:config"))) -> dict:
    """能力清单：每个 adapter 支持哪些档、每档允许哪些参数、出处是哪。

    界面只按这份清单渲染；清单里没有的 provider 一律不显示参数，
    免得"模型名听着像"就成了一个假承诺。
    """
    return adapters.manifest_payload()


@router.get("/providers/available", response_model=list[ProviderOptionOut])
def available_providers(_=Depends(require_user)) -> list[ProviderOptionOut]:
    """提交作业时可选的槽位：只列已启用且填了 provider/模型的。

    参数白名单跟着槽位一起回：界面按它渲染输入框，提交时后端再验一遍。
    这里不含 key_env、notes 与本地运行时目录——那些是系统配置那一页的事。
    """
    # 同上：这一页只关心 provider_config 那十行的验证史，定向查。
    # 0043：按档取——提交弹窗里的"已验证"说的是这一槽位当前那一档，
    # 不是这一 provider 历史上随便哪一档。
    # rows 只查一遍：原来这里、下面的循环、slot_keys 各查一次，
    # 同一个请求三趟一模一样的 SELECT。
    rows = providers.all()
    matrix = validation_repo.latest_matrix(
        {(r["capability"], r["backend"], r["provider"], r["model"])
         for r in rows}, by_model=True)
    out: list[ProviderOptionOut] = []
    for row in rows:
        if not row["enabled"] or not row["provider"].strip() or not row["model"].strip():
            continue
        adapter = adapters.find(row["capability"], row["backend"], row["provider"])
        # 提交弹窗要读这一份，所以同样用列表档超时：一个端点不通不该让弹窗卡住。
        health = adapters.health(row, probe_service=False,
                             probe_timeout=engines.LIST_TIMEOUT_SECONDS)
        badge, at = validations.badge(validations.summary_for(
            row, matrix.get(_slot_model_key(row), {})))
        out.append(ProviderOptionOut(
            capability=row["capability"],
            capability_label=CAPABILITIES.get(row["capability"], row["capability"]),
            backend=row["backend"],
            backend_label=BACKENDS.get(row["backend"], row["backend"]),
            provider=row["provider"], model=row["model"],
            adapter_label=adapter.label if adapter else "",
            health_state=health["state"], health_detail=health["detail"],
            validation_state=badge, validation_at=at,
            models=[m.as_dict() for m in adapter.models] if adapter else [],
        ))

    # 2026-11-07 起：**登记过但不在槽位里的档也要列**。
    #
    # 提交侧的显式那一支已经放开（判据从"与槽位逐字相等"改成"已登记 ∧ 已启用
    # ∧ 清单有适配器"），所以界面上必须看得见这些档——否则放开只对 curl 有效，
    # 生成中心的下拉还是那十条。同能力第二档（比如 fastmetal 的 5B）以前根本
    # 选不到，想切只能去改默认轨再改回来。
    #
    # 三件事不放松：
    #   ① 只列**清单里有适配器**的档——参数白名单在 adapters.py，没有它不能拼命令；
    #   ② 只列**已启用**的登记（`model_registration.enabled`）；
    #   ③ 与槽位重复的不再列一遍（槽位那份已经在上面的循环里，且带槽位级的健康）。
    slot_keys = {(r["capability"], r["backend"], r["provider"], r["model"])
                 for r in rows}
    extra: list[dict] = []
    for reg in registration_repo.registrations():
        key = (reg["capability"], reg["backend"], reg["provider"], reg["model_code"])
        if key in slot_keys or not reg["enabled"]:
            continue
        adapter = adapters.find(reg["capability"], reg["backend"], reg["provider"])
        if adapter is None:
            continue
        extra.append({"reg": reg, "adapter": adapter, "key": key})
    if extra:
        # 这些档的验证史同样按档定向查：它们不在 provider_config 里，
        # 但 `model_validation` 是按 (能力, 后端, provider, model) 落的。
        matrix2 = validation_repo.latest_matrix({e["key"] for e in extra}, by_model=True)
        for e in extra:
            reg, adapter = e["reg"], e["adapter"]
            row = {"capability": reg["capability"], "backend": reg["backend"],
                   "provider": reg["provider"], "model": reg["model_code"]}
            health = adapters.health(row, probe_service=False,
                             probe_timeout=engines.LIST_TIMEOUT_SECONDS)
            badge, at = validations.badge(validations.summary_for(
                row, matrix2.get(e["key"], {})))
            out.append(ProviderOptionOut(
                capability=reg["capability"],
                capability_label=CAPABILITIES.get(reg["capability"], reg["capability"]),
                backend=reg["backend"],
                backend_label=BACKENDS.get(reg["backend"], reg["backend"]),
                provider=reg["provider"], model=reg["model_code"],
                adapter_label=adapter.label,
                health_state=health["state"], health_detail=health["detail"],
                validation_state=badge, validation_at=at,
                models=[m.as_dict() for m in adapter.models],
            ))
    if ALLOW_MOCK:
        # 演示/自测开关（FASTVIDEO_ALLOW_MOCK=1，生产默认关闭）打开时，给每类能力补一条
        # 身份写明"不执行模型"的假槽位。为什么非要它进这份清单：这台机器上文本轨被
        # text_ai_route 强制走云端、云端图像档又没登记计价，于是"提交 → worker 领取 →
        # 回稿 → 填表 → 保存"这条链在真浏览器里一步都走不通，只能退回 pytest 用假数据代跑。
        # 它的产物是一段文本，所以拿它当"图像候选"时必然被导入端的格式校验挡下——
        # 这条纪律也因此能在界面上被看见，而不是只在测试里断言一次。
        for cap, cap_label in CAPABILITIES.items():
            out.append(ProviderOptionOut(
                capability=cap, capability_label=cap_label,
                backend="local", backend_label=BACKENDS["local"],
                provider="mock", model=adapters.MOCK_MODEL,
                adapter_label="假适配器（不执行模型）",
                health_state="ok",
                health_detail="演示/自测专用：只在 FASTVIDEO_ALLOW_MOCK=1 时出现，不执行任何命令",
                models=[{"code": adapters.MOCK_MODEL, "label": "mock-1（假适配器）",
                         "params": [p.as_dict() for p in adapters.MOCK_PARAMS],
                         "prices": [], "note": "只按参数睡一会儿，再写一段文本当产物"}],
            ))
    return out


@router.get("/providers/current", response_model=list[CurrentProviderOut])
def current_providers(_=Depends(require_user)) -> list[CurrentProviderOut]:
    """给其他模块只读的"当前默认"。五类能力固定五行，没配的也返回。

    普通模块要知道"这一类现在走本地还是云端、配没配好"，但不需要 notes
    与密钥变量名，所以这里只回状态，不复用管理页那份带备注的对象。
    """
    defaults = {row["capability"]: row for row in providers.defaults()}
    # 这里只念默认那几行的验证徽章，需要的槽位更少——按实际要读的给。
    # 0043：同样按档取，与另外两处同一把尺子。
    matrix = validation_repo.latest_matrix(
        {(r["capability"], r["backend"], r["provider"], r["model"])
         for r in defaults.values()}, by_model=True)
    out: list[CurrentProviderOut] = []
    for capability, label in CAPABILITIES.items():
        row = defaults.get(capability)
        if row is None:
            out.append(CurrentProviderOut(
                capability=capability, capability_label=label,
                backend="", backend_label="", provider="", model="",
                adapter="", health_state="unconfigured",
                health_detail="没有默认后端"))
            continue
        health = adapters.health(row, probe_service=False,
                             probe_timeout=engines.LIST_TIMEOUT_SECONDS)
        badge, at = validations.badge(validations.summary_for(
            row, matrix.get(_slot_model_key(row), {})))
        out.append(CurrentProviderOut(
            capability=capability, capability_label=label,
            backend=row["backend"], backend_label=BACKENDS.get(row["backend"], ""),
            provider=row["provider"], model=row["model"],
            adapter=(health["adapter"] or {}).get("label", ""),
            health_state=health["state"], health_detail=health["detail"],
            validation_state=badge, validation_at=at))
    return out


@router.get("/providers/validations", response_model=list[ValidationOut])
def list_validations(response: Response, limit: int = Query(50, ge=1),
                     before_id: int | None = None,
                     _=Depends(require_permission("system:config"))) -> list[ValidationOut]:
    """验证历史（一页）。只有管理员能看整份——detail 里会有本机路径与缺失项。

    **分页是这一版补的**：原来只有 `limit`，而仓库层把它夹在 200 以内，于是
    "第 201 条之后的历史读不到"却没有任何提示——翻到底的人会以为历史只有这么多条。
    现在两条路都给：`before_id` 拿上一页最后一条的 id 翻旧账（表只追加，
    "比某条更早"是稳定边界）；总条数走响应头 `X-Total-Count`，界面据此说
    "共 N 条，这一页 M 条"，而不是自己拿"少于 limit"猜到底有没有下一页。
    响应体形状不变（仍是数组）——它已被浏览器验收与《模型中心使用说明书》按
    "数组"引用，为了分页改成包一层对象会连带改掉一批读法。
    """
    rows = [ValidationOut.from_row(r)
            for r in validation_repo.list_recent(limit, before_id)]
    response.headers["X-Total-Count"] = str(validation_repo.count())
    return rows


@router.post("/providers/validate", response_model=ValidationOut)
def run_env_validation(payload: ValidationIn,
                       user=Depends(require_permission("system:config"))) -> ValidationOut:
    """跑一次环境检查并留痕。

    这一枪**不发网络请求、不加载权重**，所以它永远不能替代真实调用验证；
    `kind` 也被限死成 env_check——想从这里写出一条 real_call，接口直接拒。
    """
    if payload.kind != "env_check":
        raise HTTPException(status_code=422,
                            detail="这个入口只做环境检查；真实调用要从一条已成功的作业登记")
    # `provider_config` 是固定十行（5 类能力 × local／cloud），远程自建那条轴
    # 不存在这样的"槽位行"：它的环境结论由「模型中心 · 引擎管理」按登记行现查
    # （本机查不了别人的文件系统 ⇒ unknown）。所以这里对 remote 说清楚去哪儿查，
    # 不要回一句"这一轨不存在"让人以为是配置坏了。
    if payload.backend == "remote":
        raise HTTPException(
            status_code=422,
            detail="远程自建没有 provider_config 槽位行；它的环境检查请用"
                   "「模型中心 · 引擎管理」里那条登记记录上的检查按钮")
    row = providers.slot(payload.capability, payload.backend)
    if row is None:
        raise HTTPException(status_code=422, detail="这一轨不存在")
    if payload.provider and row["provider"] != payload.provider:
        raise HTTPException(status_code=422,
                            detail=f"这一轨现在配的是 {row['provider']}，不是 {payload.provider}")
    result = validations.env_check(row, user["id"])
    return ValidationOut.from_row(result["validation"])


@router.post("/providers/validate-job/{job_id}", response_model=ValidationOut)
def record_job_validation(job_id: int, payload: ValidationIn,
                          user=Depends(require_permission("system:config"))) -> ValidationOut:
    """把一条已经跑完的作业登记成该槽位的验证凭据。

    为什么只让管理员做：这条记录会让界面上那一格从"环境检查通过"升级成
    "真实调用已验证"，是个有后果的判断，不该由提交作业的人顺手点。
    """
    if payload.kind == "env_check":
        raise HTTPException(status_code=422, detail="环境检查不走作业，请用「环境检查」按钮")
    try:
        result = validations.from_job(job_id, payload.kind, user["id"], payload.detail,
                                      passed=payload.passed)
    except validations.ValidationInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ValidationOut.from_row(result["validation"])


@router.put("/providers", response_model=list[ProviderOut])
def save_providers(updates: list[ProviderUpdate],
                   user=Depends(require_permission("system:config"))) -> list[ProviderOut]:
    current = {row["id"]: row for row in providers.all()}

    # 只允许改已有的行：不认识的 id 直接拒，避免半截写入
    unknown = [u.id for u in updates if u.id not in current]
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知的配置行：{unknown}")

    # 弱版本闸（见 `models.provider_slot_version`）：这一条是**整批覆盖**，
    # 两个人各改一行先后保存时，后保存的会把前一个人改的那一行静默盖回旧值。
    # 只在调用方带了令牌时才判——脚本与命令行不带＝行为不变（与资产页同一条边界），
    # 冲突回 409 而不是覆盖，并把"哪一行变了"点名，不让人对着十行自己找。
    stale = []
    for item in updates:
        seen = (item.seen_version or "").strip()
        if not seen:
            continue
        now = provider_slot_version(current[item.id])
        if seen != now:
            row = current[item.id]
            stale.append(f"{CAPABILITIES.get(row['capability'], row['capability'])}·"
                         f"{BACKENDS.get(row['backend'], row['backend'])}")
    if stale:
        raise HTTPException(
            status_code=409,
            detail="这几行在你打开之后被别人改过：" + "、".join(stale)
                   + "。这次整批没有保存，你填的内容还在界面上；"
                   "先刷新把最新的读进来对一遍——确认要顶掉就再保存一次"
                   "（刷新后再保存时令牌自然是新的）。")

    for item in updates:
        provider = item.provider.strip()
        if not PROVIDER_RE.match(provider):
            raise HTTPException(
                status_code=422,
                detail=f"provider 只能写字母、数字、点、横线、下划线：{provider}")
        env = item.key_env.strip()
        if env and not KEY_ENV_RE.match(env):
            raise HTTPException(
                status_code=422,
                detail=f"密钥变量名不合法（只能字母/数字/下划线，且不以数字开头）：{env}")

    # 每类能力必须恰好一个默认。两个默认等于"提交作业时不知道该走哪条"
    defaults: dict[str, int] = {}
    for item in updates:
        if item.is_default:
            cap = current[item.id]["capability"]
            defaults[cap] = defaults.get(cap, 0) + 1
            # 视频的云端档只能被镜头显式选中；设成默认等于"配了云端就默认花钱"，
            # 与规格 01-4.8.2「云端视频必须逐镜头显式选择」正面冲突。
            if cap == "video" and current[item.id]["backend"] == "cloud":
                raise HTTPException(
                    status_code=422,
                    detail="云端视频不能设为默认；只能逐镜头显式选择")
    duplicated = [CAPABILITIES.get(cap, cap) for cap, n in defaults.items() if n > 1]
    if duplicated:
        raise HTTPException(status_code=422,
                            detail="每类能力只能有一个默认后端：" + "、".join(duplicated))
    missing = [CAPABILITIES[cap] for cap in CAPABILITIES if defaults.get(cap, 0) == 0]
    if missing:
        raise HTTPException(status_code=422,
                            detail="每类能力都要选一个默认后端：" + "、".join(missing))

    providers.replace_many([item.model_dump() for item in updates])

    # 0044：选路的最小变更痕。只记**变了的行**——十行全记会把"实际发生了什么"
    # 淹进噪声。summary 一句话点名谁变了；默认轨变更把 before/after 都写进 payload。
    for item in updates:
        before = current[item.id]
        after = item.model_dump()
        changed = {k: (before[k], after.get(k))
                   for k in ("provider", "model", "enabled", "is_default")
                   if (before[k] if isinstance(before[k], int) else str(before[k]))
                   != (after.get(k) if isinstance(after.get(k), int) else str(after.get(k) or ""))}
        if not changed:
            continue
        key = f"{before['capability']}/{before['backend']}"
        pieces = []
        for field, (old, new) in changed.items():
            if field == "is_default" and new:
                pieces.append("设为默认轨")
            elif field == "is_default":
                pieces.append("取消默认")
            else:
                pieces.append(f"{field}: {old or '空'} → {new if new not in (None, '') else '空'}")
        config_log_record(
            "provider_slot", key, "update",
            "；".join(pieces) or "字段有变化",
            {"changed": {k: list(v) for k, v in changed.items()},
             "is_default_before": bool(before["is_default"]),
             "is_default_after": bool(after.get("is_default"))},
            created_by=user["id"])
    return list_providers()
