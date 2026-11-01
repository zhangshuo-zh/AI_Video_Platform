"""文本抽取候选：提示词、输出解析与镜头 Markdown（W13）。

这一层只做纯文本的事，不碰数据库、不建 Job：
- 把一份正文拼成"只输出 JSON"的提示词；
- 把模型回的那段 JSON 收敛成受控的候选（字段白名单、长度上限、证据必须逐字）；
- 把镜头候选排成一张人读的 Markdown 表。

真正的提交、计价、落库与确认在 `services.py` 与 `routers/extraction.py`。
"""
from __future__ import annotations

import json
import math
import re

PROMPT_VERSION = "extract-v1"
MAX_INPUT_CHARS = 30000
MAX_ITEMS_PER_KIND = 60
KIND_LABELS = {"character": "角色", "scene": "场景", "prop": "道具", "shot": "镜头"}

# 模型常把 JSON 包在 ```json 里，或者前面写一句"好的"。
# 只剥这两种包装，不做"从任意文本里猜 JSON"——猜出来的东西没法证明出处。
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)
SUPPORTED_ITEM_KINDS = ("character", "scene", "prop", "shot")


class ExtractInvalid(Exception):
    """输入或模型输出不成立。路由按场景回 422／409。"""


def build_prompt(doc_title: str, content: str, instruction: str = "") -> tuple[str, dict]:
    """拼提示词与参数。`thinking` 关掉：要的是 JSON，不是思考过程。"""
    if len(content) > MAX_INPUT_CHARS:
        raise ExtractInvalid(
            f"正文 {len(content)} 字超过单次抽取上限 {MAX_INPUT_CHARS} 字；"
            "先分章或节选一段再抽。")
    if not content.strip():
        raise ExtractInvalid("这份文档还是空的，没有可抽取的正文")
    extra = (instruction or "").strip()[:2000]
    prompt = f"""你是中文动漫制作组的前期策划。请从下面这份正文里抽取可复用的制作资产候选。

只输出一个 JSON 对象，不要解释，不要 Markdown 代码块，不要写“好的”。
JSON 形状固定为：
{{
  "characters": [
    {{"name": "角色名", "aliases": ["别名"], "role": "主角/配角/群像",
      "appearance": "外形与服装", "personality": "性格要点",
      "suggested_prompt": "可喂给图像模型的平涂提示词",
      "confidence": 0.0,
      "evidence": {{"quote": "原文中逐字出现的一句"}}}}
  ],
  "scenes": [
    {{"name": "场景名", "time_of_day": "昼/夜/黄昏", "weather": "天气",
      "description": "空间、光线与氛围", "suggested_prompt": "场景提示词",
      "confidence": 0.0,
      "evidence": {{"quote": "原文中逐字出现的一句"}}}}
  ],
  "props": [
    {{"name": "道具名", "description": "材质、用途、特征",
      "suggested_prompt": "道具提示词", "confidence": 0.0,
      "evidence": {{"quote": "原文中逐字出现的一句"}}}}
  ],
  "shots": [
    {{"index": 1, "scene": "场景名", "summary": "这一镜发生什么",
      "shot_size": "远景/中景/近景/特写", "camera_move": "固定/推/拉/摇/移",
      "dialogue": ["台词"], "duration_hint": 5,
      "asset_refs": ["角色名或场景名"], "confidence": 0.0,
      "evidence": {{"quote": "原文中逐字出现的一句"}}}}
  ]
}}

规则：
1. 每类的条目最多 {MAX_ITEMS_PER_KIND} 条；没有就给空数组。
2. 每条 evidence.quote 必须从正文里逐字复制，不许改写、不许补主语；
   这句话要能让人回到原文核对。找不到原句的条目干脆不要。
3. confidence 只填 0—1 的小数，表示你自己的把握，不参与计费。
4. 角色名用正文里的叫法；同一人物的不同称呼放进 aliases。
5. 只写正文里出现或能直接推出的东西，不要新增设定。
6. 台词只抄正文里已有的对白，不改写。
"""
    if extra:
        prompt += f"\n额外要求（不得改变原文事实，最多 2000 字）：\n{extra}\n"
    prompt += f"\n正文（《{doc_title}》）：\n{content}\n"
    params = {"max_tokens": 8192, "temperature": 0.2, "thinking": False}
    return prompt, params


def estimate_quantity(chars: int, max_tokens: int = 8192) -> float:
    """按“输入字数 + 输出上限”向上取到 0.001 百万 token。

    和 W11 的文档 AI 用同一个口径：预计是上限，不是结算值；
    实际用量由回执写进 resources，账单仍是待核对。
    """
    total = max(1, int(chars) + int(max_tokens))
    return math.ceil(total / 1000) / 1000


