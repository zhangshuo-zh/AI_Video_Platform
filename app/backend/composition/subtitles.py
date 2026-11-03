"""成片字幕的入口侧校验（阶段 D 的补口）。

只管三件事：文件像不像字幕、多大、落在哪儿。真正的烧录在 `ffmpeg_runner`，
能不能烧由 `validators.subtitle_filter_available()` 说了算——两件事分开，
是因为"上传了一份好字幕"和"这台机器烧得动"是两个独立的事实，
把它们混在一个判断里就会出现"传得上去、合成时才炸"。
"""
from __future__ import annotations

from composition import validators
from config import FFMPEG_BIN

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


def availability() -> dict:
    """这台机器能不能烧字幕，以及不能时该怎么办（界面照这一句显示）。"""
    if validators.subtitle_filter_available():
        return {"supported": True, "hint": ""}
    return {"supported": False,
            "hint": f"这台机器的 FFmpeg（{FFMPEG_BIN}）没有 subtitles 滤镜，"
                    "烧不了字幕；要装带 libass 的构建后才能发起带字幕的合成"}
