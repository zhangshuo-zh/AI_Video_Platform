"""asset_studio prompts 业务域。"""
from __future__ import annotations
from models import ASSET_GEN_OPERATIONS
from repositories.content import profiles as profile_repo
import re
from .policy import GUIDANCE
from .policy import MODE_INSTRUCTIONS
from .policy import OUTPUT_SPEC
from .policy import TEXT_MODES



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
