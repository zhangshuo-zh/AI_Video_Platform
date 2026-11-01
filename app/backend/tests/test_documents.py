"""W05 文档层：正文落盘、Git 版本、编辑锁、冲突检测、路径安全。

这一组测试是 W05 验收项的逐条对照，四条最贵的都在：
- 一次正文编辑能生成一次 Git commit（`test_保存正文会生成新提交`）
- 第二个编辑者不会静默覆盖（`test_版本不对回_409` + 带外修改那两条）
- 非法路径不可读写（`test_库里躺着越界路径` 等三条）
- Git 失败时能恢复或保留未应用结果（`test_Git_失败时退回上一版并另存未应用结果`）

为什么大量断言要看磁盘和 Git 历史，而不是只看 HTTP 状态码：
这套设计的核心主张是"库和文件系统各管一半、靠 git_commit 钉在一起"。
只测接口返回值的话，"库里说保存成功、工作区里根本没这个文件"这种最要命的
不一致是测不出来的。
"""
import json
import uuid

import pytest

import gitrepo
import services
import workspace
from config import WORKSPACE_DIR
from db import get_conn


def _create(client, project_id: int, title: str = None, kind: str = "novel",
            episode_id=None, content: str = "燧人蹲在洞口。") -> dict:
    r = client.post(f"/api/projects/{project_id}/documents",
                    json={"kind": kind, "title": title or f"稿_{uuid.uuid4().hex[:6]}",
                          "episode_id": episode_id, "content": content})
    assert r.status_code == 201, r.text
    return r.json()


def _token(client, doc_id: int) -> str:
    r = client.get(f"/api/documents/{doc_id}/content")
    assert r.status_code == 200, r.text
    return r.json()["version"]


# ---------- 建与读 ----------

def test_建文档会同时落下文件_提交和元数据(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, title="第一稿", content="燧人钻木。")

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM document WHERE id = ?", (doc["id"],)).fetchone()
    assert row is not None, "接口说建好了，库里却没有行"
    assert row["project_id"] == pid and row["kind"] == "novel"
    # 文件真的在工作区里，且就在它自己那一层：路径前缀是 p<项目id>/<kind>/
    target = WORKSPACE_DIR / row["rel_path"]
    assert target.is_file(), f"正文文件不在：{target}"
    assert target.read_text(encoding="utf-8") == "燧人钻木。\n"   # 结尾换行由服务层补，见 _tend
    assert str(target.resolve()).startswith(str(WORKSPACE_DIR.resolve()))
    # Git 里有这一次提交，元数据钉的就是它
    assert row["git_commit"], "新建文档没有提交号，版本这一栏后面全对不上"
    assert [h["subject"] for h in gitrepo.history(row["rel_path"])] == ["新建 novel：第一稿"]


