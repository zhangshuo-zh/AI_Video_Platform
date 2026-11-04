"""资产中心增强包的服务层：人物档案、命名形态、资产生成与产物导入。

四条纪律，解释这个文件为什么长这样：

1. **生成只走 `services.submit_job` 那一条提交路。** 路由、参数白名单、云端确认、
   金额重算、幂等、`source_version` 全在那里。docai 与 audio 当年各自
   `job_repo.create()` 建过作业，那是第二、第三条提交路——本模块不再加第四条，
   所以这里只拼提示词、判归属，一个成本列都不碰。
2. **模型与计价只认清单。** 轨道来自 `provider_config`，参数与价格来自
   `adapters` manifest。云端模型档没登记价格就直接拒，不"先跑起来再说"；
   本地没配就报本地没配，绝不静默换轨（规格 02 §8.3）。
3. **归属与可见性由 `access` 判。** 这里只接已经判过的行；形态的可见性一律
   从父资产继承——只校验"variant id 查得到"是不够的，那等于任何人能读别人的形态。
4. **文件与库不假装原子。** 先把图复制进资产目录，再在一个事务里改引用；
   事务失败就把刚复制的文件删掉。留一个"库里没有、磁盘上有"的文件，
   比反过来更难查——反过来至少界面上不会显示一张打不开的图。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from types import SimpleNamespace

import adapters
import jobfiles
from config import FILES_DIR
from imagetypes import MAX_IMAGE_BYTES, sniff
from models import ASSET_GEN_OPERATIONS, JOB_STATUSES, JobSubmitIn, VARIANT_KINDS
from paths import resolve_within
from repositories.content import assets as asset_repo
from repositories.content import profiles as profile_repo
from repositories.content import variants as variant_repo
from repositories.jobs import jobs as job_repo

# 每种操作要模型输出哪几段、落到表单的哪个字段。
# 段名写进提示词、也用它切结果——两头同一份表，改一处不会漏改另一处。
OUTPUT_SPEC: dict[str, list[tuple[str, str]]] = {
    "character_profile": [
        ("【履历】", "biography"),
        ("【性格】", "personality"),
        ("【成长过程】", "growth_arc"),
        ("【一致性提示词】", "consistency_prompt"),
    ],
    "scene_setting": [
        ("【说明】", "summary"),
        ("【一致性提示词】", "consistency_prompt"),
    ],
    "prop_setting": [
        ("【说明】", "summary"),
        ("【一致性提示词】", "consistency_prompt"),
    ],
}

# 每种操作对"这段该写什么"的提示，来自开发说明 §4.2／§4.3／§4.4。
# 写在这里而不是让模型自由发挥：一致性提示词的口径要和详情页那段说明对得上，
# 用户在两处看到的应该是同一句话。
GUIDANCE: dict[str, str] = {
    "character_profile": "面部与识别特征、发型、服装、体型与总体风格、必须保持不变的细节",
    "scene_setting": "空间结构、建筑与地形、时间与天气、光线、色彩、环境材质、机位与风格约束",
    "prop_setting": "轮廓、比例、材质、颜色、磨损与纹理、关键识别特征、使用状态",
}

# 文字任务模式（增强包开发说明之外、本轮任务书 §5.4 点名的那件事）。
# 为什么要它：只有"生成完整设定"一种模式时，每一次点击都把已有的三段重写成
# 三份新的小作文——而人真正想要的往往只是"把空的填上"或"这段再展开一层"。
# 更麻烦的是它会顺带把已经定稿的段落改掉，等于让模型第二次写同一格。
# 模式决定两件事：**要模型输出哪几段**（`sections_for`）和**给它什么指令**
# （`MODE_INSTRUCTIONS`），两者都在服务端算，界面上只选不判。
TEXT_MODES: dict[str, str] = {
    "full": "生成完整设定",
    "fill_missing": "只补空着的字段",
    "polish": "优化已有设定",
    "expand": "扩写指定字段",
    "from_source": "依据原文片段写设定",
}

MODE_INSTRUCTIONS: dict[str, str] = {
    "full": "按现有信息把下面这几段写完整；已有内容可以对得上，但不要照抄成三份重复的话。",
    "fill_missing": "下面列出的段落现在是空的，**只写这些**。已有的段落不要重写、"
                    "不要在回复里复述一遍。",
    "polish": "这几段已经有内容了。只做文字层面的收紧与具体化：**不新增事实**"
              "（不加原文里没有的经历、关系、能力），不改人名与已经定下的设定。",
    "expand": "把指定的这一段往深处展开一层：写法、动机、后果都可以补，"
              "但补出来的必须和已有内容不矛盾，且不改动其他段落。",
    "from_source": "只依据最后给出的原文片段来写。原文没有的**不要编**，"
                   "确实要推测的地方在句子里标「建议」。",
}
# `from_source` 这一种要额外输入（原文片段），服务端先判——别等人勾了云端确认、
# 钱已经花出去了，才发现少填了一样东西。
MODES_NEED_EXCERPT = ("from_source",)

# 资产类型 → 允许的形态类型。DDL 里的 CHECK 只能收死取值范围，
# "outfit 只能挂在人物下"这种跨表规则 SQLite 写不了（CHECK 里不能引用别的表），
# 所以在这层判——报错也要报得比"违反约束"像人话。
VARIANT_TYPES_BY_KIND: dict[str, tuple[str, ...]] = {
    "character": ("outfit", "other"),
    "scene": ("scene_view", "other"),
    "prop": ("prop_form", "other"),
}

TEXT_CAPABILITY = "text"
IMAGE_CAPABILITY = "image"
# 图像那一支的操作名：文字设定有三种（人物／场景／道具），图像只有这一种。
# 它同时是"这条作业没有分段回稿"的判据——见 generation_view 里那一支。
IMAGE_OPERATION = "asset_image"


class AssetGenInvalid(Exception):
    """提交前的校验没过（操作与类型不匹配、提示词空、云端没确认…）。路由转 422。"""


class ProfileInvalid(Exception):
    """给非人物资产读写档案。转 422，不是 404——资产在，只是这类资产没有档案这一层。"""


class VariantMissing(Exception):
    """形态不存在，或它所属的资产对这个人不可见。转 404。"""


class VariantConflict(Exception):
    """同一资产同类型下重名。转 409，让人改名，不自动加序号。"""

class ImportUnusable(Exception):
    """那条作业不能用来导入资产图（不成功／不是图像／不可见）。转 409。"""


class ImportArtifactBad(Exception):
    """产物序号越界或文件已经不在了。转 422。"""


class ImportImageRejected(Exception):
    """产物不是允许的图片格式或超限。转 415。"""


# ---------- 归属引用 ----------

def build_refs(operation: str, asset_id: int | None,
               variant_id: int | None, mode: str = "",
               fields: tuple[str, ...] = ()) -> list[str]:
    """把"这次生成算给谁、按哪种任务模式跑、要模型出哪几段"写进 Job 的 input_refs。

    用字符串而不是给 job 表加三列：`JobSubmitIn.input_refs` 本来就是 list[str]，
    而 job.kind 有 CHECK 白名单——为一类新目标重建整张表，代价远大于收益。
    docai 那边存的是 dict，走的是它自己的 job_repo.create()；这里既然复用
    submit_job，就按公开模型声明的类型来。

    `mode` 与 `fields` 也记在这里，不另开列：回稿要按**当时请求的那几段**来切。
    事后从"现在库里有没有内容"反推是不行的——人很可能已经把这轮的回稿保存进去了，
    那时候按当前内容重算一次 `fill_missing` 会得到完全不同的段清单。
    图像作业两样都留空，就不写这两条。
    """
    refs = [f"op:{operation}"]
    if mode:
        refs.append(f"mode:{mode}")
    for field in fields:
        refs.append(f"sec:{field}")
    if asset_id is not None:
        refs.append(f"asset:{asset_id}")
    if variant_id is not None:
        refs.append(f"variant:{variant_id}")
    return refs


def parse_refs(job) -> dict:
    """从作业里读出归属。读不出 op 就说明这不是资产生成作业。"""
    try:
        refs = json.loads(job["input_refs_json"] or "[]")
    except (TypeError, ValueError):
        refs = []
    out = {"operation": "", "asset_id": None, "variant_id": None,
           "mode": "", "fields": []}
    for item in refs if isinstance(refs, list) else []:
        if not isinstance(item, str) or ":" not in item:
            continue
        key, value = item.split(":", 1)
        if key == "op":
            out["operation"] = value
        elif key == "mode":
            out["mode"] = value
        elif key == "sec":
            out["fields"].append(value)
        elif key in ("asset", "variant"):
            try:
                out[f"{key}_id"] = int(value)
            except ValueError:
                pass
    # 这里只报**行里写了什么**：图像作业本来就没有模式这一层，替它填一个
    # "full" 会让列表里出现"图像作业按完整设定模式跑"这种没发生过的事。
    # 需要默认值的调用方（文字回稿的段落切分）自己在用的那一层补，见 generation_view。
    return out


# ---------- 人物档案 ----------

def save_profile(asset_id: int, payload) -> dict:
    """整体保存人物档案。非人物资产没有"档案"这一层，直接说清楚。"""
    asset = asset_repo.get(asset_id)
    if asset is None:
        raise ProfileInvalid("资产不存在")
    if asset["kind"] != "character":
        raise ProfileInvalid("只有人物资产有文字档案；场景与道具的说明写在简介与"
                             "一致性提示词里，那里已经有编辑入口了")
    return profile_repo.upsert(
        asset_id,
        biography=(payload.biography or "").strip(),
        personality=(payload.personality or "").strip(),
        growth_arc=(payload.growth_arc or "").strip(),
        text_prompt=(payload.text_prompt or "").strip())


# ---------- 命名形态 ----------

def _checked_variant_type(kind: str, variant_type: str) -> str:
    allowed = VARIANT_TYPES_BY_KIND.get(kind, ())
    if variant_type not in allowed:
        raise AssetGenInvalid(
            f"{'人物' if kind == 'character' else '场景' if kind == 'scene' else '道具'}"
            f"的形态类型只能是 {'／'.join(VARIANT_KINDS[a] for a in allowed)}，"
            f"给的是「{VARIANT_KINDS.get(variant_type, variant_type)}」")
    return variant_type


def create_variant(asset, payload):
    """建一个命名形态。重名回人可以改，不自动加序号——
    前端悄悄改名再说"已保存"，人拿到的就不是他打的那个名字。"""
    variant_type = _checked_variant_type(asset["kind"], payload.variant_type)
    name = payload.name.strip()
    if not name:
        raise AssetGenInvalid("形态名称不能为空")
    if variant_repo.exists_name(asset["id"], variant_type, name):
        raise VariantConflict(f"这条资产里已经有一个同名的「{name}」；"
                              "要改它就在原形态上编辑，不要再建一个")
    return variant_repo.create(asset_id=asset["id"], variant_type=variant_type, name=name,
                               prompt=payload.prompt.strip(), sort_order=payload.sort_order)


def update_variant(variant_id: int, payload):
    row = variant_repo.get(variant_id)
    if row is None:
        raise VariantMissing("形态不存在")
    asset = asset_repo.get(row["asset_id"])
    if asset is None:
        raise VariantMissing("形态所属的资产已经不在了")
    variant_type = _checked_variant_type(asset["kind"], payload.variant_type)
    name = payload.name.strip()
    if not name:
        raise AssetGenInvalid("形态名称不能为空")
    if variant_repo.exists_name(row["asset_id"], variant_type, name, exclude_id=variant_id):
        raise VariantConflict(f"这条资产里已经有一个同名的「{name}」")
    out = variant_repo.update(variant_id, variant_type=variant_type, name=name,
                              prompt=payload.prompt.strip(), sort_order=payload.sort_order)
    if out is None:
        raise VariantMissing("形态不存在")
    return out


def delete_variant(variant_id: int) -> list[str]:
    """删形态记录，并把它名下的图片路径交回调用方去清文件。

    只清 `FILES_DIR` 内的：路径来自库里，但库里可能被人写脏，
    出了门就不是"这个平台的文件"，不该由这个接口删。
    """
    row = variant_repo.delete(variant_id)
    if row is None:
        raise VariantMissing("形态不存在")
    try:
        images = json.loads(row["ref_images"] or "[]")
    except (TypeError, ValueError):
        images = []
    return [p for p in images if isinstance(p, str) and p]


# ---------- 提示词与结果解析 ----------

def _asset_context(payload, asset) -> dict:
    """拼提示词用的那份上下文：已有资产读库里的，草稿用界面传来的。

    两条路必须分开写清楚：草稿的三样是**客户端给的字符串**，只用来拼提示词，
    不会因为没有保存就落进任何表。
    """
    if asset is not None:
        return {"name": asset["name"], "summary": asset["summary"],
                "consistency_prompt": asset["consistency_prompt"], "kind": asset["kind"]}
    return {"name": (payload.draft_name or "").strip(),
            "summary": (payload.draft_summary or "").strip(),
            "consistency_prompt": (payload.draft_consistency_prompt or "").strip(),
            "kind": {"character_profile": "character", "scene_setting": "scene",
                     "prop_setting": "prop"}[payload.operation]}


def current_fields(operation: str, asset, payload) -> dict[str, str]:
    """这一类操作每个段落**现在有没有内容**。模式判断全靠它，所以只读服务端的事实。

    已有资产读库（`asset` 两列 ＋ 人物再读档案表）；草稿只看得见的就是界面传来的
    `draft_*` 三样，档案三段必然还是空的——这正是"草稿阶段点补全缺失＝把三段都写上"，
    和人的预期一致，不需要为草稿再编一份假档案。
    """
    ctx = _asset_context(payload, asset)
    out = {"summary": ctx["summary"] or "", "consistency_prompt": ctx["consistency_prompt"] or ""}
    for field in ("biography", "personality", "growth_arc"):
        out[field] = ""
    if operation == "character_profile" and asset is not None:
        profile = profile_repo.get(asset["id"])
        for field in ("biography", "personality", "growth_arc"):
            out[field] = profile[field] or ""
    return out


def sections_for(operation: str, mode: str, current: dict[str, str],
                 target_field: str = "") -> list[tuple[str, str]]:
    """这一次要模型输出哪几段。顺序照 `OUTPUT_SPEC`，不按 filled 的先后。

    两个"退化"是有意的：`polish` 遇上一格内容都没有时改无可改，`fill_missing`
    遇上全都填满时无空可补——那种情况按全量跑一遍，让模型给一份完整稿，
    比返回一个空段落清单、让人对着"没有可填的段落"发愣更有用。界面会把
    实际请求了哪几段回显出来，所以退化不会变成"偷偷重写了全部"。
    """
    spec = OUTPUT_SPEC[operation]
    if mode in ("full", "from_source"):
        return spec
    if mode == "expand":
        picked = [pair for pair in spec if pair[1] == target_field]
        return picked or spec
    if mode == "polish":
        filled = [pair for pair in spec if (current.get(pair[1]) or "").strip()]
        return filled or spec
    missing = [pair for pair in spec if not (current.get(pair[1]) or "").strip()]
    return missing or spec


def build_text_prompt(payload, asset, mode: str = "full",
                      sections: list[tuple[str, str]] | None = None) -> str:
    """文字设定作业的提示词。

    段名同时出现在这里和 `OUTPUT_SPEC`：模型不按标记回，解析就退化成"整段丢进一个框"，
    那时界面必须明说"没解析出分段"，而不是假装成功。

    `sections` 由 `sections_for` 按模式算好后传进来；不传就按全量。
    这里只**拼**，不再判模式——两处都判就会有一天判出两个结果。
    """
    ctx = _asset_context(payload, asset)
    operation = payload.operation
    markers = sections if sections is not None else OUTPUT_SPEC[operation]
    label = ASSET_GEN_OPERATIONS[operation]
    lines = [
        "你在为一部动漫短剧写前期设定。请按下面的标记分段输出，"
        "段名一字不差，不要加解释、不要加 Markdown 标记、不要合并段落。",
        f"任务：{label}（{TEXT_MODES.get(mode, mode)}）",
        f"名称：{ctx['name'] or '（还没命名）'}",
    ]
    if ctx["summary"]:
        lines.append(f"现有简介：{ctx['summary']}")
    if ctx["consistency_prompt"]:
        lines.append(f"已有视觉一致性提示词：{ctx['consistency_prompt']}")
    # 模式自己的那句话：这是"不同模式给不同指令"的落点，不是给界面看的装饰文字。
    lines.append(MODE_INSTRUCTIONS.get(mode) or MODE_INSTRUCTIONS["full"])
    instruction = (payload.instruction or "").strip()
    if instruction:
        lines.append(f"本次要求：{instruction}")
    lines.append(f"一致性提示词要覆盖：{GUIDANCE[operation]}")
    if mode == "from_source":
        excerpt = (getattr(payload, "source_excerpt", "") or "").strip()
        lines.append("原文片段（你要写的一切都只能从这里来）：")
        lines.append(excerpt[:6000])
    lines.append("需要输出的段落：")
    for marker, _field in markers:
        lines.append(f"{marker}（对应这一段的内容）")
    return "\n".join(lines)[:8000]


def parse_sections(operation: str, text: str,
                   sections: list[tuple[str, str]] | None = None) -> dict[str, str]:
    """按标记切结果。缺段就留空，多余的文字不硬塞进别的字段。

    标记必须是**一行的开头**（提示词里也是这么要求的）。这条比"全文第一次出现"
    更靠得住：模型偶尔会在履历里写"他说【性格】其实很稳"，那种行中间的出现
    按位置找会被当成段首，把后面真正的性格段吃掉——留空只是少填一格，
    吃错段是把话填错人。

    `sections` 给了就只切那几段（按模式请求的子集），不给就按这一类操作的全量。
    末段的边界是"下一个**被请求的**段首"，所以切子集时不会把没请求的段名当分隔符：
    模型如果自作主张多写了一段【性格】，那段文字会留在上一段的尾巴里，
    由 `generation_view` 的"没解析出／多出来"话术交代，而不是被静悄悄丢掉。
    """
    markers = sections if sections is not None else OUTPUT_SPEC[operation]
    found: list[tuple[int, str, int]] = []
    cursor = 0
    for marker, field in markers:
        hit = re.search(rf"^{re.escape(marker)}", text[cursor:], re.M)
        if hit is None:
            continue
        at = cursor + hit.start()
        found.append((at, field, len(marker)))
        cursor = at + len(marker)
    out: dict[str, str] = {}
    for pos, (at, field, width) in enumerate(found):
        start = at + width
        end = found[pos + 1][0] if pos + 1 < len(found) else len(text)
        out[field] = re.sub(r"\s+", " ", text[start:end]).strip()
    return out


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


def generation_summary(job) -> dict:
    """一条生成作业的界面摘要。列表与提交回执共用这一份，别再写第二个映射。"""
    ref = parse_refs(job)
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
    """这个项目（可选：这条资产）最近的资产生成作业。

    为什么要有这一条：页面把"在飞的作业"存在内存里，刷新一次就没了——
    人刚生成的候选看不见，会以为没跑。状态本来就在 `job` 表里，
    把它读回来比在浏览器里攒一份副本诚实。
    归属信息住在 `input_refs` 里（job 表没有 asset_id 列，也不该为一类目标加一列），
    所以先多取一些再按 refs 筛；limit 是给界面看的条数，不是扫描上限。
    """
    rows = job_repo.list([project_id], limit=max(int(limit) * 4, 40))
    out = []
    for row in rows:
        ref = parse_refs(row)
        if ref["operation"] not in ASSET_GEN_OPERATIONS:
            continue
        if asset_id is not None and ref["asset_id"] != int(asset_id):
            continue
        out.append(generation_summary(row))
        if len(out) >= limit:
            break
    return out


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
        capability = IMAGE_CAPABILITY
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


# ---------- 从成功作业产物导入资产图 ----------

def import_generated_image(asset, payload) -> dict:
    """把某条已成功图像作业的第 N 个产物存进资产（基础图或某个形态）。

    入参只有 `job_id + artifact_index`，**没有任何路径字段**：文件在哪由服务端
    从作业的产物清单里查。这一步是整个接口里最要紧的边界——
    一旦接受客户端给的路径，"导入一张图"就能变成"删一个服务器文件"。
    """
    job = job_repo.get(payload.job_id)
    if job is None:
        raise ImportUnusable("作业不存在")
    if job["capability"] != IMAGE_CAPABILITY:
        raise ImportUnusable(f"这条作业是{job['capability']}，不是图像生成；"
                             "资产参考图只能从图像作业的产物里存")
    if job["status"] != "succeeded":
        raise ImportUnusable(f"作业现在是「{_status_label(job['status'])}」，"
                             "只有成功结束的图像作业才有可存的产物")

    variant = None
    if payload.variant_id is not None:
        variant = variant_repo.get(payload.variant_id)
        if variant is None:
            raise VariantMissing("形态不存在")
        if variant["asset_id"] != asset["id"]:
            raise ImportUnusable("这个形态不属于这条资产")

    source = jobfiles.artifact_at(job["result_paths_json"], payload.artifact_index)
    if source is None or not source.is_file():
        raise ImportArtifactBad("这个序号在作业产物清单里不存在，或文件已经被清理")

    size = source.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ImportImageRejected(f"产物有 {size // 1024 // 1024}MB，"
                                  f"超过参考图上限 {MAX_IMAGE_BYTES // 1024 // 1024}MB")
    with source.open("rb") as fh:
        head = fh.read(16)
    ext = sniff(head)
    if ext is None:
        raise ImportImageRejected("产物不是 PNG / JPEG / WebP，不能当参考图存")

    # 复制而不是移动：作业的产物是生成中心的事实，也是产物审核签字的对象
    # （artifact_version 是按"路径＋存在＋字节数"算的）。搬走它等于偷偷让
    # 已经签过字的产物清单变成空目录，那一签就说不清了。
    target_dir = FILES_DIR / str(asset["id"])
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    copied = target_dir / filename
    shutil.copyfile(source, copied)
    rel = f"{asset['id']}/{filename}"

    try:
        if variant is None:
            row = asset_repo.add_image(asset["id"], rel)
            if row is None:
                raise ImportUnusable("资产不存在")
            images = json.loads(row["ref_images"] or "[]")
            return {"asset_id": asset["id"], "variant_id": None, "path": rel,
                    "ref_images": images, "image_count": len(images),
                    "source_job_id": job["id"]}
        row = variant_repo.add_image(variant["id"], rel, source_job_id=job["id"])
        if row is None:
            raise VariantMissing("形态不存在")
        images = json.loads(row["ref_images"] or "[]")
        return {"asset_id": asset["id"], "variant_id": row["id"], "path": rel,
                "ref_images": images, "image_count": len(images),
                "source_job_id": row["source_job_id"]}
    except Exception:
        # 库没写成，磁盘上那份就是孤儿——立刻收掉。反过来（库里有、文件没了）
        # 会在界面上显示一张打不开的图，更难查。
        copied.unlink(missing_ok=True)
        raise


def remove_imported_file(rel_path: str) -> bool:
    """删一张参考图时顺带清文件：只允许清 FILES_DIR 里面的路径。

    `resolve_within` 而不是 `startswith`：后者会把 `data/files` 匹配给
    `data/files-evil`，一条脏记录就能把删除范围带出门外（批次②A 的同一课）。
    """
    target = resolve_within(FILES_DIR, rel_path)
    if target is not None and target.is_file():
        target.unlink()
        return True
    return False
