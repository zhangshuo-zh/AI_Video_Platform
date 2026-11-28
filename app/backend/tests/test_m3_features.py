"""M3 核心功能测试：首尾帧衔接抽帧、Animatic 预演合成、剪映/FCPXML 导出包、角色 TTS 批量配音与 BGM 自动闪避。"""
from __future__ import annotations

import io
import json
import math
import struct
import subprocess
import uuid
import wave
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

import adapters
import jobfiles
import runners
import services
import worker
from composition import service as composition_service
from config import FFMPEG_BIN, FILES_DIR
from db import get_conn
from pipeline_pkg import execute as pipeline_execute
from repositories.audio import clips as audio_repo
from repositories.content import assets, episodes, shots
from repositories.jobs import jobs as job_repo
from repositories.projects import projects as project_repo
from repositories.reviews import reviews


def _has_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG_BIN, "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _write_test_png(path: Path, color: str = "teal") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _has_ffmpeg():
        subprocess.run([
            FFMPEG_BIN, "-hide_banner", "-y", "-f", "lavfi",
            "-i", f"color=c={color}:s=320x180", "-frames:v", "1", str(path),
        ], check=True, capture_output=True)
        return path
    # 兜底极简 1x1 PNG
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
        b"\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return path


def _write_test_wav(path: Path, seconds: float = 1.0, freq: float = 440.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 16000
    n_samples = int(sample_rate * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            val = int(12000 * math.sin(2.0 * math.pi * freq * i / sample_rate))
            frames.extend(struct.pack("<h", val))
        wf.writeframes(bytes(frames))
    return path


def _make_approved_video(
    project_id: int, episode_id: int, shot_id: int, user_id: int, color: str, seconds: float = 1.2,
    *, second_color: str | None = None,
) -> dict:
    job = job_repo.create({
        "project_id": project_id, "episode_id": episode_id, "shot_id": shot_id,
        "capability": "video", "kind": "video_local", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
        "prompt_snapshot": f"clip-{color}", "params": {}, "input_refs": [],
        "created_by": user_id,
    })
    claimed = job_repo.claim_next("m3-test-worker", 60)
    assert claimed is not None and claimed["id"] == job["id"]
    _, outputs = jobfiles.ensure_dirs(job["id"])
    target = outputs / f"{job['id']}-{color}.mp4"
    color_change = ["-vf", f"drawbox=c={second_color}:t=fill:enable='gte(t,0.5)'"] if second_color else []
    subprocess.run([
        FFMPEG_BIN, "-hide_banner", "-y", "-f", "lavfi",
        "-i", f"color=c={color}:s=640x360:d={seconds}",
        *color_change,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "24", str(target),
    ], check=True, capture_output=True)
    job_repo.succeed(job["id"], "m3-test-worker", "", [jobfiles.relative(target)], 10)
    row = job_repo.get(job["id"])
    reviews.upsert_sign(
        project_id=project_id, target_kind=reviews.JOB, target_id=job["id"],
        gate_id=None, base_version=services.artifact_version(row),
        anchor="artifact", decision="approved", note="ok", user_id=user_id,
    )
    return row


@pytest.mark.skipif(not _has_ffmpeg(), reason="本机没有 ffmpeg")
def test_extract_current_frame_saves_selected_version_without_changing_slots(as_role, people, clean_queue):
    uid = people["editor"]["id"]
    pid = project_repo.create(f"截帧_{uuid.uuid4().hex[:6]}", "", [], uid)["id"]
    eid = episodes.create(pid, "第1集")["id"]
    shot_id = shots.create(eid, "第一镜", "", 2.0, [])["id"]
    next_id = shots.create(eid, "第二镜", "", 2.0, [])["id"]
    shots.set_frame_image(shot_id, last_frame_image="existing-tail.png")
    shots.set_frame_image(next_id, first_frame_image="existing-first.png")
    older = _make_approved_video(pid, eid, shot_id, uid, "red", second_color="blue")
    _make_approved_video(pid, eid, shot_id, uid, "green")

    options = {"offset_ms": 800, "apply_to_next_shot": False, "apply_to_self_last_frame": False}
    with as_role("editor") as c:
        default = c.post(f"/api/shots/{shot_id}/extract-frame", json={**options, "offset_ms": 0})
        assert default.status_code == 200
        r = c.post(f"/api/shots/{shot_id}/extract-frame", json={**options, "job_id": older["id"]})
        assert r.status_code == 200
        frame = r.json()
        assert frame["next_shot_id"] == next_id
        assert not frame["applied_to_next_shot"] and not frame["applied_to_self_last_frame"]
        image = c.get(frame["frame_url"])
        assert image.status_code == 200
        assert image.content.startswith(b"\x89PNG")
        wrong_shot = c.post(f"/api/shots/{next_id}/extract-frame", json={**options, "job_id": older["id"]})
        assert wrong_shot.status_code == 422

    pixels = subprocess.run([
        FFMPEG_BIN, "-v", "error", "-i", str(FILES_DIR / frame["frame_path"]),
        "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ], check=True, capture_output=True).stdout
    red, green, blue = pixels
    assert red < 30 and green < 30 and blue > 200
    assert shots.get(shot_id)["last_frame_image"] == "existing-tail.png"
    assert shots.get(next_id)["first_frame_image"] == "existing-first.png"


@pytest.mark.skipif(not _has_ffmpeg(), reason="本机没有 ffmpeg")
def test_m3_1_extract_tail_frame_and_first_last_video_spec(as_role, people, clean_queue, monkeypatch):
    """M3.1: 截尾帧存为下一镜首帧、工作台访问校验、_MLX_VIDEO 首尾帧 CLI 拼接及可选尾帧防误填。"""
    uid = people["editor"]["id"]
    pid = project_repo.create(f"M3首尾帧_{uuid.uuid4().hex[:6]}", "", [], uid)["id"]
    eid = episodes.create(pid, "第1集")["id"]
    s1 = shots.create(eid, "镜一：夜街推镜", "rainy street", 2.0, [], camera_move="推镜")["id"]
    s2 = shots.create(eid, "镜二：门前环绕", "temple gate", 2.0, [], camera_move="环绕")["id"]

    _make_approved_video(pid, eid, s1, uid, "crimson", seconds=1.2)

    with as_role("editor") as c:
        # 从第 1 镜正式视频抽取尾帧，自动写入第 2 镜 first_frame_image
        res = c.post(f"/api/shots/{s1}/extract-frame", json={
            "position": "last", "apply_to_next_shot": True,
        })
        assert res.status_code == 200
        body = res.json()
        assert body["applied_to_next_shot"] is True
        assert body["next_shot_id"] == s2
        rel = body["frame_path"]
        assert rel.startswith(f"shots/{pid}/")
        assert (FILES_DIR / rel).is_file()

        # 授权读取 /files/shots/<pid>/<name> 正常返回 PNG，且工作台带回 next_shot_id 与首帧槽位
        img_resp = c.get(body["frame_url"])
        assert img_resp.status_code == 200
        assert img_resp.content.startswith(b"\x89PNG")

        wb1 = c.get(f"/api/shots/{s1}/workbench").json()
        assert wb1["next_shot_id"] == s2
        wb2 = c.get(f"/api/shots/{s2}/workbench").json()
        assert wb2["setting"]["first_frame_image"] == rel

        # 本集最后一镜无下一镜，指定存入当前镜头尾帧槽位
        _make_approved_video(pid, eid, s2, uid, "navy", seconds=1.2)
        res2 = c.post(f"/api/shots/{s2}/extract-frame", json={
            "position": "last", "apply_to_next_shot": False, "apply_to_self_last_frame": True,
        })
        assert res2.status_code == 200
        b2 = res2.json()
        assert b2["applied_to_next_shot"] is False
        assert b2["applied_to_self_last_frame"] is True
        assert b2["next_shot_id"] is None

    # 校验 _MLX_VIDEO 的 last_frame_image 可选标记与 --last-image CLI 构造
    monkeypatch.setattr(services, "resolve_provider", lambda cap: {
        "provider": "mlx-video", "model": "wan2.2-i2v-a14b", "backend": "local",
        "adapter": adapters.find("video", "local", "mlx-video"),
    })
    assert pipeline_execute._missing_image_params("video", {"image": rel}) == []
    import runtime
    fake_probe = type("P", (), {
        "state": "ok",
        "ok": True,
        "values": {"python": "/usr/local/bin/python3", "model": "/models/wan2.2-i2v"},
        "problems": [],
    })()
    monkeypatch.setattr(runtime, "probe_mlx_video", lambda: fake_probe)
    s2_row = shots.get(s2)
    spec = runners.build(
        {
            "id": 999901, "project_id": pid, "capability": "video",
            "provider_code": "mlx-video", "model_code": "wan2.2-i2v-a14b",
            "execution_mode": "local", "prompt_snapshot": "首尾帧平滑运镜",
        },
        {"image": rel, "last_frame_image": s2_row["last_frame_image"], "duration": 2},
        FILES_DIR.parent / "outputs" / "m3_probe_test",
    )
    assert "--image" in spec.argv and "--last-image" in spec.argv
    last_idx = spec.argv.index("--last-image")
    assert spec.argv[last_idx + 1].endswith(".png")


@pytest.mark.skipif(not _has_ffmpeg(), reason="本机没有 ffmpeg")
def test_m3_2_and_3_3_animatic_composition_and_export_bundle_zip(as_role, people, clean_queue):
    """M3.2 + M3.3: 无视频走 Animatic 动态故事板合成成功，并可一键导出剪映 draft + FCPXML ZIP 工程包。"""
    uid = people["editor"]["id"]
    pid = project_repo.create(f"M3预演导出_{uuid.uuid4().hex[:6]}", "", [], uid)["id"]
    eid = episodes.create(pid, "第1集")["id"]

    # 镜 1 使用 shot.first_frame_image；镜 2 使用角色参考图
    img1 = f"shots/{pid}/{uuid.uuid4().hex[:8]}.png"
    _write_test_png(FILES_DIR / img1, color="goldenrod")
    char_img = f"test_assets/{uuid.uuid4().hex[:8]}.png"
    _write_test_png(FILES_DIR / char_img, color="steelblue")
    cid = assets.create(pid, "character", "顾清河", "主角", "", [])["id"]
    assets.add_image(cid, char_img)

    s1 = shots.create(eid, "顾清河：「雨夜出发！」", "", 1.2, [cid], first_frame_image=img1)["id"]
    s2 = shots.create(eid, "远景：古道孤灯", "", 1.0, [cid])["id"]
    assert s1 and s2

    with as_role("editor") as c:
        # 标准模式没有已签字视频 -> 422 阻塞
        std_bad = c.post(f"/api/projects/{pid}/compositions", json={
            "episode_id": eid, "width": 640, "height": 360, "fps": 24, "mode": "standard",
        })
        assert std_bad.status_code == 422

        # Animatic 预演模式成功建单并跑通真实 FFmpeg
        ani_res = c.post(f"/api/projects/{pid}/compositions", json={
            "episode_id": eid, "width": 640, "height": 360, "fps": 24, "mode": "animatic",
        })
        assert ani_res.status_code == 201
        comp = ani_res.json()
        assert comp["params"]["mode"] == "animatic"

    assert worker.run_once("m3-animatic-worker") == comp["job_id"]
    done = composition_service.view(comp["id"])
    assert done["status"] == "succeeded"
    assert 1800 <= done["duration_ms"] <= 2600

    # 验证 M3.3 ZIP 导出包（包含 draft_content.json、timeline.fcpxml、media/*.png、subtitles/*.srt）
    with as_role("editor") as c:
        z_res = c.get(f"/api/compositions/{comp['id']}/export-bundle.zip")
        assert z_res.status_code == 200
        assert z_res.headers["content-type"].startswith("application/zip")

    with zipfile.ZipFile(io.BytesIO(z_res.content)) as zf:
        names = set(zf.namelist())
        assert "draft_content.json" in names
        assert "draft_meta_info.json" in names
        assert "timeline.fcpxml" in names
        assert any(n.startswith("media/S01E01_Shot001") for n in names)
        assert any(n.startswith("media/S01E01_Shot002") for n in names)
        assert "subtitles/S01E01.srt" in names

        draft = json.loads(zf.read("draft_content.json").decode("utf-8"))
        assert len(draft["materials"]["videos"]) == 2
        assert draft["materials"]["videos"][0]["material_name"] == "S01E01_Shot001"
        assert draft["duration"] >= 2_000_000

        fcpxml_root = ET.fromstring(zf.read("timeline.fcpxml").decode("utf-8"))
        assert fcpxml_root.tag == "fcpxml"
        clips_xml = fcpxml_root.findall(".//spine/asset-clip")
        assert len(clips_xml) >= 2
        assert clips_xml[0].attrib["name"] == "S01E01_Shot001"


@pytest.mark.skipif(not _has_ffmpeg(), reason="本机没有 ffmpeg")
def test_m3_4_character_tts_batch_and_bgm_ducking(
    as_role, people, clean_queue, provider_state, register_tier, monkeypatch
):
    """M3.4: 人物档案专属 TTS 音色绑定、整集批量配音建单/注参及 FFmpeg BGM 自动闪避对白。"""
    monkeypatch.setattr(adapters, "health",
                        lambda row, **kw: {"state": "ok", "detail": "测试可达",
                                           "adapter": None, "key_env": ""})
    with get_conn() as conn:
        conn.execute("UPDATE provider_config SET is_default = 0 WHERE capability = 'tts'")
        conn.execute(
            """UPDATE provider_config
                  SET provider = 'voxcpm2', model = 'voxcpm2',
                      enabled = 1, is_default = 1, key_env = ''
                WHERE capability = 'tts' AND backend = 'local'""")
    register_tier("tts", "local", "voxcpm2", "voxcpm2")

    uid = people["editor"]["id"]
    pid = project_repo.create(f"M3配音闪避_{uuid.uuid4().hex[:6]}", "", [], uid)["id"]
    eid = episodes.create(pid, "第1集")["id"]
    char_id = assets.create(pid, "character", "林小满", "女主角", "", ["小满"])["id"]
    s1 = shots.create(eid, "林小满：「风停了，我们出发！」", "", 1.5, [char_id])["id"]
    s2 = shots.create(eid, "山门夜景", "", 1.5, [char_id])["id"]

    with as_role("editor") as c:
        p_res = c.put(f"/api/assets/{char_id}/character-profile", json={
            "biography": "北境守夜人后裔", "personality": "外冷内热",
            "growth_arc": "", "text_prompt": "",
            "tts_voice": "清冷少女音", "tts_rate": 1.25, "tts_mood": "低沉坚定",
        })
        assert p_res.status_code == 200
        p_body = p_res.json()
        assert p_body["tts_voice"] == "清冷少女音"
        assert p_body["tts_rate"] == pytest.approx(1.25)
        assert p_body["tts_mood"] == "低沉坚定"

        # 先手工建好第 2 镜对白草稿
        c.post(f"/api/projects/{pid}/audio-clips", json={
            "episode_id": eid, "shot_id": s2, "kind": "dialogue",
            "speaker": "林小满", "text": "前面有埋伏！",
        })

        # 批量配音建单（自动从 s1 摘要抽台词建草稿并匹配人物音色）
        batch_res = c.post(f"/api/projects/{pid}/episodes/{eid}/audio/batch-tts", json={
            "overwrite_existing": False, "auto_submit": False,
        })
        assert batch_res.status_code == 200
        b_out = batch_res.json()
        assert len(b_out["created_clip_ids"]) == 1
        assert "林小满" in b_out["voice_bindings"]
        params = b_out["voice_bindings"]["林小满"]["params"]
        assert params["speed"] == pytest.approx(1.25)
        assert "清冷少女音" in params["instruct"]
        assert "低沉坚定" in params["instruct"]

        # 开启 auto_submit=True，走真实 VoxCPM2 默认路线校验并落 Job 参数
        sub_res = c.post(f"/api/projects/{pid}/episodes/{eid}/audio/batch-tts", json={
            "overwrite_existing": False, "auto_submit": True,
        })
        assert sub_res.status_code == 200
        sub_data = sub_res.json()
        assert len(sub_data["submitted_job_ids"]) == 2
        submitted_job = job_repo.get(sub_data["submitted_job_ids"][0])
        assert json.loads(submitted_job["params_json"])["speed"] == pytest.approx(1.25)

        for jid in sub_data["submitted_job_ids"]:
            c.post(f"/api/jobs/{jid}/cancel")

    # 实测 FFmpeg BGM 自动闪避对白混音合成
    # 给两段对白与一条 BGM 配上真 WAV，并将两镜建出真视频
    _make_approved_video(pid, eid, s1, uid, "purple", seconds=1.2)
    _make_approved_video(pid, eid, s2, uid, "teal", seconds=1.2)

    existing_clips = audio_repo.list(pid, None, eid)
    for clip in existing_clips:
        _, a_out = jobfiles.ensure_dirs(clip["job_id"] or 9001)
        wav_path = _write_test_wav(a_out / f"voice-{clip['id']}.wav", seconds=0.8, freq=520.0)
        audio_repo.finish(clip["id"], status="ready", path=jobfiles.relative(wav_path), duration_ms=800)

    bgm_clip = audio_repo.create(
        project_id=pid, episode_id=eid, shot_id=None, kind="music",
        speaker="", text="紧张低沉配乐", created_by=uid,
    )
    _, bgm_out = jobfiles.ensure_dirs(9002)
    bgm_wav = _write_test_wav(bgm_out / "bgm.wav", seconds=2.5, freq=220.0)
    audio_repo.finish(bgm_clip["id"], status="ready", path=jobfiles.relative(bgm_wav), duration_ms=2500)

    with as_role("editor") as c:
        comp_res = c.post(f"/api/projects/{pid}/compositions", json={
            "episode_id": eid, "width": 640, "height": 360, "fps": 24,
            "mode": "standard", "bgm_ducking": True,
        })
        assert comp_res.status_code == 201
        comp = comp_res.json()

    assert worker.run_once("m3-ducking-worker") == comp["job_id"]
    done = composition_service.view(comp["id"])
    assert done["status"] == "succeeded"
    out_file = jobfiles.resolve_artifact(done["output_path"])
    assert out_file is not None and out_file.is_file()
    receipt = json.loads((out_file.parent / f"{comp['job_id']}-compose.json").read_text("utf-8"))
    assert receipt["bgm_ducking"] is True
    assert receipt["audio_inputs"] == 3