def test_列表只回元数据不回正文(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    _create(c, pid, content="这一句不该出现在列表里。")
    rows = c.get(f"/api/projects/{pid}/documents").json()
    assert len(rows) == 1
    assert "content" not in json.dumps(rows), "列表把正文带出来了（规格 02 §9 只要元数据）"
    assert {"id", "kind", "title", "rel_path", "status", "git_commit"} <= set(rows[0])


def test_正文丢了报_409_而不是_404(as_role, fresh_project):
    """库里有、盘上没有——两套事实来源对不上，得说清楚是哪一套没了。

    用 404 会被前端解释成"不在你的可见范围内"（util.whyHint 那条），
    把人往权限方向带，而真正要做的是去工作区把文件找回来。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid)
    (WORKSPACE_DIR / doc["rel_path"]).unlink()
    r = c.get(f"/api/documents/{doc['id']}/content")
    assert r.status_code == 409, r.text
    assert "找不到" in r.json()["detail"]


def test_标题重名会排号不报错(as_role, fresh_project):
    """两个人给不同章节起同一个副标题是常事，路径自动避让；title 列照样各自保留。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    a = _create(c, pid, title="取火")
    b = _create(c, pid, title="取火")
    assert a["rel_path"] != b["rel_path"], "同路径会互相覆盖，第二份稿子等于没建"
    assert b["rel_path"].endswith("取火-2.md"), b["rel_path"]


def test_删掉再同名重建会排号而不是接上旧历史(as_role, fresh_project):
    """路径不只看"库里有没有行"，还要看"Git 历史上有没有过这个文件"。

    只看库的话，删掉《取火》再建同名的一篇会占回同一个路径，
    新文档的版本面板里就混着上一篇的"新建／删除"提交——读的人会以为自己漏看了什么。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    first = _create(c, pid, title="回头再写", content="第一版。")
    assert c.request("DELETE", f"/api/documents/{first['id']}").status_code == 204
    again = _create(c, pid, title="回头再写", content="重新开始。")
    assert again["rel_path"] != first["rel_path"], "同名重建占了同一个文件，历史会串"
    assert again["rel_path"].endswith("回头再写-2.md"), again["rel_path"]
    hist = c.get(f"/api/documents/{again['id']}/versions").json()
    assert len(hist) == 1, [h["subject"] for h in hist]


# ---------- 保存与版本 ----------

def test_保存正文会生成新提交(as_role, people, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, content="燧人蹲在洞口。")
    tok = _token(c, doc["id"])

    r = c.request("PUT", f"/api/documents/{doc['id']}/content",
                  json={"content": "燧人站起来了。", "expected_version": tok})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert (WORKSPACE_DIR / doc["rel_path"]).read_text(encoding="utf-8") == "燧人站起来了。\n"

    with get_conn() as conn:
        row = conn.execute("SELECT git_commit FROM document WHERE id = ?",
                           (doc["id"],)).fetchone()
    assert row["git_commit"] == body["git_commit"]
    hist = c.get(f"/api/documents/{doc['id']}/versions").json()
    assert len(hist) == 2, hist
    # 署名是**改的人**，不是"平台"：查历史时要能回答"这版谁写的"
    assert hist[0]["author"] == people["editor"]["name"]


def test_同样内容重复保存不攒空提交(as_role, fresh_project):
    """每次点保存都造一个"什么都没改"的提交，版本列表会被垃圾填满、diff 全空。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, content="同一句。")
    tok = _token(c, doc["id"])
    r = c.request("PUT", f"/api/documents/{doc['id']}/content",
                  json={"content": "同一句。", "expected_version": tok}).json()
    assert r["changed"] is False, "内容没变却报了新提交"
    assert len(c.get(f"/api/documents/{doc['id']}/versions").json()) == 1


def test_没改内容再点保存不推改动时间(as_role, fresh_project):
    """`updated_at` 的语义是"内容动过"（与 v0.62 归档、打开同一条口径）。

    点一下保存、文本一个字没变，却把时间推到当前，界面上那行"改动"就跳了——
    人看着以为存了什么新东西。这里用 2020 哨兵值断言它没被推（同一秒内相等会假绿）。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, content="一句。")
    tok = _token(c, doc["id"])
    with get_conn() as conn:
        conn.execute("UPDATE document SET updated_at = '2020-01-01 00:00:00' WHERE id = ?",
                     (doc["id"],))
    r = c.request("PUT", f"/api/documents/{doc['id']}/content",
                  json={"content": "一句。", "expected_version": tok}).json()
    assert r["changed"] is False
    assert r["updated_at"] == "2020-01-01 00:00:00", "没改内容却把改动时间推走了"
    assert r["git_commit"] == doc["git_commit"]


def test_保存会带回改动时间(as_role, fresh_project):
    """前端靠这个字段把底部那行"改动"当场换掉，不回传就只能显示旧时间。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, content="一句。")
    tok = _token(c, doc["id"])
    r = c.request("PUT", f"/api/documents/{doc['id']}/content",
                  json={"content": "改过的另一句。", "expected_version": tok}).json()
    assert r["changed"] is True
    assert r["updated_at"] and r["updated_at"] != "2020-01-01 00:00:00"


