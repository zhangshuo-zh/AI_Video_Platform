"""M4 核心功能测试：设定中心词缀与矛盾审阅、ComfyUI 工作流与对口型节点、SSE 实时流与废片安全 GC。"""
from __future__ import annotations

import io
import json
import math
import shutil
import struct
import subprocess
import urllib.parse
import uuid
import wave
from pathlib import Path

import pytest

import adapters
import engine_http_run
import imagetypes
import jobfiles
import runners
import safenet
import services
import worker
from config import FFMPEG_BIN, FILES_DIR
from db import get_conn
from pipeline_pkg import execute as pipeline_execute
from pipeline_pkg import templates as pipeline_templates
from repositories.audio import clips as audio_repo
from repositories.content import assets, episodes, shots
from repositories.engine_instances import engine_instances
from repositories.jobs import jobs as job_repo
from repositories.pipeline import pipeline_repo
from repositories.projects import projects as project_repo
from repositories.reviews import reviews
from runners_pkg import inputs as runner_inputs
from services import asset_upscale


def _has_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG_BIN, "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _write_test_png(path: Path, color: str = "cyan") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _has_ffmpeg():
        subprocess.run([
            FFMPEG_BIN, "-hide_banner", "-y", "-f", "lavfi",
            "-i", f"color=c={color}:s=320x180", "-frames:v", "1", str(path),
        ], check=True, capture_output=True)
        if path.stat().st_size >= 64:
            return path
    # 兜底满足 >= 64 字节门槛的最小合法 PNG
    png_stub = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
        b"\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
        + b"\x00" * 32
    )
    path.write_bytes(png_stub)
    return path


