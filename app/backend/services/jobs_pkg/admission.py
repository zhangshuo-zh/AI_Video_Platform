"""作业服务 admission 域。"""
from ..errors import JobInvalid, NoRoute
from ..providers import resolve_provider
from db import get_conn
from repositories.content import shots
from repositories.engine_instances import EngineInstanceError
from repositories.providers import providers as provider_repo
from repositories.jobs import jobs as job_repo
from repositories.model_center import model_center as registration_repo
import adapters
import config
import engines
import sqlite3
from .billing import _cost_of
from .source import shot_source_version

from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class PreparedJob:
    """单次请求的预校验快照；不缓存、不对外暴露，不负责执行作业。"""

    intent: dict
    row: dict
    created_by: int | None
    existing: sqlite3.Row | None = None


def _intent_of(payload) -> dict:
    # 幂等键在分配 agent_run id 后填入，其余提交内容必须与预校验一致。
    fields = (
        "project_id", "episode_id", "shot_id", "capability", "execution_mode",
        "provider_code", "model_code", "engine_instance_id", "prompt", "params", "input_refs", "priority",
        "estimate", "user_confirmed_cloud", "rework_of_job_id",
    )
    return deepcopy({name: getattr(payload, name, None) for name in fields})




def job_kind(capability: str, execution_mode: str) -> str:
    """adapter 请求类别。视频必须区分本地/云端——同样的 capability，
    本地跑 FastMetal 与云端调 Wan API 是两条完全不同的执行路径。"""
    return f"video_{execution_mode}" if capability == "video" else capability





COMFYUI_ROUTE = ("image", "local", "comfyui", "comfyui-workflow")


def resolve_job_route(capability: str, mode: str | None, provider: str = "",
                      model: str = "", *, confirmed_cloud: bool = False,
                      engine_instance_id: int | None = None) -> dict:
    """解析默认槽位或完整显式路线，并校验登记与实例绑定。"""
    explicit = bool(provider or model)
    if explicit:
        if not (mode and provider and model):
            raise JobInvalid("执行方式、供应商、模型要么都不传（走默认轨），要么三件一起给")
        if capability == "video" and mode == "cloud" and not confirmed_cloud:
            raise JobInvalid("云端视频要逐镜头确认按供应商计费")
        adapter = adapters.find(capability, mode, provider)
        if adapter is None:
            raise JobInvalid(f"provider「{provider}」没有登记适配器，平台不会猜它的参数")
        slot = provider_repo.slot(capability, mode)
        row = {"capability": capability, "backend": mode, "provider": provider, "model": model}
        if slot is not None and slot["provider"] == provider:
            row["key_env"] = slot["key_env"]
        route = {"row": row, "adapter": adapter, "backend": mode,
                 "provider": provider, "model": model}
    else:
        try:
            route = resolve_provider(capability, mode, confirmed_cloud=confirmed_cloud)
        except NoRoute as exc:
            raise JobInvalid(str(exc)) from exc
        mode, provider, model = route["backend"], route["provider"], route["model"]
        adapter = route["adapter"]

    is_comfyui = (capability, mode, adapter.code, model) == COMFYUI_ROUTE
    registration = registration_repo.find_registration(capability, mode, provider, model)
    if registration is None and adapter.code != provider:
        registration = registration_repo.find_registration(capability, mode, adapter.code, model)
    if registration is not None and not registration["enabled"]:
        raise JobInvalid(f"{provider}/{model} 这条登记已停用（enabled=0）："
                         "请到模型仓库把它启用")
    if explicit and registration is None and not is_comfyui:
        raise JobInvalid(f"{capability} · {mode} 上没有登记 {provider}/{model}。"
                         "请先在模型中心登记它，或者走默认轨")

    instance = None
    if adapter.code in engines.SERVICE_ENGINES:
        try:
            instance = engines.resolve_instance(adapter.code, engine_instance_id)
        except EngineInstanceError as exc:
            raise JobInvalid(str(exc)) from exc
        if instance is None:
            raise JobInvalid(f"请先在「引擎实例」中配置并启用 {adapter.label.split('（', 1)[0]} 节点")
    elif engine_instance_id is not None:
        raise JobInvalid(f"供应商 {provider} 不支持绑定引擎实例")
    return {**route, "instance": instance, "is_comfyui": is_comfyui,
            "registration": registration}


