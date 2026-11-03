"""这一轮点定的三件事的回归：删账号的事务、两处摘除（死列与空权限点）。

三条的共性是"摘掉或收紧之后不会有人报错，坏在下一次"：
`shot.status` 留着就有人以为它有含义；`setting:*` 留着就有管理员以为勾上能控什么；
删账号那一段不是原子写，就会在某一天留下"半套转移"。
所以断言都写具体集合与具体数字，不写 `status_code < 500` 这种永远绿的。
"""
import uuid
from pathlib import Path

import pytest

import routers.users as users_router
from db import get_conn, transaction
from repositories.projects import projects as project_repo
from repositories.users import users as user_repo
from security import hash_password

BACKEND = Path(__file__).resolve().parents[1]
PW = "pytest-only-not-a-real-secret"


def _make_user(role: str) -> int:
    """临时账号。口令值不是真口令，测试从不拿它登录，只用来把项目挂到名下。"""
    return user_repo.create(f"t_{role}_{uuid.uuid4().hex[:6]}", f"{role} 临时账号",
                            hash_password(PW), role_codes=[role])["id"]


def _owned(uid: int, n: int) -> list[int]:
    return [project_repo.create(f"账号事务_{uuid.uuid4().hex[:6]}_{i}", "", [], uid)["id"]
            for i in range(n)]


def _owners(ids: list[int]) -> dict:
    return {pid: project_repo.get(pid)["owner_id"] for pid in ids}


# ---------------------------------------------------------------- ① 删账号的事务

def test_transaction_里_get_conn_借用同一条连接():
    """嵌套不能提前提交：内层一 commit，外层回滚就救不回已经写出去的那半。"""
    with pytest.raises(RuntimeError):
        with transaction() as conn:
            with get_conn() as inner:
                assert inner is conn, "get_conn 没加入活动事务，这一层就白包了"
            raise RuntimeError("故意失败，看回滚彻不彻底")


def test_事务中途失败时那一半写没留下():
    """上一条只证"借用同一条连接"；这一条证回滚真的把写带走了。"""
    uid = _make_user("editor")
    pid = _owned(uid, 1)[0]
    heir = _make_user("editor")
    with pytest.raises(RuntimeError):
        with transaction():
            project_repo.set_owner(pid, heir)
            raise RuntimeError("写完就炸")
    assert project_repo.get(pid)["owner_id"] == uid, "事务里的写漏出去了，等于没包事务"


def test_转移账号中途失败时不留半套状态(as_role, monkeypatch):
    """第 3 个项目改不动时，前 2 个也必须没改——这才叫"一个动作"。"""
    admin = as_role("admin")
    victim = _make_user("editor")
    heir = _make_user("editor")
    ids = _owned(victim, 3)
    before = _owners(ids)
    assert set(before.values()) == {victim}

    calls = {"n": 0}
    real = project_repo.set_owner

    def flaky(project_id, target_id):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("假装第 3 次写不下去")
        return real(project_id, target_id)

    monkeypatch.setattr(project_repo, "set_owner", flaky)
    r = admin.request("DELETE", f"/api/users/{victim}",
                      json={"transfer_to": heir, "delete_projects": False})
    assert r.status_code == 500, r.text
    assert calls["n"] == 3, "第三次没炸，这条测试就是空跑"
    assert _owners(ids) == before, "回滚不彻底：部分项目已经换了东家"
    assert user_repo.get_by_id(victim) is not None, "整段失败时账号不该被带走"

    # 修好之后再来一次，必须能一次做完（证明失败没把库留在不能动的状态）
    monkeypatch.setattr(project_repo, "set_owner", real)
    ok = admin.request("DELETE", f"/api/users/{victim}",
                       json={"transfer_to": heir, "delete_projects": False})
    assert ok.status_code == 204, ok.text
    assert set(_owners(ids).values()) == {heir}


def test_名下没有项目的账号也要真删掉(as_role):
    """阶段 B 验收撞到的：这一支原来不存在，接口回 204、账号还在库里。

    `transfer_to is None` 且 `delete_projects=False` 时，代码走的是
    `elif owned:`——名下没有项目就两个分支都不进，函数什么也不做就返回 204。
    界面上写"已删除"，下一次登录列表里那个人还在，这就是 W01-b 收拾过的
    那一类"假成功的 DELETE"，只是这一处漏了网。
    """
    admin = as_role("admin")
    victim = _make_user("viewer")
    assert project_repo.owned_by(victim) == []          # 前提：名下一个项目都没有

    r = admin.request("DELETE", f"/api/users/{victim}",
                      json={"transfer_to": None, "delete_projects": False})
    assert r.status_code == 204, r.text
    assert user_repo.get_by_id(victim) is None, "回了 204 但账号还在＝假成功"
    with get_conn() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM app_user WHERE id = ?",
                            (victim,)).fetchone()["n"]
    assert rows == 0


