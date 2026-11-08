"""所有权转移（W03 补的那块）。

这块之所以值得单独一份测试：它改的是"谁说了算"，出错的方式不是报错而是
悄悄把项目交出去或者把人锁在门外。所以除了成功路径，重点验三件事——
**谁能发起、能交给谁、发起之后自己还剩什么**。
"""
import uuid

import pytest

from db import get_conn
from repositories.projects import projects as prepo
from repositories.users import users as urepo
from security import hash_password


def _user(role='editor'):
    """建一个测试账号。停用要走 set_active，create 没有这个参数。"""
    return urepo.create(f"t_{role}_{uuid.uuid4().hex[:6]}", f"{role}测试",
                        hash_password('x'), role_codes=[role])


@pytest.fixture
def proj_with_member(app, as_role, people):
    """一个项目 + 一个已加入的协作人，转移的正例就发生在这两者之间。"""
    owner = people['editor']['id']
    pid = prepo.create(f"转移测试_{uuid.uuid4().hex[:6]}", "", [], owner)['id']
    mate = _user('editor')
    c = as_role('editor')
    c.post(f"/api/projects/{pid}/members", json={"user_id": mate['id']})
    return {"pid": pid, "owner": owner, "mate": mate['id'], "client": c}


def test_所有人可转给协作人且自己降为协作人(proj_with_member, as_role):
    pid, owner, mate, c = proj_with_member['pid'], proj_with_member['owner'], \
        proj_with_member['mate'], proj_with_member['client']
    # 先把 updated_at 设成哨兵值：直接比"前后相等"会在同一秒内侥幸通过，
    # 浏览器里隔了 5 秒才露馅——那种断言等于没断言
    with get_conn() as conn:
        conn.execute("UPDATE project SET updated_at='2020-01-01 00:00:00' WHERE id=?", (pid,))
    r = c.request("POST", f"/api/projects/{pid}/transfer", json={"user_id": mate})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owner_id"] == mate, "所有权没换人"
    with get_conn() as conn:
        after = conn.execute("SELECT updated_at FROM project WHERE id=?", (pid,)).fetchone()["updated_at"]
        members = {m['user_id'] for m in conn.execute(
            "SELECT user_id FROM project_member WHERE project_id=?", (pid,))}
    assert after == '2020-01-01 00:00:00', f"转移不该推 updated_at，实得 {after}"
    assert owner in members, "原所有人应留在协作人名单里，别把他锁在门外"
    assert mate not in members, "接手人已经是所有人，不该还挂在协作人名单上"
    # 交接后原所有人还能看见这个项目（协作人级别），列表里不该消失
    ids = {p['id'] for p in c.get('/api/projects').json()}
    assert pid in ids


def test_可以交给还没加入的账号(proj_with_member):
    """v0.81 改口：不再要求先加为协作人。

    原来那条门槛让「成员」弹窗在没有协作人时整行都不出现，人以为没这功能；
    而交接这一步本身就把对方变成所有人，"先加进来"并没有多守住什么。
    """
    c, pid, owner = proj_with_member['client'], proj_with_member['pid'], proj_with_member['owner']
    stranger = _user('editor')
    r = c.request("POST", f"/api/projects/{pid}/transfer", json={"user_id": stranger['id']})
    assert r.status_code == 200, r.text
    assert r.json()["owner_id"] == stranger['id'], "交接没换成新所有人"
    with get_conn() as conn:
        members = {m['user_id'] for m in conn.execute(
            "SELECT user_id FROM project_member WHERE project_id=?", (pid,))}
    assert owner in members, "原所有人要留在协作人名单里"
    assert stranger['id'] not in members, "接手人已是所有人，不该还挂在协作人名单上"


def test_不能交给层级高于自己的账号(proj_with_member):
    """层级这条不放宽：制作把项目交给超管后自己降为协作人，就再也拿不回来了。"""
    c, pid = proj_with_member['client'], proj_with_member['pid']
    boss = _user('admin')
    r = c.request("POST", f"/api/projects/{pid}/transfer", json={"user_id": boss['id']})
    assert r.status_code == 422, r.text
    assert "层级" in r.json()["detail"], r.json()


def test_转给已经是所有人的对象要拒(proj_with_member):
    c, pid, owner = proj_with_member['client'], proj_with_member['pid'], proj_with_member['owner']
    r = c.request("POST", f"/api/projects/{pid}/transfer", json={"user_id": owner})
    assert r.status_code == 422 and "已经是" in r.json()["detail"]


def test_接手人不存在或停用要挡(proj_with_member, app):
    c, pid = proj_with_member['client'], proj_with_member['pid']
    assert c.request("POST", f"/api/projects/{pid}/transfer",
                     json={"user_id": 999999}).status_code == 404
    off = urepo.create(f"off_{uuid.uuid4().hex[:6]}", "停用的人", hash_password('x'),
                       role_codes=['editor'])
    c.post(f"/api/projects/{pid}/members", json={"user_id": off['id']})
    urepo.set_active(off['id'], False)
    r = c.request("POST", f"/api/projects/{pid}/transfer", json={"user_id": off['id']})
    assert r.status_code == 404, f"停用的账号不该能接手项目，实得 {r.status_code}"


def test_协作人不能把项目转走(as_role, people):
    """ensure(..., 'manage')：协作人可改可看，但归属不是他能动的。"""
    pid = prepo.create(f"别人的_{uuid.uuid4().hex[:6]}", "", [],
                       people['editor']['id'])['id']
    mate = _user('editor')
    as_role('editor').post(f"/api/projects/{pid}/members", json={"user_id": mate['id']})
    tok = mate['id']
    # 用协作人自己的身份打：先给他铸一个会话
    from datetime import datetime, timedelta
    from repositories.users import sessions
    from security import new_session_token
    t, h = new_session_token()
    sessions.create(h, tok, (datetime.now() + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S'))
    from fastapi.testclient import TestClient
    import main
    mc = TestClient(main.app, cookies={"fv_session": t})
    r = mc.request("POST", f"/api/projects/{pid}/transfer", json={"user_id": tok})
    assert r.status_code == 403, f"协作人发起转移应 403，实得 {r.status_code}"


def test_管理员可代转且原所有人降为协作人(app, as_role, people):
    """管理员对全部项目放行（access.is_manager），交接规则对他也一样。"""
    owner = people['editor']['id']
    pid = prepo.create(f"管理员代转_{uuid.uuid4().hex[:6]}", "", [], owner)['id']
    admin = as_role('admin')
    target = _user('editor')
    admin.post(f"/api/projects/{pid}/members", json={"user_id": target['id']})
    r = admin.request("POST", f"/api/projects/{pid}/transfer", json={"user_id": target['id']})
    assert r.status_code == 200, r.text
    assert r.json()["owner_id"] == target['id']
    with get_conn() as conn:
        members = {x['user_id'] for x in conn.execute(
            "SELECT user_id FROM project_member WHERE project_id=?", (pid,))}
    assert owner in members
