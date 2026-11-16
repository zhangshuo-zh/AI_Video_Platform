"""asset_studio generation 业务域。"""
from __future__ import annotations
from asset_generation_refs import parse_refs
from models import ASSET_GEN_OPERATIONS, JOB_STATUSES, JobSubmitIn
from repositories.content import assets as asset_repo
from repositories.content import variants as variant_repo
from repositories.jobs import jobs as job_repo
from types import SimpleNamespace
from typing import Literal
import adapters
import hashlib
import jobfiles
import json
from .policy import AssetGenInvalid
from .policy import IMAGE_CAPABILITY
from .policy import IMAGE_OPERATION
from .policy import OUTPUT_SPEC
from .policy import TEXT_CAPABILITY
from .policy import TEXT_MODES
from .prompts import build_refs
from .prompts import build_text_prompt
from .prompts import current_fields
from .prompts import parse_sections
from .prompts import sections_for



# ---------- 提交生成作业 ----------

def _cloud_estimate(capability: str, payload, model_code: str, provider_code: str):
    """云端作业：把客户端的单价与清单登记的价格对一遍，再用**清单那份**重建。

    客户端传什么总价都不采信；`_cost_of` 拿到的只能是这里重建出来的单子。
    清单里没登记价格的模型档直接拒——"先跑起来，账以后再补"正是待核对账单的来源。
    """
    if provider_code == "mock":
        return None                      # 假适配器不计费，也不进清单
    adapter = adapters.find(capability, payload.execution_mode, provider_code)
    if adapter is None:
        raise AssetGenInvalid(f"provider「{provider_code}」没有登记适配器，平台不会猜它的参数")
    model = next((m for m in adapter.models if m.code == model_code), None)
    if model is None:
        raise AssetGenInvalid(f"模型档 {model_code} 不在清单里；先到系统设置核对这一轨")
    if not model.prices:
        raise AssetGenInvalid(f"模型档 {model.code} 没登记计价，不能提交云端计费作业")
    if payload.estimate is None:
        raise AssetGenInvalid("云端作业要带预计成本（数量、单位、单价、币种与价格来源）")
    price = next((p for p in model.prices if p.unit == payload.estimate.unit), None)
    if price is None:
        units = "／".join(p.unit for p in model.prices)
        raise AssetGenInvalid(f"这一档登记的计价单位是 {units}，提交的是 "
                              f"{payload.estimate.unit}；单位对不上就不算钱")
    if payload.estimate.unit_price_micros != price.amount_micros:
        raise AssetGenInvalid(f"单价与清单不一致：清单登记的是 {price.amount_micros} micros，"
                              f"提交的是 {payload.estimate.unit_price_micros}；"
                              "先到系统设置核对价格，不要按旧价提交")
    return SimpleNamespace(
        quantity=payload.estimate.quantity, unit=price.unit,
        unit_price_micros=price.amount_micros, currency=price.currency,
        source=price.source)



def generation_summary(job, ref: dict | None = None) -> dict:
    """一条生成作业的界面摘要。列表与提交回执共用这一份，别再写第二个映射。

    `ref` 让调用方把已经解析过的归属传进来（`recent_generations` 要先按它筛一遍），
    不传就自己解一次——省的是同一行 `json.loads` 两遍。
    """
    ref = ref if ref is not None else parse_refs(job)
    return {
        "job_id": job["id"], "status": job["status"],
        "status_label": _status_label(job["status"]),
        "operation": ref["operation"],
        "operation_label": ASSET_GEN_OPERATIONS.get(ref["operation"], ref["operation"]),
        "text_mode": ref["mode"],
        "text_mode_label": TEXT_MODES.get(ref["mode"], ""),
        "capability": job["capability"], "execution_mode": job["execution_mode"],
        "provider_code": job["provider_code"], "model_code": job["model_code"],
        "cost_micros": job["cost_micros"] or 0, "currency": job["currency"] or "",
        "billable_status": job["billable_status"],
        "target_asset_id": ref["asset_id"], "target_variant_id": ref["variant_id"],
        "note": "",
    }



