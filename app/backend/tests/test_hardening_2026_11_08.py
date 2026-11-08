"""2026-11-08 那轮后端修复的回归：每一条都对应一个**真实会造成损失**的形状。

写法沿用 `test_hardening.py`：每条都测"越界/坏路径那一面"，而不是只测正常路径能通。
文件按修复项分组，组名就是《02-后端代码检查与优化方案》里的编号（P0-1b、P0-1e …），
将来要查"这条当初为什么改"能直接对回去。

为什么这些要单独成文件而不是塞进 test_guards.py：
它们分属安全（SSRF）、状态机、产物完整性（RIFE）、并发（重试幂等）、
可用性（worker 冷启动）五个不同关注点，混进一个"守卫"文件里，
下次有人改状态机时不会被提醒去跑 SSRF 断言。
"""
import json
import pathlib
import threading

import pytest

import job_state
import safenet
from db import get_conn
from repositories.jobs import jobs as job_repo
from repositories.users import login_throttle


# ---------- P0-1b：出站地址的信任边界（SSRF） ----------

def test_本机与局域网仍然放行():
    """这条平台的主要用法：模型跑在本机或同一网段。

    加固如果把 loopback/私网一起封了，产品就直接不可用——所以这两档必须留。
    """
    for url in ("http://127.0.0.1:7860", "http://192.168.1.9:11434",
                "http://10.0.0.5:8080", "https://api.deepseek.com/v1"):
        assert safenet.check_url(url) == url


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # AWS/GCP/Azure 元数据
    "http://169.254.169.254:80",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://[fe80::1]/x",                          # IPv6 链路本地
    "http://0.0.0.0:8080/x",                       # 未指定地址
    "file:///etc/passwd",                          # 非 http(s) scheme
    "ftp://127.0.0.1/x",
])
def test_云元数据与非_http_地址一律不连(url):
    with pytest.raises(safenet.UnsafeTarget):
        safenet.check_url(url)


def test_引擎端点校验挡住元数据地址():
    """`engine_instance.endpoint` 会进 `engines.probe()` 并被原样回显给界面，
    所以它必须在**存进库之前**就挡住内网探测那一类地址。"""
    from repositories.engine_instances import EngineInstanceError, validate_endpoint
    with pytest.raises(EngineInstanceError):
        validate_endpoint("http://169.254.169.254:80")
    with pytest.raises(EngineInstanceError):
        validate_endpoint("http://metadata.google.internal:8080")
    # 正常用法不受影响
    assert validate_endpoint("http://127.0.0.1:7860") == "http://127.0.0.1:7860"
    assert validate_endpoint("http://192.168.1.9:11434") == "http://192.168.1.9:11434"


def test_云视频下载有字节上限(tmp_path, monkeypatch):
    """`video_url` 来自供应商应答，是外部输入：不许无限流把盘写满。"""
    import cloud_run
    monkeypatch.setenv("FASTVIDEO_CLOUD_MAX_DOWNLOAD_GB", "1")
    assert cloud_run._max_download_bytes() == 1 * 1024 ** 3


# ---------- P0-1d：backend 枚举与 DDL 对齐 ----------

def test_验证登记接受_remote_与_0045_的_CHECK_一致():
    """0045 把 CHECK 放宽到 local/cloud/remote，表现层不许把它堵回去。"""
    from models import ValidationIn
    assert ValidationIn(capability="image", backend="remote",
                        kind="real_call").backend == "remote"


def test_排序轴含_remote_不再当脏值沉底():
    from repositories.ordering import backend_order
    sql = backend_order()
    assert "'remote' THEN 3" in sql


# ---------- P0-1e：状态机真的接在生产路径上 ----------

def test_收尾时的非法转移会被挡住(app, seeded):
    """`assert_transition` 过去只被测试调用。现在终态写入前必须过它。"""
    # seeded 里那条已经是 succeeded（见 conftest）。
    # `set_running` 只带 `AND status='starting'`，写不中就是静默 0 行，不抛——
    # 真正接上状态机的是**终态写入**那四个（succeed/fail/cancelled/needs_attention）。
    # 从 succeeded 再写一次 succeeded 是幂等的（允许），写别的才非法。
    with get_conn() as conn:
        conn.execute("UPDATE job SET status='cancelled', worker_id='w-guard' WHERE id=?",
                     (seeded["job_id"],))
    with pytest.raises(ValueError):
        job_repo.succeed(seeded["job_id"], "w-guard", "", [], 1)


