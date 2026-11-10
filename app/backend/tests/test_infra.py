"""W01 的基础设施与真实行为回归：迁移/备份/引导口令/上传安全/层级/前端出口。

这一份的重点不是"功能多"，而是**每一条都能证伪**：
断言都写具体值或具体集合，不写 `assert r.status_code < 500` 这种永远绿的。
"""
import ast
import re
import uuid

import pytest
from pathlib import Path

from fastapi.testclient import TestClient

import migrate
from db import get_conn
from repositories.users import users as user_repo
from security import hash_password

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "frontend"


# ---------------------------------------------------------------- 迁移与库
def test_库停在最新迁移且外键干净():
    """版本序列必须与 migrations/ 里的文件一一对上。

    这里不写死"到 12 为止"：写死之后每加一条迁移都要记得来改它，
    而忘了改的结果是"新迁移没跑也照样绿"。改成拿目录当基准，
    加迁移的人不需要动这条测试。
    """
    on_disk = sorted(int(p.name.split("_")[0]) for p in (BACKEND / "migrations").glob("*.sql"))
    with get_conn() as conn:
        v = sorted(r["version"] for r in conn.execute("SELECT version FROM schema_version"))
        assert v == on_disk, f"库停在 {v}，磁盘上有 {on_disk}"
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
        # bootstrap 建的超管带着"必须改初始口令"标记，会被 deps 那道闸门挡在 403；
        # 这条测的是权限点不是改密闸门，先把标记清掉（改密闸门另有测试）
        conn.execute("UPDATE app_user SET must_change_password = 0 WHERE is_superuser = 1")
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
    """W00 抓到的真 bug：这三个 DELETE 漏了 toJson，后端拒绝也照样刷新成「已删除」。

    2026-11-08 起 api.js 的请求收成 get/send 两个出口，两个出口内部都调 toJson，
    所以「这一行有没有 fetch + toJson」不再是有效判据——改成钉真正的不变式：
    这三个方法必须走统一出口（get/send），**不许裸 fetch**。裸 fetch 正是当年
    那个 bug 的来源（漏写 .then(toJson)，403 也当成功）。
    """
    src = (FRONTEND / "js" / "api.js").read_text()
    for fn in ("deleteEpisode", "deleteShot", "deleteAsset"):
        line = next((l for l in src.split("\n") if fn in l), "")
        assert line.strip(), f"{fn} 不见了"
        assert "send(" in line or "get(" in line, (
            f"{fn} 没有走统一出口（get/send）：{line.strip()}")
        assert "fetch(" not in line, (
            f"{fn} 用了裸 fetch——当年就是这个写法漏了 toJson：{line.strip()}")


def test_统一出口之外不许直接调_toJson():
    """反过来钉一次：get/send 之外再出现 toJson，等于绕开统一出口。

    绕开的后果与当年那个 bug 同源：拿到原始 Response 却不解析，
    或者解析了却拿不到 headers，两种都是「看起来对、错误被咽掉」。
    例外只有 providerValidations——它要读 X-Total-Count，必须持有 Response。
    """
    src = (FRONTEND / "js" / "api.js").read_text()
    body = [l for l in src.split("\n")
            if "toJson" in l and not l.strip().startswith(("//", "*"))]
    allowed = ("async function toJson", "const send =", "return fetch(",
               ".then(toJson)", "const items = await toJson(res)")
    bad = [l.strip() for l in body if not any(a in l for a in allowed)]
    assert not bad, f"这些地方绕过统一出口直接调 toJson：{bad}"


def test_业务视图不直接拼_url():
    """规格 01-6-6／02 §3.1：前端只用 api.js 一个出口，视图里不该出现 fetch(。"""
    offenders = []
    # 2026-11-08 起 views/ 下还有一层子目录（models/repo.js 等），要一并扫到，
    # 否则「再写一份 agoText」可以躲进子目录不被发现。
    for f in list((FRONTEND / "js" / "views").glob("*.js")) + list((FRONTEND / "js" / "views").rglob("models/*.js")):
        if "fetch(" in f.read_text():
            offenders.append(f.name)
    assert not offenders, f"这些视图绕过了 api.js：{offenders}"


def test_前端一个阻塞式原生弹窗都不许有():
    """`alert`／`confirm`／`prompt` 全站为零。

    历史：这条原来叫 `test_alert_只有_util_一处`，断言是
    `set(hits) == {"util.js"}`——当时 `util.run()` 里留着全站唯一那条 `alert`，
    它作为"统一错误出口"被容忍。2026-11-09 把那条也换了：
    `run()` 是**调用最频繁**的错误出口（"写回失败""删除失败"都走它），
    于是每一次失败都弹一次原生框。原生弹窗有三个具体坏处，不是审美问题：
    它**阻塞**脚本（真浏览器验收里唯一能把脚本挂住的调用）、抢走焦点
    （键盘用户要先去点一下才算处理完）、样式与两套主题脱节。

    现在两条路各有归宿：危险操作确认走 `util.confirmDanger()`，
    一句话反馈派 `app:toast` 事件（`app.js:641` 接住渲染成右下角那条）。
    所以判据从"只许一处"收紧成"一处都不许"，并且**反向验**：
    `util.run()` 真的把失败说出去了，而不是删掉 alert 就什么都不说。
    """
    hits = []
    for f in (FRONTEND / "js").rglob("*.js"):
        for n, line in enumerate(f.read_text().split("\n"), 1):
            code = line.split("//", 1)[0]
            if code.lstrip().startswith("*"):
                continue
            if re.search(r"(?<![\w.$])(alert|confirm|prompt)\s*\(", code):
                hits.append(f"{f.name}:{n}")
    assert not hits, f"前端还留着阻塞式原生弹窗（走 confirmDanger() 或 app:toast）：{hits}"
    # 反向：删掉 alert 必须接上说明，不许静默
    util_src = (FRONTEND / "js" / "util.js").read_text()
    assert "app:toast" in util_src, "util.run() 删掉 alert 后没接上 app:toast"


def test_危险确认不再用原生_confirm():
    """W02-c：删集／删镜／停用账号原来用原生 confirm()，跟两套主题脱节，
    而且"删除"和"知道了"长得一样。现在全站一套 confirmDanger。"""
    hits = []
    # 2026-11-08 起 views/ 下还有一层子目录（models/repo.js 等），要一并扫到，
    # 否则「再写一份 agoText」可以躲进子目录不被发现。
    for f in list((FRONTEND / "js" / "views").glob("*.js")) + list((FRONTEND / "js" / "views").rglob("models/*.js")):
        for n, line in enumerate(f.read_text().split("\n"), 1):
            if re.search(r"(?<![\w.])confirm\s*\(", line) and not line.strip().startswith("//"):
                hits.append(f"{f.name}:{n}")
    assert not hits, f"还在用原生 confirm()：{hits}"


def test_每张表都在横向滚动容器里():
    """W02-d：卡片是 overflow:hidden，窄屏时列被裁掉又没有滚动条。"""
    offenders = []
    # 2026-11-08 起 views/ 下还有一层子目录（models/repo.js 等），要一并扫到，
    # 否则「再写一份 agoText」可以躲进子目录不被发现。
    for f in list((FRONTEND / "js" / "views").glob("*.js")) + list((FRONTEND / "js" / "views").rglob("models/*.js")):
        text = f.read_text()
        tables = text.count("<table")
        wraps = text.count('class="table-wrap"')
        if tables != wraps:
            offenders.append(f"{f.name}: table={tables} wrap={wraps}")
    assert not offenders, f"表格没包 .table-wrap：{offenders}"


def test_会话过期有统一出口():
    """W02-a：原来只有启动那一次认 401，过期后每个操作只弹一句"失败：未登录"。"""
    assert "app:unauthorized" in (FRONTEND / "js" / "api.js").read_text()
    assert "app:unauthorized" in (FRONTEND / "js" / "app.js").read_text()
    # 登录/改密接口的 401 是"口令错了"，不能拿去挂过期闸门（源码里是转义过的正则）
    assert "login|password" in (FRONTEND / "js" / "api.js").read_text()


