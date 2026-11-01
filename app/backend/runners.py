"""本地 adapter 的命令构造与产物校验（W09）。

这一层只回答两个问题：**用什么 argv 起进程**、**跑完后什么算成功**。
它不碰数据库、不写 Job 状态——那些仍由 worker.py 统一收尾。

四条硬约束：

1. **argv 是数组**，绝不拼 shell 字符串。提示词里带引号、分号、`$(...)`
   都只是普通字符；`shell=False` 是这里的默认，也是唯一写法。
2. 每条命令必须能解释清楚"为什么这么填"：参数来自 adapters.py 的白名单，
   路径来自 runtime.py 的探测，产物落在 `data/jobs/<id>/outputs/`。
3. 输入图只能来自 Job 自己的目录，或一个显式配置的本地路径；不接受
   "界面随手填一个 /etc/passwd"——I2V 的真实资产选择器在 W12。
4. 产物要验真：mp4 看 ftyp、png 看魔数、文件非空。进程返回 0 不代表
   它真的写了东西（磁盘满、脚本提前 return 都会留下 0 字节文件）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import runtime
from config import JOBS_DIR

class RunnerUnavailable(Exception):
    """运行时缺东西：解释器／入口／权重／模型目录。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class RunSpec:
    runner: str
    argv: list[str]
    cwd: Path
    expected: list[Path]
    media: str                    # video / image / audio / other
    meta: dict = field(default_factory=dict)

def _need_runtime(probe) -> None:
    if probe.state != "ok":
        raise RunnerUnavailable(probe.detail)


def _mp4_path(outputs: Path, job_id: int, label: str) -> Path:
    return outputs / f"{job_id}-{label}.mp4"


def _metrics_path(outputs: Path, job_id: int) -> Path:
    return outputs / f"{job_id}-metrics.json"


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
        if params.get("fast"):
            argv.append("--fast")
        if params.get("fast_spatial"):
            argv.append("--fast-spatial")
        if params.get("refine"):
            argv.append("--refine")
    else:
        argv = [v["python"], v["script"],
                "--model-root", v["model"], "--mlx-checkpoint", v["model"],
                "--prompt", job["prompt_snapshot"],
                "--height", str(height), "--width", str(width),
                "--num-frames", str(frames), "--seed", str(seed),
                "--output-path", str(out), "--metrics-json", str(metrics)]
        if params.get("fast"):
            argv.append("--fast")
        if params.get("fast_spatial"):
            argv.append("--fast-spatial")

    return RunSpec(
        runner="fastmetal", argv=argv, cwd=outputs, expected=[out], media="video",
        meta={"adapter": "fastmetal", "model": model_code,
              "model_dir": v["model"], "python": v["python"],
              "seed": seed, "width": width, "height": height, "frames": frames,
              "generation_steps": 3 if not is_5b else 3,
              "metrics_path": str(metrics)})


def _mlx_video(job, params: dict, outputs: Path) -> RunSpec:
    probe = runtime.probe_mlx_video()
    _need_runtime(probe)
    v = probe.values
    image = (params.get("image") or "").strip()
    if not image:
        raise RunnerUnavailable("I2V 需要条件图；资产选择器接上之前不能从界面提交")
    image_path = Path(image).expanduser()
    if not image_path.is_absolute():
        # 相对路径只允许落在本作业目录里；resolve 之后再做一次边界比对，
        # 挡住 ../ 和符号链接绕出去。
        root = (JOBS_DIR / str(job["id"])).resolve()
        image_path = (root / image_path).resolve()
        if not str(image_path).startswith(str(root) + "/"):
            raise RunnerUnavailable("条件图路径越出作业目录")
    if not image_path.is_file():
        raise RunnerUnavailable(f"条件图不存在：{image_path.name}")

    out = _mp4_path(outputs, job["id"], "mlx-video")
    seed = int(params.get("seed", 42))
    frames = int(params.get("frames", 81))
    width = int(params.get("width", 1280))
    height = int(params.get("height", 704))
    steps = int(params.get("steps", 4))
    argv = [v["python"], "-m", "mlx_video.wan_2.generate",
            "--model-dir", v["model"],
            "--prompt", job["prompt_snapshot"],
            "--image", str(image_path),
            "--height", str(height), "--width", str(width),
            "--num-frames", str(frames), "--steps", str(steps),
            "--seed", str(seed), "--output-path", str(out)]
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
              "steps": steps, "image": str(image_path)})


