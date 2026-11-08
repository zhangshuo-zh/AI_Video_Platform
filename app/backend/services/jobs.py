import adapters
import engines
import hashlib
import jobfiles
import json
import sqlite3

import config
from config import LOG_SILENCE_SECONDS, QUEUE_STALL_SECONDS, WORKER_MAX_RUNTIME_SECONDS, WORKER_ONLINE_SECONDS
from datetime import datetime
from db import get_conn
from decimal import Decimal, ROUND_HALF_UP
from models import JOB_STATUSES
from repositories.content import assets, shots, variants
from repositories.jobs import jobs as job_repo
from repositories.model_center import model_center as registration_repo

from .errors import JobInvalid, NoRoute
from .providers import resolve_provider


def job_kind(capability: str, execution_mode: str) -> str:
    """adapter 请求类别。视频必须区分本地/云端——同样的 capability，
    本地跑 FastMetal 与云端调 Wan API 是两条完全不同的执行路径。"""
    return f"video_{execution_mode}" if capability == "video" else capability




def shot_source_version(shot, loaded_assets: dict | None = None,
                        loaded_variants: dict | None = None) -> str:
    """这一镜"源内容"的指纹：内容、提示词、时长、引用资产及资产的版本信息。

    为什么不覆盖模型与参数：换模型不改变"这一镜讲什么、引用谁"，那是 Job 参数
    快照与成本账要回答的问题；把模型塞进指纹会让"换条便宜的轨"变成"素材过期"，
    反而逼人重跑一遍已经能用的片子。

    资产那边用 `updated_at`＋一致性提示词＋参考图清单：参考图被换掉、定妆描述被改，
    都会让这一镜的画面依据变掉，旧视频就不该还挂着"已签"。

    0034 起再叠一层**形态**（这一镜用的是哪套衣服）：形态的描述与图同样是画面依据，
    改了形态而不让旧产物过期，等于承认"改衣服不用重跑"——那正是任务书 §6.3
    要防的"仅修改服装却意外改变人物身份"里的前半段。

    `loaded_assets`／`loaded_variants` 是给"一集几十镜一起算"的调用方留的口子
    （分镜列表、生产前检查）：它们本来就要把这一集的资产读一遍，逐镜再 `get()`
    会把一次列表打成几十次回库。传了就只查这一份字典，没传就自己回库。
    """
    try:
        ids = [int(x) for x in json.loads(shot["asset_ids"] or "[]")]
    except (TypeError, ValueError):
        ids = []
    try:
        # 老库读出来的行可能还没有这一列（0034 未跑到的测试夹具）：按空清单处理，
        # 不抛——指纹要能算，"没有形态引用"本来就是空清单的意思。
        vids = ([int(x) for x in json.loads(shot["variant_ids"] or "[]")]
                if "variant_ids" in shot.keys() else [])
    except (TypeError, ValueError):
        vids = []
    parts = {
        "summary": shot["summary"],
        "prompt": shot["prompt"],
        "duration": round(float(shot["duration"] or 0), 3),
        "assets": [],
        "variants": [],
    }
    if ids and loaded_assets is None:
        loaded_assets = assets.get_many(ids)
    for asset_id in ids:
        row = (loaded_assets or {}).get(asset_id)
        parts["assets"].append({
            "id": asset_id,
            "updated_at": row["updated_at"] if row else "",
            "consistency_prompt": row["consistency_prompt"] if row else "",
            "ref_images": row["ref_images"] if row else "",
        })
    if vids and loaded_variants is None:
        loaded_variants = variants.get_many(vids)
    for variant_id in vids:
        row = (loaded_variants or {}).get(variant_id)
        parts["variants"].append({
            "id": variant_id,
            "updated_at": row["updated_at"] if row else "",
            "prompt": row["prompt"] if row else "",
            "ref_images": row["ref_images"] if row else "",
        })
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]




