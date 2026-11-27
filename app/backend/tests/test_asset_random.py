"""Tests for random character asset drafting endpoint."""
from __future__ import annotations

import json


def test_random_character_auth_guards(client, as_role, seeded):
    pid = seeded["project_id"]
    r = client.post(f"/api/projects/{pid}/assets/random-character", json={})
    assert r.status_code == 401

    with as_role("outsider") as c:
        r2 = c.post(f"/api/projects/{pid}/assets/random-character", json={})
        assert r2.status_code == 404


def test_random_character_returns_structured_profile(as_role, seeded, monkeypatch):
    from services import asset_random

    pid = seeded["project_id"]
    sample = {
        "name": "星野澪",
        "summary": "表面冷淡的星舰机械师，其实暗地里在修补一台会讲笑话的老式猫形无人机。",
        "tags": ["科幻", "机械师", "异瞳", "外冷内热"],
        "aliases": ["澪姐", "猫耳技师"],
        "consistency_prompt": "日系二次元厚涂画风，银白齐肩短发，左金右蓝异色瞳，深蓝高领工装夹克配金色拉链，机械猫耳发箍，全身三视图比例一致。",
        "image_prompt_en": "anime style, cel shading, silver bob hair, heterochromia gold left eye blue right eye, navy high-collar jacket with gold zipper, mechanical cat ear headband",
    }
    monkeypatch.setattr(
        asset_random, "_run_ollama",
        lambda instruction, model, deadline: (json.dumps(sample, ensure_ascii=False), "qwen3:8b"),
    )
    with as_role("editor") as c:
        r = c.post(f"/api/projects/{pid}/assets/random-character", json={"theme": "星舰机械师"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "星野澪"
        assert body["provider"] == "ollama"
        assert body["model"] == "qwen3:8b"
        assert body["tags"] == ["科幻", "机械师", "异瞳", "外冷内热"]
        assert body["aliases"] == ["澪姐", "猫耳技师"]
        assert "三视图" in body["consistency_prompt"]
        assert "silver bob hair" in body["image_prompt_en"]


def test_random_character_rejects_empty_required_fields(as_role, seeded, monkeypatch):
    from services import asset_random

    pid = seeded["project_id"]
    monkeypatch.setattr(
        asset_random, "_run_ollama",
        lambda instruction, model, deadline: ('{"name": "", "summary": "x"}', "qwen3:8b"),
    )
    with as_role("editor") as c:
        r = c.post(
            f"/api/projects/{pid}/assets/random-character",
            json={"execution_mode": "local", "provider_code": "ollama"},
        )
        assert r.status_code == 502


def test_random_character_timeout_message_is_contextual(as_role, seeded, monkeypatch):
    from services import asset_random
    from services.asset_vision_transport import _VisionError

    pid = seeded["project_id"]

    def _slow_ollama(instruction, model, deadline):
        raise _VisionError(504, "Ollama推理超时，请减少参考图或更换通道")

    monkeypatch.setattr(asset_random, "_run_ollama", _slow_ollama)
    with as_role("editor") as c:
        r = c.post(
            f"/api/projects/{pid}/assets/random-character",
            json={"execution_mode": "local", "provider_code": "ollama"},
        )
        assert r.status_code == 504
        detail = r.json()["detail"]
        assert "生成角色人设超时" in detail
        assert "参考图" not in detail


def test_random_character_auto_starts_local_ollama_for_loopback_manager(as_role, seeded, monkeypatch):
    import engine_launch
    from services import asset_random
    from services.asset_vision_transport import _VisionError

    pid = seeded["project_id"]
    sample = {
        "name": "白露",
        "summary": "在蒸汽工坊里给机械鸟调音的少女学徒。",
        "tags": ["蒸汽朋克", "学徒", "工坊"],
        "aliases": ["小露"],
        "consistency_prompt": "精致赛璐璐画风，浅茶色双麻花辫，琥珀色大眼睛，米白泡泡袖衬衫配棕色皮质背带围裙，全身比例一致。",
    }
    state = {"running": False, "launched": 0}

    monkeypatch.setattr(engine_launch, "LOOPBACK", frozenset({"testclient", "127.0.0.1"}))
    monkeypatch.setattr(
        asset_random.engine_instances, "instances",
        lambda: [{"engine_code": "ollama", "enabled": 1, "endpoint": "http://127.0.0.1:11434"}],
    )

    def _fake_launch(code, endpoint):
        assert code == "ollama"
        assert endpoint == "http://127.0.0.1:11434"
        state["launched"] += 1
        state["running"] = True
        return engine_launch.LaunchResult(True, "ollama", True, True, "已启动 ollama serve")

    def _fake_request(url, **kwargs):
        if url.endswith("/api/tags"):
            if not state["running"]:
                raise _VisionError(503, "Ollama无法连接", unavailable=True)
            return {"models": [{"name": "qwen3:8b"}]}
        if url.endswith("/api/chat"):
            return {"message": {"content": json.dumps(sample, ensure_ascii=False)}}
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(engine_launch, "launch", _fake_launch)
    monkeypatch.setattr(asset_random, "_request", _fake_request)

    with as_role("admin") as c:
        r = c.post(
            f"/api/projects/{pid}/assets/random-character",
            json={"execution_mode": "local", "provider_code": "ollama", "model_code": "qwen3:8b"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "白露"
        assert body["ollama_auto_started"] is True
        assert state["launched"] == 1


def test_random_character_does_not_auto_start_ollama_for_non_manager(as_role, seeded, monkeypatch):
    import engine_launch
    from services import asset_random
    from services.asset_vision_transport import _VisionError

    pid = seeded["project_id"]
    launched = []

    monkeypatch.setattr(engine_launch, "LOOPBACK", frozenset({"testclient", "127.0.0.1"}))
    monkeypatch.setattr(
        asset_random.engine_instances, "instances",
        lambda: [{"engine_code": "ollama", "enabled": 1, "endpoint": "http://127.0.0.1:11434"}],
    )
    monkeypatch.setattr(engine_launch, "launch", lambda code, ep: launched.append((code, ep)))
    monkeypatch.setattr(
        asset_random, "_request",
        lambda url, **kwargs: (_ for _ in ()).throw(_VisionError(503, "Ollama无法连接", unavailable=True)),
    )

    with as_role("editor") as c:
        r = c.post(
            f"/api/projects/{pid}/assets/random-character",
            json={"execution_mode": "local", "provider_code": "ollama"},
        )
        assert r.status_code == 503
        assert launched == []


def test_random_character_retries_once_on_malformed_json(as_role, seeded, monkeypatch):
    from services import asset_random

    pid = seeded["project_id"]
    sample = {
        "name": "青岚",
        "summary": "云端修真学院的剑修助教。",
        "tags": ["仙侠", "剑修"],
        "aliases": ["岚师兄"],
        "consistency_prompt": "新中式仙侠水墨画风，墨色高马尾，青白交领剑袍，腰佩玉剑穗，全身比例一致。",
    }
    calls = {"n": 0}

    def _flaky_deepseek(instruction, model, deadline):
        calls["n"] += 1
        if calls["n"] == 1:
            return ('{"name": "青岚", "consistency_prompt": "截断了', "deepseek-chat")
        return (json.dumps(sample, ensure_ascii=False), "deepseek-chat")

    monkeypatch.setattr(asset_random, "_run_deepseek", _flaky_deepseek)
    with as_role("editor") as c:
        r = c.post(
            f"/api/projects/{pid}/assets/random-character",
            json={"execution_mode": "cloud", "provider_code": "deepseek", "model_code": "deepseek-chat"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "青岚"
        assert calls["n"] == 2


def test_显式指定ollama模型时跨实例精确查找不回退错模(monkeypatch):
    """P1-2 回归：多 Ollama 实例时，显式指定模型应跨实例精确查找，找不到时报 422 而不回退到第一个实例的默认模型。"""
    import time
    import pytest
    from services import asset_random
    from services.asset_vision_transport import _VisionError

    instances = [
        {"engine_code": "ollama", "enabled": True, "endpoint": "http://127.0.0.1:11434"},
        {"engine_code": "ollama", "enabled": True, "endpoint": "http://127.0.0.1:11435"},
    ]
    monkeypatch.setattr(asset_random.engine_instances, "instances", lambda: instances)

    def _fake_tags(url, **_kwargs):
        if "11434" in url:
            return {"models": [{"name": "llama3:latest"}]}
        return {"models": [{"name": "qwen2.5:14b"}]}

    monkeypatch.setattr(asset_random, "_request", _fake_tags)
    ep, model = asset_random._ollama_endpoint_and_model("qwen2.5:14b", time.monotonic() + 10)
    assert ep == "http://127.0.0.1:11435" and model == "qwen2.5:14b"

    with pytest.raises(_VisionError) as ei:
        asset_random._ollama_endpoint_and_model("nonexistent:7b", time.monotonic() + 10)
    assert ei.value.status_code == 422