def _text(value, limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return str(value).strip()[:limit]


def _list(value, limit: int = 10, each: int = 60) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = _text(item, each)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _confidence(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (0 <= number <= 1):
        return None
    return round(number, 3)


def _clean_payload(kind: str, raw: dict) -> dict:
    """字段白名单。模型多给的键一律丢掉，少给的补空串。"""
    if kind == "character":
        return {
            "name": _text(raw.get("name"), 80),
            "aliases": _list(raw.get("aliases")),
            "role": _text(raw.get("role"), 40),
            "appearance": _text(raw.get("appearance"), 600),
            "personality": _text(raw.get("personality"), 400),
            "suggested_prompt": _text(raw.get("suggested_prompt"), 800),
        }
    if kind == "scene":
        return {
            "name": _text(raw.get("name"), 80),
            "time_of_day": _text(raw.get("time_of_day"), 40),
            "weather": _text(raw.get("weather"), 40),
            "description": _text(raw.get("description"), 800),
            "suggested_prompt": _text(raw.get("suggested_prompt"), 800),
        }
    if kind == "prop":
        return {
            "name": _text(raw.get("name"), 80),
            "description": _text(raw.get("description"), 800),
            "suggested_prompt": _text(raw.get("suggested_prompt"), 800),
        }
    dialogue = raw.get("dialogue")
    if isinstance(dialogue, str):
        dialogue = [dialogue]
    if not isinstance(dialogue, list):
        dialogue = []
    shots = []
    for line in dialogue[:20]:
        text = _text(line, 200)
        if text:
            shots.append(text)
    try:
        index = int(raw.get("index"))
    except (TypeError, ValueError):
        index = 0
    try:
        duration = float(raw.get("duration_hint"))
    except (TypeError, ValueError):
        duration = 5.0
    return {
        "index": max(0, index),
        "scene": _text(raw.get("scene"), 80),
        "summary": _text(raw.get("summary"), 500),
        "shot_size": _text(raw.get("shot_size"), 40),
        "camera_move": _text(raw.get("camera_move"), 40),
        "dialogue": shots,
        "duration_hint": min(60.0, max(0.5, duration)),
        "asset_refs": _list(raw.get("asset_refs"), limit=20, each=80),
    }


def _find_evidence(content: str, raw, cursor: dict, kind: str) -> dict | None:
    """把模型给的 quote 对回正文，返回 {quote,start,end}。

    不接受模型自己报的偏移：它对不上就是错的证据，宁可丢掉这条。
    同一句在正文里出现多次时，从上次找到的位置往后找，让人按阅读顺序看候选。
    """
    quote = ""
    if isinstance(raw, dict):
        quote = _text(raw.get("quote"), 800)
    elif isinstance(raw, str):
        quote = _text(raw, 800)
    if not quote:
        return None
    start = content.find(quote, cursor.get(kind, 0))
    if start < 0:
        start = content.find(quote)
    if start < 0:
        return None
    cursor[kind] = start + len(quote)
    return {"quote": quote, "start": start, "end": start + len(quote)}


def parse_output(content: str, raw_text: str) -> tuple[list[dict], str]:
    """解析模型输出，返回 (items, note)。

    一条候选证据对不上就丢这一条，并在 note 里记数；不是整批失败——
    模型偶尔把一句话改写了一个字，不该让整章重跑一次。
    """
    text = (raw_text or "").strip()
    fence = _FENCE.match(text)
    if fence:
        text = fence.group(1).strip()
    if not text:
        raise ExtractInvalid("模型返回了空结果")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ExtractInvalid(f"模型输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ExtractInvalid("模型输出的顶层必须是对象")

    groups = {"character": "characters", "scene": "scenes",
              "prop": "props", "shot": "shots"}
    cursor: dict[str, int] = {}
    items: list[dict] = []
    skipped = 0
    for kind, key in groups.items():
        raw_items = data.get(key) or []
        if not isinstance(raw_items, list):
            continue
        kept = 0
        for raw in raw_items:
            if kept >= MAX_ITEMS_PER_KIND:
                break
            if not isinstance(raw, dict):
                skipped += 1
                continue
            payload = _clean_payload(kind, raw)
            if not payload.get("name") and not payload.get("summary"):
                skipped += 1
                continue
            evidence = _find_evidence(content, raw.get("evidence"), cursor, kind)
            if evidence is None:
                skipped += 1
                continue
            items.append({
                "kind": kind,
                "payload": payload,
                "confidence": _confidence(raw.get("confidence")),
                "evidence": evidence,
            })
            kept += 1
    note = f"共 {len(items)} 条候选"
    if skipped:
        note += f"；{skipped} 条因缺少原文证据或字段为空被跳过"
    return items, note


def shots_markdown(doc_title: str, items: list[dict]) -> str:
    """镜头候选只导出，不入 shot 表（接口草案的 v1 边界）。"""
    shots = [item for item in items if item["kind"] == "shot"]
    lines = [f"# {doc_title} · 镜头候选", ""]
    if not shots:
        lines.append("（这次没有抽到镜头候选。）")
        return "\n".join(lines) + "\n"
    lines.append("| # | 场景 | 内容 | 景别 | 运镜 | 台词 | 建议时长 | 引用 | 证据 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, item in enumerate(sorted(shots, key=lambda x: x["payload"].get("index") or 0), 1):
        p = item["payload"]
        dialogue = " / ".join(p.get("dialogue") or [])
        lines.append("| {i} | {scene} | {summary} | {size} | {move} | {dialogue} | {dur}s | {refs} | {quote} |".format(
            i=i,
            scene=(p.get("scene") or "—").replace("|", "\\|"),
            summary=(p.get("summary") or "—").replace("|", "\\|"),
            size=p.get("shot_size") or "—",
            move=p.get("camera_move") or "—",
            dialogue=(dialogue or "—").replace("|", "\\|"),
            dur=p.get("duration_hint") or "—",
            refs="、".join(p.get("asset_refs") or []) or "—",
            quote=(item["evidence"].get("quote") or "").replace("|", "\\|"),
        ))
    return "\n".join(lines) + "\n"
