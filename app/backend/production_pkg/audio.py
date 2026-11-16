"""production audio 业务域。"""
from __future__ import annotations
import adapters



def _audio_wired() -> dict:
    """配音／音乐到底能不能真跑。只用两个既有事实：

    1. `services.resolve_provider()` 能不能解析出默认轨，以及那一轨的健康状态；
    2. 该 provider 在 `runners.build()` 里有没有分支——问 `runners.has_branch()`，
       它与 `build()` 读同一串条件，所以这张表不会在接线之后还写着"没接线"。
    """
    import services

    out = {}
    for capability, label in (("tts", "配音"), ("music", "音乐")):
        provider, health = "", {"state": "unconfigured", "detail": "默认轨没配"}
        try:
            resolved = services.resolve_provider(capability)
            provider = resolved["provider"]
            # 项目概览是**读**接口（工作台一次读取要给「跑到哪＋花了多少」），
            # 不能因为某台服务半死就把整页拖住。要现查走模型中心的显式入口。
            health = adapters.health(resolved["row"], probe_service=False)
        except services.NoRoute as exc:
            health = {"state": "unconfigured", "detail": str(exc)}
        # `runner_wired` 问 `runners.has_branch()`：这里曾经写死 False，
        # 而写死的那个值在接线当天就会变成假话——能力表必须跟着代码走。
        import runners
        out[capability] = {"label": label, "provider": provider,
                           "health": health,
                           "runner_wired": runners.has_branch(capability, provider)}
    return out



def _audio_stage_note(wired: dict) -> str:
    """项目看板「音频制作」那一格该说的话——由 `_audio_wired()` 的三处事实现算。

    为什么单独成一个函数：这句话原来是在 `episode_board()` 里硬编码的常量
    「执行引擎尚未接入，只能登记台账」。硬编码的当前状态陈述一旦过时，
    就是一句**对用户说的假话**，而且没人会去试，所以能活很久——它确实活了
    两天多（2026-11-05 09:39 写下，当天 10:37 配音腿就接线了）。

    抽出来的另一个理由是**可测**：`episode_board()` 要建项目、集、镜头才能走到
    这一句，与"这句话说得对不对"完全无关；单独成函数之后，
    `test_项目看板的音频那句也必须现算不能写死` 直接喂三档事实就能验。

    四档，按"哪一处先坏"排：
      * 配音 provider 与分支都在 → 能出声；音乐那一格另外说（缺默认轨／缺分支）；
      * 只有 provider、没有分支 → 配了但跑不了，明说缺分支；
      * provider 都没有 → 只能登记台账。
    """
    tts = wired.get("tts") or {}
    music = wired.get("music") or {}
    tts_ok = bool(tts.get("provider")) and bool(tts.get("runner_wired"))
    music_ok = bool(music.get("provider")) and bool(music.get("runner_wired"))
    if tts_ok:
        if music_ok:
            return "配音可提交并真出声；音乐也能提交"
        missing = ("默认轨还没配" if not music.get("provider")
                   else "执行器没有这一条分支")
        return f"配音可提交并真出声；音乐这一类{missing}"
    if tts.get("provider"):
        return "配音默认轨配了，但执行器没有这一条分支"
    # 三处都空：这时"未接入"是现算出来的结论，不是写死的常量——
    # 反向测试 `test_两条腿都没接线时那句才说未接入` 钉着这一句必须还在，
    # 免得有人把上面几条修完之后顺手把这里也改成永远沉默。
    if not tts.get("runner_wired") and not music.get("runner_wired"):
        return "配音/音乐执行引擎尚未接入，音频段只能先登记台账"
    return "配音默认轨还没配，音频段只能先登记台账"



def audio_status_view() -> dict:
    """音频这一腿的真实能力表（任务书 §6.1 第五阶段、§15 P1「音频能力状态透明化」）。

    为什么要服务端算这张表而不是前端写死四行：这一页上"能不能提交"以前是**看按钮存不存在**
    推断出来的，而真相分散在三处——清单里有没有这个 capability、默认轨解析不解析得出、
    `runners.build()` 有没有这一条分支。三处任意一处变了，前端那份写死的话就成了假话。
    所以这里只读那三处已有的事实，一条都不新造。

    四类（配音／旁白走 tts，音乐走 music）之外还回答**口型与音效**：
    它们今天连 capability 都没有，透明化的意思是把"没有"这件事也摆到同一张表上，
    而不是让人在界面上找了半天才发现这功能不存在。
    """

    wired = _audio_wired()

    def row(code, label, capability, engine, *, can_register=True):
        entry = wired.get(capability) or {}
        health = entry.get("health") or {}
        runner_wired = bool(entry.get("runner_wired"))
        missing = []
        if not entry:
            missing.append("清单里没有这个能力的适配器")
        else:
            if not entry.get("provider"):
                missing.append("这一类能力的默认轨没配")
            if health.get("state") not in ("ok",):
                missing.append(health.get("detail") or "运行时或权重不齐")
            if not runner_wired:
                missing.append("`runners.build()` 没有这条分支（执行未接线）")
        return {
            "code": code, "label": label, "capability": capability, "engine": engine,
            "provider": entry.get("provider") or "",
            "health_state": health.get("state") or "absent",
            "health_detail": health.get("detail") or "",
            "runner_wired": runner_wired,
            "can_submit": runner_wired and bool(entry.get("provider"))
                          and health.get("state") == "ok",
            # 登记台账是纯库内动作，与执行接没接线无关——台词该先记下来就记下来
            "can_register": can_register,
            "missing": missing,
            "why": "可以提交执行" if runner_wired and health.get("state") == "ok" else
                   ("；".join(missing) if missing else "现在还不能提交"),
        }

    rows = [
        row("dialogue", "配音（台词）", "tts", "VoxCPM2"),
        row("narration", "旁白", "tts", "VoxCPM2"),
        row("music", "背景音乐", "music", "ACE-Step 1.5"),
        row("sfx", "音效", "", "（无）"),
        row("lipsync", "口型同步", "", "（设计里是 Rhubarb viseme 分层）",
            can_register=False),
    ]
    ready = [r for r in rows if r["can_submit"]]
    return {
        "rows": rows,
        "any_wired": bool(ready),
        "note": ("四类里有 " + str(len(ready)) + " 类已经能提交执行" if ready else
                 "今天音频这一腿没有一类能真正执行：能登记台词与素材归属，"
                 "但配音／音乐／音效／口型的执行都还没接线。"
                 "所以这几页不给「提交」按钮——给了就是让人点一次失败一次。")
        # `ready` 为空只说明「没有一类能提交」：可能是默认轨没配、health 不 ok，
        # 也可能是执行没接线（见上面 `can_submit` 的三个条件）。上面那句兜底
        # 把三种情况一并说成"执行还没接线"，是沿用旧口径的措辞，别当成现算结论；
        # 逐类的真实缺口在每行 `missing` / `why` 里，看那两列才不会误判。
    }
