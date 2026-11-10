"""W11：文档 AI 操作。纯文本规则 + 一条真 Job 的完整闭环。

三层各自回答一个问题：
- `docai` 单测：六种操作的提示词与范围、三种写回语义、diff——不碰库、不花钱。
- 接口/服务测：提交时冻结了什么、预览是否只给能用的结果、应用是否复用
  `save_content` 的冲突/锁/Git 三道关。
- 失败分支：模型 401、文档在这一期间被改、锁在别人手里——**原文一个字都不能动**。

供应商仍然打本地假 HTTP（与 test_cloud 同一套思路），真实 DeepSeek 的
一次润色闭环记在 `盘点/W11_DOC_AI.md`，不塞进 pytest。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import docai
import gitrepo
import services
import worker
from db import get_conn
from repositories.documents import documents as docs
from repositories.jobs import jobs as job_repo

SECRET = "sk-w11-test-secret"


class _Chat(BaseHTTPRequestHandler):
    """只做 /chat/completions。回包内容由 server.reply 决定，状态码由 server.status。"""

    def log_message(self, *args):
        return

    def _json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {}
        self.server.calls.append(body)
        if self.headers.get("Authorization") != f"Bearer {SECRET}":
            self._json(401, {"error": {"message": "bad key " + SECRET,
                                       "code": "invalid_api_key"}})
            return
        if self.server.status != 200:
            self._json(self.server.status,
                       {"error": {"message": "mock 失败 " + SECRET,
                                  "code": "rate_limit"}})
            return
        self._json(200, {
            "id": "chatcmpl-docai-1",
            "model": "deepseek-flash",
            "choices": [{"message": {"content": self.server.reply},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8,
                      "total_tokens": 28},
        })


@pytest.fixture
def text_ai(monkeypatch, provider_state):
    """把文本默认轨切到 deepseek 云端，并给一个本地假接口。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Chat)
    server.reply = "润色后的第二段。"
    server.status = 200
    server.calls = []
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


BASE_TEXT = "第一段，已经很好。\n第二段要润色。\n"


def _make_doc(fresh_project, content=BASE_TEXT, title="W11 文档", kind="novel"):
    pid = fresh_project("editor")
    made = services.create_document(pid, None, kind, title, content, "测试账号")
    return made["row"], services.read_content(made["row"])


def _estimate(**extra):
    data = {"quantity": 0.006, "unit": "million_token",
            "unit_price_micros": 8_000_000, "currency": "CNY",
            "source": "测试价"}
    data.update(extra)
    return data


def _submit(client, doc_id, content, operation="polish", start=None, end=None,
            scope="selection", instruction="", **extra):
    text = content["content"]
    if scope == "document" or operation in ("draft", "continue"):
        start, end = 0, 0
    else:
        if start is None:
            start = text.index("第二段")
        if end is None:
            end = start + len("第二段要润色。")
    body = {"operation": operation, "instruction": instruction,
            "scope": scope, "start": start, "end": end,
            "selection_text": text[start:end],
            "estimate": _estimate(**extra), "user_confirmed_cloud": True}
    return client.post(f"/api/documents/{doc_id}/ai-jobs", json=body)


def _drain():
    """跑掉队列里那一条（假接口的 env 已由 text_ai 夹具注入本进程）。"""
    return worker.run_once("w11-test-worker")


# ---------- docai 纯文本规则 ----------

def test_六种操作的落点写死():
    assert set(docai.OPERATIONS) == {
        "draft", "expand", "continue", "polish", "scriptify", "prompt"}
    assert docai.OPERATIONS["continue"]["base_apply"] == "append"
    assert docai.OPERATIONS["draft"]["base_apply"] == "replace_document"
    # 起草与续写永远处理全文，前端传 selection 也不会生效
    part = docai.resolve_scope("甲乙丙", "continue", "selection", 0, 1, "甲")
    assert part["scope"] == "document" and part["apply_mode"] == "append"


def test_选区偏移不对时按唯一次文字找回():
    content = "前面\n目标句子\n后面"
    part = docai.resolve_scope(content, "polish", "selection", 0, 2, "目标句子")
    assert (part["start"], part["end"]) == (3, 7)
    assert part["apply_mode"] == "replace_selection"


def test_选区文字出现两次就拒():
    with pytest.raises(docai.DocAiInvalid):
        docai.resolve_scope("同一句。同一句。", "polish", "selection", 0, 3, "同一句。")


def test_选区空白与超限都拒():
    with pytest.raises(docai.DocAiInvalid):
        docai.resolve_scope("abc", "polish", "selection", 0, 0, "  ")
    with pytest.raises(docai.DocAiInvalid):
        docai.resolve_scope("x" * (docai.MAX_INPUT_CHARS + 1), "polish",
                            "document", 0, 0)


