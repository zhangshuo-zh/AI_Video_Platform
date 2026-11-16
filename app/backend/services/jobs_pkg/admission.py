"""作业服务 admission 域。"""
from ..errors import JobInvalid, NoRoute
from ..providers import resolve_provider
from db import get_conn
from repositories.content import shots
from repositories.jobs import jobs as job_repo
from repositories.model_center import model_center as registration_repo
import adapters
import config
import engines
import sqlite3
from .billing import _cost_of
from .source import shot_source_version



def job_kind(capability: str, execution_mode: str) -> str:
    """adapter 请求类别。视频必须区分本地/云端——同样的 capability，
    本地跑 FastMetal 与云端调 Wan API 是两条完全不同的执行路径。"""
    return f"video_{execution_mode}" if capability == "video" else capability





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



def submit_job(payload, created_by: int | None) -> sqlite3.Row:
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
    elif provider == "ffmpeg":
        if mode not in (None, "local"):
            raise JobInvalid("FFmpeg 只能走本地执行")
        if capability != "video" or model != "ffmpeg-lipsync-1":
            raise JobInvalid("FFmpeg 仅支持 video/ffmpeg-lipsync-1 对口型任务")
        if str(payload.params.get("operation") or "") != "lip_sync" or not str(payload.params.get("audio_path") or "").strip():
            raise JobInvalid("对口型任务缺少 operation=lip_sync 或 audio_path")
        mode = "local"
        adapter_version = "ffmpeg"
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
