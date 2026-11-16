"""执行器 common 域。"""
from __future__ import annotations

from pathlib import Path

import adapters

from .protocol import RunnerUnavailable


def _need_runtime(probe) -> None:
    if probe.state != "ok":
        raise RunnerUnavailable(probe.detail)




def _spec_default(job, model_code: str, name: str, fallback):
    """作业参数里没写这一项时，回落到清单里的默认值。

    为什么要回落：产线节点提交的作业 `params` 是空的（模板不写死参数，
    `pipeline._submit_job_node` 只带模板 input 里真有的那几项）。而清单给
    FastMetal 1.3B 标的 `fast_spatial` 默认是 True——那是 W09 在 16GB 上的实测结论
    （640×352×81 不开 fast-spatial 解码就 OOM）。不回落等于把这条实测丢掉：
    作业会以"模型跑崩了"的样貌失败，人看不出是自己没填参数还是平台漏了默认。
    规格仍以 `adapters.py` 为唯一来源，这里只照读一份数字，不另写第二份。
    """
    adapter = adapters.find(job["capability"], job["execution_mode"],
                           job["provider_code"])
    if adapter is None:
        return fallback
    for model in adapter.models or ():
        if model.code != model_code:
            continue
        for param in model.params or ():
            if param.name == name and param.default is not None:
                return param.default
    return fallback




def _flag(params: dict, name: str, job, model_code: str) -> bool:
    """布尔开关：显式给了就照给，没给就照清单默认。"""
    value = params.get(name)
    if value is None:
        value = _spec_default(job, model_code, name, False)
    return bool(value)




def _mp4_path(outputs: Path, job_id: int, label: str) -> Path:
    return outputs / f"{job_id}-{label}.mp4"




def _metrics_path(outputs: Path, job_id: int) -> Path:
    return outputs / f"{job_id}-metrics.json"