def test_取消排队中的作业在竞争失败时不覆盖状态(app, seeded):
    """取消的 UPDATE 带 `AND status=...`：别人已经把它领走了，就不该被覆盖回 cancelled。"""
    with get_conn() as conn:
        conn.execute("UPDATE job SET status='queued', worker_id=NULL WHERE id=?",
                     (seeded["job_id"],))
    claimed = job_repo.claim_next("w-race", 60)      # queued -> starting
    assert claimed is not None and claimed["id"] == seeded["job_id"]
    row = job_repo.request_cancel(seeded["job_id"])   # 此时已不是 queued
    assert row["status"] == "cancel_requested", \
        "领取之后取消应当落在 cancel_requested，而不是把状态硬改回 cancelled"


# ---------- P0-1f：RIFE 失败不能把原始成片一起删掉 ----------

def test_补帧失败时原片必须还在(tmp_path, monkeypatch):
    """改名成 pre 之后任何异常都不许把 pre 删掉——那是成片的唯一副本。"""
    from composition import interpolate

    outputs = tmp_path / "outputs"
    outputs.mkdir()
    video = outputs / "7.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"A" * 128)
    original = video.read_bytes()

    class _Probe:
        state, detail = "ok", "ok"
        values = {"python": "/usr/bin/python3", "script": "/tmp/x.py", "weights": ""}

    monkeypatch.setattr(interpolate.runtime, "probe_rife", lambda: _Probe())
    monkeypatch.setattr(interpolate.validators, "probe_fps", lambda p: (24.0, 100))

    def _boom(*_a, **_k):
        raise interpolate.InterpolateUnavailable("模拟补帧推理失败")

    monkeypatch.setattr(interpolate, "_run", _boom)

    with pytest.raises(interpolate.InterpolateUnavailable):
        interpolate.interpolate(video, outputs, 7, 24.0, None)

    assert video.is_file(), "补帧失败后原始成片必须被放回原处"
    assert video.read_bytes() == original, "放回的必须是那一份原片，不能是空文件"


# ---------- P1：重试幂等（双击不该排两件活） ----------

def test_并发重试只建一条新作业(app, seeded):
    original = job_repo.get(seeded["job_id"])
    with get_conn() as conn:
        conn.execute("UPDATE job SET status='failed', superseded_by_job_id=NULL WHERE id=?",
                     (original["id"],))
    original = job_repo.get(original["id"])

    ids, errors = [], []
    def _click():
        try:
            ids.append(job_repo.retry(original, None)["id"])
        except Exception as exc:                      # noqa: BLE001 - 记下来断言
            errors.append(exc)

    threads = [threading.Thread(target=_click) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发重试不该抛异常：{errors}"
    assert len(set(ids)) == 1, f"四个并发点击应当拿到同一条重试：{ids}"
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM job WHERE retry_of_job_id = ?",
                         (original["id"],)).fetchone()["n"]
    assert n == 1, f"库里只该有一条重试作业，实际 {n} 条"


# ---------- P1：登录限速 ----------

def test_连续失败会被限速(app, client):
    login_throttle.clear()
    try:
        codes = [client.post("/api/auth/login",
                             json={"username": "superadmin", "password": "wrong"})
                 .status_code for _ in range(6)]
        assert codes[:5] == [401] * 5
        assert codes[5] == 429, f"第 6 次应当被限速，实际 {codes}"
        # 锁住期间即使口令正确也不放行（否则限速等于没做）
        blocked = client.post("/api/auth/login",
                              json={"username": "superadmin",
                                    "password": "whatever-it-is"})
        assert blocked.status_code == 429
    finally:
        login_throttle.clear()


def test_登录成功后失败计数清零(app, client):
    login_throttle.clear()
    try:
        for _ in range(2):
            client.post("/api/auth/login",
                        json={"username": "superadmin", "password": "wrong"})
        login_throttle.reset("superadmin", "testclient")
        assert login_throttle.retry_after("superadmin", "testclient") == 0
    finally:
        login_throttle.clear()