def test_有二级页的模块页内也有切换器():
    """W02-d：窄屏侧栏二级标签会收起，页内没有第二个入口就等于把功能藏了。

    只查"已经有真实内容的模块"；生成／音频／审核／设定四页的 subs 目前
    还是装饰，等接真功能时再纳入（那时这条测试要一起改）。

    v0.99 时 system-config.js 只剩一个二级页，所以不在名单里；
    v0.111 它又变成两页（用户管理／角色管理），切换器收回它自己画——
    两个面板只出正文卡片，头部与那一排 chip 只有一个出处。
    模型中心四个工作区是新的多页模块，所以进名单。
    """
    for name in ("assets.js", "writing.js", "models.js", "system-config.js"):
        text = (FRONTEND / "js" / "views" / name).read_text()
        assert "data-kind" in text or "subChipRow" in text, f"{name} 缺页内切换器"
    # 拆页之后：切换器不许在两个面板里各画一遍（一次点击会触发两遍重渲染）
    for name in ("system-users.js", "system-roles.js"):
        text = (FRONTEND / "js" / "views" / name).read_text()
        assert "subChipRow" not in text, f"{name} 自己又画了一排切换器"


def test_页内切换器改完导航必须重渲染():
    """上一条只查"有没有第二个入口"，这条查"这个入口按了有没有反应"。

    2026-11-04 实测：assets.js 的工作区切换器写成 `bindSubChips(root, (i) => {
    setWsIndex(i); })`——只写导航状态、不重渲染，而 ≤860px 时侧栏那一层整块隐藏，
    这一排 chip 是唯一出口，于是"点了没反应、下次刷新莫名跳过去"。全仓八处调用
    只有这一处漏，正是"同一个规矩八处各写一遍"的下场，所以钉成文件级判据。
    """
    bad = []
    # 2026-11-08 起 views/ 下还有一层子目录（models/repo.js 等），要一并扫到，
    # 否则「再写一份 agoText」可以躲进子目录不被发现。
    for f in list((FRONTEND / "js" / "views").glob("*.js")) + list((FRONTEND / "js" / "views").rglob("models/*.js")):
        text = f.read_text()
        for m in re.finditer(r"bindSubChips\(", text):
            tail = text[m.start():m.start() + 300]      # 够看完一个箭头函数体
            if not re.search(r"refresh\(\)|dispatchEvent", tail):
                line_no = text[:m.start()].count("\n") + 1
                bad.append(f"{f.name}:{line_no}")
    assert not bad, f"这些页内切换器改完导航不重渲染：{bad}"


def test_每个工作区都画得出切换器():
    """窄屏里侧栏二级导航整块隐藏，页内那一排 chip 是**唯一**的跨工作区出口。

    2026-11-04 实测到的：资产中心三个工作区里只有「资产库」自己画了切换器，
    另外两个（资产创作／文本提取）各写各的页头——于是从资产库切进「资产创作」之后，
    380px 上既回不去也换不到第三个工作区，等于把人关在一个页面里。
    判据：三个视图文件里都要同时出现"渲染切换器"与"绑定切换器"两件事。
    """
    views = ("assets.js", "asset-studio.js", "asset-triage.js")
    for name in views:
        text = (FRONTEND / "js" / "views" / name).read_text()
        assert "subChipRow(WS_LABELS" in text, f"{name} 没画工作区切换器"
        assert "bindSubChips(root" in text, f"{name} 画了切换器但没绑——点了没反应"


def test_bindLive_里的绑定都防重复():
    """纪律：凡被 patch 重画出来的控件，绑定住在 `bindLive()`。

    但这条纪律有个反面：**没被重画的那几格是同一批节点**，`bindLive` 每 tick
    跑一遍就会按次数累加监听。2026-11-04 实测漏了两处——跑一条 60 秒的图像作业
    ≈20 个 tick，之后点一次「删这张」连发 20 次 DELETE（后十几次 404 变报错风暴）。
    判据：bindLive 函数体内每个 `querySelectorAll(...).forEach(` 后面 10 行里
    必须出现 `dataset.bound`（同文件里 dropBtn／fillBtn 早就是这么写的）。
    窗口给到 10 行而不是 3 行：这条纪律值得一句解释为什么，
    而把注释压短换不来任何安全性——判据只看"这一处绑定有没有守卫"，不看文体。
    """
    text = (FRONTEND / "js" / "views" / "asset-studio.js").read_text()
    start = text.index("function bindLive(")
    body = text[start:text.index("\nfunction ", start + 10)]
    lines = body.split("\n")
    bad = []
    for i, line in enumerate(lines):
        if re.search(r"querySelectorAll\(.*\)\.forEach\(", line):
            if "dataset.bound" not in "\n".join(lines[i:i + 10]):
                bad.append(line.strip()[:64])
    assert not bad, f"bindLive 里有没防重复的绑定：{bad}"


def test_网格重画后辅助按钮重新绑上():
    """空态与错误态里的「清除筛选」「重试」是 `paintGrid()` 画出来的**新节点**；
    绑在 mount() 里等于首屏有结果时从没绑上（`querySelector` 拿到 null），
    搜一个没命中的词之后点了没反应。2026-11-04 实测，改成 paintGrid 末尾统一绑。
    """
    text = (FRONTEND / "js" / "views" / "assets.js").read_text()
    start = text.index("function paintGrid(")
    body = text[start:text.index("\nfunction ", start + 10)]
    assert "bindGridHelp(root)" in body, "paintGrid 重画之后没有重绑网格里的辅助按钮"
    # 一次性绑定必须已经撤掉：同一颗钮绑两处＝点一次跑两遍
    assert len(re.findall(r"\[data-clear\][^;]*addEventListener", text)) == 1, \
        "[data-clear] 不止绑了一处"
    assert len(re.findall(r"\[data-retry\][^;]*addEventListener", text)) == 1, \
        "[data-retry] 不止绑了一处"


def test_模型中心的二级页与模块清单一致():
    """modules.js 的 subs 与视图里的 SUBS 必须是同一个数组。

    这条不是洁癖：router 会把越界的二级下标钳回 0（router.js:39），
    所以两处不一致的后果是"最后一个页签永远打不开"，
    而且不报错——阶段 E 的整季页签就是这么悄悄哑掉的（产线中心有同类注释）。
    只钉模型中心这一个模块：别的模块各用 WORKSPACES／TABS 等不同名字，
    现在写一个通用解析器会比页面更容易错。
    """
    import re

    modules = (FRONTEND / "js" / "modules.js").read_text()
    # 2026-11-08 拆分后 SUBS 住在 models/shared.js（入口只 import 不声明）。
    view = (FRONTEND / "js" / "views" / "models" / "shared.js").read_text()
    block = re.search(r"id: 'models'.*?subs: \[([^\]]*)\]", modules, re.S)
    assert block, "modules.js 里找不到模型中心的 subs"
    listed = re.findall(r"'([^']+)'", block.group(1))
    declared = re.findall(r"'([^']+)'", re.search(r"const SUBS = \[([^\]]*)\]", view).group(1))
    assert listed == declared, f"侧栏写的是 {listed}，页内切换器是 {declared}"


# ---------------------------------------------------------------- 口径单一来源
def _js_object_literal(src: str, name: str) -> dict[str, str]:
    """从 JS 源码里取出 `const NAME = { ... };` 的键与**值原文**（不执行 JS）。

    机器上没有 node，也不打算为一条测试装一个 JS 解析器：这里只按"一行一个键"
    的字面量形状取，取不到就报错让写测试的人改写法。刻意不做完整解析——
    真解析 JS 的话，这条守卫本身就成了第二个容易出错的前端。
    """
    start = src.find(name)
    assert start >= 0, f"源码里找不到 {name}"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} 后面没有对象字面量"
    depth, end = 0, brace
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = src[brace + 1:end]
    # 一行可能写好几对（enums.js 的 CAPS 就是紧凑写法），所以整段扫而不是逐行 split
    body = re.sub(r"(?m)^\s*(//.*|/\*.*?\*/)$", "", body)
    pairs = re.findall(r"([A-Za-z_][\w]*):\s*(\[[^\]]*\]|'[^']*'|\{[^}]*\})", body)
    return {k: v.strip() for k, v in pairs}


def _js_array(src: str, name: str) -> list[str]:
    """取 `export const NAME = ['a', 'b'];` 的字符串元素。"""
    m = re.search(re.escape(name) + r"\s*=\s*\[([^\]]*)\]", src)
    assert m, f"源码里找不到数组 {name}"
    return re.findall(r"'([^']*)'", m.group(1))


