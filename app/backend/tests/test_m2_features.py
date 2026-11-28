"""M2 核心功能测试：单镜粗剪与转场、镜头候选落库、自动字幕、Img2Img 与形态传图。"""
from __future__ import annotations

import base64
import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import adapters
import engine_http_run
import jobfiles
import runners
import services
import worker
from composition import service as composition_service
from config import FFMPEG_BIN
from repositories.audio import clips as audio_repo
from repositories.content import assets, episodes, shots
from repositories.extraction import runs as extractions
from repositories.jobs import jobs as job_repo
from repositories.projects import projects as project_repo
from repositories.reviews import reviews

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
    b"\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _has_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG_BIN, "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _make_video_clip(
    project_id: int, episode_id: int, shot_id: int, user_id: int, color: str, seconds: float = 1.5
) -> dict:
    """用 FFmpeg 生成一条真视频片段，并签署通过使其成为正式视频。"""
    job = job_repo.create({
        "project_id": project_id, "episode_id": episode_id, "shot_id": shot_id,
        "capability": "video", "kind": "video_local", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
        "prompt_snapshot": f"clip-{color}", "params": {}, "input_refs": [],
        "created_by": user_id,
    })
    claimed = job_repo.claim_next("m2-test-worker", 60)
    assert claimed is not None and claimed["id"] == job["id"]
    _, outputs = jobfiles.ensure_dirs(job["id"])
    target = outputs / f"{job['id']}-{color}.mp4"
    subprocess.run([
        FFMPEG_BIN, "-hide_banner", "-y", "-f", "lavfi",
        "-i", f"color=c={color}:s=640x360:d={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "24", str(target),
    ], check=True, capture_output=True)
    job_repo.succeed(job["id"], "m2-test-worker", "", [jobfiles.relative(target)], 10)
    row = job_repo.get(job["id"])
    reviews.upsert_sign(
        project_id=project_id, target_kind=reviews.JOB, target_id=job["id"],
        gate_id=None, base_version=services.artifact_version(row),
        anchor="artifact", decision="approved", note="ok", user_id=user_id,
    )
    return row


def test_m2_1_shot_trim_and_transition_validation(as_role, people):
    """M2.1: 镜头出入点与转场字段持久化、工作台回显以及边界校验。"""
    pid = project_repo.create(f"M2粗剪_{uuid.uuid4().hex[:6]}", "", [], people["editor"]["id"])["id"]
    eid = episodes.create(pid, "第1集")["id"]
    sid = shots.create(eid, "夜街开场", "夜雨长街", 5.0, [])["id"]

    with as_role("editor") as c:
        bad_trim = c.put(f"/api/shots/{sid}", json={
            "summary": "夜雨长街", "duration": 5.0,
            "trim_in_ms": 1500, "trim_out_ms": 1200, "transition": "cut",
        })
        assert bad_trim.status_code == 422

        bad_trans = c.put(f"/api/shots/{sid}", json={
            "summary": "夜雨长街", "duration": 5.0,
            "trim_in_ms": 200, "trim_out_ms": 1200, "transition": "wipe_spiral",
        })
        assert bad_trans.status_code == 422

        ok = c.put(f"/api/shots/{sid}", json={
            "summary": "夜雨长街", "prompt": "细雨反光", "duration": 5.0,
            "trim_in_ms": 300, "trim_out_ms": 1800, "transition": "crossfade",
        })
        assert ok.status_code == 200
        body = ok.json()
        assert body["trim_in_ms"] == 300
        assert body["trim_out_ms"] == 1800
        assert body["transition"] == "crossfade"

        wb = c.get(f"/api/shots/{sid}/workbench")
        assert wb.status_code == 200
        setting = wb.json()["setting"]
        assert setting["trim_in_ms"] == 300
        assert setting["trim_out_ms"] == 1800
        assert setting["transition"] == "crossfade"