def _service_engine_gate(capability: str, mode: str, provider: str, model: str,
                         instance=None) -> None:
    """第五条闸门（W-B；G1b 已裁＝甲）：服务型引擎的当前模型必须与登记行一致。

    为什么非要在这道拦：Draw Things 这类常驻服务**同一时刻只有一个当前模型**，
    而换模型只能在它的界面里点——实测 `POST /sdapi/v1/options` 回 404、
    `sd_model_checkpoint` 被 422 拒，平台没有任何编程入口。所以"作业说要跑 A 档、
    引擎其实选着 B 档"这件事只能靠提交前核对，不能靠运行时纠正。

    不符就拒绝，**不自动切、也不悄悄换成另一档引擎**（与 MC1 同一条纪律）：
    自动切等于替人决定"这一集长什么样"，而那是人该看见的一次选择。
    期望值取登记行的 `artifact_path`——那一格本来就是"这台机器上的事实"，
    对服务型引擎来说就是应用里显示的那个文件名（同 runners._mflux 把平台码
    换成 HF 仓名的思路，只是方向反过来：那里是出参，这里是核对）。
    """
    adapter = adapters.find(capability, mode, provider)
    # 只有"同一时刻只有一个当前模型"的那一小族才过这道闸门（MODEL_GATED_ENGINES）。
    # 服务型但按请求选模型的（Ollama）不在里面：核对一个不存在的概念
    # 只会得到假话或假阻断。分两个集合而不是加特判，理由写在 engines.py 那一段。
    if adapter is None or adapter.code not in engines.MODEL_GATED_ENGINES:
        return
    # 这道闸门要的是**这一刻**的答案：人可能刚在应用里换了模型，15 秒内的旧结论
    # 会让这条作业带着"选中的是 A、引擎跑的是 B"进队列——那正是它存在的原因。
    # 一次提交只问一次，5 秒上限在这里是等得起的（页面那条路有 0.8 秒的列表档）。
    if instance is None:
        status = engines.probe(adapter.code, fresh=True)
    else:
        status = engines.probe(adapter.code, instance["endpoint"], fresh=True, instance=instance)
    registration = registration_repo.find_registration(capability, mode, adapter.code, model)
    check_current_engine_model(adapter, registration, status)


def check_current_engine_model(adapter, registration, status: engines.EngineStatus | None) -> None:
    if status is not None and status.state != "ok":
        raise JobInvalid(f"{adapter.label} 现在不能用：{status.detail}")
    expected = (registration["artifact_path"].strip()
                if registration is not None else "")
    live = (status.values.get("model") or "").strip() if status is not None else ""
    if not expected:
        raise JobInvalid(
            f"这一档引擎（{adapter.label}）的登记没填「权重路径」，"
            f"平台无法核对引擎当前选的是哪一档（现在自述为 {live or '读不到'}）。"
            "请在模型中心把应用里显示的那个文件名填进登记行的权重路径")
    if status is not None and live.lower() != expected.lower():
        raise JobInvalid(
            f"引擎当前选中的是 {live or '（读不到）'}，这条作业登记的是 {expected}。"
            f"平台不代切模型：请在 {adapter.label} 里选中 {expected} 再提交，"
            "或把这条登记的权重路径改成你实际要用的那一档")





