"""资产参考图反推一致性提示词（Step 2）测试。"""
from __future__ import annotations

import base64
import io
import json
import time
import uuid

from email.message import Message
from urllib.error import HTTPError

import pytest
from PIL import Image

from config import FILES_DIR
from db import transaction
from services import asset_vision as av
from services import asset_vision_transport as transport


def _create_asset_with_images(client, project_id: int) -> int:
    asset = client.post(
        f"/api/projects/{project_id}/assets",
        json={"kind": "character", "name": f"阿雨-{uuid.uuid4().hex[:6]}", "summary": "测试角色"},
    ).json()
    aid = asset["id"]
    rel_dir = FILES_DIR / str(aid)
    rel_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("cover.png", "three.png"):
        Image.new("RGB", (48, 32), "blue").save(rel_dir / fname)
    with transaction() as conn:
        conn.execute(
            "UPDATE asset SET ref_images = ?, image_purposes = ? WHERE id = ?",
            (
                json.dumps([f"{aid}/cover.png", f"{aid}/three.png"]),
                json.dumps({f"{aid}/three.png": "三视图"}),
                aid,
            ),
        )
    return aid


def test_reverse_prompt_requires_images(as_role, fresh_project):
    client = as_role("admin")
    pid = fresh_project()
    asset = client.post(
        f"/api/projects/{pid}/assets",
        json={"kind": "character", "name": f"无图角色-{uuid.uuid4().hex[:6]}"},
    ).json()
    resp = client.post(f"/api/assets/{asset['id']}/reverse-prompt", json={})
    assert resp.status_code == 400
    assert "参考图" in resp.json()["detail"]


def test_reverse_prompt_rejects_foreign_image_path(as_role, fresh_project):
    client = as_role("admin")
    pid = fresh_project()
    aid = _create_asset_with_images(client, pid)
    resp = client.post(
        f"/api/assets/{aid}/reverse-prompt",
        json={"image_paths": ["../secret.png"]},
    )
    assert resp.status_code == 400
    assert "不属于当前资产" in resp.json()["detail"]