# ---------- P1：停用账号的旧会话立刻失效 ----------

def test_停用账号后旧会话当场失效(app, seeded, people):
    """只改 is_active 而不删会话（迁移脚本/手工 SQL 那条路）也必须立刻踢人。"""
    from datetime import datetime, timedelta

    from fastapi.testclient import TestClient
    from security import new_session_token
    from repositories.users import sessions as session_repo
    import main

    user_id = people["editor"]["id"]
    token, digest = new_session_token()
    session_repo.create(digest, user_id,
                        (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))
    c = TestClient(main.app, cookies={"fv_session": token})
    assert c.get("/api/auth/me").status_code == 200

    with get_conn() as conn:      # 故意绕过 set_active()，模拟"只改了一格"
        conn.execute("UPDATE app_user SET is_active = 0 WHERE id = ?", (user_id,))
    try:
        assert c.get("/api/auth/me").status_code == 401, \
            "停用必须立刻失效，不能等会话自己过期"
    finally:
        # 只清**本用例铸的那一个**会话：`as_role` 这套 fixture 是按角色共享的，
        # 按 user_id 全删会把后面用例手里的登录态一起废掉
        # （症状是 test_workbench 那一片 KeyError: 'projects'——401 的响应体没有这个键）。
        with get_conn() as conn:
            conn.execute("UPDATE app_user SET is_active = 1 WHERE id = ?", (user_id,))
            conn.execute("DELETE FROM session WHERE token_hash = ?", (digest,))


# ---------- P1：接口文档默认不公开 ----------

def test_接口文档默认不对外(app):
    from fastapi.testclient import TestClient
    import main
    anon = TestClient(main.app)
    for path in ("/api/docs", "/api/openapi.json"):
        assert anon.get(path).status_code == 404, \
            f"{path} 不该在没有 FASTVIDEO_DOCS=1 时对外"


# ---------- P1：磁盘放不下就不再是"提醒"，是阻塞 ----------

def test_磁盘算下来放不下就阻塞发起():
    from precheck import BLOCK, LOCATE, _disk_item
    disk = {"sentence": "预计还要 12GB，只剩 9GB",
            "shortfall_bytes": 3 * 1024 ** 3,
            "free_bytes": 9 * 1024 ** 3, "floor_bytes": 5 * 1024 ** 3}
    item = _disk_item(disk, LOCATE["generate"])
    assert item["level"] == BLOCK, "shortfall>0 应当是阻塞，而不只是一句提醒"


def test_只是低于提醒线仍是提醒():
    """没有产物样本可比时，低于固定提醒线只算提醒——那是保守的地板，不是结论。

    （注意 `shortfall_bytes` 为 0 而 free < floor 的组合意味着"按样本算够用、
    但盘余低于提醒线"，这一档不该拦人。）
    """
    from precheck import LOCATE, WARN, _disk_item
    disk = {"sentence": "低于提醒线", "shortfall_bytes": 0,
            "free_bytes": 3 * 1024 ** 3, "floor_bytes": 5 * 1024 ** 3}
    assert _disk_item(disk, LOCATE["generate"])["level"] == WARN


# ---------- P1：静默告警按日志文件算，不是按行更新时间 ----------

def test_静默秒数看日志文件而不是_updated_at(app, seeded, tmp_path):
    """`renew()` 每 0.2 秒刷 `updated_at`；拿它判静默，告警永远不触发。"""
    import os
    import time

    from config import LOG_SILENCE_SECONDS
    from jobfiles import ensure_dirs

    logs, _outputs = ensure_dirs(seeded["job_id"])
    log_path = logs / "run.log"
    log_path.write_text("x", encoding="utf-8")
    # 把日志文件的 mtime 拨到"很久以前"，但 updated_at 保持刚刚
    old = time.time() - (LOG_SILENCE_SECONDS + 120)
    os.utime(log_path, (old, old))

    rel = log_path.relative_to(log_path.parents[2]).as_posix()
    with get_conn() as conn:
        conn.execute(
            """UPDATE job SET status='running', worker_id='w-silent', log_path=?,
                      started_at=datetime('now','localtime'),
                      updated_at=datetime('now','localtime') WHERE id=?""",
            (rel, seeded["job_id"]))

    from repositories.jobs import jobs as repo
    health = repo.queue_health(7200)
    row = next(r for r in health["running_jobs"] if r["id"] == seeded["job_id"])
    assert row["silent_seconds"] is not None
    assert row["silent_seconds"] >= LOG_SILENCE_SECONDS, \
        f"日志文件已静默 {row['silent_seconds']}s，必须能报出来"

    with get_conn() as conn:
        conn.execute("UPDATE job SET status='succeeded', worker_id=NULL WHERE id=?",
                     (seeded["job_id"],))


