"""资产中心封版的行为测试（指令 §九 的后端那一半）。

三条最值得盯的：
- 全局资产的重名必须拦得住——SQLite 认为 NULL 互不相等，
  0012 把唯一索引拆成两条部分索引就是为了这件事。
- 删项目不能删掉全局资产（指令 §5.3）。机制是外键按 project_id = ? 匹配、
  NULL 比不中，所以这条测的是"我没写错注释"。
- 被镜头引用的资产不给删，且不给 force 旁路。
"""
import uuid


from db import get_conn


def _png() -> bytes:
    return bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                         "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")


def _mk_asset(client, pid, **over):
    body = {"kind": "character", "name": f"角色_{uuid.uuid4().hex[:6]}", **over}
    r = client.post(f"/api/projects/{pid}/assets", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- 全局资产池
def test_全局资产只有管理员能建(as_role, fresh_project):
    pid = fresh_project("editor")
    name = f"全局_{uuid.uuid4().hex[:6]}"
    denied = as_role("editor").post(f"/api/projects/{pid}/assets",
                                    json={"kind": "scene", "name": name, "scope": "global"})
    assert denied.status_code == 403, denied.text
    assert "全局" in denied.json()["detail"]

    ok = as_role("admin").post(f"/api/projects/{pid}/assets",
                               json={"kind": "scene", "name": name, "scope": "global"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["project_id"] is None, "全局资产的归属必须是 NULL"


def test_两条全局资产同名要拦住(as_role, fresh_project):
    """部分唯一索引的验收点：SQLite 里 NULL 彼此不相等，
    一条 (project_id, kind, name) 唯一索引对全局资产等于不设防。"""
    pid = fresh_project("editor")
    c = as_role("admin")
    name = f"同名的灯_{uuid.uuid4().hex[:6]}"
    first = c.post(f"/api/projects/{pid}/assets",
                   json={"kind": "prop", "name": name, "scope": "global"})
    assert first.status_code == 201
    dup = c.post(f"/api/projects/{pid}/assets",
                 json={"kind": "prop", "name": name, "scope": "global"})
    assert dup.status_code == 409, "全局池里重名必须撞唯一约束"


def test_不同范围可以同名(as_role, fresh_project):
    """全局有一条"掌灯人"、项目里也有一条"掌灯人"是允许的——
    指令 §4.2 的唯一性口径是"同归属范围、同类型内"。"""
    pid = fresh_project("editor")
    name = f"复用角色_{uuid.uuid4().hex[:6]}"
    g = as_role("admin").post(f"/api/projects/{pid}/assets",
                              json={"kind": "character", "name": name, "scope": "global"})
    assert g.status_code == 201
    own = as_role("editor").post(f"/api/projects/{pid}/assets",
                                 json={"kind": "character", "name": name})
    assert own.status_code == 201, own.text
    assert own.json()["project_id"] == pid


def test_全局资产人人可读不可改(as_role, fresh_project):
    pid = fresh_project("editor")
    g = _mk_asset(as_role("admin"), pid, scope="global")
    c = as_role("editor")
    assert c.get(f"/api/assets/{g['id']}").status_code == 200
    r = c.put(f"/api/assets/{g['id']}", json={**g, "summary": "偷偷改一下"})
    assert r.status_code == 403, "能查看绝不等于能修改（指令 §5.3）"


def test_列表按范围筛(as_role, fresh_project):
    pid = fresh_project("editor")
    own = _mk_asset(as_role("editor"), pid)
    glob = _mk_asset(as_role("admin"), pid, scope="global")
    c = as_role("editor")
    ids = lambda scope: {a["id"] for a in
                         c.get(f"/api/projects/{pid}/assets", params={"scope": scope}).json()}
    # 用包含而不是集合相等：整个 session 共用一个库，别的用例也建过全局资产，
    # 写 == 等于假设库里只有自己，那是测试的错不是产品的错
    assert own["id"] in ids("all") and glob["id"] in ids("all")
    assert own["id"] in ids("project") and glob["id"] not in ids("project")
    assert glob["id"] in ids("global") and own["id"] not in ids("global")
    bad = c.get(f"/api/projects/{pid}/assets", params={"scope": "whatever"})
    assert bad.status_code == 422


def test_删项目不会删掉全局资产(as_role, fresh_project):
    """机制上靠外键：全局行的 project_id 是 NULL，CASCADE 匹配不到它。
    这条测试存在的意义是——将来有人"顺手"改成先删 asset 再删 project，这里会红。"""
    pid = fresh_project("admin")
    g = _mk_asset(as_role("admin"), pid, scope="global")
    r = as_role("admin").request("DELETE", f"/api/projects/{pid}",
                                 params={"confirm": as_role("admin").get(
                                     f"/api/projects/{pid}/summary").json()["name"]})
    assert r.status_code == 204, r.text
    assert as_role("editor").get(f"/api/assets/{g['id']}").status_code == 200, "全局资产被误删了"


# ---------------------------------------------------------------- 编辑
def test_整体编辑生效并带标签别名(as_role, fresh_project):
    pid = fresh_project("editor")
    a = _mk_asset(as_role("editor"), pid)
    c = as_role("editor")
    r = c.put(f"/api/assets/{a['id']}", json={
        "kind": "character", "name": a["name"], "summary": "掌灯的老者",
        "consistency_prompt": "瘦高，平涂", "aliases": ["提灯人", "  ", "提灯人"],
        "tags": ["主角", "主角", "夜戏"], "scope": "project"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == "掌灯的老者"
    assert body["aliases"] == ["提灯人"], "别名要去空白去重"
    assert body["tags"] == ["主角", "夜戏"]
    # 详情页要摆这两个时间；schema 漏带字段时界面会显示 undefined，只有断言拦得住
    assert body["created_at"] and body["updated_at"]


def test_编辑成重名要_409(as_role, fresh_project):
    pid = fresh_project("editor")
    a = _mk_asset(as_role("editor"), pid, kind="scene")
    b = _mk_asset(as_role("editor"), pid, kind="scene")
    r = as_role("editor").put(f"/api/assets/{b['id']}",
                              json={"kind": "scene", "name": a["name"]})
    assert r.status_code == 409


def test_全局资产不能收回成项目资产(as_role, fresh_project):
    """它可能正被多个项目的镜头引用，挪进一个项目等于让其他引用悬空。"""
    pid = fresh_project("editor")
    g = _mk_asset(as_role("admin"), pid, scope="global")
    r = as_role("admin").put(f"/api/assets/{g['id']}",
                             json={"kind": g["kind"], "name": g["name"], "scope": "project"})
    assert r.status_code == 409, r.text
    assert "悬空" in r.json()["detail"]


def test_项目资产可以升为全局(as_role, fresh_project):
    pid = fresh_project("admin")
    a = _mk_asset(as_role("admin"), pid)
    r = as_role("admin").put(f"/api/assets/{a['id']}",
                             json={"kind": a["kind"], "name": a["name"], "scope": "global"})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] is None


# ---------------------------------------------------------------- 封面
def test_设封面是把那张图挪到第一位(as_role, fresh_project):
    from config import FILES_DIR
    pid = fresh_project("editor")
    a = _mk_asset(as_role("editor"), pid)
    c = as_role("editor")
    paths = []
    for _ in range(2):
        r = c.post(f"/api/assets/{a['id']}/images", files={"file": ("x.png", _png(), "image/png")})
        assert r.status_code == 200, r.text
        paths.append(r.json()["ref_images"][-1])
    assert c.get(f"/api/assets/{a['id']}").json()["ref_images"] == paths

    r = c.put(f"/api/assets/{a['id']}/cover", json={"path": paths[1]})
    assert r.status_code == 200, r.text
    assert r.json()["ref_images"] == [paths[1], paths[0]], "封面＝数组第一位"
    # 再设一次同一张不该变
    again = c.put(f"/api/assets/{a['id']}/cover", json={"path": paths[1]})
    assert again.json()["ref_images"] == [paths[1], paths[0]]

    bad = c.put(f"/api/assets/{a['id']}/cover", json={"path": "9999/不存在.png"})
    assert bad.status_code == 404
    assert (FILES_DIR / paths[0]).is_file() and (FILES_DIR / paths[1]).is_file(), "文件不该被动过"


# ---------------------------------------------------------------- 引用保护与反查
def test_被镜头引用就不给删(as_role, fresh_project):
    pid = fresh_project("editor")
    a = _mk_asset(as_role("editor"), pid)
    c = as_role("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = c.post(f"/api/episodes/{eid}/shots",
                 json={"summary": "开场", "asset_ids": [a["id"]]}).json()["id"]

    shots = c.get(f"/api/assets/{a['id']}/shots").json()
    assert [s["shot_id"] for s in shots] == [sid]
    assert shots[0]["project_name"] and shots[0]["episode_title"] == "第 1 集"
    # 列表里那个"关联 N 镜"必须是同一个数，不能两处各算一遍
    listed = c.get(f"/api/projects/{pid}/assets").json()
    assert next(x for x in listed if x["id"] == a["id"])["ref_shot_count"] == 1

    r = c.request("DELETE", f"/api/assets/{a['id']}")
    assert r.status_code == 409, "被引用就不给删"
    assert "1 条镜头" in r.json()["detail"]

    assert c.request("DELETE", f"/api/shots/{sid}").status_code == 204
    assert c.request("DELETE", f"/api/assets/{a['id']}").status_code == 204


def test_引用清单只含看得见的项目(as_role, fresh_project):
    """跨项目反查不能把别人的镜头列出来——不可见就是不存在（01-2.2-R1）。"""
    mine = fresh_project("editor")
    theirs = fresh_project("admin")
    a = _mk_asset(as_role("editor"), mine)
    c = as_role("editor")
    eid = c.post(f"/api/projects/{mine}/episodes", json={"title": "我的集"}).json()["id"]
    c.post(f"/api/episodes/{eid}/shots", json={"summary": "镜", "asset_ids": [a["id"]]})

    admin = as_role("admin")
    teid = admin.post(f"/api/projects/{theirs}/episodes", json={"title": "别人的集"}).json()["id"]
    # 这条要建成全局资产：项目资产 editor 连读都读不到（404），
    # 那就测不到"读得到资产、但要过滤掉看不见的引用"这一层
    aid = _mk_asset(admin, theirs, scope="global")["id"]
    admin.post(f"/api/episodes/{teid}/shots", json={"summary": "镜", "asset_ids": [aid]})

    # editor 看不见 theirs 项目 ⇒ 那条引用不该出现在任何清单里
    assert [s["project_id"] for s in c.get(f"/api/assets/{aid}/shots").json()] == []
    # admin 两边都看得见（管理员对全部项目放行）
    assert len(admin.get(f"/api/assets/{a['id']}/shots").json()) == 1


def test_删资产只清它自己登记过的图(as_role, fresh_project, delete_guard):
    from config import FILES_DIR
    pid = fresh_project("editor")
    a = _mk_asset(as_role("editor"), pid)
    c = as_role("editor")
    r = c.post(f"/api/assets/{a['id']}/images", files={"file": ("x.png", _png(), "image/png")})
    rel = r.json()["ref_images"][0]
    target = FILES_DIR / rel
    assert target.is_file()
    # 同目录下塞一个不属于该资产的文件：删资产不能把它带走
    sibling = target.parent / "不属于这里.txt"
    sibling.write_text("keep me")
    assert c.request("DELETE", f"/api/assets/{a['id']}").status_code == 204
    assert not target.exists(), "登记过的图应删掉"
    assert sibling.exists(), "没登记的图不是本平台资产，不该动"
    assert (FILES_DIR / str(a["id"])).is_dir(), "目录里还有别的东西就不该收掉目录"
    sibling.unlink()
    (FILES_DIR / str(a["id"])).rmdir()


def test_列表的引用数不区分数字与字符串写法(as_role, fresh_project):
    """`_SELECT` 从相关子查询换成派生表 LEFT JOIN 时的**语义钉**。

    2026-11-04 换写法时被比对脚本打回来过一次：直接 `GROUP BY j.value` 会把
    镜头里的 `asset_ids='["12"]'`（字符串）与 `'[12]'`（数字）分成两组，
    LEFT JOIN 于是让一行资产匹配两遍——列表行数从 600 变 970、"关联 N 镜"还少算。
    原来那句 `j.value = a.id` 靠 a.id 的 INTEGER 亲和把两种写法并成一个数。
    这条用例就是把"两种写法算同一个资产、且不重复出行"钉死，
    顺带钉住标签顺序（原来靠 (asset_id, tag) 主键扫出来，聚合后必须显式 ORDER BY）。
    """
    pid = fresh_project("editor")
    c = as_role("editor")
    a = _mk_asset(c, pid, tags=["灯", "A类", "夜戏"])
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    c.post(f"/api/episodes/{eid}/shots", json={"summary": "数字写法", "asset_ids": [a["id"]]})

    with get_conn() as conn:                    # 字符串写法只能直接写库：接口会转成 int
        conn.execute("""INSERT INTO shot (episode_id, seq, summary, prompt, duration,
                                          asset_ids, variant_ids)
                        VALUES (?,?,'字符串写法','',3.0,?,'[]')""",
                     (eid, 2, f'["{a["id"]}"]'))
        conn.execute("""INSERT INTO shot (episode_id, seq, summary, prompt, duration,
                                          asset_ids, variant_ids)
                        VALUES (?,?,'坏 JSON','',3.0,?,'[]')""", (eid, 3, "not json"))

    rows = c.get(f"/api/projects/{pid}/assets").json()
    # 按 id 取，不假设列表里只有这一条：同文件里别的用例会往全局池里建资产，
    # 断言"整个列表只有一行"会变成"看谁先跑"的假失败
    mine = [r for r in rows if r["id"] == a["id"]]
    assert len(mine) == 1, f"LEFT JOIN 把这条资产配成了多行：{len(mine)} 行"
    hit = mine[0]
    assert hit["ref_shot_count"] == 2, f"两种写法要算成同一个数，实得 {hit['ref_shot_count']}"
    assert hit["tags"] == ["A类", "夜戏", "灯"], f"标签顺序变了：{hit['tags']}"
