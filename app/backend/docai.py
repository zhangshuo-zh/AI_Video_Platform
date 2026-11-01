"""文档 AI 操作：提示词、范围、应用语义与 diff（W11）。

这一层只做**纯文本**的事：给定正文、选中范围和操作，拼出要交给文本模型的
提示词；模型跑完后，再把结果按事先定死的语义拼回正文。它不碰数据库、不写
文件——真正落盘仍然走 `services.save_content()`，所以版本冲突、编辑锁与
Git 提交这三道关一个都不会绕过去。

六种操作的落点写死在这里，不交给模型自己决定：

| 操作 | 默认范围 | apply_mode | 说明 |
|---|---|---|---|
| 起草 draft | 全文 | replace_document | 用标题/要求起草；有旧正文时作为参考 |
| 扩写 expand | 选中（无选中则全文） | replace_selection / replace_document | 只扩写选中的段落 |
| 续写 continue | 全文 | append | 接在正文最后，另起一段 |
| 润色 polish | 选中（无选中则全文） | replace_selection / replace_document | 只改表达，不改事实 |
| 剧本化 scriptify | 选中（无选中则全文） | replace_selection / replace_document | 小说段落改写成剧本格式 |
| 提示词 prompt | 选中（无选中则全文） | replace_selection / replace_document | 从正文提炼镜头提示词 |

为什么 apply 不在客户端算：客户端传来的“新正文”无法证明是从这份文档改出来的；
这里用 Job 里冻结的 base_version 与选中偏移，服务端重新拼一遍再写。
"""
from __future__ import annotations

import math
from decimal import Decimal
from difflib import unified_diff

# 单次 AI 操作的输入上限。不是模型的上下文上限，而是“这次要花多少钱”的上限：
# 一份 1MB 的文档整篇丢给模型，一次就可能几十元；超过就让人先选一段。
MAX_INPUT_CHARS = 30000
DIFF_MAX_CHARS = 60000

OPERATIONS: dict[str, dict] = {
    "draft": {
        "label": "起草",
        "scope": "document",
        "base_apply": "replace_document",
        "max_tokens": 4096,
        "hint": "按标题与要求起一段新正文；已有内容只作参考，应用后会替换整篇。",
    },
    "expand": {
        "label": "扩写",
        "scope": "any",
        "base_apply": "replace_selection",
        "max_tokens": 4096,
        "hint": "把选中的段落写得更充分；没选中就处理全文。",
    },
    "continue": {
        "label": "续写",
        "scope": "document",
        "base_apply": "append",
        "max_tokens": 4096,
        "hint": "紧接正文最后一句往后写，应用时追加到文末。",
    },
    "polish": {
        "label": "润色",
        "scope": "any",
        "base_apply": "replace_selection",
        "max_tokens": 4096,
        "hint": "只改表达与节奏，不改事实和人物口吻；没选中就处理全文。",
    },
    "scriptify": {
        "label": "剧本化",
        "scope": "any",
        "base_apply": "replace_selection",
        "max_tokens": 8192,
        "hint": "把小说段落改成场景/动作/对白的剧本格式。",
    },
    "prompt": {
        "label": "提示词",
        "scope": "any",
        "base_apply": "replace_selection",
        "max_tokens": 2048,
        "hint": "从正文提炼可用的镜头提示词，每镜一行。",
    },
}


class DocAiInvalid(Exception):
    """输入不成立（范围、操作、空正文）。路由回 422。"""


def operation_info(code: str) -> dict:
    info = OPERATIONS.get(code)
    if info is None:
        raise DocAiInvalid(f"不认识的 AI 操作：{code}")
    return info


def operation_label(code: str) -> str:
    return OPERATIONS.get(code, {}).get("label", code)


def _clip(value: str, limit: int) -> str:
    value = str(value or "")
    return value[:limit]


def resolve_scope(content: str, operation: str, scope: str,
                  start: int, end: int, selection_text: str = "") -> dict:
    """把前端给的范围收敛成服务端认可的一对偏移。

    前端传 `selection_text` 是第二重校验：偏移对不上时，如果这段文字在正文里
    只出现一次，就用找到的位置；出现多次或找不到就拒——宁可让人多选几个字，
    也不猜他要改哪一段。UTF-16 与 Python 码点在这类中文稿上通常一致，
    但 emoji 会让偏移差一截，所以文字比对兜底。
    """
    info = operation_info(operation)
    if info["scope"] == "document":
        scope = "document"

    if scope not in ("document", "selection"):
        raise DocAiInvalid("范围只有 document / selection 两种")

    if scope == "document":
        if not content and operation != "draft":
            raise DocAiInvalid("这份文档还是空的，先写一点正文再做这个操作")
        if len(content) > MAX_INPUT_CHARS:
            raise DocAiInvalid(
                f"全文 {len(content)} 字超过单次 AI 操作上限 {MAX_INPUT_CHARS} 字；"
                f"先选中要处理的段落再试")
        return {"scope": "document", "start": 0, "end": len(content),
                "text": content, "apply_mode": info["base_apply"]
                if info["base_apply"] != "replace_selection" else "replace_document"}

    text = str(selection_text or "")
    start, end = max(0, int(start or 0)), max(0, int(end or 0))
    if start > end:
        start, end = end, start
    if 0 <= start <= end <= len(content) and content[start:end] == text:
        selected = text
    elif text:
        first = content.find(text)
        if first < 0:
            raise DocAiInvalid("选中的文字不在当前正文里（可能刚被改过），请重新选择")
        if content.count(text) > 1:
            raise DocAiInvalid("选中的文字在正文里出现了不止一次；多选几个字再试")
        start, end, selected = first, first + len(text), text
    else:
        raise DocAiInvalid("没有拿到选中的文字；请先选中一段正文")

    if not selected.strip():
        raise DocAiInvalid("选中的内容是空白")
    if len(selected) > MAX_INPUT_CHARS:
        raise DocAiInvalid(f"选中 {len(selected)} 字超过单次上限 {MAX_INPUT_CHARS} 字")
    return {"scope": "selection", "start": start, "end": end,
            "text": selected, "apply_mode": "replace_selection"}