@pytest.mark.skipif(not _has_ffmpeg(), reason="本机没有 ffmpeg")
def test_m2_1_composition_ffmpeg_trim_and_transitions(as_role, people, clean_queue):
    """M2.1: 真 FFmpeg 按出入点精确裁剪并应用 fade_black 与 crossfade 转场。"""
    uid = people["editor"]["id"]
    pid = project_repo.create(f"M2合成转场_{uuid.uuid4().hex[:6]}", "", [], uid)["id"]
    eid = episodes.create(pid, "第1集")["id"]
    s1 = shots.create(eid, "镜一", "", 2.0, [])["id"]
    s2 = shots.create(eid, "镜二", "", 2.0, [])["id"]

    _make_video_clip(pid, eid, s1, uid, "red", seconds=1.6)
    _make_video_clip(pid, eid, s2, uid, "blue", seconds=1.6)

    with as_role("editor") as c:
        assert c.put(f"/api/shots/{s1}", json={
            "summary": "镜一", "duration": 2.0,
            "trim_in_ms": 200, "trim_out_ms": 1000, "transition": "fade_black",
        }).status_code == 200
        assert c.put(f"/api/shots/{s2}", json={
            "summary": "镜二", "duration": 2.0,
            "trim_in_ms": 100, "trim_out_ms": 900, "transition": "crossfade",
        }).status_code == 200

        r = c.post(f"/api/projects/{pid}/compositions", json={
            "episode_id": eid, "width": 640, "height": 360, "fps": 24,
        })
        assert r.status_code == 201
        comp = r.json()

    assert worker.run_once("m2-compose-worker") == comp["job_id"]

    done = composition_service.view(comp["id"])
    assert done["status"] == "succeeded"
    out_file = jobfiles.resolve_artifact(done["output_path"])
    assert out_file is not None and out_file.exists() and out_file.stat().st_size > 0
    receipt = json.loads((out_file.parent / f"{comp['job_id']}-compose.json").read_text("utf-8"))
    assert receipt["transitions"] == ["fade_black", "crossfade"]
    # 两镜净长各 800ms，crossfade 重叠 200ms -> 约 1400ms
    assert 1200 <= done["duration_ms"] <= 1550


def test_m2_2_import_shots_from_extraction(as_role, people, clean_queue):
    """M2.2: 已接纳镜头候选一键导入分镜表，自动绑定同名资产且幂等防重。"""
    uid = people["editor"]["id"]
    pid = project_repo.create(f"M2导入_{uuid.uuid4().hex[:6]}", "", [], uid)["id"]
    eid = episodes.create(pid, "第1集")["id"]
    char_a = assets.create(pid, "character", "掌灯人", "", "", ["提灯老者"])["id"]
    scene_a = assets.create(pid, "scene", "灯市", "", "", [])["id"]

    job = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": None,
        "capability": "text", "kind": "text", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
        "prompt_snapshot": "extract", "params": {}, "input_refs": [],
        "created_by": uid,
    })
    claimed = job_repo.claim_next("m2-extract-worker", 60)
    assert claimed is not None and claimed["id"] == job["id"]
    job_repo.succeed(job["id"], "m2-extract-worker", "", [], 5)
    run = extractions.create_run(
        project_id=pid, doc_id=None, job_id=job["id"], doc_title="测试稿",
        base_version="v1", content_hash="h1", model_code="mock-1",
        prompt_version="v1", created_by=uid,
    )
    extractions.insert_items(run["id"], [
        {
            "kind": "shot",
            "payload": {
                "index": 1, "scene": "灯市", "summary": "掌灯人举灯等在灯柱下",
                "shot_size": "中景", "camera_move": "推镜头", "duration_hint": 6,
                "dialogue": ["你迟了三年。"], "asset_refs": ["掌灯人", "灯市"],
            },
            "evidence": {"quote": "掌灯人举灯等在灯柱下"},
            "confidence": 0.9,
        },
        {
            "kind": "shot",
            "payload": {
                "index": 2, "summary": "尚未审核通过的镜头", "duration_hint": 4,
            },
            "evidence": {"quote": "尚未审核通过的镜头"},
            "confidence": 0.5,
        },
    ])
    items = extractions.items(run["id"], kind="shot")
    accepted_item = next(i for i in items if "掌灯人" in i["payload"])
    rejected_item = next(i for i in items if "尚未审核" in i["payload"])
    extractions.decide(accepted_item["id"], "accepted", uid, asset_id=None)
    extractions.decide(rejected_item["id"], "rejected", uid, asset_id=None)

    with as_role("editor") as c:
        r1 = c.post(f"/api/extract/runs/{run['id']}/import-shots", json={"episode_id": eid})
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["episode_id"] == eid
        assert len(d1["created_shot_ids"]) == 1
        assert d1["skipped_item_ids"] == [rejected_item["id"]]
        assert d1["matched_assets"] >= 2

        shot_rows = c.get(f"/api/episodes/{eid}/shots").json()
        assert len(shot_rows) == 1
        assert shot_rows[0]["summary"] == "掌灯人举灯等在灯柱下"
        assert char_a in shot_rows[0]["asset_ids"]
        assert scene_a in shot_rows[0]["asset_ids"]

        r2 = c.post(f"/api/extract/runs/{run['id']}/import-shots", json={"episode_id": eid})
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["created_shot_ids"] == []
        assert len(d2["skipped_item_ids"]) == 2


