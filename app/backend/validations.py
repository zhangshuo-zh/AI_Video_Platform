"""模型验证的编排（P0：把"配置就绪"和"真的能产出"分开）。

为什么单独一个模块而不是塞进 `adapters.py`：`adapters.py` 是**清单**，
它回答"这一档允许什么参数、健康要看哪几件东西"；这里回答"这一档凭什么被认为能用过"。
两者混在一起，很容易把"文件都在"写成"模型可用"——那正是外部评审点名的口径事故。

三条边界：

1. **环境检查不发请求、不加载权重**，只复用 `adapters.health()` 的结论。
2. **真实调用不自己执行模型**：它读一条已经跑完的 Job。执行仍然只有 worker 一条路，
   成本仍然只在 Job 上；这里只是给那条 Job 盖一个"这是真调用，不是 mock"的戳。
3. **mock 永远不能升级成 real_call**。`execution_mode='mock'` 或 provider 是 `mock` 的
   作业一律拒记，否则测试环境里跑绿一次，生产界面就敢显示"真实调用已验证"。
"""
from __future__ import annotations

import hashlib

import adapters
import jobfiles
from models import VALIDATION_KINDS
from repositories.jobs import jobs as job_repo
from repositories.validation import validations as repo

KIND_LABELS = VALIDATION_KINDS


class ValidationInvalid(Exception):
    """验证请求本身不成立（缺 Job、是 mock、没确认计费）。路由层转 422。"""


def _row_of(row, key: str, default=None):
    return row[key] if key in row.keys() else default


def _artifact_digest(job) -> tuple[str, int | None]:
    """取第一个产物的 sha256 与字节数。只用于"确实落盘过"这一条证据，不做完整性体系。"""
    import json
    try:
        paths = json.loads(job["result_paths_json"] or "[]")
    except ValueError:
        return "", None
    if not isinstance(paths, list) or not paths:
        return "", None
    try:
        target = jobfiles._resolve(str(paths[0]))
    except jobfiles.JobPathError:
        return "", None
    if not target.is_file():
        return "", None
    digest = hashlib.sha256()
    with target.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), target.stat().st_size


def from_job(job_id: int, kind: str, created_by: int | None,
             detail: str = "", passed: bool | None = None) -> dict:
    """把一条跑完的作业登记成该槽位的验证记录。

    `real_call` 与 `target_machine` 都走这里：前者证明"这条链路真的产出过文件"，
    后者还需要人在界面上确认"跑的就是目标机"（本模块不猜机器）。
    `artifact_review` 是第五档——产物验收：链路产出过不等于产出得好，
    必须有人看过文件并给出通过／不通过的结论，`passed` 必须明说，服务端不猜。
    """
    if kind not in ("mock", "real_call", "target_machine", "artifact_review"):
        raise ValidationInvalid(f"不能从作业记成「{kind}」")
    if kind == "artifact_review" and passed is None:
        # 请求本身的形状问题先于作业状态判定：结论没给，看哪条作业都不成立
        raise ValidationInvalid("产物验收必须给出结论：通过还是不通过")
    job = job_repo.get(job_id)
    if job is None:
        raise ValidationInvalid("这条作业不存在或已删除")
    if job["status"] != "succeeded":
        raise ValidationInvalid("只有成功的作业能作为验证凭据")
    is_mock = job["execution_mode"] == "mock" or job["provider_code"] == "mock"
    if kind in ("real_call", "artifact_review") and is_mock:
        raise ValidationInvalid("假适配器的成功不能记成真实调用／产物验收")
    if kind == "mock" and not is_mock:
        raise ValidationInvalid("这条作业不是假适配器跑的；要记真实调用请选「真实调用」")
    if kind in ("real_call", "target_machine", "artifact_review") \
            and job["execution_mode"] == "cloud" and not job["user_confirmed_cloud"]:
        raise ValidationInvalid("云端调用必须先有计费确认记录")

    sha, size = _artifact_digest(job)
    if kind in ("real_call", "target_machine", "artifact_review") and not sha:
        raise ValidationInvalid("产物文件已不可读，不能记成真实调用／产物验收")

    resources = {}
    try:
        import json
        resources = json.loads(job["resources_json"] or "{}")
    except ValueError:
        resources = {}
    provider = resources.get("provider") if isinstance(resources.get("provider"), dict) else {}
    adapter = adapters.find(job["capability"], job["execution_mode"], job["provider_code"])
    checks = {
        "job_id": job["id"],
        "job_status": job["status"],
        "artifact_bytes": size,
        "artifact_sha256_present": bool(sha),
        "execution_mode": job["execution_mode"],
    }
    # 产物验收的 state 记的是人的结论：通过＝ok，不通过＝failed。
    # 其余三档本来就是"作业成功"这条事实的凭据，一律 ok。
    state = ("ok" if passed else "failed") if kind == "artifact_review" else "ok"
    if kind == "artifact_review" and not detail:
        detail = "产物人工验收通过。" if passed else "产物人工验收不通过。"
    row = repo.record({
        "capability": job["capability"], "backend": job["execution_mode"],
        "provider": job["provider_code"], "model": job["model_code"],
        "adapter_code": adapter.code if adapter else job["provider_code"],
        "manifest_version": job["adapter_version"], "kind": kind, "state": state,
        "detail": detail or f"作业 #{job['id']} 成功，产物校验通过。",
        "checks": checks,
        "params": json_loads(job["params_json"]),
        "job_id": job["id"],
        "provider_request_id": job["provider_request_id"],
        "http_status": provider.get("http_status"),
        "latency_ms": provider.get("latency_ms") or job["elapsed_ms"],
        "artifact_path": _first_path(job),
        "artifact_sha256": sha or None,
        "cost_micros": job["cost_micros"], "currency": job["currency"],
        "paid_confirmed": bool(job["user_confirmed_cloud"]),
        "created_by": created_by,
    })
    return {"ok": True, "validation": row}


