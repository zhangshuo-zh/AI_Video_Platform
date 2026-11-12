"""资产生成归属的兼容解析，供服务与仓储筛选共用。"""
import json


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
