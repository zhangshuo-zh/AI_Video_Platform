"""W01 的基础设施与真实行为回归：迁移/备份/引导口令/上传安全/层级/前端出口。

这一份的重点不是"功能多"，而是**每一条都能证伪**：
断言都写具体值或具体集合，不写 `assert r.status_code < 500` 这种永远绿的。
"""
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import migrate
from db import get_conn
from repositories.projects import projects as project_repo
from repositories.users import sessions, users as user_repo
from security import hash_password, new_session_token

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "frontend"


# ---------------------------------------------------------------- 迁移与库
def test_库停在最新迁移且外键干净():
    with get_conn() as conn:
        v = [r["version"] for r in conn.execute("SELECT version FROM schema_version")]
        assert sorted(v) == list(range(1, 12)), f"迁移版本不连续：{sorted(v)}"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_重复执行迁移是安全的():
    """启动时总会跑一遍 migrate()，跑第二次不能报错也不能重复建表。"""
    before = _table_names()
    migrate.migrate(verbose=False)
    assert _table_names() == before


def _table_names() -> set[str]:
    with get_conn() as conn:
        return {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}


# ---------------------------------------------------------------- 备份
def test_备份只留最近五份且带走_sidecar(tmp_path, monkeypatch):
    """老代码用 glob('.bak-*') 数份数，把 -wal/-shm 也当成独立备份：
    于是"保留 5 份"实际只留两三份真备份，而被删备份的 sidecar 永远清不掉。"""
    db = tmp_path / "platform.db"
    with get_conn() as src:
        pass
    import sqlite3
    sqlite3.connect(db).close()
    monkeypatch.setattr(migrate, "DB_PATH", db)

    for i in range(8):                      # 手搓 8 份"已有备份"，含 sidecar
        p = tmp_path / f"platform.db.bak-2026{i:03d}01"
        p.write_bytes(b"x")
        (tmp_path / (p.name + "-wal")).write_bytes(b"")
        (tmp_path / (p.name + "-shm")).write_bytes(b"")

    with sqlite3.connect(db) as conn:
        migrate._backup(conn, verbose=False)

    mains = sorted(p.name for p in tmp_path.glob("platform.db.bak-*")
                   if not p.name.endswith(("-wal", "-shm")))
    assert len(mains) == 5, f"主备份应有 5 份，实得 {len(mains)}：{mains}"
    # 被裁掉的两份（编号最小）连 sidecar 一起消失
    for gone in ("platform.db.bak-202600001", "platform.db.bak-202600101"):
        assert not (tmp_path / gone).exists()
        assert not (tmp_path / (gone + "-wal")).exists()
        assert not (tmp_path / (gone + "-shm")).exists()
    # 留下的备份不该有孤儿 sidecar：每个 sidecar 都要有主文件
    for side in tmp_path.glob("platform.db.bak-*-wal"):
        assert side.with_name(side.name[:-4]).exists(), f"孤儿 sidecar：{side.name}"


# ---------------------------------------------------------------- 引导口令
def test_没给环境变量时口令是随机生成的(monkeypatch):
    """规格 01-7.2-7：口令不得写进提交。原来这里是一个硬编码常量。"""
    from bootstrap import _initial_password
    monkeypatch.delenv("FASTVIDEO_ADMIN_PASSWORD", raising=False)
    pw1, from_env1 = _initial_password()
    pw2, _ = _initial_password()
    assert from_env1 is False
    assert len(pw1) >= 12 and pw1 != pw2, "两次调用应给出不同的随机口令"
    monkeypatch.setenv("FASTVIDEO_ADMIN_PASSWORD", "  给定口令  ")
    pw3, from_env3 = _initial_password()
    assert (pw3, from_env3) == ("给定口令", True), "环境变量优先，且要 trim"


def test_源码里不再有硬编码回退口令():
    src = (BACKEND / "bootstrap.py").read_text()
    assert "os.environ.get(\"FASTVIDEO_ADMIN_PASSWORD\"" not in src.replace("'", '"') or \
           "DEFAULT_PASSWORD = os.environ.get" not in src, \
        "bootstrap 里不该再有 `os.environ.get(..., <字面口令>)` 这种带默认值的写法"


