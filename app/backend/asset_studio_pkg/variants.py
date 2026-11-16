"""asset_studio variants 业务域。"""
from __future__ import annotations
from models import VARIANT_KINDS
from repositories.content import assets as asset_repo
from repositories.content import profiles as profile_repo
from repositories.content import variants as variant_repo
import json
from .policy import AssetGenInvalid
from .policy import ProfileInvalid
from .policy import VARIANT_TYPES_BY_KIND
from .policy import VariantConflict
from .policy import VariantMissing



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
        text_prompt=(payload.text_prompt or "").strip(),
        tts_voice=(getattr(payload, "tts_voice", "") or "").strip(),
        tts_rate=float(getattr(payload, "tts_rate", 1.0) or 1.0),
        tts_mood=(getattr(payload, "tts_mood", "") or "").strip())



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