def test_提示词按操作不同且要求只输出结果():
    content = "他推开门。"
    polish, params = docai.build_prompt("polish", "第一章", content, content, "")
    script, _ = docai.build_prompt("scriptify", "第一章", content, content, "")
    assert "润色" in polish and "只输出润色后的段落" in polish
    assert "剧本" in script and "场景" in script
    assert "只输出结果本身" in polish
    assert params["max_tokens"] == 4096 and params["thinking"] is False
    # 附加要求会进提示词，但不改变原文段落
    extra, _ = docai.build_prompt("expand", "第一章", content, content, "多写气味")
    assert "多写气味" in extra and content in extra


def test_三种写回语义():
    assert docai.apply_text("旧", "新", "replace_document") == "新"
    assert docai.apply_text("甲\n乙", "丙", "append") == "甲\n乙\n\n丙"
    assert docai.apply_text("甲乙丙", "X", "replace_selection", 1, 2) == "甲X丙"
    with pytest.raises(docai.DocAiInvalid):
        docai.apply_text("甲", "  ", "replace_document")
    with pytest.raises(docai.DocAiInvalid):
        docai.apply_text("甲", "X", "replace_selection", 0, 9)


def test_估算向上取且_diff_有增删行():
    assert docai.estimate_quantity(100, 4096) == 0.005
    assert docai.estimate_quantity(0, 1) == 0.001
    diff = docai.diff_text("第一段。\n旧句。\n", "第一段。\n新句。\n")
    assert "-旧句。" in diff and "+新句。" in diff


# ---------- 接口：计价、提交与预览 ----------