def shots_view(episode_id: int) -> list[dict]:
    """分镜列表 + 每镜「成片会用到的那条视频作业是否基于旧源内容」。

    标记放在服务端算：前端拿不到分镜源指纹（它需要读引用资产的版本信息），
    让浏览器自己判断等于把口径复制一份到 JS 里。

    回库次数是算过的：作业一条、分镜一条、这一集引用到的资产一条，共三次。
    逐镜 `shots.get()` ＋逐资产 `assets.get()` 的老写法在 13 镜一集上是 20+ 次，
    而创作中心每次打开一集都要走这条路。
    """
    rows = shots.list_by_episode(episode_id)
    latest = job_repo.latest_succeeded_videos_by_shot(episode_id)
    asset_ids: set[int] = set()
    variant_ids: set[int] = set()
    for row in rows:
        if int(row["id"]) not in latest:
            continue                      # 没有可用视频作业的镜头不判过期，省掉一批资产读
        asset_ids |= shot_asset_ids(row)
        variant_ids |= shot_variant_ids(row)
    loaded = assets.get_many(sorted(asset_ids)) if asset_ids else {}
    loaded_variants = variants.get_many(sorted(variant_ids)) if variant_ids else {}
    out = []
    for row in rows:
        job = latest.get(int(row["id"]))
        stale = bool(job and job_source_stale(
            job, shot=row, loaded_assets=loaded, loaded_variants=loaded_variants))
        out.append({"shot": row, "source_stale": stale})
    return out




def shot_asset_ids(shot) -> set[int]:
    """镜头行里的 `asset_ids`（JSON 文本）→ 整数集合。坏数据当空清单。"""
    try:
        return {int(x) for x in json.loads(shot["asset_ids"] or "[]")}
    except (TypeError, ValueError):
        return set()




def shot_variant_ids(shot) -> set[int]:
    """同上，读 `variant_ids`（0034）。列还不存在时当空清单，不抛。"""
    try:
        if "variant_ids" not in shot.keys():
            return set()
        return {int(x) for x in json.loads(shot["variant_ids"] or "[]")}
    except (TypeError, ValueError):
        return set()




def job_source_stale(job, shot=None, loaded_assets: dict | None = None,
                     loaded_variants: dict | None = None) -> bool:
    """这条作业的产物是不是基于**旧版**分镜／资产生的。

    没有 `source_version` 的作业（0031 之前落的）一律不判过期：那时候平台确实
    没记过源内容，硬判成过期会把历史作业全打成废纸，也会让已经签过字的片子凭空失效。

    **0034 加形态列之后，旧作业的指纹仍然对不上**——那不是 bug，是事实：
    那些产物确实是在"镜头没有形态引用"的世界里生的，而这一镜现在指到了某个形态上，
    画面依据变了。要保住旧产物就得保住它当时的形态引用，而当时没记（记在
    `source_version` 那个哈希里，反推不出来）。所以这里照实标过期，
    由人决定重跑还是接受，而不是替平台编一个"应该还是可用的"。

    `shot`／`loaded_assets`／`loaded_variants` 是同一次列表里已经在手的行；
    不传就自己回库（单条检查那条路）。
    """
    keys = job.keys()
    version = job["source_version"] if "source_version" in keys else ""
    if not version:
        return False
    shot_id = job["shot_id"] if "shot_id" in keys else None
    if not shot_id:
        return False
    if shot is None:
        shot = shots.get(int(shot_id))
    if shot is None:
        return False
    return shot_source_version(shot, loaded_assets, loaded_variants) != version




def _service_engine_gate(capability: str, mode: str, provider: str, model: str) -> None:
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
    status = engines.probe(adapter.code, fresh=True)
    if status.state != "ok":
        raise JobInvalid(f"{adapter.label} 现在不能用：{status.detail}")
    registration = registration_repo.find_registration(capability, mode,
                                                       adapter.code, model)
    expected = (registration["artifact_path"].strip()
                if registration is not None else "")
    live = (status.values.get("model") or "").strip()
    if not expected:
        raise JobInvalid(
            f"这一档引擎（{adapter.label}）的登记没填「权重路径」，"
            f"平台无法核对引擎当前选的是哪一档（现在自述为 {live or '读不到'}）。"
            "请在模型中心把应用里显示的那个文件名填进登记行的权重路径")
    if live.lower() != expected.lower():
        raise JobInvalid(
            f"引擎当前选中的是 {live or '（读不到）'}，这条作业登记的是 {expected}。"
            f"平台不代切模型：请在 {adapter.label} 里选中 {expected} 再提交，"
            "或把这条登记的权重路径改成你实际要用的那一档")




