"""一键导出剪映草稿包（draft_content.json）与 Final Cut Pro X XML（timeline.fcpxml）ZIP 工程包。"""
from __future__ import annotations

import io
import json
import uuid
import zipfile
from xml.sax.saxutils import escape as xml_escape

import jobfiles
from composition import service as composition_service
from composition import subtitles
from composition.validators import resolve_timeline_media
from repositories.compositions import compositions


def _fcpxml_time(ms: int, fps: int = 24) -> str:
    frames = max(0, int(round((ms / 1000.0) * fps)))
    return f"{frames}/{fps}s"


def _build_fcpxml(ep_tag: str, shots: list[dict], audio: list[dict],
                  fps: int, width: int, height: int) -> str:
    resources = [
        f'<format id="r1" name="FFVideoFormat{height}p{fps}" frameDuration="1/{fps}s" '
        f'width="{width}" height="{height}"/>'
    ]
    spine_items: list[str] = []
    audio_clips_xml: list[str] = []
    offset_ms = 0

    for idx, s in enumerate(shots):
        res_id = f"r_v{idx + 1}"
        seq = int(s.get("seq") or idx + 1)
        rel_name = xml_escape(s.get("_export_rel") or f"media/{ep_tag}_Shot{seq:03d}.mp4")
        dur_ms = max(100, int(s.get("duration_ms") or 1000))
        trim_in_ms = max(0, int(s.get("trim_in_ms") or 0))
        title = xml_escape(f"{ep_tag}_Shot{seq:03d}")
        resources.append(
            f'<asset id="{res_id}" name="{title}" src="./{rel_name}" '
            f'start="0s" duration="{_fcpxml_time(dur_ms + trim_in_ms, fps)}" hasVideo="1" format="r1"/>'
        )
        spine_items.append(
            f'<asset-clip name="{title}" ref="{res_id}" '
            f'offset="{_fcpxml_time(offset_ms, fps)}" '
            f'start="{_fcpxml_time(trim_in_ms, fps)}" '
            f'duration="{_fcpxml_time(dur_ms, fps)}"/>'
        )
        offset_ms += dur_ms

    for idx, a in enumerate(audio):
        res_id = f"r_a{idx + 1}"
        cid = int(a.get("clip_id") or idx + 1)
        rel_name = xml_escape(a.get("_export_rel") or f"audio/{ep_tag}_Audio{cid:03d}.wav")
        a_dur_ms = max(200, int(a.get("duration_ms") or 2000))
        a_start_ms = max(0, int(a.get("start_ms") or 0))
        title = xml_escape(f"{ep_tag}_Audio{cid:03d}_{a.get('kind') or 'audio'}")
        resources.append(
            f'<asset id="{res_id}" name="{title}" src="./{rel_name}" '
            f'start="0s" duration="{_fcpxml_time(a_dur_ms, fps)}" hasAudio="1"/>'
        )
        audio_clips_xml.append(
            f'<asset-clip name="{title}" ref="{res_id}" lane="{-(idx + 1)}" '
            f'offset="{_fcpxml_time(a_start_ms, fps)}" duration="{_fcpxml_time(a_dur_ms, fps)}"/>'
        )

    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE fcpxml>',
        '<fcpxml version="1.10">',
        '  <resources>',
        *[f'    {item}' for item in resources],
        '  </resources>',
        f'  <library><event name="{xml_escape(ep_tag)}"><project name="{xml_escape(ep_tag)}">',
        f'    <sequence format="r1" duration="{_fcpxml_time(max(offset_ms, 100), fps)}">',
        '      <spine>',
        *[f'        {item}' for item in spine_items],
        *[f'        {item}' for item in audio_clips_xml],
        '      </spine>',
        '    </sequence>',
        '  </project></event></library>',
        '</fcpxml>',
    ])