def test_计价回清单单价与预计用量(as_role, text_ai, fresh_project):
    row, content = _make_doc(fresh_project)
    client = as_role("editor")
    start = content["content"].index("第二段")
    resp = client.get(f"/api/documents/{row['id']}/ai-options",
                      params={"operation": "polish", "scope": "selection",
                              "start": start, "end": start + 7,
                              "selection_text": "第二段要润色。"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["provider"] == "deepseek" and data["model"] == "deepseek-flash"
    assert data["unit"] == "million_token"
    assert data["unit_price_micros"] == 8_000_000
    assert data["chars"] == 7
    assert data["estimated_quantity"] == docai.estimate_quantity(7, 4096)
    assert data["health_state"] == "ok"


def test_提交冻结提示词与版本_且正文此刻不动(as_role, text_ai, clean_queue,
                                          fresh_project):
    row, content = _make_doc(fresh_project)
    client = as_role("editor")
    resp = _submit(client, row["id"], content)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    job = job_repo.get(job_id)
    assert job["status"] == "queued"
    assert job["kind"] == "text" and job["execution_mode"] == "cloud"
    assert job["provider_code"] == "deepseek"
    assert "第二段要润色。" in job["prompt_snapshot"]
    refs = json.loads(job["input_refs_json"])
    assert refs[0]["doc_id"] == row["id"]
    assert refs[0]["operation"] == "polish"
    assert refs[0]["base_version"] == content["version"]
    assert refs[0]["apply_mode"] == "replace_selection"
    assert job["cost_micros"] > 0 and job["billable_status"] == "pending_reconciliation"
    # 提交只落 Job：正文与版本一个字节都没变
    assert services.read_content(row)["version"] == content["version"]
    assert services.read_content(row)["content"] == content["content"]


def test_没勾确认或价格不符都拒(as_role, text_ai, fresh_project):
    row, content = _make_doc(fresh_project)
    client = as_role("editor")
    bad = {"operation": "polish", "scope": "selection",
           "start": content["content"].index("第二段"),
           "end": content["content"].index("第二段") + 7,
           "selection_text": "第二段要润色。",
           "estimate": _estimate(), "user_confirmed_cloud": False}
    assert client.post(f"/api/documents/{row['id']}/ai-jobs",
                       json=bad).status_code == 422
    bad["user_confirmed_cloud"] = True
    bad["estimate"] = _estimate(unit_price_micros=1)
    assert client.post(f"/api/documents/{row['id']}/ai-jobs",
                       json=bad).status_code == 422


def test_密钥缺失时不建作业(as_role, text_ai, monkeypatch, fresh_project):
    row, content = _make_doc(fresh_project)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client = as_role("editor")
    with get_conn() as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"]
    resp = _submit(client, row["id"], content)
    assert resp.status_code == 422
    assert "不可用" in resp.json()["detail"]
    with get_conn() as conn:
        after = conn.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"]
    assert after == before


# ---------- 完整闭环：润色 → diff → 应用 → Git ----------

def test_润色选区_预览_diff_应用_git提交(as_role, text_ai, clean_queue,
                                        fresh_project):
    row, content = _make_doc(fresh_project)
    client = as_role("editor")
    job_id = _submit(client, row["id"], content).json()["job_id"]
    assert _drain() == job_id

    done = job_repo.get(job_id)
    assert done["status"] == "succeeded", done["error_message"]
    assert done["provider_request_id"] == "chatcmpl-docai-1"
    assert json.loads(done["resources_json"])["provider"]["usage"]["total_tokens"] == 28

    preview = client.get(f"/api/documents/{row['id']}/ai-jobs/{job_id}")
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["status"] == "succeeded" and data["can_apply"] is True
    assert data["conflict"] is False
    assert "-第二段要润色。" in data["diff"]
    assert "+润色后的第二段。" in data["diff"]
    assert data["content"] == "润色后的第二段。"
    # 预览阶段仍然没动正文
    assert services.read_content(row)["version"] == content["version"]

    applied = client.post(f"/api/documents/{row['id']}/ai-jobs/{job_id}/apply")
    assert applied.status_code == 200, applied.text
    out = applied.json()
    assert out["changed"] is True and out["job_id"] == job_id
    assert out["operation"] == "polish" and out["operation_label"] == "润色"

    after = services.read_content(row)
    assert after["version"] == out["version"]
    assert after["version"] != content["version"]
    assert after["content"] == "第一段，已经很好。\n润色后的第二段。\n"

    hist = gitrepo.history(row["rel_path"])
    assert hist[0]["commit"] == out["git_commit"]
    assert hist[0]["subject"] == f"AI 润色：W11 文档（作业 #{job_id}）"

    # 再应用一次：基线已经变了，必须当冲突拒掉，而不是把同一段再拼一遍
    again = client.post(f"/api/documents/{row['id']}/ai-jobs/{job_id}/apply")
    assert again.status_code == 409
    assert services.read_content(row)["content"] == after["content"]


def test_续写追加到文末(as_role, text_ai, clean_queue, fresh_project):
    text_ai.reply = "他弯腰捡起一块干木头。"
    row, content = _make_doc(fresh_project)
    client = as_role("editor")
    job_id = _submit(client, row["id"], content, operation="continue",
                     scope="document").json()["job_id"]
    assert _drain() == job_id
    assert client.post(f"/api/documents/{row['id']}/ai-jobs/{job_id}/apply").status_code == 200
    after = services.read_content(row)["content"]
    assert after == BASE_TEXT.rstrip() + "\n\n他弯腰捡起一块干木头。\n"


def test_空文档可以起草(as_role, text_ai, clean_queue, fresh_project):
    text_ai.reply = "第一章的开头。"
    row, content = _make_doc(fresh_project, content="", title="空稿")
    assert content["content"] == ""
    client = as_role("editor")
    job_id = _submit(client, row["id"], content, operation="draft",
                     scope="document").json()["job_id"]
    assert _drain() == job_id
    assert client.post(f"/api/documents/{row['id']}/ai-jobs/{job_id}/apply").status_code == 200
    assert services.read_content(row)["content"] == "第一章的开头。\n"


# ---------- 失败与冲突：原文一个字都不能动 ----------

def test_模型失败时作业失败且原文不动(as_role, text_ai, clean_queue, fresh_project):
    text_ai.status = 429
    row, content = _make_doc(fresh_project)
    client = as_role("editor")
    job_id = _submit(client, row["id"], content).json()["job_id"]
    assert _drain() == job_id
    done = job_repo.get(job_id)
    assert done["status"] == "failed"
    assert done["error_code"] == "provider_rate_limit"
    assert SECRET not in (done["error_message"] or "")

    preview = client.get(f"/api/documents/{row['id']}/ai-jobs/{job_id}").json()
    assert preview["can_apply"] is False and preview["diff"] == ""
    assert preview["error_code"] == "provider_rate_limit"
    assert client.post(
        f"/api/documents/{row['id']}/ai-jobs/{job_id}/apply").status_code == 422
    assert services.read_content(row)["version"] == content["version"]
    assert services.read_content(row)["content"] == content["content"]


def test_运行期间文档被改过就拒绝(as_role, text_ai, clean_queue, fresh_project):
    row, content = _make_doc(fresh_project)
    client = as_role("editor")
    job_id = _submit(client, row["id"], content).json()["job_id"]
    assert _drain() == job_id

    # 另一个编辑器抢在前面保存了一份新正文
    other = "第一段，已经很好。\n第二段被别人改过了。\n"
    services.save_content(row, other, content["version"], "别人",
                          as_role.id("admin"), 15)

    preview = client.get(f"/api/documents/{row['id']}/ai-jobs/{job_id}").json()
    assert preview["conflict"] is True and preview["can_apply"] is False
    assert preview["diff"] == "" and preview["current_version"] != preview["base_version"]

    applied = client.post(f"/api/documents/{row['id']}/ai-jobs/{job_id}/apply")
    assert applied.status_code == 409
    assert services.read_content(row)["content"] == other


def test_锁在别人手里时应用被挡(as_role, text_ai, clean_queue, fresh_project):
    row, content = _make_doc(fresh_project)
    client = as_role("editor")
    job_id = _submit(client, row["id"], content).json()["job_id"]
    assert _drain() == job_id
    docs.acquire_lock(row["id"], as_role.id("admin"), 15)
    resp = client.post(f"/api/documents/{row['id']}/ai-jobs/{job_id}/apply")
    assert resp.status_code == 423
    assert services.read_content(row)["version"] == content["version"]


def test_作业未成功不能应用也不跨文档(as_role, text_ai, clean_queue,
                                   fresh_project):
    row, content = _make_doc(fresh_project)
    # 同一项目下的另一份：不是"另一个项目看不见"，而是"这条作业不长在这份上"
    other_row = services.create_document(
        row["project_id"], None, "novel", "同一项目的另一份", "另一段。", "测试账号")["row"]
    client = as_role("editor")
    job_id = _submit(client, row["id"], content).json()["job_id"]
    # 还没跑
    assert client.post(
        f"/api/documents/{row['id']}/ai-jobs/{job_id}/apply").status_code == 422
    # 拿到别的文档下面去问：404，不透露这条作业属于谁
    assert client.get(
        f"/api/documents/{other_row['id']}/ai-jobs/{job_id}").status_code == 404
    assert client.get(f"/api/documents/{row['id']}/ai-jobs/999999").status_code == 404


def test_选中文字找不到就拒(as_role, text_ai, fresh_project):
    row, content = _make_doc(fresh_project)
    client = as_role("editor")
    body = {"operation": "polish", "scope": "selection", "start": 0, "end": 3,
            "selection_text": "不存在的句子", "estimate": _estimate(),
            "user_confirmed_cloud": True}
    resp = client.post(f"/api/documents/{row['id']}/ai-jobs", json=body)
    assert resp.status_code == 422
    assert "不在当前正文里" in resp.json()["detail"]


def test_报价必须随带估算口径(as_role, text_ai, fresh_project):
    """两份报价都要说明"这个数字是估算"（2026-11-10 裁定，F11）。

    为什么这条值得单独立：`estimate_quantity` 把「输入字数 + 输出上限」
    直接当 token 数用（`docai.py` 里那个函数），而界面写的是「预计费用 ¥0.xx」。
    一个精确到分位的数字很容易被当成"就是这么多"，实际结算按供应商回执的
    `usage` 走，两者会差。口径由**服务端下发**而不是前端写死：
    它跟算法是一体的，算法改了文案得跟着改。

    判据三条（缺一条都会让界面重新开始说半句话）：
      A `estimate_kind` 必须是 `approximate`（不是 exact 就不能写得像精确值）；
      B `estimate_note` 不能是空字符串——空就等于"没说"；
      C 两处报价口径必须一致，否则同一个用户在两个弹窗里看到两种可信度。
    """
    row, content = _make_doc(fresh_project)
    client = as_role("editor")

    r1 = client.get(f"/api/documents/{row['id']}/ai-options",
                    params={"operation": "polish", "scope": "document"})
    assert r1.status_code == 200, r1.text
    o1 = r1.json()

    r2 = client.get(f"/api/projects/{row['project_id']}/extract/options",
                    params={"doc_id": row["id"]})
    assert r2.status_code == 200, r2.text
    o2 = r2.json()

    for name, o in (("文档 AI 报价", o1), ("文本抽取报价", o2)):
        assert o.get("estimate_kind") == docai.ESTIMATE_KIND, (
            f"{name} 的 estimate_kind 不对：{o.get('estimate_kind')!r}")
        assert o.get("estimate_note"), (
            f"{name} 没下发 estimate_note —— 界面上「预计费用 ¥0.xx」"
            f"就变成了一句没有限定的断言")
    assert o1["estimate_note"] == o2["estimate_note"], (
        "两处报价的估算口径不一致，同一用户在两个弹窗里会看到两种可信度")
