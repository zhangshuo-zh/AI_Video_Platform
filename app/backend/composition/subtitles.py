"""成片字幕的入口侧校验（阶段 D 的补口）。

只管三件事：文件像不像字幕、多大、落在哪儿。真正的烧录在 `ffmpeg_runner`，
能不能烧由 `validators.subtitle_filter_available()` 说了算——两件事分开，
是因为"上传了一份好字幕"和"这台机器烧得动"是两个独立的事实，
把它们混在一个判断里就会出现"传得上去、合成时才炸"。
"""
from __future__ import annotations

import re
from pathlib import Path

import jobfiles
from composition import timeline, validators
from config import FFMPEG_BIN
from repositories.audio import clips as audio_repo
from repositories.content import episodes, shots
from repositories.jobs import jobs as job_repo

MAX_SUBTITLE_BYTES = 1024 * 1024      # 字幕是文本；1MB 足够一整集，再多就是传错了东西
ALLOWED_SUFFIXES = (".srt", ".vtt")


class SubtitleInvalid(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def check(filename: str, content: bytes) -> str:
    """验一份字幕，返回规范化后的后缀。不成立就抛 `SubtitleInvalid`。"""
    name = (filename or "").strip().lower()
    suffix = next((s for s in ALLOWED_SUFFIXES if name.endswith(s)), None)
    if suffix is None:
        raise SubtitleInvalid("字幕只收 .srt 或 .vtt；别的格式烧不进画面")
    if not content:
        raise SubtitleInvalid("字幕文件是空的")
    if len(content) > MAX_SUBTITLE_BYTES:
        raise SubtitleInvalid(
            f"字幕超过 {MAX_SUBTITLE_BYTES // 1024}KB；一整集用不了这么多，"
            "请确认传的不是视频或别的东西")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SubtitleInvalid("字幕不是 UTF-8 文本；先用编辑器另存为 UTF-8") from exc
    # 只做"形状"检查：有没有时间轴。语法错了 FFmpeg 会自己报，
    # 在这里当解析器只会多养出一套和 libass 不一样的口径。
    body = text.strip()
    if suffix == ".vtt" and not body.startswith("WEBVTT"):
        raise SubtitleInvalid("WebVTT 第一行要写 WEBVTT")
    if suffix == ".srt" and "-->" not in text:
        raise SubtitleInvalid("SRT 里没有一条时间轴（少了 `-->` 那行）")
    return suffix


_TIMESTAMP = r"(?:[0-9]{2,6}:)?[0-5][0-9]:[0-5][0-9][.,][0-9]{3}"
_CUE_LINE = re.compile(rf"^({_TIMESTAMP})\s+-->\s+({_TIMESTAMP})(?:\s.*)?$")


def _timestamp_ms(value: str) -> int:
    whole, fraction = value.replace(",", ".").split(".")
    seconds = 0
    for part in whole.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds * 1000 + int(fraction)


def snapshot(target: Path, relative_path: str) -> dict:
    """冻结字幕原文和可导航的时间点，保留 FFmpeg 对字幕文本的解释权。"""
    try:
        with target.open("rb") as stream:
            content = stream.read(MAX_SUBTITLE_BYTES + 1)
    except OSError as exc:
        raise SubtitleInvalid("字幕文件暂时读不到") from exc
    suffix = check(target.name, content)
    text = content.decode("utf-8-sig")
    cues = []
    for block in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.strip().splitlines()
        if not lines or lines[0].split(maxsplit=1)[0] in {"WEBVTT", "NOTE", "STYLE", "REGION"}:
            continue
        for index, line in enumerate(lines[:2]):
            match = _CUE_LINE.fullmatch(line.strip())
            if match:
                start, end = map(_timestamp_ms, match.groups())
                if end > start:
                    cues.append({"start_ms": start, "end_ms": end,
                                 "text": "\n".join(lines[index + 1:])})
                break
    return {"path": relative_path, "bytes": len(content), "suffix": suffix,
            "text": text, "cues": sorted(cues, key=lambda cue: cue["start_ms"])}


def availability() -> dict:
    """这台机器能不能烧字幕，以及不能时该怎么办（界面照这一句显示）。"""
    if validators.subtitle_filter_available():
        return {"supported": True, "hint": ""}
    return {"supported": False,
            "hint": f"这台机器的 FFmpeg（{FFMPEG_BIN}）没有 subtitles 滤镜，"
                    "烧不了字幕；要装带 libass 的构建后才能发起带字幕的合成"}


def _fmt_srt_ts(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def build_srt_text(project_id: int, episode_id: int) -> str:
    """按一集分镜实际/计划时长与台词生成 `.srt` 字幕文本（供存盘与工程包导出复用）。"""
    ep = episodes.get(episode_id)
    if ep is None or int(ep["project_id"]) != int(project_id):
        raise SubtitleInvalid("创作单元不存在或不属于当前项目")
    shot_rows = shots.list_by_episode(episode_id)
    if not shot_rows:
        raise SubtitleInvalid("这一集还没有分镜，无法生成字幕")

    latest_videos = job_repo.latest_succeeded_videos_by_shot(episode_id)
    audio_rows = audio_repo.list(project_id, None, episode_id)
    lines_by_shot: dict[int, list[str]] = {}
    for clip in audio_rows:
        sid = clip["shot_id"]
        if sid is None or clip["kind"] not in ("dialogue", "narration"):
            continue
        text = str(clip["text"] or "").strip()
        if text:
            lines_by_shot.setdefault(int(sid), []).append(text)

    cursor = 0
    prev_dur_ms = 0
    blocks: list[str] = []
    cue_idx = 0
    for idx, shot in enumerate(shot_rows):
        sid = int(shot["id"])
        keys = shot.keys()
        raw_ms = max(100, int(round(float(shot["duration"] or 5.0) * 1000)))
        video_job = latest_videos.get(sid)
        if video_job is not None:
            target = jobfiles.artifact_at(video_job["result_paths_json"], 0)
            if target is not None:
                try:
                    info = validators.inspect(target)
                    if int(info.get("duration_ms") or 0) >= 100:
                        raw_ms = int(info["duration_ms"])
                except validators.MediaInvalid:
                    pass
        trim_in_ms = max(0, int(shot["trim_in_ms"] or 0)) if "trim_in_ms" in keys else 0
        trim_out_ms = max(0, int(shot["trim_out_ms"] or 0)) if "trim_out_ms" in keys else 0
        transition = str((shot["transition"] if "transition" in keys else "cut") or "cut")
        eff_in = min(trim_in_ms, raw_ms)
        eff_out = min(trim_out_ms, raw_ms) if trim_out_ms > 0 else raw_ms
        dur_ms = max(100, eff_out - eff_in)
        if idx > 0:
            ov = timeline.crossfade_overlap_ms(prev_dur_ms, dur_ms, transition)
            cursor = max(0, cursor - ov)
        start_ms = cursor
        end_ms = cursor + dur_ms
        cursor = end_ms
        prev_dur_ms = dur_ms

        cue_lines = lines_by_shot.get(sid)
        cue_text = "\n".join(cue_lines) if cue_lines else str(shot["summary"] or "").strip()
        if cue_text:
            cue_idx += 1
            blocks.append(f"{cue_idx}\n{_fmt_srt_ts(start_ms)} --> {_fmt_srt_ts(end_ms)}\n{cue_text}")

    if not blocks:
        raise SubtitleInvalid("分镜缺少可用的台词或描述文本，无法生成字幕")
    return "\n\n".join(blocks) + "\n"


def generate_srt_from_shots(project_id: int, episode_id: int) -> str:
    """按一集分镜实际/计划时长与台词自动生成 `.srt` 字幕并落盘到项目字幕目录。"""
    raw = build_srt_text(project_id, episode_id).encode("utf-8")
    return jobfiles.save_subtitle(project_id, raw, ".srt")
