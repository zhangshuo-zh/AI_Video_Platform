"""外部创作桥接测试：NotebookLM Pro 设定包导出 & Lyria 外部音频回导。"""
from __future__ import annotations

import pytest

import config
from repositories.audio import clips
from services import knowledge as knowledge_service
from workspace import path_of


def _tiny_wav() -> bytes:
    # 最小合法 PCM WAV 文件头（44 字节）+ 4 字节静音样本
    return (
        b"RIFF" + (40).to_bytes(4, "little") + b"WAVE"
        + b"fmt " + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
        + (16000).to_bytes(4, "little") + (32000).to_bytes(4, "little")
        + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
        + b"data" + (4).to_bytes(4, "little") + b"\x00\x00\x00\x00"
    )


def _setup_project(c) -> int:
    r = c.post("/api/projects", json={"name": "星穹动漫项目", "description": "赛博仙侠番剧"})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


def test_notebooklm_bundle_aggregates_project_bible(as_role) -> None:
    admin_client = as_role("admin")
    pid = _setup_project(admin_client)
    # 画风词缀
    r = admin_client.post(f"/api/projects/{pid}/knowledge/affixes", json={
        "name": "赛璐璐透亮光影", "category": "lighting",
        "positive": "cel shading, crisp lineart", "negative": "blurry", "is_default": True,
    })
    assert r.status_code == 201
    # 角色资产 + 人物档案
    asset = admin_client.post(f"/api/projects/{pid}/assets", json={
        "kind": "character", "name": "林霜", "summary": "白发异瞳剑修少女",
        "consistency_prompt": "silver long hair, heterochromia eyes, cyan robe",
        "aliases": ["霜儿"], "tags": ["主角"],
    }).json()
    aid = int(asset["id"])
    r = admin_client.put(f"/api/assets/{aid}/character-profile", json={
        "biography": "自幼在星陨峰修剑", "personality": "外冷内热",
        "growth_arc": "从独行到信任伙伴", "text_prompt": "说话简短克制",
        "tts_voice": "zh-CN-XiaoxiaoNeural", "tts_rate": 1.0, "tts_mood": "清冷",
    })
    assert r.status_code == 200
    # 分集与分镜
    ep = admin_client.post(f"/api/projects/{pid}/episodes", json={"title": "初入星门"}).json()
    r = admin_client.post(f"/api/episodes/{ep['id']}/shots", json={
        "summary": "林霜拔剑破开迷雾", "prompt": "silver hair girl drawing sword in mist",
        "duration": 4.0, "asset_ids": [aid],
    })
    assert r.status_code == 201

    resp = admin_client.get(f"/api/projects/{pid}/knowledge/notebooklm-bundle")
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers.get("content-type", "")
    assert f"project-{pid}-notebooklm.md" in resp.headers.get("content-disposition", "")
    body = resp.text
    assert "星穹动漫项目" in body
    assert "赛璐璐透亮光影" in body
    assert "林霜" in body
    assert "silver long hair, heterochromia eyes" in body
    assert "自幼在星陨峰修剑" in body
    assert "初入星门" in body
    assert "林霜拔剑破开迷雾" in body


def _create_document(c, project_id: int, title: str, content: str) -> str:
    response = c.post(f"/api/projects/{project_id}/documents", json={
        "kind": "novel", "title": title, "content": content,
    })
    assert response.status_code == 201, response.text
    return response.json()["rel_path"]


def test_notebooklm_bundle_caps_each_document(as_role, monkeypatch) -> None:
    c = as_role("admin")
    pid = _setup_project(c)
    monkeypatch.setattr(knowledge_service, "_NOTEBOOKLM_DOC_MAX_CHARS", 120)
    at_limit = "甲" * 120
    over_limit = "乙" * 120
    _create_document(c, pid, "01 设定", at_limit)
    _create_document(c, pid, "02 剧本", over_limit + "不应导出的尾部")

    response = c.get(f"/api/projects/{pid}/knowledge/notebooklm-bundle")
    assert response.status_code == 200
    assert at_limit in response.text
    assert over_limit in response.text
    assert "不应导出的尾部" not in response.text
    assert response.text.count("单文档正文超过 120 字符，已截断") == 1
    assert "知识包达到" not in response.text