def test_连带删项目失败时要说清删到哪一步(as_role, monkeypatch):
    """这条路回滚不回来（Git 与文件已经动了），至少让人看见做到哪里、账号还在不在。"""
    admin = as_role("admin")
    victim = _make_user("editor")
    ids = _owned(victim, 2)
    done = []
    real = users_router.delete_project_cascade

    def boom(project_id, actor="平台"):
        if done:
            raise RuntimeError("假装第二个项目的文件删不动")
        real(project_id, actor)
        done.append(project_id)

    monkeypatch.setattr(users_router, "delete_project_cascade", boom)
    r = admin.request("DELETE", f"/api/users/{victim}",
                      json={"transfer_to": None, "delete_projects": True})
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert "已删掉 1 个" in detail, detail
    assert "账号还在" in detail, detail
    assert user_repo.get_by_id(victim) is not None, "没删干净时不该把账号也带走"
    assert project_repo.get(done[0]) is None, f"报「已删掉 1 个」但 #{done[0]} 还在，等于说谎"
    assert project_repo.get(ids[1]) is not None, "没删成的那个不该被当成删掉了"


# ---------------------------------------------------------------- ② 两张表的死列

def test_shot_与_asset_不再有_status_列():
    """0023：没有写入方的状态列就是第二个真相源，摘掉。"""
    with get_conn() as conn:
        for table in ("shot", "asset"):
            cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert "status" not in cols, f"{table}.status 还在表里：{sorted(cols)}"
        # 别的状态列不许被顺手带走：集与项目的 status 都有真实写入方
        for table in ("episode", "project"):
            cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert "status" in cols, f"{table}.status 被误删了"


def test_镜头与资产接口不再返回_status(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    ep = c.post(f"/api/projects/{pid}/episodes", json={"title": "摘列后的一集"}).json()
    body = {"summary": "开场", "prompt": "", "duration": 5, "asset_ids": []}
    shot = c.post(f"/api/episodes/{ep['id']}/shots", json=body).json()
    assert "status" not in shot, shot
    put = c.put(f"/api/shots/{shot['id']}",
                json={**body, "summary": "改一版", "duration": 6}).json()
    assert "status" not in put, put
    asset = c.post(f"/api/projects/{pid}/assets",
                   json={"kind": "character", "name": f"摘列资产_{uuid.uuid4().hex[:6]}",
                         "summary": "", "consistency_prompt": "", "aliases": [],
                         "tags": []}).json()
    assert "status" not in asset, asset
    assert "status" not in c.get(f"/api/assets/{asset['id']}").json()


def test_仓储层不再留着没人调的_set_status():
    """死代码不留在仓储层等人误用；集的 set_status 有调用方，必须还在。"""
    from repositories.content import episodes, shots

    assert not hasattr(shots, "set_status"), "shots.set_status 零调用方，应随 0023 删除"
    assert hasattr(episodes, "set_status"), "集状态是真在用的，不该被顺手删掉"


# ---------------------------------------------------------------- ③ 空权限点

def test_setting_权限点已从库里撤掉():
    with get_conn() as conn:
        codes = {row["code"] for row in conn.execute("SELECT code FROM permission")}
        assert "setting:read" not in codes and "setting:write" not in codes, sorted(codes)
        assert len(codes) == 18, f"权限点总数不是 18：{len(codes)}"
        assert conn.execute(
            "SELECT 1 FROM permission WHERE group_name = '设定'").fetchone() is None
        # 迁移期间外键是关着的，级联不会自己触发——孤儿行必须单独查
        orphan = conn.execute(
            """SELECT COUNT(*) AS n FROM role_permission rp
                WHERE NOT EXISTS (SELECT 1 FROM permission p WHERE p.id = rp.permission_id)"""
        ).fetchone()["n"]
        assert orphan == 0, f"role_permission 里留下 {orphan} 行孤儿授权"


def test_三个默认角色的权限点随之减到位():
    with get_conn() as conn:
        counts = {row["code"]: row["n"] for row in conn.execute(
            """SELECT r.code, COUNT(rp.permission_id) AS n
                 FROM role r LEFT JOIN role_permission rp ON rp.role_id = r.id
                GROUP BY r.code""")}
    # admin 用子查询拿全部，跟着权限点总数走；editor 原 15 去掉 setting 两条；
    # viewer 原 8（所有 :read）去掉 setting:read
    assert counts == {"admin": 18, "editor": 13, "viewer": 7}, counts


def test_代码里再也没有引用这两个权限点的地方():
    """撤干净才算撤：留一处 `require_permission('setting:read')` 就是留一扇永远 403 的门。"""
    py = [p.name for p in BACKEND.rglob("*.py")
          if ".venv" not in p.parts and p.parts[-2] != "tests"
          and "setting:" in p.read_text(encoding="utf-8", errors="ignore")]
    assert py == [], f"还有代码引用已撤的权限点：{py}"
    # 只有两条迁移可以提到它们：0002 的 seed 与 0024 的撤销都是历史事实
    sql = sorted(p.name for p in (BACKEND / "migrations").glob("*.sql")
                 if "setting:" in p.read_text(encoding="utf-8"))
    assert sql == ["0002_auth.sql", "0024_drop_setting_perms.sql"], sql
    # 前端也别留：`store.can('setting:write')` 这种判断在权限点撤掉后永远为假，
    # 表现为"按钮再也不出现"，比报错更难查
    js_root = BACKEND.parent / "frontend"
    js = [p.name for p in js_root.rglob("*.js")
          if "setting:" in p.read_text(encoding="utf-8", errors="ignore")]
    assert js == [], f"前端还在判断已撤的权限点：{js}"
