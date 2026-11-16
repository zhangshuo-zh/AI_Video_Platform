"""模型中心：scenes 业务域。"""
from __future__ import annotations
from repositories.model_center import model_center
from .catalog_view import catalog
from .labels import _loads, producible_tags
from .slots import STATUS_LABEL



def resolve_scene(scene_key: str) -> dict:
    """场景 → 候选与挡路原因。只读：不选路、不写库、不启动任务（MC1 裁甲）。"""
    row = None
    for p in model_center.policies():
        if p["scene_key"] == scene_key:
            row = dict(p)
            break
    if row is None:
        return {"scene_key": scene_key, "found": False, "candidates": [],
                "blockers": [f"没有这个任务场景：{scene_key}"]}
    required = _loads(row["required_json"], [])
    return _resolve(row, required)



def _resolve(row: dict, required: list[str]) -> dict:
    """候选＝登记且启用且健康就绪且**能力齐**；其余一律进 blockers 并说清缺哪一项。

    为什么状态不合格的也要点名（而不是悄悄从列表里消失）：任务书 §七 规则 7 要的是
    "用户显式指定模型时如果无法执行，说明具体原因"。人最不该看到的是
    "我明明登记了它，候选里却没有，也没人告诉我为什么"。
    备用轨同样只进 candidates 列表并标 role=fallback，不给它任何自动生效的通道。
    """
    out: dict = {
        "scene_key": row["scene_key"], "scene_label": row["scene_label"],
        "capability": row["capability"], "required": required, "found": True,
        "preference": row["preference"],
        "slot_provider": str(row.get("slot_provider") or ""),
        "slot_model": str(row.get("slot_model") or ""),
        "enabled": bool(row["enabled"]),
        "cloud_confirm_required": bool(row["cloud_confirm_required"]),
        "candidates": [], "blockers": [],
        "policy_note": "本接口只回答「谁可能行」；选路仍由 resolve_provider() 按 "
                       "provider_config.is_default 决定，备用轨不会自动顶上。",
    }
    # 场景要的标签里，有哪几档**整个清单都产不出来**。这是"配不出来"与
    # "还没配"的分界：前者要改 adapters.py（写清这一档凭哪个已登记的参数收
    # 条件图），后者只是去登记、去点探测。两种必须说不同的话——都写成
    # "没有候选"，人就会对着一个永远点不亮的场景反复配置（2026-11-07 审查里
    # character_variant / text_extraction 等 4 个场景正是这样）。
    producible = producible_tags(row["capability"])
    for r in catalog():
        if r["capability"] != row["capability"] or not r["registered"]:
            continue
        label = r["display_name"] or r["model_label"] or r["model_code"]
        reasons: list[str] = []
        if r["status"] == "disabled":
            reasons.append("已禁用")
        elif r["status"] == "registration_only":
            # 这一档在本机登记了，但清单里没有它——点多少次「登记」都没用，
            # 得去 adapters.py 补规格，或者撤掉这条登记。措辞必须让这一步显形。
            reasons.append("清单里没有这一档（参数与运行方式都不猜，要去 adapters.py 补规格）")
        elif r["status"] in ("dependency_missing", "connection_failed"):
            reasons.append(f"{STATUS_LABEL.get(r['status'], r['status'])}：{r['health']['detail']}")
        elif r["status"] == "not_installed":
            reasons.append(STATUS_LABEL["not_installed"])
        elif r["status"] == "unchecked" and not r["health"].get("probe_deferred"):
            # 真·未检查（平台自己也查不了）：照旧点名。
            reasons.append(STATUS_LABEL["unchecked"])
        # 「列表里还没现查过」（probe_deferred）**不算**挡路：目录本身不出网（见
        # catalog 的注解），服务型引擎在列表里必然是这一档——把它写成挡路原因，
        # 等于让每个用本机引擎的场景永远 0 候选，而真相只是"还没人点过一次探测"。
        # 候选里会带「未检查」徽章，人一眼看得出这一条没现查过。
        # unverified 同样允许：没验过不等于不能用，界面带着"未验证"徽章进候选。
        # 人工声明可以补齐候选，**但不能补"清单里根本没有这一档"**：
        # 那等于用一句自我声明冒充能力证明，与这一页"健康≠验证"的纪律自相矛盾。
        # 判据是 `producible_tags(capability)`——清单这一轴有可能产出的标签全集。
        # 注意：被剔掉的只是"清单这一轴根本产不出来"的那些声明；清单能产出、
        # 只是这一档没登记的，声明仍然算数（那是人的专业判断，不是凭空造能力）。
        have = {t["tag"] for t in r["tags"]
                if t["source"] == "manifest" or t["tag"] in producible}
        missing = [t for t in required if t not in have]
        if missing:
            hard = [t for t in missing if t not in producible]
            soft = [t for t in missing if t in producible]
            if hard:
                reasons.append("缺能力 " + "、".join(hard)
                               + "（清单里没有任何一档能产出它；人工声明不替代清单事实）")
            if soft:
                reasons.append("缺能力 " + "、".join(soft))
        if reasons:
            # 名字之外带上 model_code：人扫"我登记的那一档为什么不在候选里"时，
            # 只有显示名会认不出来（显示名是可以随便改的）
            out["blockers"].append(f"{label}（{r['model_code']}）：" + "；".join(reasons))
            continue
        role = "fallback" if (r["provider"] == row["fallback_provider"]
                              and r["model_code"] == row["fallback_model"]) else "primary"
        out["candidates"].append({
            "registration_id": r["registration_id"], "provider": r["provider"],
            "model_code": r["model_code"], "backend": r["backend"],
            "label": label, "role": role, "status": r["status"],
            "status_label": STATUS_LABEL.get(r["status"]),
            "needs_cloud_confirm": r["backend"] == "cloud",
        })
    impossible = [t for t in required if t not in producible]
    out["required_producible"] = not impossible
    out["impossible_tags"] = impossible
    if impossible:
        out["blockers"].append(
            "这几项必需能力在清单里**没有任何来源**：" + "、".join(impossible) + "。"
            + "「" + row["scene_label"] + "」因此在任何配置下都不可能有候选——"
            + "要让它有解，得先在 adapters.py 里给这一轴登记一档真的具备该能力的模型，"
            + "或者在策略里把这一项从「必需」降下来。平台不会替你把它当成可用")
    elif not out["candidates"]:
        out["blockers"].append(
            f"场景「{row['scene_label']}」没有可用候选：这一类能力里"
            f"没有一档同时满足 {'、'.join(required) or '（无必需能力）'}")
    return out