def submit_job(payload, created_by: int | None) -> dict:
    """校验并写入 Job 行，返回 sqlite3.Row。**不触发执行**——执行是 worker 的事。

    三条闸门在这里一次性过完：路线（默认轨或显式槽位）、参数白名单、
    云端视频的人工确认。任何一条不过就抛 JobInvalid，绝不落一条"看起来能跑"的空作业。

    第四条是**幂等复用**，收在这一层而不是只收在 `POST /api/jobs` 里：
    那条路由查过键（还要查权限、要把 202 改成 200），但资产中心这些
    经由本函数建单的路径如果各自再查一遍，就是第四、第五份判断。现在只有一个
    出处，代价是任何调用方都白拿同一条语义。

    走本函数的四条业务提交路：生成中心（`routers/jobs.py`）、资产中心
    （`asset_studio._submit_job`）、单集产线的节点（`pipeline._dispatch_ready`）、
    音频中心（本文件 `audio_submit`）。

    **另有三条旁路直接 `job_repo.create`，别把它们也说成"唯一入口"**（上一版这一句
    写的是"资产中心、文档 AI 这些经由本函数建单"，文档 AI 其实不走，读代码的人会被
    带偏）：文档 AI（本文件 `submit_doc_ai_job`）、抽取（`extraction_submit`）、
    合成（`composition/service.py`）。三条各有自己的闸门，但都不是全套：
    抽取带 25 小时同文复用、合成带幂等键与事务、**文档 AI 两样都没有**——
    这是已知开口，记在 `tests/test_guards.py::test_建单路径只有这一份清单` 的注释里。
    为什么不一并收进本函数：这三条要冻的东西不一样（选区与 base_version、
    抽取 run 行、时间线快照），且 route 走的是 `text_ai_route()` 而不是默认轨，
    硬塞进来会让"唯一入口"变成"什么都塞"。守卫测试钉的是**旁路数量不许再长**。
    """
    if payload.idempotency_key:
        existing = job_repo.by_key(payload.idempotency_key)
        if existing is not None:
            # 键撞上了但项目不同＝有人把别人的键拿来用了。不能把那条作业回给他，
            # 连 id 都不能回——那等于通过幂等键泄露了别的项目的作业号。
            if existing["project_id"] != payload.project_id:
                raise JobInvalid("这个幂等键已被别的作业占用")
            return existing

    capability = payload.capability
    mode = payload.execution_mode
    provider = (payload.provider_code or "").strip()
    model = (payload.model_code or "").strip()
    explicit = bool(mode or provider or model)
    adapter_version = adapters.MANIFEST_VERSION

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
    elif explicit:
        if not (mode and provider and model):
            raise JobInvalid("执行方式、供应商、模型要么都不传（走默认轨），要么三件一起给")
        # 显式那一支不再要求"与 provider_config 槽位逐字相等"（2026-11-07 放开）。
        #
        # 原来这条要求把"同能力只能用一档"钉死在了提交契约上：`provider_config`
        # 是十行固定（5 类能力 × 本地／云端），一格只装一个 provider，所以
        # Draw Things 与 mflux 只能二选一——想对比两种采样器就得改默认轨再改回来，
        # 而改默认轨是有后果的动作（`is_default` 是 `resolve_provider()` 的唯一输入）。
        #
        # 现在改成"**已登记 ∧ 已启用 ∧ 清单有适配器**"就放行，三件事各有出处：
        #   * 已登记：`model_registration` 那一行在——这是"这台机器上真有这一档"；
        #   * 已启用：登记行的 `enabled`——人可以用它把某一档停掉而不撤登记；
        #   * 清单有适配器：参数白名单在 `adapters.py`，没有它平台不会拼命令。
        # 槽位仍然决定**不说的时候**走哪条（默认轨唯一来源没变），只是不再
        # 兼任"允许清单"。`resolve_provider()` 一个字没改。
        reg = registration_repo.find_registration(capability, mode, provider, model)
        if reg is None:
            raise JobInvalid(
                f"{capability} · {mode} 上没有登记 {provider}/{model}。"
                "显式指定只接受「模型仓库」里登记过的档——"
                "先在模型中心登记它，或者不传这三件走默认轨")
        if not reg["enabled"]:
            raise JobInvalid(
                f"{provider}/{model} 这条登记已停用（enabled=0）："
                "显式指定也走不了停用的档，请到模型仓库把它启用")
        if capability == "video" and mode == "cloud" and not payload.user_confirmed_cloud:
            raise JobInvalid("云端视频要逐镜头确认按供应商计费")
        adapter = adapters.find(capability, mode, provider)
        if adapter is None:
            raise JobInvalid(f"provider「{provider}」没有登记适配器，平台不会猜它的参数")
        errors = adapters.validate_params(adapter, model, payload.params)
        if errors:
            raise JobInvalid("；".join(errors))
    else:
        try:
            got = resolve_provider(capability, confirmed_cloud=payload.user_confirmed_cloud)
        except NoRoute as exc:
            raise JobInvalid(str(exc)) from exc
        row = got["row"]
        provider, model, mode = row["provider"], row["model"], row["backend"]
        errors = adapters.validate_params(got["adapter"], model, payload.params)
        if errors:
            raise JobInvalid("；".join(errors))

    # 闸门走的是**两条路都要过**：显式选择与默认轨解析之后都可能是服务型引擎，
    # 只挂在 explicit 那一支会留一条"默认轨指到 Draw Things 就没人核对"的缝。
    _service_engine_gate(capability, mode, provider, model)

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
        "adapter_version": adapter_version,
        "prompt_snapshot": payload.prompt,
        "params": payload.params,
        "input_refs": payload.input_refs,
        "priority": payload.priority,
        "idempotency_key": payload.idempotency_key,
        "user_confirmed_cloud": payload.user_confirmed_cloud,
        "source_version": source_version,
        "created_by": created_by,
        **cost,
    }
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
        return existing




