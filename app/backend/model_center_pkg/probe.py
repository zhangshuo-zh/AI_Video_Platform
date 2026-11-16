"""模型中心：probe 业务域。"""
from __future__ import annotations
import engines
from repositories.model_center import model_center
from .catalog_view import catalog, stats
from .slots import find_adapter



def probe_all(fresh: bool = False) -> dict:
    """现查一次服务型引擎与云端已登记网关，再把同一份目录与四数一起返回。"""
    import cloud_probe

    rows = [dict(r) for r in model_center.registrations()]
    # 按引擎去重：同一个 engine_code 可能有两三行登记（同机同服务的不同档），
    # 逐行探等于把同一个端点问三遍，而结论是同一台机器给的。
    probed: dict[str, dict] = {}
    for row in rows:
        adapter = find_adapter(row["capability"], row["backend"], row["provider"])
        if adapter is None or adapter.code not in engines.SERVICE_ENGINES:
            continue
        if adapter.code in probed:
            continue
        status = engines.probe(adapter.code, fresh=bool(fresh))
        probed[adapter.code] = {
            "state": status.state, "detail": status.detail,
            "endpoint": status.values.get("endpoint", ""),
            "engine_model": status.values.get("model", ""),
            "soft": status.soft,
        }
    # 缓存此刻已被上面这一轮填好，所以下面这次 catalog 里的探测全是缓存命中。
    items = catalog(probe=True)
    cloud = cloud_probe.probe_registered(rows, fresh=bool(fresh))
    return {
        "items": items,
        "stats": stats(items),
        "probed": [{"engine_code": code, **value} for code, value in probed.items()],
        "cloud": cloud,
        "note": "只读：探测不建 Job、不写库、不改默认轨",
    }
