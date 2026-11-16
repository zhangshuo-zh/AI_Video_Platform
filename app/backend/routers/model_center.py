"""模型中心：登记、引擎视图、任务调用策略、场景候选。

一句话职责边界：**这一层只管"模型与引擎的配置与真相"，不管选路。**
选路仍然只有 `services.resolve_provider()` 一处，默认轨仍然只有
`provider_config.is_default` 一个来源（裁定 MC1＝备用方案只提示不自动切）。
所以本文件里没有任何接口会被提交路径自动采用，`/model-policies/resolve`
返回的候选只是给人看的，返回值里也明写 `auto_switch: false`。

权限全部沿用 `system:config`（18 个权限点之一，不新增）。只有
`/model-policies/resolve/{scene}` 是登录即可——它和 `/providers/available` 一样是
"业务页面上的候选池"，不含密钥变量名、不含备注、不含本机路径之外的敏感信息。
"""
import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import adapters
import cloud_probe
import engine_launch
import engines
import model_center as mc
import validations
from services import model_configuration
from deps import require_permission, require_user
from models import (CAPABILITIES, ConfigChangesOut,
                    EngineActionOut, EngineInstanceIn, EngineInstancePatch,
                    EngineListOut, EngineMutationOut, EngineProbeOut,
                    EngineProbesOut, ModelCatalogOut, ModelMutationOut,
                    ModelPoliciesOut, ModelPoliciesSaveOut, ModelPolicyIn,
                    ModelProbeOut, ModelRegistrationIn, ModelRegistrationPatch,
                    ModelResolveOut, ModelScenesOut, ModelStatsOut,
                    REG_SOURCES,
                    RuntimePathIn, RuntimePathItem, RuntimePathsOut,
                    SecretItem, SecretProbeOut, SecretsOut,
                    SecretValueIn, SecretValueOut, SecretValuesOut,
                    ValidationOut)
from repositories.config_log import record as config_log_record
from repositories.engine_instances import (SUPPORTED_CONNECTION_TYPES,
                                           EngineInstanceError, engine_instances,
                                           engine_probe_log)
from repositories.model_center import RegistrationError, model_center
from repositories import secret_value

router = APIRouter(prefix="/api", tags=["model-center"])

# 验证表认哪些 backend。**不在这里数一遍**：0045 起 model_validation 的 CHECK
# 与登记层的 REG_SOURCES 是同一套取值（local／cloud／remote），第二份枚举就是
# 第二把会漂的尺子——这个文件里已经有过一次教训（见 test_枚举的说法只有一份出处）。
# 原来只认 local/cloud，于是 remote 登记"验不了也留不下痕"；那不是设计选择，
# 是表装不下那个值。0045 扩了 CHECK 之后两边取值相同。
_VERIFIABLE_BACKENDS = tuple(REG_SOURCES)


def _json_list(raw) -> list:
    return model_configuration._json_list(raw)


# 破坏性接口的确认口令。原来是「confirm 必须等于中文字面量」——`撤登记`／`撤实例`
# 各是一个魔法词，从 curl 手工调用要 UTF-8 转义（`confirm=%E6%92%A4%E7%99%BB%E8%AE%B0`），
# 拼错一个字得到的是 422 而不是"你拼错了"这种信息；更糟的是它把界面文案变进了协议：
# 哪天改一句提示语，接口的入参也跟着变。
# 现在只认"人表达过一次确认"这件事本身：1／true／yes（大小写不敏感）。
# 语义边界不变——**这一枪仍然不能裸调**，只是不再要求一个中文词。
# 项目删除那条仍然是 `confirm=<项目名>`：那里比的是"你有没有打对名字"，
# 是防手滑的另一种强度（见 routers/projects.py），不入这一套。
_CONFIRM_WORDS = frozenset({"1", "true", "yes"})


def _confirmed(raw: str) -> bool:
    return (raw or "").strip().lower() in _CONFIRM_WORDS