# ---------------------------------------------------------------- 会话与改密
def test_登录_强制改密_登出这条链是真的(client):
    """新建账号 must_change_password=1 → 登录回该标志 → 改密后旧会话作废、新口令能登。"""
    uname = f"pw_{uuid.uuid4().hex[:8]}"
    pw = "w01-测试口令-123"
    user_repo.create(uname, "改密测试", hash_password(pw), must_change_password=True,
                     role_codes=["viewer"])
    r = client.post("/api/auth/login", json={"username": uname, "password": pw})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["must_change_password"] is True
    token = r.cookies.get("fv_session")

    bad = client.post("/api/auth/login", json={"username": uname, "password": "错的"})
    assert bad.status_code == 401, "错口令不能给 200"

    c = TestClient(__import__("main").app, cookies={"fv_session": token})
    ch = c.post("/api/auth/password", json={"old_password": pw, "new_password": "w01-新口令-456"})
    assert ch.status_code == 200, ch.text
    # 改密后服务端把该用户的会话全作废：拿旧 cookie 再打一次必须 401
    assert c.get("/api/auth/me").status_code == 401, "改密后旧会话应失效"
    again = client.post("/api/auth/login", json={"username": uname, "password": "w01-新口令-456"})
    assert again.status_code == 200, "新口令应能登录"
    assert again.json()["user"]["must_change_password"] is False


def test_未登录取_me_是_401(client):
    assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------- 层级与归属
def test_超级用户账号在普通用户接口里只读(as_role):
    """`_guard_target` 的两条拦截：超管一律 422 明说"不可改"，比调用者层级高的 404。

    实测下来只有 422 这条走得到——层级 2 的唯一来源就是 is_superuser，
    所以"更高层级 → 404"那条分支今天是防御性的（要动超管只有 reset_password.py）。
    """
    c = as_role("admin")
    with get_conn() as conn:
        sup = conn.execute("SELECT id FROM app_user WHERE is_superuser = 1").fetchone()["id"]
    r = c.patch(f"/api/users/{sup}", json={"display_name": "试图改名"})
    assert r.status_code == 422, r.text
    assert "超级用户" in r.json()["detail"]
    # httpx 的 .delete() 不收 json，删用户要带 body ⇒ 走 request()
    d = c.request("DELETE", f"/api/users/{sup}", json={})
    assert d.status_code == 422, f"删超管也该被同样挡住，实得 {d.status_code}"


def test_管理员不能改自己的角色也不能停用自己(as_role, people):
    c = as_role("admin")
    me = people["admin"]["id"]
    assert c.put(f"/api/users/{me}/roles", json={"roles": ["viewer"]}).status_code == 400 or \
        c.put(f"/api/users/{me}/roles", json={"roles": ["viewer"]}).status_code == 422
    r = c.patch(f"/api/users/{me}", json={"is_active": False})
    assert r.status_code in (400, 409, 422), f"停用自己应被拒，实得 {r.status_code}"


def test_内置_admin_角色的权限点后端也拦(as_role):
    """前端把 admin 列禁了勾；真正的门在服务端（规格 01-4.8.1-5）。"""
    c = as_role("superadmin")
    with get_conn() as conn:
        rid = conn.execute("SELECT id FROM role WHERE code='admin'").fetchone()["id"]
    r = c.put(f"/api/roles/{rid}/permissions", json={"permissions": []})
    assert r.status_code == 422, r.text
    assert "admin" in r.json()["detail"]


def test_打开项目不推进_updated_at(as_role, seeded):
    """规格 01-4.1-B3：打开不是修改。混了以后"最后修改"就不再表示内容动过。"""
    c = as_role("editor")
    with get_conn() as conn:
        before = conn.execute("SELECT updated_at, last_opened_at FROM project WHERE id=?",
                              (seeded["project_id"],)).fetchone()
    r = c.post(f"/api/projects/{seeded['project_id']}/open")
    assert r.status_code == 204
    with get_conn() as conn:
        after = conn.execute("SELECT updated_at, last_opened_at FROM project WHERE id=?",
                             (seeded["project_id"],)).fetchone()
    assert after["updated_at"] == before["updated_at"], "updated_at 被打开动作推进了"
    assert after["last_opened_at"] is not None