def recent_generations(project_id: int, asset_id: int | None = None,
                      limit: int = 12) -> list[dict]:
    """先按项目、操作和资产归属筛选，再按 Job id 倒序取 limit。"""
    rows = job_repo.recent_asset_generations(
        project_id, tuple(ASSET_GEN_OPERATIONS), asset_id=asset_id, limit=limit)
    return [generation_summary(row) for row in rows]



def submit_generation(project_id: int, payload, created_by: int | None) -> dict:
    """提交一次资产生成（文字设定或图像候选）。

    这里做的是"拼提示词 + 判归属 + 核价格"，然后交给 `submit_job`：
    槽位、参数白名单、云端确认、成本、幂等、`source_version` 全在那一条路里判。
    所以这一层不再 resolve 一次就自己建作业——那是第二条提交路。
    """
    operation = payload.operation
    if operation not in ASSET_GEN_OPERATIONS:
        raise AssetGenInvalid(f"不认识的操作：{operation}")

    asset = None
    if payload.target_asset_id is not None:
        asset = asset_repo.get(payload.target_asset_id)
        if asset is None:
            raise AssetGenInvalid("目标资产不存在（可能已被删除）；"
                                  "要先生成再建资产，就别带目标资产 id")
    variant = None
    if payload.target_variant_id is not None:
        variant = variant_repo.get(payload.target_variant_id)
        if variant is None:
            raise AssetGenInvalid("目标形态不存在")
        if asset is None or variant["asset_id"] != asset["id"]:
            raise AssetGenInvalid("目标形态不属于这个资产；形态的归属只能往父资产对，"
                                  "不能各指一个")

    if operation == "asset_image":
        prompt = (payload.image_prompt or "").strip()
        if not prompt:
            raise AssetGenInvalid("图像提示词是空的；一致性提示词可以先同步过来再改")
        capability: Literal["image", "text"] = IMAGE_CAPABILITY
        mode = ""                      # 图像作业没有"任务模式"这一层
        sections: list[tuple[str, str]] = []
    else:
        mode = (payload.text_mode or "full").strip()
        if mode not in TEXT_MODES:
            raise AssetGenInvalid(
                "不认识的任务模式：" + mode + "；这一轮只有 "
                + "／".join(f"{key}（{label}）" for key, label in TEXT_MODES.items()))
        if asset is not None and operation == "character_profile" and asset["kind"] != "character":
            raise AssetGenInvalid("人物文字设定只能提交给人物资产")
        if asset is not None and operation == "scene_setting" and asset["kind"] != "scene":
            raise AssetGenInvalid("场景说明只能提交给场景资产")
        if asset is not None and operation == "prop_setting" and asset["kind"] != "prop":
            raise AssetGenInvalid("道具说明只能提交给道具资产")
        if mode == "from_source" and not (payload.source_excerpt or "").strip():
            raise AssetGenInvalid("「依据原文片段写设定」要先给一段原文；"
                                  "没有原文可依据时请用『生成完整设定』")
        target_field = (payload.target_field or "").strip()
        if mode == "expand":
            known = [field for _marker, field in OUTPUT_SPEC[operation]]
            if target_field not in known:
                raise AssetGenInvalid(
                    f"要扩写哪一段没对上；{ASSET_GEN_OPERATIONS[operation]}能扩写的是："
                    + "／".join(known))
        current = current_fields(operation, asset, payload)
        sections = sections_for(operation, mode, current, target_field)
        if not sections:
            raise AssetGenInvalid("这一段前头的标记都不在这类操作的输出清单里")
        prompt = build_text_prompt(payload, asset, mode, sections)
        capability = TEXT_CAPABILITY

    # 幂等键：界面没给就在这一层按"这次意图"派一个。为什么必须派而不是留着空：
    # `job.idempotency_key` 上有 UNIQUE 索引，但**没人传就等于没有幂等**——
    # 连点两下「生成图像候选」是两条各自计费的作业（本轮任务书 §11.2 点名的那条）。
    # 键的内容变了（改提示词／换档位／改参数）就是新意图，所以派生自这些字段而不是随机串；
    # 真要"同样的话再跑一批"由界面显式带一个带随机后缀的键进来，那是人的决定，不是页面的手滑。
    key = (payload.idempotency_key or "").strip() or _derived_key(
        project_id, operation, mode, payload.target_asset_id, payload.target_variant_id,
        capability, payload, prompt)
    reused = job_repo.by_key(key) is not None

    submit = JobSubmitIn(
        project_id=project_id, episode_id=None, shot_id=None, capability=capability,
        execution_mode=payload.execution_mode, provider_code=payload.provider_code,
        model_code=payload.model_code, prompt=prompt, params=payload.params,
        input_refs=build_refs(operation, payload.target_asset_id,
                              payload.target_variant_id, mode,
                              tuple(field for _label, field in sections)),
        idempotency_key=key,
        user_confirmed_cloud=payload.user_confirmed_cloud, priority=0)

    # 云端：先按清单核一遍价格，再把 estimate 换成清单那份重建的单子。
    # 本地：estimate 一律不带——_cost_of 见到本地带确认单会直接拒，这是故意的。
    if payload.execution_mode == "cloud":
        if not payload.user_confirmed_cloud:
            raise AssetGenInvalid("云端作业要先勾「我已知晓可能产生费用」；"
                                  "没勾就不建作业，一次都发不出去")
        confirmed = _cloud_estimate(capability, payload, submit.model_code, submit.provider_code)
        submit = submit.model_copy(update={"estimate": confirmed})

    job = _submit_job(submit, created_by)
    return {
        "job_id": job["id"], "status": job["status"],
        "status_label": _status_label(job["status"]), "operation": operation,
        "operation_label": ASSET_GEN_OPERATIONS[operation], "capability": capability,
        "text_mode": mode, "text_mode_label": TEXT_MODES.get(mode, ""),
        "sections": [label for label, _field in sections],
        "reused": reused,
        "execution_mode": job["execution_mode"], "provider_code": job["provider_code"],
        "model_code": job["model_code"], "cost_micros": job["cost_micros"] or 0,
        "currency": job["currency"] or "", "billable_status": job["billable_status"],
        "target_asset_id": payload.target_asset_id,
        "target_variant_id": payload.target_variant_id,
        "note": ("这条作业的意图和已排队的某条完全一样，平台没有再排一条——"
                 "要再来一批就改提示词或点『换一批重新生成』。"
                 if reused else
                 "作业已排进生成中心那一个队列；状态、日志、成本与产物都在那里看。"),
    }



