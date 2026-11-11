"""队列翻页：游标不重复、不漏，分母与筛选同口径。

这三条是分页唯一真正会错的地方，也是「加了分页反而更糟」的三种典型：
  * 游标写错 → 翻第二页时第一页末尾重复出现；
  * 边界写错（<= 取代 <）→ 每页都漏一条，而且看不出来；
  * 分母口径与列表不一致 → 界面显示「共 412」但只能翻出 3 条，比不给分母更糟。

另外两条守的是**不该变**的东西：不给 before_id 时行为与从前逐字一致
（12 个调用方里只有队列需要翻页），以及 limit 仍然被夹在上界内。
"""
import pytest


@pytest.fixture(autouse=True)
def _收摊(app):
    """本模块提交的作业一律收尾。

    `seeded` fixture 的注释里写着这条约定（conftest.py:128-135）：排队作业会被
    后面的用例当成"活"领走，而 model_center 的 `_succeeded_cloud_job` 断言
    "claim_next 领到的必须是我刚建的那条"。我这几条用例提交了十几条 mock 作业
    却不收尾，于是它们抢在别人前面被领走——单独跑绿、全套红，红的地方离
    出错的地方隔了三个文件。
    """
    yield
    from db import get_conn
    with get_conn() as conn:
        conn.execute(
            """UPDATE job SET status = 'cancelled',
                      finished_at = datetime('now','localtime'),
                      error_code = 'paging_test_cleanup'
                WHERE status IN ('queued','starting','running','cancel_requested')""")

def _submit(c, project_id, prompt="翻页测试", **kw):
    body = {
        "project_id": project_id, "capability": "video", "prompt": prompt,
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.02},
    }
    body.update(kw)
    r = c.post("/api/jobs", json=body)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def test_游标翻页不重复也不漏(as_role, seeded, clean_queue):
    """逐页翻到底，id 集合必须与「一次性取足够多」完全一致。

    重复＝人在第二页看见第一页末尾那几条；漏＝有作业永远翻不到。两者都是
    「分页看着能用、其实坏了」，所以按集合相等断言，而不是只看页数。
    """
    c = as_role("admin")
    pid = seeded["project_id"]
    made = [_submit(c, pid, prompt=f"翻页-{i}") for i in range(7)]

    # 一次性取全，作为对照组（limit 上界 200，够覆盖本用例）
    whole = c.get(f"/api/jobs?project_id={pid}&limit=200")
    assert whole.status_code == 200, whole.text
    all_ids = [row["id"] for row in whole.json()]
    assert set(made) <= set(all_ids)

    # 每页 2 条，一直翻到空页
    seen, cursor, pages = [], None, 0
    while True:
        qs = f"/api/jobs?project_id={pid}&limit=2"
        if cursor is not None:
            qs += f"&before_id={cursor}"
        r = c.get(qs)
        assert r.status_code == 200, r.text
        rows = r.json()
        if not rows:
            break
        seen += [row["id"] for row in rows]
        cursor = rows[-1]["id"]
        pages += 1
        assert pages < 200, "没有收敛，游标可能没生效"

    assert len(seen) == len(set(seen)), f"翻页出现重复：{seen}"
    assert set(seen) == set(all_ids)
    assert set(made) <= set(seen)


def test_分母与当前筛选同口径(as_role, seeded, clean_queue):
    """X-Total-Count 必须是**筛选之后**的总数，不是项目总数。

    早先界面拿 cost_summary 的 total_jobs 当分母，那个数只按项目过滤，
    不认 status／capability／q。一旦加了筛选就会出现「筛选出 3 条／共 412」。
    """
    c = as_role("admin")
    pid = seeded["project_id"]
    for i in range(5):
        _submit(c, pid, prompt=f"口径-{i}", capability="video")

    r = c.get(f"/api/jobs?project_id={pid}&limit=1")
    assert r.status_code == 200, r.text
    total = int(r.headers["X-Total-Count"])
    assert total == len(c.get(f"/api/jobs?project_id={pid}&limit=200").json())
    assert total > 1, "本用例要有超过一页的数据，否则分母断言是空的"

    # 换一个筛选，分母必须跟着变。
    # 这里**不能用「text 能力一条都没有」当预期**：seeded 是会话级项目，
    # 别的用例会往同一个项目里提交 text 作业，于是「恰好为 0」只在单跑时成立
    # （单独跑绿、全套红就是踩的这个）。自己提交一条，问它有几条——
    # 得到 1 比得到 0 更有说服力：分母真的在数这一种能力，而不是回项目总数。
    text_id = _submit(c, pid, prompt="口径-text", capability="text")
    r2 = c.get(f"/api/jobs?project_id={pid}&limit=1&capability=text")
    assert r2.status_code == 200, r2.text
    total_text = int(r2.headers["X-Total-Count"])
    listed = [row["id"] for row in
              c.get(f"/api/jobs?project_id={pid}&limit=200&capability=text").json()]
    assert total_text == len(listed), f"分母 {total_text} 与列表 {len(listed)} 不同口径"
    assert text_id in listed, "刚提交的 text 作业没进列表"
    assert total_text < total, "分母没跟着能力筛选变小"

    # 关键字筛选也必须是同口径
    r3 = c.get(f"/api/jobs?project_id={pid}&limit=1&q=口径-3")
    assert r3.status_code == 200, r3.text
    assert int(r3.headers["X-Total-Count"]) == 1


def test_不传游标时行为与从前一致(as_role, seeded):
    """12 个调用方里只有队列要翻页，其余必须一字不变：不带 before_id
    就等于"从最新一条开始取 limit 条"。"""
    c = as_role("admin")
    r = c.get("/api/jobs")
    assert r.status_code == 200, r.text
    assert r.json(), "空列表说明默认取数坏了"
    assert "X-Total-Count" in r.headers


def test_游标取到不存在的大_id时回第一页(as_role, seeded):
    """游标是"比它更早"，给一个比所有 id 都大的值时应当回最新一页，
    而不是空列表——否则前端从深链恢复一个失效游标时会误显示"没有作业"。"""
    c = as_role("admin")
    r = c.get("/api/jobs?limit=3&before_id=99999999")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 3
    assert rows[0]["id"] > rows[-1]["id"], "顺序必须是 id 倒序"


def test_非数字游标被拒而不是静默忽略(as_role):
    """`before_id=abc` 必须 422。静默忽略会让前端以为翻页生效了，
    实际一直在取第一页——那是"翻页按了没反应"的经典来源。"""
    c = as_role("admin")
    assert c.get("/api/jobs?before_id=abc").status_code == 422