# ---------- 第一批收尾杂项（2026-11-08 第二批改动） ----------

def test_资产版本令牌能接受普通_dict():
    """`asset_version` 原来两条分支写的是同一个表达式（`… else row.keys()`），
    退到 dict 时会 AttributeError。dict 没有 `keys` 方法这个概念要说准：
    dict **有** `keys()`，所以这里用 `Mapping` 之外的普通 dict 验——
    真正会走到 else 的是"没有 keys 方法的行对象"，用一个只支持 `[k]` 与 `in` 的
    最小替身来代表它。"""
    from models import asset_version

    class _RowLike:
        """没有 keys()，但支持下标与 in —— 即 `sqlite3.Row` 之外的行表示。"""
        def __init__(self, data):
            self._d = data
        def __getitem__(self, k):
            return self._d[k]
        def __contains__(self, k):
            return k in self._d

    data = {"updated_at": "2026-11-08 10:00:00", "kind": "character",
            "name": "角色甲", "summary": "", "consistency_prompt": "",
            "aliases": "[]", "ref_images": "[]"}
    token = asset_version(_RowLike(data))
    assert isinstance(token, str) and token, "没有 keys() 的行对象也要算得出令牌"
    # 同一份数据、换一个 dict 形态，结果应当一致（同一口径）
    assert asset_version(dict(data)) == token


def test_超管引导的变量走config解析(monkeypatch):
    """`FASTVIDEO_ADMIN_USER` 给空白时不能建出一个用户名叫空白的账号。"""
    import config

    monkeypatch.setenv("FASTVIDEO_ADMIN_USER", "   ")
    assert config.admin_username() == "superadmin", "空白要回退默认值"
    monkeypatch.setenv("FASTVIDEO_ADMIN_USER", "  boss  ")
    assert config.admin_username() == "boss", "两端空白要去掉"


def test_引导口令仍在调用时读环境变量(monkeypatch):
    """部署允许 `FASTVIDEO_ADMIN_PASSWORD=xxx python main.py`：
    变量是先在进程里设好再拉起的，所以必须**调用时**读，不能在 import 期定死。"""
    from bootstrap import _initial_password
    monkeypatch.setenv("FASTVIDEO_ADMIN_PASSWORD", "  运行期设的  ")
    pw, from_env = _initial_password()
    assert (pw, from_env) == ("运行期设的", True)


def test_git错误回给前端的文案不含部署机绝对路径(as_role, fresh_project):
    """git 的 stderr 会带仓库绝对路径（实测锁冲突那条），5xx 出门前要脱敏。

    **这条必须验到「调用点真的用了脱敏」**，不能只验 `_git_detail` 自己——
    只测辅助函数的话，把路由里那句改回 `detail=f"...{err}"` 测试照样绿
    （本轮初版就是这么写的，实测抓不到回归）。所以这里走真实 HTTP：
    让建文档时的 git 提交失败，看回给客户端的 `detail`。
    """
    import config
    import gitrepo

    ws = str(config.WORKSPACE_DIR)
    pid = fresh_project("editor")
    c = as_role("editor")

    def _boom(*_a, **_k):
        raise gitrepo.GitError(
            f"fatal: Unable to create '{ws}/.git/index.lock': File exists.")

    original = gitrepo.commit_file
    gitrepo.commit_file = _boom
    try:
        r = c.post(f"/api/projects/{pid}/documents",
                   json={"title": "脱敏验证稿", "kind": "novel", "content": "正文"})
    finally:
        gitrepo.commit_file = original

    assert r.status_code == 500, r.text
    detail = str(r.json().get("detail", ""))
    assert ws not in detail, f"部署机路径不该跟着错误出门：{detail}"
    assert "index.lock" in detail, f"脱敏不能把排障信息一起删掉：{detail}"