def _row_or_404(reg_id: int):
    row = model_center.registration(reg_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有这条登记：#{reg_id}")
    return row


def _item_of(reg_id: int) -> dict:
    return model_configuration._item_of(reg_id)


@router.get("/models", response_model=ModelCatalogOut)
def list_models(_=Depends(require_permission("system:config"))) -> dict:
    return model_configuration.catalog_payload()


@router.get("/models/stats", response_model=ModelStatsOut)
def model_stats(_=Depends(require_permission("system:config"))) -> dict:
    """顶部四数。口径由后端一处给（caliber 字段），界面只念不算。"""
    return mc.stats()


@router.post("/models/probe", response_model=ModelProbeOut)
def probe_models(fresh: int = 0,
                 _=Depends(require_permission("system:config"))) -> dict:
    """现查一次服务型引擎，再把**同一份**目录与四数一起回给界面。

    为什么要有这一条：`GET /api/models` 从 2026-11-07 起**不出网**（详见
    `mc.catalog()` 的注解）——打开一页不该被一台卡住的服务拖住。于是"这一档
    到底连不连得上"必须有一个显式入口，这就是它：人点了「探测」或页面画完
    之后自己补这一枪，两种都等得起。

    `fresh=1`＝绕过 15 秒缓存（人明确要求现查）。页面画完自动补的那一枪用 0：
    刚在别处探过就直接复用，不为一屏之内重复问同一台机器。

    返回的是**整张目录**而不是几条健康：界面拿它整页重绘，字号、状态、顶部四数
    因此与列表逐行对得上；只回健康的话前端还得自己把状态拼一遍，那就是第二套口径。

    `cloud` 段是 2026-11-08 补的：按钮原来只现查本机服务型引擎，云端轨"查的是
    密钥变量在不在"，点一下按钮云端一格不动，用起来怪。现在云端也真去供应商
    网关问一次——但**结论口径不变**：探的是"路通不通、钥匙对不对"，永远不等于
    "那一档模型可用"。实现与纪律见 `cloud_probe.py` 模块头。
    """
    return mc.probe_all(fresh=bool(fresh))


@router.post("/models", response_model=ModelMutationOut, status_code=201)
def create_model(payload: ModelRegistrationIn,
                 user=Depends(require_permission("system:config"))) -> dict:
    capability = payload.capability.strip()
    backend = payload.backend.strip()
    provider = payload.provider.strip()
    model_code = payload.model_code.strip()
    if capability not in CAPABILITIES:
        raise HTTPException(status_code=422,
                            detail=f"能力只能是 {'、'.join(CAPABILITIES)}，不是「{capability}」")
    if backend not in REG_SOURCES:
        raise HTTPException(status_code=422,
                            detail=f"运行位置只能是 {'、'.join(REG_SOURCES.values())}")
    if not provider or not model_code:
        raise HTTPException(status_code=422, detail="provider 与模型标识都不能空")

    # remote 在清单里没有对应轴（清单按 local/cloud 登记），比的时候折回 local，
    # 这样"远程自建但用的是同一套 wan 参数"能认出来；认不出来就走下面的警告。
    # 折回判据住在 `adapters.MANIFEST_AXIS`：写路径与 `mc.find_adapter()` 的读路径
    # 必须同一个口径，否则人按界面登记一条 remote，列表里立刻说"清单未登记"。
    adapter = mc.find_adapter(capability, backend, provider)

    try:
        row = model_center.create({
            "capability": capability, "backend": backend, "provider": provider,
            "model_code": model_code, "display_name": payload.display_name,
            "source": payload.source if payload.source in REG_SOURCES else backend,
            "engine_code": payload.engine_code or (adapter.code if adapter else provider),
            "version": payload.version, "artifact_path": payload.artifact_path,
            "remote_model_id": payload.remote_model_id,
            "capabilities": payload.capabilities, "enabled": payload.enabled,
            "note": payload.note, "created_by": user["id"],
        })
    except RegistrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": row["id"], "item": _item_of(row["id"]),
            "in_manifest": adapter is not None,
            "warning": "" if adapter is not None else
            "清单里没有这个 provider：登记留下了，但参数不猜、运行方式不猜，提交侧会被拒"}


@router.patch("/models/{reg_id}", response_model=ModelMutationOut)
def patch_model(reg_id: int, payload: ModelRegistrationPatch,
                _=Depends(require_permission("system:config"))) -> dict:
    _row_or_404(reg_id)
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "source" in patch and patch["source"] not in REG_SOURCES:
        raise HTTPException(status_code=422,
                            detail=f"运行位置只能是 {'、'.join(REG_SOURCES.values())}")
    try:
        model_center.update(reg_id, patch)
    except RegistrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"item": _item_of(reg_id)}


@router.delete("/models/{reg_id}", response_model=ModelMutationOut)
def remove_model(reg_id: int, confirm: str = "",
                 user=Depends(require_permission("system:config"))) -> dict:
    """撤登记。**不删权重文件、不动历史作业、不清验证记录**。

    要 confirm 是因为人常把"移除登记"理解成"删掉模型"；
    提示语里必须把"文件还在盘上"说明白，否则下次找权重就找不着了。
    """
    row = _row_or_404(reg_id)
    if not _confirmed(confirm):
        raise HTTPException(
            status_code=422,
            detail="要撤登记请带 confirm=1（或 confirm=true／yes）。这一步只删除这条登记："
                   "权重文件仍在盘上，历史作业与验证记录一律不动。")
    model_center.delete(row["id"])
    # 0044：撤登记可能把默认轨正指着的那一档撤掉——这一笔尤其要留痕。
    config_log_record(
        "model_registration",
        f"{row['capability']}/{row['backend']}/{row['provider']}/{row['model_code']}",
        "delete", f"撤了登记 #{row['id']}",
        {"registration_id": row["id"], "display_name": row["display_name"]},
        created_by=user["id"])
    return {"removed": row["id"],
            "note": "只撤了登记；权重文件、job 历史与 model_validation 记录都还在"}