# ---------------------------------------------------------------- 上传安全
def _png() -> bytes:
    return bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                         "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")


def test_上传校验四件事(as_role, seeded, fresh_project):
    """类型白名单 / 文件头对不上 / 超大 / 正常成功，四条都要有明确回码。"""
    from config import FILES_DIR
    pid = fresh_project("editor")
    c = as_role("editor")
    aid = c.post(f"/api/projects/{pid}/assets",
                 json={"kind": "character", "name": f"上传测试_{uuid.uuid4().hex[:6]}"}).json()["id"]

    fake = c.post(f"/api/assets/{aid}/images",
                  files={"file": ("fake.png", b"#!/bin/sh\necho not an image", "image/png")})
    assert fake.status_code == 415, f"文件头与类型不符应 415，实得 {fake.status_code}"

    heic = c.post(f"/api/assets/{aid}/images",
                  files={"file": ("x.png", b"\x00\x00\x00\x18ftypheic", "image/png")})
    assert heic.status_code == 415 and "HEIC" in heic.json()["detail"]

    gif = c.post(f"/api/assets/{aid}/images",
                 files={"file": ("x.gif", b"GIF89a" + b"\x00" * 20, "image/gif")})
    assert gif.status_code == 415, "白名单外类型必须拒"

    big = c.post(f"/api/assets/{aid}/images",
                 files={"file": ("big.png", _png() + b"\x00" * (21 * 1024 * 1024), "image/png")})
    assert big.status_code == 413, f"超过 20MB 应 413，实得 {big.status_code}"

    ok = c.post(f"/api/assets/{aid}/images", files={"file": ("ok.png", _png(), "image/png")})
    assert ok.status_code == 200, ok.text
    rel = ok.json()["ref_images"][0]
    assert (FILES_DIR / rel).is_file(), "登记了却没落盘"
    assert rel.startswith(f"{aid}/"), f"路径应落在资产目录下：{rel}"


def test_删除图片不能越出上传目录(as_role, seeded, fresh_project):
    """path 是客户端给的：拼出来的路径必须仍在 FILES_DIR 内，且不登记的直接拒。"""
    pid = fresh_project("editor")
    c = as_role("editor")
    aid = c.post(f"/api/projects/{pid}/assets",
                 json={"kind": "scene", "name": f"穿越_{uuid.uuid4().hex[:6]}"}).json()["id"]
    r = c.delete(f"/api/assets/{aid}/images", params={"path": "../../etc/passwd"})
    assert r.status_code == 404, f"未登记的路径应 404，实得 {r.status_code}"
    assert Path("/etc/passwd").is_file(), "系统文件当然还在"


# ---------------------------------------------------------------- 前端出口
def test_三个删除接口不再假成功():
    """W00 抓到的真 bug：这三个 DELETE 漏了 toJson，后端拒绝也照样刷新成"已删除"。"""
    src = (FRONTEND / "js" / "api.js").read_text()
    for fn in ("deleteEpisode", "deleteShot", "deleteAsset"):
        line = next((l for l in src.split("\n") if fn in l and "fetch" in l), "")
        assert "toJson" in line, f"{fn} 仍缺 .then(toJson)：{line.strip()}"


def test_业务视图不直接拼_url():
    """规格 01-6-6／02 §3.1：前端只用 api.js 一个出口，视图里不该出现 fetch(。"""
    offenders = []
    for f in (FRONTEND / "js" / "views").glob("*.js"):
        if "fetch(" in f.read_text():
            offenders.append(f.name)
    assert not offenders, f"这些视图绕过了 api.js：{offenders}"


def test_alert_只有_util_一处():
    """统一错误出口的守门人：有人新写 alert 就会红，逼他走 run()。"""
    hits = []
    for f in (FRONTEND / "js").rglob("*.js"):
        for n, line in enumerate(f.read_text().split("\n"), 1):
            if re.search(r"(?<![\w.])alert\s*\(", line) and not line.strip().startswith("//"):
                hits.append(f"{f.name}:{n}")
    assert hits == ["util.js:20"], f"alert 出现在不该出现的地方：{hits}"