def env_check(row, created_by: int | None, health: dict | None = None) -> dict:
    """跑一次环境检查并留痕。结论默认来自 `adapters.health()`，这里只负责记录。

    `health` 是给**结论不由 `adapters.health()` 说得出来**的调用方留的口子：
    远程自建（backend='remote'）那一档，`adapters.health()` 拿本机文件系统
    去回答别人的机器，得到的永远是"清单里没这一家"或一条错位的结论——
    它的真相是"平台查不了，归引擎实例那张表"，由 `model_center._health_of()`
    回答。传进来就用它，不传仍走 `adapters.health()`（其余五档的老路径一字未改）。
    """
    if health is None:
        health = adapters.health(row)
    adapter = health.get("adapter") or {}
    record = repo.record({
        "capability": row["capability"], "backend": row["backend"],
        "provider": row["provider"], "model": row["model"],
        "adapter_code": adapter.get("code", "") if isinstance(adapter, dict) else "",
        "manifest_version": adapters.MANIFEST_VERSION,
        "kind": "env_check", "state": health.get("state", "unknown"),
        "detail": health.get("detail", ""),
        "checks": {k: v for k, v in health.items() if k not in ("adapter", "params")},
        "params": {}, "created_by": created_by,
    })
    return {"ok": health.get("state") == "ok", "validation": record}


def json_loads(text: str) -> dict:
    import json
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _first_path(job) -> str:
    import json
    try:
        paths = json.loads(job["result_paths_json"] or "[]")
    except ValueError:
        return ""
    return str(paths[0]) if isinstance(paths, list) and paths else ""


def summary_for(row, latest: dict | None = None) -> dict:
    """某一槽位的五类 provenance 摘要。界面按这个决定敢不敢说"生产可用"。

    `latest` 是给"一整页槽位一起渲染"的调用方留的口子：传 `repo.latest_matrix()`
    里那一格，五个槽位档就是一趟查询；不传则自己按槽位读（单槽位调用，
    比如刚验证完只看这一条）。判断规则只有一份，两种走法读的是同一批行。

    0043 起自带读路径**按档取**：row 的 `model` 就是这一槽位当前选的那一档，
    记录里 model 不是它的根本不参与判定——"1.3B 验过不等于 5B 验过"是
    WHERE 的形状，不再是调用方自觉对一遍的纪律。显式传了 `latest` 的
    （列表页批量渲染）仍然信传入值：那些调用方已经按档给了。
    """
    if latest is None:
        # row 可能是没有 model 列的调用方字典（测试夹具、脚本）：按档取的前提是
        # 这一行真的带档。没有就不筛——与 0043 之前的口径一致，而不是抛错。
        model = row["model"] if "model" in row.keys() else None
        latest = repo.latest_by_kind(row["capability"], row["backend"],
                                     row["provider"], model)

    def brief(kind: str) -> dict:
        rec = latest.get(kind)
        if rec is None:
            return {"state": "never", "at": "", "detail": "没有记录"}
        return {"state": rec["state"], "at": rec["created_at"],
                "detail": rec["detail"], "job_id": rec["job_id"]}

    out = {kind: brief(kind) for kind in KIND_LABELS}
    # 生产可用的唯一口径：环境齐＋真实调用过＋产物验收通过。
    # 目标机基准没跑就是没跑，不猜；产物没验过也不能顶上。
    out["production_ready"] = (out["env_check"]["state"] == "ok"
                               and out["real_call"]["state"] == "ok"
                               and out["artifact_review"]["state"] == "ok")
    out["target_machine_ready"] = out["target_machine"]["state"] == "ok"
    return out


def badge(summary: dict) -> tuple[str, str]:
    """给界面的一枚标签：(state, at)。

    顺序就是可信度顺序，**从高到低**；产物验收比真实调用多一道人眼，
    所以排更高；环境检查永远不能冒充真实调用。
    """
    for kind in ("target_machine", "artifact_review", "real_call", "mock", "env_check"):
        item = summary.get(kind) or {}
        if item.get("state") == "ok":
            return kind, item.get("at") or ""
    return "never", ""


def recent(limit: int = 50) -> list[dict]:
    rows = repo.list_recent(limit)
    out = []
    for r in rows:
        item = dict(r)
        out.append(item)
    return out