def test_正文补结尾换行且不会因此一直报冲突(as_role, fresh_project):
    """规范化成一条规则测：不补换行的话 diff 末尾永远挂着
    `\\ No newline at end of file`，而且下一次在末尾追加一句会把上一行也标成改动。

    另一半更要紧：编辑器里的文本没有那个换行，盘上有 ⇒ 若令牌按客户端那份算，
    每次保存都会自己和自己冲突。这里连着存两次，第二次必须还是 200。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, content="没有换行结尾的一句")
    assert (WORKSPACE_DIR / doc["rel_path"]).read_text(encoding="utf-8").endswith("\n")

    tok = _token(c, doc["id"])
    r1 = c.request("PUT", f"/api/documents/{doc['id']}/content",
                   json={"content": "没有换行结尾的一句", "expected_version": tok})
    assert r1.status_code == 200, r1.text
    r2 = c.request("PUT", f"/api/documents/{doc['id']}/content",
                   json={"content": "没有换行结尾的一句", "expected_version": r1.json()["version"]})
    assert r2.status_code == 200, f"同一份内容第二次保存自己和自己冲突：{r2.text}"
    assert r2.json()["changed"] is False


def test_空正文建出来的就是空文件(as_role, fresh_project):
    """刚建好的一章一个字都没写，不该被"补换行"这条规则塞进一个字符。

    塞了以后编辑器上的字数显示成 1，读起来像平台替人写了一个字；
    而且空稿连存两次也要稳定（令牌按空串算，两边一致）。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, title="空章节", content="")
    target = WORKSPACE_DIR / doc["rel_path"]
    assert target.read_bytes() == b"", "空稿被塞了一个换行"
    tok = _token(c, doc["id"])
    assert tok == services.version_token("")
    again = c.request("PUT", f"/api/documents/{doc['id']}/content",
                      json={"content": "", "expected_version": tok})
    assert again.status_code == 200, again.text
    assert again.json()["changed"] is False


def test_版本不对回_409_且盘上不动(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, content="原版。")
    before = (WORKSPACE_DIR / doc["rel_path"]).read_text(encoding="utf-8")

    r = c.request("PUT", f"/api/documents/{doc['id']}/content",
                  json={"content": "覆盖版。", "expected_version": "sha256:我瞎写的"})
    assert r.status_code == 409, r.text
    assert "变了" in r.json()["detail"]
    assert (WORKSPACE_DIR / doc["rel_path"]).read_text(encoding="utf-8") == before, \
        "冲突了还把人家写的盖下去了"


def test_不带版本一律不收(as_role, fresh_project):
    """没有"最后一次写入覆盖前一次"这条便道——W05 验收项要的就是这条。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid)
    r = c.request("PUT", f"/api/documents/{doc['id']}/content", json={"content": "抢写"})
    assert r.status_code == 422, r.text


def test_编辑器外面改过的正文也能检出(as_role, fresh_project):
    """版本令牌是正文内容的哈希，不是 commit 号。

    这套设计里正文本来就允许手工编辑（设计稿 §3.3：稿子留在文件系统并走 git）。
    那种改动不会前进 commit 号，拿 commit 当令牌就检不出冲突——
    而"第二个编辑者不会静默覆盖"要的恰恰是这种情况。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, content="我在 Terminal 里改。）")
    tok = _token(c, doc["id"])
    # 绕过平台，直接改盘上的文件，再提交一次（模拟有人在外面写完又自己 commit）
    workspace.atomic_write(doc["rel_path"], "外面改的一句话。")
    gitrepo.commit_file(doc["rel_path"], "手工改的", "外部编辑")

    r = c.request("PUT", f"/api/documents/{doc['id']}/content",
                  json={"content": "平台上再写。", "expected_version": tok})
    assert r.status_code == 409, "带外修改没检出，等于静默覆盖"
    assert (WORKSPACE_DIR / doc["rel_path"]).read_text(encoding="utf-8") == "外面改的一句话。"