def test_notebooklm_bundle_caps_total_and_stops_reading_documents(as_role, monkeypatch) -> None:
    c = as_role("admin")
    pid = _setup_project(c)
    base_len = len(c.get(f"/api/projects/{pid}/knowledge/notebooklm-bundle").text)
    first = _create_document(c, pid, "01 剧本", "一" * 180)
    _create_document(c, pid, "02 剧本", "二" * 180)
    _create_document(c, pid, "03 后续剧本", "三" * 180)
    cap = base_len + 120
    monkeypatch.setattr(knowledge_service, "_NOTEBOOKLM_BUNDLE_MAX_CHARS", cap)
    paths_read = []

    def track_path(rel_path):
        paths_read.append(rel_path)
        return path_of(rel_path)

    monkeypatch.setattr(knowledge_service, "path_of", track_path)
    response = c.get(f"/api/projects/{pid}/knowledge/notebooklm-bundle")
    assert response.status_code == 200
    assert len(response.text) <= cap
    assert "01 剧本" in response.text
    assert response.text.endswith(f"> 知识包达到 {cap} 字符上限，后续内容已截断。\n")
    assert "03 后续剧本" not in response.text
    assert paths_read == [first]


def test_audio_upload_lyria_and_cleanup(as_role) -> None:
    admin_client = as_role("admin")
    pid = _setup_project(admin_client)
    ep = admin_client.post(f"/api/projects/{pid}/episodes", json={"title": "第一集"}).json()
    clip = admin_client.post(f"/api/projects/{pid}/audio-clips", json={
        "episode_id": ep["id"], "kind": "music", "speaker": "Lyria", "text": "燃向古风电子战斗 BGM",
    }).json()
    cid = int(clip["id"])

    # 伪装文本应被魔数校验拒绝
    bad = admin_client.post(
        f"/api/audio-clips/{cid}/upload",
        files={"file": ("fake.mp3", b"not real audio data", "audio/mpeg")},
    )
    assert bad.status_code == 422

    # 合法 WAV 上传成功并标记 ready
    ok = admin_client.post(
        f"/api/audio-clips/{cid}/upload",
        files={"file": ("battle_bgm.wav", _tiny_wav(), "audio/wav")},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "ready"
    assert body["path"].startswith(f"audio/{pid}/")
    disk_file = config.FILES_DIR / body["path"]
    assert disk_file.is_file()

    # 已就绪素材不可重复覆盖上传
    dup = admin_client.post(
        f"/api/audio-clips/{cid}/upload",
        files={"file": ("again.wav", _tiny_wav(), "audio/wav")},
    )
    assert dup.status_code == 409

    # 删除素材后自动清理外部上传磁盘文件
    deleted = admin_client.delete(f"/api/audio-clips/{cid}")
    assert deleted.status_code == 204
    assert not disk_file.exists()


@pytest.mark.parametrize("case", ["replace", "outside-audio", "outside-files", "symlink", "write-fails"])
def test_audio_upload_replaces_failed_clip_safely(as_role, monkeypatch, case) -> None:
    c = as_role("admin")
    pid = _setup_project(c)
    created = c.post(f"/api/projects/{pid}/audio-clips", json={"kind": "music", "text": "待重传"})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    folder = config.FILES_DIR / "audio" / str(pid)
    folder.mkdir(parents=True)
    if case == "outside-files":
        old_path = f"audio/../../outside-{cid}.wav"
    elif case == "outside-audio":
        old_path = f"jobs/old-{cid}.wav"
    else:
        old_path = f"audio/{pid}/old.wav"
    old_file = (config.FILES_DIR / old_path).resolve()
    if case == "symlink":
        symlink = old_file
        old_file = config.FILES_DIR / f"protected-{cid}.wav"
        symlink.symlink_to(old_file)
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_bytes(_tiny_wav())
    clips.finish(cid, status="failed", path=old_path, duration_ms=None)
    url = f"/api/audio-clips/{cid}/upload"
    files = {"file": ("new.wav", _tiny_wav(), "audio/wav")}

    if case == "write-fails":
        def fail_finish(*args, **kwargs):
            raise OSError("database write failed")

        monkeypatch.setattr(clips, "finish", fail_finish)
        with pytest.raises(OSError, match="database write failed"):
            c.post(url, files=files)
        assert [p.resolve() for p in folder.iterdir()] == [old_file]
        assert clips.get(cid)["path"] == old_path
        return

    response = c.post(url, files=files)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["path"] != old_path
    assert (config.FILES_DIR / body["path"]).read_bytes() == _tiny_wav()
    assert old_file.exists() == (case != "replace")
