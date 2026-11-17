"""模型中心登记与引擎操作业务；不依赖 HTTP 框架。"""

import json
import time
import adapters
import engine_launch
import engines
import model_center as mc
import validations
from models import (CAPABILITIES, EngineInstanceIn, EngineInstancePatch,
                    ModelRegistrationIn, ModelRegistrationPatch,
                    REG_SOURCES,
                    ValidationOut)
from repositories.config_log import record as config_log_record
from repositories.engine_instances import (SUPPORTED_CONNECTION_TYPES,
                                           EngineInstanceError, engine_instances,
                                           engine_probe_log)
from repositories.model_center import RegistrationError, model_center

from .model_configuration import ModelOperationError, _item_of, _age_seconds, _adapter_for


_CONFIRM_WORDS = frozenset({"1", "true", "yes"})
_VERIFIABLE_BACKENDS = tuple(REG_SOURCES)


def _confirmed(raw: str) -> bool:
    return (raw or "").strip().lower() in _CONFIRM_WORDS


def _row_or_404(reg_id: int):
    row = model_center.registration(reg_id)
    if row is None:
        raise ModelOperationError(status_code=404, detail=f"没有这条登记：#{reg_id}")
    return row


def create_model(payload: ModelRegistrationIn, user) -> dict:
    capability = payload.capability.strip()
    backend = payload.backend.strip()
    provider = payload.provider.strip()
    model_code = payload.model_code.strip()
    if capability not in CAPABILITIES:
        raise ModelOperationError(status_code=422,
                            detail=f"能力只能是 {'、'.join(CAPABILITIES)}，不是「{capability}」")
    if backend not in REG_SOURCES:
        raise ModelOperationError(status_code=422,
                            detail=f"运行位置只能是 {'、'.join(REG_SOURCES.values())}")
    if not provider or not model_code:
        raise ModelOperationError(status_code=422, detail="provider 与模型标识都不能空")

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
        raise ModelOperationError(status_code=422, detail=str(exc)) from exc
    return {"id": row["id"], "item": _item_of(row["id"]),
            "in_manifest": adapter is not None,
            "warning": "" if adapter is not None else
            "清单里没有这个 provider：登记留下了，但参数不猜、运行方式不猜，提交侧会被拒"}


def patch_model(reg_id: int, payload: ModelRegistrationPatch) -> dict:
    _row_or_404(reg_id)
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "source" in patch and patch["source"] not in REG_SOURCES:
        raise ModelOperationError(status_code=422,
                            detail=f"运行位置只能是 {'、'.join(REG_SOURCES.values())}")
    try:
        model_center.update(reg_id, patch)
    except RegistrationError as exc:
        raise ModelOperationError(status_code=422, detail=str(exc)) from exc
    return {"item": _item_of(reg_id)}


