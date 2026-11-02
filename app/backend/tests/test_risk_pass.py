"""整体风险与调优这一轮（v0.86）的回归。

四条共性是"今天没出事，所以没人看见"：路径边界在四处各写一遍、写法还不一样；
worker 把自己崩掉的堆栈存进一个会随每条列表接口出门的列；会话行带着口令哈希
到处走；看板与成本账对"什么算钱"各有自己的理解。
所以每条都断言**越界的那一面**——正常路径能通不代表门关着。
"""
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import config
import paths
import worker
from db import get_conn
from repositories.dashboard import dashboard
from repositories.jobs import jobs as job_repo
from repositories.users import sessions as session_repo
from security import new_session_token

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")


def _files_root() -> Path:
    """解析后的上传目录。测试要造的是它的**同名前缀兄弟目录**。"""
    return Path(str(config.FILES_DIR.resolve()))


def _evil_sibling(name: str) -> Path:
    """data/files-evil：字符串前缀与 data/files 完全一样，人却在外面。"""
    root = _files_root()
    outside = root.parent / (root.name + "-evil")
    outside.mkdir(parents=True, exist_ok=True)
    victim = outside / name
    victim.write_bytes(b"not mine")
    return victim


def _make_job(project_id: int, created_by: int, prompt: str = "成本口径") -> int:
    return job_repo.create({
        "project_id": project_id, "episode_id": None, "shot_id": None,
        "capability": "video", "kind": "video_local", "execution_mode": "cloud",
        "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
        "prompt_snapshot": prompt, "params": {}, "input_refs": [],
        "created_by": created_by,
    })["id"]


# ---------- A：路径边界收成一个出处 ----------

def test_resolve_within_不被同名前缀骗(tmp_path):
    root = tmp_path / "files"
    (root / "12").mkdir(parents=True)
    (tmp_path / "files-evil").mkdir()
    (tmp_path / "files-evil" / "secret.png").write_bytes(b"x")

    # 写法里没有上跳关键字不行——要的是解析后的位置
    assert paths.resolve_within(root, "../files-evil/secret.png") is None
    ok = paths.resolve_within(root, "12/a.png")
    assert ok == (root / "12" / "a.png").resolve()
    # root 自己不算"在里面"：发图接口拿它当文件用会读出目录
    assert paths.resolve_within(root, ".") is None
    assert paths.resolve_within(root, "12") == (root / "12").resolve()


