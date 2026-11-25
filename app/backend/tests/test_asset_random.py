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