def _mflux(job, params: dict, outputs: Path) -> RunSpec:
    probe = runtime.probe_mflux()
    _need_runtime(probe)
    v = probe.values
    out = outputs / f"{job['id']}-mflux.png"
    seed = int(params.get("seed", 42))
    width = int(params.get("width", 1024))
    height = int(params.get("height", 1024))
    steps = int(params.get("steps", 4))
    argv = [v["cli"], "--model", "schnell",
            "--prompt", job["prompt_snapshot"],
            "--steps", str(steps), "--seed", str(seed),
            "--width", str(width), "--height", str(height),
            "--quantize", "8", "--output", str(out)]
    return RunSpec(
        runner="mflux", argv=argv, cwd=outputs, expected=[out], media="image",
        meta={"adapter": "mflux", "model": job["model_code"],
              "cli": v["cli"], "seed": seed,
              "width": width, "height": height, "steps": steps})


def build(job, params: dict, outputs: Path) -> RunSpec:
    """按能力与 provider 分发。没接线的组合明确拒绝，不找替代品。"""
    capability = job["capability"]
    provider = job["provider_code"]
    model = job["model_code"]
    if capability == "video" and provider == "fastmetal":
        return _fastmetal(job, params, outputs, model)
    if capability == "video" and provider == "mlx-video":
        return _mlx_video(job, params, outputs)
    if capability == "image" and provider == "mflux":
        return _mflux(job, params, outputs)
    raise RunnerUnavailable(f"{capability}/{provider} 还没有执行接线")


def _magic_ok(path: Path, media: str) -> bool:
    try:
        head = path.open("rb").read(16)
    except OSError:
        return False
    if media == "video":
        # ISO-BMFF: 4 字节长度 + 'ftyp'。FastMetal 与 ffmpeg 都产出这个头。
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if media == "image":
        return head.startswith(b"\x89PNG\r\n\x1a\n") or head[:3] == b"\xff\xd8\xff"
    return True


def verify(spec: RunSpec) -> list[Path]:
    """按媒体类型验真；返回实际存在的产物路径。缺一件就抛，不写半条成功。"""
    missing = [str(p) for p in spec.expected if not p.is_file()]
    if missing:
        raise RunnerUnavailable("命令返回了，但没有产物：" + "、".join(Path(p).name for p in missing))
    for path in spec.expected:
        if path.stat().st_size <= 0:
            raise RunnerUnavailable(f"产物是空的：{path.name}")
        if not _magic_ok(path, spec.media):
            raise RunnerUnavailable(f"产物不是可识别的 {spec.media} 文件：{path.name}")
    return spec.expected


def metrics_summary(path: Path) -> dict:
    """从模型自己写的 metrics.json 里挑几项进 resources；原文件仍留在 outputs。

    不是整份搬进库：metrics 里有模型路径、完整调度表，既不必要也不该
    进 /api/jobs 的响应。
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    keep = {}
    for key in ("prompt", "height", "width", "num_frames", "seed",
                "decode_backend", "spatial_mode", "fast", "refine",
                "prompt_encode_s", "mlx_dit_load_s", "mlx_denoise_s",
                "vae_decode_export_s", "total_s", "dmd_denoising_steps"):
        if key in raw:
            keep[key] = raw[key]
    for src, dst in (("mlx_denoise_peak_bytes", "denoise_peak_gb"),
                     ("mlx_load_peak_bytes", "load_peak_gb")):
        value = raw.get(src)
        if isinstance(value, (int, float)) and value >= 0:
            keep[dst] = round(value / (1024 ** 3), 2)
    return keep


def cleanup_partial(spec: RunSpec) -> None:
    """失败时删掉半截产物，免下次重试被旧文件骗过。"""
    for path in spec.expected:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
