"""执行器 text 域。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import engines

from .cloud import _secret_envs
from .protocol import LLM_RUNNER, RunnerUnavailable, RunSpec


# ---------- 文本：本机常驻 LLM 服务（A4：Ollama）----------
# 与 draw_things 同族（端点住 `engine_instance`，健康由 `engines.probe()` 现查），
# 但**不过** `_service_engine_gate` 那道"当前模型对不对"的核对：
# 这一族的模型名是每次请求里带的，不存在"服务自己选着另一档"这件事。
# 分族的那一行判断在 `engines.MODEL_GATED_ENGINES`，理由写在那里。
def _ollama(job, params: dict, outputs: Path, model: str) -> RunSpec:
    # 同 _draw_things：起跑前现查，不吃列表那一层给页面用的短缓存。
    status = engines.probe("ollama", fresh=True)
    if status.state != "ok":
        raise RunnerUnavailable(f"本机模型服务现在不能用：{status.detail}")
    endpoint = status.values.get("endpoint") or ""
    out = outputs / f"{job['id']}-ollama.md"
    request_path = outputs / f"{job['id']}-ollama-request.json"
    receipt = outputs / f"{job['id']}-ollama-receipt.json"

    request = {"endpoint": endpoint, "path": "/api/chat", "provider": "ollama",
               "model": model, "prompt": job["prompt_snapshot"],
               "params": {"max_tokens": params.get("max_tokens"),
                          "temperature": params.get("temperature"),
                          "think": params.get("think")},
               "output": str(out), "receipt": str(receipt), "timeout": 300}
    # 计划类请求要的是**结构化输出**：提示词里写着"只输出一个 JSON 数组"。
    # 那一句是给人读的约束，模型不一定守；`format` 才是服务端会守的那一道。
    # 所以这里按提示词的特征把它补上——补的依据是"这段提示词在要 JSON"，
    # 不是"这个 provider 大概支持 JSON"，后者是猜。
    prompt = job["prompt_snapshot"] or ""
    if "JSON 数组" in prompt and "只输出一个 JSON 数组" in prompt:
        request["format"] = {"type": "array", "items": {"type": "object"}}
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    argv = [sys.executable, str(LLM_RUNNER), str(request_path)]
    return RunSpec(
        runner="ollama", argv=argv, cwd=outputs, expected=[out], media="text",
        meta={"adapter": "ollama", "model": model, "endpoint": endpoint,
              "request_path": str(request_path), "result_path": str(receipt),
              "resident_models": status.values.get("models") or [],
              "formatted": request.get("format") is not None},
        pass_env=_secret_envs(request))
