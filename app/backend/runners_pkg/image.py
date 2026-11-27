"""执行器 image 域。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import engines
import runtime
from repositories.engine_instances import engine_instances

from .cloud import _secret_envs
from .common import _need_runtime
from .inputs import _resolve_input_image
from .protocol import ENGINE_HTTP_RUNNER, RunnerUnavailable, RunSpec


def _mflux(job, params: dict, outputs: Path) -> RunSpec:
    probe = runtime.probe_mflux()
    _need_runtime(probe)
    v = probe.values
    out = outputs / f"{job['id']}-mflux.png"
    seed = int(params.get("seed", 42))
    width = int(params.get("width", 1024))
    height = int(params.get("height", 1024))
    steps = int(params.get("steps", 4))
    # 权重用社区转好的 Q4 档，不用 `--model schnell` 拉官方仓库：
    #   1) 官方 black-forest-labs/FLUX.1-schnell 是 gated 仓库，worker 没有 HF token 就 401；
    #   2) 官方整包 33.7GB，且 `--quantize 8` 在 16GB 机器上装不下。
    # Q4 是同一套 FLUX.1-schnell 权重的 MLX 量化转换（Apache-2.0），9.6GB，
    # 量化级别已经写进权重，所以不能再传 `--quantize`（传了会和已存的量化打架）。
    # 2026-11-05 在 M4 Air 16GB 实测 512×512、4 步：墙钟 102s、MLX 峰值 9.97GB、
    # 进程峰值 footprint 12.3GB；1024×1024 未测，按 512 的余量不要在这台机器上默认跑。
    argv = [v["cli"], "--model", "mflux-community/flux-1-schnell-mflux-q4",
            "--base-model", "schnell",
            "--prompt", job["prompt_snapshot"],
            "--steps", str(steps), "--seed", str(seed),
            "--width", str(width), "--height", str(height),
            "--output", str(out)]
    return RunSpec(
        runner="mflux", argv=argv, cwd=outputs, expected=[out], media="image",
        meta={"adapter": "mflux", "model": job["model_code"],
              "cli": v["cli"], "seed": seed,
              "width": width, "height": height, "steps": steps})




# ---------- 本机常驻服务型引擎（W-B：Draw Things）----------
# 与上面几档的区别只在"怎么把活儿交出去"：这里是往一个已经在跑的本地服务
# 发一次 HTTP，产物从响应体的 base64 里落盘。请求与落盘都在子进程
# （`engine_http_run.py`）里做，worker 侧沿用 `_pump` 那套超时与取消，
# 长不出第二套执行封装。
DRAW_THINGS_PATH = "/sdapi/v1/txt2img"



def _comfyui_workflow_payload(
    prompt_text: str,
    params: dict,
    *,
    init_filename: str | None = None,
) -> dict:
    """组装或参数化注入 ComfyUI API Workflow JSON 节点图。"""
    raw_wf = str(params.get("workflow_json") or "").strip()
    width = int(params.get("width", 1024) or 1024)
    height = int(params.get("height", 1024) or 1024)
    steps = int(params.get("steps", 20) or 20)
    cfg = float(7.0 if params.get("cfg") is None else params["cfg"])
    seed = int(params["seed"]) if params.get("seed") is not None else 42
    denoise = float(0.75 if params.get("denoise") is None else params["denoise"])
    negative = str(params.get("negative_prompt") or "").strip()
    checkpoint = str(params.get("checkpoint") or "").strip()
    image_val = init_filename or "__UPLOADED_IMAGE__"

    if raw_wf:
        try:
            wf = json.loads(raw_wf)
        except ValueError as exc:
            raise RunnerUnavailable(f"ComfyUI workflow_json 不是合法的 JSON：{exc}") from exc
        if not isinstance(wf, dict) or not wf:
            raise RunnerUnavailable("ComfyUI workflow_json 必须是非空节点映射对象")

        exact_map: dict[str, object] = {
            "{{prompt}}": prompt_text,
            "{{negative_prompt}}": negative,
            "{{width}}": width,
            "{{height}}": height,
            "{{steps}}": steps,
            "{{cfg}}": cfg,
            "{{seed}}": seed,
            "{{denoise}}": denoise,
            "{{checkpoint}}": checkpoint,
            "{{image}}": image_val,
        }
        str_map: dict[str, str] = {k: str(v) for k, v in exact_map.items()}

        def _subst(val):
            if isinstance(val, str):
                if val in exact_map:
                    return exact_map[val]
                out_s = val
                for token, rep in str_map.items():
                    if token in out_s:
                        out_s = out_s.replace(token, rep)
                return out_s
            if isinstance(val, list):
                return [_subst(x) for x in val]
            if isinstance(val, dict):
                return {k: _subst(v) for k, v in val.items()}
            return val

        injected = _subst(wf)
        for node in injected.values():
            if not isinstance(node, dict):
                continue
            ctype = str(node.get("class_type") or "")
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            if checkpoint and ctype == "CheckpointLoaderSimple" and "ckpt_name" in inputs:
                inputs["ckpt_name"] = checkpoint
            if init_filename and ctype == "LoadImage":
                inputs["image"] = init_filename
        return injected

    ckpt_name = checkpoint or "__AUTO_CHECKPOINT__"
    if init_filename:
        return {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt_name}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt_text, "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
            "4": {"class_type": "LoadImage", "inputs": {"image": init_filename}},
            "5": {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": ["1", 2]}},
            "6": {
                "class_type": "KSampler",
                "inputs": {
                    "model": ["1", 0],
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "latent_image": ["5", 0],
                    "seed": seed,
                    "steps": steps,
                    "cfg": cfg,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": denoise,
                },
            },
            "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
            "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": f"job_{seed}"}},
        }
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt_name}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt_text, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": f"job_{seed}"}},
    }




def _comfyui(job, params: dict, outputs: Path, model_code: str) -> RunSpec:
    """ComfyUI：把参数注入 API Workflow JSON，经由 engine_http_run 跑 /prompt→/history→/view。"""
    status = engines.probe("comfyui", fresh=True)
    if status.state != "ok":
        raise RunnerUnavailable(f"ComfyUI 现在不能用：{status.detail}")
    endpoint = status.values.get("endpoint") or ""
    out = outputs / f"{job['id']}-comfyui.png"
    receipt = outputs / f"{job['id']}-comfyui-receipt.json"
    request_path = outputs / f"{job['id']}-comfyui-request.json"

    init_image = str(params.get("image") or "").strip()
    init_target: Path | None = None
    if init_image:
        init_target = _resolve_input_image(init_image, job, label="参考底图")
    workflow = _comfyui_workflow_payload(
        str(job["prompt_snapshot"] or ""),
        params,
        init_filename="__UPLOADED_IMAGE__" if init_target is not None else None,
    )
    request: dict = {
        "engine": "comfyui",
        "endpoint": endpoint,
        "workflow": workflow,
        "init_image": str(init_target) if init_target is not None else "",
        "client_id": f"fastvideo-{job['id']}",
        "output": str(out),
        "receipt": str(receipt),
        "poll_interval": 0.8,
        "poll_timeout": 300,
    }
    secret_env = status.values.get("shared_secret_env")
    if secret_env is None:
        instance = engine_instances.pick("comfyui")
        if instance is not None and instance["endpoint"] == endpoint:
            secret_env = instance["shared_secret_env"]
    if secret_env:
        request["secret_env"] = secret_env
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    argv = [sys.executable, str(ENGINE_HTTP_RUNNER), str(request_path)]
    return RunSpec(
        runner="comfyui",
        argv=argv,
        cwd=outputs,
        expected=[out],
        media="image",
        meta={
            "adapter": "comfyui",
            "model": model_code,
            "endpoint": endpoint,
            "request_path": str(request_path),
            "result_path": str(receipt),
            "node_count": len(workflow),
        },
        pass_env=_secret_envs(request),
    )




def _draw_things(job, params: dict, outputs: Path, model_code: str) -> RunSpec:
    # 起跑前现查一次（fresh）：worker 可能刚从队列里领到一条几分钟前排的活，
    # 拿 15 秒缓存里的端点去发请求，会把"换了机器"这件事静默地漏过去。
    status = engines.probe("draw_things", fresh=True)
    if status.state != "ok":
        # unknown（没登记实例）与 missing（登记了但连不上）都由 engines 分好档，
        # 这里只转述，不再各写一套"不可用"的措辞。
        raise RunnerUnavailable(f"Draw Things 现在不能用：{status.detail}")
    endpoint = status.values.get("endpoint") or ""
    out = outputs / f"{job['id']}-draw_things.png"
    receipt = outputs / f"{job['id']}-draw-things-receipt.json"
    request_path = outputs / f"{job['id']}-draw-things-request.json"

    body = {"prompt": job["prompt_snapshot"],
            "width": int(params.get("width", 1024)),
            "height": int(params.get("height", 1024)),
            "steps": int(params.get("steps", 16))}
    negative = str(params.get("negative_prompt") or "").strip()
    if negative:
        body["negative_prompt"] = negative
    if params.get("guidance_scale") is not None:
        body["guidance_scale"] = float(params["guidance_scale"])
    if params.get("seed") is not None:
        body["seed"] = int(params["seed"])

    req_path = DRAW_THINGS_PATH
    request: dict = {
        "endpoint": endpoint, "path": req_path, "body": body,
        "output": str(out), "receipt": str(receipt), "timeout": 600,
    }
    init_image = str(params.get("init_image") or "").strip()
    if init_image:
        init_target = _resolve_input_image(init_image, job, label="参考底图")
        request["path"] = "/sdapi/v1/img2img"
        request["init_image"] = str(init_target)
        strength = params.get("strength")
        body["denoising_strength"] = float(0.65 if strength is None else strength)

    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    argv = [sys.executable, str(ENGINE_HTTP_RUNNER), str(request_path)]
    return RunSpec(
        runner="draw_things", argv=argv, cwd=outputs, expected=[out], media="image",
        meta={"adapter": "draw_things", "model": model_code, "endpoint": endpoint,
              "engine_model": status.values.get("model") or "",
              "request_path": str(request_path), "result_path": str(receipt),
              "body": body},
        pass_env=_secret_envs(request))
