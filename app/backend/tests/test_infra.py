"""W01 的基础设施与真实行为回归：迁移/备份/引导口令/上传安全/层级/前端出口。

这一份的重点不是"功能多"，而是**每一条都能证伪**：
断言都写具体值或具体集合，不写 `assert r.status_code < 500` 这种永远绿的。
"""
import ast
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
    """统一错误出口的守门人：有人新写 alert 就会红，逼他走 run()。

    不写行号——util.js 加几个函数行号就飘了，那种断言只会制造无谓的改动。
    """
    hits = []
    for f in (FRONTEND / "js").rglob("*.js"):
        for line in f.read_text().split("\n"):
            if re.search(r"(?<![\w.])alert\s*\(", line) and not line.strip().startswith("//"):
                hits.append(f.name)
    assert set(hits) == {"util.js"}, f"alert 出现在不该出现的地方：{sorted(set(hits))}"


def test_危险确认不再用原生_confirm():
    """W02-c：删集／删镜／停用账号原来用原生 confirm()，跟两套主题脱节，
    而且"删除"和"知道了"长得一样。现在全站一套 confirmDanger。"""
    hits = []
    for f in (FRONTEND / "js" / "views").glob("*.js"):
        for n, line in enumerate(f.read_text().split("\n"), 1):
            if re.search(r"(?<![\w.])confirm\s*\(", line) and not line.strip().startswith("//"):
                hits.append(f"{f.name}:{n}")
    assert not hits, f"还在用原生 confirm()：{hits}"


def test_每张表都在横向滚动容器里():
    """W02-d：卡片是 overflow:hidden，窄屏时列被裁掉又没有滚动条。"""
    offenders = []
    for f in (FRONTEND / "js" / "views").glob("*.js"):
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

    v0.99 起 system-config.js 不在名单里了：它只剩一个二级页，
    整页转发给 system-users.js，页内切换器由后者画（最后一行查的就是它）。
    模型中心四个工作区是新的多页模块，所以进名单。
    """
    for name in ("assets.js", "writing.js", "models.js"):
        text = (FRONTEND / "js" / "views" / name).read_text()
        assert "data-kind" in text or "subChipRow" in text, f"{name} 缺页内切换器"
    assert "subChipRow" in (FRONTEND / "js" / "views" / "system-users.js").read_text()


def test_页内切换器改完导航必须重渲染():
    """上一条只查"有没有第二个入口"，这条查"这个入口按了有没有反应"。

    2026-11-04 实测：assets.js 的工作区切换器写成 `bindSubChips(root, (i) => {
    setWsIndex(i); })`——只写导航状态、不重渲染，而 ≤860px 时侧栏那一层整块隐藏，
    这一排 chip 是唯一出口，于是"点了没反应、下次刷新莫名跳过去"。全仓八处调用
    只有这一处漏，正是"同一个规矩八处各写一遍"的下场，所以钉成文件级判据。
    """
    bad = []
    for f in (FRONTEND / "js" / "views").glob("*.js"):
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
    view = (FRONTEND / "js" / "views" / "models.js").read_text()
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


def test_视图不再自己写第二份状态字典():
    """`views/*.js` 里不许再出现 job 状态字面量的第二份表。

    上一轮是"复制一份字典"，这一轮起是"import 那一份"。守的是复制这个动作，
    所以扫描整个目录而不是点某几个文件——新页面也会犯同样的错。
    """
    offenders = []
    for f in (FRONTEND / "js" / "views").glob("*.js"):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.match(r"\s*(queued|starting|needs_attention|cancel_requested):"                          r"\s*(\[|'|\{)", line):
                offenders.append(f"{f.name}:{n}")
    assert not offenders, f"这些行又在视图里抄了一份作业状态表：{offenders}"


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
    # services.py 三处：submit_job（唯一入口）／文档 AI／抽取；
    # composition 一处：合成建单，它带自己的幂等键与事务。
    assert hits == {"services.py": 3, "composition/service.py": 1}, (
        f"建单点与本表不符，新增旁路前先想清楚它过不过三条闸门：{hits}")
