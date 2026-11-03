"""成片中心接口（阶段 D）：201／幂等／422 点名／权限边界。

真实 FFmpeg 合成的那一半在 test_composition.py；这一份只盯接口契约：
状态码、返回字段、幂等重放，以及"看不见的项目回 404"这条老规矩。
前置数据复用 test_composition 的 `stage`（两镜真片段 + 两张审核签字）。
"""
from __future__ import annotations

import uuid
from urllib.parse import quote

import pytest

from repositories.projects import projects as project_repo
from test_composition import _approve, _clip, _has_ffmpeg, stage  # noqa: F401 —— stage 是夹具

pytestmark = pytest.mark.skipif(not _has_ffmpeg(), reason="本机没有 ffmpeg")


def _signed(stage_fixture) -> None:
    for job in stage_fixture["jobs"]:
        _approve(job, stage_fixture["project_id"], stage_fixture["user_id"])


def test_创建列表详情与权限(as_role, people, stage):  # noqa: F811
    _signed(stage)
    pid = stage["project_id"]
    editor = as_role("editor")

    body = {"episode_id": stage["episode_id"], "width": 640, "height": 360,
            "fps": 24, "idempotency_key": f"api-{uuid.uuid4().hex[:8]}"}
    created = editor.post(f"/api/projects/{pid}/compositions", json=body)
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["version"] == 1 and row["job_id"]
    assert row["episode_seq"] == 1
    assert row["shot_count"] == 2
    assert row["timeline"]["shots"][0]["video"]["review"] == "approved"

    listing = editor.get(f"/api/projects/{pid}/compositions")
    assert listing.status_code == 200
    assert [r["id"] for r in listing.json()] == [row["id"]]

    detail = editor.get(f"/api/compositions/{row['id']}")
    assert detail.status_code == 200
    assert detail.json()["job_id"] == row["job_id"]

    # viewer 被加成协作人后读得到（job:read），但发不起（没有 job:submit）
    project_repo.add_member(pid, people["viewer"]["id"], people["editor"]["id"])
    viewer = as_role("viewer")
    assert viewer.get(f"/api/projects/{pid}/compositions").status_code == 200
    assert viewer.get(f"/api/compositions/{row['id']}").status_code == 200
    denied = viewer.post(f"/api/projects/{pid}/compositions",
                         json={"episode_id": stage["episode_id"]})
    assert denied.status_code == 403

    # 外人：列表 404、详情 404，连"存在"都不透露
    outsider = as_role("outsider")
    assert outsider.get(f"/api/projects/{pid}/compositions").status_code == 404
    assert outsider.get(f"/api/compositions/{row['id']}").status_code == 404
    assert editor.get("/api/compositions/999999").status_code == 404


def test_同一个幂等键重放不建第二版(as_role, stage):  # noqa: F811
    _signed(stage)
    pid = stage["project_id"]
    key = f"same-{uuid.uuid4().hex[:8]}"
    body = {"episode_id": stage["episode_id"], "width": 640, "height": 360,
            "idempotency_key": key}
    c = as_role("editor")
    first = c.post(f"/api/projects/{pid}/compositions", json=body)
    again = c.post(f"/api/projects/{pid}/compositions", json=body)
    assert first.status_code == 201 and again.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["version"] == first.json()["version"]
    assert len(c.get(f"/api/projects/{pid}/compositions").json()) == 1


def test_前置检查不过回422并点名哪几镜(as_role, people, fresh_project, clean_queue):
    from repositories.content import episodes, shots

    pid = fresh_project("editor")
    eid = episodes.create(pid, f"未审集_{uuid.uuid4().hex[:5]}")["id"]
    s1 = shots.create(eid, "已拍未审", "", 1.0, [])["id"]
    s2 = shots.create(eid, "没片", "", 1.0, [])["id"]
    _clip(pid, eid, s1, people["editor"]["id"], "teal")

    r = as_role("editor").post(f"/api/projects/{pid}/compositions",
                               json={"episode_id": eid, "width": 640, "height": 360})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "第 1 镜" in detail and "审核" in detail
    assert "第 2 镜" in detail and "没有成功的视频作业" in detail
    assert s2


# ---------- 字幕入口（阶段 D 的补口） ----------

GOOD_SRT = "1\n00:00:00,000 --> 00:00:01,000\n火\n"


def _upload(client, pid, name, content, mime="text/plain"):
    return client.post(f"/api/projects/{pid}/composition-subtitles",
                       files={"file": (name, content, mime)})