def verify_job_links(project_id: int, episode_id: int | None, shot_id: int | None) -> None:
    """集与镜头必须真的属于这个项目；两者同时给时必须互相对得上。

    这不是权限问题（项目那一道已经过了），是输入自相矛盾，所以交由调用方回 422。
    """
    with get_conn() as conn:
        if episode_id is not None:
            ep = conn.execute("SELECT project_id FROM episode WHERE id = ?",
                              (episode_id,)).fetchone()
            if ep is None or ep["project_id"] != project_id:
                raise JobInvalid("这一集不属于该项目")
        if shot_id is not None:
            shot = conn.execute(
                """SELECT e.project_id, e.id AS episode_id FROM shot s
                     JOIN episode e ON e.id = s.episode_id WHERE s.id = ?""",
                (shot_id,)).fetchone()
            if shot is None or shot["project_id"] != project_id:
                raise JobInvalid("这个镜头不属于该项目")
            if episode_id is not None and shot["episode_id"] != episode_id:
                raise JobInvalid("这个镜头不在指定的那一集里")



def prepare_job(payload, created_by: int | None) -> PreparedJob:
    """解析路线、校验参数与成本，返回本次提交快照，不写入 Job。

    引擎握手、分镜源指纹等外部读取在这里完成。需要同时写入业务关联的
    调用方在事务外准备，再把结果交给事务内的 submit_job；不缓存或跨请求复用。
    幂等查键保留原有项目归属校验，发布阶段再次查键并处理唯一约束竞态。

    通用提交仍统一经过 submit_job；文档 AI、抽取、合成的专用建单路径
    由 test_guards 的独立清单约束，不在此新增建单旁路。
    """
    if payload.idempotency_key:
        existing = job_repo.by_key(payload.idempotency_key)
        if existing is not None:
            # 键撞上了但项目不同＝有人把别人的键拿来用了。不能把那条作业回给他，
            # 连 id 都不能回——那等于通过幂等键泄露了别的项目的作业号。
            if existing["project_id"] != payload.project_id:
                raise JobInvalid("这个幂等键已被别的作业占用")
            return PreparedJob(_intent_of(payload), {}, created_by, existing)

    rework_id = getattr(payload, "rework_of_job_id", None)
    if rework_id is not None:
        source = job_repo.get(rework_id)
        if (source is None or payload.shot_id is None
                or payload.capability not in ("image", "video")
                or source["status"] != "succeeded"
                or source["project_id"] != payload.project_id
                or source["shot_id"] != payload.shot_id
                or source["capability"] != payload.capability):
            raise JobInvalid("返工来源必须是同一镜头、同一能力的成功作业")

    capability = payload.capability
    mode = payload.execution_mode
    provider = (payload.provider_code or "").strip()
    model = (payload.model_code or "").strip()
    explicit = bool(mode or provider or model)
    adapter_version = adapters.MANIFEST_VERSION
    requested_instance_id = getattr(payload, "engine_instance_id", None)
    instance = None

    if provider == "mock":
        # 测试/演示专用：不进清单、不执行命令，只在 ALLOW_MOCK 打开时可用
        # 走 `config.ALLOW_MOCK`（模块属性）而不是 `from config import ALLOW_MOCK`
        # 的局部副本：2026-11-08 拆包后，测试要
        # `monkeypatch.setattr("services.ALLOW_MOCK", False)` 把它关掉，
        # 局部副本改不动——那是拆包引入的语义差异，改成模块属性读就没有了。
        if not config.ALLOW_MOCK:
            raise JobInvalid("假适配器没有启用（FASTVIDEO_ALLOW_MOCK=1 才可用）")
        if mode not in (None, "local"):
            raise JobInvalid("假适配器只能走本地")
        if model != adapters.MOCK_MODEL:
            raise JobInvalid(f"假适配器的模型档只有 {adapters.MOCK_MODEL}")
        errors = adapters.validate_mock_params(payload.params)
        if errors:
            raise JobInvalid("；".join(errors))
        mode = "local"
        adapter_version = "mock"
    elif provider == "ffmpeg":
        if mode not in (None, "local"):
            raise JobInvalid("FFmpeg 只能走本地执行")
        if capability != "video" or model != "ffmpeg-lipsync-1":
            raise JobInvalid("FFmpeg 仅支持 video/ffmpeg-lipsync-1 对口型任务")
        if str(payload.params.get("operation") or "") != "lip_sync" or not str(payload.params.get("audio_path") or "").strip():
            raise JobInvalid("对口型任务缺少 operation=lip_sync 或 audio_path")
        mode = "local"
        adapter_version = "ffmpeg"
    else:
        if explicit and not (mode and provider and model):
            raise JobInvalid("执行方式、供应商、模型要么都不传（走默认轨），要么三件一起给")
        route = resolve_job_route(capability, mode, provider, model,
                                  confirmed_cloud=payload.user_confirmed_cloud,
                                  engine_instance_id=requested_instance_id)
        provider, model, mode = route["provider"], route["model"], route["backend"]
        instance = route["instance"]
        errors = adapters.validate_params(route["adapter"], model, payload.params)
        if errors:
            raise JobInvalid("；".join(errors))

    if requested_instance_id is not None and instance is None:
        raise JobInvalid(f"供应商 {provider} 不支持绑定引擎实例")
    _service_engine_gate(capability, mode, provider, model, instance)

    cost = _cost_of(payload, mode, created_by)

    # 分镜作业记下"提交那一刻的源内容指纹"：改完剧本或资产之后，
    # 成片／产线拿它和当前指纹一比就知道这条产物是不是过期了。
    source_version = ""
    if payload.shot_id:
        shot = shots.get(int(payload.shot_id))
        if shot is not None:
            source_version = shot_source_version(shot)

    row = {
        "project_id": payload.project_id,
        "episode_id": payload.episode_id,
        "shot_id": payload.shot_id,
        "capability": capability,
        "kind": job_kind(capability, mode),
        "execution_mode": mode,
        "provider_code": provider,
        "model_code": model,
        "engine_instance_id": instance["id"] if instance is not None else None,
        "adapter_version": adapter_version,
        "prompt_snapshot": payload.prompt,
        "params": payload.params,
        "input_refs": payload.input_refs,
        "rework_of_job_id": rework_id,
        "priority": payload.priority,
        "idempotency_key": payload.idempotency_key,
        "user_confirmed_cloud": payload.user_confirmed_cloud,
        "source_version": source_version,
        "created_by": created_by,
        **cost,
    }
    return PreparedJob(_intent_of(payload), deepcopy(row), created_by)