def _write_test_wav(path: Path, seconds: float = 0.8, freq: float = 440.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sr = 16000
    n_samples = int(sr * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        buf = bytearray()
        for i in range(n_samples):
            val = int(12000 * math.sin(2.0 * math.pi * freq * i / sr))
            buf.extend(struct.pack("<h", val))
        wf.writeframes(bytes(buf))
    return path


def test_m4_knowledge_affixes_crud_and_validation(as_role, people):
    c = as_role("admin")
    pid = project_repo.create(
        f"词缀项目_{uuid.uuid4().hex[:6]}", "", [], people["admin"]["id"],
    )["id"]

    # 正负词缀同时为空应 422
    bad = c.post(f"/api/projects/{pid}/knowledge/affixes", json={
        "name": "空模板", "category": "general", "positive": "", "negative": "",
    })
    assert bad.status_code == 422

    # 创建正向/负向默认词缀
    created = c.post(f"/api/projects/{pid}/knowledge/affixes", json={
        "name": "新海诚透亮光影",
        "category": "lighting",
        "positive": "anime aesthetic, volumetric sunlight, rim light",
        "negative": "3d render, blurry",
        "is_default": True,
        "sort_order": 5,
    })
    assert created.status_code == 201, created.text
    affix = created.json()
    assert affix["is_default"] is True
    assert affix["category"] == "lighting"

    listed = c.get(f"/api/projects/{pid}/knowledge/affixes")
    assert listed.status_code == 200
    assert any(a["id"] == affix["id"] for a in listed.json())

    updated = c.put(f"/api/projects/{pid}/knowledge/affixes/{affix['id']}", json={
        "name": "赛璐璐高反差",
        "category": "quality",
        "positive": "cel shading, crisp lineart",
        "negative": "photorealistic",
        "is_default": False,
        "sort_order": 1,
    })
    assert updated.status_code == 200
    assert updated.json()["name"] == "赛璐璐高反差"
    assert updated.json()["is_default"] is False

    deleted = c.delete(f"/api/projects/{pid}/knowledge/affixes/{affix['id']}")
    assert deleted.status_code == 204
    assert c.get(f"/api/projects/{pid}/knowledge/affixes").json() == []


def test_m4_consistency_scan_and_adopt_reopen(as_role, people):
    c = as_role("admin")
    uid = people["admin"]["id"]
    pid = project_repo.create(f"设定扫描_{uuid.uuid4().hex[:6]}", "", [], uid)["id"]
    eid = episodes.create(pid, "第一集")["id"]

    # 资产 A（缺 hero_image，有别名「小雨」）；资产 B（冲突别名「小雨」）
    a1 = assets.create(pid, "character", "林小雨", "蓝发少女", "", ["小雨"])
    a2 = assets.create(pid, "character", "夏小雨", "另一个角色", "", ["小雨"])
    # 分镜 1 提及林小雨但未绑定 asset_ids；分镜 2 已绑定无参考图资产 a2
    shot = shots.create(eid, "林小雨在天台望向远方", "林小雨站在风中", 4.0, [])
    shots.create(eid, "夏小雨在教室里回头", "夏小雨特写", 3.0, [a2["id"]])

    # 剧本写入未登记角色的对话行
    services.create_document(
        pid,
        eid,
        "script",
        "第1集剧本",
        "天台戏\n神秘路人：快看天空！\n",
        people["admin"]["name"],
    )

    scan = c.post(f"/api/projects/{pid}/knowledge/consistency/scan")
    assert scan.status_code == 200, scan.text
    body = scan.json()
    assert body["scanned_shots"] >= 1
    assert body["scanned_assets"] >= 2
    assert body["new_count"] >= 3
    issues = body["issues"]
    keys = {it["issue_key"] for it in issues}
    assert f"shot_missing_asset:{shot['id']}:{a1['id']}" in keys
    assert any(k.startswith("alias_conflict:") for k in keys)
    assert any(k.startswith("asset_no_ref:") for k in keys)

    # 采纳「分镜漏绑资产」：应自动将林小雨写入 shot.asset_ids_json
    missing_issue = next(it for it in issues if it["issue_key"] == f"shot_missing_asset:{shot['id']}:{a1['id']}")
    adopted = c.post(
        f"/api/projects/{pid}/knowledge/consistency/{missing_issue['id']}/resolve",
        json={"action": "adopt"},
    )
    assert adopted.status_code == 200
    assert adopted.json()["status"] == "adopted"
    shot_after = shots.get(shot["id"])
    assert a1["id"] in services.shot_asset_ids(shot_after)

    # 忽略与重新打开另一条提示
    other = next(it for it in issues if it["id"] != missing_issue["id"])
    ignored = c.post(
        f"/api/projects/{pid}/knowledge/consistency/{other['id']}/resolve",
        json={"action": "ignore"},
    )
    assert ignored.status_code == 200
    assert ignored.json()["status"] == "ignored"
    reopened = c.post(
        f"/api/projects/{pid}/knowledge/consistency/{other['id']}/resolve",
        json={"action": "reopen"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"


@pytest.mark.parametrize(("checkpoint", "installed", "selected"), [
    ("", ["legacy.ckpt", "sdxl.safetensors", "animagine-xl-3.1.safetensors"],
     "animagine-xl-3.1.safetensors"),
    ("", [], None),
    ("my-model.safetensors", [], "my-model.safetensors"),
], ids=["auto-checkpoint", "no-checkpoint", "explicit-checkpoint"])
def test_m4_comfyui_workflow_http_e2e(tmp_path, monkeypatch, checkpoint, installed, selected):
    assert runners.has_branch("image", "comfyui") is True
    adapter = adapters.find("image", "local", "comfyui")
    assert adapter is not None
    spec = next((m for m in adapter.models if m.code == "comfyui-workflow"), None)
    assert spec is not None
    assert any(p.type == "image" for p in spec.params)

    monkeypatch.setattr(runner_inputs, "JOBS_DIR", tmp_path)
    secret_env = "TEST_COMFYUI_SHARED_SECRET"
    monkeypatch.setenv(secret_env, "test-comfyui-token")
    png_out_bytes = (_write_test_png(tmp_path / "server_out.png", color="magenta")).read_bytes()
    params = {"checkpoint": checkpoint}
    if checkpoint:
        params["image"] = str(_write_test_png(tmp_path / "123" / "init.png"))
    endpoint = "https://127.0.0.1:8188"
    requests_seen = []
    submitted_prompt = {}

    def open_comfyui(req, *, timeout):
        url = urllib.parse.urlparse(req.full_url)
        assert f"{url.scheme}://{url.netloc}" == endpoint
        headers = {key.lower(): value for key, value in req.header_items()}
        assert headers["authorization"] == "Bearer test-comfyui-token"
        assert headers["x-shared-secret"] == "test-comfyui-token"
        if url.path != "/system_stats":
            assert headers["ngrok-skip-browser-warning"] == "1"
        requests_seen.append((req.get_method(), url.path))
        if url.path == "/system_stats":
            payload = {"system": {"comfyui_version": "0.3.40"}}
        elif url.path == "/object_info/CheckpointLoaderSimple":
            assert timeout == 20
            payload = {"CheckpointLoaderSimple": {
                "input": {"required": {"ckpt_name": [installed]}},
            }}
        elif url.path == "/upload/image":
            assert b"init.png" in req.data
            payload = {"name": "uploaded_init.png", "subfolder": "", "type": "input"}
        elif url.path == "/prompt":
            submitted_prompt.update(json.loads(req.data)["prompt"])
            payload = {"prompt_id": "comfy-test-123", "node_errors": {}}
        elif url.path == "/history/comfy-test-123":
            payload = {
                "comfy-test-123": {
                    "status": {"status_str": "success"},
                    "outputs": {"9": {"images": [
                        {"filename": "result_001.png", "subfolder": "", "type": "output"},
                    ]}},
                },
            }
        elif url.path == "/view":
            assert urllib.parse.parse_qs(url.query)["filename"] == ["result_001.png"]
            return io.BytesIO(png_out_bytes)
        else:
            pytest.fail(f"意外的 ComfyUI 请求：{req.full_url}")
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(safenet, "safe_urlopen", open_comfyui)
    instance = engine_instances.create({
        "engine_code": "comfyui", "endpoint": endpoint, "shared_secret_env": secret_env,
    })
    try:
        job = {
            "id": 123, "capability": "image", "execution_mode": "local",
            "provider_code": "comfyui", "model_code": "comfyui-workflow",
            "prompt_snapshot": "test anime",
        }
        run = runners.build(job, params, tmp_path)
    finally:
        engine_instances.delete(instance["id"])

    raw_request = Path(run.meta["request_path"]).read_text(encoding="utf-8")
    req = json.loads(raw_request)
    assert req["secret_env"] == secret_env
    assert secret_env in run.pass_env
    assert "test-comfyui-token" not in raw_request
    assert req["workflow"]["1"]["inputs"]["ckpt_name"] == (checkpoint or "__AUTO_CHECKPOINT__")

    out_path = run.expected[0]
    receipt_path = Path(run.meta["result_path"])
    code = engine_http_run._run_comfyui(req, req["endpoint"], out_path, receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected_requests = [("GET", "/system_stats")]
    if not checkpoint:
        expected_requests.append(("GET", "/object_info/CheckpointLoaderSimple"))
    if selected is None:
        assert code == 1
        assert receipt["ok"] is False
        assert receipt["error"] == "comfyui_no_checkpoint"
        assert "models/checkpoints 为空" in receipt["message"]
        assert "Animagine XL 3.1" in receipt["message"]
        assert receipt["endpoint"] == endpoint
        assert not out_path.exists()
        assert requests_seen == expected_requests
        return
    assert code == 0
    assert receipt["ok"] is True
    assert receipt["engine"] == "comfyui"
    assert receipt["prompt_id"] == "comfy-test-123"
    if checkpoint:
        expected_requests.append(("POST", "/upload/image"))
        assert submitted_prompt["4"]["inputs"]["image"] == "uploaded_init.png"
    assert requests_seen == expected_requests + [
        ("POST", "/prompt"), ("GET", "/history/comfy-test-123"), ("GET", "/view"),
    ]
    assert submitted_prompt["1"]["inputs"]["ckpt_name"] == selected
    assert out_path.read_bytes() == png_out_bytes


@pytest.mark.parametrize("prompt_id", [None, "comfy-test"])
def test_m4_comfyui_rejects_prompt_node_errors_before_polling(tmp_path, monkeypatch, prompt_id):
    out_path = tmp_path / "image.png"
    receipt_path = tmp_path / "receipt.json"
    submitted = {"prompt_id": prompt_id, "node_errors": {"1": {"errors": ["模型不可用"]}}}
    requests_seen = []

    def open_comfyui(req, *, timeout):
        requests_seen.append(urllib.parse.urlparse(req.full_url).path)
        assert requests_seen == ["/prompt"]
        assert req.get_header("Ngrok-skip-browser-warning") == "1"
        assert req.get_header("Authorization") is None
        return io.BytesIO(json.dumps(submitted).encode())

    monkeypatch.setattr(safenet, "safe_urlopen", open_comfyui)
    code = engine_http_run._run_comfyui(
        {"workflow": {"1": {"class_type": "SaveImage"}}},
        "http://127.0.0.1:8188", out_path, receipt_path,
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert code == 1
    assert receipt["error"] == "comfyui_node_error"
    assert "模型不可用" in receipt["message"]
    assert requests_seen == ["/prompt"]
    assert not out_path.exists()


@pytest.mark.parametrize(("case", "error"), [
    ("at-limit", ""),
    ("over-limit", "comfyui_image_too_large"),
    ("html", "comfyui_view_failed"),
    ("empty", "empty_image"),
])
def test_m4_comfyui_view_has_size_and_signature_guards(tmp_path, monkeypatch, case, error):
    png = _write_test_png(tmp_path / "server.png").read_bytes()
    limit = len(png)
    bodies = {
        "at-limit": png,
        "over-limit": png + b"x" * 1000,
        "html": b"<html><body>" + b"x" * 64 + b"</body></html>",
        "empty": b"",
    }
    monkeypatch.setattr(imagetypes, "MAX_IMAGE_BYTES", limit)
    bytes_read = 0

    class ViewResponse(io.BytesIO):
        def read(self, size=-1):
            nonlocal bytes_read
            assert 0 < size <= min(65536, limit + 1 - bytes_read)
            chunk = super().read(size)
            bytes_read += len(chunk)
            return chunk

    def open_comfyui(req, *, timeout):
        path = urllib.parse.urlparse(req.full_url).path
        if path == "/prompt":
            payload = {"prompt_id": "task"}
        elif path == "/history/task":
            payload = {"task": {"outputs": {"1": {"images": [{"filename": "result.png"}]}}}}
        else:
            assert path == "/view"
            return ViewResponse(bodies[case])
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(safenet, "safe_urlopen", open_comfyui)
    out_path = tmp_path / "image.png"
    receipt_path = tmp_path / "receipt.json"
    code = engine_http_run._run_comfyui(
        {"workflow": {"1": {"class_type": "SaveImage"}}},
        "http://127.0.0.1:8188", out_path, receipt_path,
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["error"] == error
    assert bytes_read == min(len(bodies[case]), limit + 1)
    if error:
        assert code == 1
        assert not out_path.exists()
        assert not out_path.with_suffix(".png.part").exists()
    else:
        assert code == 0
        assert out_path.read_bytes() == png


@pytest.mark.parametrize(("configured", "secret"), [
    (True, "  upscale-token  "), (True, None), (True, " "), (False, None),
])
def test_m4_comfyui_upscale_headers(as_role, fresh_project, tmp_path, monkeypatch, configured, secret):
    c = as_role("admin")
    asset = c.post(f"/api/projects/{fresh_project()}/assets", json={
        "kind": "character", "name": "超分认证测试",
    })
    assert asset.status_code == 201, asset.text
    aid = asset.json()["id"]
    png = _write_test_png(tmp_path / "source.png").read_bytes()
    uploaded = c.post(f"/api/assets/{aid}/images", files={"file": ("source.png", png, "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    old_path = uploaded.json()["ref_images"][0]
    secret_env = "TEST_COMFYUI_UPSCALE_SECRET"
    if secret is None:
        monkeypatch.delenv(secret_env, raising=False)
    else:
        monkeypatch.setenv(secret_env, secret)
    monkeypatch.setattr(asset_upscale.engine_instances, "pick", lambda code: {
        "endpoint": "http://127.0.0.1:8188",
        "shared_secret_env": secret_env if configured else "",
    })
    requests_seen = []
    responses = {
        "/upload/image": b'{"name": "in.png"}',
        "/prompt": b'{"prompt_id": "task"}',
        "/history/task": b'{"task": {"outputs": {"4": {"images": [{"filename": "out.png"}]}}}}',
        "/view": png,
    }

    def open_comfyui(req, *, timeout):
        path = urllib.parse.urlparse(req.full_url).path
        requests_seen.append(path)
        assert req.get_header("Authorization") == ("Bearer upscale-token" if configured else None)
        assert req.get_header("Ngrok-skip-browser-warning") == "1"
        return io.BytesIO(responses[path])

    monkeypatch.setattr(safenet, "safe_urlopen", open_comfyui)
    result = c.post(f"/api/assets/{aid}/images/upscale", json={"path": old_path})
    if not configured or (secret and secret.strip()):
        assert result.status_code == 200, result.text
        assert requests_seen == list(responses)
    else:
        assert result.status_code == 422, result.text
        assert secret_env in result.json()["detail"]
        assert requests_seen == []
        assert (FILES_DIR / old_path).is_file()


def test_m4_lip_sync_endpoint_and_pipeline_node(as_role, people):
    c = as_role("admin")
    uid = people["admin"]["id"]
    pid = project_repo.create(f"对口型项目_{uuid.uuid4().hex[:6]}", "", [], uid)["id"]
    eid = episodes.create(pid, "对口型集")["id"]
    shot = shots.create(eid, "主角开口说话", "close up character speaking", 2.0, [])

    # 准备一条本地图片作业产出（供对口型无视频时兜底静态图）以及一条已就绪台词音频
    while worker.run_once("m4-pre-drain"):
        pass
    img_job = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": shot["id"],
        "capability": "image", "kind": "image", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
        "prompt_snapshot": "lip sync keyframe", "params": {}, "input_refs": [],
        "created_by": uid,
    })
    claimed = job_repo.claim_next("m4-ls-init", 60)
    assert claimed is not None and claimed["id"] == img_job["id"]
    _, out_dir = jobfiles.ensure_dirs(img_job["id"])
    png_file = _write_test_png(out_dir / f"{img_job['id']}-kf.png")
    img_log = out_dir.parent / "run.log"
    img_log.write_text("ok\n", encoding="utf-8")
    job_repo.succeed(img_job["id"], "m4-ls-init", str(img_log), [str(png_file)], 10)

    wav_rel = f"audio/{pid}/line_{uuid.uuid4().hex[:6]}.wav"
    _write_test_wav(FILES_DIR / wav_rel, seconds=0.6)
    clip = audio_repo.create(
        project_id=pid,
        episode_id=eid,
        shot_id=shot["id"],
        kind="dialogue",
        speaker="林小雨",
        text="这声音真清晰！",
        created_by=uid,
    )
    with get_conn() as conn:
        conn.execute(
            "UPDATE audio_clip SET status = 'ready', path = ? WHERE id = ?",
            (wav_rel, clip["id"]),
        )
        conn.commit()

    # 1) Mock 模式端到端通过 /api/audio-clips/{clip_id}/lip-sync
    res_mock = c.post(f"/api/audio-clips/{clip['id']}/lip-sync", json={
        "execution_mode": "local",
        "provider_code": "mock",
        "model_code": "mock-1",
    })
    assert res_mock.status_code == 202, res_mock.text
    mock_job_id = res_mock.json()["job_id"]
    assert res_mock.json()["source_kind"] == "image"
    worker.run_once("m4-ls-worker")
    mock_job = job_repo.get(mock_job_id)
    assert mock_job["status"] == "succeeded", mock_job["error_message"]

    # 2) 若环境有 FFmpeg，也真实合成对口型 MP4
    if _has_ffmpeg():
        res_ff = c.post(f"/api/audio-clips/{clip['id']}/lip-sync", json={})
        assert res_ff.status_code == 202, res_ff.text
        ff_job_id = res_ff.json()["job_id"]
        assert res_ff.json()["provider_code"] == "ffmpeg"
        worker.run_once("m4-ls-worker-ff")
        ff_job = job_repo.get(ff_job_id)
        assert ff_job["status"] == "succeeded", ff_job["error_message"]
        arts = json.loads(ff_job["result_paths_json"])
        assert arts
        art_dl = c.get(f"/api/jobs/{ff_job_id}/artifacts/0")
        assert art_dl.status_code == 200 and len(art_dl.content) > 100

    # 3) 验证内置模板 LIP_SYNC_EPISODE 与 lip_sync 节点 DAG 提交
    pipeline_templates.ensure_builtin_templates()
    tpls = pipeline_repo.list_templates(enabled_only=True)
    ls_tpl = next((t for t in tpls if t["name"] == "对口型漫剧生产"), None)
    assert ls_tpl is not None
    run_row = pipeline_repo.create_run(
        project_id=pid,
        episode_id=eid,
        template_id=ls_tpl["id"],
        template_version=int(ls_tpl["version"]),
        input_snapshot={},
        idempotency_key=f"m4-ls-{uuid.uuid4().hex[:8]}",
        created_by=uid,
        started_at="2026-11-16 01:00:00",
    )
    pipeline_repo.create_nodes(
        run_row["id"],
        [
            {
                "node_key": "shot_video",
                "node_type": "video_generate",
                "depends_on": [],
                "label": "镜头视频",
                "input": {"shot_id": shot["id"], "episode_id": eid},
            },
            {
                "node_key": "shot_lipsync",
                "node_type": "lip_sync",
                "depends_on": ["shot_video"],
                "label": "视频对口型",
                "input": {"shot_id": shot["id"], "episode_id": eid, "params": {"mock": True}},
            },
        ],
    )
    nodes = pipeline_repo.nodes_for_run(run_row["id"])
    by_key = {n["node_key"]: n for n in nodes}
    ls_node = by_key["shot_lipsync"]
    with get_conn() as conn:
        conn.execute(
            "UPDATE pipeline_node_run SET status = 'completed', job_id = ?, output_json = ? WHERE id = ?",
            (
                mock_job_id,
                json.dumps({"artifacts": [{"kind": "video", "path": str(png_file)}]}),
                by_key["shot_video"]["id"],
            ),
        )
        conn.commit()
    by_key = {n["node_key"]: n for n in pipeline_repo.nodes_for_run(run_row["id"])}
    pipeline_execute._submit_job_node(run_row, ls_node, by_key)
    ls_node_after = next(n for n in pipeline_repo.nodes_for_run(run_row["id"]) if n["id"] == ls_node["id"])
    assert ls_node_after["job_id"] is not None
    worker.run_once("m4-ls-dag")
    assert job_repo.get(ls_node_after["job_id"])["status"] == "succeeded"


def test_m4_sse_stream_and_safe_gc(as_role, people):
    c = as_role("admin")
    uid = people["admin"]["id"]
    pid = project_repo.create(f"GC项目_{uuid.uuid4().hex[:6]}", "", [], uid)["id"]
    eid = episodes.create(pid, "第1集")["id"]
    shot = shots.create(eid, "GC 测试镜头", "gc test", 3.0, [])

    def _create_done_job(status: str, filename: str) -> tuple[dict, Path, Path]:
        while worker.run_once("m4-gc-drain"):
            pass
        j = job_repo.create({
            "project_id": pid, "episode_id": eid, "shot_id": shot["id"],
            "capability": "image", "kind": "image", "execution_mode": "local",
            "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
            "prompt_snapshot": f"gc-{filename}", "params": {}, "input_refs": [],
            "created_by": uid,
        })
        claimed = job_repo.claim_next("m4-gc-worker", 60)
        assert claimed is not None and claimed["id"] == j["id"]
        job_dir, outputs = jobfiles.ensure_dirs(j["id"])
        log_path = job_dir / "run.log"
        log_path.write_text("log line kept\n", encoding="utf-8")
        art = outputs / filename
        art.write_bytes(b"X" * 2048)
        if status == "succeeded":
            job_repo.succeed(j["id"], "m4-gc-worker", str(log_path), [str(art)], 10)
        else:
            job_repo.fail(j["id"], "m4-gc-worker", "err", "failed for gc", str(log_path))
        return job_repo.get(j["id"]), art, log_path

    try:
        # 1) 失败废片（可回收）
        j_fail, fail_art, fail_log = _create_done_job("failed", "fail.png")
        # 2) 已签字通过的旧成功作业（受 review_item 保护）
        j_signed, signed_art, _ = _create_done_job("succeeded", "signed.png")
        reviews.upsert_sign(
            project_id=pid,
            target_kind="job",
            target_id=j_signed["id"],
            gate_id=None,
            base_version="v1",
            anchor="artifact",
            decision="approved",
            note="GC保护",
            user_id=uid,
        )
        # 3) 被后一条覆盖的未签字旧成功作业（可回收）
        j_old, old_art, old_log = _create_done_job("succeeded", "old.png")
        # 4) 最新成功作业（受最新成功保护）
        j_latest, latest_art, _ = _create_done_job("succeeded", "latest.png")

        # SSE 单帧快照测试
        sse = c.get(f"/api/events/stream?once=1&project_id={pid}")
        assert sse.status_code == 200
        assert "text/event-stream" in sse.headers.get("content-type", "")
        assert "event: snapshot" in sse.text
        data_line = next(line[5:].strip() for line in sse.text.splitlines() if line.startswith("data:"))
        snap = json.loads(data_line)
        assert "jobs" in snap and "pipeline_runs" in snap

        # GC 预览：fail 与 old 应在可清理清单，signed 与 latest 绝不在清单
        prev = c.get(f"/api/system/gc-preview?project_id={pid}")
        assert prev.status_code == 200, prev.text
        prev_body = prev.json()
        cand_ids = {it["job_id"] for it in prev_body["items"]}
        assert j_fail["id"] in cand_ids
        assert j_old["id"] in cand_ids
        assert j_signed["id"] not in cand_ids
        assert j_latest["id"] not in cand_ids
        assert prev_body["protected_count"] >= 2

        # GC 执行：只删 outputs 文件，run.log 与数据库记录完整保留，受保护产物不动
        exe = c.post("/api/system/gc-execute", json={
            "project_id": pid,
            "job_ids": [j_fail["id"], j_old["id"], j_signed["id"], j_latest["id"]],
        })
        assert exe.status_code == 200, exe.text
        ex = exe.json()
        assert ex["deleted_jobs"] == 2
        assert ex["reclaimed_bytes"] >= 4096
        assert j_signed["id"] in ex["skipped_job_ids"]
        assert j_latest["id"] in ex["skipped_job_ids"]

        assert not fail_art.exists()
        assert not old_art.exists()
        assert fail_log.is_file()
        assert old_log.is_file()
        assert signed_art.is_file()
        assert latest_art.is_file()
        assert job_repo.get(j_fail["id"]) is not None
        assert job_repo.get(j_old["id"]) is not None
    finally:
        for d in (FILES_DIR / f"audio/{pid}",):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
