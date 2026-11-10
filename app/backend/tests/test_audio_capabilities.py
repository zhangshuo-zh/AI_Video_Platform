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


def test_项目看板的音频那句也必须现算不能写死(client, monkeypatch):
    """E5 那次修复的回归钉：`episode_board()` 的 `audio_note` 曾经是硬编码
    「执行引擎尚未接入，只能登记台账」。

    它写下时（2026-11-05 09:39）是对的，但当天 10:37 配音腿就接线了
    （`runners._voxcpm`）。于是同一屏里 `audio_status_view()` 如实说"配音能提交"，
    而 `episode_board()` 还在说"没接线"——**一个模块两种口径**。

    这里把 `_audio_wired()` 拨成"配音接了、音乐没配"，要求那句话跟着变。
    只断言"不含未接入"不够：把整句删空也能过；所以同时要求它说出
    配音能提交、且点出音乐缺的是默认轨。
    """
    monkeypatch.setattr(production, "_audio_wired", lambda: {
        "tts": {"label": "配音", "provider": "voxcpm2",
                "health": {"state": "ok", "detail": "运行时齐"},
                "runner_wired": True},
        "music": {"label": "音乐", "provider": "",
                  "health": {"state": "unconfigured", "detail": "默认轨没配"},
                  "runner_wired": False},
    })
    # 直接问那句话的生成逻辑，不建一整集（建集要多张表与权限，与这条判据无关）
    note = production._audio_stage_note(production._audio_wired())
    assert "尚未接入" not in note, f"配音已接线，这句话是假的：{note}"
    assert "配音可提交" in note, note
    assert "音乐" in note and "默认轨" in note, note


def test_两条腿都没接线时那句才说未接入(client, monkeypatch):
    """反向：真没接线时仍要如实说"未接入"，不能被上一条修成永远沉默。"""
    monkeypatch.setattr(production, "_audio_wired", lambda: {
        "tts": {"label": "配音", "provider": "",
                "health": {"state": "unconfigured", "detail": "默认轨没配"},
                "runner_wired": False},
        "music": {"label": "音乐", "provider": "",
                  "health": {"state": "unconfigured", "detail": "默认轨没配"},
                  "runner_wired": False},
    })
    note = production._audio_stage_note(production._audio_wired())
    assert "尚未接入" in note, note


def test_能力表不含项目内容任何登录用户都能读(as_role):
    """这一页只说平台能力，所以不需要 project_id，也不许带出任何项目字段。

    viewer 角色带 `audio:read`（音频台账要能看），所以这一条也在验权限面：
    它读得到能力，读不到任何项目内容。"""
    body = as_role("viewer").get("/api/audio/capabilities").json()
    text = str(body)
    for key in ("project_id", "episode_id", "shot_id", "clip"):
        assert key not in text, f"能力表里漏出了 {key}"