def submit_job(payload, created_by: int | None, *, prepared: PreparedJob | None = None) -> sqlite3.Row:
    """唯一通用建单入口；预校验结果可在调用方的原子写事务内使用。"""
    if prepared is None:
        prepared = prepare_job(payload, created_by)
    elif payload.idempotency_key:
        existing = job_repo.by_key(payload.idempotency_key)
        if existing is not None:
            if existing["project_id"] != payload.project_id:
                raise JobInvalid("这个幂等键已被别的作业占用")
            return existing
    if prepared.created_by != created_by or prepared.intent != _intent_of(payload):
        raise JobInvalid("作业内容在预校验后发生变化，请重新校验")
    if prepared.existing is not None:
        if prepared.existing["idempotency_key"] != payload.idempotency_key:
            raise JobInvalid("幂等键在预校验后发生变化，请重新校验")
        return prepared.existing
    row = deepcopy(prepared.row)
    row["idempotency_key"] = payload.idempotency_key
    try:
        return job_repo.create(row)
    except sqlite3.IntegrityError as exc:
        # 竞态兜底：同一个幂等键的两条请求同时走到这里，后插入的那条撞
        # `job.idempotency_key` 的 UNIQUE 约束。撞约束不是故障，是"另一边已经建好了"
        # ——把那一条读回来当本次结果，用户看到的是同一个作业号，而不是 500。
        # 没带键时不进这一支：NULL 在 UNIQUE 里互不相等，真撞上了就是别的约束，
        # 那种情况必须照实抛出去，不能拿"幂等"当遮布。
        existing = job_repo.by_key(payload.idempotency_key) if payload.idempotency_key else None
        if existing is None:
            raise exc
        if existing["project_id"] != payload.project_id:
            raise JobInvalid("这个幂等键已被别的作业占用") from exc
        return existing