def test_import_shots_filters_item_ids(as_role, fresh_project, clean_queue):
    uid = as_role.id("editor")
    project_id = fresh_project()
    episode_id = episodes.create(project_id, "第1集")["id"]
    job = job_repo.create({
        "project_id": project_id, "episode_id": episode_id,
        "capability": "text", "kind": "text", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
        "prompt_snapshot": "extract", "params": {}, "input_refs": [], "created_by": uid,
    })
    claimed = job_repo.claim_next("selective-import", 60)
    assert claimed is not None and claimed["id"] == job["id"]
    job_repo.succeed(job["id"], "selective-import", "", [], 5)
    run = extractions.create_run(
        project_id=project_id, doc_id=None, job_id=job["id"], doc_title="测试稿",
        base_version="v1", content_hash="h1", model_code="mock-1",
        prompt_version="v1", created_by=uid,
    )
    run_id = run["id"]
    extractions.insert_items(run_id, [
        {"kind": "shot", "payload": {"index": 1, "summary": "未选中的镜头"}, "evidence": {}},
        {"kind": "shot", "payload": {"index": 2, "summary": "选中的镜头"}, "evidence": {}},
        {"kind": "character", "payload": {"name": "角色"}, "evidence": {}},
    ])
    extractions.set_status(run_id, "done", materialized=True)
    unselected, selected = extractions.items(run_id, kind="shot")
    character, = extractions.items(run_id, kind="character")

    url = f"/api/extract/runs/{run_id}/import-shots"
    with as_role("editor") as c:
        empty = c.post(url, json={"episode_id": episode_id, "item_ids": []})
        assert empty.status_code == 200, empty.text
        assert empty.json()["created_shot_ids"] == []
        assert shots.list_by_episode(episode_id) == []

        selection = {"episode_id": episode_id, "item_ids": [selected["id"], character["id"], selected["id"]]}
        imported = c.post(url, json=selection)
        assert imported.status_code == 200, imported.text
        shot, = shots.list_by_episode(episode_id)
        assert imported.json()["created_shot_ids"] == [shot["id"]]
        assert imported.json()["skipped_item_ids"] == []
        assert shot["summary"] == "选中的镜头"
        assert extractions.get_item(selected["id"])["status"] == "accepted"
        assert extractions.get_item(unselected["id"])["status"] == "pending"
        assert extractions.get_item(character["id"])["status"] == "pending"

        retry = c.post(url, json=selection)
        assert retry.status_code == 200, retry.text
        assert retry.json()["created_shot_ids"] == []
        assert retry.json()["skipped_item_ids"] == [selected["id"]]

        bulk = c.post(url, json={"episode_id": episode_id, "item_ids": None})
        assert bulk.status_code == 200, bulk.text
        assert len(bulk.json()["created_shot_ids"]) == 1
        assert extractions.get_item(unselected["id"])["status"] == "accepted"