def test_看某一版的差异(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, content="第一行。")
    hist = c.get(f"/api/documents/{doc['id']}/versions").json()
    diff = c.get(f"/api/documents/{doc['id']}/versions/{hist[0]['commit']}/diff")
    assert diff.status_code == 200, diff.text
    assert "第一行。" in diff.json()["diff"]


def test_非法_commit_号被挡在_git_参数之外(as_role, fresh_project):
    """这一位会进 git 的命令行。`--help`、`-p` 这类字符串传进去命令行为就变了。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid)
    for bad in ["--help", "-p", "HEAD", "notacommit", "$(whoami)", "0"]:
        r = c.get(f"/api/documents/{doc['id']}/versions/{bad}/diff")
        assert r.status_code == 422, f"{bad} 没被挡住：{r.status_code} {r.text[:80]}"
    # 格式对但仓库里没有这个号：git 自己会报错，界面要拿到一句能懂的话（404），
    # 而不是把 git 的 stderr 原样摊成 500
    ghost = c.get(f"/api/documents/{doc['id']}/versions/{'0' * 40}/diff")
    assert ghost.status_code == 404, ghost.text


def test_Git_失败时退回上一版并另存未应用结果(as_role, fresh_project, monkeypatch):
    """规格 02 §6.1：提交失败要么回滚、要么保留未应用的结果，不能静默显示定稿。

    这里两条一起做：盘上退回刚才那一份（库和文件重新对齐），
    人这次写的东西另存到工作区外面（不用重敲）。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, content="原来的 good text")
    tok = _token(c, doc["id"])

    def boom(*a, **kw):
        raise gitrepo.GitError("假装 git 挂了")

    monkeypatch.setattr(gitrepo, "commit_file", boom)
    r = c.request("PUT", f"/api/documents/{doc['id']}/content",
                  json={"content": "这次写的不该丢", "expected_version": tok})
    assert r.status_code == 500, r.text
    assert (WORKSPACE_DIR / doc["rel_path"]).read_text(
        encoding="utf-8") == "原来的 good text\n", \
        "提交失败了却没回滚，工作区比库新，下一次保存必然 409"
    kept = list((WORKSPACE_DIR.parent / "unapplied").glob(f"doc{doc['id']}-*.md"))
    assert kept, f"没提交上去的正文没留底：{r.json()['detail'][:60]}"
    assert kept[0].read_text(encoding="utf-8") == "这次写的不该丢\n"   # 留的是规范化之后那份
    assert "另存在" in r.json()["detail"], "错误消息要把底存哪儿了告诉人"