@router.post("/models/{reg_id}/check", response_model=ValidationOut)
def check_model(reg_id: int, user=Depends(require_permission("system:config"))):
    """对这一档跑一次环境检查并留痕（复用 `validations.env_check`）。

    这一枪不发请求、不加载权重，所以它永远不能把状态推到"可用"——
    可用要求 real_call 或 artifact_review，那是另一回事。

    留痕时 `backend` 写**登记行自己的值**（0045），不写折回后的清单轴：
    `mc.health_row()` 折回是为了让 `adapters.health()` 找得到适配器，
    而记录该记在谁名下是另一件事。remote 那一档尤其不能写成 local，
    否则这条凭据会混进本地的验证史里。
    """
    row = _row_or_404(reg_id)
    if row["backend"] not in _VERIFIABLE_BACKENDS:
        raise HTTPException(
            status_code=422,
            detail=f"这个运行位置（{row['backend']}）不在验证表认的取值里。"
                   "model_validation 的 CHECK 只认 local／cloud／remote 三档。")
    try:
        # 结论走 `mc._health_of()` 而不是 `adapters.health(mc.health_row(...))`：
        # 后者会把 backend 折回清单轴，remote 那一档因此被当成"本地同 provider"去查，
        # 得到的是**另一台机器的健康结论**。`_health_of()` 对 remote 直接回 unknown
        # （本机不查别人的文件系统），这才是这一档真实的结论。
        probe_row = {"capability": row["capability"], "backend": row["backend"],
                     "provider": row["provider"], "model": row["model_code"]}
        result = validations.env_check(probe_row, user["id"],
                                       health=mc._health_of(dict(row)))
    except validations.ValidationInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ValidationOut.from_row(result["validation"])


@router.get("/model-policies", response_model=ModelPoliciesOut)
def list_policies(_=Depends(require_permission("system:config"))) -> dict:
    return model_configuration.policies_payload()


@router.put("/model-policies", response_model=ModelPoliciesSaveOut)
def save_policies(payload: list[ModelPolicyIn],
                  user=Depends(require_permission("system:config"))) -> dict:
    try:
        return model_configuration.save_policies(payload, user["id"])
    except model_configuration.ModelPolicyInvalid as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/config-changes", response_model=ConfigChangesOut)
def list_config_changes(limit: int = Query(50, ge=1),
                        _=Depends(require_permission("system:config"))) -> dict:
    """选路配置的变更痕（0044）。最近改了什么：谁、什么时候、从什么改成什么。

    只读。这是"最小变更痕"的出口，不是通用审计——业务表大部分写路径
    仍然没有痕（v0.87 裁定），这张表只收选路三件事：
    能力轨整批保存、撤登记、策略整批保存。
    """
    from repositories.config_log import recent

    rows = [dict(r) for r in recent(limit)]
    # payload_json 是写进库的 JSON 串：读出来解析好再给，界面不用自己 json.loads
    for r in rows:
        try:
            r["payload"] = json.loads(r.pop("payload_json") or "{}")
        except ValueError:
            r["payload"] = {}
    return {"items": rows}


@router.get("/model-policies/scenes", response_model=ModelScenesOut)
def list_scenes(_=Depends(require_user)) -> dict:
    return model_configuration.scenes_payload()


@router.get("/model-policies/resolve/{scene_key}", response_model=ModelResolveOut)
def resolve(scene_key: str, _=Depends(require_user)) -> dict:
    """场景 → 候选与挡路原因。只读，登录即可（与 /providers/available 同一档）。"""
    out = mc.resolve_scene(scene_key)
    # 跟着清单里的策略走，而不是在这里写死一个字面量：今天没人自动切是因为
    # `no_auto_fallback` 为真，将来谁把那条策略改了，这一句必须跟着变——
    # 界面据此说"要不要替你顶上"，说错比不说更糟。
    out["auto_switch"] = not adapters.POLICY["no_auto_fallback"]
    return out



def _tier_problem(capability: str, provider: str, model_code: str) -> str:
    return model_configuration._tier_problem(capability, provider, model_code)


# ================= 引擎实例（W-B／CR-001） =================
# 这一族接口管的是"这一档引擎在哪个端点"，不是"这台机器上跑了什么模型"。
# 三条边界与 0040 表头、`repositories/engine_instances.py` 的头一致：
#   * engine_code 必须在清单里——实例不能凭空发明一个 provider；
#   * 返回值里的健康是**现查**的，库里没有那一列；
#   * 密钥只出现在"变量名＋有没有值"这一层，值永不进响应。

@router.get("/engines", response_model=EngineListOut)
def list_engines(_=Depends(require_permission("system:config"))) -> dict:
    return model_configuration.engines_payload()