def test_字幕上传列表与删除(as_role, stage):  # noqa: F811
    pid = stage["project_id"]
    c = as_role("editor")

    empty = c.get(f"/api/projects/{pid}/composition-subtitles")
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    # 能力位与文件一起给：界面靠它决定字幕框是可选还是禁用
    assert empty.json()["subtitle_supported"] in (True, False)

    up = _upload(c, pid, "第一版.srt", GOOD_SRT.encode("utf-8"))
    assert up.status_code == 201, up.text
    row = up.json()["items"][0]
    assert row["path"].startswith(f"subtitles/{pid}/")
    assert row["bytes"] == len(GOOD_SRT.encode("utf-8"))
    assert row["name"].endswith(".srt")

    got = _upload(c, pid, "second.vtt", b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhi\n")
    assert got.status_code == 201, got.text
    assert len(got.json()["items"]) == 2

    gone = c.delete(f"/api/projects/{pid}/composition-subtitles"
                    f"?path={quote(row['path'])}")
    assert gone.status_code == 200, gone.text
    assert [i["path"] for i in gone.json()["items"]] == [
        i["path"] for i in got.json()["items"] if i["path"] != row["path"]]


def test_坏字幕在门口就拒(as_role, stage):  # noqa: F811
    pid = stage["project_id"]
    c = as_role("editor")
    cases = [
        ("字幕.txt", GOOD_SRT.encode("utf-8"), "只收"),
        ("空.srt", b"", "空的"),
        ("没轴.srt", "1\n火\n".encode("utf-8"), "时间轴"),
        ("不像vtt.vtt", "1\n00:00:00,000 --> x\n".encode("utf-8"), "WEBVTT"),
        ("超大.srt", ("1\n00:00:00,000 --> 00:00:01,000\n" + "火" * 900_000)
         .encode("utf-8"), "超过"),
        ("不是utf8.srt", b"\xff\xfe\x00bad", "UTF-8"),
    ]
    for name, content, why in cases:
        r = _upload(c, pid, name, content)
        assert r.status_code == 422, (name, r.text)
        assert why in r.json()["detail"], (name, r.json())
    assert c.get(f"/api/projects/{pid}/composition-subtitles").json()["items"] == []


def test_字幕的越权与跨项目边界(as_role, people, fresh_project, stage):  # noqa: F811
    pid = stage["project_id"]
    other = fresh_project("editor")
    c = as_role("editor")
    up = _upload(c, other, "别人的.srt", GOOD_SRT.encode("utf-8"))
    assert up.status_code == 201, up.text
    foreign = up.json()["items"][0]["path"]

    # 看不见的项目：列表 404、上传 404，不透露存在
    outsider = as_role("outsider")
    assert outsider.get(f"/api/projects/{pid}/composition-subtitles").status_code == 404
    assert _upload(outsider, pid, "x.srt", GOOD_SRT.encode("utf-8")).status_code == 404

    # viewer 读得到但删不掉（没有 job:submit）
    project_repo.add_member(pid, people["viewer"]["id"], people["editor"]["id"])
    viewer = as_role("viewer")
    assert viewer.get(f"/api/projects/{pid}/composition-subtitles").status_code == 200
    assert viewer.delete(f"/api/projects/{pid}/composition-subtitles?path=x").status_code == 403

    # 拿别的项目的字幕路径来删：删不动，回 404（不是 403——不承认它在这里可用）
    mine = _upload(c, pid, "我的.srt", GOOD_SRT.encode("utf-8"))
    assert mine.status_code == 201
    cross = c.delete(f"/api/projects/{pid}/composition-subtitles?path={quote(foreign)}")
    assert cross.status_code == 404, cross.text
    assert foreign in [i["path"] for i in
                      c.get(f"/api/projects/{other}/composition-subtitles").json()["items"]]

    # 路径写法本身也不给越界机会
    for sneaky in ["../subtitles/%s/x.srt" % other, "/etc/passwd",
                   "subtitles/%s/../../%s/x.srt" % (pid, other)]:
        r = c.delete(f"/api/projects/{pid}/composition-subtitles?path={quote(sneaky)}")
        assert r.status_code == 404, sneaky


def test_删项目会连字幕一起清掉(as_role, stage):  # noqa: F811
    """字幕不在任何 Job 名下，删项目时不会被"整目录摘作业"顺带带走。

    漏了这一步，`data/jobs/subtitles/<删掉的项目>/` 会永远留在盘上：
    没人看得见它，但每次备份都会多背一份。
    """
    import jobfiles
    import services

    pid = stage["project_id"]
    up = _upload(as_role("editor"), pid, "留下.srt", GOOD_SRT.encode("utf-8"))
    assert up.status_code == 201
    target = jobfiles.subtitle_dir(pid)
    assert target.is_dir() and list(target.iterdir())

    services.delete_project_cascade(pid)
    assert not target.exists()
