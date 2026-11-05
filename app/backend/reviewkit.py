"""审核质量清单（任务书 §8.2／§12.1，P1 第 4、5 项）。一句话职责：**把"这一版能不能用"拆成看得过去的几问**。

为什么要有这一层：产物审核今天只有一枚结论加一句 note。可 §8 明写"审核不仅检查文件，
也检查内容质量"——画面跟不跟提示词、人物形态对不对、有没有坏帧、台词能不能对上，
这些是**逐条判断**。压成一句 note 之后，第二个人复审同一条作业时看不出上一版卡在哪儿，
批量签字也没法回答"这一批里有多少是认真看过的"。

三条纪律：

1. **不替代签字**。结论仍然只有 `review_item` 那一个写入口（签字只落审核中心，别处只显示状态）。
   这里存的是"你看过哪几项、分别判了什么"；清单和签字绑**同一个** `base_version`，
   产物一换两者一起失配，不会出现"清单是新的、结论是旧的"。
2. **自动项不入库**。文件在不在、时长对不对、源版本变没变——每次都从
   `jobfiles` / `composition.validators.inspect` / `services.job_source_stale` 现算。
   存下来就是养第二份事实，而它一定会过期。
3. **判据只有一条新增**：`通过` 要求人工清单的必选项都有结论。
   这是 §8 的本意（不许没看就盖章）；但**驳回永远不需要先填清单**——
   一眼看出不行就该立刻能驳回，不能被表单卡住。这个不对称是有意的，写在这里免得后人"顺手统一"。
"""
from __future__ import annotations

import jobfiles
from composition import validators

# 人工清单项。code 的取值集合就是 `review_check.code` 该存的东西（迁移 0038 的注释指回这里）。
# 每项两行话：`label` 给界面列表用，`question` 是给审核人看的那一句人话。
HUMAN_ITEMS: dict[str, list[dict]] = {
    "image": [
        {"code": "content.matches_prompt", "label": "画面对提示词",
         "question": "这一帧画的是提示词要的内容吗？（跑偏就算不合格，不必等其他项）",
         "required": True},
        {"code": "content.subject_consistency", "label": "主体与资产一致",
         "question": "人物／场景／道具的长相和引用资产（含具体形态）对得上吗？",
         "required": True},
        {"code": "content.no_defect", "label": "无明显缺陷",
         "question": "有没有多手指、断裂肢体、文字乱码、边缘糊掉这类必须重做的毛病？",
         "required": True},
        {"code": "content.usable_as_keyframe", "label": "能当关键帧用",
         "question": "这一帧可以拿去做图生视频的条件图吗？（只是好看但不当条件图，选不适用）",
         "required": False},
    ],
    "video": [
        {"code": "content.matches_prompt", "label": "画面对提示词",
         "question": "这段动的是提示词要的内容吗？", "required": True},
        {"code": "content.subject_consistency", "label": "主体与资产一致",
         "question": "整段里人物／场景的长相稳定，且和引用的资产形态一致吗？",
         "required": True},
        {"code": "content.motion_ok", "label": "运动可用",
         "question": "运动幅度与运镜能配合这一镜的分镜意图吗？（漂移、抽搐、静止都算不合格）",
         "required": True},
        {"code": "content.no_defect", "label": "无坏帧闪烁",
         "question": "有没有坏帧、闪一下、多余肢体？（本机 MPS 出过静默坏帧，这一项必须真看）",
         "required": True},
        {"code": "content.dialogue_sync", "label": "台词能对上",
         "question": "这一镜有台词吗？口型与台词是否按现有分层口径对得上？",
         "required": False, "required_if": "has_dialogue"},
    ],
}

# 待裁一件事：清单要求要不要下沉到**单条签字接口**。
# 现行为（False）＝要求只住在批量接口与审核中心界面（按钮禁用）；
# 脚本与产线节点走 `services.job_review_sign` 仍能直接签通过。
# 翻 True＝任何 signed approval 都必须先看完人工必看项（产线人工关卡与验收脚本要跟着改）。
# 这一栏是"要不要更严"的产品决定，所以写成开关而不是一次悄悄改死。
ENFORCE_CHECKLIST_ON_SINGLE_SIGN = False

STATES = ("pass", "issue", "na")
STATE_LABELS = {"pass": "合格", "issue": "不合格", "na": "不适用"}

# 时长与分镜意图的容差：模型只能按帧数档出片，差半秒是常态，差两秒是该重看。
DURATION_TOLERANCE = 0.25
# 小于这个字节数的视频／图像基本是坏文件（纯色 1 秒 mp4 也有几十 KB）。
MIN_ARTIFACT_BYTES = 1024


