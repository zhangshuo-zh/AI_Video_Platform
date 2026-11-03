"""把成片时间线翻译成一条 FFmpeg 命令，并在跑完后验真（阶段 D）。

为什么不在请求线程里直接调 FFmpeg：合成是 CPU 重活，可能跑几分钟；它必须
走 Job → worker 那条现成的路（不建第二个队列）。这里只负责构造 argv 与
读出结果，Job 状态仍由 worker 统一收尾。

安全边界：
  * argv 是数组，`shell=False`；镜头路径来自库里的 Job 相对路径，逐条过
    `jobfiles.resolve_artifact` 的边界，不接受调用方拼绝对路径；
  * concat 清单写在**本次 Job 的 outputs/** 里，不落共享目录；
  * 不因为 ffmpeg 返回 0 就记成功——`post_check()` 用 ffprobe 复核分辨率、
    编码、时长与音轨，缺一项就抛错。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from config import (FFMPEG_BIN, COMPOSE_CRF, COMPOSE_PRESET)
from jobfiles import resolve_artifact, resolve_subtitle
from runners import RunSpec, RunnerUnavailable

from composition import validators
from composition.validators import MediaInvalid


class CompositionJobInvalid(Exception):
    """作业参数或成片记录不成立（不是环境问题，重试同样会失败）。"""


def build(job, params: dict, outputs: Path) -> RunSpec:
    ffmpeg = shutil.which(FFMPEG_BIN)
    if not ffmpeg:
        raise RunnerUnavailable(
            f"找不到 ffmpeg（{FFMPEG_BIN}）；装好 FFmpeg 或设置 FASTVIDEO_FFMPEG")
    composition_id = int(params.get("composition_id") or 0)
    if composition_id <= 0:
        raise CompositionJobInvalid("合成作业缺少 composition_id")
    # 只读库，不写：时间线在建成片行时就冻结了，执行期不再改
    from repositories.compositions import compositions
    row = compositions.get(composition_id)
    if row is None:
        raise CompositionJobInvalid(f"成片记录 #{composition_id} 不在了")
    if int(row["project_id"]) != int(job["project_id"]):
        raise CompositionJobInvalid("成片记录不属于这条作业的项目")
    try:
        timeline = json.loads(row["timeline_json"] or "{}")
        frozen_params = json.loads(row["params_json"] or "{}")
    except ValueError as exc:
        raise CompositionJobInvalid("成片记录里的时间线读不出来") from exc

    shots = timeline.get("shots") or []
    if not shots:
        raise CompositionJobInvalid("时间线里没有镜头")
    width = int(params.get("width") or frozen_params.get("width") or 1280)
    height = int(params.get("height") or frozen_params.get("height") or 720)
    fps = int(params.get("fps") or frozen_params.get("fps") or 24)
    if width % 2 or height % 2 or not (320 <= width <= 3840) or not (320 <= height <= 2160):
        raise CompositionJobInvalid(f"分辨率 {width}×{height} 不合法（宽高须为偶数）")
    if not (1 <= fps <= 60):
        raise CompositionJobInvalid(f"帧率 {fps} 不合法")

    try:
        validators.verify_inputs(timeline)
    except MediaInvalid as exc:
        raise CompositionJobInvalid(f"合成前检查：{exc.message}") from exc

    concat_path = outputs / f"{job['id']}-inputs.txt"
    total_ms = 0
    lines = []
    for shot in shots:
        target = resolve_artifact((shot.get("video") or {}).get("path") or "")
        if target is None:                       # verify_inputs 刚查过，这里是双保险
            raise CompositionJobInvalid(f"第 {shot.get('seq')} 镜的视频文件不在了")
        lines.append(_concat_line(target))
        total_ms += int(shot.get("duration_ms") or 0)
    if total_ms <= 0:
        total_ms = int(timeline.get("total_duration_ms") or 0)
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    output = outputs / f"{job['id']}-compose.mp4"
    receipt = outputs / f"{job['id']}-compose.json"
    argv = [ffmpeg, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_path)]

    audio = timeline.get("audio") or []
    audio_inputs: list[Path] = []
    for clip in audio:
        target = resolve_artifact(clip.get("path") or "")
        if target is None:
            raise CompositionJobInvalid(f"音频素材 #{clip.get('clip_id')} 的文件不在了")
        audio_inputs.append(target)
    for target in audio_inputs:
        argv += ["-i", str(target)]

    video_chain = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )
    subtitles = timeline.get("subtitles") or []
    if subtitles:
        sub_path = str(subtitles[0].get("path") or "")
        # 与 timeline.build 同一套解析规则：`subtitles/` 开头的是成片页上传的，
        # 只认本项目目录；两处写法不一样就会出现"提交时过、跑的时候找不到"
        sub = (resolve_subtitle(int(timeline.get("project_id") or 0), sub_path)
               if sub_path.startswith("subtitles/") else resolve_artifact(sub_path))
        if sub is None:
            raise CompositionJobInvalid("字幕文件不在了")
        video_chain += "," + _subtitle_filter(sub)
    video_chain += "[v]"

    chains = [video_chain]
    maps = ["-map", "[v]"]
    if audio_inputs:
        labels = []
        for index, clip in enumerate(audio):
            start = max(0, int(clip.get("start_ms") or 0))
            labels.append(f"[a{index}]")
            chains.append(f"[{index + 1}:a]adelay={start}|{start}[a{index}]")
        chains.append("".join(labels) +
                      f"amix=inputs={len(labels)}:duration=longest:"
                      f"dropout_transition=0[a]")
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    else:
        maps += ["-an"]

    argv += [
        "-filter_complex", ";".join(chains), *maps,
        "-c:v", "libx264", "-preset", COMPOSE_PRESET, "-crf", str(COMPOSE_CRF),
        "-pix_fmt", "yuv420p", "-r", str(fps), "-movflags", "+faststart",
        str(output),
    ]

    expected = {"width": width, "height": height, "fps": fps,
                "total_duration_ms": total_ms, "audio": bool(audio)}
    return RunSpec(
        runner="ffmpeg", argv=argv, cwd=outputs, expected=[output], media="video",
        meta={"compose_json": str(receipt), "expected": expected,
              "composition_id": composition_id,
              "timeline_sha": hashlib.sha256(
                  json.dumps(timeline, ensure_ascii=False, sort_keys=True)
                  .encode("utf-8")).hexdigest(),
              "inputs": len(shots), "audio_inputs": len(audio_inputs)},
    )


def post_check(spec: RunSpec) -> dict:
    """ffprobe 复核最终文件；通过就把回执写到 outputs/ 下（原子替换）。"""
    expected = spec.meta.get("expected") or {}
    try:
        info = validators.verify_output(spec.expected[0], expected)
    except MediaInvalid as exc:
        raise MediaInvalid(f"合成结果没通过验收：{exc.message}") from exc
    receipt = Path(spec.meta.get("compose_json") or "")
    if str(receipt):
        payload = {
            "schema": 1,
            "composition_id": spec.meta.get("composition_id"),
            "timeline_sha256": spec.meta.get("timeline_sha"),
            "inputs": spec.meta.get("inputs"),
            "audio_inputs": spec.meta.get("audio_inputs"),
            "expected": expected,
            "observed": info,
        }
        tmp = receipt.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, receipt)
    return info


def _concat_line(path: Path) -> str:
    # concat 清单用的是 FFmpeg 自己的转义：单引号写成 '\''，反斜杠写成 \\
    text = str(path).replace("\\", "\\\\").replace("'", "'\\''")
    return f"file '{text}'"


def _subtitle_filter(path: Path) -> str:
    # subtitles 滤镜按 FFmpeg 的滤镜转义处理：反斜杠、冒号、单引号都要转
    text = str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"subtitles='{text}'"