def test_前端作业状态表和后端枚举对齐():
    """job.status 的说法与配色只有一份，且必须真的跟着后端。

    这一条是本轮体检的落点：同一张表原来在五个视图文件里各写一遍，
    其中 asset-studio 与 asset-detail 那份**漏了 cancel_requested**——
    "取消中"的作业还在进程里跑，那一页却当它已经结束，于是轮询停掉、按钮放开。
    少一个值不会报错，只会让界面骗人，所以必须机器对。
    """
    import models
    from repositories.ordering import capability_order
    js = (FRONTEND / "js" / "validation.js").read_text()
    table = _js_object_literal(js, "JOB_STATUS =")
    assert list(table) == list(models.JOB_STATUSES), (
        f"前端 {list(table)} ≠ 后端 {list(models.JOB_STATUSES)}")
    # 后端字典里的中文必须和前端兜底文案逐字一致
    for code, raw in table.items():
        inner = raw.strip().lstrip("[").rstrip("]").split(",")
        assert len(inner) == 2, f"JOB_STATUS.{code} 形状不是 [配色, 文案]：{raw}"
        assert inner[1].strip().strip("'") == models.JOB_STATUSES[code], (
            f"{code} 的文案前端「{inner[1]}」／后端「{models.JOB_STATUSES[code]}」")
    # 在飞／终态两个集合：前者＝queued + JOB_RUNNING，后者＝JOB_FINISHED
    assert _js_array(js, "JOB_ACTIVE") == ["queued", *models.JOB_RUNNING]
    assert _js_array(js, "JOB_TERMINAL") == list(models.JOB_FINISHED)
    # 顺序的权威也在这里：能力与运行位置的 CASE 由 models 的键序现推，
    # 库里躺着认不出来的值时它排最后，不冒充"最常用的那一档"
    assert "'text' THEN 1" in capability_order()
    assert capability_order().count("WHEN") == len(models.CAPABILITIES)


def test_前端枚举表和后端枚举对齐():
    """能力／运行位置／计费单位在界面上只有一份中文叫法。

    原来抄出过真事故：`writing.js` 把 character 写成「千字」，
    后端 models.UNITS 是「字符」；`models.js` 把 million_token 写成「百万 token」。
    同一张价格卡在两屏上是两种单位说法，而单位恰恰是给人对账用的。
    """
    import models
    js = (FRONTEND / "js" / "enums.js").read_text()
    for name, source in (("CAPS =", models.CAPABILITIES),
                         ("MODES =", models.BACKENDS),
                         ("UNITS =", models.UNITS),
                         ("REG_SOURCES =", models.REG_SOURCES)):
        table = _js_object_literal(js, name)
        assert table == {k: f"'{v}'" for k, v in source.items()}, (
            f"{name} 与后端不一致：前端 {table}／后端 {source}")

    # 上面那半只钉得住 enums.js **自己**跟后端一致，看不见"某个视图又抄了一份"。
    # 后半截扫视图，因为 `models.js` 的价格牌正是这么抄出事的：本地那张表只抄了
    # 三档，于是 `token`／`request`／`minute`／`character` 四档把英文 code 直接念在
    # 界面上（「8 元/token」），抄下来的 `million_token` 还写成「百万 token」。
    # 锚只认 `million_token`／`second` 这两个不会撞车的键名——`character` 在资产那边
    # 是"人物／角色"的意思，拿它当锚会误报两条正常代码。
    copies = []
    # 2026-11-08 起 views/ 下还有一层子目录（models/repo.js 等），要一并扫到，
    # 否则「再写一份 agoText」可以躲进子目录不被发现。
    for f in list((FRONTEND / "js" / "views").glob("*.js")) + list((FRONTEND / "js" / "views").rglob("models/*.js")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"\b(million_token|second):\s*'[^']*[一-鿿]", line):
                copies.append(f"{f.name}:{n}: {line.strip()[:70]}")
    assert not copies, (
        "这些行在视图里又抄了一份计费单位表，改成 import enums.js 的 unitLabel："
        + "；".join(copies))


