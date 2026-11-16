"""执行器 video 域。"""
from __future__ import annotations

import os
from pathlib import Path

import runtime

from .common import _flag, _metrics_path, _mp4_path, _need_runtime
from .inputs import _resolve_input_image
from .protocol import RunnerUnavailable, RunSpec


def _fastmetal(job, params: dict, outputs: Path, model_code: str) -> RunSpec:
    probe = runtime.probe_fastmetal(model_code)
    _need_runtime(probe)
    v = probe.values
    is_5b = model_code == "fastmetal-5b-qad"
    out = _mp4_path(outputs, job["id"], "fastmetal")
    metrics = _metrics_path(outputs, job["id"])
    seed = int(params.get("seed", 1024 if not is_5b else 1234))
    frames = int(params.get("frames", 81 if not is_5b else 121))
    width = int(params.get("width", 640 if not is_5b else 832))
    height = int(params.get("height", 352 if not is_5b else 448))

    if is_5b:
        argv = [v["python"], v["script"],
                "--mlx-checkpoint", v["model"],
                "--text-encoder-root", v["model"],
                "--vae-root", str(Path(v["model"]) / "vae"),
                "--prompt", job["prompt_snapshot"],
                "--height", str(height), "--width", str(width),
                "--num-frames", str(frames), "--seed", str(seed),
                "--output-path", str(out), "--metrics-json", str(metrics)]
        if _flag(params, "fast", job, model_code):
            argv.append("--fast")
        if _flag(params, "fast_spatial", job, model_code):
            argv.append("--fast-spatial")
        if _flag(params, "refine", job, model_code):
            argv.append("--refine")
    else:
        argv = [v["python"], v["script"],
                "--model-root", v["model"], "--mlx-checkpoint", v["model"],
                "--prompt", job["prompt_snapshot"],
                "--height", str(height), "--width", str(width),
                "--num-frames", str(frames), "--seed", str(seed),
                "--output-path", str(out), "--metrics-json", str(metrics)]
        if _flag(params, "fast", job, model_code):
            argv.append("--fast")
        if _flag(params, "fast_spatial", job, model_code):
            argv.append("--fast-spatial")

    # 这三项只在作业**显式给了值**时才下发：没给就沿用入口脚本自己的默认，
    # 于是这道之前提交的作业 argv 一个字节都不变。
    # 为什么不像 fast_spatial 那样回落清单默认：那个默认是 16GB 的 OOM 实测，
    # 不回落会跑出崩掉的作业；而 fps／factor 的脚本默认本来就是可用值，
    # 回落只会让老作业的 argv 凭空多出几个参数，把"旧作业可复现"这条弄脏。
    for name, flag, cast in (("fps", "--fps", int),
                             ("fast_factor", "--fast-factor", int),
                             ("fast_sharpen", "--fast-sharpen", float)):
        if params.get(name) is not None:
            argv += [flag, str(cast(params[name]))]

    return RunSpec(
        runner="fastmetal", argv=argv, cwd=outputs, expected=[out], media="video",
        meta={"adapter": "fastmetal", "model": model_code,
              "model_dir": v["model"], "python": v["python"],
              "seed": seed, "width": width, "height": height, "frames": frames,
              "generation_steps": 3,
              "metrics_path": str(metrics)})




def _mlx_video(job, params: dict, outputs: Path) -> RunSpec:
    probe = runtime.probe_mlx_video()
    _need_runtime(probe)
    v = probe.values
    image = (params.get("image") or "").strip()
    if not image:
        raise RunnerUnavailable("I2V 需要条件图；资产选择器接上之前不能从界面提交")
    target = _resolve_input_image(image, job, label="条件图")

    out = _mp4_path(outputs, job["id"], "mlx-video")
    seed = int(params.get("seed", 42))
    frames = int(params.get("frames", 81))
    width = int(params.get("width", 1280))
    height = int(params.get("height", 704))
    steps = int(params.get("steps", 4))
    last_raw = (params.get("last_frame_image") or "").strip()
    last_target: Path | None = None
    argv = [v["python"], "-m", "mlx_video.wan_2.generate",
            "--model-dir", v["model"],
            "--prompt", job["prompt_snapshot"],
            "--image", str(target)]
    if last_raw:
        last_target = _resolve_input_image(last_raw, job, label="尾帧图")
        argv.extend(["--last-image", str(last_target)])
    argv.extend(["--height", str(height), "--width", str(width),
                 "--num-frames", str(frames), "--steps", str(steps),
                 "--seed", str(seed), "--output-path", str(out)])
    # Lightning LoRA 是四步档的关键；有就挂，没有就按模型默认步数跑。
    # 这两个值不是用户参数，路径只从环境读，避免把任意本地文件喂给加载器。
    for key, flag in (("FASTVIDEO_MLX_VIDEO_LORA_HIGH", "--lora-high"),
                      ("FASTVIDEO_MLX_VIDEO_LORA_LOW", "--lora-low")):
        value = (os.environ.get(key) or "").strip()
        if value:
            argv.extend([flag, value, "1"])
    return RunSpec(
        runner="mlx-video", argv=argv, cwd=outputs, expected=[out], media="video",
        meta={"adapter": "mlx-video", "model": job["model_code"],
              "model_dir": v["model"], "python": v["python"],
              "seed": seed, "width": width, "height": height, "frames": frames,
              "steps": steps, "image": str(target),
              "last_frame_image": str(last_target) if last_target else ""})