def _derived_key(project_id: int, operation: str, mode: str, asset_id: int | None,
                 variant_id: int | None, capability: str, payload, prompt: str) -> str:
    """把"这一次意图"压成一个幂等键。

    键里放的是**会让人一眼看出不是同一件事**的那几样：给谁做、做什么、按哪档做、
    提示词本身。故意不放时间戳——放了它就等于每次都新键，幂等成了装饰。
    """
    digest = hashlib.sha1("|".join([
        prompt, capability, (payload.execution_mode or ""), (payload.provider_code or ""),
        (payload.model_code or ""), json.dumps(payload.params or {}, ensure_ascii=False,
                                               sort_keys=True),
    ]).encode("utf-8")).hexdigest()[:12]
    return (f"asset-gen:{project_id}:{operation}:{mode or 'img'}:"
            f"a{asset_id or 0}v{variant_id or 0}:{digest}")



def _submit_job(payload, created_by):
    """唯一一条提交路的入口。

    `import services` 放在函数里而不是文件头：services 会经由 routers 引到本模块，
    顶层互 import 会转圈。这一层做的事只有一件——把已经拼好的 JobSubmitIn 交出去，
    自己既不 resolve 也不建作业行。
    """
    import services
    return services.submit_job(payload, created_by)



def _status_label(status: str) -> str:
    return JOB_STATUSES.get(status, status)



