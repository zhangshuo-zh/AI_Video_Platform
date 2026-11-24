"""模型中心：labels 业务域。"""
from __future__ import annotations
import adapters
import json


# 能力轴 → 基础标签。这一层是清单里 `Adapter.capability` 的直接译名，
# 不是新造的能力：适配器声明自己服务哪类活儿，标签就跟着哪类。
BASE_TAG = {"text": "文本生成", "image": "文生图", "video": "文生视频",
            "tts": "语音合成", "music": "音乐生成"}


# 收参考图时，按能力轴追加的标签。图生图／图生视频都要求"确实有一个图片输入参数"，
# 所以 mflux（flux1-schnell 没有 image 参数）拿不到"图生图"，
# 而 wan2.2-i2v-a14b 拿得到——差别完全来自登记的参数表。
IMAGE_TAG = {"image": "图生图", "video": "图生视频"}



def _loads(raw, default):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default



def derived_tags(capability: str, spec: dict) -> list[str]:
    """从清单已登记的事实里数出标签。只认参数表与计价表，不认模型名。

    2026-11-07 补：「结构化输出」。这一条原来在清单里**没有任何来源**，
    而 0035 的种子把它的要求写进了 4 个任务场景（character_bio / text_extraction /
    storyboard / …），于是那些场景永远 0 候选——不是人没配，是标签体系少了一档。
    现在的来源是 `ModelSpec.structured_output`：只有"运行时真的会按 schema 约束输出"
    的档才带它（今天只有本机 Ollama，`runners._ollama()` 会下发 `format`，
    云端 `cloud_run.py` 至今没有 response_format 分支，所以云端档一律不勾）。
    """
    tags = []
    base = BASE_TAG.get(capability)
    if base:
        tags.append(base)
    if spec.get("structured_output"):
        tags.append("结构化输出")
    if spec.get("vision_inputs"):
        tags.append("图像理解")
    if spec.get("image_inputs"):
        tags.append("参考图")
        extra = IMAGE_TAG.get(capability)
        if extra:
            tags.append(extra)
    return tags



def producible_tags(capability: str) -> set[str]:
    """这一条能力轴上，清单**有可能**产出的标签全集。

    用途只有一个：把"配不出来"和"还没配"分开。场景要「图生图」，而 image 轴
    上没有任何一档登记过图片输入参数时，无论人怎么点，那个场景都是 0 候选——
    这不是配置没做完，是清单少了一档能力来源。这两种在界面上必须说不同的话：
    前者要去写适配器（改代码），后者要去登记与探测（点界面）。

    口径与 `derived_tags()` 严格同源：同一批适配器、同一个函数数出来，
    不另写一份"哪些标签存在"的名单——那会立刻漂成第二真相。
    """
    out: set[str] = set()
    for adapter in adapters.ADAPTERS:
        if adapter.capability != capability:
            continue
        for spec in adapter.models:
            out.update(derived_tags(capability, spec.as_dict()))
    return out



def _tags_of(capability: str, spec: dict, reg: dict | None, kinds: set[str]) -> list[dict]:
    """派生标签 + 人工标签，各自带来源与可信度。"""
    out = [{"tag": t, "source": "manifest", "verified": True}
           for t in derived_tags(capability, spec)]
    for t in _loads((reg or {}).get("capabilities_json"), []):
        name = str(t).strip()
        if not name or any(o["tag"] == name for o in out):
            continue
        # 人工声明只有在这一档真跑过（real_call）或人看过产物（artifact_review）之后
        # 才允许说"已验证"；否则就是"我说它行"。
        out.append({"tag": name, "source": "manual",
                    "verified": bool({"real_call", "artifact_review"} & kinds)})
    return out


class PolicyDataInvalid(ValueError):
    """策略必需能力损坏，不能按没有要求继续筛选。"""


def parse_required(raw: object) -> list[str]:
    if not isinstance(raw, str):
        raise PolicyDataInvalid("策略必需能力不是字符串列表，请管理员修复配置")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise PolicyDataInvalid("策略必需能力 JSON 损坏，请管理员修复配置") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PolicyDataInvalid("策略必需能力不是字符串列表，请管理员修复配置")
    return value