@router.post("/engines", response_model=EngineMutationOut, status_code=201)
def create_engine(payload: EngineInstanceIn,
                  user=Depends(require_permission("system:config"))) -> dict:
    code = payload.engine_code.strip()
    if _adapter_for(code) is None:
        raise HTTPException(
            status_code=422,
            detail=f"清单里没有这一档引擎：{code}。加引擎是改规格（`adapters.py`），"
                   "实例表不能凭空发明一个 provider")
    try:
        row = engine_instances.create({
            "engine_code": code, "display_name": payload.display_name,
            "connection_type": payload.connection_type, "endpoint": payload.endpoint,
            "shared_secret_env": payload.shared_secret_env,
            "enabled": payload.enabled, "note": payload.note,
            "created_by": user["id"]})
    except EngineInstanceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": row["id"], "warning": _probe_warning(row, user["id"])}


@router.patch("/engines/{instance_id}", response_model=EngineMutationOut)
def patch_engine(instance_id: int, payload: EngineInstancePatch,
                 _=Depends(require_permission("system:config"))) -> dict:
    if engine_instances.instance(instance_id) is None:
        raise HTTPException(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        row = engine_instances.update(instance_id, patch)
    except EngineInstanceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": row["id"], "enabled": bool(row["enabled"])}


@router.delete("/engines/{instance_id}", response_model=EngineMutationOut)
def remove_engine(instance_id: int, confirm: str = "",
                  _=Depends(require_permission("system:config"))) -> dict:
    """撤实例。**不动应用本身、不动历史作业**。"""
    row = engine_instances.instance(instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
    if not _confirmed(confirm):
        raise HTTPException(
            status_code=422,
            detail="要撤实例请带 confirm=1（或 confirm=true／yes）。这一步只删除这条登记："
                   "应用与它的模型都不受影响，历史作业仍按当时冻结的快照可查。")
    engine_instances.delete(row["id"])
    return {"removed": row["id"],
            "note": "只撤了端点登记；应用、权重与 job 历史一律没碰"}


@router.get("/engines/{instance_id}/probes", response_model=EngineProbesOut)
def list_engine_probes(instance_id: int, limit: int = Query(20, ge=1),
                       _=Depends(require_permission("system:config"))) -> dict:
    """这条实例的探测历史，最新在前。**只读，不触发探测**。

    有这一条的理由：卡面上那一行只说"最近一条"。要回答"这台是不是三天两头掉线"，
    得看几条；而历史只追加不改，倒序按 id 天然成立（不需要 OFFSET，那张表的
    时间列精度只到秒，同一秒里探两次按时间排会让顺序变成猜的）。
    """
    row = engine_instances.instance(instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
    items = [dict(r) for r in engine_probe_log.recent(instance_id, limit)]
    return {"items": [{**item, "age_seconds": _age_seconds(item["created_at"])}
                      for item in items],
            "note": "只追加不改：这条历史只回答「什么时候探过、当时如何」，"
                    "不参与任何判断——现在通不通仍由探测现查"}


@router.post("/engines/{instance_id}/probe", response_model=EngineProbeOut)
def probe_engine(instance_id: int,
                 user=Depends(require_permission("system:config"))) -> dict:
    """手动探一次：只读一次服务自述，不建 Job。**留一条痕**（0042）。

    2026-11-07 起留痕：原来这里明写不写库，理由是 `model_validation`（0021）
    按 (capability, backend, provider) 索引、装不下实例这个形状——那条理由是对的，
    结论（所以就不留了）不是。于是全平台没有地方能回答"这台 Draw Things
    上周还连不连得上"。现在 `engine_probe_log`（0042）按 instance_id 记，形状对得上。

    **两张历史表不合并**（同一轮的结论）：主体不同——那边是"这一档凭什么能用"的
    验证凭据，这里是"这个端点上一次被问"的探测事件；`state` 枚举也不是两套说法，
    而是这边（ok/missing/unknown）为那边六档的真子集。硬合成一张公共事件表，
    等于逼这张表接受它永远产不出的三档。

    留痕**只在这里与建实例那一次**发生；`GET /api/engines` 是读路径，一次都不写
    （它只读最近一条，用来把"上次什么时候探的"摆出来）。这条边界与
    "读接口不出网"同源：读路径不做副作用。
    """
    row = engine_instances.instance(instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
    # 人明确按了「探测」，所以 fresh=True 绕过 15 秒缓存、用满 5 秒超时问到底：
    # "我刚把应用打开了，为什么还显示连不上"这类问题不该由缓存回答。
    started = time.monotonic()
    status = engines.probe(row["engine_code"], row["endpoint"], fresh=True)
    latency_ms = int((time.monotonic() - started) * 1000)
    # 留痕失败不把一次成功的探测变成 500：人要的是"现在通不通"，那是已经
    # 得到的结论；历史没记上是这一屏的次要信息。
    engine_probe_log.record(row["id"], status, source="manual",
                            latency_ms=latency_ms, created_by=user["id"],
                            engine_code=row["engine_code"], endpoint=row["endpoint"])
    return {"id": row["id"], "state": status.state, "detail": status.detail,
            "model": status.values.get("model", ""),
            "latency_ms": latency_ms,
            "endpoint": status.values.get("endpoint", row["endpoint"])}


@router.post("/engines/{instance_id}/launch", response_model=EngineActionOut)
def launch_engine(instance_id: int, request: Request,
                  user=Depends(require_permission("system:config"))) -> dict:
    """按内置配方把这一台本机服务拉起来，起完复探一次。

    存在的理由：引擎列表说「连不上」时，人得离开页面去终端/启动台把服务起来，
    再回来点探测。平台本来就跑在这台机器上，这个来回可以省掉。

    **三道边界，缺一不可**（详见 `engine_launch` 模块头）：
      1. 配方是常量不是数据——命令按 engine_code 查表，绝不接受请求里传来的
         可执行路径。`0040_engine_instance.sql:34` 那句 CR-001 明令禁止的就是
         把「可执行入口」变成可写字段；这个接口不能把它变成可执行字段。
      2. 只认回环来源——`FASTVIDEO_HOST=0.0.0.0` 开放到局域网时，
         「点一下在你机器上起进程」必须挡住。
      3. 先探再起、起后半秒一探——重复点击不起第二个进程，
         且「进程起来了」与「端口答话了」分开报（Draw Things 的 API Server
         是应用内开关，默认关，应用起来照样连不上）。

    返回值里 `started` 与 `reachable` 是两个字段而不是一个布尔：把 exit code 0
    读成「能用了」正是这一屏最容易出的谎。
    """
    client = request.client.host if request.client else ""
    if client not in engine_launch.LOOPBACK:
        raise HTTPException(
            status_code=403,
            detail="只能在运行平台的这台机器上启动本机引擎（当前请求来自 "
                   f"{client}）。请在服务器本机打开页面，或手动启动后回来点探测。")

    row = engine_instances.instance(instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
    if row["connection_type"] not in SUPPORTED_CONNECTION_TYPES:
        raise HTTPException(
            status_code=422,
            detail="这一档连接方式不是本机 HTTP 服务，平台不会按启动配方拉它")

    result = engine_launch.launch(row["engine_code"], row["endpoint"])
    if not result.ok and not result.started and not result.reachable:
        # 三次明确拒绝（没配方 / 没装 / Popen 抛错）都落在这里；已经在跑与
        # 「起了但没答话」不算失败请求——那是如实结论，界面要能原样显示。
        raise HTTPException(status_code=409, detail=result.message)

    if result.reachable:
        # 起来并答话了才留痕：留痕表记的是「这个端点被问过」，
        # 没答话的那几次已经由返回体告诉人，不往历史里塞半截结论。
        #
        # source 记 `manual` 而不是新开一个 `launch` 档：0042 的 CHECK 只认
        # manual/create，加第三档要改约束（SQLite 得重建表）。而"这一次是
        # 人按了按钮去问的"这个语义，manual 已经说全了——按启动按钮的人
        # 与按探测按钮的人是同一个人、同一个意图（"我要知道这台通不通"）。
        # 为了一个界面不显示的区别去重建一张只追加表，代价与收益不成比例。
        status = engines.probe(row["engine_code"], row["endpoint"], fresh=True)
        engine_probe_log.record(row["id"], status, source="manual",
                                latency_ms=0, created_by=user["id"],
                                engine_code=row["engine_code"],
                                endpoint=row["endpoint"])

    return {"id": row["id"], "engine_code": row["engine_code"],
            "started": result.started, "reachable": result.reachable,
            "message": result.message}


@router.post("/engines/{instance_id}/stop", response_model=EngineActionOut)
def stop_engine(instance_id: int, request: Request,
                user=Depends(require_permission("system:config"))) -> dict:
    """按内置配方优雅关闭这一台本机服务。

    通过 AppleScript 或基于端口号安全定位进程 PID 并向其发送退出信号，
    绝不误杀无关进程。
    """
    client = request.client.host if request.client else ""
    if client not in engine_launch.LOOPBACK:
        raise HTTPException(
            status_code=403,
            detail="只能在运行平台的这台机器上关闭本机引擎（当前请求来自 "
                   f"{client}）。请在服务器本机操作。")

    row = engine_instances.instance(instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
    if row["connection_type"] not in SUPPORTED_CONNECTION_TYPES:
        raise HTTPException(
            status_code=422,
            detail="这一档连接方式不是本机 HTTP 服务，平台不会按控制配方关闭它")

    result = engine_launch.stop(row["engine_code"], row["endpoint"])
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)

    return {"id": row["id"], "engine_code": row["engine_code"],
            "stopped": result.stopped, "message": result.message}


def _age_seconds(stamp: str) -> int | None:
    return model_configuration._age_seconds(stamp)


def _last_probe_fields(row) -> dict:
    return model_configuration._last_probe_fields(row)


def _adapter_for(code: str):
    return model_configuration._adapter_for(code)


def _probe_warning(row, user_id: int | None = None) -> str:
    """新建成功后顺手探一次，连不上就把原话带回界面——

    但不因此回 4xx：登记一个"还没启动的服务"是合法动作（人可能先建记录再开应用）。
    2026-11-07 起这一枪也留痕（`source="create"`）：它是"这台机器在这个时刻
    是什么状态"的第一个事实点，丢掉可惜，而且"建完就没再碰过"恰恰是最需要
    看见的情况（那条痕会明说"从没手动探测过"）。
    """
    if not row["enabled"]:
        return ""
    started = time.monotonic()
    status = engines.probe(row["engine_code"], row["endpoint"])
    engine_probe_log.record(row["id"], status, source="create",
                            latency_ms=int((time.monotonic() - started) * 1000),
                            created_by=user_id,
                            engine_code=row["engine_code"], endpoint=row["endpoint"])
    if status.state == "ok":
        return ""
    return f"实例留下了，但现在连不上：{status.detail}"


# ---------------------------------------------------------------- 本地运行时路径（0051）
#
# 为什么挂在模型中心这一组而不是新开一个"参数管理"一级菜单：
#   * 这 10 个变量**全部**是引擎/模型运行时相关的（见 `runtime_paths.LABELS`）；
#   * 引擎管理页已经在同一屏，人配完引擎顺手配路径，不必去第二个地方找；
#   * 一级菜单是稀缺资源（每次加都要教人"去哪个新页面"），不值得为 10 行配置开一个。
#
# 三条与密钥边界的硬关系：
#   * 这一组**只接受路径类变量**，白名单在 `repositories/runtime_path.ALLOWED_ENV`
#     与 migration 0051 的 CHECK 里各写一遍。任何 `*_API_KEY` / `*_TOKEN`
#     都进不来，两道各自独立。
#   * 密钥仍只从进程环境读（`adapters.py` 头第 3 条），这里一行都不碰。
#   * 响应里出现的路径是**本机文件系统路径**，只给 `system:config` 的人看。

@router.get("/runtime-paths", response_model=RuntimePathsOut)
def list_runtime_paths(_=Depends(require_permission("system:config"))
                       ) -> RuntimePathsOut:
    """本地运行时路径：每一项三份值 + 探测状态。

    `state` 用 `runtime` 的真探测（文件系统检查，不加载权重、不发网络请求），
    三档 ok／missing／unknown 与 `adapters.health` 共用一套词：
      * ok      —— 配了且东西真的在
      * missing —— 配了但路径不在（人要做的事：改路径或把东西搬过去）
      * unknown —— 没配（人要做的事：配一个）
    把 missing 说成 unknown 会让人以为"还没配"，而去配一个其实配错的地方。
    """
    import runtime_paths
    items = []
    for it in runtime_paths.describe():
        state, detail = _runtime_state(it["env_name"], it["effective"])
        items.append(RuntimePathItem(**it, state=state, detail=detail))
    return RuntimePathsOut(
        items=items,
        worker_needs_restart=False,
        note="环境变量优先于页面；被环境占住的那几项页面改不动。"
             "改完路径后 worker 需要重启才会用新值。")


def _runtime_state(env_name: str, effective: str | None) -> tuple[str, str]:
    """把"这个值到底能不能用"问出来。返回 (state, detail)。

    只用文件系统检查，与 `runtime.py` 同一档纪律。认不出属于哪个引擎的变量
    （理论上不会有，白名单是固定的）就回 unknown，不猜。
    """
    if not effective:
        return "unknown", "还没配置"
    from pathlib import Path
    p = Path(effective).expanduser()
    # `*_BIN` 是可执行文件，其余是目录——判据不同，用名字区分而不是猜
    if env_name.endswith("_BIN"):
        if p.is_file():
            return "ok", str(p)
        return "missing", f"{p} 不是一个存在的文件"
    if p.is_dir():
        return "ok", str(p)
    if p.exists():
        return "missing", f"{p} 存在但不是目录"
    return "missing", f"{p} 不存在"


@router.put("/runtime-paths/{env_name}", response_model=RuntimePathsOut)
def save_runtime_path(env_name: str, payload: RuntimePathIn,
                      user=Depends(require_permission("system:config"))
                      ) -> RuntimePathsOut:
    """保存一项。空字符串 = 清除覆盖（回到跟随环境/默认）。

    **不在这里重启 worker**：重启会 `_terminate` 正在跑的子进程，让作业记成
    `worker_stopped`。那是人该做的决定（页面上有独立的重启按钮），
    不能因为"顺手保存一下路径"就把人家跑着的视频掐掉。

    环境变量占住的那一项允许写库（写进去将来环境没了就用得上），但会如实
    告诉调用方"当前不生效"——`locked=True` 就是那句话。
    """
    import runtime_paths
    from repositories import runtime_path

    try:
        saved = runtime_path.save(env_name, payload.path,
                                  updated_by=user.get("id") if isinstance(user, dict) else None)
    except ValueError as exc:
        # 不在白名单（含任何密钥类变量名）：415 太怪，用 422 表达"这个字段不接受"
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # 顺手记一笔变更痕（0044 那张表只追加）。subject 取值受它自己的 CHECK 限制，
    # 这里借 `model_policy` 之外没有更合适的档，所以**不硬塞**——配置变更本就有
    # `updated_at`/`updated_by` 两列可查，为一条路径去改 0044 的 CHECK 不划算。
    runtime_paths.apply_one(env_name, saved)

    items = []
    for it in runtime_paths.describe():
        state, detail = _runtime_state(it["env_name"], it["effective"])
        items.append(RuntimePathItem(**it, state=state, detail=detail))
    return RuntimePathsOut(
        items=items,
        worker_needs_restart=True,
        note="已保存。**正在跑的 worker 还在用旧值**——它拿的是启动那一刻的环境快照，"
             "要重启 worker 才会用新路径。重启会中断当前作业（可重试）。")


# ---------------------------------------------------------------- 密钥指纹与连通性（2026-11-11）
#
# 为什么需要（用户场景）：配置里可能同时有好几把 key（旧的没删、新加的、
# 按项目分开的），人在供应商后台看到一串 key 时**无法确认这台机器上配的是哪一把**，
# 而让他去 `cat ~/.zshrc` 看等于给了 shell。指纹解决的就是这一个问题。
#
# **这一组一个字符的密钥值都不返回。** 指纹是 `sha256(值)` 的前 8 位，
# 推不回原值；`preview` 只露出供应商的公开家族标记（`sk-`／`sk-ant-` 这类
# 公开文档里写着的名字），**不露值的任何随机段**——第一版写错成
# `value[:10]` 时实测把真实密钥前 12 位印在了页面上，已修（见 `_KEY_PREFIX_RE`）。
#
# 两条与重启的关系，必须在界面上一并说清（这是本组存在的一半理由）：
#   * 密钥由**环境变量**给，而 worker 是 fork 时拿到环境快照的独立进程：
#     改了 `.zshrc` 不重启 worker，云端作业仍会报「环境变量 X 没有值」；
#   * 但 **API 进程读的是实时的**（`adapters.key_present` 每次现读 `os.environ`），
#     所以这一页的「已配置／指纹」在导出后会立刻变，而作业那边没变——
#     这个错配是让人怀疑平台坏了的第一来源，页面上要直说。

def _cloud_key_items() -> list[dict]:
    """所有云端能力登记过的 (provider, key_env)，按 provider 去重。

    为什么从**登记表**取而不是把 `adapters.ADAPTERS` 全列一遍：清单里有
    kling／seedance／jimeng 这类"已声明但没接"的档，把它们的空 key 摆一屏
    会让人以为平台支持那些供应商。登记过的才是真的在用。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for row in model_center.registrations():
        if row["backend"] != "cloud":
            continue
        adapter = adapters.find(row["capability"], "cloud", row["provider"])
        if adapter is None or adapter.code in seen:
            continue
        seen.add(adapter.code)
        env_name = adapters.resolved_key_env(adapter.code, adapter.capability, "cloud")
        out.append({"env_name": env_name or "", "provider": adapter.code,
                    "label": adapter.label})
    return out


@router.get("/secrets", response_model=SecretsOut)
def list_secrets(_=Depends(require_permission("system:config"))) -> SecretsOut:
    """云端密钥的**指纹**：能不能确认是哪一把、以及它现在有没有配。

    **只给指纹，不给值。** 指纹 = `sha256(值)` 前 8 位，推不回原值；
    `preview` 只露供应商公开的家族标记（`sk-` 这类）。长度也给——
    复制粘贴时多个换行或少几个字符，人对一下长度就能发现。
    """
    items = []
    for it in _cloud_key_items():
        fp = adapters.key_fingerprint(it["env_name"])
        items.append(SecretItem(
            env_name=it["env_name"], provider=it["provider"], label=it["label"],
            present=fp["present"], length=fp.get("length", 0),
            fingerprint=fp.get("fingerprint", ""), preview=fp.get("preview", "")))
    return SecretsOut(items=items, note=(
        "指纹是 sha256 的前 8 位，用来确认「是哪一把 key」，推不回原值。"
        "**改了密钥后 API 进程立刻看到新值（这一页会变），但 worker 不会**——"
        "它拿的是启动那一刻的环境快照，必须重启 worker 云端作业才会用新 key。"
        "若改的是 ~/.zshrc，要重启 run.sh 才能同时刷新两者。"))


@router.post("/secrets/{provider}/test", response_model=SecretProbeOut)
def test_secret(provider: str, _=Depends(require_permission("system:config"))
                ) -> SecretProbeOut:
    """用**当前进程环境里**的那把 key 发一次最小请求，回答「这把到底能不能用」。

    复用 `cloud_probe.probe_one(fresh=True)`：它已经实现了"没配密钥不发请求"
    （纪律 2）、地址闸（`safenet.check_url`＋不跟随重定向）、错误码归一
    （401/403 钥匙问题、402 欠费、429 限流）。**`fresh=True` 是关键**：
    默认有 15 秒缓存，而人刚 export 完新 key 就来点测试，拿到旧结论会以为没生效。

    注意这是**真出网**的调用（会打供应商网关）。它不做任何生成、不花钱，
    但确实是一次外部请求——所以只对 `system:config` 开放。
    """
    adapter = None
    for cap in ("text", "image", "video", "tts", "music"):
        adapter = adapters.find(cap, "cloud", provider)
        if adapter is not None:
            break
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"云端清单里没有「{provider}」这一家")
    out = cloud_probe.probe_one(adapter, fresh=True)
    return SecretProbeOut(
        provider=out["provider"], label=out["label"], state=out["state"],
        detail=out["detail"], key_env=out.get("key_env", ""),
        has_key=bool(out.get("has_key")))


# ------------------------------------------------ 密钥的密文存储（0052，2026-11-11）
#
# 用户批准实现 A 后的入口：**管理员可以在页面上配置供应商密钥**，
# 值以 AES-256-GCM 密文落库，KEK 只在环境变量 `PLATFORM_SECRET_KEK`。
#
# 三条边界（与 `secret_store.py` 文件头同一套）：
#   1. **响应里永远不含明文**。读回来只有指纹、长度、更新时间、来源。
#      "保存成功"也不回显值——回显就把它写进浏览器历史/截屏/日志。
#   2. **环境变量优先**。库里存的是"环境里没有时的值"，页面显示
#      `source: env/db/none` 让人一眼看出当前生效的是哪一份。
#   3. **KEK 缺失时功能整体不可用**，返回 409 并说清怎么生成，
#      而不是静默接受写入（那会写进一条将来解不开的行）。

def _secret_out(env_name: str) -> SecretValueOut:
    """一把密钥的**显示态**：不解密，也不含明文。"""
    env_value = (os.environ.get(env_name) or "").strip()
    row = secret_value.get_row(env_name)
    out = SecretValueOut(env_name=env_name)
    if env_value:
        # 环境里的值可以现算指纹（不落库）：这样页面能显示"环境里那把是哪一把"
        fp = adapters.key_fingerprint(env_name)
        out.present = True
        out.source = "env"
        out.fingerprint = fp.get("fingerprint", "")
        out.length = fp.get("length", 0)
        out.preview = fp.get("preview", "")
        out.note = "当前生效的是环境变量；库里的值（若有）不生效"
        return out
    if row is not None:
        out.present = True
        out.source = "db"
        out.fingerprint = row["fingerprint"]
        out.length = int(row["length"] or 0)
        out.preview = adapters._preview_for_db(row["fingerprint"])
        out.updated_at = row["updated_at"]
        out.updated_by = row["updated_by"]
        out.note = str(row["note"] or "")
    return out


@router.get("/secret-values", response_model=SecretValuesOut)
def list_secret_values(_=Depends(require_permission("system:config"))
                       ) -> SecretValuesOut:
    """每个云端密钥的显示态：有没有配、值在哪（环境/库）、指纹。

    **一个字符的明文都不返回。** 页面拿指纹去和供应商后台比对就够了。
    """
    import secret_store
    items = [_secret_out(it["env_name"]) for it in _cloud_key_items()]
    return SecretValuesOut(
        items=items,
        kek_ready=secret_store.available(),
        kek_env=secret_store.KEK_ENV,
        note=("密钥以 AES-256-GCM 密文存在库里，主密钥只在环境变量 "
              f"`{secret_store.KEK_ENV}`。**环境变量优先**：同一把密钥若在环境里"
              "也有值，以环境里的为准，库里的不生效。保存后**下一单立即生效**，"
              "不需要重启 worker（读取发生在作业执行时）。"))


@router.put("/secret-values/{env_name}", response_model=SecretValueOut)
def save_secret_value(env_name: str, payload: SecretValueIn,
                      user=Depends(require_permission("system:config"))
                      ) -> SecretValueOut:
    """保存一把密钥（**密文落库**）。收明文、存密文、回指纹，不回明文。

    KEK 没配时 409：宁可不收，也不写进一条将来解不开的行。
    """
    import secret_store
    if not secret_store.available():
        raise HTTPException(status_code=409, detail=(
            f"没有配置 {secret_store.KEK_ENV}，密钥无法加密，所以没有保存。"
            f"生成方法：python3 -c \"import base64,secrets;"
            f"print(base64.b64encode(secrets.token_bytes(32)).decode())\" "
            f"然后把结果写进环境变量并重启 API。"))
    value = (payload.value or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="密钥不能为空；要清除请用 DELETE")
    try:
        secret_value.save(env_name, value, updated_by=user["id"],
                          note="网页保存")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except secret_store.SecretStoreUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _secret_out(env_name)


@router.delete("/secret-values/{env_name}", response_model=SecretValueOut)
def delete_secret_value(env_name: str,
                        _=Depends(require_permission("system:config"))
                        ) -> SecretValueOut:
    """删掉库里的值（回落到"只认环境变量"）。删一个本来就没有的不算错误。"""
    try:
        secret_value.forget(env_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _secret_out(env_name)