def _common(instruction: str) -> str:
    extra = _clip(instruction, 2000).strip()
    tail = ("\n额外要求（必须遵守，但不许改变下面给出的原文事实）：" + extra) if extra else ""
    return (tail + "\n只输出结果本身，不要解释，不要用 Markdown 代码块包裹，"
                   "不要在开头写“好的”“以下是”。")


def build_prompt(operation: str, title: str, content: str,
                 selected: str, instruction: str) -> tuple[str, dict]:
    """按操作拼提示词，返回 (prompt, params)。

    参数只给 max_tokens 与 temperature；两者都在 DeepSeek 清单的白名单里。
    `thinking` 固定关：文档改写要的是正文，不是思考过程。
    """
    info = operation_info(operation)
    title = _clip(title, 120) or "未命名"
    common = _common(instruction)
    if operation == "draft":
        body = content.strip() or "（还没有正文）"
        prompt = (
            f"你是一位中文小说/剧本写作助手。请为《{title}》起草一段正文。\n"
            f"已有正文（只作参考，可为空）：\n{body}\n"
            f"要求：写出可以直接放进作品里的正文，人物、场景、语气要统一。"
            + common)
    elif operation == "continue":
        prompt = (
            f"请续写《{title}》下面的正文，紧接最后一句，保持人称、语气与世界观。\n"
            f"已有正文：\n{content}\n"
            f"只输出续写出来的那部分，不要重复已有内容，不要总结。" + common)
    elif operation == "expand":
        prompt = (
            f"请扩写《{title}》下面这段文字，保留原意、事实、人物与时间顺序，"
            f"可以补充动作、感官和细节，但不要新增情节转折。\n"
            f"待扩写的段落：\n{selected}\n"
            f"只输出扩写后的段落。" + common)
    elif operation == "polish":
        prompt = (
            f"请润色《{title}》下面这段文字，保持原意、事实与人物口吻，"
            f"只改表达、节奏和用词，不增删情节。\n"
            f"待润色的段落：\n{selected}\n"
            f"只输出润色后的段落。" + common)
    elif operation == "scriptify":
        prompt = (
            f"请把《{title}》下面这段小说改写成剧本格式：先给场景标题"
            f"（地点/时间），再写动作与对白；对白前写人物名，动作用现在时。\n"
            f"小说原文：\n{selected}\n"
            f"只输出剧本正文。" + common)
    elif operation == "prompt":
        prompt = (
            f"请从《{title}》下面这段正文提炼可用于文生视频的镜头提示词。\n"
            f"每镜一行，格式：镜号｜画面主体与动作｜镜头运动/景别｜光线与氛围。"
            f"只写画面能看见的东西，不要写心理活动，不要解释。\n"
            f"正文：\n{selected}" + common)
    else:  # operation_info 已经挡过，这里只是防御
        raise DocAiInvalid(f"不认识的 AI 操作：{operation}")
    params = {"max_tokens": info["max_tokens"], "temperature": 0.7, "thinking": False}
    return prompt, params


def estimate_quantity(chars: int, max_tokens: int) -> float:
    """按“输入字数 + 输出上限”估算百万 token 数，向上取到 0.001。

    向上取而不是四舍五入：这是给用户看的预计上限，宁可多报一档，
    也不要事后因为实际用量超过预计而显得平台在藏价。
    """
    raw = max(0, int(chars)) + max(1, int(max_tokens))
    thousandths = math.ceil(raw / 1000)
    value = Decimal(max(1, thousandths)) / Decimal(1000)
    return float(value.quantize(Decimal("0.001")))


def apply_text(base: str, result: str, apply_mode: str,
               start: int = 0, end: int = 0) -> str:
    """把模型输出拼回正文。只做字符串拼接，不写盘。"""
    text = str(result or "").strip()
    if not text:
        raise DocAiInvalid("AI 返回了空结果，不能应用")
    if apply_mode == "replace_document":
        return text
    if apply_mode == "append":
        head = str(base or "").rstrip()
        return (head + "\n\n" + text) if head else text
    if apply_mode == "replace_selection":
        base = str(base or "")
        start, end = max(0, int(start)), max(0, int(end))
        if not (0 <= start <= end <= len(base)):
            raise DocAiInvalid("选中范围越出了正文边界，不能应用")
        return base[:start] + text + base[end:]
    raise DocAiInvalid(f"不认识的 apply_mode：{apply_mode}")


def diff_text(base: str, applied: str) -> str:
    """统一 diff。行首 +/- 足够人判断；太长就截断，避免把界面拖死。"""
    lines = list(unified_diff(
        str(base or "").splitlines(),
        str(applied or "").splitlines(),
        fromfile="盘上正文", tofile="AI 应用后", lineterm="", n=3))
    text = "\n".join(lines)
    if len(text) > DIFF_MAX_CHARS:
        return text[:DIFF_MAX_CHARS] + f"\n…（差异过长，只显示前 {DIFF_MAX_CHARS} 字符）"
    return text
