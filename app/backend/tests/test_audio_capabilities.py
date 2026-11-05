"""音频能力状态透明化（任务书 §6.1 第五阶段、§15 P1 第 8 项）。

这一页以前靠**按钮存不存在**和四行写死的提示来说话。写死的部分一过时就变成假话：
清单加了档、默认轨配上了、或者 `runners.build()` 接上了一条分支，
前端那四行还写着"未接线"。所以判据搬到了服务端（`production.audio_status_view()`），
并且只读三处已有的事实：清单里有没有这个 capability、默认轨解析不解析得出、
执行分支接没接上。

用例的重点是**这套判据能被改红**：把三处里的一处拨一下，结论就得跟着变；
一直回 False 的字段不值得测。
"""
import production


def test_能力表五行且口型音效也在表里(as_role):
    """透明化包含"这功能根本不存在"这一类：口型与音效也占一行，写清缺什么。"""
    rows = as_role("editor").get("/api/audio/capabilities").json()["rows"]
    codes = [r["code"] for r in rows]
    assert codes == ["dialogue", "narration", "music", "sfx", "lipsync"], codes
    assert all(len(set(r)) >= 10 for r in rows)
    # 音效与口型今天连 capability 都没有——空串就是空串，不编一个
    by_code = {r["code"]: r for r in rows}
    assert by_code["sfx"]["capability"] == ""
    assert by_code["lipsync"]["capability"] == ""
    assert any("清单" in m for m in by_code["lipsync"]["missing"])


def test_今天没有一类能提交但登记台账永远可以(as_role):
    body = as_role("editor").get("/api/audio/capabilities").json()
    assert body["any_wired"] is False
    assert all(r["can_submit"] is False for r in body["rows"])
    assert all(r["why"] for r in body["rows"]), "不能提交却不说为什么＝没透明化"
    # 登记台词与归属是纯库内动作，和执行接没接线无关
    assert all(r["can_register"] for r in body["rows"] if r["code"] != "lipsync")
    assert "没有一类能真正执行" in body["note"]


def test_三处判据拨一下就跟着变红(client, monkeypatch):
    """把"接线了"这一事实喂进去，结论必须翻——证明字段不是写死的 False。"""
    def fake_wired():
        return {
            "tts": {"label": "配音", "provider": "voxcpm2",
                    "health": {"state": "ok", "detail": "运行时齐"},
                    "runner_wired": True},
            "music": {"label": "音乐", "provider": "",
                      "health": {"state": "unconfigured", "detail": "默认轨没配"},
                      "runner_wired": False},
        }

    monkeypatch.setattr(production, "_audio_wired", fake_wired)
    body = production.audio_status_view()
    by_code = {r["code"]: r for r in body["rows"]}
    assert by_code["dialogue"]["can_submit"] is True
    # 旁白走同一个 tts 能力，所以它也一起亮：这条不测就会出现"配音能提交旁白不能"的怪话
    assert by_code["narration"]["can_submit"] is True
    assert by_code["music"]["can_submit"] is False
    assert "默认轨" in by_code["music"]["why"]
    assert body["any_wired"] is True
    assert "已经能提交执行" in body["note"]


def test_只接一半时给的是提醒不是可提交(client, monkeypatch):
    """runner 接了但默认轨没配＝还不能提交。"""
    monkeypatch.setattr(production, "_audio_wired", lambda: {
        "tts": {"label": "配音", "provider": "",
                "health": {"state": "unconfigured", "detail": "默认轨没配"},
                "runner_wired": True}})
    row = production.audio_status_view()["rows"][0]
    assert row["runner_wired"] is True and row["can_submit"] is False
    assert "默认轨" in row["why"]


def test_能力表不含项目内容任何登录用户都能读(as_role):
    """这一页只说平台能力，所以不需要 project_id，也不许带出任何项目字段。

    viewer 角色带 `audio:read`（音频台账要能看），所以这一条也在验权限面：
    它读得到能力，读不到任何项目内容。"""
    body = as_role("viewer").get("/api/audio/capabilities").json()
    text = str(body)
    for key in ("project_id", "episode_id", "shot_id", "clip"):
        assert key not in text, f"能力表里漏出了 {key}"
