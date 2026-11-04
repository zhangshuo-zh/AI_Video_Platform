"""项目中心的服务端口径（v0.84 那一轮补的）。

这份测试盯的是三件"界面会替人撒谎"的地方：
1. 归档／恢复谁能做——它改变别人眼前看到的是哪部剧，不该跟着 write 走；
2. 批量加标签没加上时要不要说——只回 done，界面就会把没变化的也报成成功；
3. 列表要不要带文档与定妆图计数——不带，批量删除清点就得逐个打 /summary。
"""
import uuid

import pytest

from db import get_conn
from repositories.projects import projects as prepo
from repositories.users import sessions as session_repo
from repositories.users import users as urepo
from security import hash_password, new_session_token


def _mate() -> int:
    """一个同级 editor，用来当协作人。"""
    return urepo.create(f"p_mate_{uuid.uuid4().hex[:6]}", "协作人",
                        hash_password("x"), role_codes=["editor"])["id"]


def _as(user_id: int):
    """给任意账号铸一个会话客户端——协作人不在 conftest 的角色表里。"""
    import main
    from datetime import datetime, timedelta
    from fastapi.testclient import TestClient
    token, digest = new_session_token()
    session_repo.create(digest, user_id,
                        (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))
    return TestClient(main.app, cookies={"fv_session": token})


def _owned_editor(app, people) -> tuple:
    """editor 自己名下的项目 ＋ 一个客户端。"""
    pid = prepo.create(f"项目口径_{uuid.uuid4().hex[:6]}", "介绍", [],
                       people["editor"]["id"])["id"]
    return pid


def test_列表带文档与定妆图计数(app, as_role, seeded):
    """批量删除的清点要靠这两个数，不能再逐个打 /summary。"""
    rows = as_role("editor").get("/api/projects").json()
    mine = [r for r in rows if r["id"] == seeded["project_id"]]
    assert mine, "seeded 的项目没出现在它所有人的列表里"
    # seeded 建了一份文档、一条没有参考图的资产
    assert mine[0]["document_count"] == 1, mine[0]
    assert mine[0]["image_count"] == 0, mine[0]


def test_归档与恢复只有所有人或管理员能做(app, people, as_role):
    """v0.84 起这一件事是 own 级，不再跟着 project:write 走。"""
    pid = _owned_editor(app, people)
    mate = _mate()
    owner = as_role("editor")
    assert owner.post(f"/api/projects/{pid}/members", json={"user_id": mate}).status_code == 200

    mc = _as(mate)
    r = mc.request("PATCH", f"/api/projects/{pid}/status", json={"status": "archived"})
    assert r.status_code == 403, f"协作人不该能把别人的项目收起来，实得 {r.status_code}"
    assert "归档" in r.json()["detail"], r.json()

    assert owner.request("PATCH", f"/api/projects/{pid}/status",
                         json={"status": "archived"}).status_code == 200, "所有人自己归档要能成"
    assert as_role("admin").request("PATCH", f"/api/projects/{pid}/status",
                                    json={"status": "active"}).status_code == 200, "管理员放行"


def test_归档与打开一样不推进_updated_at(app, people, as_role):
    pid = _owned_editor(app, people)
    c = as_role("editor")
    with get_conn() as conn:
        conn.execute("UPDATE project SET updated_at='2020-01-01 00:00:00' WHERE id=?", (pid,))
    assert c.request("PATCH", f"/api/projects/{pid}/status",
                     json={"status": "archived"}).status_code == 200
    with get_conn() as conn:
        after = conn.execute("SELECT updated_at FROM project WHERE id=?", (pid,)).fetchone()["updated_at"]
    assert after == "2020-01-01 00:00:00", f"归档不该推「最后修改」，实得 {after}"


def test_批量归档把没权限的那几项单独报出来(app, people, as_role):
    pid = _owned_editor(app, people)
    mate = _mate()
    owner = as_role("editor")
    owner.post(f"/api/projects/{pid}/members", json={"user_id": mate})
    mc = _as(mate)
    r = mc.post("/api/projects/batch", json={"ids": [pid], "action": "archive", "tags": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["done"] == [], f"协作人不该归档成功：{body}"
    assert body["skipped"] and "归档" in body["skipped"][0]["reason"], body

    r2 = owner.post("/api/projects/batch", json={"ids": [pid], "action": "archive", "tags": []})
    assert r2.json()["done"] == [pid] and r2.json()["skipped"] == [], r2.json()


def test_批量加标签在标签已满时说实话(app, people, as_role):
    """这是这一轮的核心：接口以前只回 done，界面据此报"已加标签 1 个项目"。"""
    pid = _owned_editor(app, people)
    c = as_role("editor")
    full = [f"满{ i }" for i in range(10)]
    assert c.put(f"/api/projects/{pid}/tags", json={"tags": full}).status_code == 200

    r = c.post("/api/projects/batch",
               json={"ids": [pid], "action": "tag", "tags": ["再加一个"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["done"] == [pid], "写操作本身没失败，不该进 skipped"
    assert len(body["notes"]) == 1 and "标签已满 10 个" in body["notes"][0]["note"], body
    assert "再加一个" in body["notes"][0]["note"], body
    got = [p for p in c.get("/api/projects").json() if p["id"] == pid][0]
    assert got["tags"] == full, f"一个都没加上才对，实得 {got['tags']}"


def test_批量加标签正常追加时不多话(app, people, as_role):
    pid = _owned_editor(app, people)
    c = as_role("editor")
    r = c.post("/api/projects/batch", json={"ids": [pid], "action": "tag", "tags": ["新标签"]})
    assert r.json()["notes"] == [], r.json()
    got = [p for p in c.get("/api/projects").json() if p["id"] == pid][0]
    assert got["tags"] == ["新标签"], got["tags"]


def test_标签装得下一半时报清楚加了哪个(app, people, as_role):
    pid = _owned_editor(app, people)
    c = as_role("editor")
    c.put(f"/api/projects/{pid}/tags", json={"tags": [f"占{i}" for i in range(9)]})
    r = c.post("/api/projects/batch",
               json={"ids": [pid], "action": "tag", "tags": ["挤进来", "挤不进"]})
    note = r.json()["notes"][0]["note"]
    assert "只加上 挤进来" in note and "未追加：挤不进" in note, note


def test_未知项目状态筛选直接报错而不是列出全部(people):
    """`list_visible(status=...)` 原来把认不出来的值兜成 `1`（＝查看全部）。

    路由层有 Literal 挡着，所以 HTTP 上碰不到；但 worker 与脚本是直接从这一层调的，
    一个拼错的参数会静默变成"没筛"——调用方以为筛过了，实际什么都没筛，
    这是最难被发现的一类错。宁可当场抛。
    """
    uid = people["editor"]["id"]
    # 三个合法值照旧能用
    for status in ("all", "active", "archived"):
        prepo.list_visible(uid, False, status)
    with pytest.raises(ValueError) as bad:
        prepo.list_visible(uid, False, "archved")     # 少一个 r
    assert "archved" in str(bad.value)
