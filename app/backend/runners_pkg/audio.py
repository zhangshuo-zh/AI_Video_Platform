"""执行器 audio 域。"""
from __future__ import annotations

from pathlib import Path

import runtime

from .common import _need_runtime, _spec_default
from .protocol import RunnerUnavailable, RunSpec

# ---------- 音频两腿（VoxCPM2 配音 / ACE-Step 1.5 音乐）----------
# 权重都走 mlx-community 的量化转换，不拉官方整包：
#   openbmb/VoxCPM2 主文件 4,580,080,592B（卡面 gated=false、apache-2.0），
#   MLX 8bit 版 3.2GB 级、由 `mlx-audio` 直接跑；ACE-Step 1.5 的 MLX 4bit 版
#   卡面写 2.2GB 主模型＋0.7GB VAE＋2.4GB 文本编码器。
# 这台 16GB 无风扇机器上"整包官方权重＋bf16"是装不下的，量化档才是可用档。
VOXCPM_MLX_MODEL = "mlx-community/VoxCPM2-8bit"

ACE_STEP_MLX_MODEL = "mlx-community/ACE-Step1.5-MLX-4bit"

# `mlx_audio.music.generate` 的 --lyrics/--lyrics-file 是必填互斥组：
# 没有歌词也要给一段结构标记，否则子进程直接 argparse 退出。
ACE_STEP_INSTRUMENTAL = "[inst]"



def _voxcpm(job, params: dict, outputs: Path) -> RunSpec:
    """配音：一条台词 → 一个 wav（可能分段，文件名由执行器决定）。

    产物用 `expected_glob` 事后找，不是偷懒：`mlx_audio.tts.generate` 在
    单段时写 `<prefix>.wav`、多段时写 `<prefix>_000.wav`、`_001.wav`……
    分段数只有跑完才知道。找不到任何文件就抛，绝不写半条成功。
    """
    probe = runtime.probe_mlx_audio("tts")
    _need_runtime(probe)
    text = (job["prompt_snapshot"] or "").strip()
    if not text:
        raise RunnerUnavailable("这条配音作业没有可朗读的文本")
    out_dir = outputs / ("%s-tts" % job["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    speed = params.get("speed")
    if speed is None:
        speed = _spec_default(job, job["model_code"], "speed", 1.0)
    argv = [probe.values["cli"], "--model", VOXCPM_MLX_MODEL,
            "--text", text, "--speed", str(float(speed)),
            "--output_path", str(out_dir), "--file_prefix", str(job["id"]),
            "--audio_format", "wav", "--verbose"]
    instruct = str(params.get("instruct") or "").strip()
    if instruct:
        argv += ["--instruct", instruct]
    return RunSpec(
        runner="voxcpm2", argv=argv, cwd=out_dir, expected=[], media="audio",
        expected_glob="%s*.wav" % job["id"],
        meta={"adapter": "voxcpm2", "model": job["model_code"],
              "cli": probe.values["cli"], "hf_model": VOXCPM_MLX_MODEL,
              "speed": float(speed), "instruct": instruct})




def _ace_step(job, params: dict, outputs: Path) -> RunSpec:
    """音乐：一句风格描述（＋可选歌词）→ 一个 wav。

    `duration`／`steps`／`seed` 都照清单默认回落，理由与 FastMetal 那条一样：
    产线节点提交的作业 params 是空的，不回落就等于把清单里的默认丢掉。
    """
    probe = runtime.probe_mlx_audio("music")
    _need_runtime(probe)
    caption = (job["prompt_snapshot"] or "").strip()
    if not caption:
        raise RunnerUnavailable("这条音乐作业没有风格描述")
    out = outputs / ("%s-music.wav" % job["id"])
    model = job["model_code"]
    duration = int(params.get("duration")
                   or _spec_default(job, model, "duration", 30))
    steps = int(params.get("steps") or _spec_default(job, model, "steps", 27))
    seed = params.get("seed")
    argv = [probe.values["cli"], "--model", ACE_STEP_MLX_MODEL,
            "--caption", caption,
            "--lyrics", str(params.get("lyrics") or "").strip() or ACE_STEP_INSTRUMENTAL,
            "--duration", str(duration), "--steps", str(steps),
            "--output", str(out), "--verbose"]
    if seed is not None:
        argv += ["--seed", str(int(seed))]
    return RunSpec(
        runner="ace-step", argv=argv, cwd=outputs, expected=[out], media="audio",
        meta={"adapter": "ace-step", "model": job["model_code"],
              "cli": probe.values["cli"], "hf_model": ACE_STEP_MLX_MODEL,
              "duration": duration, "steps": steps,
              "lyrics": str(params.get("lyrics") or "").strip() or ACE_STEP_INSTRUMENTAL})