def _build_jianying_draft(ep_tag: str, shots: list[dict], audio: list[dict],
                          srt_rel: str, width: int, height: int) -> dict:
    """生成兼容剪映草稿格式的 `draft_content.json`（微秒时间轴 + 标准片段结构）。"""
    materials_videos: list[dict] = []
    materials_audios: list[dict] = []
    video_segments: list[dict] = []
    audio_segments: list[dict] = []

    cursor_us = 0
    for idx, s in enumerate(shots):
        seq = int(s.get("seq") or idx + 1)
        rel_name = s.get("_export_rel") or f"media/{ep_tag}_Shot{seq:03d}.mp4"
        dur_us = max(100, int(s.get("duration_ms") or 1000)) * 1000
        trim_in_us = max(0, int(s.get("trim_in_ms") or 0)) * 1000
        mat_id = str(uuid.uuid4())
        materials_videos.append({
            "id": mat_id,
            "path": f"./{rel_name}",
            "material_name": f"{ep_tag}_Shot{seq:03d}",
            "type": "photo" if (s.get("video") or {}).get("media_kind") == "image" else "video",
            "duration": dur_us + trim_in_us,
            "width": int((s.get("video") or {}).get("width") or width),
            "height": int((s.get("video") or {}).get("height") or height),
        })
        video_segments.append({
            "id": str(uuid.uuid4()),
            "material_id": mat_id,
            "source_timerange": {"start": trim_in_us, "duration": dur_us},
            "target_timerange": {"start": cursor_us, "duration": dur_us},
            "track_index": 0,
            "transition": str(s.get("transition") or "cut"),
        })
        cursor_us += dur_us

    for idx, a in enumerate(audio):
        cid = int(a.get("clip_id") or idx + 1)
        rel_name = a.get("_export_rel") or f"audio/{ep_tag}_Audio{cid:03d}.wav"
        a_dur_us = max(200, int(a.get("duration_ms") or 2000)) * 1000
        a_start_us = max(0, int(a.get("start_ms") or 0)) * 1000
        mat_id = str(uuid.uuid4())
        materials_audios.append({
            "id": mat_id,
            "path": f"./{rel_name}",
            "name": f"{ep_tag}_Audio{cid:03d}_{a.get('kind') or 'audio'}",
            "type": "extract_music" if a.get("kind") == "music" else "sound",
            "duration": a_dur_us,
        })
        audio_segments.append({
            "id": str(uuid.uuid4()),
            "material_id": mat_id,
            "source_timerange": {"start": 0, "duration": a_dur_us},
            "target_timerange": {"start": a_start_us, "duration": a_dur_us},
        })

    return {
        "id": str(uuid.uuid4()),
        "name": ep_tag,
        "version": 360000,
        "duration": cursor_us,
        "canvas_config": {"width": width, "height": height, "ratio": f"{width}:{height}"},
        "materials": {
            "videos": materials_videos,
            "audios": materials_audios,
            "subtitles": [{"path": f"./{srt_rel}"}] if srt_rel else [],
        },
        "tracks": [
            {"id": str(uuid.uuid4()), "type": "video", "segments": video_segments},
            {"id": str(uuid.uuid4()), "type": "audio", "segments": audio_segments},
        ],
    }


def build_export_bundle_zip(composition_id: int) -> tuple[bytes, str]:
    """把成片时间线关联的媒体、字幕、剪映草稿 JSON 与 FCPXML 打包为可下载 ZIP 字节。"""
    row = compositions.get(composition_id)
    if row is None:
        raise composition_service.CompositionMissing("成片记录不存在")
    project_id = int(row["project_id"])
    episode_id = int(row["episode_id"])
    ep_seq = int(row["episode_seq"] or 1)
    ep_tag = f"S01E{ep_seq:02d}"

    try:
        timeline = json.loads(row["timeline_json"] or "{}")
        params = json.loads(row["params_json"] or "{}")
    except ValueError as exc:
        raise composition_service.CompositionInvalid("成片时间线格式损坏") from exc

    width = int(row["width"] or params.get("width") or 1280)
    height = int(row["height"] or params.get("height") or 720)
    fps = int(params.get("fps") or 24)
    shots = [dict(s) for s in (timeline.get("shots") or [])]
    audio = [dict(a) for a in (timeline.get("audio") or [])]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, shot in enumerate(shots):
            seq = int(shot.get("seq") or idx + 1)
            vinfo = shot.get("video") or {}
            src = resolve_timeline_media(vinfo)
            suffix = src.suffix if src is not None and src.suffix else ".mp4"
            export_rel = f"media/{ep_tag}_Shot{seq:03d}{suffix}"
            shot["_export_rel"] = export_rel
            if src is not None and src.is_file():
                zf.write(src, arcname=export_rel)

        for idx, clip in enumerate(audio):
            cid = int(clip.get("clip_id") or idx + 1)
            src = jobfiles.resolve_artifact(str(clip.get("path") or ""))
            suffix = src.suffix if src is not None and src.suffix else ".wav"
            kind = str(clip.get("kind") or "audio")
            export_rel = f"audio/{ep_tag}_Audio{cid:03d}_{kind}{suffix}"
            clip["_export_rel"] = export_rel
            if src is not None and src.is_file():
                zf.write(src, arcname=export_rel)

        srt_rel = f"subtitles/{ep_tag}.srt"
        sub_list = timeline.get("subtitles") or []
        sub_bytes: bytes | None = None
        if sub_list:
            sp = str(sub_list[0].get("path") or "")
            sub_file = (jobfiles.resolve_subtitle(project_id, sp)
                        if sp.startswith("subtitles/") else jobfiles.resolve_artifact(sp))
            if sub_file is not None and sub_file.is_file():
                sub_bytes = sub_file.read_bytes()
        if sub_bytes is None:
            try:
                sub_bytes = subtitles.build_srt_text(project_id, episode_id).encode("utf-8")
            except subtitles.SubtitleInvalid:
                sub_bytes = b""
        if sub_bytes:
            zf.writestr(srt_rel, sub_bytes)
        else:
            srt_rel = ""

        draft = _build_jianying_draft(ep_tag, shots, audio, srt_rel, width, height)
        fcpxml = _build_fcpxml(ep_tag, shots, audio, fps, width, height)
        zf.writestr("draft_content.json", json.dumps(draft, ensure_ascii=False, indent=2))
        zf.writestr(
            "draft_meta_info.json",
            json.dumps({"draft_name": ep_tag, "version": int(row["version"]),
                        "composition_id": composition_id}, ensure_ascii=False, indent=2),
        )
        zf.writestr("timeline.fcpxml", fcpxml)

    return buf.getvalue(), f"{ep_tag}_v{row['version']}_bundle.zip"