def test_视图不再自己写第二份状态字典():
    """`views/*.js` 里不许再出现 job 状态字面量的第二份表。

    上一轮是"复制一份字典"，这一轮起是"import 那一份"。守的是复制这个动作，
    所以扫描整个目录而不是点某几个文件——新页面也会犯同样的错。
    """
    offenders = []
    # 2026-11-08 起 views/ 下还有一层子目录（models/repo.js 等），要一并扫到，
    # 否则「再写一份 agoText」可以躲进子目录不被发现。
    for f in list((FRONTEND / "js" / "views").glob("*.js")) + list((FRONTEND / "js" / "views").rglob("models/*.js")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.match(r"\s*(queued|starting|needs_attention|cancel_requested):"                          r"\s*(\[|'|\{)", line):
                offenders.append(f"{f.name}:{n}")
    assert not offenders, f"这些行又在视图里抄了一份作业状态表：{offenders}"


def test_前端调用的api方法名必须真的存在():
    """`api.X()` 的 X 必须在 `api.js` 的导出对象里，一个都不许凭印象写。

    直接起因（2026-11-09 体检）：`generate.js:1857` 调 `api.modelScenes()`，而
    801b2c9 把 api.js 收敛成 get/send 出口时压根没导出过这个名字——git log -S
    确认它从未存在。这不是"接口 404"那种能看见的坏：`Promise.all([..., api.modelScenes()...])`
    里方法名写错是**同步抛 TypeError**，挂在返回 value 上的 `.catch()` 根本来不及
    接管，外层 try 把它咽进一个永不渲染的 `optionsError`，于是提交弹窗的场景／
    路线／槽位三个下拉一起变空而页面一声不吭。`generate_scene_check.py` 当场 8 项红。

    机器上没有 node、也不给一条测试装 JS 解析器：按"两空格缩进一行一键"的字面量
    形状取导出名（api.js 全文就是这个写法），调用面扫 views/（含 models/ 子目录）
    与 app.js。动态取用 `api[name]` 不在射程内——那是注释里要写明的豁免，不是漏网。
    """
    src = (FRONTEND / "js" / "api.js").read_text()
    start = src.find("export const api = {")
    assert start >= 0, "api.js 的导出形状变了，这条守卫要跟着改而不是静默放行"
    depth, end = 0, start
    for i in range(start + len("export const api = "), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    exports = set(re.findall(r"(?m)^  ([A-Za-z_]\w*):", src[start:end]))
    assert len(exports) > 100, f"导出名只取到 {len(exports)} 个，多半是形状解析漂了"

    offenders = []
    files = (list((FRONTEND / "js" / "views").glob("*.js"))
             + list((FRONTEND / "js" / "views").rglob("*.js"))
             + [FRONTEND / "js" / "app.js"])
    for f in dict.fromkeys(files):          # 去重：glob 与 rglob 在 views 顶层重叠
        for n, line in enumerate(f.read_text().splitlines(), 1):
            for name in re.findall(r"\bapi\.([A-Za-z_]\w*)\s*\(", line):
                if name not in exports:
                    offenders.append(f"{f.name}:{n}: api.{name}()")
    assert not offenders, (
        "这些调用点用了 api.js 没导出的方法名（同步 TypeError，.catch 接不住）："
        + "；".join(offenders))


def test_多久前只有util一处写法():
    """「多久前」只许住 `js/util.js` 的 `agoText()`。

    起因是使用侧指着运行监控那张 worker 表说「46045s 前不好算」。查下去病根不是单位，
    是**同一件事有两份定义**：`views/models.js` 直接拼 `${age_seconds}s 前`，
    `views/pipeline.js` 自己有个只到小时的 agoText——同一份 `/api/worker/status`
    在两个页面念出两种精度。心跳新旧是判断"要不要重启 worker"的依据，不该让人做除法。

    守的是"再写一份"这个动作，所以扫整个 views 目录而不是点那两个文件。
    """
    offenders = []
    # 2026-11-08 起 views/ 下还有一层子目录（models/repo.js 等），要一并扫到，
    # 否则「再写一份 agoText」可以躲进子目录不被发现。
    for f in list((FRONTEND / "js" / "views").glob("*.js")) + list((FRONTEND / "js" / "views").rglob("models/*.js")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"function agoText", line):
                offenders.append(f"{f.name}:{n} 又定义了一份 agoText")
            elif re.search(r"\}s 前", line) or re.search(r"seconds\s*/\s*(60|3600)", line):
                offenders.append(f"{f.name}:{n} 又手写了一遍相对时间")
    assert not offenders, f"这些行在视图里另写了一份「多久前」：{offenders}"

    # 反向半边：两张 worker 表必须真在调用那一处，不然扫描通过而行为没改。
    # 2026-11-08 拆分后模型中心的运行监控搬到 models/monitor.js。
    for rel in ("views/models/monitor.js", "views/pipeline.js"):
        text = (FRONTEND / "js" / rel).read_text()
        assert "agoText(" in text, f"{rel} 的 worker 表没用 util.js 那一份 agoText"
    util = (FRONTEND / "js" / "util.js").read_text()
    assert "86400" in util, "util.js 的 agoText 没有天这一级，2 天前的行还会念成 66 小时"


def test_金额格式化只有util一处写法():
    """金额只许用 `js/util.js` 的 `formatMoney()` 格式化。

    与上一条同形：`views/pipeline.js` 原先自带一份
    `money()` —— `toFixed(2)` 加 `currencies[0] || 'CNY'`。三个后果：

    * 金额为 `null` 时显示 `¥0.00`——**凭空造一个数**，本该是「—」；
    * 小额被吃掉：云端按秒计价，¥0.0036 显示成 `¥0.00`；
    * 没有币种时硬套 `¥`。整季全是本地作业时后端 `currencies` 是空数组
      （`repositories/seasons.py:216` 的 `DISTINCT ... IS NOT NULL` 查不到行），
      于是"没有可计价的花费"被显示成"花了 0 元"——会被当成对账结论。

    守两件事：别的 `views/` 文件不许再写一份金额格式化；`util.js` 那一份
    必须真的被 `pipeline.js` 用上（不然扫描通过而行为没改）。
    """
    offenders = []
    for f in list((FRONTEND / "js" / "views").glob("*.js")) + \
            list((FRONTEND / "js" / "views").rglob("models/*.js")):
        src = f.read_text()
        lines = src.splitlines()
        for n, line in enumerate(lines, 1):
            m = re.search(r"function (money|formatMoney|fmtMoney)\s*\(", line)
            if m:
                # **转发包装不算第二份口径**：`asset-studio.js` 的 `fmtMoney`
                # 整个函数体就是 `return formatMoney(...)`；`pipeline.js` 的
                # `money()` 也只是"从数组里取币种、无币种念「—」"再转交。
                # 判据取**整个函数体**（花括号配平到底），不是函数名也不是行数——
                # 按行数取窗口会被注释长短影响，那是假判据。
                depth, body_lines = 0, []
                for ln in lines[n - 1:]:
                    body_lines.append(ln)
                    depth += ln.count("{") - ln.count("}")
                    if depth <= 0 and len(body_lines) > 1:
                        break
                body = "\n".join(body_lines)
                if re.search(r"return\s+formatMoney\s*\(", body):
                    continue
                offenders.append(f"{f.name}:{n} 又定义了一份金额格式化")
            # 绕过 formatMoney 自己除 1e6 再定小数位，同样是第二份口径
            elif re.search(r"/\s*1_?000_?000", line) and "toFixed" in line:
                offenders.append(f"{f.name}:{n} 自己把 micros 换算成金额")
    assert not offenders, "金额格式化只该有 util.js 那一份：" + "；".join(offenders)

    pipeline = (FRONTEND / "js" / "views" / "pipeline.js").read_text()
    assert "formatMoney(" in pipeline, \
        "pipeline.js 的整季成本没有走 util.js 的 formatMoney()"
    # 无币种必须念「—」而不是造一个 0：本地作业不按量计费。
    util = (FRONTEND / "js" / "util.js").read_text()
    assert "return '—'" in util, "formatMoney 丢了 micros 为 null 时的「—」"


def test_队列行数超过一页时说出真实总数():
    """「当前筛选」那个数字是**这一页的行数**，不是总数——超过一页必须补真数。

    队列请求带着 `limit: 100`（`generate.js:562`），后端默认也是 100
    （`routers/jobs.py:112`）。作业多于 100 条时，那个读数会停在 100，
    看起来像"总共就这些"——恰好是在最需要知道积压多少的时候骗人。

    `total_jobs` 一直是后端算好的（`jobs_parts/cost.py:59` 的 `COUNT(*)`，
    与翻页无关，`test_cost.py:142` 已经钉住它的口径），前端却一处都没读。
    这条守的不是"字段存在"，是"超过一页时要把它说出来"。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "frontend" / "js" / "views" / "generate.js").read_text()
    # 正向：必须读 total_jobs，并且是拿它跟本页行数比
    assert "costs?.total_jobs" in src, "「当前筛选」没读 total_jobs，超过一页时会骗人"
    assert "total > shown" in src, "读到了 total_jobs 但没拿它跟本页行数比"
    # 口径闸：total_jobs 只按项目过滤，不知道 q/status/capability/mode。
    # 有额外筛选时两者不是一回事，"共 N" 会变成假分母——那种情况必须不显示。
    assert "narrowed" in src, (
        "没拦住「有额外筛选时不许报总数」：total_jobs 只按项目过滤，"
        "搜索时的 total 是项目总数、shown 是搜索结果，会给出假分母")
    # 反向：队列请求必须真的带分页参数（口径的另一半；改了这里这条判断就该重写）
    # 原来这里钉的是字符串 "limit: 100"。加了翻页之后队列请求改由 PAGE_SIZE
    # 驱动并带 before_id，于是这条判断按它自己的要求重写：不再盯某个字面量，
    # 而是盯"请求带了页长、翻页带游标、取回的是带 total 的响应"。
    assert "const PAGE_SIZE" in src, "队列的页长不是常量，翻页条算不出还剩几条"
    assert "limit: PAGE_SIZE" in src, "队列请求没带页长"
    assert "before_id: last.id" in src, (
        "翻页没带游标：要么翻不动，要么用 offset 会在轮询期间重复出行")
    assert "X-Total-Count" in (
        Path(__file__).resolve().parents[2] / "frontend" / "js" / "api.js"
    ).read_text(), "前端没读 X-Total-Count，分母只能靠 state.jobs.length 猜"


def test_样式表花括号配平():
    """app.css 必须括号配平，且不能出现"没有选择器的规则体"。

    为什么写成一条测试：删死规则时按行数删，连着两次把隔壁的规则啃掉半截——
    一次吃了还在用的 `.t-dim{}`，一次吃了 `.modal{` 的头两行，
    还有一处更早的删除留下了没有选择器的声明体。CSS 坏规则浏览器不报错，
    它只是整条丢掉，于是界面"有点不对"但没人能指出哪里不对，也没有任何测试会红。
    """
    text = (FRONTEND / "css" / "app.css").read_text()
    depth = 0
    for n, line in enumerate(text.splitlines(), 1):
        depth += line.count("{") - line.count("}")
        assert depth >= 0, f"app.css:{n} 多出来一个 }}（上一轮删规则删坏了）"
    assert depth == 0, f"app.css 结尾还差 {depth} 个 }}"
    # 没有选择器的规则体：注释块刚结束就出现"缩进的声明行"
    lines = text.splitlines()
    depth = 0
    for n, line in enumerate(lines, 1):
        # 注释写在规则**体外**（depth 为 0）之后紧接一条声明，就是上一轮那种
        # "选择器被删了、声明体留着"的坏规则；写在规则体内是正常的解释，不报。
        if depth == 0 and n > 1 and lines[n - 2].strip().endswith("*/") \
                and re.match(r"\s+[\w-]+\s*:", line):
            raise AssertionError(f"app.css:{n} 是一条没有选择器的规则体：{line.strip()}")
        depth += line.count("{") - line.count("}")
        depth = max(depth, 0)


def test_建单路径只有这一份清单():
    """`job_repo.create` 的直调点必须还是这四个，第五个一冒头就红。

    `services.submit_job()` 的文档写着"业务提交都走这里"，实际有三条旁路
    （文档 AI／抽取／合成）——那三条各有各的闸门，但不能没人数着。
    这条测试不替它们辩护，只是把"旁路不许再长"钉住；
    要新增一条，就得同时在这里说清它的闸门是什么。
    """
    # 用 AST 数真调用，不按文本数：asset_studio.py 的模块注释里写着
    # "`job_repo.create()` 建过作业"，文本计数会把一句解释当成一条旁路。
    hits: dict[str, int] = {}
    for f in sorted(BACKEND.rglob("*.py")):
        if ".venv" in str(f) or "/tests/" in str(f):
            continue
        n = 0
        for node in ast.walk(ast.parse(f.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "create":
                base = func.value
                if isinstance(base, ast.Name) and base.id == "job_repo":
                    n += 1
        if n:
            hits[str(f.relative_to(BACKEND))] = n
    # 2026-11-08 `services.py` 拆成 `services/` 包后，这三处落在各自模块里：
    #   services/jobs.py            submit_job（唯一入口）
    #   services/doc_ai.py          文档 AI
    #   services/extract_candidates.py  抽取
    # composition 一处：合成建单，它带自己的幂等键与事务。
    # **这张表的意义不变**：旁路不许再长；要新增一条，就得同时说清它的闸门。
    assert hits == {
        "services/jobs.py": 1,
        "services/doc_ai.py": 1,
        "services/extract_candidates.py": 1,
        "composition/service.py": 1,
    }, (f"建单点与本表不符，新增旁路前先想清楚它过不过三条闸门：{hits}")


def test_告警标签和后端告警码对齐():
    """队列告警的中文名只有一份，且必须覆盖后端真会发出的那些 code。

    原来这一张表在两个视图里各有一份（生成中心叫 ALERT_CODE、首页叫 ALERT_LABEL），
    模型中心的运行监控干脆没写——同一句告警在生成中心是「待核对」、
    在模型中心是 `needs_attention`。英文 code 只该用于样式分档。
    """
    import re as _re

    js = (FRONTEND / "js" / "validation.js").read_text()
    table = _js_object_literal(js, "ALERT_LABEL =")
    # 后端唯一出处：`queue_health()` 里 `alerts.append({"code": ...})` 那几行。
    # 2026-11-08 `services.py` 拆成 `services/` 包后它们在 `services/jobs.py`；
    # 这里**扫整个包**而不是点某个文件——以后再挪位置，这条测试不该跟着改。
    backend = set()
    for src_file in sorted((BACKEND / "services").glob("*.py")):
        backend |= set(_re.findall(
            r'\{"level": "(?:bad|warn)", "code": "([a-z_]+)"',
            src_file.read_text(encoding="utf-8")))
    assert backend, "没在后端找到告警码，说明这条测试的取样方式过期了"
    assert set(table) == backend, f"前端 {sorted(table)} ≠ 后端 {sorted(backend)}"
    # 三个视图都不许再自带一份（2026-11-08 拆分后模型中心那份在 models/monitor.js）
    for rel in ("views/generate.js", "views/overview.js", "views/models/monitor.js"):
        src = (FRONTEND / "js" / rel).read_text()
        assert "alertLabel(" in src, f"{rel} 没走唯一出口"
        assert "const ALERT_CODE" not in src and "const ALERT_LABEL" not in src, \
            f"{rel} 又抄了一份告警标签"


def test_同视图重绘不重播入场动画():
    """切项目、保存、轮询都会走同一条重绘路径，它不许重播 `view-in`。

    病根：`router.js` 用 `root.classList.toggle("cold", firstPaint)` 决定要不要压掉
    入场动画，而 `firstPaint` 首屏之后恒为 false——于是每一次同视图刷新都重播一遍
    `view-in`（整块从透明+裁切变成不透明），而切项目一次连发两三条 refresh，
    肉眼就是连续闪屏。

    这条读源码而不是走浏览器：动画只在真浏览器里看得见，但"判据有没有带上
    sameView"在源码层面就能钉死，且比开一次 CDP 快三个数量级。
    """
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "router.js").read_text()
    code = "\n".join(line for line in js.split("\n")
                      if not line.strip().startswith(("//", "*", "/*")))
    # 入场动画的判据必须同时看首屏与同视图刷新
    assert "firstPaint || sameView" in code, (
        "cold 的判据又只判首屏了：同视图重绘会重播入场动画（切项目闪屏）")
    # sameView 必须算在 cold 之前——顺序反了它当时还是 undefined
    i_same = code.index("const sameView =")
    i_cold = code.index("classList.toggle('cold'")
    assert i_same < i_cold, "sameView 算在 cold 之后了，判据拿到的是旧值"


def test_切项目不连发三次整页重绘():
    """切一次项目不许派三条 refresh 各重绘一遍。

    原来 `store.select()` 派三次 `app:refresh`（立刻一次、loadEpisodes 回来一次、
    openProject + load 回来一次），每次都是整页 `innerHTML` 重写。
    即使动画已压掉，三次整页重写本身也会让列表闪、焦点丢。
    这条钉"最多三条，且立刻那条不许少"：切项目不能白屏等网络。
    """
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "store.js").read_text()
    seg = js[js.index("  select(id) {"):]
    seg = seg[:seg.index("\n  },")]
    n = seg.count("app:refresh")
    assert n <= 3, f"select() 派了 {n} 次 app:refresh，每次都是整页重写"
    # 立刻重绘那一次必须在（切项目不能白屏等网络）
    assert "先立刻重绘" in seg, "切项目不再立刻重绘了：点下去会像没反应"

def test_自绘下拉照抄原生控件的宽高():
    """夜间主题把 `<select>` 换成自绘按钮，两者的宽高必须一致。

    病根：外壳 `.fv-pick` 是 `width:100%` 按内容撑开，原生 `<select>` 多半带固定
    宽度（项目中心排序下拉 `.proj-sort{width:112px}`，实测 318→296）；高度上原生
    靠浏览器默认行高约 31px，自绘按钮 `padding:8px 10px` 约 38px。
    于是替换那一刻卡片头 56→63、整块跟着挪 —— 切项目时「列表先一个大小、
    后又变一个大小」就是这两处叠加。

    这条读源码：浏览器里量得出来，但"有没有抄宽度/高度"在源码层面就能钉死。
    """
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "pick.js").read_text()
    code = "\n".join(line for line in js.split("\n")
                      if not line.strip().startswith(("//", "*", "/*")))
    assert "wrap.style.width = cs.width" in code, "外壳没抄原生宽度"
    assert "wrap.style.height = cs.height" in code, "外壳没抄原生高度"
    assert "btn.style.height = selHeight" in code, "按钮没跟外壳同高"


def test_重绘当场就补自绘下拉():
    """`router.renderView` 必须在同一次渲染里调 `applyPicks`，不能只靠 80ms 去抖。

    `app.js` 的 MutationObserver 有 80ms 去抖；只靠它的话，每次重绘都会先
    摆出原生控件、80ms 后才换成自绘的。同帧补一次，中间态就没有了。
    """
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "router.js").read_text()
    code = "\n".join(line for line in js.split("\n")
                      if not line.strip().startswith(("//", "*", "/*")))
    assert "applyPicks(root)" in code, "路由没在渲染里补 applyPicks"
    i_mount = code.index("view.mount?.(root)")
    i_pick = code.index("applyPicks(root)")
    assert i_mount < i_pick, "applyPicks 要在 mount 之后（视图可能刚插了 select）"

def test_顶栏不再重复显示_worker_状态():
    """worker 在线与否已在侧栏底部有一行，顶栏不许再摆一颗同名胶囊。

    两处同名同状态，人还得想「这俩是不是不一样」。撤顶栏那一颗，
    留侧栏那份（它更全：带灯，且和队列、常驻模型排在一起）。
    """
    from pathlib import Path

    html = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text()
    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "app.js").read_text()
    assert 'id="worker-pill"' not in html, "顶栏那颗 worker 胶囊又回来了"
    assert "worker-pill" not in js, "app.js 还在找一颗已不存在的胶囊"
    # 侧栏那一份必须还在（撤的不是状态本身）
    assert 'id="worker-label"' in html and 'id="worker-dot"' in html


def test_侧栏队列那行也有圆点():
    """队列那行原来没有点，与上面两行的圆点对不齐，看着像漏画了一个图标。

    点表达的是「队列有没有活」，与 worker 点不是同一件事（在不在线），
    所以各有各的点、各用各的亮法，不合并成一颗。
    """
    from pathlib import Path

    html = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text()
    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "app.js").read_text()
    assert 'id="queue-dot"' in html, "队列那行又没点了"
    assert "queue-dot" in js and "queueDot.className" in js, "队列的点没人画"


def test_侧栏底部不再挂快速跳转提示():
    """`⌘K 快速跳转` 那行小字收掉了，但功能必须还在。

    撤的是提示，不是入口：按 ⌘K / Ctrl+K 仍能唤起命令面板。
    这条同时钉两件事，免得下次有人「顺着搜索结果」把快捷键一起删了。
    """
    from pathlib import Path

    html = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text()
    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "app.js").read_text()
    # 只查可见行：HTML 注释里说明「这行为什么撤」是必要的，不该被判成提示还在。
    visible = "\n".join(line for line in html.split("\n")
                          if "<!--" not in line and "-->")
    assert "快速跳转" not in visible, "侧栏底部又挂上快速跳转提示了"
    assert "'k'" in js and "pal.hidden ? open() : close()" in js, (
        "⌘K 快捷键没了：撤的该是提示，不是功能")

def test_模型仓库筛选下拉有不折行的宽度():
    """「全部类型／全部来源」两个下拉必须有显式宽度，且自绘按钮不许折行。

    病根：原生 `<select>` 没给宽度时按**最短选项**算宽（实测四字只给 92px），
    夜间主题换成自绘按钮后 pick.js 把这个宽度照抄过去，文字就折成两行。
    给 .repo-filter 一个够放一行文字＋箭头的宽度，并压住 nowrap。
    """
    from pathlib import Path

    # 2026-11-08 拆分后模型仓库的标记住在 models/repo.js
    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "views" / "models" / "repo.js").read_text()
    css = (Path(__file__).resolve().parents[2] / "frontend" / "css" / "app.css").read_text()
    assert js.count('class="ctl repo-filter"') == 2, "两个筛选下拉没都带上 repo-filter 类"
    assert ".repo-filter{width:" in css, "没给显式宽度（原生按最短选项算 92px 会折行）"
    assert "white-space:nowrap" in css, "自绘按钮没压住折行"

def test_下拉浮层宽度跟着按钮与最长选项定():
    """浮层不该比按钮宽——除非按钮窄到装不下选项文字。

    原来写死 `${Math.max(width, 160)}px`：160 这个下限对按钮本来就够宽的场合
    是纯多余（模型仓库那两个筛选下拉 120px，浮层被顶到 160px，看着比控件宽一截，
    而里面的字根本没溢出）。现在改成「按钮宽度」与「最长选项文字宽度」取大。

    这条钉住那个 160 不许回来：它是拍出来的数，不是量出来的。
    """
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "pick.js").read_text()
    code = "\n".join(line for line in js.split("\n")
                      if not line.strip().startswith(("//", "*", "/*")))
    assert "Math.max(Math.round(r.width), 160)" not in code, "浮层宽度又写死 160 了"
    assert "measureText" in code, "没按选项文字量宽度"
    assert "list.style.minWidth" in code

def test_带布局属性的排版类不许直接用在单元格上():
    """把「哪些类能挂 <td> 上」判成一条可执行的规则，而不是只认识 .t-desc。

    病根（2026-11-07 实测）：.t-desc{display:block;max-width:320px} 本来是给
    单元格**里面**那段次要文字用的，却被直接套在 <td> 上。那一格于是丢掉
    table-cell 布局（实测 display=block）、再被 320px 限宽，浏览器只好重新
    分配整张表的列宽——「必需标签」撑到 277px 空白、「建议轨」挤成 110px。
    当时全库 16 处，靠一条只认识 .t-desc 的守卫抓了 6 处漏网的。

    现在从 CSS 推导：凡是自带 display / max-width / min-width / width /
    position / overflow 的类，都不许出现在 <td class=…> 上——它们会改掉
    单元格的布局角色。纯字色、字族、对齐（.hint／.num／.r／.mono）不受限。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "frontend"
    css = (root / "css" / "app.css").read_text(encoding="utf-8")
    views = root / "js" / "views"

    # 有布局副作用的类（从 CSS 读出来，不手抄一份）
    risky = []
    for m in re.finditer(r"^\.([a-z][\w-]*)\{([^}]*)\}", css, re.M):
        name, body = m.group(1), m.group(2)
        if re.search(r"\b(display|max-width|position|overflow)\s*:", body):
            risky.append(name)
    assert "t-desc" in risky, "取样失败：.t-desc 应当被判为带布局属性"

    allowed = {"r", "num", "mono", "hint"}
    banned = [c for c in risky if c not in allowed]

    offenders = []
    for f in list(views.glob("*.js")) + list(views.rglob("models/*.js")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            m = re.search(r'<td[^>]*class="([^"]*)"', line)
            if not m:
                continue
            hit = [c for c in m.group(1).split() if c in banned]
            if hit:
                offenders.append(f"{f.name}:{n} 把 {hit} 挂在 <td> 上")
    assert not offenders, ("这些单元格挂了会改布局角色的类（应在格内包一层 span）："
                          + "；".join(offenders))

def test_视图里的_window_监听必须成对解绑():
    """视图里挂在 window／document 上的监听，必须由自己解绑。

    为什么单列一条：视图的 mount 每次渲染都跑一遍，而 #view-root 的 innerHTML
    会被整块换掉——挂在**节点**上的监听随节点回收，挂在 **window** 上的不会。
    每渲染一次加一个、切走不解，人点几下就会有一串重复回调（旧闭包还拿着
    已经不在文档里的节点，写进去没有效果，但每次事件都白跑一遍）。

    2026-11-08 实测：全库 addEventListener 379 处，其中真正挂在 window／document
    上的只有 17 处；视图里只有 projects.js 的行菜单那两处，而且 arm/disarm 成对。
    所以这条守卫是**清单式**的：允许的写法写死，新加一处就得先回答它何时解绑。
    """
    from pathlib import Path

    views = Path(__file__).resolve().parents[2] / "frontend" / "js" / "views"
    allowed = {
        "projects.js": "行菜单的 scroll/resize 由 armRowMenu 挂、disarmRowMenu 解",
        # `visibilitychange` 只在 `document` 上派发，**不冒泡到普通元素**，
        # 所以除了 document 没有别处可挂（挂 root 收不到）。成对解绑已实现：
        # 模块级 `visHandler` 记住这一次挂的闭包，下次 mount 开头先
        # `removeEventListener` 再重新挂。这里不能靠 `root.isConnected` 兜底——
        # `root` 是 `#view-root`，换页时它仍在 body 里，那个判断永远为真。
        "generate.js": "回前台补一拍的 visibilitychange，挂 document（不冒泡），"
                       "由模块级 visHandler 在下次 mount 开头成对解绑",
    }

    offenders = []
    files = list(views.glob("*.js")) + list(views.rglob("models/*.js"))
    for f in files:
        if f.name in allowed:
            continue
        text = f.read_text(encoding="utf-8")
        # 去掉注释行再找，避免注释里提到 window.addEventListener 被误判
        code = "\n".join(l for l in text.splitlines()
                         if not l.strip().startswith(("//", "*", "/*")))
        if "window.addEventListener" in code or "document.addEventListener" in code:
            offenders.append(f.name)
    assert not offenders, (
        "这些视图往 window/document 上挂了监听但不在允许清单里："
        + "、".join(offenders)
        + "。要么改成挂节点、要么成对解绑、要么把理由写进 allowed 表")


def test_可点的重试按钮都必须接上线():
    """`util.errorBox()` 画的重试默认是**可点**的，两处都得有人接住。

    2026-11-10 整库审计逐文件普查得到的事实：
      * `errorBox(what, err, retry=true)` 第三参不显式给 `false` 时渲染
        `<button data-retry>`（可点）；给 `false` 才是 `disabled`，不用接。
      * **全站没有任何 `document`/容器级的 `[data-retry]` 委托**
        （`generate.js` 那两处是在它自己的表体上 `closest` 委托，
        且两处调用都显式传了 `false`），
        所以每个渲染它的视图都得自己绑。
      * 审计当时 `agents.js`(5 处)、`audio.js`(1 处)、`review.js`(1 处)、
        `composition.js`(1 处) 都只渲染不绑；`writing.js` 与
        `review-actions.js` 只在单数 `querySelector` 上绑了一颗，
        同屏第二张卡的按钮仍是死的。点了没反应、不发请求、不变灰、
        控制台也不报错——后端测试完全摸不到，因为它整个活在浏览器里。

    于是分两条判据（缺任何一条都会漏掉一类真 bug）：
      **A 同步渲染**：文件里有可点的 `errorBox` 出现在**返回 HTML 的模板**里
         （`return` / 模板插值），就必须存在**复数**接线
         `querySelectorAll('[data-retry]')`——单数只绑第一颗，
         同屏两张卡（`writing.js` 的 :1346 与 :1506）就漏一颗。
      **B 异步换节点**：`errorBox` 出现在 `innerHTML =`/`outerHTML =` 赋值里，
         mount 顶层那次绑不住它（节点是刚换上的、而且 mount 只跑一遍），
         必须在**同一函数往下几行内**看到接线。
    """
    import re

    views = FRONTEND / "js" / "views"
    files = list(views.glob("*.js")) + list(views.rglob("models/*.js"))

    def code_of(text: str) -> str:
        """注释行**置空而不是删掉**，好让报出来的行号就是文件里的行号。

        原来这里直接把这些行滤掉，于是断言里写的「第 252 行」是压缩后的序号，
        人照着行号去看会看到另一段代码——守卫测试给出的位置必须是可跳的。
        """
        return "\n".join("" if l.strip().startswith(("//", "*", "/*")) else l
                         for l in text.splitlines())

    call_re = re.compile(r"errorBox\(([^()]*)\)")
    plural_re = re.compile(r"querySelectorAll\(['\"][^'\"]*data-retry")
    async_re = re.compile(r"(innerHTML|outerHTML)\s*=.*errorBox\(")
    any_wire_re = re.compile(
        r"(?:querySelector(?:All)?\(|closest\()['\"]?[^)\]]*data-retry")

    def clickable_calls(text: str):
        """返回 [(行号, 是否可点)]：第三参显式 false 的不算死按钮。"""
        out = []
        for n, line in enumerate(text.split("\n"), 1):
            for m in call_re.finditer(line):
                args = [a.strip() for a in m.group(1).split(",")]
                out.append((n, not (len(args) >= 3 and args[2] == "false")))
        return out

    offenders = []
    for f in files:
        code = code_of(f.read_text(encoding="utf-8"))
        calls = clickable_calls(code)
        if not any(ok for _, ok in calls):
            continue
        lines = code.split("\n")

        def is_async_swap(n: int) -> bool:
            """这一处 errorBox 是不是「换整块节点」那类。

            `holder.outerHTML = \'<div>\' + errorBox(...)` 是跨行拼的，
            所以只看当前行会把这类误判成模板那类（规则 A），
            给人一条方向不对的建议。往前看两行足够覆盖本仓两种写法。
            """
            for k in range(max(0, n - 3), n):
                if async_re.search(lines[k]) or re.search(
                        r"(innerHTML|outerHTML)\s*=", lines[k]):
                    return True
            return False

        # A：模板里的可点卡 -> 必须有复数接线
        tpl = [n for n, ok in calls
               if ok and not is_async_swap(n)]
        if tpl and not plural_re.search(code):
            offenders.append(
                f"{f.name}: 第 {tpl} 行在模板里画了可点重试，"
                f"但没有复数接线（单数只绑第一颗，同屏第二张卡就是死的）")

        # B：异步换节点画的可点卡 -> 同函数内几行必须接线
        for n, ok in calls:
            if not ok or not is_async_swap(n):
                continue
            window = "\n".join(lines[n - 1:n + 11])
            if not any_wire_re.search(window):
                offenders.append(
                    f"{f.name}: 第 {n} 行用 innerHTML/outerHTML 换了整块节点再画"
                    f"可点重试，紧接着没绑——mount 顶层那次绑不到新节点")
    assert not offenders, (
        "这些重试按钮点了没有任何反应（静默失败）：\n  "
        + "\n  ".join(offenders)
        + "\n要么绑上（本仓做法：mount 顶层用复数 querySelectorAll，"
          "异步换节点的在渲染后立刻绑新节点），"
          "要么确认这里不该给重试、显式传 `false`")


def test_云端提交必须入口即置busy否则连点会双份计费():
    """`generate.js` 的 `submitJob` 必须在**任何 await 之前**挡住第二次点击。

    2026-11-10 审计实测的真 bug：原来 `btn.disabled = true` 写在
    `await api.precheckShot(...)` **之后**（原 :1575，改后见文件），也就是
    从点击到按钮变灰之间隔着一整趟网络往返。窗口期内连点两下，两趟都跑到底，
    于是 `api.submitJob` 被调用两次——而这条路径正是「云端要勾选确认按供应商
    计费」的那条，**重复提交就是重复花钱**。本地轨重复提交只是多一条废作业。

    这条是浏览器行为，后端测试摸不到（它整个活在点击与网络往返之间），
    所以只能在这里做源码结构检查。判据三条，缺一条就漏一类真 bug：

      A **入口置位**：函数体里第一个 `await` 之前必须出现 busy 守卫与置位。
        只把 `disabled` 提前到 `await` 前还不够——按钮还没变灰的那一下点击
        仍然排进了事件队列，变量守卫才是硬的。
      B **提前返回有兜底**：函数里有多处 `return`（没选项目、没写提示词、
        没有槽位、云端数量/单价/来源没填、没勾确认、预检阻塞，共 6 处）。
        任一提前 return 若不复位 busy，这个弹窗的提交按钮就**永久变灰**，
        人只能刷新整页。所以必须有 `finally` 复位。
      C **不能只靠按钮 disabled**：`disabled` 在同一次事件循环里挡不住已经
        排队的第二个 handler，必须有一个模块级布尔位。
    """
    raw = (FRONTEND / "js" / "views" / "generate.js").read_text(encoding="utf-8")
    # **必须先去掉注释行**：上面那段说明里就写着 `if (submitBusy) return;` 这几个
    # 字符，不去注释的话断言会被自己的注释满足（这条测试第一版就是这么假绿的）。
    # 置空而不删行，报出来的行号才和文件一致。
    src = "\n".join("" if l.strip().startswith(("//", "*", "/*")) else l
                    for l in raw.splitlines())
    start = src.index("async function submitJob()")
    # 函数到下一个同缩进 `}` 为止（下一行以 `    }` 开头）
    body_lines = []
    for line in src[start:].split("\n")[1:]:
        if line == "    }":
            body_lines.append(line)
            break
        body_lines.append(line)
    body = "\n".join(body_lines)
    assert "api.submitJob" in body, "定位错了函数？submitJob 里没有 api.submitJob"

    first_await = body.find("await ")
    assert first_await > 0, "submitJob 里没有 await？"
    head = body[:first_await]

    # A：入口守卫 + 置位都在第一个 await 之前
    assert re.search(r"if\s*\(\s*submitBusy\s*\)\s*return\s*;", head), (
        "submitJob 的第一个 await 之前没有 `if (submitBusy) return` —— "
        "从点击到按钮变灰之间的第二下点击会再提交一条作业；"
        "云端那条路径重复提交＝重复计费")
    assert re.search(r"submitBusy\s*=\s*true", head), (
        "submitJob 没有在第一个 await 之前把 busy 置 true")

    # B：提前 return 必须有 finally 兜底
    assert "finally" in body, (
        "submitJob 里有多处提前 return，却没有 finally 复位 busy —— "
        "任何一条校验不通过（没选项目、没写提示词…）都会让提交按钮永久变灰")
    tail = body[body.rindex("finally"):]
    assert re.search(r"submitBusy\s*=\s*false", tail), (
        "finally 里没有把 submitBusy 复位")

    # C：模块级声明（放在 mountMonitor 的作用域里，与 drawerBusy 同层）
    assert re.search(r"let\s+submitBusy\s*=\s*false", src), (
        "submitBusy 没有模块级声明，运行时会 ReferenceError")


def test_轮询必须让开后台标签页并成对解绑():
    """三个轮询点都不能在人看不见的时候照跑，且挂 document 的要能解绑。

    2026-11-10 审计：`monitor.js:642` 与 `app.js:797` 早有 `document.hidden`
    判断（注释写着「与全站轮询同一纪律」），但 `generate.js` 的 3 秒队列轮询
    与 `notifications.js` 的角标轮询**都没有**——人切到别的标签页干活时照跑，
    而屏幕上没有"让人知道"的对象。generate.js 一轮还是三个请求
    （队列＋成本账＋worker 状态）。

    判据分三段，各自防一种退化：
      A 每个轮询回调里必须有 `document.hidden` 提前 return；
      B 跳过后必须**补一拍**（否则切回来看到的是离开那一刻的旧队列），
        且补拍挂在 `visibilitychange` 上；
      C 挂 document 的监听必须成对解绑——`#view-root` 换页时仍在 body 里，
        `isConnected` 挡不住，只能靠 removeEventListener。
    """
    js = FRONTEND / "js"
    gen = (js / "views" / "generate.js").read_text(encoding="utf-8")
    notif = (js / "notifications.js").read_text(encoding="utf-8")

    def code_of(text: str) -> str:
        return "\n".join("" if l.strip().startswith(("//", "*", "/*")) else l
                         for l in text.splitlines())

    gen_code, notif_code = code_of(gen), code_of(notif)

    # A：两处轮询都要让开
    assert re.search(r"if\s*\(\s*document\.hidden\s*\)\s*return", gen_code), (
        "generate.js 的轮询没有 `document.hidden` 判断，后台标签页照跑三个请求")
    assert re.search(r"if\s*\(\s*document\.hidden\s*\)\s*return", notif_code), (
        "notifications.js 的角标轮询没有 `document.hidden` 判断")

    # B：回前台补一拍
    assert "visibilitychange" in gen_code, (
        "generate.js 只是跳过、没有回前台补一拍——人切回来看到的是旧队列")
    assert re.search(r"addEventListener\(\s*['\"]visibilitychange['\"]", gen_code)

    # C：成对解绑（记下闭包 → 下次 mount 先 remove 再 add）
    assert re.search(r"removeEventListener\(\s*['\"]visibilitychange['\"]",
                     gen_code), (
        "generate.js 挂了 visibilitychange 却不解绑：`#view-root` 换页后仍在 "
        "body 里，isConnected 挡不住，切十个来回会叠十份监听")


def test_抽屉重画必须保住_details_展开态():
    """抽屉每 3 秒整块重绘，展开着的 `<details>` 会被缩回去。

    2026-11-10 审计实测：`loadDrawer()` 里 `drawerBody.innerHTML = drawerHtml(...)`，
    而抽屉里的「实际命令（argv 数组…）」正是 `<details class="cmd-details">`
    （`commandHtml`）。展开态住在 DOM 属性里，整块换掉就没了——人刚点开的那一节
    每 3 秒自动缩回去，作业跑得越久（本地视频几分钟）越像界面抽风。

    判据：重绘前把 `details` 的 `open` 下标记下来，画完按下标恢复。
    纯文本扫描（前端没有 JS 测试框架），但足以钉住"有没有这段恢复逻辑"。
    """
    gen = (FRONTEND / "js" / "views" / "generate.js").read_text(encoding="utf-8")
    code = "\n".join("" if l.strip().startswith(("//", "*", "/*")) else l
                     for l in gen.splitlines())
    idx = code.index("drawerBody.innerHTML = drawerHtml(")
    before = code[max(0, idx - 900):idx]
    after = code[idx:idx + 700]
    assert "querySelectorAll('details')" in before or 'querySelectorAll("details")' in before, (
        "重画抽屉之前没有记下哪些 details 是展开的")
    assert re.search(r"\.open\s*=\s*true", after), (
        "重画抽屉之后没有恢复 details 的展开态")


def test_通知角标轮询不要拉全量正文():
    """菜单关着时只要未读数，不必把 30 条正文拉回来。

    2026-11-10 审计：角标用的是 `payload.unread`——服务端独立那条 COUNT 查询
    （`notifications.py:190-196`），与 `items` 长度无关。菜单关着的每一轮轮询把
    30 条正文拉回来、赋值给 `items`、然后没人看，下一次又被覆盖。
    `api.notifications(true)` 走 `unread_only=true`，`unread` 仍是全量未读数，
    角标口径不变。

    但**只在菜单关着时省**：菜单开着必须拉全量，否则已读通知会从眼前消失
    （`:166` 点条目后 `:175` 还要 `items.find` 找到那一条）。判据钉住这个方向。
    """
    notif = (FRONTEND / "js" / "notifications.js").read_text(encoding="utf-8")
    code = "\n".join("" if l.strip().startswith(("//", "*", "/*")) else l
                     for l in notif.splitlines())
    assert re.search(r"api\.notifications\(\s*!\s*menu\s*\)", code), (
        "菜单关着时没有走 unread_only，仍在拉全量正文")
    assert "payload?.unread" in code, (
        "角标没有读服务端独立返回的 unread 字段")


def test_资产类型中文名只有一份且与后端一致():
    """同一个 `character` 在资产这两屏必须都叫「角色」。

    2026-11-10 裁决时的实测冲突：服务端权威 `schemas/enums.py` 的
    `ASSET_KINDS` 写的是「角色」，`assets.js`／`writing.js`／`asset-detail.js`
    也都是「角色」，唯独 `asset-studio.js` 与 `asset-triage.js` 自己写了一份
    「人物」。同一件东西在「资产创作」和「资产库」里叫两个名字，人对着两屏
    会以为不是一类。

    注意判据只扫**资产类型**这一层，不是把全仓的「人物」都判死：
    「人物档案」是 `character_profile`（后端 `ASSET_GEN_OPERATIONS` 叫
    「人物文字设定」）——那是 asset 的附属文本，不是资产类型，沿用自己说法是对的。
    所以这里锚的是 `{ character: '…' }` / `['character', '…']` 这种**类型映射**，
    扫不到自然语言里正常使用"人物"的地方。
    """
    import schemas.enums as backend_enums

    js = (FRONTEND / "js" / "enums.js").read_text(encoding="utf-8")
    table = _js_object_literal(js, "ASSET_KIND_LABELS =")
    assert table == {k: f"'{v}'" for k, v in backend_enums.ASSET_KINDS.items()}, (
        f"ASSET_KIND_LABELS 与后端 ASSET_KINDS 不一致："
        f"前端 {table}／后端 {backend_enums.ASSET_KINDS}")

    # 视图里不许再自己写一份资产类型译名（抄一份就又会分叉）
    offenders = []
    views = FRONTEND / "js" / "views"
    for f in list(views.glob("*.js")) + list(views.rglob("models/*.js")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith(("//", "*", "/*")):
                continue
            if re.search(r"\{[^}]*\bcharacter\s*:\s*'[^']*[一-鿿]", line) or \
               re.search(r"\[\s*'character'\s*,\s*'[^']*[一-鿿]", line):
                offenders.append(f"{f.name}:{n}: {line.strip()[:70]}")
    assert not offenders, (
        "这些行又本地写了一份资产类型译名，改成从 `enums.js` 的 "
        "ASSET_KIND_LABELS 取：\n  " + "\n  ".join(offenders))


def test_mypy_门禁真的在检查而不是空转():
    """mypy 是渐进式的，范围只有 `schemas/` 与 `db.py`——但必须**真的在查**。

    2026-11-10 加这一层时踩过的同类风险的现实版本：本仓 `ruff` 那条
    `test_测试里不再引用未定义的变量名` 在 venv 没有 ruff 时一直
    `pytest.skip`，装上前从未真跑过（装上后立刻抓到一条既有的 F821）。
    静态检查最容易变成"配好了、看着绿、其实什么都没查"。

    所以这条做两件事，缺一条都可能是空转：
      A **配置范围是写死的**：`files` 必须正好覆盖 schemas 与 db.py，
        不能被人悄悄改成 `files = []` 或挪到别的目录（挪走＝不查）；
      B **拿一个真错误去试**：临时写一个类型错误进 schemas/，mypy 必须报出来。
        这才是"它在检查"的证据，而不是"它退出了 0"。
    """
    import subprocess
    import sys

    root = BACKEND          # app/backend：pyproject.toml 与 schemas/ 都在这
    conf = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.mypy]" in conf, "pyproject.toml 里没有 [tool.mypy] 段"
    mypy_cfg = conf[conf.index("[tool.mypy]"):]
    assert 'files = ["schemas", "db.py"]' in mypy_cfg, (
        f"mypy 的检查范围被改了，必须仍是 schemas 与 db.py：{mypy_cfg[:200]}")

    # B：探针。写进 schemas/ 再删掉——用 try/finally 保证不留下文件
    probe = root / "schemas" / "_mypy_probe_tmp.py"
    try:
        probe.write_text('x: int = "这明明是字符串"\n', encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "mypy"],
            cwd=root, capture_output=True, text=True, timeout=300)
        if proc.returncode == 1 and "No module named mypy" in proc.stderr:
            pytest.skip("这台机器没装 mypy（开发依赖，见 requirements-dev.txt）")
        assert proc.returncode != 0, (
            "往 schemas/ 里塞了一个类型错误，mypy 仍然退出 0 —— "
            "这层门禁在空转（要么没扫到，要么配置把错误吞了）")
        assert "_mypy_probe_tmp.py" in (proc.stdout + proc.stderr), (
            f"mypy 报了错但不是探针文件，说明它没在查 schemas/：{proc.stdout[:300]}")
    finally:
        probe.unlink(missing_ok=True)