def _cost_of(payload, mode: str, created_by: int | None) -> dict:
    """把"预计成本确认单"变成 Job 的成本列。

    三条口径，任何一条不成立都不落库：
    1. 云端必须带确认单、必须勾确认。确认不是走形式——它把"按供应商计费"
       这句话和一组具体的量 × 单价绑在一起，事后能指着这一行问"当时是谁点的、按什么价"。
    2. 金额由服务端用 Decimal 算，四舍五入到整数 micros；客户端传什么总价都不采信。
    3. 本地作业不计费。带确认单说明调用方把两条轨搞混了，直接拒掉，
       比默默忽略、等到有人拿这条账去对预算更早暴露问题。
    """
    estimate = payload.estimate
    if mode == "cloud":
        if not payload.user_confirmed_cloud:
            raise JobInvalid("云端作业要先确认按供应商计费的预计成本")
        if estimate is None:
            raise JobInvalid("云端作业必须带预计成本（数量、单位、单价、币种与价格来源）")
        quantity = estimate.quantity
        cost_micros = int((quantity * Decimal(estimate.unit_price_micros))
                          .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        snapshot = {
            "quantity": str(quantity),
            "unit": estimate.unit,
            "unit_price_micros": estimate.unit_price_micros,
            "currency": estimate.currency,
            "source": estimate.source.strip(),
            "cost_micros": cost_micros,
            "formula": "quantity * unit_price_micros, half-up to integer micros",
            "confirmed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed_by": created_by,
            "billable": "pending_reconciliation",
        }
        return {"quantity": float(quantity), "unit": estimate.unit,
                "unit_price_micros": estimate.unit_price_micros,
                "cost_micros": cost_micros, "currency": estimate.currency,
                "pricing_snapshot": snapshot,
                "billable_status": "pending_reconciliation"}
    if estimate is not None:
        raise JobInvalid("本地作业不计费，不要提交预计成本；云端作业请显式指定 execution_mode")
    return {"quantity": None, "unit": None, "unit_price_micros": None,
            "cost_micros": 0, "currency": None, "pricing_snapshot": None,
            "billable_status": "not_billable"}


# ---------- P0：队列健康、人工核对与重试可见性 ----------

class JobMissing(Exception):
    """作业不存在或不属于可见项目。路由层转 404。"""




class JobStateConflict(Exception):
    """当前状态不允许这个动作（比如给一条 queued 做账单核对）。转 409。"""




def _seconds_since(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return max(0, int((datetime.now()
                           - datetime.strptime(text, "%Y-%m-%d %H:%M:%S")).total_seconds()))
    except ValueError:
        return None




def worker_status(user=None) -> dict:
    """worker 状态 + 队列健康。

    这里刻意**不**把"超时"写成 failed：判定与处置分开。
    界面看到告警，处置仍然是人取消或 worker 自己的 deadline 生效。

    给了 user 就把"正在跑哪条作业"的明细按可见范围裁一遍：顶栏那个小圆点是
    每人都在拉的，别人项目的 job id／供应商／模型不该顺着它漏出去。
    计数（队列长度、待核对数）不裁——那是共享机器的客观状态，不是项目内容。
    """
    state = job_repo.worker_state()
    health = job_repo.queue_health(WORKER_MAX_RUNTIME_SECONDS)
    nodes = job_repo.workers(WORKER_ONLINE_SECONDS)
    by_resource = job_repo.queue_by_resource()
    if user is not None:
        from access import ensure as _ensure
        from fastapi import HTTPException as _HTTPException
        visible = []
        for row in health["running_jobs"]:
            try:
                _ensure(user, row["project_id"], "read")
                visible.append(row)
            except _HTTPException:
                continue        # 看不见的项目连"它存在"都不说
        health = {**health, "running_jobs": visible}
    age = None
    online = False
    heartbeat = ""
    current_job_id = None
    live = [w for w in nodes if w["online"]]
    if live:
        online = True
        heartbeat = live[0]["heartbeat_at"] or ""
        age = live[0]["age_seconds"]
        current_job_id = next((w["current_job_id"] for w in live
                               if w["current_job_id"]), None)
    elif state is not None:
        # 兼容只写过单行 worker_state 的旧库／旧进程：没有节点明细时退回单行判断
        heartbeat = state["heartbeat_at"] or ""
        age = _seconds_since(heartbeat)
        online = age is not None and age <= WORKER_ONLINE_SECONDS
        current_job_id = state["current_job_id"] if online else None

    alerts: list[dict] = []
    if health["queued"] > 0 and not online:
        alerts.append({"level": "bad", "code": "worker_offline",
                       "text": f"worker 离线，队列里还有 {health['queued']} 条在等。"
                               f"启动：./run.sh worker"})
    if health["queued"] > 0 and online and (health["oldest_queued_seconds"] or 0) > QUEUE_STALL_SECONDS:
        alerts.append({"level": "warn", "code": "queue_stalled",
                       "text": f"worker 在线，但最长一条已排队 "
                               f"{health['oldest_queued_seconds']} 秒还没被领取。"
                               f"多半是上一条占着本地槽位。"})
    if health["expired_leases"] > 0:
        alerts.append({"level": "bad", "code": "lease_expired",
                       "text": f"{health['expired_leases']} 条作业的租约已过期但还挂着执行中；"
                               f"worker 下一轮恢复会把它们收掉。"})
    for running in health["running_jobs"]:
        if running["over_runtime"]:
            alerts.append({"level": "bad", "code": "run_timeout",
                           "text": f"作业 #{running['id']} 已跑 "
                                   f"{running['started_seconds']} 秒，超过上限 "
                                   f"{WORKER_MAX_RUNTIME_SECONDS} 秒。"})
        if running["silent_seconds"] is not None and running["silent_seconds"] > LOG_SILENCE_SECONDS:
            alerts.append({"level": "warn", "code": "log_silent",
                           "text": f"作业 #{running['id']} 已经 "
                                   f"{running['silent_seconds']} 秒没有新日志。"})
    if health["needs_attention"] > 0:
        alerts.append({"level": "warn", "code": "needs_attention",
                       "text": f"{health['needs_attention']} 条云端作业待人工核对；"
                               f"核对前不要重试，可能重复计费。"})

    return {
        "online": online, "heartbeat_at": heartbeat, "age_seconds": age,
        "current_job_id": current_job_id,
        "workers": nodes,
        # 两个计数由这里数，不让界面自己 filter 一遍再算：
        # "在线几台、历史几台"是同一份 worker_node 上的事实，两处各算迟早分家。
        # `workers` 仍原样给全部行——面板默认只画在线的，历史靠这两个数说话，
        # 点开时用的还是这一份数据，不另开一条查询。
        "live_count": len(live),
        "history_count": len(nodes) - len(live),
        "queue": {"queued": health["queued"], "running": health["running"],
                  "by_resource": by_resource},
        "alerts": alerts, "by_status": health["by_status"],
        "oldest_queued_seconds": health["oldest_queued_seconds"],
        "running_jobs": health["running_jobs"],
        "expired_leases": health["expired_leases"],
        "needs_attention": health["needs_attention"],
    }




def job_detail_view(job) -> dict:
    """详情视图：在 JobDetailOut 之外补日志静默与租约状态。

    这些是"现在动没动"的事实，不是新状态；所以不改 job 行，只随响应算一次。
    """
    from models import JobDetailOut

    log = jobfiles.stat_log(job["log_path"])
    out = JobDetailOut.from_row(job).model_dump()
    now = datetime.now()
    out.update({
        "lease_until": job["lease_until"],
        "lease_expired": bool(job["lease_until"]) and job["lease_until"] < now.strftime(
            "%Y-%m-%d %H:%M:%S"),
        "log_silent_seconds": log["silent_seconds"],
        "log_size": log["size"],
        "running_seconds": _seconds_since(job["started_at"])
        if job["status"] in ("starting", "running", "cancel_requested") else None,
    })
    return out




def retry_preview(job_id: int) -> dict:
    """重试前那一屏：继承什么、这一串已经计了多少、会不会再花一次钱。"""
    job = job_repo.get(job_id)
    if job is None:
        raise JobMissing("作业不存在")
    chain = job_repo.retry_chain(job_id)
    cloud = job["execution_mode"] == "cloud"
    rows = []
    total = 0
    pending = 0
    for row in chain:
        cost = row["cost_micros"] or 0
        total += int(cost)
        if row["billable_status"] == "pending_reconciliation":
            pending += 1
        rows.append({"id": row["id"], "status": row["status"],
                     "status_label": JOB_STATUSES.get(row["status"], row["status"]),
                     "attempt": int(row["attempt"] or 1), "cost_micros": int(cost),
                     "billable_status": row["billable_status"],
                     "created_at": row["created_at"]})
    may_charge = cloud and job["unit_price_micros"] is not None
    warning = ""
    if may_charge:
        warning = ("云端重试会新建一条作业并重新走一次供应商调用；"
                   f"这一串目前已有 {pending} 笔待核对、合计 "
                   f"{total / 1_000_000:.2f} {job['currency'] or ''}。")
    elif cloud:
        warning = "这条云端作业没有计价快照；重试仍不会自动补价，请先核对。"
    else:
        warning = "本地重试不计费，但会再占一次本机队列。"
    return {
        "job_id": job["id"], "execution_mode": job["execution_mode"],
        "provider_code": job["provider_code"], "model_code": job["model_code"],
        "prompt_preview": (job["prompt_snapshot"] or "")[:120],
        "params": json_loads_dict(job["params_json"]),
        "quantity": job["quantity"], "unit": job["unit"], "currency": job["currency"],
        "cost_micros": job["cost_micros"], "chain": rows,
        "chain_total_micros": total, "chain_pending": pending,
        "may_charge_again": bool(may_charge), "warning": warning,
    }




def json_loads_dict(text: str) -> dict:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}




def reconcile_job(job_id: int, payload, user) -> dict:
    """人工核对一条待核对的云端作业。

    三条硬规矩：
    - 只有 `needs_attention` 能核；正常成功/失败不该被这条路径改写。
    - `succeeded`/`failed` 必须给实际金额，且云端作业才允许核。
    - 只动账单与核对列，**绝不重新提交**；重投是人的另一个决定。
    """
    job = job_repo.get(job_id)
    if job is None:
        raise JobMissing("作业不存在")
    if job["execution_mode"] != "cloud":
        raise JobStateConflict("只有云端作业需要账单核对；本地作业不计费")
    if job["status"] != "needs_attention":
        raise JobStateConflict("这条作业当前不是待核对状态")
    if payload.decision in ("succeeded", "failed") and payload.actual_cost_micros is None:
        raise JobStateConflict("确认结果前必须填实际金额；不确定就选「继续待核对」")
    row = job_repo.reconcile(job_id, payload.decision, payload.actual_cost_micros,
                             payload.note.strip(), user["id"])
    if row is None:
        raise JobMissing("作业不存在")
    return {"job": row, "message": {
        "succeeded": "已确认可用，账单记为已确认。",
        "failed": "已确认不可用，这笔账按实际金额结清。",
        "pending": "仍保持待核对。",
    }[payload.decision]}




def mark_job_attention(job_id: int, reason: str, user) -> dict:
    """人工把一条云端作业送进"待核对"。

    为什么允许从 succeeded/failed 进来：供应商回执说成功、产物却打不开，
    或用量与预计差很多，这类情况现在只能落 failed，而 failed 的默认动作是重试——
    对一笔可能已经计过费的调用，"默认重试"是最坏的建议。
    """
    job = job_repo.get(job_id)
    if job is None:
        raise JobMissing("作业不存在")
    if job["execution_mode"] != "cloud":
        raise JobStateConflict("本地作业没有供应商账单可核对")
    if job["status"] not in ("succeeded", "failed"):
        raise JobStateConflict("只有已结束的成功或失败作业能改成待核对")
    with get_conn() as conn:
        conn.execute(
            """UPDATE job SET status = 'needs_attention', attention_reason = ?,
                      error_message = COALESCE(error_message, ?),
                      updated_at = datetime('now','localtime')
                WHERE id = ?""",
            (reason[:500], f"人工标记待核对：{reason[:500]}", job_id))
    return {"job": job_repo.get(job_id), "message": "已标记为待核对。"}