def _auto_items(job, shot, clips, stale: bool, checked_exists: list[dict]) -> list[dict]:
    """自动项：每次现算，一条都不入库。"""
    import services

    items: list[dict] = []
    artifacts = checked_exists or jobfiles.artifacts(job["result_paths_json"])
    missing = [a["path"] for a in artifacts if not a.get("exists")]
    if missing:
        items.append({"code": "artifact.exists", "level": "block",
                      "label": "产物文件在盘上",
                      "detail": "文件已不在：" + "、".join(missing)})
    elif not artifacts:
        items.append({"code": "artifact.exists", "level": "block",
                      "label": "产物文件在盘上", "detail": "这条作业没有登记任何产物"})
    else:
        items.append({"code": "artifact.exists", "level": "pass",
                      "label": "产物文件在盘上",
                      "detail": f"{len(artifacts)} 件，共 {sum(int(a.get('size') or 0) for a in artifacts)} 字节"})

    small = [a["path"] for a in artifacts if a.get("exists") and int(a.get("size") or 0) < MIN_ARTIFACT_BYTES]
    if small:
        items.append({"code": "artifact.size", "level": "warn",
                      "label": "产物大小合理",
                      "detail": "这几件小得不像有效文件：" + "、".join(small)})

    # 媒体真读得出来才算数：合成取片时同样要 ffprobe，这里提前一次，
    # 免得"审核签了通过、合成当天才说不认"。
    first = next((a for a in artifacts if a.get("exists")), None)
    info: dict = {}
    if first is not None:
        try:
            info = validators.inspect(jobfiles.resolve_artifact(first["path"]))
        except Exception as exc:                      # 读不出来就是不合格，别猜
            items.append({"code": "media.readable", "level": "block",
                          "label": "媒体读得出来",
                          "detail": f"ffprobe 读不出：{type(exc).__name__}"})
    if info:
        items.append({"code": "media.readable", "level": "pass",
                      "label": "媒体读得出来",
                      "detail": f"{info.get('width')}×{info.get('height')}"
                                f"｜{info.get('duration_ms') or 0}ms"})
        if shot is not None and info.get("duration_ms"):
            want_ms = float(shot["duration"]) * 1000.0
            got_ms = float(info["duration_ms"])
            diff = abs(got_ms - want_ms) / want_ms if want_ms else 0.0
            if diff > DURATION_TOLERANCE:
                items.append({"code": "media.duration", "level": "warn",
                              "label": "时长对分镜",
                              "detail": f"分镜要 {want_ms:.0f}ms，实得 {got_ms:.0f}ms"
                                        f"（差 {diff * 100:.0f}%，超过 {DURATION_TOLERANCE * 100:.0f}%）"})
    if job["status"] != "succeeded":
        items.append({"code": "review.state", "level": "block", "label": "作业已成功",
                      "detail": f"当前状态「{services.JOB_STATUSES.get(job['status'], job['status'])}」，"
                                "只有已成功的作业才有产物可审"})
    if stale:
        items.append({"code": "source.current", "level": "warn", "label": "源版本一致",
                      "detail": "分镜或引用的资产在这条产物生成之后改过；"
                                "签了通过也不会进成片，要重新生成并重新签字"})
    else:
        items.append({"code": "source.current", "level": "pass",
                      "label": "源版本一致", "detail": "与当前分镜、当前资产引用同一版"})
    unready = [c for c in clips if c["status"] != "ready"]
    if unready:
        items.append({"code": "audio.ready", "level": "warn", "label": "本镜音频就绪",
                      "detail": f"{len(unready)} 条台词／配音素材未就绪"
                                "（配音与音乐执行今天未接线，成片时按静音处理）"})
    elif clips:
        items.append({"code": "audio.ready", "level": "pass",
                      "label": "本镜音频就绪", "detail": f"{len(clips)} 条都已就绪"})
    return items


def checklist(job, *, shot=None, clips=None, stale: bool = False,
              checks: dict[str, dict] | None = None,
              artifacts: list[dict] | None = None) -> dict:
    """一条作业的完整审核面：自动项 ＋ 人工项 ＋ 能不能签字的结论。

    `checks` 是已存的人工结论（code → 行），由调用方一次批量读好传进来——
    审核中心一屏几十条作业时不许逐条回库。
    """
    import services

    checks = checks or {}
    clips = clips or []
    items = HUMAN_ITEMS.get(job["capability"], [])
    auto = _auto_items(job, shot, clips, stale, artifacts or [])
    blocking = [i for i in auto if i["level"] == "block"]

    human: list[dict] = []
    missing: list[str] = []
    for item in items:
        stored = checks.get(item["code"])
        required = bool(item.get("required"))
        # `required_if: has_dialogue`：这一镜没台词就不该逼人勾一项
        if item.get("required_if") == "has_dialogue":
            required = bool(clips)
        state = (stored or {}).get("state")
        if required and state not in STATES:
            missing.append(item["code"])
        human.append({**item, "required": required, "state": state,
                      "state_label": STATE_LABELS.get(state or "", "未看"),
                      "note": (stored or {}).get("note") or "",
                      "checked_at": (stored or {}).get("checked_at") or "",
                      "checked_by": (stored or {}).get("checked_by_name") or ""})

    issues = [h["code"] for h in human if h["state"] == "issue"]
    decision = "可签字" if not blocking and not missing else (
        "先补清单" if missing and not blocking else "有阻塞项")
    return {
        "job_id": int(job["id"]),
        "capability": job["capability"],
        "version": services.artifact_version(job) if job["status"] == "succeeded" else "",
        "auto": auto,
        "human": human,
        "counts": {
            "auto_total": len(auto),
            "auto_blocking": len(blocking),
            "auto_warning": sum(1 for i in auto if i["level"] == "warn"),
            "human_required": sum(1 for h in human if h["required"]),
            "human_done": sum(1 for h in human if h["required"] and h["state"] in STATES),
            "human_missing": len(missing),
            "human_issues": len(issues),
        },
        "missing": missing,
        "issues": issues,
        "can_approve": not blocking and not missing and job["status"] == "succeeded",
        "decision_label": decision,
        "hint": ("有阻塞项，先解决再签：" + "；".join(i["detail"] for i in blocking[:3])
                 if blocking else
                 (f"人工清单还差 {len(missing)} 项才能签通过（驳回不用先填）"
                  if missing else "清单齐了，可以签字")),
    }


def validate_codes(capability: str, codes) -> list[str]:
    """界面传来的项必须属于这一类能力的清单——不许借这个口子往库里塞野 code。"""
    known = {i["code"] for i in HUMAN_ITEMS.get(capability, [])}
    return [c for c in codes if c not in known]