def test_正文超大回_413(as_role, fresh_project, monkeypatch):
    from config import DOC_MAX_BYTES

    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid)
    tok = _token(c, doc["id"])
    r = c.request("PUT", f"/api/documents/{doc['id']}/content",
                  json={"content": "字" * (DOC_MAX_BYTES // 3 + 1), "expected_version": tok})
    assert r.status_code == 413, r.text      # 一个汉字 3 字节，过 1MB 就拦


# ---------- 编辑锁 ----------

def test_两个人抢同一份锁(as_role, fresh_project):
    """A 拿着锁时 B：拿→held、写→423；A 自己再拿是续期，不是报错。"""
    a = as_role("editor")
    b = as_role("admin")
    pid = fresh_project("editor")
    doc = _create(a, pid)

    assert a.post(f"/api/documents/{doc['id']}/lock", json={}).json()["state"] == "acquired"
    again = a.post(f"/api/documents/{doc['id']}/lock", json={}).json()
    assert again["state"] == "renewed", again
    held = b.post(f"/api/documents/{doc['id']}/lock", json={}).json()
    assert held["state"] == "held", "别人拿着锁时不该报成功，也不该 500——这是要显示的状态"
    assert held["holder_name"], "held 时得说出是谁拿着，界面才能显示'谁谁正在编辑'"

    tok = _token(a, doc["id"])
    r = b.request("PUT", f"/api/documents/{doc['id']}/content",
                  json={"content": "B 抢写", "expected_version": tok})
    assert r.status_code == 423, f"锁没拦住写：{r.status_code} {r.text[:80]}"
    assert (WORKSPACE_DIR / doc["rel_path"]).read_text(encoding="utf-8") != "B 抢写"

    assert a.request("DELETE", f"/api/documents/{doc['id']}/lock").json()["state"] == "released"
    assert b.request("PUT", f"/api/documents/{doc['id']}/content",
                     json={"content": "B 现在能写了", "expected_version": tok}
                     ).status_code == 200


def test_只有持锁人能放锁(as_role, people, fresh_project):
    a, b = as_role("editor"), as_role("admin")
    pid = fresh_project("editor")
    doc = _create(a, pid)
    mine = a.post(f"/api/documents/{doc['id']}/lock", json={}).json()
    assert mine["state"] == "acquired"
    # B 想替 A 放锁：放不掉，但也不报错（他确实没拿着），A 的锁原样还在
    assert b.request("DELETE", f"/api/documents/{doc['id']}/lock").json()["state"] == "not_held"
    still = a.get(f"/api/documents/{doc['id']}").json()
    assert still["lock_holder_id"] == people["editor"]["id"], "别人放了一下，A 的锁却没了"
    assert still["lock_holder_name"] == people["editor"]["name"], still


def test_过期锁自动回收(as_role, fresh_project):
    """不靠定时任务：过期行由下一次 acquire 直接覆盖（仓储层注释里说了为什么不建 daemon）。"""
    a, b = as_role("editor"), as_role("admin")
    pid = fresh_project("editor")
    doc = _create(a, pid)
    a.post(f"/api/documents/{doc['id']}/lock", json={})
    with get_conn() as conn:
        conn.execute("UPDATE doc_lock SET expires_at = datetime('now','localtime','-30 minutes')"
                     " WHERE doc_id = ?", (doc["id"],))
    # 过期后 lock_of 查不到 → 界面上显示"没人在编辑"，不是"某某锁住了"
    assert a.get(f"/api/documents/{doc['id']}").json()["lock_holder_id"] is None
    assert b.post(f"/api/documents/{doc['id']}/lock", json={}).json()["state"] == "acquired"


def test_保存会顺带续期锁(as_role, fresh_project):
    """规格 02 §6.1「心跳/续租或在保存时延长」里选了延长那一支：前端不用定闹钟。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid)
    c.post(f"/api/documents/{doc['id']}/lock", json={})
    # 先把到期时间压到 1 分钟后，否则"续期"和"没续"在同一秒里比不出来
    with get_conn() as conn:
        conn.execute("UPDATE doc_lock SET expires_at = datetime('now','localtime','+1 minute')"
                     " WHERE doc_id = ?", (doc["id"],))
    before = c.get(f"/api/documents/{doc['id']}").json()["lock_expires_at"]
    tok = _token(c, doc["id"])
    c.request("PUT", f"/api/documents/{doc['id']}/content",
              json={"content": "写一句新的", "expected_version": tok})
    after = c.get(f"/api/documents/{doc['id']}").json()["lock_expires_at"]
    assert after > before, f"保存没有把锁推后（{before} → {after}）：写十分钟的人会中途丢锁"


def test_锁到点自动失效不挡住后来人(as_role, fresh_project):
    """锁是协作提示，不是分布式锁：到期就当我没拿，不需要谁来释放。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid)
    c.post(f"/api/documents/{doc['id']}/lock", json={})
    with get_conn() as conn:
        conn.execute("UPDATE doc_lock SET expires_at = datetime('now','localtime','-1 second')"
                     " WHERE doc_id = ?", (doc["id"],))
    tok = _token(c, doc["id"])
    r = c.request("PUT", f"/api/documents/{doc['id']}/content",
                  json={"content": "过期后自己还能写", "expected_version": tok})
    assert r.status_code == 200, r.text


# ---------- 路径安全 ----------

def test_库里躺着越界路径时读写都拒绝(as_role, fresh_project):
    """正常输入走不到这一步（路径是服务端生成的），防的是脏数据与将来某个传路径的接口。

    这条是 W05 验收项里"非法路径不可读写"的直接对照：
    即便元数据表里已经被写进 `../../`，也不能让人读到工作区外面的文件。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid)
    outside = WORKSPACE_DIR.parent / "outside.md"
    outside.write_text("不该被平台读到的一句话", encoding="utf-8")
    with get_conn() as conn:
        conn.execute("UPDATE document SET rel_path = ? WHERE id = ?",
                     ("../outside.md", doc["id"]))

    r = c.get(f"/api/documents/{doc['id']}/content")
    assert r.status_code in (409, 422), f"越界路径竟然读到了：{r.status_code} {r.text[:80]}"
    assert "不该被平台读到" not in r.text
    tok = "sha256:whatever"
    w = c.request("PUT", f"/api/documents/{doc['id']}/content",
                  json={"content": "写出去", "expected_version": tok})
    assert w.status_code in (409, 422), w.text
    assert outside.read_text(encoding="utf-8") == "不该被平台读到的一句话", "写到根目录外面去了"


@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "/abs/path/a.md", ".git/config", "p1/novel/../../x.md",
    "p1\\novel\\a.md", "p1/novel/a.txt", "p1//a.md", "", "p1/novel/ab.md\x00",
])
def test_非法路径一律拒收(bad):
    with pytest.raises(workspace.PathError):
        workspace.normalize(bad)


def test_合法路径通过并归一():
    """`./` 前缀不放过：宁可让服务端自己生成的路径通过，也不给"看起来差不多"的输入开口子。"""
    assert workspace.normalize("p1/novel/a.md") == "p1/novel/a.md"
    assert workspace.normalize("p1/novel/中文名.md") == "p1/novel/中文名.md"
    assert workspace.normalize("p1/script/第1集/场景.md") == "p1/script/第1集/场景.md"
    with pytest.raises(workspace.PathError):
        workspace.normalize("./p1/novel/a.md")


def test_slug_与路径生成():
    """标题里什么都有：斜杠、井号、emoji、空格。文件名只留最保守的字符集。"""
    assert workspace.slugify("第一集：取火/改天 a#b") == "第一集-取火-改天-a-b"
    assert workspace.slugify("///") == "doc"
    hits = {"n": 0}

    def taken(_path):
        # 前两条路径都被占了 ⇒ 第三个 candidate（`-3`）才是能用的
        hits["n"] += 1
        return hits["n"] <= 2

    assert workspace.plan_rel_path(7, "script", "试镜", taken).endswith("试镜-3.md")


# ---------- 权限与状态 ----------

def test_只读角色能看不能改(as_role, people, fresh_project):
    """viewer 有 doc:read、没有 doc:write ⇒ 看得见项目时列表 200、写一律 403。

    两种拒法要分清：这里是"权限点不够"（403），
    outsider 那种是"项目根本看不见"（404），见 test_guards。
    """
    owner = as_role("editor")
    pid = fresh_project("editor")
    owner.post(f"/api/projects/{pid}/members", json={"user_id": people["viewer"]["id"]})
    doc = _create(owner, pid)

    v = as_role("viewer")
    rows = v.get(f"/api/projects/{pid}/documents")
    assert rows.status_code == 200, rows.text
    assert len(rows.json()) == 1
    assert v.get(f"/api/documents/{doc['id']}/content").status_code == 200   # 读正文可以
    assert v.post(f"/api/projects/{pid}/documents",
                  json={"kind": "novel", "title": "试"}).status_code == 403
    tok = _token(owner, doc["id"])
    assert v.request("PUT", f"/api/documents/{doc['id']}/content",
                     json={"content": "改一句", "expected_version": tok}).status_code == 403
    # 拿锁也是写动作：只读角色不能靠"占住锁"把别人挡在外面
    assert v.post(f"/api/documents/{doc['id']}/lock", json={}).status_code == 403


def test_跨项目挂靠被拒(as_role, fresh_project):
    c = as_role("editor")
    a, b = fresh_project("editor"), fresh_project("editor")
    other_ep = c.post(f"/api/projects/{b}/episodes", json={"title": "别的项目的集"}).json()
    r = c.post(f"/api/projects/{a}/documents",
               json={"kind": "novel", "title": "错位", "episode_id": other_ep["id"]})
    assert r.status_code == 422, r.text
    doc = _create(c, a)
    assert c.request("PUT", f"/api/documents/{doc['id']}",
                     json={"title": "改名", "episode_id": other_ep["id"]}
                     ).status_code == 422


def test_状态三档与白名单(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid)
    for status in ("draft", "polishing", "finalized"):
        r = c.request("PATCH", f"/api/documents/{doc['id']}/status", json={"status": status})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == status
    # Literal 之外的值由 FastAPI 挡在门外，不会写进那个 CHECK
    assert c.request("PATCH", f"/api/documents/{doc['id']}/status",
                     json={"status": "done"}).status_code == 422


def test_删文档后文件从工作区摘掉但历史还在(as_role, fresh_project):
    """删一刀是可回退的：git rm 是一次提交，那一稿还在历史里。

    所以删除只需要 doc:write，不需要项目所有人级别（routers/documents.py 里说了取舍）。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    doc = _create(c, pid, content="删掉的那一句")
    rel = doc["rel_path"]
    assert c.request("DELETE", f"/api/documents/{doc['id']}").status_code == 204
    assert not (WORKSPACE_DIR / rel).exists()
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM document WHERE id = ?", (doc["id"],)).fetchone() is None
    # 内容还在对象的历史里，`git log -- <path>` 查得到那条删除记录
    assert _run_in_repo(["log", "--oneline", "--", rel]).count("\n") >= 2


def _run_in_repo(args: list[str]) -> str:
    """只读地跑一条 git，用于断言历史确实记着东西。参数是列表，不拼 shell。"""
    import subprocess

    proc = subprocess.run(["git", *args], cwd=str(WORKSPACE_DIR),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_删项目带走文档元数据与文件(as_role, fresh_project):
    """项目删除只清理登记过的路径：`document` 表里那几条 rel_path 就是授权依据。

    正文走 git rm 提交，所以历史里还找得回；锁行跟着外键一起清。
    """
    from repositories.projects import projects as project_repo

    # 用 admin：删项目要的 `project:delete` 权限点 editor 没有（editor 只有 read/write），
    # 这一条测的是"删项目带走什么"，不是"谁能删项目"（后者在 test_guards 里）
    c = as_role("admin")
    pid = fresh_project("editor")
    doc = _create(c, pid, content="跟项目一起走")
    rel = doc["rel_path"]
    c.post(f"/api/documents/{doc['id']}/lock", json={})
    summary = c.get(f"/api/projects/{pid}/summary").json()
    assert summary["documents"] == 1, "删除前的清点里没算文档，人会以为稿子不受影响"

    name = project_repo.get(pid)["name"]
    r = c.request("DELETE", f"/api/projects/{pid}", params={"confirm": name})
    assert r.status_code == 204, r.text
    assert not (WORKSPACE_DIR / rel).exists()
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM document WHERE id = ?",
                            (doc["id"],)).fetchone() is None
        assert conn.execute("SELECT 1 FROM doc_lock WHERE doc_id = ?",
                            (doc["id"],)).fetchone() is None, "项目没了，锁还挂着"
    # 历史里那条删除记录还在：那一稿没真的从世界上消失
    assert _run_in_repo(["log", "--oneline", "--", rel]).strip()
