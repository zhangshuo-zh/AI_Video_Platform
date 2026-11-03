"""合成前/后的媒体检查（阶段 D）。只回答"这个文件到底能不能用"。

FFmpeg 返回 0 不等于合成成功：磁盘满、编码器提前退出、输入只有音频流，
都会留下一个"退出码 0 却没有画面"的文件。所以这里不看退出码，只看 ffprobe
读出来的事实：文件在、非空、有视频流、编码是 h264、时长与分辨率对得上。

所有检查都返回/抛出中文原因，界面照原话显示；不把 ffprobe 的整段输出塞给用户。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from config import FFMPEG_BIN, FFPROBE_BIN


class MediaInvalid(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# `ffmpeg -filters` 的输出是"每行一个滤镜"，跑一次要几十毫秒，但合成前后各查一次
# 就浪费——缓存到进程里。测试要换分档时调 reset_subtitle_filter()。
_SUBTITLE_FILTER: bool | None = None


def subtitle_filter_available(*, refresh: bool = False) -> bool:
    """这台机器的 FFmpeg 能不能烧字幕。

    为什么要在提交前问：Homebrew 的默认构建**不带 libass**，`subtitles` 滤镜
    根本不存在（本机 `ffmpeg 9.0.2` 实测如此）。硬让 FFmpeg 去跑只会留下一条
    "No such filter"的失败作业；而"没做的事不假装能做、跑不了就别说要跑"
    是这套平台的红线，所以要钱要机时的动作前置到检查阶段就拒掉。
    """
    global _SUBTITLE_FILTER
    if _SUBTITLE_FILTER is not None and not refresh:
        return _SUBTITLE_FILTER
    try:
        proc = subprocess.run([FFMPEG_BIN, "-hide_banner", "-filters"],
                              capture_output=True, text=True, timeout=20,
                              shell=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _SUBTITLE_FILTER = False
        return _SUBTITLE_FILTER
    # 滤镜名在第一列之前，形如 " .. subtitles        V->V       Render text..."
    _SUBTITLE_FILTER = re.search(r"^\s*[TSC.]{0,4}\s+subtitles\s",
                                 proc.stdout or "", re.M) is not None
    return _SUBTITLE_FILTER


def reset_subtitle_filter() -> None:
    """清掉缓存。给测试与"装了新 FFmpeg 之后"用。"""
    global _SUBTITLE_FILTER
    _SUBTITLE_FILTER = None


def inspect(path: Path) -> dict:
    """读媒体信息。ffprobe 不在、文件读不了、JSON 坏掉都归成一句人话。"""
    if not path.is_file():
        raise MediaInvalid(f"文件不存在：{path.name}")
    if path.stat().st_size <= 0:
        raise MediaInvalid(f"文件是空的：{path.name}")
    try:
        proc = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30, shell=False)
    except FileNotFoundError as exc:
        raise MediaInvalid(f"找不到 ffprobe（{FFPROBE_BIN}）；先装 FFmpeg 再加 PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaInvalid(f"ffprobe 读取 {path.name} 超时") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["（无输出）"]
        raise MediaInvalid(f"{path.name} 不是可读的媒体文件：{tail[0][:160]}")
    try:
        raw = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise MediaInvalid(f"{path.name} 的媒体信息读不出来") from exc

    streams = raw.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = raw.get("format") or {}
    duration = _float(fmt.get("duration"))
    if duration is None and video is not None:
        duration = _float(video.get("duration"))
    return {
        "bytes": int(fmt.get("size") or path.stat().st_size),
        "duration_ms": int(round(duration * 1000)) if duration else None,
        "format": str(fmt.get("format_name") or ""),
        "video_codec": (video or {}).get("codec_name") or "",
        "width": int(video["width"]) if video and video.get("width") else None,
        "height": int(video["height"]) if video and video.get("height") else None,
        "has_audio": bool(audios),
        "audio_codecs": sorted({str(a.get("codec_name") or "") for a in audios}),
        "has_video": video is not None,
    }


def verify_inputs(timeline: dict) -> list[dict]:
    """逐个读镜头视频与音频；任何一件不可读就拒绝，不带病开跑。"""
    from jobfiles import resolve_artifact

    checked: list[dict] = []
    for shot in timeline.get("shots") or []:
        rel = (shot.get("video") or {}).get("path") or ""
        target = resolve_artifact(rel)
        if target is None:
            raise MediaInvalid(f"第 {shot.get('seq')} 镜的视频文件不在了")
        info = inspect(target)
        if not info["has_video"]:
            raise MediaInvalid(f"第 {shot.get('seq')} 镜的文件里没有视频流")
        checked.append({"kind": "shot", "seq": shot.get("seq"),
                        "path": rel, **info})
    for clip in timeline.get("audio") or []:
        target = resolve_artifact(clip.get("path") or "")
        if target is None:
            raise MediaInvalid(f"音频素材 #{clip.get('clip_id')} 的文件不在了")
        info = inspect(target)
        if not info["has_audio"]:
            raise MediaInvalid(f"音频素材 #{clip.get('clip_id')} 的文件里没有音频流")
        checked.append({"kind": "audio", "clip_id": clip.get("clip_id"),
                        "path": clip.get("path"), **info})
    return checked


def verify_output(path: Path, expected: dict) -> dict:
    """合成后的验真。`expected` 来自时间线与参数：时长、宽高、是否该有音轨。"""
    info = inspect(path)
    if not info["has_video"]:
        raise MediaInvalid("合成结果里没有视频流")
    if info["video_codec"] != "h264":
        raise MediaInvalid(f"合成结果的视频编码是 {info['video_codec'] or '未知'}，不是 h264")
    width = expected.get("width")
    height = expected.get("height")
    if width and height and (info["width"], info["height"]) != (width, height):
        raise MediaInvalid(
            f"合成结果分辨率是 {info['width']}×{info['height']}，"
            f"应为 {width}×{height}")
    total_ms = int(expected.get("total_duration_ms") or 0)
    duration = info["duration_ms"] or 0
    if duration < 500:
        raise MediaInvalid(f"合成结果只有 {duration}ms，不像一段成片")
    if total_ms and abs(duration - total_ms) > max(2000, int(total_ms * 0.08)):
        raise MediaInvalid(
            f"合成结果时长 {duration}ms 与时间线 {total_ms}ms 相差太大")
    if expected.get("audio") and not info["has_audio"]:
        raise MediaInvalid("时间线里有音频，但合成结果没有音轨")
    if "mp4" not in info["format"]:
        raise MediaInvalid(f"合成结果的容器格式是 {info['format'] or '未知'}，不是 mp4")
    return info


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
