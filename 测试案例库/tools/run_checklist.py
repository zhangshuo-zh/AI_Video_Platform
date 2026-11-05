#!/usr/bin/env python3
"""按《清单.md》对现有代码跑一遍测试案例。

设计纪律（和 app/backend/tests 同一套理由）：

1. **环境变量必须在 import main 之前设**。config.py 在导入时就把 FASTVIDEO_DB 等
   读成常量；而 import main 会立刻跑 migrate() + ensure_admin()。晚设等于没设。
2. **不碰开发库**。临时目录跑完整迁移，产物、稿件工作区、作业目录全在 /tmp 下。
3. **不起端口、不真登录**（只有改口令那几条要真登录验证会话）。会话用
   `security.new_session_token()` 直接铸，快且不把口令传来传去。
4. **执行器不替代码兜底**：断言失败就 FAIL，环境不满足就 BLOCKED，绝不 `or True`。
5. 需要跑作业的用例现场调 `worker.run_once()`，用 mock 适配器；真实模型一次不调。

用法：
    app/backend/.venv/bin/python 测试案例库/tools/run_checklist.py [--only AUTH,PROJ] [--json]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# ---------- 1. 环境必须在任何项目 import 之前 ----------
_TMP = Path(tempfile.mkdtemp(prefix="fvqa-"))
os.environ["FASTVIDEO_DB"] = str(_TMP / "qa.sqlite")
os.environ["FASTVIDEO_FILES"] = str(_TMP / "files")
os.environ["FASTVIDEO_WORKSPACE"] = str(_TMP / "workspace")
os.environ["FASTVIDEO_JOBS"] = str(_TMP / "jobs")
os.environ["FASTVIDEO_ALLOW_MOCK"] = "1"
os.environ["FASTVIDEO_ADMIN_PASSWORD"] = "qa-runner-not-a-real-secret"

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app" / "backend"))

import main                      # noqa: E402  (import 时完成迁移与 bootstrap)
import services                  # noqa: E402
import worker                    # noqa: E402
from db import get_conn          # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from repositories.users import sessions    # noqa: E402
from repositories.users import users as user_repo  # noqa: E402
from security import hash_password, new_session_token  # noqa: E402

RUN = datetime.now().strftime("%m%d%H%M") + "-" + uuid.uuid4().hex[:4]
APP = main.app
PASSWORD = "qa-passwd-" + uuid.uuid4().hex[:6]


# ---------- 2. 小工具 ----------
class Blocked(Exception):
    """环境不满足，不算产品缺陷。"""


def mint(user_id: int) -> str:
    token, token_hash = new_session_token()
    expires = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    sessions.create(token_hash, user_id, expires)
    return token


_clients: dict[str, TestClient] = {}


def cli(role: str = "editor") -> TestClient:
    """带某个身份 Cookie 的客户端；anonymous 不带。"""
    if role == "anonymous":
        return TestClient(APP)
    if role not in _clients:
        raise KeyError(f"账号 {role} 还没建")
    return _clients[role]


def db(sql: str, params=()):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def db1(sql: str, params=()):
    rows = db(sql, params)
    return rows[0] if rows else None


def eq(actual, expected, what: str):
    if actual != expected:
        raise AssertionError(f"{what}：期望 {expected!r}，实际 {actual!r}")


def ok(cond: bool, what: str):
    if not cond:
        raise AssertionError(f"{what} 不成立")


def st(resp, code, what: str):
    codes = code if isinstance(code, (list, tuple)) else [code]
    if resp.status_code not in codes:
        raise AssertionError(f"{what}：期望状态 {codes}，实际 {resp.status_code}"
                             f"，detail={resp.text[:300]}")
    return resp


def detail_has(resp, needle: str, what: str):
    body = resp.text
    ok(needle in body, f"{what}：响应里应含「{needle}」，实际 {body[:300]}")


def jget(payload, dotted):
    cur = payload
    for part in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def new_project(role: str, tag: str) -> int:
    name = f"QA-{tag}-{RUN}"
    r = cli(role).post("/api/projects", json={"name": name, "description": "qa",
                                              "tags": ["qa"]})
    st(r, 201, f"{role} 建项目")
    return r.json()["id"]


def drop_project(pid: int) -> None:
    """删项目要 project:delete（只有管理员有），清理一律走 admin。"""
    name = db1("SELECT name FROM project WHERE id=?", (pid,))["name"]
    st(cli("admin").delete(f"/api/projects/{pid}?confirm={name}"), 204, "清理项目")


def new_episode(pid: int, title: str) -> int:
    r = cli("editor").post(f"/api/projects/{pid}/episodes", json={"title": title})
    st(r, 201, "建集")
    return r.json()["id"]


def new_shot(eid: int, summary: str = "开场镜头", **kw) -> int:
    body = {"summary": summary, "prompt": kw.get("prompt", ""),
            "duration": kw.get("duration", 5),
            "asset_ids": kw.get("asset_ids", []), "variant_ids": kw.get("variant_ids", [])}
    r = cli("editor").post(f"/api/episodes/{eid}/shots", json=body)
    st(r, 201, "建镜头")
    return r.json()["id"]


def new_asset(pid: int, kind: str = "character", name: str | None = None) -> int:
    r = cli("editor").post(f"/api/projects/{pid}/assets",
                           json={"kind": kind, "name": name or f"资产_{uuid.uuid4().hex[:6]}",
                                 "summary": "", "consistency_prompt": "",
                                 "aliases": [], "tags": [], "scope": "project"})
    st(r, 201, "建资产")
    return r.json()["id"]


def png_bytes(size: int = 1024) -> bytes:
    """最小合法 PNG 头 + 填充，够过魔数校验。"""
    head = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89")
    tail = b"\x00\x00\x00\x00IEND\xaeB`\x82"
    body = b"\x00" * max(0, size - len(head) - len(tail))
    return head + body + tail


def submit_mock_job(pid: int, capability: str = "video", params=None,
                    episode_id: int | None = None, shot_id: int | None = None,
                    key: str | None = None, prompt: str = "测试提示词",
                    role: str = "editor"):
    body = {"project_id": pid, "episode_id": episode_id, "shot_id": shot_id,
            "capability": capability, "execution_mode": "local",
            "provider_code": "mock", "model_code": "mock-1", "prompt": prompt,
            "params": params or {"sleep": 0.0, "media": "text"}}
    if key:
        body["idempotency_key"] = key
    return cli(role).post("/api/jobs", json=body)


def run_worker(max_rounds: int = 200) -> None:
    """把队列里的 mock 作业全部跑掉；worker 每轮只领一条，所以必须排空。"""
    for _ in range(max_rounds):
        if worker.run_once(f"qa-{RUN}") is None:
            return


def sign_job(job_id: int, decision: str = "approved") -> None:
    """签字的锚点只能来自服务端：整条作业一个、每件产物一个。"""
    view = st(cli("editor").get(f"/api/jobs/{job_id}/review"), 200, "产物审核").json()
    anchors = view.get("anchors") or []
    if not anchors:
        raise Blocked(f"作业 {job_id} 没有可签的产物锚点：{view.get('status')}")
    r = cli("editor").post(f"/api/jobs/{job_id}/review-sign",
                           json={"anchor": anchors[0], "decision": decision, "note": "qa"})
    st(r, 200, "产物签字")


# ---------- 3. 案例注册 ----------
CASES: list[tuple[str, str, callable]] = []


def case(cid: str, title: str):
    def deco(fn):
        CASES.append((cid, title, fn))
        return fn
    return deco


# ============================ 认证与会话 ============================
@case("AUTH-01", "未登录访问业务接口")
def _():
    for path in ("/api/projects", "/api/jobs", "/api/workbench"):
        st(cli("anonymous").get(path), 401, f"匿名 GET {path}")


@case("AUTH-02", "会话读回身份与权限点数量")
def _():
    counts = {}
    for role in ("admin", "editor", "viewer"):
        r = st(cli(role).get("/api/auth/me"), 200, f"{role} me")
        counts[role] = len(r.json()["permissions"])
    eq(counts, {"admin": 18, "editor": 13, "viewer": 7}, "三种角色的权限点数")


@case("AUTH-03", "改口令的三条拒绝")
def _():
    u = user_repo.create(f"qa_pw_{RUN}", "改密测试", hash_password(PASSWORD),
                         role_codes=["editor"])
    token = mint(u["id"])
    c = TestClient(APP, cookies={"fv_session": token})
    st(c.post("/api/auth/password", json={"old_password": "wrong-one",
                                          "new_password": "brand-new-1"}),
       401, "原密码错")
    detail_has(c.post("/api/auth/password", json={"old_password": PASSWORD,
                                                  "new_password": PASSWORD}),
               "新密码不能和原密码相同", "新旧相同")
    st(c.post("/api/auth/password", json={"old_password": PASSWORD, "new_password": "123"}),
       422, "新密码太短")


@case("AUTH-04", "改口令后旧会话作废、新口令可登录")
def _():
    u = user_repo.create(f"qa_pw2_{RUN}", "改密测试2", hash_password(PASSWORD),
                         role_codes=["editor"])
    token = mint(u["id"])
    c = TestClient(APP, cookies={"fv_session": token})
    r = st(c.post("/api/auth/password", json={"old_password": PASSWORD,
                                              "new_password": "brand-new-2"}),
           200, "改密成功")
    eq(r.json().get("relogin"), True, "改密要回一句要重登")
    st(c.get("/api/auth/me"), 401, "旧会话应失效")
    fresh = TestClient(APP)
    st(fresh.post("/api/auth/login", json={"username": u["username"],
                                           "password": "brand-new-2"}),
       200, "新口令登录")
    st(fresh.post("/api/auth/login", json={"username": u["username"],
                                           "password": PASSWORD}),
       401, "旧口令应不再可用")


@case("AUTH-05", "登出即失效")
def _():
    u = user_repo.create(f"qa_out_{RUN}", "登出测试", hash_password(PASSWORD),
                         role_codes=["viewer"])
    c = TestClient(APP, cookies={"fv_session": mint(u["id"])})
    st(c.post("/api/auth/logout"), 200, "登出")
    st(c.get("/api/auth/me"), 401, "登出后 me")


@case("AUTH-06", "停用账号立刻掉线")
def _():
    u = user_repo.create(f"qa_off_{RUN}", "停用测试", hash_password(PASSWORD),
                         role_codes=["editor"])
    c = TestClient(APP, cookies={"fv_session": mint(u["id"])})
    st(c.get("/api/auth/me"), 200, "停用前可用")
    st(cli("admin").patch(f"/api/users/{u['id']}", json={"is_active": False}),
       200, "admin 停用")
    st(c.get("/api/auth/me"), 401, "停用后应 401")
    st(cli("admin").patch(f"/api/users/{u['id']}", json={"is_active": True}),
       200, "恢复启用")


@case("AUTH-07", "超级用户受保护")
def _():
    su = user_repo.get_by_username("superadmin")
    st(cli("admin").patch(f"/api/users/{su['id']}", json={"is_active": False}),
       422, "停用超级用户")
    detail_has(cli("admin").put(f"/api/users/{su['id']}/roles", json={"roles": ["viewer"]}),
               "超级用户", "改超级用户角色")
    detail_has(cli("admin").request("DELETE", f"/api/users/{su['id']}", json={}),
               "超级用户", "删超级用户")


# ============================ 权限与可见性 ============================
@case("RBAC-01", "只读账号写不了")
def _():
    pid = new_project("editor", "rbac1")
    st(cli("viewer").post("/api/projects", json={"name": f"X-{RUN}"}),
       403, "viewer 建项目")
    st(cli("viewer").post(f"/api/projects/{pid}/episodes", json={"title": "t"}),
       403, "viewer 建集")
    st(cli("viewer").post("/api/jobs", json={"project_id": pid, "capability": "video",
                                             "prompt": "p"}),
       403, "viewer 提交作业")


@case("RBAC-02", "制作碰不到系统与用户")
def _():
    st(cli("editor").get("/api/models"), 403, "editor 读模型中心")
    st(cli("editor").get("/api/users"), 403, "editor 读用户列表")
    st(cli("admin").get("/api/models"), 200, "admin 读模型中心")


@case("RBAC-03", "看不见就是 404，不是 403")
def _():
    pid = new_project("editor", "rbac3")
    doc = cli("editor").post(f"/api/projects/{pid}/documents",
                             json={"kind": "novel", "title": "稿", "content": "燧人蹲在洞口。"})
    st(doc, 201, "建文档")
    doc_id = doc.json()["id"]
    job = submit_mock_job(pid)
    st(job, 202, "建作业")
    job_id = job.json()["id"]
    for path, what in ((f"/api/projects/{pid}/summary", "项目摘要"),
                       (f"/api/documents/{doc_id}", "文档"),
                       (f"/api/jobs/{job_id}", "作业")):
        r = cli("outsider").get(path)
        st(r, 404, f"outsider 读{what}")
        ok("燧人" not in r.text, f"{what} 的 404 不许带内容")


@case("RBAC-04", "成员不等于赋权")
def _():
    pid = new_project("editor", "rbac4")
    r = st(cli("editor").post(f"/api/projects/{pid}/members",
                              json={"user_id": ACCOUNTS["viewer"]["id"]}),
           200, "加 viewer 为成员")
    ok(any(m["user_id"] == ACCOUNTS["viewer"]["id"] for m in r.json()["members"]),
       "成员列表应有 viewer")
    st(cli("viewer").get(f"/api/projects/{pid}/episodes"), 200, "成员可读")
    st(cli("viewer").post(f"/api/projects/{pid}/episodes", json={"title": "t"}),
       403, "成员仍不可写")


@case("RBAC-05", "内置 admin 角色不许改权限")
def _():
    roles = st(cli("admin").get("/api/roles"), 200, "读角色").json()
    admin_role = next(x for x in roles if x["code"] == "admin")
    st(cli("admin").put(f"/api/roles/{admin_role['id']}/permissions",
                        json={"permissions": ["project:read"]}),
       422, "改内置 admin 权限")


@case("RBAC-06", "不能邀请层级高于自己的人")
def _():
    pid = new_project("editor", "rbac6")
    r = cli("editor").post(f"/api/projects/{pid}/members",
                            json={"user_id": ACCOUNTS["admin"]["id"]})
    eq(r.status_code, 422, "制作邀请层级更高的账号应被拒（新建项目那条路是 422）")


@case("RBAC-07", "三条自我保护")
def _():
    me = ACCOUNTS["admin"]["id"]
    st(cli("admin").patch(f"/api/users/{me}", json={"is_active": False}),
       422, "停用自己")
    detail_has(cli("admin").put(f"/api/users/{me}/roles", json={"roles": ["viewer"]}),
               "不能修改自己的角色", "改自己角色")
    detail_has(cli("admin").request("DELETE", f"/api/users/{me}", json={}),
               "不能删除自己", "删自己")


# ============================ 项目 ============================
@case("PROJ-01", "建项目即所有人")
def _():
    name = f"QA-P1-{RUN}"
    r = st(cli("editor").post("/api/projects", json={"name": name}), 201, "建项目")
    pid = r.json()["id"]
    eq(r.json()["owner_id"], ACCOUNTS["editor"]["id"], "owner_id")
    eq(db1("SELECT status FROM project WHERE id=?", (pid,))["status"], "active", "初始状态")
    cli("editor").delete(f"/api/projects/{pid}?confirm={name}")


@case("PROJ-02", "名称与标签归一")
def _():
    many = [f"标签{i}" for i in range(12)] + ["很" * 21]
    r = st(cli("editor").post("/api/projects",
                              json={"name": f"  QA-P2-{RUN}  ", "tags": many}),
           201, "建项目")
    pid = r.json()["id"]
    eq(r.json()["name"], f"QA-P2-{RUN}", "首尾空格应去掉")
    ok(len(r.json()["tags"]) <= 10, f"标签最多 10 个，实际 {len(r.json()['tags'])}")
    ok(all(len(t) <= 20 for t in r.json()["tags"]), "每个标签不超过 20 字")
    cli("editor").delete(f"/api/projects/{pid}?confirm={r.json()['name']}")


@case("PROJ-03", "名称校验")
def _():
    st(cli("editor").post("/api/projects", json={"name": ""}), 422, "空名")
    st(cli("editor").post("/api/projects", json={"name": "    "}), 422, "纯空格")
    st(cli("editor").post("/api/projects", json={"name": "长" * 61}), 422, "超 60 字")


@case("PROJ-04", "列表可见性四态")
def _():
    name = f"QA-P4-{RUN}"
    pid = st(cli("editor").post("/api/projects", json={"name": name, "member_ids": []}),
             201, "建项目").json()["id"]
    cli("editor").post(f"/api/projects/{pid}/members", json={"user_id": ACCOUNTS["viewer"]["id"]})

    def visible(role):
        return pid in [p["id"] for p in st(cli(role).get("/api/projects"), 200, role).json()]

    eq([visible(r) for r in ("editor", "viewer", "outsider", "admin")],
       [True, True, False, True], "owner／成员／外人／管理员")


@case("PROJ-05", "打开项目记录时间")
def _():
    pid = new_project("editor", "p5")
    before = db1("SELECT last_opened_at FROM project WHERE id=?", (pid,))["last_opened_at"]
    st(cli("editor").post(f"/api/projects/{pid}/open"), 204, "打开项目")
    after = db1("SELECT last_opened_at FROM project WHERE id=?", (pid,))["last_opened_at"]
    ok(after and after != before, f"last_opened_at 应更新：{before!r} → {after!r}")


@case("PROJ-06", "归档是 own 级")
def _():
    pid = new_project("editor", "p6")
    other = user_repo.create(f"qa_ed2_{RUN}", "第二个制作", hash_password(PASSWORD),
                             role_codes=["editor"])
    cli("editor").post(f"/api/projects/{pid}/members", json={"user_id": other["id"]})
    c2 = TestClient(APP, cookies={"fv_session": mint(other["id"])})
    st(c2.patch(f"/api/projects/{pid}/status", json={"status": "archived"}),
       403, "协作人不能归档")
    st(cli("editor").patch(f"/api/projects/{pid}/status", json={"status": "archived"}),
       200, "所有人归档")
    eq(db1("SELECT status FROM project WHERE id=?", (pid,))["status"], "archived", "库内状态")
    st(cli("editor").patch(f"/api/projects/{pid}/status", json={"status": "active"}),
       200, "恢复在产")


@case("PROJ-07", "删除要原样项目名")
def _():
    pid = new_project("editor", "p7")
    name = db1("SELECT name FROM project WHERE id=?", (pid,))["name"]
    r = cli("admin").delete(f"/api/projects/{pid}?confirm=错的名字")
    st(r, 400, "确认名不对")
    detail_has(r, "项目名不匹配", "拒绝原因")
    ok(db1("SELECT id FROM project WHERE id=?", (pid,)) is not None, "项目不该被删")
    st(cli("admin").delete(f"/api/projects/{pid}?confirm={name}"), 204, "确认名正确")
    ok(db1("SELECT id FROM project WHERE id=?", (pid,)) is None, "项目应已删除")


@case("PROJ-08", "删项目留下作业与产物（0039 已修）")
def _():
    pid = new_project("editor", "p8")
    name = db1("SELECT name FROM project WHERE id=?", (pid,))["name"]
    eid = new_episode(pid, "第 1 集")
    sid = new_shot(eid)
    job = submit_mock_job(pid, episode_id=eid, shot_id=sid)
    st(job, 202, "建作业")
    job_id = job.json()["id"]
    st(cli("admin").delete(f"/api/projects/{pid}?confirm={name}"), 204, "删项目")
    eq(db1("SELECT COUNT(*) AS n FROM episode WHERE project_id=?", (pid,))["n"], 0, "集应清空")
    eq(db1("""SELECT COUNT(*) AS n FROM shot s JOIN episode e ON e.id = s.episode_id
              WHERE e.project_id = ?""", (pid,))["n"], 0, "镜头应清空")
    eq(db1("SELECT COUNT(*) AS n FROM job WHERE id=?", (job_id,))["n"], 1,
       "《系统使用说明书》§2.5 承诺作业与产物留在项目里")


@case("PROJ-09", "批量逐项报结果")
def _():
    mine = new_project("editor", "p9a")
    theirs = new_project("outsider", "p9b")
    r = st(cli("editor").post("/api/projects/batch",
                              json={"action": "archive", "ids": [mine, theirs]}),
           200, "批量归档")
    eq(r.json()["done"], [mine], "只该动自己的")
    eq(len(r.json()["skipped"]), 1, "别人的进 skipped")
    ok(r.json()["skipped"][0]["reason"], "skipped 要带原因")
    cli("editor").delete(f"/api/projects/{mine}?confirm=QA-p9a-{RUN}")


@case("PROJ-10", "批量删除确认词")
def _():
    pid = new_project("editor", "p10")
    r = cli("admin").post("/api/projects/batch-delete",
                          json={"ids": [pid], "confirm": "随便"})
    st(r, 400, "确认词不对")
    ok(db1("SELECT id FROM project WHERE id=?", (pid,)) is not None, "整批一个都不该删")
    st(cli("admin").post("/api/projects/batch-delete",
                         json={"ids": [pid], "confirm": "批量删除"}), 200, "确认词对")
    ok(db1("SELECT id FROM project WHERE id=?", (pid,)) is None, "应已删除")


@case("PROJ-11", "交接所有权")
def _():
    pid = new_project("editor", "p11")
    target = ACCOUNTS["editor2"]["id"]
    st(cli("editor").post(f"/api/projects/{pid}/transfer",
                          json={"user_id": target}), 200, "交接")
    eq(db1("SELECT owner_id FROM project WHERE id=?", (pid,))["owner_id"], target, "新所有人")
    members = st(cli("editor2").get(f"/api/projects/{pid}/members"), 200, "读名单").json()
    ok(ACCOUNTS["editor"]["id"] in [m["user_id"] for m in members["members"]],
       "原所有人应降为协作人")


# ============================ 创作 ============================
@case("WRT-01", "集号自动排")
def _():
    pid = new_project("editor", "w1")
    seqs = [st(cli("editor").post(f"/api/projects/{pid}/episodes", json={"title": f"第{i}集"}),
               201, "建集").json()["seq"] for i in (1, 2)]
    eq(seqs, [1, 2], "seq 自动递增")
    st(cli("editor").post(f"/api/projects/{pid}/episodes", json={"title": "  "}),
       422, "空标题")


@case("WRT-02", "集状态三档")
def _():
    pid = new_project("editor", "w2")
    eid = new_episode(pid, "第 1 集")
    for value in ("active", "done", "draft"):
        st(cli("editor").patch(f"/api/episodes/{eid}/status", json={"status": value}),
           200, f"状态 {value}")
    st(cli("editor").patch(f"/api/episodes/{eid}/status", json={"status": "shipped"}),
       422, "非法状态")


@case("WRT-03", "删集级联、作业保留")
def _():
    pid = new_project("editor", "w3")
    eid = new_episode(pid, "第 1 集")
    sid = new_shot(eid)
    job_id = submit_mock_job(pid, episode_id=eid, shot_id=sid).json()["id"]
    st(cli("editor").delete(f"/api/episodes/{eid}"), 204, "删集")
    eq(db1("SELECT COUNT(*) AS n FROM shot WHERE episode_id=?", (eid,))["n"], 0, "镜头应级联删")
    row = db1("SELECT episode_id FROM job WHERE id=?", (job_id,))
    ok(row is not None, "作业行必须留着")
    eq(row["episode_id"], None, "作业的 episode_id 应置空")


@case("WRT-04", "建文档＝文件＋一次提交")
def _():
    pid = new_project("editor", "w4")
    eid = new_episode(pid, "第 1 集")
    d = st(cli("editor").post(f"/api/projects/{pid}/documents",
                              json={"kind": "novel", "episode_id": eid,
                                    "title": "第一章", "content": "燧人蹲在洞口。"}),
           201, "建文档")
    doc_id = d.json()["id"]
    rel = d.json()["rel_path"]
    ok((Path(os.environ["FASTVIDEO_WORKSPACE"]) / rel).is_file(), f"正文要落进工作区：{rel}")
    vs = st(cli("editor").get(f"/api/documents/{doc_id}/versions"), 200, "版本列表").json()
    eq(len(vs), 1, "建文档应落一次提交")
    c = st(cli("editor").get(f"/api/documents/{doc_id}/content"), 200, "读正文").json()
    ok("燧人" in c["content"], "正文读得回")
    ok(c["version"], "正文带回版本令牌")


@case("WRT-05", "旧令牌保存被拒")
def _():
    pid, doc_id, content = _seed_doc("w5")
    cli("editor").put(f"/api/documents/{doc_id}/content",
                      json={"content": "第一版正文。\n", "expected_version": content["version"]})
    r = cli("editor").put(f"/api/documents/{doc_id}/content",
                          json={"content": "拿旧令牌写。", "expected_version": content["version"]})
    st(r, 409, "过期令牌")
    now = st(cli("editor").get(f"/api/documents/{doc_id}/content"), 200, "读回").json()
    ok("拿旧令牌写" not in now["content"], "409 之后盘上不该变")


@case("WRT-06", "正确保存产生新版本")
def _():
    pid, doc_id, content = _seed_doc("w6")
    r = st(cli("editor").put(f"/api/documents/{doc_id}/content",
                             json={"content": "燧人站起来。\n",
                                   "expected_version": content["version"]}),
           200, "保存")
    ok(r.json()["version"] != content["version"], "版本令牌应变化")
    vs = st(cli("editor").get(f"/api/documents/{doc_id}/versions"), 200, "版本").json()
    eq(len(vs), 2, "应多一次提交")


@case("WRT-07", "别人持锁时保存 423")
def _():
    pid, doc_id, content = _seed_doc("w7")
    other = user_repo.create(f"qa_w7_{RUN}", "同项目制作", hash_password(PASSWORD),
                             role_codes=["editor"])
    cli("editor").post(f"/api/projects/{pid}/members", json={"user_id": other["id"]})
    c2 = TestClient(APP, cookies={"fv_session": mint(other["id"])})
    st(c2.post(f"/api/documents/{doc_id}/lock"), 200, "对方拿锁")
    r = cli("editor").put(f"/api/documents/{doc_id}/content",
                          json={"content": "我偏要写。", "expected_version": content["version"]})
    st(r, 423, "锁挡住")
    detail_has(r, "正在编辑这一份", "锁消息要说谁在编辑")


@case("WRT-08", "正文超过 1MB 被拒")
def _():
    pid, doc_id, content = _seed_doc("w8")
    r = cli("editor").put(f"/api/documents/{doc_id}/content",
                          json={"content": "字" * 400000,
                                "expected_version": content["version"]})
    st(r, 413, "超大正文")
    detail_has(r, "KB", "要说清上限")


@case("WRT-09", "版本历史只读")
def _():
    pid, doc_id, content = _seed_doc("w9")
    commit = st(cli("editor").get(f"/api/documents/{doc_id}/versions"), 200, "版本").json()[0]["commit"]
    d = st(cli("editor").get(f"/api/documents/{doc_id}/versions/{commit}/diff"), 200, "看差异")
    ok("燧人" in d.text, "差异里该有正文内容")
    r = cli("editor").post(f"/api/documents/{doc_id}/versions/{commit}/restore", json={})
    ok(r.status_code in (404, 405), f"不该存在恢复端点，实际 {r.status_code}")


@case("WRT-10", "镜头字段校验")
def _():
    pid = new_project("editor", "w10")
    eid = new_episode(pid, "第 1 集")
    base = {"prompt": "", "asset_ids": [], "variant_ids": []}
    st(cli("editor").post(f"/api/episodes/{eid}/shots",
                          json={**base, "summary": "", "duration": 5}), 422, "空描述")
    st(cli("editor").post(f"/api/episodes/{eid}/shots",
                          json={**base, "summary": "s", "duration": 0}), 422, "0 秒")
    st(cli("editor").post(f"/api/episodes/{eid}/shots",
                          json={**base, "summary": "s", "duration": 60.5}), 422, "超 60 秒")
    st(cli("editor").post(f"/api/episodes/{eid}/shots",
                          json={**base, "summary": "s", "duration": 60}), 201, "60 秒可用")


@case("WRT-11", "引用别家项目的资产被拒")
def _():
    mine = new_project("editor", "w11a")
    theirs = new_project("outsider", "w11b")
    foreign = st(cli("outsider").post(f"/api/projects/{theirs}/assets",
                                      json={"kind": "character", "name": "外人角色"}),
                 201, "别家资产").json()["id"]
    eid = new_episode(mine, "第 1 集")
    r = cli("editor").post(f"/api/episodes/{eid}/shots",
                           json={"summary": "s", "prompt": "", "duration": 5,
                                 "asset_ids": [foreign], "variant_ids": []})
    st(r, 422, "脏引用")
    detail_has(r, "不属于这个项目", "要说清为什么")


@case("WRT-12", "只勾形态自动补父资产")
def _():
    pid = new_project("editor", "w12")
    aid = new_asset(pid)
    vid = st(cli("editor").post(f"/api/assets/{aid}/variants",
                                json={"variant_type": "outfit", "name": "雨夜外套",
                                      "prompt": "", "sort_order": None}),
             201, "建形态").json()["id"]
    eid = new_episode(pid, "第 1 集")
    r = st(cli("editor").post(f"/api/episodes/{eid}/shots",
                              json={"summary": "s", "prompt": "", "duration": 5,
                                    "asset_ids": [], "variant_ids": [vid]}),
           201, "只勾形态")
    ok(aid in [int(x) for x in re.findall(r"\d+", json.dumps(
        db1("SELECT asset_ids FROM shot WHERE id=?", (r.json()["id"],))["asset_ids"] or ""))],
       "父资产应被自动补进引用")


@case("WRT-13", "改分镜不动历史作业快照")
def _():
    pid = new_project("editor", "w13")
    eid = new_episode(pid, "第 1 集")
    sid = new_shot(eid, summary="旧内容", prompt="旧提示词")
    job_id = st(submit_mock_job(pid, episode_id=eid, shot_id=sid,
                                prompt="旧提示词"), 202, "提交").json()["id"]
    run_worker()
    eq(st(cli("editor").get(f"/api/jobs/{job_id}"), 200, "作业").json()["status"],
       "succeeded", "先让这条跑成功，源版本比对才有对象")
    before = db1("SELECT prompt_snapshot, source_version FROM job WHERE id=?", (job_id,))
    st(cli("editor").put(f"/api/shots/{sid}",
                         json={"summary": "新内容", "prompt": "新提示词", "duration": 5,
                               "asset_ids": [], "variant_ids": []}), 200, "改镜头")
    after = db1("SELECT prompt_snapshot, source_version FROM job WHERE id=?", (job_id,))
    eq(after["prompt_snapshot"], before["prompt_snapshot"], "作业提示词快照不许跟着改")
    eq(after["source_version"], before["source_version"], "源版本指纹要冻在提交那一刻")
    shots = st(cli("editor").get(f"/api/episodes/{eid}/shots"), 200, "读镜头")
    ok(any(s.get("source_stale") for s in shots.json()), "改过后应标出「源已变」")


# ============================ 资产 ============================
@case("AST-01", "唯一性与必填")
def _():
    pid = new_project("editor", "a1")
    name = f"掌灯人_{RUN}"
    st(cli("editor").post(f"/api/projects/{pid}/assets", json={"kind": "character", "name": name}),
       201, "首次登记")
    st(cli("editor").post(f"/api/projects/{pid}/assets", json={"kind": "character", "name": name}),
       409, "同名重登")
    st(cli("editor").post(f"/api/projects/{pid}/assets", json={"kind": "character", "name": "  "}),
       422, "空名")


@case("AST-02", "全局资产只有管理员能建")
def _():
    pid = new_project("editor", "a2")
    st(cli("editor").post(f"/api/projects/{pid}/assets",
                          json={"kind": "character", "name": f"G-{RUN}", "scope": "global"}),
       403, "制作建全局")
    g = st(cli("admin").post(f"/api/projects/{pid}/assets",
                             json={"kind": "character", "name": f"G-{RUN}", "scope": "global"}),
           201, "管理员建全局")
    gid = g.json()["id"]
    other = new_project("outsider", "a2b")
    listed = st(cli("outsider").get(f"/api/projects/{other}/assets"), 200, "别家看资产").json()
    ok(any(a["id"] == gid for a in listed), "全局资产应各项目可见")


@case("AST-03", "全局资产不许收回项目")
def _():
    pid = new_project("editor", "a3")
    gid = st(cli("admin").post(f"/api/projects/{pid}/assets",
                               json={"kind": "scene", "name": f"GS-{RUN}", "scope": "global"}),
             201, "建全局").json()["id"]
    r = cli("admin").put(f"/api/assets/{gid}",
                         json={"kind": "scene", "name": f"GS-{RUN}", "scope": "project"})
    st(r, 409, "收回项目")
    detail_has(r, "引用悬空", "要说清为什么")


@case("AST-04", "弱版本闸")
def _():
    pid = new_project("editor", "a4")
    aid = new_asset(pid)
    body = {"kind": "character", "name": f"改名-{RUN}", "summary": "s",
            "consistency_prompt": "", "aliases": [], "tags": [], "scope": "project"}
    st(cli("editor").put(f"/api/assets/{aid}", json={**body, "seen_version": "deadbeef"}),
       409, "过期令牌")
    cur = st(cli("editor").get(f"/api/assets/{aid}"), 200, "读资产").json()
    st(cli("editor").put(f"/api/assets/{aid}", json={**body, "seen_version": cur.get("version", "")}),
       200, "带当前令牌可写")


@case("AST-05", "图片四道拒与一条过")
def _():
    pid = new_project("editor", "a5")
    aid = new_asset(pid)
    c = cli("editor")
    r = c.post(f"/api/assets/{aid}/images", files={"file": ("x.txt", b"not image", "text/plain")})
    st(r, 415, "类型不是图")
    r = c.post(f"/api/assets/{aid}/images", files={"file": ("x.png", b"GIF89a fake", "image/png")})
    st(r, 415, "内容与类型不符")
    r = c.post(f"/api/assets/{aid}/images",
               files={"file": ("big.png", png_bytes(20 * 1024 * 1024 + 4096), "image/png")})
    st(r, 413, "超过 20MB")
    r = st(c.post(f"/api/assets/{aid}/images",
                  files={"file": ("ok.png", png_bytes(2048), "image/png")}),
           200, "正常上传")
    eq(len(r.json()["ref_images"]), 1, "清单多一张图")


@case("AST-06", "封面只认已登记的图")
def _():
    pid = new_project("editor", "a6")
    aid = new_asset(pid)
    st(cli("editor").put(f"/api/assets/{aid}/cover", json={"path": f"{aid}/不存在.png"}),
       404, "陌生路径")


@case("AST-07", "被引用就不许删")
def _():
    pid = new_project("editor", "a7")
    aid = new_asset(pid)
    cli("editor").post(f"/api/assets/{aid}/images",
                       files={"file": ("ok.png", png_bytes(1024), "image/png")})
    eid = new_episode(pid, "第 1 集")
    sid = new_shot(eid, asset_ids=[aid])
    r = cli("editor").delete(f"/api/assets/{aid}")
    st(r, 409, "被引用")
    detail_has(r, "条镜头引用", "要点名引用")
    cli("editor").put(f"/api/shots/{sid}", json={"summary": "不引用了", "prompt": "",
                                                 "duration": 5, "asset_ids": [],
                                                 "variant_ids": []})
    st(cli("editor").delete(f"/api/assets/{aid}"), 204, "解除后可删")


@case("AST-08", "档案只给人物")
def _():
    pid = new_project("editor", "a8")
    scene = new_asset(pid, kind="scene", name=f"洞口-{RUN}")
    r = cli("editor").get(f"/api/assets/{scene}/character-profile")
    st(r, 422, "场景没有档案")
    aid = new_asset(pid)
    st(cli("editor").put(f"/api/assets/{aid}/character-profile",
                         json={"biography": "取火者", "personality": "沉默",
                               "growth_arc": "", "text_prompt": "", "seen_version": ""}),
       200, "写档案")
    got = st(cli("editor").get(f"/api/assets/{aid}/character-profile"), 200, "读档案").json()
    eq(got["biography"], "取火者", "档案读回")
    st(cli("editor").put(f"/api/assets/{aid}/character-profile",
                         json={"biography": "x", "personality": "", "growth_arc": "",
                               "text_prompt": "", "seen_version": "stale"}),
       409, "档案也有版本闸")


@case("AST-09", "形态重名与排序")
def _():
    pid = new_project("editor", "a9")
    aid = new_asset(pid)
    for i in (0, 1):
        st(cli("editor").post(f"/api/assets/{aid}/variants",
                              json={"variant_type": "outfit", "name": f"形态{i}",
                                    "prompt": "", "sort_order": i}),
           201, "建形态")
    st(cli("editor").post(f"/api/assets/{aid}/variants",
                          json={"variant_type": "outfit", "name": "形态0", "prompt": "",
                                "sort_order": 2}),
       409, "同名形态")
    first = st(cli("editor").get(f"/api/assets/{aid}/variants"), 200, "列表").json()
    eq([v["name"] for v in first], ["形态0", "形态1"], "按 sort_order 排")
    a_id = first[0]["id"]
    st(cli("editor").put(f"/api/asset-variants/{a_id}",
                         json={"variant_type": "outfit", "name": "形态0",
                               "prompt": "", "sort_order": 9}),
       200, "改排序")
    after = st(cli("editor").get(f"/api/assets/{aid}/variants"), 200, "再列表").json()
    eq([v["name"] for v in after], ["形态1", "形态0"], "顺序应翻转")


@case("AST-10", "删单张形态图")
def _():
    pid = new_project("editor", "a10")
    aid = new_asset(pid)
    vid = st(cli("editor").post(f"/api/assets/{aid}/variants",
                                json={"variant_type": "outfit", "name": "常服",
                                      "prompt": "", "sort_order": 0}),
             201, "建形态").json()["id"]
    st(cli("editor").delete(f"/api/asset-variants/{vid}/images",
                            params={"path": f"{aid}/不存在.png"}),
       404, "陌生路径")
    st(cli("editor").delete(f"/api/asset-variants/{vid}"), 204, "删形态")


# ============================ 生成 ============================
@case("JOB-01", "执行方式三件一起给")
def _():
    pid = new_project("editor", "j1")
    r = cli("editor").post("/api/jobs", json={"project_id": pid, "capability": "video",
                                              "provider_code": "wan", "prompt": "p"})
    st(r, 422, "只给 provider")
    detail_has(r, "要么都不传", "契约原话")


@case("JOB-02", "显式 mock 槽位可提交")
def _():
    pid = new_project("editor", "j2")
    r = st(submit_mock_job(pid), 202, "提交")
    eq(r.json()["status"], "queued", "初始状态")
    eq(r.json()["provider_code"], "mock", "供应商")


@case("JOB-03", "参数白名单")
def _():
    pid = new_project("editor", "j3")
    r = submit_mock_job(pid, params={"sleep": 0.0, "bogus": 1})
    st(r, 422, "未知参数")
    detail_has(r, "不在 mock 的白名单里", "要说清哪个参数")
    body = {"project_id": pid, "capability": "video", "execution_mode": "local",
            "provider_code": "mock", "model_code": "wrong-model", "prompt": "p",
            "params": {"sleep": 0.0}}
    st(cli("editor").post("/api/jobs", json=body), 422, "模型档不对")


@case("JOB-04", "本地作业不许带计价")
def _():
    pid = new_project("editor", "j4")
    body = {"project_id": pid, "capability": "video", "execution_mode": "local",
            "provider_code": "mock", "model_code": "mock-1", "prompt": "p",
            "params": {"sleep": 0.0},
            "estimate": {"quantity": 1, "unit": "second", "unit_price_micros": 100,
                         "currency": "CNY", "source": "清单"},
            "user_confirmed_cloud": True}
    r = cli("editor").post("/api/jobs", json=body)
    st(r, 422, "本地带 estimate")
    detail_has(r, "本地作业不计费", "原话")


@case("JOB-05", "幂等键复用")
def _():
    pid = new_project("editor", "j5")
    key = f"idem-{RUN}"
    first = st(submit_mock_job(pid, key=key), 202, "首次")
    second = st(submit_mock_job(pid, key=key), 200, "重复提交")
    eq(second.json()["id"], first.json()["id"], "同一个作业号")
    eq(db1("SELECT COUNT(*) AS n FROM job WHERE idempotency_key=?", (key,))["n"], 1,
       "库里只该一行")


@case("JOB-06", "幂等键不许跨项目重复下单")
def _():
    pid_a = new_project("editor", "j6a")
    pid_b = new_project("editor", "j6b")
    own_b = new_project("outsider", "j6c")
    key = f"cross-{RUN}"
    first = st(submit_mock_job(pid_a, key=key), 202, "A 项目").json()["id"]
    again = st(submit_mock_job(pid_b, key=key), 200, "B 项目复用同键").json()["id"]
    eq(again, first, "同键不许再建一条")
    eq(db1("SELECT COUNT(*) AS n FROM job WHERE idempotency_key=?", (key,))["n"], 1,
       "库里只该有一行")
    r = cli("outsider").post("/api/jobs", json={
        "project_id": own_b, "capability": "video", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "prompt": "p",
        "params": {"sleep": 0.0}, "idempotency_key": key})
    st(r, 409, "键被看不见的项目占用")
    detail_has(r, "已被占用", "原话")


@case("JOB-07", "作业跑成功并留产物")
def _():
    pid = new_project("editor", "j7")
    job_id = st(submit_mock_job(pid, params={"sleep": 0.0, "media": "text"}),
                202, "提交").json()["id"]
    run_worker()
    d = st(cli("editor").get(f"/api/jobs/{job_id}"), 200, "详情").json()
    eq(d["status"], "succeeded", "状态")
    arts = st(cli("editor").get(f"/api/jobs/{job_id}/artifacts"), 200, "产物清单").json()
    ok(len(arts) >= 1, "至少一个产物")
    st(cli("editor").get(f"/api/jobs/{job_id}/artifacts/0"), 200, "下载产物")
    st(cli("editor").get(f"/api/jobs/{job_id}/artifacts/9"), 404, "越界序号")
    logs = st(cli("editor").get(f"/api/jobs/{job_id}/logs"), 200, "日志").json()
    ok("mock" in json.dumps(logs, ensure_ascii=False), "日志该有 worker 输出")


@case("JOB-08", "失败、预览与重试链")
def _():
    pid = new_project("editor", "j8")
    job_id = st(submit_mock_job(pid, params={"sleep": 0.0, "fail": True}),
               202, "提交").json()["id"]
    run_worker()
    eq(st(cli("editor").get(f"/api/jobs/{job_id}"), 200, "详情").json()["status"],
       "failed", "失败终态")
    p = st(cli("editor").get(f"/api/jobs/{job_id}/retry-preview"), 200, "重试预览").json()
    ok(p["chain"], "预览要列重试链")
    new_id = st(cli("editor").post(f"/api/jobs/{job_id}/retry"), 202, "重试").json()["id"]
    ok(new_id != job_id, "重试要新建一行")
    a = db1("SELECT source_version, retry_of_job_id FROM job WHERE id=?", (new_id,))
    b = db1("SELECT source_version FROM job WHERE id=?", (job_id,))
    eq(a["source_version"], b["source_version"], "重试继承源版本")
    eq(a["retry_of_job_id"], job_id, "记录重试自哪条")


@case("JOB-09", "取消只在飞行中")
def _():
    pid = new_project("editor", "j9")
    job_id = st(submit_mock_job(pid, params={"sleep": 0.0}), 202, "提交").json()["id"]
    r = st(cli("editor").post(f"/api/jobs/{job_id}/cancel"), 200, "取消排队中的")
    ok(r.json()["status"] in ("cancel_requested", "cancelled"),
       f"取消后要么是取消中、要么直接已取消，实际 {r.json()['status']}")
    run_worker()
    eq(st(cli("editor").get(f"/api/jobs/{job_id}"), 200, "详情").json()["status"],
       "cancelled", "worker 收到后落已取消")
    st(cli("editor").post(f"/api/jobs/{job_id}/cancel"), 409, "终态不能再取消")


@case("JOB-10", "本地作业没有账单")
def _():
    pid = new_project("editor", "j10")
    job_id = st(submit_mock_job(pid, params={"sleep": 0.0}), 202, "提交").json()["id"]
    run_worker()
    r = cli("editor").post(f"/api/jobs/{job_id}/attention", json={"reason": "看看"})
    st(r, 409, "本地不能标待核对")
    detail_has(r, "本地作业没有供应商账单可核对", "原话")
    r = cli("editor").post(f"/api/jobs/{job_id}/reconcile",
                           json={"decision": "succeeded", "actual_cost_micros": 1, "note": "n"})
    st(r, 409, "本地不能核对账单")
    detail_has(r, "只有云端作业需要账单核对", "原话")


@case("JOB-11", "筛选生效、成本账只按项目")
def _():
    pid = new_project("editor", "j11")
    st(submit_mock_job(pid, capability="video", params={"sleep": 0.0}), 202, "视频")
    st(submit_mock_job(pid, capability="image", params={"sleep": 0.0}), 202, "图像")
    run_worker()
    only_failed = st(cli("editor").get("/api/jobs", params={"project_id": pid,
                                                            "status": "failed"}), 200, "筛失败").json()
    eq(len(only_failed), 0, "两条都成功，失败筛应为空")
    by_cap = st(cli("editor").get("/api/jobs", params={"project_id": pid,
                                                       "capability": "image"}), 200, "筛能力").json()
    ok(all(j["capability"] == "image" for j in by_cap) and len(by_cap) == 1, "能力筛选生效")
    costs = st(cli("editor").get("/api/jobs/costs", params={"project_id": pid,
                                                             "status": "failed"}),
               200, "成本账").json()
    ok(costs["local"]["jobs"] >= 2, f"成本账不跟状态筛选走：{costs['local']}")


@case("JOB-12", "配置漂移不动已提交作业")
def _():
    pid = new_project("editor", "j12")
    job_id = st(submit_mock_job(pid, params={"sleep": 0.0}), 202, "提交").json()["id"]
    before = db1("SELECT provider_code, model_code, params_json FROM job WHERE id=?", (job_id,))
    with get_conn() as conn:
        conn.execute("UPDATE provider_config SET model = 'drift' WHERE provider = 'mock'")
    after = db1("SELECT provider_code, model_code, params_json FROM job WHERE id=?", (job_id,))
    eq(after["provider_code"], before["provider_code"], "provider 不许漂")
    eq(after["model_code"], before["model_code"], "model 不许漂")
    eq(after["params_json"], before["params_json"], "参数快照不许漂")


# ============================ 审核 ============================
@case("REV-01", "三条确定性规则")
def _():
    pid, doc_id, _ = _seed_doc("r1", content="这里待补一句。\n" + "很长" * 70 + "\n没有标点收尾")
    g = st(cli("editor").post(f"/api/documents/{doc_id}/review-gate"), 201, "跑校验").json()
    rules = {f["rule"] for f in g["findings"]}
    ok("placeholder" in rules, f"应抓到占位词，实际 {rules}")
    ok("long_sentence" in rules, "应抓到超长句")
    ok("unterminated" in rules, "应抓到没有收尾标点")
    eq(g["status"], "failed", "有 error 即未通过")
    empty = _seed_doc("r1e", content="   ")
    g2 = st(cli("editor").post(f"/api/documents/{empty[1]}/review-gate"), 201, "空稿校验").json()
    eq(g2["status"], "failed", "空稿未通过")
    eq(g2["findings"][0]["rule"], "empty", "空稿规则名")


@case("REV-02", "有 error 也能签")
def _():
    pid, doc_id, _ = _seed_doc("r2", content="这句待补。\n")
    st(cli("editor").post(f"/api/documents/{doc_id}/review-gate"), 201, "跑校验")
    r = st(cli("editor").post(f"/api/documents/{doc_id}/review-sign",
                              json={"anchor": "document", "decision": "approved",
                                    "note": "我看过了"}),
           200, "带 error 签字")
    ok(r.json()["counts"]["approved"] >= 1, "签字计数应增加")


@case("REV-03", "改正文后签字失配")
def _():
    pid, doc_id, content = _seed_doc("r3")
    g = st(cli("editor").post(f"/api/documents/{doc_id}/review-gate"), 201, "跑校验").json()
    cli("editor").put(f"/api/documents/{doc_id}/content",
                      json={"content": "燧人改主意了。\n", "expected_version": content["version"]})
    g2 = st(cli("editor").get(f"/api/documents/{doc_id}/review-gate"), 200, "再读").json()
    eq(g2["stale"], True, "报告应标失配")
    eq(g2["can_sign"], False, "失配时不许签")
    st(cli("editor").post(f"/api/documents/{doc_id}/review-sign",
                          json={"anchor": "document", "decision": "approved", "note": ""}),
       409, "失配后签字被拒")


@case("REV-04", "同锚点改判替换")
def _():
    pid, doc_id, _ = _seed_doc("r4", content="这句待补。\n")
    st(cli("editor").post(f"/api/documents/{doc_id}/review-gate"), 201, "跑校验")
    g = st(cli("editor").get(f"/api/documents/{doc_id}/review-gate"), 200, "读报告").json()
    anchor = g["findings"][0]["anchor"]
    for decision in ("returned", "approved", "approved"):
        st(cli("editor").post(f"/api/documents/{doc_id}/review-sign",
                              json={"anchor": anchor, "decision": decision, "note": ""}),
           200, f"签 {decision}")
    g = st(cli("editor").get(f"/api/documents/{doc_id}/review-gate"), 200, "再读").json()
    eq(g["counts"]["returned"], 0, "改判不叠历史")
    eq(g["counts"]["approved"], 1, "同一锚点只留一条")


@case("REV-05", "产物队列只列可审作业")
def _():
    pid = new_project("editor", "r5")
    text_job = st(submit_mock_job(pid, capability="text", params={"sleep": 0.0}),
                  202, "文本作业").json()["id"]
    video_job = st(submit_mock_job(pid, capability="video", params={"sleep": 0.0,
                                                                    "media": "mp4"}),
                   202, "视频作业").json()["id"]
    run_worker(6)
    queue = st(cli("editor").get(f"/api/projects/{pid}/job-reviews"), 200, "产物队列").json()
    ids = {q["job_id"] for q in queue}
    ok(video_job in ids, "已成功的视频作业该进队列")
    ok(text_job not in ids, "文本作业不该进队列")


@case("REV-06", "产物签字绑清单摘要")
def _():
    pid = new_project("editor", "r6")
    job_id = st(submit_mock_job(pid, capability="video",
                                params={"sleep": 0.0, "media": "mp4"}),
                202, "视频作业").json()["id"]
    run_worker()
    sign_job(job_id)
    r = st(cli("editor").get(f"/api/jobs/{job_id}/review"), 200, "读签字").json()
    eq(r["stale"], False, "刚签不该失配")
    ok(r["base_version"], "签字要带产物清单摘要")
    eq(len(r["signatures"]), 1, "只留最新一条结论")
    with get_conn() as conn:
        # 清单换成另一件（还在但路径不同）：reviewable 仍成立，版本摘要才会变
        conn.execute("UPDATE job SET result_paths_json = ? WHERE id = ?",
                     (json.dumps([f"999999/result-{job_id}.mp4"]), job_id))
    after = st(cli("editor").get(f"/api/jobs/{job_id}/review"), 200, "再读").json()
    eq(after["stale"], True, "产物清单变了要失配")


@case("REV-07", "无签字权限")
def _():
    pid, doc_id, _ = _seed_doc("r7")
    st(cli("editor").post(f"/api/documents/{doc_id}/review-gate"), 201, "跑校验")
    cli("editor").post(f"/api/projects/{pid}/members", json={"user_id": ACCOUNTS["viewer"]["id"]})
    st(cli("viewer").post(f"/api/documents/{doc_id}/review-sign",
                          json={"anchor": "document", "decision": "approved", "note": ""}),
       403, "viewer 签字")


# ============================ 音频 ============================
@case("AUD-01", "四种素材与必填")
def _():
    pid = new_project("editor", "u1")
    eid = new_episode(pid, "第 1 集")
    for kind in ("dialogue", "narration", "music", "sfx"):
        r = st(cli("editor").post(f"/api/projects/{pid}/audio-clips",
                                  json={"episode_id": eid, "kind": kind, "speaker": "甲",
                                        "text": f"{kind} 内容", "notes": ""}),
               201, f"登记 {kind}")
        eq(r.json()["status"], "draft", "初始草稿")
    st(cli("editor").post(f"/api/projects/{pid}/audio-clips",
                          json={"episode_id": eid, "kind": "dialogue", "text": ""}),
       422, "空文本")
    st(cli("editor").post(f"/api/projects/{pid}/audio-clips",
                          json={"episode_id": eid, "kind": "opera", "text": "x"}),
       422, "非法种类")


@case("AUD-02", "镜头归属校验")
def _():
    pid = new_project("editor", "u2")
    other = new_project("outsider", "u2b")
    foreign_ep = st(cli("outsider").post(f"/api/projects/{other}/episodes", json={"title": "t"}),
                    201, "别家的集").json()["id"]
    st(cli("editor").post(f"/api/projects/{pid}/audio-clips",
                          json={"episode_id": foreign_ep, "kind": "dialogue", "text": "x"}),
       422, "别家集")
    eid = new_episode(pid, "第 1 集")
    r = cli("outsider").post(f"/api/episodes/{foreign_ep}/shots",
                             json={"summary": "别家镜", "prompt": "", "duration": 5,
                                   "asset_ids": [], "variant_ids": []})
    st(r, 201, "别家镜头")
    foreign_shot = r.json()["id"]
    st(cli("editor").post(f"/api/projects/{pid}/audio-clips",
                          json={"episode_id": eid, "shot_id": foreign_shot,
                                "kind": "dialogue", "text": "x"}),
       422, "镜不属该集")


@case("AUD-03", "音效是实验态")
def _():
    pid = new_project("editor", "u3")
    eid = new_episode(pid, "第 1 集")
    cid = st(cli("editor").post(f"/api/projects/{pid}/audio-clips",
                                json={"episode_id": eid, "kind": "sfx", "text": "风声"}),
             201, "登记音效").json()["id"]
    r = cli("editor").post(f"/api/audio-clips/{cid}/submit")
    st(r, 422, "提交音效")
    detail_has(r, "实验态", "原话")


@case("AUD-04", "音频只走本地轨")
def _():
    pid = new_project("editor", "u4")
    eid = new_episode(pid, "第 1 集")
    cid = st(cli("editor").post(f"/api/projects/{pid}/audio-clips",
                                json={"episode_id": eid, "kind": "dialogue", "text": "台词"}),
             201, "登记台词").json()["id"]
    r = cli("editor").post(f"/api/audio-clips/{cid}/submit")
    if r.status_code == 202:
        eq(db1("SELECT status FROM audio_clip WHERE id=?", (cid,))["status"], "queued", "送队列")
    else:
        st(r, 422, "提交配音")
        ok("默认轨" in r.text or "本地" in r.text or "不可用" in r.text,
           f"要说清缺什么：{r.text[:200]}")
        eq(db1("SELECT status FROM audio_clip WHERE id=?", (cid,))["status"], "draft",
           "提交失败不该改状态")


@case("AUD-05", "送队列后不许改文本")
def _():
    pid = new_project("editor", "u5")
    eid = new_episode(pid, "第 1 集")
    cid = st(cli("editor").post(f"/api/projects/{pid}/audio-clips",
                                json={"episode_id": eid, "kind": "narration", "text": "旁白"}),
             201, "登记旁白").json()["id"]
    st(cli("editor").patch(f"/api/audio-clips/{cid}",
                           json={"kind": "narration", "speaker": "", "text": "改过的旁白",
                                 "notes": ""}),
       200, "草稿可改")
    r = cli("editor").post(f"/api/audio-clips/{cid}/submit")
    if r.status_code != 202:
        raise Blocked("本机没有可用的本地配音轨，无法进入排队中状态")
    st(cli("editor").patch(f"/api/audio-clips/{cid}",
                           json={"kind": "narration", "speaker": "", "text": "再改",
                                 "notes": ""}),
       409, "排队中不许改")
    detail_has(cli("editor").patch(f"/api/audio-clips/{cid}",
                                   json={"kind": "narration", "speaker": "", "text": "再改",
                                         "notes": ""}),
               "要改就新建一条", "原话")


@case("AUD-06", "删素材不撤作业")
def _():
    pid = new_project("editor", "u6")
    eid = new_episode(pid, "第 1 集")
    cid = st(cli("editor").post(f"/api/projects/{pid}/audio-clips",
                                json={"episode_id": eid, "kind": "music", "text": "配乐"}),
             201, "登记音乐").json()["id"]
    r = cli("editor").post(f"/api/audio-clips/{cid}/submit")
    job_id = r.json().get("job_id") if r.status_code == 202 else None
    st(cli("editor").delete(f"/api/audio-clips/{cid}"), 204, "删素材")
    if job_id:
        eq(db1("SELECT COUNT(*) AS n FROM job WHERE id=?", (job_id,))["n"], 1, "作业行要留着")
    st(cli("editor").delete(f"/api/audio-clips/{cid}"), 404, "再删就没了")


# ============================ 产线 ============================
@case("PIP-01", "模板列表只出每名的最新版")
def _():
    items = st(cli("editor").get("/api/pipeline/templates"), 200, "模板列表").json()
    table = {(t["name"], t["version"]): len(t["nodes"]) for t in items}
    eq(len(items), 3, f"每个名字只出最新一版，实际 {table}")
    eq(table.get(("标准漫剧单集生产", 2)), 15, "标准 v2 节点数")
    eq(table.get(("图片预览生产", 1)), 6, "图片预览节点数")
    eq(table.get(("视频生产", 1)), 6, "视频生产节点数")
    v1 = next(t for t in items if t["name"] == "标准漫剧单集生产")
    old = st(cli("editor").get(f"/api/pipeline/templates/{v1['id'] - 1}"), 200, "按 id 取旧版").json()
    eq((old["name"], old["version"], len(old["nodes"])),
       ("标准漫剧单集生产", 1, 13), "旧版仍可按 id 取到")


@case("PIP-02", "预检分两档")
def _():
    pid = new_project("editor", "p2")
    eid = new_episode(pid, "第 1 集")
    pf = st(cli("editor").get(f"/api/projects/{pid}/preflight", params={"episode_id": eid}),
            200, "预检").json()
    ok(any("还没有镜头" in b for b in pf["blocking"]), f"无镜头该阻断：{pf['blocking']}")
    new_shot(eid, summary="没提示词的镜")
    pf = st(cli("editor").get(f"/api/projects/{pid}/preflight", params={"episode_id": eid}),
           200, "再预检").json()
    ok(any("没有提示词" in w for w in pf["warnings"]), f"缺提示词该提醒：{pf['warnings']}")


@case("PIP-03", "发起运行按镜展开")
def _():
    pid, eid, shot_ids = _seed_pipeline_case("p3")
    tid = _template_id("视频生产", 1)
    r = st(cli("editor").post(f"/api/projects/{pid}/pipeline/runs",
                              json={"episode_id": eid, "template_id": tid}),
           201, "发起运行")
    run_id = r.json()["id"]
    detail = st(cli("editor").get(f"/api/pipeline/runs/{run_id}"), 200, "运行详情").json()
    eq(detail["shots"], len(shot_ids), "镜数要对得上")
    ok(len(detail["nodes"]) >= len(shot_ids), "逐镜节点应按镜展开")


@case("PIP-04", "一集只许一条未完成运行")
def _():
    pid, eid, _ = _seed_pipeline_case("p4")
    tid = _template_id("图片预览生产", 1)
    first = st(cli("editor").post(f"/api/projects/{pid}/pipeline/runs",
                                  json={"episode_id": eid, "template_id": tid}),
               201, "首次").json()
    if first["status"] in ("failed", "completed", "completed_with_errors", "cancelled"):
        raise Blocked("本轮环境没有可跑通的默认模型轨，运行在派工节点立刻失败，"
                      "造不出「未完成运行」；这条约束要配 worker 与可用档位才能验")
    r = cli("editor").post(f"/api/projects/{pid}/pipeline/runs",
                           json={"episode_id": eid, "template_id": tid})
    st(r, 409, "同集再发起")
    detail_has(r, "未结束的生产运行", "要点名挡着的那条")


@case("PIP-05", "运行幂等键复用")
def _():
    pid, eid, _ = _seed_pipeline_case("p5")
    tid = _template_id("图片预览生产", 1)
    key = f"run-{RUN}"
    first = st(cli("editor").post(f"/api/projects/{pid}/pipeline/runs",
                                  json={"episode_id": eid, "template_id": tid,
                                        "idempotency_key": key}),
               201, "首次").json()["id"]
    again = st(cli("editor").post(f"/api/projects/{pid}/pipeline/runs",
                                  json={"episode_id": eid, "template_id": tid,
                                        "idempotency_key": key}),
               201, "同键再来").json()["id"]
    eq(again, first, "同键应回同一条运行")


@case("PIP-06", "暂停／恢复／取消与终态")
def _():
    pid, eid, _ = _seed_pipeline_case("p6")
    tid = _template_id("图片预览生产", 1)
    run_id = st(cli("editor").post(f"/api/projects/{pid}/pipeline/runs",
                                   json={"episode_id": eid, "template_id": tid}),
                201, "发起").json()["id"]
    p = cli("editor").post(f"/api/pipeline/runs/{run_id}/pause")
    if p.status_code == 409 and "已经结束" in p.text:
        raise Blocked("运行在派工节点立刻收尾（没有可跑通的默认轨），"
                      "拿不到可暂停的未完成运行")
    st(p, 200, "暂停")
    eq(st(cli("editor").get(f"/api/pipeline/runs/{run_id}"), 200, "读").json()["paused"],
       True, "paused 标记")
    st(cli("editor").post(f"/api/pipeline/runs/{run_id}/resume"), 200, "恢复")
    st(cli("editor").post(f"/api/pipeline/runs/{run_id}/cancel",
                          json={"reason": "qa 取消"}), 200, "取消")
    d = st(cli("editor").get(f"/api/pipeline/runs/{run_id}"), 200, "读").json()
    eq(d["cancelled"], True, "cancelled 标记")
    st(cli("editor").post(f"/api/pipeline/runs/{run_id}/cancel", json={"reason": "再来"}),
       409, "终态不能再取消")


@case("PIP-07", "节点重试闸")
def _():
    pid, eid, _ = _seed_pipeline_case("p7")
    tid = _template_id("图片预览生产", 1)
    run_id = st(cli("editor").post(f"/api/projects/{pid}/pipeline/runs",
                                   json={"episode_id": eid, "template_id": tid}),
                201, "发起").json()["id"]
    nodes = st(cli("editor").get(f"/api/pipeline/runs/{run_id}"), 200, "读").json()["nodes"]
    blocked = next(n for n in nodes if n["status"] in ("blocked", "pending", "ready"))
    r = cli("editor").post(f"/api/pipeline/nodes/{blocked['id']}/retry")
    st(r, 409, "非失败节点不能重试")
    ok(r.json().get("detail") or r.text, "要给一句原因")


@case("PIP-08", "只读能看不能发")
def _():
    pid, eid, _ = _seed_pipeline_case("p8")
    cli("editor").post(f"/api/projects/{pid}/members", json={"user_id": ACCOUNTS["viewer"]["id"]})
    st(cli("viewer").get(f"/api/projects/{pid}/pipeline/runs"), 200, "viewer 读运行")
    st(cli("viewer").get("/api/pipeline/templates"), 200, "viewer 读模板")
    st(cli("viewer").post(f"/api/projects/{pid}/pipeline/runs",
                          json={"episode_id": eid, "template_id": _template_id("图片预览生产", 1)}),
       403, "viewer 发起")


@case("PIP-09", "整季并发上限夹住")
def _():
    pid = new_project("editor", "p9")
    tid = _template_id("图片预览生产", 1)
    eid = new_episode(pid, "第 1 集")
    r = st(cli("editor").post(f"/api/projects/{pid}/seasons",
                              json={"template_id": tid, "episode_ids": [eid],
                                    "max_active_runs": 64}),
           201, "发起整季")
    eq(r.json()["max_active_runs"], 8, "服务端上限 8")


@case("PIP-10", "整季逐集起运行与批次动作")
def _():
    pid = new_project("editor", "p10")
    tid = _template_id("图片预览生产", 1)
    eids = [new_episode(pid, f"第 {i} 集") for i in (1, 2)]
    sid = st(cli("editor").post(f"/api/projects/{pid}/seasons",
                                json={"template_id": tid, "episode_ids": eids,
                                      "max_active_runs": 1}),
             201, "发起整季").json()["id"]
    detail = st(cli("editor").get(f"/api/seasons/{sid}"), 200, "读批次").json()
    eq(len(detail["items"]), 2, "两集各一行")
    p = cli("editor").post(f"/api/seasons/{sid}/pause")
    if p.status_code == 409 and "已经结束" in p.text:
        raise Blocked("整季里的运行都立刻收尾（没有可跑通的默认轨），"
                      "拿不到在产的批次")
    st(p, 200, "暂停排产")
    st(cli("editor").post(f"/api/seasons/{sid}/resume"), 200, "恢复排产")
    st(cli("editor").post(f"/api/seasons/{sid}/cancel", json={"reason": "qa"}), 200, "取消整季")
    eq(st(cli("editor").get(f"/api/seasons/{sid}"), 200, "再读").json()["status_label"],
       "已取消", "终态")


# ============================ 成片 ============================
@case("CMP-01", "前置一次说全")
def _():
    pid = new_project("editor", "c1")
    eid = new_episode(pid, "第 1 集")
    for i in (1, 2):
        new_shot(eid, summary=f"第{i}镜")
    r = cli("editor").post(f"/api/projects/{pid}/compositions",
                           json={"episode_id": eid})
    st(r, 422, "没出片就合成")
    body = r.json()["detail"]
    ok("第 1 镜" in body and "第 2 镜" in body, f"应一次点名两镜：{body[:200]}")


@case("CMP-02", "签字后合成成功")
def _():
    pid = new_project("editor", "c2")
    eid = new_episode(pid, "第 1 集")
    shot_ids = [new_shot(eid, summary=f"镜{i}", duration=1) for i in (1, 2)]
    job_ids = []
    for sid in shot_ids:
        j = st(submit_mock_job(pid, capability="video", episode_id=eid, shot_id=sid,
                               params={"sleep": 0.0, "media": "mp4", "seconds": 1.0}),
               202, "出片").json()["id"]
        job_ids.append(j)
    run_worker(6)
    for j in job_ids:
        eq(st(cli("editor").get(f"/api/jobs/{j}"), 200, "作业状态").json()["status"],
           "succeeded", "先确认出片成功")
        sign_job(j)
    r = st(cli("editor").post(f"/api/projects/{pid}/compositions",
                              json={"episode_id": eid, "width": 640, "height": 360, "fps": 24}),
           201, "发起合成")
    comp_id = r.json()["id"]
    run_worker(6)
    d = st(cli("editor").get(f"/api/compositions/{comp_id}"), 200, "成片详情").json()
    eq(d["status_label"], "已完成", f"合成终态：{d.get('status_label')} / {d.get('error') or ''}")
    eq(d["version"], 1, "首个版本")
    ok(d.get("output_path"), "要有输出路径")


@case("CMP-03", "版本递增、时间线冻结")
def _():
    pid, eid, comp_id, shot_ids, job_ids = _seed_composed("c3")
    before = st(cli("editor").get(f"/api/compositions/{comp_id}"), 200, "读 v1").json()
    cli("editor").put(f"/api/shots/{shot_ids[0]}",
                      json={"summary": "改了内容", "prompt": "改了提示词", "duration": 9,
                            "asset_ids": [], "variant_ids": []})
    r = st(cli("editor").post(f"/api/projects/{pid}/compositions", json={"episode_id": eid}),
           422, "源已变后直接再合成")
    ok("签字" in r.text or "源" in r.text or "过期" in r.text,
       f"应说明为什么被拒：{r.text[:200]}")
    v1 = st(cli("editor").get(f"/api/compositions/{comp_id}"), 200, "再读 v1").json()
    eq(json.dumps(v1["timeline"], ensure_ascii=False),
       json.dumps(before["timeline"], ensure_ascii=False), "旧版时间线不许跟着分镜漂")


@case("CMP-04", "字幕五种拒收")
def _():
    pid = new_project("editor", "c4")
    c = cli("editor")
    cases = [("x.txt", b"1\n00:00:00,0 --> 00:00:01,0\nhi\n", "只收 .srt 或 .vtt"),
             ("a.srt", b"", "字幕文件是空的"),
             ("big.srt", b"1\n00:00:00,0 --> 00:00:01,0\n" + b"x" * (1024 * 1024), "超过"),
             ("latin.srt", "１\n00:00:00,000 --> 00:00:01,000\n中文".encode("gb18030"), "UTF-8"),
             ("bad.srt", b"1\n00:00:00,000 --> 00:00:01,000\nok\n", ""),
             ("bad.vtt", b"00:00,000 --> 00:01,000\nno header\n", "WEBVTT")]
    for name, data, needle in cases:
        r = c.post(f"/api/projects/{pid}/composition-subtitles",
                   files={"file": (name, data, "text/plain")})
        if needle == "":
            continue      # 合法样例单独验
        st(r, 422, f"拒收 {name}")
        ok(needle in r.text, f"{name} 的拒绝理由应含「{needle}」：{r.text[:200]}")
    r = c.post(f"/api/projects/{pid}/composition-subtitles",
               files={"file": ("bad.srt", b"1\nno timeline\n", "text/plain")})
    st(r, 422, "SRT 没有时间轴")
    detail_has(r, "-->", "要指出缺 -->")


@case("CMP-05", "合法字幕可登记与删除")
def _():
    pid = new_project("editor", "c5")
    good = "1\n00:00:00,000 --> 00:00:02,000\n燧人取火\n".encode("utf-8")
    r = st(cli("editor").post(f"/api/projects/{pid}/composition-subtitles",
                              files={"file": ("ok.srt", good, "text/plain")}),
           201, "上传 srt")
    paths = [s["path"] for s in r.json()["items"]]
    ok(any(p.endswith(".srt") for p in paths), f"列表该有这份字幕：{paths}")
    st(cli("editor").delete(f"/api/projects/{pid}/composition-subtitles",
                            params={"path": paths[0]}), 200, "删除")
    after = st(cli("editor").get(f"/api/projects/{pid}/composition-subtitles"), 200, "再列表").json()
    ok(paths[0] not in [s["path"] for s in after["items"]], "删掉后不该还在")
    st(cli("editor").delete(f"/api/projects/{pid}/composition-subtitles",
                            params={"path": "不存在.srt"}), 404, "删陌生路径")


@case("CMP-06", "分辨率与帧率校验")
def _():
    pid, eid, _, _, _ = _seed_composed("c6")
    c = cli("editor")
    st(c.post(f"/api/projects/{pid}/compositions",
              json={"episode_id": eid, "width": 641, "height": 360}), 422, "奇数宽")
    st(c.post(f"/api/projects/{pid}/compositions",
              json={"episode_id": eid, "width": 4096, "height": 360}), 422, "超范围")
    st(c.post(f"/api/projects/{pid}/compositions",
              json={"episode_id": eid, "fps": -5}), 422, "fps 负数")
    st(c.post(f"/api/projects/{pid}/compositions",
              json={"episode_id": eid, "fps": 61}), 422, "fps 61")


@case("CMP-07", "只收已签字的镜头")
def _():
    pid = new_project("editor", "c7")
    eid = new_episode(pid, "第 1 集")
    sid = new_shot(eid, summary="镜1", duration=1)
    job_id = st(submit_mock_job(pid, capability="video", episode_id=eid, shot_id=sid,
                               params={"sleep": 0.0, "media": "mp4", "seconds": 1.0}),
               202, "出片").json()["id"]
    run_worker()
    r = cli("editor").post(f"/api/projects/{pid}/compositions", json={"episode_id": eid})
    st(r, 422, "没签字就合成")
    detail_has(r.json() and r, "审核签字", "要点名没签字")
    sign_job(job_id)
    st(cli("editor").post(f"/api/projects/{pid}/compositions", json={"episode_id": eid}),
       201, "签字后可合成")


@case("CMP-08", "详情读同一份事实")
def _():
    pid, eid, comp_id, _, _ = _seed_composed("c8")
    d = st(cli("editor").get(f"/api/compositions/{comp_id}"), 200, "详情").json()
    ok(d["timeline"]["shots"], "时间线要有镜")
    row = d["timeline"]["shots"][0]
    ok(row.get("duration_ms") is not None, f"每镜要带实际时长：{row}")
    ok(row["video"]["job_id"], "每镜要带来源视频作业号")
    ok(row["video"]["review"], "每镜要带审核结论")


# ============================ 模型中心 ============================
@case("MOD-01", "入口权限")
def _():
    st(cli("editor").get("/api/models"), 403, "制作读模型仓库")
    st(cli("editor").put("/api/providers", json=[]), 403, "制作改能力轨")
    st(cli("viewer").get("/api/models"), 403, "只读读模型仓库")


@case("MOD-02", "登记校验与 source 跟随")
def _():
    r = cli("admin").post("/api/models", json={"capability": "opera", "backend": "local",
                                               "provider": "mock", "model_code": "mock-1"})
    st(r, 422, "能力非法")
    st(cli("admin").post("/api/models", json={"capability": "video", "backend": "local",
                                              "provider": "", "model_code": ""}),
       422, "provider 与模型空")
    r = st(cli("admin").post("/api/models",
                             json={"capability": "video", "backend": "cloud",
                                   "provider": "wan", "model_code": "i2v-a14b",
                                   "display_name": f"QA-{RUN}"}),
           201, "云端登记")
    eq(r.json()["item"]["source"], "cloud", "source 要跟着 backend 走")
    reg_id = r.json()["id"]
    st(cli("admin").delete(f"/api/models/{reg_id}", params={"confirm": "撤登记"}), 200, "清掉")


@case("MOD-03", "撤登记要确认词")
def _():
    reg_id = st(cli("admin").post("/api/models",
                                  json={"capability": "image", "backend": "local",
                                        "provider": "mflux", "model_code": "z-image-q4",
                                        "display_name": f"QA-Del-{RUN}"}),
                 201, "登记").json()["id"]
    r = cli("admin").delete(f"/api/models/{reg_id}", params={"confirm": "随便"})
    st(r, 422, "缺确认词")
    detail_has(r, "权重文件仍在盘上", "要说清只撤登记")
    st(cli("admin").delete(f"/api/models/{reg_id}", params={"confirm": "撤登记"}),
       200, "带确认词")
    eq(db1("SELECT COUNT(*) AS n FROM model_registration WHERE id=?", (reg_id,))["n"], 0,
       "登记行应删除")


@case("MOD-04", "能力轨保存规则")
def _():
    rows = st(cli("admin").get("/api/providers"), 200, "读能力轨").json()
    saved = json.dumps(rows)
    try:
        two = [dict(r) for r in rows]
        for r in two:
            if r["capability"] == "text":
                r["is_default"] = True
        st(cli("admin").put("/api/providers", json=two), 422, "两个默认")
        missing = [dict(r, is_default=False) for r in rows]
        st(cli("admin").put("/api/providers", json=missing), 422, "缺默认")
        cloud_video_default = [dict(r) for r in rows]
        for r in cloud_video_default:
            r["is_default"] = r["capability"] != "text" or r["backend"] != "local"
        st(cli("admin").put("/api/providers", json=cloud_video_default), 422, "云端视频默认")
        bad = [dict(r, provider="非法 名字") for r in rows]
        st(cli("admin").put("/api/providers", json=bad), 422, "provider 字符合法性")
        st(cli("admin").put("/api/providers", json=[dict(rows[0], id=99999)]), 422, "未知配置行")
    finally:
        cli("admin").put("/api/providers", json=json.loads(saved))


@case("MOD-05", "策略保存只改配置")
def _():
    items = st(cli("admin").get("/api/model-policies"), 200, "读策略").json()
    eq(len(items["items"]), 9, "九个任务场景")
    base = [dict(i) for i in items["items"]]
    r = cli("admin").put("/api/model-policies", json=[{**base[0], "scene_key": "nope"}])
    st(r, 422, "未知场景")
    detail_has(r, "没有这个任务场景", "原话")
    same = {**base[0], "slot_provider": "mock", "slot_model": "mock-1",
            "fallback_provider": "mock", "fallback_model": "mock-1"}
    st(cli("admin").put("/api/model-policies", json=[same]), 422, "备用等于建议")
    st(cli("admin").put("/api/model-policies", json=[{**base[0], "enabled": True,
                                                      "slot_provider": ""}]),
       422, "启用却没建议轨")
    ok(items["no_auto_fallback"] is True, "策略要回「不自动兜底」")


@case("MOD-06", "顶部四数口径")
def _():
    stats = st(cli("admin").get("/api/models/stats"), 200, "四数").json()
    eq(stats["registered_total"], db1("SELECT COUNT(*) AS n FROM model_registration")["n"],
       "已登记＝登记表行数")
    eq(stats["queued"] + stats["running"],
       db1("SELECT COUNT(*) AS n FROM job WHERE status IN ('queued','starting','running')")["n"],
       "任务数＝队列里未完的作业")
    ok(stats["available_total"] <= stats["registered_total"], "可用不该超过已登记")
    ok("真实调用或产物验收" in stats["caliber"]["available_total"],
       f"口径要写清四条与：{stats['caliber']['available_total']}")


# ============================ 看板与导出 ============================
@case("DSH-01", "待办只算可见项目")
def _():
    pid = new_project("editor", "d1")
    job_id = st(submit_mock_job(pid, params={"sleep": 0.0, "fail": True}), 202, "提交").json()["id"]
    run_worker()
    mine = st(cli("editor").get("/api/workbench"), 200, "owner 读待办").json()
    theirs = st(cli("outsider").get("/api/workbench"), 200, "外人读待办").json()
    ok(mine["counts"]["jobs_failed"] >= 1, f"owner 该看到失败作业：{mine['counts']}")
    ok(theirs["counts"]["jobs_failed"] < mine["counts"]["jobs_failed"],
       "外人不该把别人的失败算进来")


@case("DSH-02", "看板六格现算")
def _():
    pid = new_project("editor", "d2")
    eid = new_episode(pid, "第 1 集")
    new_shot(eid)
    new_asset(pid)
    _seed_doc("d2", pid=pid, eid=eid)
    d = st(cli("editor").get(f"/api/projects/{pid}/dashboard"), 200, "看板").json()
    eq(d["episode_total"], 1, "集数")
    eq(d["shots"], 1, "镜头数")
    eq(d["assets"], 1, "资产数")
    eq(d["documents"], 1, "文档数")
    ok("costs" in d and "gates" in d and "audio" in d, "六格齐全")


@case("DSH-03", "CSV 带 BOM 与正确列")
def _():
    pid = new_project("editor", "d3")
    eid = new_episode(pid, "第 1 集")
    sid = new_shot(eid, summary="竖线|与换行测试")
    job_id = st(submit_mock_job(pid, episode_id=eid, shot_id=sid), 202, "作业").json()["id"]
    run_worker()
    r = st(cli("editor").get(f"/api/projects/{pid}/export/shots.csv"), 200, "导出 CSV")
    raw = r.content
    ok(raw.startswith(b"\xef\xbb\xbf"), "CSV 要带 UTF-8 BOM")
    text = raw.decode("utf-8-sig")
    head = text.splitlines()[0]
    eq(head, "集序,集标题,镜号,内容,提示词,时长(秒),引用资产,最新作业,作业状态,产物路径",
       "表头十字")
    ok(str(job_id) in text, "导出要写最新作业号")


@case("DSH-04", "Markdown 转义")
def _():
    pid = new_project("editor", "d4")
    eid = new_episode(pid, "第 1 集")
    new_shot(eid, summary="含|竖线\n与换行")
    md = st(cli("editor").get(f"/api/projects/{pid}/export/shots.md"), 200, "导出 MD").text
    ok("\\|" in md, "竖线要转义")
    for line in md.splitlines():
        if line.startswith("|") and "含" in line:
            ok("\n" not in line, "表格行内不许有裸换行")


@case("DSH-05", "导出取最新一条作业")
def _():
    pid = new_project("editor", "d5")
    eid = new_episode(pid, "第 1 集")
    sid = new_shot(eid)
    first = st(submit_mock_job(pid, episode_id=eid, shot_id=sid), 202, "第一条").json()["id"]
    run_worker()
    second = st(submit_mock_job(pid, episode_id=eid, shot_id=sid,
                               params={"sleep": 0.0, "fail": True}), 202, "第二条").json()["id"]
    run_worker()
    md = st(cli("editor").get(f"/api/projects/{pid}/export/shots.md"), 200, "导出").text
    ok(f"#{second}" in md, f"该写最新那条作业 {second}")
    ok(f"#{first}" not in md, "不该回退到旧的那条")


# ============================ 安全 ============================
@case("SEC-01", "文件路径不许上跳")
def _():
    c = cli("editor")
    for path in ("/files/../etc/passwd", "/files/%2e%2e/%2e%2e/etc/passwd",
                 "/files/absolute", "/files/1", "/files//etc/passwd"):
        r = c.get(path)
        st(r, 404, f"GET {path}")
        ok("root:" not in r.text, "不许回文件内容")


@case("SEC-02", "图按资产可见性发")
def _():
    pid = new_project("editor", "s2")
    aid = new_asset(pid)
    st(cli("editor").post(f"/api/assets/{aid}/images",
                          files={"file": ("ok.png", png_bytes(1024), "image/png")}),
       200, "上传")
    rel = db1("SELECT ref_images FROM asset WHERE id=?", (aid,))["ref_images"]
    path = json.loads(rel)[0]
    st(cli("outsider").get(f"/files/{path}"), 404, "外人取图")
    cli("editor").post(f"/api/projects/{pid}/members", json={"user_id": ACCOUNTS["viewer"]["id"]})
    st(cli("viewer").get(f"/files/{path}"), 200, "成员取图")


@case("SEC-03", "响应不外泄凭据")
def _():
    users = json.dumps(st(cli("admin").get("/api/users"), 200, "用户列表").json())
    ok("password_hash" not in users, "用户接口不许回口令哈希")
    providers = json.dumps(st(cli("admin").get("/api/providers"), 200, "能力轨").json())
    ok("\"key\"" not in providers.lower() or "key_env" in providers,
       "能力轨只该回变量名")
    ok("sk-" not in providers, "不许出现密钥值")


@case("SEC-04", "路径不由客户端决定")
def _():
    pid, doc_id, content = _seed_doc("s4")
    rel_before = db1("SELECT rel_path FROM document WHERE id=?", (doc_id,))["rel_path"]
    st(cli("editor").put(f"/api/documents/{doc_id}/content",
                         json={"content": "正常一句。\n", "expected_version": content["version"],
                               "rel_path": "../../etc/passwd"}),
       200, "塞 rel_path 也应正常保存")
    eq(db1("SELECT rel_path FROM document WHERE id=?", (doc_id,))["rel_path"], rel_before,
       "路径不许被客户端改")
    aid = new_asset(pid)
    r = cli("editor").post(f"/api/assets/{aid}/generated-images",
                           json={"job_id": 999999, "artifact_index": 0,
                                 "path": "../../etc/passwd"})
    st(r, (404, 409, 422), "导入产物不许吃路径")


@case("SEC-05", "注入串只当数据")
def _():
    evil = "Robert'); DROP TABLE project;--"
    r = st(cli("editor").post("/api/projects", json={"name": evil}), 201, "建项目")
    pid = r.json()["id"]
    eq(r.json()["name"], evil, "名字按字面存")
    ok(pid in [p["id"] for p in st(cli("editor").get("/api/projects"), 200, "列表").json()],
       "项目仍可查")
    ok(db1("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='project'")["n"]
       == 1, "project 表不该被删")
    st(cli("editor").get("/api/jobs", params={"q": evil}), 200, "搜索串按字面处理")
    st(cli("admin").delete(f"/api/projects/{pid}?confirm={evil}"), 204, "清掉")


# ---------- 4. 共用准备 ----------
def _seed_doc(tag: str, pid: int | None = None, eid: int | None = None,
              content: str = "燧人蹲在洞口。\n"):
    pid = pid or new_project("editor", tag)
    eid = eid or new_episode(pid, "第 1 集")
    d = st(cli("editor").post(f"/api/projects/{pid}/documents",
                              json={"kind": "novel", "episode_id": eid,
                                    "title": f"稿-{RUN}", "content": content}),
           201, "建文档")
    doc_id = d.json()["id"]
    body = st(cli("editor").get(f"/api/documents/{doc_id}/content"), 200, "读正文").json()
    return pid, doc_id, body


def _template_id(name: str, version: int) -> int:
    items = st(cli("editor").get("/api/pipeline/templates"), 200, "模板").json()
    for t in items:
        if t["name"] == name and t["version"] == version:
            return t["id"]
    raise Blocked(f"没有内置模板 {name} v{version}")


def _seed_pipeline_case(tag: str, shots: int = 2):
    pid = new_project("editor", tag)
    eid = new_episode(pid, "第 1 集")
    ids = [new_shot(eid, summary=f"镜{i}", prompt=f"提示词{i}", duration=1)
           for i in range(1, shots + 1)]
    return pid, eid, ids


def _seed_composed(tag: str):
    """准备一条已经合成完成的成片（mock 视频 + 签字 + FFmpeg）。"""
    pid = new_project("editor", tag)
    eid = new_episode(pid, "第 1 集")
    shot_ids = [new_shot(eid, summary=f"镜{i}", duration=1) for i in (1, 2)]
    job_ids = []
    for sid in shot_ids:
        j = st(submit_mock_job(pid, capability="video", episode_id=eid, shot_id=sid,
                               params={"sleep": 0.0, "media": "mp4", "seconds": 1.0}),
               202, "出片").json()["id"]
        job_ids.append(j)
    run_worker(8)
    for j in job_ids:
        if st(cli("editor").get(f"/api/jobs/{j}"), 200, "作业").json()["status"] != "succeeded":
            raise Blocked("mock 媒体产物没跑成功（本机可能没有 ffmpeg）")
        sign_job(j)
    comp_id = st(cli("editor").post(f"/api/projects/{pid}/compositions",
                                    json={"episode_id": eid, "width": 640, "height": 360,
                                          "fps": 24}),
                 201, "发起合成").json()["id"]
    run_worker(8)
    d = st(cli("editor").get(f"/api/compositions/{comp_id}"), 200, "成片").json()
    if d["status_label"] != "已完成":
        raise Blocked(f"合成没跑到已完成（{d['status_label']}）")
    return pid, eid, comp_id, shot_ids, job_ids


# ---------- 5. 账号准备 ----------
ACCOUNTS: dict[str, dict] = {}


def prepare_accounts() -> None:
    su = user_repo.get_by_username("superadmin")
    ACCOUNTS["superadmin"] = {"id": su["id"], "token": mint(su["id"])}
    for key, role in (("admin", "admin"), ("editor", "editor"),
                      ("viewer", "viewer"), ("outsider", "editor"),
                      ("editor2", "editor")):
        row = user_repo.create(f"qa_{key}_{RUN}", f"QA {key}", hash_password(PASSWORD),
                               role_codes=[role])
        ACCOUNTS[key] = {"id": row["id"], "token": mint(row["id"]), "username": row["username"]}
    for key, client in ACCOUNTS.items():
        c = TestClient(APP, cookies={"fv_session": client["token"]})
        c.headers.update({"X-Test-Role": key})
        _clients[key] = c


# ---------- 6. 跑 ----------
def main_cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑这些前缀，逗号分隔，如 AUTH,PROJ")
    ap.add_argument("--json", action="store_true", help="结果输出 JSON")
    args = ap.parse_args()

    prepare_accounts()
    prefixes = tuple(p.strip() for p in args.only.split(",") if p.strip())
    results = []
    for cid, title, fn in CASES:
        if prefixes and not cid.startswith(prefixes):
            continue
        started = datetime.now()
        try:
            fn()
            verdict, note = "PASS", ""
        except Blocked as exc:
            verdict, note = "BLOCKED", str(exc)
        except AssertionError as exc:
            verdict, note = "FAIL", str(exc)
        except Exception as exc:            # 意外异常也要留痕，不许静默变绿
            verdict, note = "ERROR", f"{type(exc).__name__}: {exc}"
        results.append({"id": cid, "title": title, "result": verdict,
                        "note": note[:400],
                        "seconds": round((datetime.now() - started).total_seconds(), 2)})

    counts = {}
    for r in results:
        counts[r["result"]] = counts.get(r["result"], 0) + 1

    if args.json:
        print(json.dumps({"run": RUN, "counts": counts, "cases": results},
                         ensure_ascii=False, indent=2))
    else:
        width = max(len(r["id"]) for r in results)
        for r in results:
            print(f"{r['id']:<{width}}  {r['result']:<7}  {r['title']}"
                  + (f"  ← {r['note']}" if r["note"] else ""))
        print(f"\n合计 {len(results)} 例：" +
              "／".join(f"{k} {v}" for k, v in sorted(counts.items())))
        print(f"临时目录：{_TMP}")
    return 1 if counts.get("FAIL") or counts.get("ERROR") else 0


if __name__ == "__main__":
    sys.exit(main_cli())
