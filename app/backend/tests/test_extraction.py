"""W13：文本抽取候选。

三层：纯文本解析（证据必须逐字）、Job 闭环（假 HTTP，真子进程）、
人工确认（入库／合并／驳回、版本失效、去重与权限）。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import extraction
import services
import worker
from db import get_conn
from repositories.content import assets
from repositories.documents import documents as docs
from repositories.extraction import runs as extraction_runs
from repositories.jobs import jobs as job_repo

SECRET = "sk-w13-test-secret"
CONTENT = ("掌灯人站在灯市口，手里提着一盏没点的灯。\n"
           "灯市从河岸一直排到城门，夜里没有风。\n"
           "他把灯举过头顶，说：你迟了三年。\n")

REPLY = {
    "characters": [{
        "name": "掌灯人", "aliases": ["提灯的老者"], "role": "配角",
        "appearance": "身形瘦高，常提一盏没点的灯", "personality": "寡言",
        "suggested_prompt": "瘦高老者，平涂赛璐璐，暖灰主色",
        "confidence": 0.86,
        "evidence": {"quote": "掌灯人站在灯市口"},
    }],
    "scenes": [{
        "name": "灯市", "time_of_day": "夜", "weather": "无风",
        "description": "河岸到城门的长街，两侧挂满纸灯",
        "suggested_prompt": "夜晚长街，密集纸灯，暖光",
        "confidence": 0.9,
        "evidence": {"quote": "灯市从河岸一直排到城门"},
    }],
    "props": [{
        "name": "没点的灯", "description": "竹骨纸面，灯芯干枯",
        "suggested_prompt": "竹骨纸灯，未点亮，暖灰调", "confidence": 0.8,
        "evidence": {"quote": "提着一盏没点的灯"},
    }],
    "shots": [{
        "index": 1, "scene": "灯市", "summary": "掌灯人举灯等在灯柱下",
        "shot_size": "中景", "camera_move": "固定",
        "dialogue": ["你迟了三年。"], "duration_hint": 5,
        "asset_refs": ["掌灯人", "灯市"], "confidence": 0.7,
        "evidence": {"quote": "他把灯举过头顶"},
    }],
}


class _Chat(BaseHTTPRequestHandler):
    """只认 /chat/completions；回包由 server.reply 决定。"""

    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.headers.get("Authorization") != f"Bearer {SECRET}":
            self._json(401, {"error": {"message": "bad key", "code": "invalid_api_key"}})
            return
        self._json(200, {
            "id": "chatcmpl-w13", "model": "deepseek-flash",
            "choices": [{"message": {"content": self.server.reply},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 60, "completion_tokens": 40,
                      "total_tokens": 100},
        })

    def _json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def text_ai(monkeypatch, provider_state):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Chat)
    server.reply = json.dumps(REPLY, ensure_ascii=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("DEEPSEEK_BASE_URL",
                       f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with get_conn() as conn:
        conn.execute("UPDATE provider_config SET is_default = 0 WHERE capability = 'text'")
        conn.execute(
            """UPDATE provider_config
                  SET provider = 'deepseek', model = 'deepseek-flash',
                      enabled = 1, is_default = 1, key_env = 'DEEPSEEK_API_KEY'
                WHERE capability = 'text' AND backend = 'cloud'""")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _make_doc(fresh_project, content=CONTENT, title="W13 抽取稿"):
    pid = fresh_project("editor")
    made = services.create_document(pid, None, "novel", title, content, "测试账号")
    return made["row"], services.read_content(made["row"]), pid


def _body(pid, doc_id, **extra):
    body = {
        "project_id": pid, "doc_id": doc_id, "instruction": "",
        "estimate": {"quantity": 0.011, "unit": "million_token",
                     "unit_price_micros": 8_000_000, "currency": "CNY",
                     "source": "测试价"},
        "user_confirmed_cloud": True,
    }
    body.update(extra)
    return body


def _submit(client, pid, doc_id, **extra):
    return client.post(f"/api/projects/{pid}/extract/runs",
                       json=_body(pid, doc_id, **extra))


def _drain(job_id=None, tries=6):
    """跑掉队列里的活。按 job id 等它真的终态，避免上一轮排队作业插队。"""
    terminal = {"succeeded", "failed", "cancelled", "needs_attention"}
    for _ in range(tries):
        worker.run_once("w13-test-worker")
        if job_id is None:
            return
        _row = job_repo.get(job_id)
        if _row is not None and _row["status"] in terminal:
            return
    return


# ---------- 纯文本 ----------

def test_解析剥掉代码块并按原文找回证据():
    raw = "```json\n" + json.dumps(REPLY, ensure_ascii=False) + "\n```"
    items, note = extraction.parse_output(CONTENT, raw)
    assert len(items) == 4
    quote = items[0]["evidence"]["quote"]
    assert CONTENT[items[0]["evidence"]["start"]:items[0]["evidence"]["end"]] == quote
    assert "4 条候选" in note


def test_证据不是原文就丢掉这一条():
    broken = json.loads(json.dumps(REPLY))
    broken["characters"][0]["evidence"]["quote"] = "掌灯人站在城门口"
    items, note = extraction.parse_output(CONTENT, json.dumps(broken, ensure_ascii=False))
    assert [i["kind"] for i in items] == ["scene", "prop", "shot"]
    assert "跳过" in note


def test_非_JSON_与空正文都拒():
    with pytest.raises(extraction.ExtractInvalid):
        extraction.parse_output(CONTENT, "好的，我来抽取：")
    with pytest.raises(extraction.ExtractInvalid):
        extraction.build_prompt("空稿", "   ")


def test_镜头候选导出_Markdown():
    items, _ = extraction.parse_output(CONTENT, json.dumps(REPLY, ensure_ascii=False))
    md = extraction.shots_markdown("第一章", items)
    assert md.startswith("# 第一章 · 镜头候选")
    assert "掌灯人举灯等在灯柱下" in md and "你迟了三年。" in md


# ---------- Job 闭环 ----------

def test_提交去重_抽完落候选_并冻结版本(as_role, text_ai, fresh_project, clean_queue):
    doc, data, pid = _make_doc(fresh_project)
    c = as_role("editor")
    first = _submit(c, pid, doc["id"])
    assert first.status_code == 202, first.text
    run1 = first.json()
    assert run1["deduped"] is False and run1["base_version"] == data["version"]

    second = _submit(c, pid, doc["id"])
    assert second.status_code == 202
    assert second.json()["deduped"] is True
    assert second.json()["id"] == run1["id"]

    _drain(run1["job_id"])
    got = c.get(f"/api/extract/runs/{run1['id']}")
    assert got.status_code == 200, got.text
    view = got.json()
    assert view["status"] == "done" and view["counts"]["pending"] == 4
    assert view["stale"] is False
    assert view["cost_micros"] and view["billable_status"] == "pending_reconciliation"

    items = c.get(f"/api/extract/runs/{run1['id']}/items").json()
    kinds = [i["kind"] for i in items]
    assert kinds == ["character", "scene", "prop", "shot"]
    for item in items:
        assert CONTENT[item["evidence"]["start"]:item["evidence"]["end"]] == item["evidence"]["quote"]


def test_确认入资产_重复确认_409_镜头候选不能入库(as_role, text_ai, fresh_project, clean_queue):
    doc, _data, pid = _make_doc(fresh_project)
    c = as_role("editor")
    run = _submit(c, pid, doc["id"]).json()
    _drain(run["job_id"])
    items = c.get(f"/api/extract/runs/{run['id']}/items").json()
    character = next(i for i in items if i["kind"] == "character")
    shot = next(i for i in items if i["kind"] == "shot")

    made = c.post(f"/api/extract/items/{character['id']}/accept",
                  json={"summary": "人工改过的简介"})
    assert made.status_code == 200, made.text
    asset = made.json()["asset"]
    assert asset["kind"] == "character" and asset["summary"] == "人工改过的简介"
    assert asset["aliases"] == ["提灯的老者"]

    again = c.post(f"/api/extract/items/{character['id']}/accept", json={})
    assert again.status_code == 409

    bad = c.post(f"/api/extract/items/{shot['id']}/accept", json={})
    assert bad.status_code == 422


def test_合并进已有资产_只补空值(as_role, text_ai, fresh_project, clean_queue):
    doc, _data, pid = _make_doc(fresh_project)
    c = as_role("editor")
    existing = assets.create(pid, "scene", "灯市旧名", "", "人写的提示词", [], [])
    run = _submit(c, pid, doc["id"]).json()
    _drain(run["job_id"])
    items = c.get(f"/api/extract/runs/{run['id']}/items").json()
    scene = next(i for i in items if i["kind"] == "scene")

    merged = c.post(f"/api/extract/items/{scene['id']}/merge",
                    json={"asset_id": existing["id"]})
    assert merged.status_code == 200, merged.text
    target = merged.json()["asset"]
    assert "灯市" in target["aliases"] and target["name"] == "灯市旧名"
    assert target["consistency_prompt"] == "人写的提示词"   # 不覆盖人写的


def test_驳回留记录_原文改了旧证据作废(as_role, text_ai, fresh_project, clean_queue):
    doc, data, pid = _make_doc(fresh_project)
    c = as_role("editor")
    run = _submit(c, pid, doc["id"]).json()
    _drain(run["job_id"])
    items = c.get(f"/api/extract/runs/{run['id']}/items").json()
    prop = next(i for i in items if i["kind"] == "prop")

    rejected = c.post(f"/api/extract/items/{prop['id']}/reject",
                      json={"reason": "同一个人，已在第 3 章建过"})
    assert rejected.status_code == 200
    assert rejected.json()["item"]["status"] == "rejected"

    # 改正文之后，run 立刻标 stale；剩下待处理的候选不能再按旧证据入库
    services.save_content(doc, "改过的正文。\n", data["version"], "测试账号",
                          as_role.id("editor"))
    view = c.get(f"/api/extract/runs/{run['id']}").json()
    assert view["stale"] is True
    remaining = c.get(f"/api/extract/runs/{run['id']}/items?status=pending").json()
    assert remaining
    blocked = c.post(f"/api/extract/items/{remaining[0]['id']}/accept", json={})
    assert blocked.status_code == 409


def test_镜头候选导出接口(as_role, text_ai, fresh_project, clean_queue):
    doc, _data, pid = _make_doc(fresh_project)
    c = as_role("editor")
    run = _submit(c, pid, doc["id"]).json()
    _drain(run["job_id"])
    md = c.get(f"/api/extract/runs/{run['id']}/shots.md")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert "镜头候选" in md.text and "灯市" in md.text


def test_权限与跨项目(as_role, text_ai, fresh_project):
    doc, _data, pid = _make_doc(fresh_project)
    assert as_role("viewer").post(
        f"/api/projects/{pid}/extract/runs", json=_body(pid, doc["id"])
    ).status_code == 403
    assert as_role("outsider").post(
        f"/api/projects/{pid}/extract/runs", json=_body(pid, doc["id"])
    ).status_code == 404

    run = _submit(as_role("editor"), pid, doc["id"]).json()
    assert as_role("outsider").get(f"/api/extract/runs/{run['id']}").status_code == 404


def test_作业失败时运行也失败(as_role, text_ai, fresh_project, clean_queue, monkeypatch):
    doc, _data, pid = _make_doc(fresh_project)
    text_ai.reply = "这不是 JSON"
    c = as_role("editor")
    run = _submit(c, pid, doc["id"]).json()
    _drain(run["job_id"])
    view = c.get(f"/api/extract/runs/{run['id']}").json()
    assert view["status"] == "parse_failed"
    assert "JSON" in view["note"]