def test_m2_3_auto_generate_srt_from_shots(as_role, people):
    """M2.3: 按分镜对白与时长自动生成 `.srt` 字幕，对白音轨优先于分镜摘要。"""
    uid = people["editor"]["id"]
    pid = project_repo.create(f"M2字幕_{uuid.uuid4().hex[:6]}", "", [], uid)["id"]
    eid = episodes.create(pid, "第1集")["id"]
    s1 = shots.create(eid, "开场空镜摘要", "", 3.0, [], trim_in_ms=500, trim_out_ms=2500)["id"]
    shots.create(eid, "第二镜旁白", "", 4.0, [])
    audio_repo.create(
        project_id=pid, episode_id=eid, shot_id=s1, kind="dialogue",
        speaker="掌灯人", text="你迟了三年。", created_by=uid,
    )

    with as_role("editor") as c:
        r = c.post(f"/api/projects/{pid}/episodes/{eid}/composition-subtitles/auto")
        assert r.status_code == 201
        data = r.json()
        gen_path = data["generated_path"]
        assert gen_path.startswith("subtitles/") and gen_path.endswith(".srt")
        assert any(it["path"] == gen_path for it in data["items"])

        srt_file = jobfiles.resolve_subtitle(pid, gen_path)
        assert srt_file is not None and srt_file.exists()
        srt_text = srt_file.read_text(encoding="utf-8")
        assert "00:00:00,000 --> 00:00:02,000" in srt_text
        assert "你迟了三年。" in srt_text
        assert "开场空镜摘要" not in srt_text
        assert "00:00:02,000 --> 00:00:06,000" in srt_text
        assert "第二镜旁白" in srt_text


def test_m2_4_variant_image_upload_and_draw_things_img2img(as_role, people, monkeypatch, tmp_path):
    """M2.4: 形态本地传图持久化与校验，以及 Draw Things img2img 参数和 HTTP 回执链路。"""
    uid = people["editor"]["id"]
    pid = project_repo.create(f"M2图生图_{uuid.uuid4().hex[:6]}", "", [], uid)["id"]
    aid = assets.create(pid, "character", "林昭", "", "", [])["id"]

    with as_role("editor") as c:
        vc = c.post(f"/api/assets/{aid}/variants", json={
            "variant_type": "outfit", "name": "战斗服", "prompt": "战损风衣",
        })
        assert vc.status_code == 201
        variant = vc.json()["id"]

        bad = c.post(
            f"/api/asset-variants/{variant}/images",
            files={"file": ("bad.png", b"not-an-image", "image/png")},
        )
        assert bad.status_code == 422

        ok = c.post(
            f"/api/asset-variants/{variant}/images",
            files={"file": ("pose.png", _TINY_PNG, "image/png")},
        )
        assert ok.status_code == 200
        vdata = ok.json()
        assert vdata["image_count"] == 1
        rel_path = vdata["ref_images"][0]
        assert c.get(f"/files/{rel_path}").status_code == 200

    # Draw Things adapter 校验 + img2img payload 构建 + engine_http_run.py 验证
    adp = next(a for a in adapters.ADAPTERS if a.code == "draw_things")
    cleaned = {"init_image": rel_path, "strength": 0.6}
    assert adapters.validate_params(adp, adp.models[0].code, cleaned) == []

    monkeypatch.setattr(
        runners.engines, "probe",
        lambda code, fresh=False: SimpleNamespace(
            state="ok", values={"endpoint": "http://127.0.0.1:17860", "model": ""}
        ),
    )
    job_stub = {"id": 999991, "project_id": pid, "prompt_snapshot": "夜色战斗服", "input_refs": []}
    spec = runners._draw_things(job_stub, cleaned, tmp_path, "dt-sdxl")
    req_file = Path(spec.argv[2])
    req_data = json.loads(req_file.read_text("utf-8"))
    assert req_data["path"] == "/sdapi/v1/img2img"
    assert req_data["init_image"].endswith(Path(rel_path).name)
    assert req_data["body"]["denoising_strength"] == 0.6

    captured: dict = {}

    class _FakeResp:
        status = 200

        def __init__(self, data: bytes):
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_urlopen(req, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/sdapi/v1/img2img"):
            captured["url"] = url
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp(json.dumps({"images": [base64.b64encode(_TINY_PNG + b"0" * 100).decode()]}).encode())
        return _FakeResp(json.dumps({"model": "sdxl-test"}).encode())

    monkeypatch.setattr(engine_http_run.safenet, "safe_urlopen", _fake_urlopen)
    monkeypatch.setattr("sys.argv", ["engine_http_run.py", str(req_file)])
    assert engine_http_run.main() == 0
    receipt = Path(req_data["receipt"])
    rec = json.loads(receipt.read_text("utf-8"))
    assert rec["ok"] is True
    assert rec["mode"] == "img2img"
    assert "init_images" in captured["payload"]
    assert captured["payload"]["init_images"][0] == base64.b64encode(_TINY_PNG).decode()