def remove_model(reg_id: int, confirm: str, user) -> dict:
    """撤登记。**不删权重文件、不动历史作业、不清验证记录**。

    要 confirm 是因为人常把"移除登记"理解成"删掉模型"；
    提示语里必须把"文件还在盘上"说明白，否则下次找权重就找不着了。
    """
    row = _row_or_404(reg_id)
    if not _confirmed(confirm):
        raise ModelOperationError(
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


def check_model(reg_id: int, user):
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
        raise ModelOperationError(
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
        raise ModelOperationError(status_code=422, detail=str(exc)) from exc
    return ValidationOut.from_row(result["validation"])


def list_config_changes(limit: int) -> dict:
    """选路配置的变更痕（0044）。最近改了什么：谁、什么时候、从什么改成什么。

    只读。这是"最小变更痕"的出口，不是通用审计——业务表大部分写路径
    仍然没有痕（v0.87 裁定）；选路配置沿用原写点，0057 补入 Worker
    并发与守护配置，不承载 Job 队列或任何配置现值。
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


def resolve(scene_key: str) -> dict[str, object]:
    """场景 → 候选与挡路原因。只读，登录即可（与 /providers/available 同一档）。"""
    from model_center_pkg.labels import PolicyDataInvalid
    try:
        out = mc.resolve_scene(scene_key)
    except PolicyDataInvalid as exc:
        raise ModelOperationError(500, str(exc)) from exc
    # 跟着清单里的策略走，而不是在这里写死一个字面量：今天没人自动切是因为
    # `no_auto_fallback` 为真，将来谁把那条策略改了，这一句必须跟着变——
    # 界面据此说"要不要替你顶上"，说错比不说更糟。
    out["auto_switch"] = not adapters.POLICY["no_auto_fallback"]
    return out


def create_engine(payload: EngineInstanceIn, user) -> dict:
    code = payload.engine_code.strip()
    if _adapter_for(code) is None:
        raise ModelOperationError(
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
        raise ModelOperationError(status_code=422, detail=str(exc)) from exc
    return {"id": row["id"], "warning": _probe_warning(row, user["id"])}


def patch_engine(instance_id: int, payload: EngineInstancePatch) -> dict:
    if engine_instances.instance(instance_id) is None:
        raise ModelOperationError(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        row = engine_instances.update(instance_id, patch)
    except EngineInstanceError as exc:
        raise ModelOperationError(status_code=422, detail=str(exc)) from exc
    return {"id": row["id"], "enabled": bool(row["enabled"])}


def remove_engine(instance_id: int, confirm: str) -> dict:
    """撤实例。**不动应用本身、不动历史作业**。"""
    row = engine_instances.instance(instance_id)
    if row is None:
        raise ModelOperationError(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
    if not _confirmed(confirm):
        raise ModelOperationError(
            status_code=422,
            detail="要撤实例请带 confirm=1（或 confirm=true／yes）。这一步只删除这条登记："
                   "应用与它的模型都不受影响，历史作业仍按当时冻结的快照可查。")
    engine_instances.delete(row["id"])
    return {"removed": row["id"],
            "note": "只撤了端点登记；应用、权重与 job 历史一律没碰"}


def list_engine_probes(instance_id: int, limit: int) -> dict:
    """这条实例的探测历史，最新在前。**只读，不触发探测**。

    有这一条的理由：卡面上那一行只说"最近一条"。要回答"这台是不是三天两头掉线"，
    得看几条；而历史只追加不改，倒序按 id 天然成立（不需要 OFFSET，那张表的
    时间列精度只到秒，同一秒里探两次按时间排会让顺序变成猜的）。
    """
    row = engine_instances.instance(instance_id)
    if row is None:
        raise ModelOperationError(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
    items = [dict(r) for r in engine_probe_log.recent(instance_id, limit)]
    return {"items": [{**item, "age_seconds": _age_seconds(item["created_at"])}
                      for item in items],
            "note": "只追加不改：这条历史只回答「什么时候探过、当时如何」，"
                    "不参与任何判断——现在通不通仍由探测现查"}


def probe_engine(instance_id: int, user) -> dict:
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
        raise ModelOperationError(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
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


def launch_engine(instance_id: int, user) -> dict:
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

    row = engine_instances.instance(instance_id)
    if row is None:
        raise ModelOperationError(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
    if row["connection_type"] not in SUPPORTED_CONNECTION_TYPES:
        raise ModelOperationError(
            status_code=422,
            detail="这一档连接方式不是本机 HTTP 服务，平台不会按启动配方拉它")

    result = engine_launch.launch(row["engine_code"], row["endpoint"])
    if not result.ok and not result.started and not result.reachable:
        # 三次明确拒绝（没配方 / 没装 / Popen 抛错）都落在这里；已经在跑与
        # 「起了但没答话」不算失败请求——那是如实结论，界面要能原样显示。
        raise ModelOperationError(status_code=409, detail=result.message)

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


def stop_engine(instance_id: int, user) -> dict:
    """按内置配方优雅关闭这一台本机服务。

    通过 AppleScript 或基于端口号安全定位进程 PID 并向其发送退出信号，
    绝不误杀无关进程。
    """

    row = engine_instances.instance(instance_id)
    if row is None:
        raise ModelOperationError(status_code=404, detail=f"没有这条引擎实例：#{instance_id}")
    if row["connection_type"] not in SUPPORTED_CONNECTION_TYPES:
        raise ModelOperationError(
            status_code=422,
            detail="这一档连接方式不是本机 HTTP 服务，平台不会按控制配方关闭它")

    result = engine_launch.stop(row["engine_code"], row["endpoint"])
    if not result.ok:
        raise ModelOperationError(status_code=409, detail=result.message)

    return {"id": row["id"], "engine_code": row["engine_code"],
            "stopped": result.stopped, "message": result.message}


def _probe_warning(row, user_id: int | None=None) -> str:
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