def test_resolve_within_拒掉不合规矩的写法(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.png").write_bytes(b"x")
    for bad in ("", "   ", "/etc/passwd", "..", "../a.png", "x/../../y"):
        assert paths.resolve_within(root, bad) is None, bad
    # 库里混进 Windows 写法时不该绕过去：反斜杠先换成分隔符再按段判
    assert paths.resolve_within(root, "..\\a.png") is None
    # 目录里一条指向外面的软链接：跟过去之后不在目录内，同样算越界
    (root / "link.png").symlink_to(Path("/etc/passwd"))
    assert paths.resolve_within(root, "link.png") is None


def test_within_与_resolve_within_同一条边界(tmp_path):
    root = tmp_path / "files"
    (root / "12").mkdir(parents=True)
    (tmp_path / "files-evil").mkdir()
    assert paths.within(root, root / "12" / "a.png") is True
    assert paths.within(root, root) is False
    assert paths.within(root, tmp_path / "files-evil" / "x.png") is False


def test_删资产时脏路径不能变成删除目标(app, as_role, fresh_project):
    """删资产会把 `ref_images` 里的文件逐个 unlink；库里那条是不可信的。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    a = c.post(f"/api/projects/{pid}/assets",
               json={"kind": "character", "name": f"脏路径_{uuid.uuid4().hex[:6]}",
                     "summary": "", "consistency_prompt": "", "aliases": [], "tags": []})
    assert a.status_code == 201, a.text
    aid = a.json()["id"]

    victim = _evil_sibling("keepme_del.png")
    rel = f"../{victim.parent.name}/{victim.name}"
    with get_conn() as conn:
        conn.execute("UPDATE asset SET ref_images = ? WHERE id = ?",
                     (f'["{rel}"]', aid))

    r = c.delete(f"/api/assets/{aid}")
    assert r.status_code == 204, r.text
    assert victim.is_file(), "越界的那条路径被删了——边界等于没写"
    victim.unlink(missing_ok=True)


def test_删单张图时脏路径不能变成删除目标(app, as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    a = c.post(f"/api/projects/{pid}/assets",
               json={"kind": "character", "name": f"删图_{uuid.uuid4().hex[:6]}",
                     "summary": "", "consistency_prompt": "", "aliases": [], "tags": []})
    aid = a.json()["id"]
    up = c.post(f"/api/assets/{aid}/images", files={"file": ("x.png", PNG, "image/png")})
    assert up.status_code == 200, up.text

    victim = _evil_sibling("keepme_img.png")
    dirty = f"../{victim.parent.name}/{victim.name}"
    # 接口上的 `path` 是调用方给的，库里 `ref_images` 里也得有同一条才删得动——
    # 也就是说：脏路径完全能一路走到那句 unlink。旧写法（bare startswith）当场就删了。
    with get_conn() as conn:
        conn.execute("UPDATE asset SET ref_images = ? WHERE id = ?",
                     (f'["{dirty}"]', aid))

    r = c.request("DELETE", f"/api/assets/{aid}/images", params={"path": dirty})
    assert r.status_code == 200, r.text
    assert victim.is_file(), "拼出来的路径已经出了上传目录，还照删"
    victim.unlink(missing_ok=True)


def test_describe_只给配置目录贴标签(tmp_path):
    # 故意用 config 里那个**没解析过**的写法传进来：macOS 上 `/var -> /private/var`
    # 是软链接，只备解析后那一种写法时这条替换会静默不生效，绝对路径照样出家门
    raw = Path(config.FILES_DIR) / "12" / "a.png"
    labeled = paths.describe(raw)
    assert labeled.startswith("data/files/"), labeled
    assert str(config.FILES_DIR) not in labeled
    assert str(Path(config.FILES_DIR).resolve()) not in labeled
    # 目录外的路径不硬编一个假标签骗人：原样留着，让人自己看得懂
    elsewhere = tmp_path / "elsewhere.png"
    assert paths.describe(elsewhere) == str(elsewhere)


# ---------- B：错误消息不跟着列表出门 ----------

def test_worker_内部错误不把堆栈写进那一列():
    """`error_message` 随每一条作业列表接口回传，也会进素材导出与成本 CSV。"""
    msg = worker._internal_error_message(
        FileNotFoundError(2, "No such file", f"{_files_root()}/1/a.png"))
    assert "Traceback" not in msg and 'File "' not in msg, msg
    assert "FileNotFoundError" in msg, msg
    assert str(_files_root()) not in msg, f"绝对路径跟着列表出门了：{msg}"
    # 没有消息的异常也要有一句人话，不能只剩一个冒号
    assert worker._internal_error_message(ValueError()) == "worker 内部错误：ValueError"
    # 多行只取第一行，并且限长
    long = worker._internal_error_message(RuntimeError("第一行\n第二行\n" + "x" * 600))
    assert "第二行" not in long and len(long) < 300, long


def test_落库的那条错误消息没有堆栈(app, as_role, people, fresh_project):
    """上一条是纯函数断言，这一条把它接到真接口上：列表里读到的就是干净的。"""
    pid = fresh_project("editor")
    jid = _make_job(pid, people["editor"]["id"], "堆栈不该进这一列")
    with get_conn() as conn:
        conn.execute("UPDATE job SET status = 'running', worker_id = 't-worker' WHERE id = ?",
                     (jid,))
    message = worker._internal_error_message(KeyError("boom"))
    job_repo.fail(jid, "t-worker", "internal_error", message, "", None, 1)

    body = as_role("editor").get(f"/api/jobs?project_id={pid}").json()
    row = next(x for x in body if x["id"] == jid)
    assert row["error_message"] == message, row
    assert "Traceback" not in row["error_message"], row


# ---------- C：会话行不带口令哈希、过期行有人清 ----------

def test_会话行不带口令哈希():
    token, digest = new_session_token()
    with get_conn() as conn:
        uid = conn.execute("SELECT id FROM app_user LIMIT 1").fetchone()["id"]
    session_repo.create(digest, uid,
                        (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
    row = session_repo.get(digest)
    assert row is not None
    assert "password_hash" not in row.keys(), "口令哈希又跟着「当前用户」这个对象出门了"
    for key in ("id", "username", "display_name", "is_active",
                "is_superuser", "must_change_password", "expires_at"):
        assert key in row.keys(), f"当前用户少了 {key}，某个调用方会 KeyError"
    session_repo.delete(digest)


def test_过期会话在下一次铸会话时被清掉():
    """`purge_expired` 以前没有调用方：废行不会自己消失，表里只攒取不出来的垃圾。"""
    with get_conn() as conn:
        uid = conn.execute("SELECT id FROM app_user LIMIT 1").fetchone()["id"]
        past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        dead = uuid.uuid4().hex
        conn.execute("INSERT INTO session (token_hash, user_id, expires_at) VALUES (?,?,?)",
                     (dead, uid, past))
    # 上面那个 with 得先进事务收口：`create()` 会另开一条连接，
    # WAL 下两条写连接同时动手就是 database is locked
    assert session_repo.get(dead) is None, "过期行本来就读不到，这里只是钉住前提"

    before = session_repo.count()
    _token, digest = new_session_token()
    session_repo.create(digest, uid,
                        (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
    with get_conn() as conn:
        still = conn.execute("SELECT 1 FROM session WHERE token_hash = ?",
                             (dead,)).fetchone()
    assert still is None, "过期行还躺在表里：清理没挂上登录这条路"
    # 死行少一条、新行多一条，总数不变＝没顺手把别人的活会话一起删掉
    assert session_repo.count() == before, "清理越界了，把还没到期的会话也删了"
    assert session_repo.get(digest) is not None
    session_repo.delete(digest)


# ---------- D：成本口径只有一个出处 ----------

def test_看板与成本账对什么算钱给同一个数(app, people, fresh_project):
    """`currency IS NULL` 的作业以前会被看板聚成一行"开销"，而成本账里根本没有它。"""
    pid = fresh_project("editor")
    ids = [_make_job(pid, people["editor"]["id"]), _make_job(pid, people["editor"]["id"])]
    ghost = _make_job(pid, people["editor"]["id"])
    with get_conn() as conn:
        for jid, micros in zip(ids, (1_000_000, 250_000)):
            conn.execute("""UPDATE job SET status = 'succeeded', cost_micros = ?,
                                   currency = 'CNY', billable_status = 'known'
                            WHERE id = ?""", (micros, jid))
        # 有金额没币种：一条说不清是什么钱的记录。两处都不该把它算成钱
        conn.execute("""UPDATE job SET status = 'succeeded', cost_micros = 900000,
                               currency = NULL, billable_status = 'known' WHERE id = ?""",
                     (ghost,))

    costs = dashboard.summary(pid)["costs"]
    assert [c["currency"] for c in costs] == ["CNY"], f"NULL 币种被当成一种钱了：{costs}"
    assert costs[0]["micros"] == 1_250_000, costs
    assert costs[0]["jobs"] == 2, costs

    rows = job_repo.cost_summary([pid])["rows"]
    assert sum(r["total"] for r in rows) == 1_250_000, rows
    assert {r["currency"] for r in rows} == {"CNY"}, rows

    with get_conn() as conn:
        row = conn.execute("SELECT cost_micros, currency FROM job WHERE id = ?",
                           (ghost,)).fetchone()
    assert row["cost_micros"] == 900000 and row["currency"] is None, dict(row)


def test_两处的金额口径引用同一个常量():
    """数字对得上是结果，共用一条 WHERE 才是原因——各写一遍迟早会漂。"""
    from repositories.jobs import BILLABLE_WHERE

    assert "currency IS NOT NULL" in BILLABLE_WHERE, BILLABLE_WHERE
    sql = Path(__file__).resolve().parents[1] / "repositories" / "dashboard.py"
    assert "AND {BILLABLE_WHERE}" in sql.read_text(encoding="utf-8"), \
        "看板又自己写了一遍金额口径"