def test_reverse_prompt_prefers_ollama_vision_and_selects_three_view(as_role, fresh_project, monkeypatch):
    client = as_role("admin")
    pid = fresh_project()
    aid = _create_asset_with_images(client, pid)

    monkeypatch.setattr(
        av.engine_instances,
        "instances",
        lambda: [{"engine_code": "ollama", "endpoint": "http://127.0.0.1:11434", "enabled": 1}],
    )
    captured = {}

    def fake_request(url, *, body=None, **_options):
        if url.endswith("/api/tags"):
            return {"models": [{"name": "qwen2.5vl:7b"}, {"name": "qwen3:8b"}]}
        captured["url"] = url
        captured["body"] = body
        return {"message": {"content": "```text\n黑发少女, 蓝瞳, 雨夜风衣\n```"}}

    monkeypatch.setattr(av, "_request", fake_request)
    resp = client.post(f"/api/assets/{aid}/reverse-prompt", json={"language": "zh"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "ollama"
    assert data["model"] == "qwen2.5vl:7b"
    assert data["prompt"] == "黑发少女, 蓝瞳, 雨夜风衣"
    assert data["used_images"] == [f"{aid}/cover.png", f"{aid}/three.png"]
    assert captured["body"]["model"] == "qwen2.5vl:7b"
    message = captured["body"]["messages"][0]
    assert captured["body"]["format"] == "json"
    assert len(message["images"]) == 2
    assert "图1：封面定妆" in message["content"]
    assert "图2：三视图" in message["content"]
    assert "测试角色" in message["content"]
    with transaction() as conn:
        assert not conn.execute("SELECT consistency_prompt FROM asset WHERE id = ?", (aid,)).fetchone()[0]


def test_reverse_prompt_falls_back_to_chatgpt_when_no_ollama(as_role, fresh_project, monkeypatch):
    client = as_role("admin")
    pid = fresh_project()
    aid = _create_asset_with_images(client, pid)

    monkeypatch.setattr(av.engine_instances, "instances", lambda: [])
    monkeypatch.setattr(av.chatgpt_plan, "access_token", lambda uid, **kw: "tok-fake")
    stream = _sse(
        {"type": "response.output_text.delta", "delta": "anime girl, "},
        {"type": "response.output_text.delta", "delta": "navy raincoat"},
        {"type": "response.output_text.done", "text": "anime girl, navy raincoat"},
        {"type": "response.completed"},
    )

    def open_response(req, *, timeout):
        assert req.full_url.endswith("/responses")
        assert req.get_header("Authorization") == "Bearer tok-fake"
        assert json.loads(req.data)["input"][0]["content"][1]["type"] == "input_image"
        return stream

    monkeypatch.setattr(transport.safenet, "safe_urlopen", open_response)
    resp = client.post(f"/api/assets/{aid}/reverse-prompt", json={"language": "en"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "chatgpt"
    assert data["model"] == "gpt-5.1"
    assert data["prompt"] == "anime girl, navy raincoat"


def test_reverse_prompt_returns_503_when_no_provider_available(as_role, fresh_project, monkeypatch):
    client = as_role("admin")
    pid = fresh_project()
    aid = _create_asset_with_images(client, pid)

    monkeypatch.setattr(av.engine_instances, "instances", lambda: [])

    def _no_token(_uid, **_options):
        raise av.chatgpt_plan.ChatGPTError("未连接")

    monkeypatch.setattr(av.chatgpt_plan, "access_token", _no_token)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    resp = client.post(f"/api/assets/{aid}/reverse-prompt", json={})
    assert resp.status_code == 503
    assert "看图模型" in resp.json()["detail"]


class _Response(io.BytesIO):
    def __init__(self, body: bytes, content_type: str = "application/json"):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type


def _sse(*events: dict) -> _Response:
    data = b"".join(b"data: " + json.dumps(event).encode() + b"\r\n\r\n" for event in events)
    return _Response(b": heartbeat\r\n\r\n" + data, "text/event-stream; charset=utf-8")


def test_bilingual_draft_removes_guide_tags_and_keeps_pairs_under_budget():
    zh = [f"角色纹饰{i}" + "银" * 55 for i in range(20)]
    en = [f"silver filigree {i}" + " refined texture" * 5 for i in range(20)]
    prompt = av._finalize(json.dumps({
        "zh": ["色卡条", "蓝瞳", "蓝瞳", *zh],
        "en": ["FRONT / SIDE / BACK", "blue eyes", "blue eyes", *en],
    }), "bilingual")
    assert av._finalize("FRONT / SIDE / BACK, navy raincoat", "en") == "navy raincoat"
    chinese, english = (line.split(", ") for line in prompt.splitlines())
    assert len(prompt) <= 1000
    assert chinese[0] == "蓝瞳" and english[0] == "blue eyes"
    assert chinese[1:] == zh[:len(chinese) - 1]
    assert english[1:] == en[:len(english) - 1]
    assert 2 < len(english) == len(chinese) < len(zh)


def test_reference_image_applies_orientation_and_caps_resolution():
    src = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (2800, 1400), "blue").save(src, "JPEG", exif=exif)
    from services.asset_vision_images import _encode

    for side in (1536, 2048):
        data = base64.b64decode(_encode(src.getvalue(), side))
        assert len(data) <= 768 * 1024
        with Image.open(io.BytesIO(data)) as img:
            assert img.size == (side // 2, side)
            assert img.getexif().get(274) is None


@pytest.mark.parametrize(("corrupt_ref", "status"), [(True, 400), (False, 422)])
def test_invalid_reference_fails_before_network(as_role, fresh_project, monkeypatch, corrupt_ref, status):
    client = as_role("admin")
    aid = _create_asset_with_images(client, fresh_project())
    rel = f"{aid}/cover.png"
    if corrupt_ref:
        rel = "../outside.png"
        with transaction() as conn:
            conn.execute("UPDATE asset SET ref_images = ? WHERE id = ?", (json.dumps([rel]), aid))
    else:
        (FILES_DIR / rel).write_bytes(b"invalid image")

    def deny_network(*args, **kwargs):
        pytest.fail("invalid sources must fail before provider selection")

    monkeypatch.setattr(av, "_request", deny_network)
    response = client.post(f"/api/assets/{aid}/reverse-prompt", json={"image_paths": [rel]})
    assert response.status_code == status


@pytest.mark.parametrize(("events", "status"), [
    ([{"type": "response.output_text.delta", "delta": "partial response"}], 502),
    ([{"type": "response.refusal.done", "refusal": "private detail"}], 422),
    ([{"type": "response.incomplete", "response": {"error": "private detail"}}], 502),
])
def test_responses_rejects_unfinished_and_refused_streams(events, status):
    with pytest.raises(transport._VisionError) as error:
        transport._read_responses(_sse(*events), time.monotonic() + 5)
    assert error.value.status_code == status
    assert "private detail" not in str(error.value)


def test_responses_reads_completed_message_without_duplicate_chunks():
    stream = _sse(
        {"type": "response.output_text.delta", "delta": "unfinalized tag"},
        {"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "navy raincoat"}]},
        ]}},
    )
    assert transport._read_responses(stream, time.monotonic() + 5) == "navy raincoat"


def test_auto_does_not_retry_or_switch_cloud_after_submission(as_role, fresh_project, monkeypatch, caplog):
    client = as_role("admin")
    aid = _create_asset_with_images(client, fresh_project())
    monkeypatch.setattr(av.engine_instances, "instances", lambda: [])
    secret = "secret-from-provider"
    monkeypatch.setattr(av.chatgpt_plan, "access_token", lambda uid, **kw: secret)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-secret")
    sent = []

    def open_response(req, *, timeout):
        sent.append(req.full_url)
        assert req.get_method() == "POST"
        raise HTTPError(req.full_url, 429, secret, None, io.BytesIO(secret.encode()))

    monkeypatch.setattr(transport.safenet, "safe_urlopen", open_response)
    resp = client.post(f"/api/assets/{aid}/reverse-prompt", json={})
    assert resp.status_code == 429
    assert "额度" in resp.json()["detail"]
    assert secret not in resp.text and secret not in caplog.text
    assert sent == [f"{av.chatgpt_plan.RESOURCE}/responses"]


def test_auto_uses_dashscope_when_chatgpt_authorization_is_unavailable(as_role, fresh_project, monkeypatch):
    client = as_role("admin")
    aid = _create_asset_with_images(client, fresh_project())
    monkeypatch.setattr(av.engine_instances, "instances", lambda: [])
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-secret")

    def no_token(_uid, **kw):
        raise av.chatgpt_plan.ChatGPTError("expired")

    def open_response(req, *, timeout):
        assert req.full_url == av._DASHSCOPE_URL
        parts = json.loads(req.data)["messages"][0]["content"]
        assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        result = {"choices": [{"message": {"content": [
            {"type": "text", "text": '{"zh":["黑发","蓝瞳"]}'},
        ]}}]}
        return _Response(json.dumps(result).encode())

    monkeypatch.setattr(av.chatgpt_plan, "access_token", no_token)
    monkeypatch.setattr(transport.safenet, "safe_urlopen", open_response)
    resp = client.post(f"/api/assets/{aid}/reverse-prompt", json={})
    assert resp.status_code == 200
    assert resp.json()["provider"] == "dashscope"
    assert resp.json()["prompt"] == "黑发, 蓝瞳"


def test_ollama_disables_thinking_in_json_mode(as_role, fresh_project, monkeypatch):
    client = as_role("admin")
    aid = _create_asset_with_images(client, fresh_project())
    monkeypatch.setattr(
        av.engine_instances,
        "instances",
        lambda: [{"engine_code": "ollama", "endpoint": "http://127.0.0.1:11434", "enabled": 1}],
    )
    captured = {}

    def open_response(req, *, timeout):
        if req.full_url.endswith("/api/tags"):
            return _Response(json.dumps({"models": [{"name": "qwen3-vl:8b"}]}).encode())
        captured["body"] = json.loads(req.data)
        return _Response(json.dumps({
            "done": True,
            "message": {"content": '{"zh":["银发双马尾","赛博猫耳"]}'},
        }).encode())

    monkeypatch.setattr(transport.safenet, "safe_urlopen", open_response)
    resp = client.post(f"/api/assets/{aid}/reverse-prompt", json={"provider": "ollama", "language": "zh"})
    assert resp.status_code == 200
    assert captured["body"]["format"] == "json"
    assert captured["body"]["think"] is False
    assert resp.json()["prompt"] == "银发双马尾, 赛博猫耳"


def test_bilingual_allows_unequal_tag_counts_and_respects_budget():
    prompt = av._finalize(json.dumps({
        "zh": ["色卡条", "银白短发", "发光机械猫耳"],
        "en": ["FRONT / SIDE / BACK", "silver short hair", "glowing cyber cat ears", "blue neon accents", "blue neon accents"],
    }), "bilingual")
    zh_line, en_line = prompt.splitlines()
    assert zh_line == "银白短发, 发光机械猫耳"
    assert en_line == "silver short hair, glowing cyber cat ears, blue neon accents"
    assert len(prompt) <= av._MAX_PROMPT


def test_chatgpt_picks_available_vision_model_from_account_list(as_role, fresh_project, monkeypatch):
    client = as_role("admin")
    aid = _create_asset_with_images(client, fresh_project())
    monkeypatch.setattr(av.engine_instances, "instances", lambda: [])
    monkeypatch.setattr(av.chatgpt_plan, "access_token", lambda uid, **kw: "tok-live")
    monkeypatch.setattr(av.chatgpt_plan, "models", lambda uid: [
        {"id": "o3-mini", "name": "o3-mini"},
        {"id": "gpt-4o", "name": "GPT-4o"},
    ])
    captured = {}

    def open_response(req, *, timeout):
        captured["body"] = json.loads(req.data)
        return _sse(
            {"type": "response.output_text.delta", "delta": '{"en":["cyber catgirl","amber eyes"]}'},
            {"type": "response.completed"},
        )

    monkeypatch.setattr(transport.safenet, "safe_urlopen", open_response)
    resp = client.post(f"/api/assets/{aid}/reverse-prompt", json={"provider": "chatgpt", "language": "en"})
    assert resp.status_code == 200
    assert captured["body"]["model"] == "gpt-4o"
    assert resp.json()["model"] == "gpt-4o"
    assert resp.json()["prompt"] == "cyber catgirl, amber eyes"


def test_ollama_negative_cache_skips_dead_endpoint_in_auto_but_probes_explicit(as_role, fresh_project, monkeypatch):
    client = as_role("admin")
    aid = _create_asset_with_images(client, fresh_project())
    av._ollama_down.clear()
    monkeypatch.setattr(
        av.engine_instances,
        "instances",
        lambda: [{"engine_code": "ollama", "endpoint": "http://127.0.0.1:11434", "enabled": 1}],
    )
    monkeypatch.setattr(av.chatgpt_plan, "access_token", lambda uid, **kw: "tok-live")
    monkeypatch.setattr(av.chatgpt_plan, "models", lambda uid: [{"id": "gpt-5.1", "name": "GPT-5.1"}])
    tag_hits = 0

    def open_response(req, *, timeout):
        nonlocal tag_hits
        if req.full_url.endswith("/api/tags"):
            tag_hits += 1
            raise transport.URLError("connection refused")
        return _sse(
            {"type": "response.output_text.delta", "delta": '{"zh":["黑发少女"]}'},
            {"type": "response.completed"},
        )

    monkeypatch.setattr(transport.safenet, "safe_urlopen", open_response)
    r1 = client.post(f"/api/assets/{aid}/reverse-prompt", json={"provider": "auto", "language": "zh"})
    r2 = client.post(f"/api/assets/{aid}/reverse-prompt", json={"provider": "auto", "language": "zh"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert tag_hits == 1
    r3 = client.post(f"/api/assets/{aid}/reverse-prompt", json={"provider": "ollama", "language": "zh"})
    assert r3.status_code == 503
    assert tag_hits == 2
    av._ollama_down.clear()


def test_scene_slots_order_matches_frontend():
    assert av._SLOTS_BY_KIND["scene"] == ("全景定调", "机位结构", "光影时段", "材质细节")