# ---------- 结果预览 ----------

def generation_view(project_id: int, job_id: int) -> dict:
    """作业状态 + 解析好的表单草稿。

    三条不骗人的规则：
    - 作业没成功 → `can_fill=False`，字段全空，note 说清为什么；
    - 解析不出任何一段 → 仍然 `can_fill=False`，但把原文带回去让人自己抄，
      而不是把整坨文字塞进第一个字段冒充成功；
    - 这里的字段是**草稿**，界面填进表单，由人显式点保存才落库。
    """
    job = job_repo.get(job_id)
    if job is None or job["project_id"] != project_id:
        raise AssetGenInvalid("作业不存在，或不属于这个项目")
    ref = parse_refs(job)
    if not ref["operation"] or ref["operation"] not in ASSET_GEN_OPERATIONS:
        raise AssetGenInvalid("这条作业不是资产中心发起的生成")
    operation = ref["operation"]
    mode = ref["mode"]
    out = {
        "job_id": job["id"], "status": job["status"],
        "status_label": _status_label(job["status"]), "operation": operation,
        "operation_label": ASSET_GEN_OPERATIONS[operation],
        "text_mode": mode, "text_mode_label": TEXT_MODES.get(mode, ""), "can_fill": False,
        "fields": {}, "raw": "", "note": "", "error_code": job["error_code"] or "",
        "error_message": job["error_message"] or "",
        "target_asset_id": ref["asset_id"], "target_variant_id": ref["variant_id"],
    }
    if job["status"] != "succeeded":
        out["note"] = ("作业还没有成功结束；表单里的内容不会被改动。"
                       if job["status"] in ("pending", "queued", "running")
                       else f"作业现在是「{out['status_label']}」，没有可用的结果。")
        return out
    if operation == IMAGE_OPERATION:
        # 图像作业没有"分段回稿"这一层：它的产物是一条条文件，界面在候选网格里
        # 按序号预览。轮询对文字与图像用的是同一个端点，这一支不写清楚，
        # 下一行就会去查 OUTPUT_SPEC['asset_image']——查不到就是 500。
        out["note"] = "图像作业的产物在下面的候选网格里逐张预览，不在这里填表。"
        return out

    path = jobfiles.artifact_at(job["result_paths_json"], 0)
    if path is None:
        out["note"] = "作业成功了，但找不到可读的文本产物。"
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        out["note"] = f"读不到 AI 结果：{type(exc).__name__}"
        return out

    out["raw"] = text[:20000]
    # 按**这一条作业当时请求的段落**切，不是按这一类操作的全量切（见 build_refs）。
    # 上一轮建的作业没有 sec 记录，那时确实要求全量输出，`not ref["fields"]` 就按全量。
    requested = [pair for pair in OUTPUT_SPEC[operation]
                 if not ref["fields"] or pair[1] in ref["fields"]]
    fields = parse_sections(operation, text, requested)
    out["fields"] = fields
    if fields:
        out["can_fill"] = True
        missing = [label for label, field in requested if field not in fields]
        head = (f"这次是「{TEXT_MODES[mode]}」，只请求了 "
                + "／".join(label for label, _f in requested) + "；"
                if mode in TEXT_MODES and len(requested) < len(OUTPUT_SPEC[operation]) else "")
        out["note"] = (head + ("填进表单后请自己核对再保存。没解析出：" + "／".join(missing)
                               if missing else "填进表单后请自己核对再保存。"))
    else:
        out["note"] = ("模型没有按标记分段，所以这里不自动填表——"
                       "原文在下面，可以自己挑着粘；或者重跑一次。")
    return out
