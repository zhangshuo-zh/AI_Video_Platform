"""2026-11-08 那轮后端修复的回归：每一条都对应一个**真实会造成损失**的形状。

写法沿用 `test_hardening.py`：每条都测"越界/坏路径那一面"，而不是只测正常路径能通。
文件按修复项分组，组名就是《02-后端代码检查与优化方案》里的编号（P0-1b、P0-1e …），
将来要查"这条当初为什么改"能直接对回去。

为什么这些要单独成文件而不是塞进 test_guards.py：
它们分属安全（SSRF）、状态机、产物完整性（RIFE）、并发（重试幂等）、
可用性（worker 冷启动）五个不同关注点，混进一个"守卫"文件里，
下次有人改状态机时不会被提醒去跑 SSRF 断言。
"""
import threading

import pytest

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


# ---------- 第二批：工具链与静态检查（2026-11-08） ----------

def test_依赖声明齐全且都有上界():
    """`pydantic` 曾被漏声明（靠 fastapi 传递进来），而 models.py 直接 import 它。

    同时要求**每条都有上界**：只写下界时，换个时间装机可能拿到下一个大版本，
    121 个 BaseModel 对 v1/v2 差异非常敏感。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    text = (root / "requirements.txt").read_text(encoding="utf-8")
    specs = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]+\])?(.*)$", line)
        if m:
            specs[m.group(1).lower()] = m.group(3)

    for name in ("fastapi", "uvicorn", "python-multipart", "pydantic"):
        assert name in specs, f"{name} 必须在 requirements.txt 里显式声明"
        assert "<" in specs[name], f"{name} 必须有上界，现在是 {specs[name]!r}"


def test_约束文件的版本与已装版本一致():
    """`constraints.txt` 是"验证过的那一代"的快照，不能和实际情况脱节。"""
    import importlib.metadata as md
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    pinned = {}
    for line in (root / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, ver = line.split("==", 1)
        pinned[name.strip()] = ver.strip()

    assert pinned, "constraints.txt 里应当有钉住的版本"
    for name in ("fastapi", "pydantic", "uvicorn", "python-multipart"):
        assert name in pinned, f"{name} 应当钉在 constraints.txt 里"
        actual = md.version(name)
        assert pinned[name] == actual, (
            f"{name} 约束写的是 {pinned[name]}，实际装的是 {actual}——"
            "改了依赖就要更新 constraints.txt")


def test_静态检查配置存在且规则集说明了理由():
    """工具配置必须留在仓库里，否则每个人本地的 ruff 行为都不一样。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.ruff]" in text
    assert "select" in text, "要写明开了哪些规则集"
    # F811 必须被忽略且写明原因（pytest 夹具会被 ruff 误判成重复定义）
    assert "F811" in text, "pytest 夹具的 F811 误报必须显式忽略"


def test_错误目录里没有重复的错误码():
    """同一个键定义两次时，后一份会静默覆盖前一份——文案永远不生效。

    `upstream_job_missing` 就重复过：104 行那份（写了 technical 依据）
    被 326 行那份覆盖，人看到的标签和代码里的实际语义对不上。
    """
    # CATALOG 是普通 dict，重复键在构造时就已塌陷；这里守的是"以后别再写出重复"。
    # 做法：重新解析源码里的字面量键，数一遍。
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "error_catalog.py").read_text(encoding="utf-8"))
    catalog = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and len(node.keys) > 50:
            catalog = node
            break
    assert catalog is not None, "没找到错误目录的字典字面量"

    keys = [k.value for k in catalog.keys if isinstance(k, ast.Constant)]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"错误目录里这些码定义了不止一次（后者会静默覆盖前者）：{sorted(dupes)}"


def test_测试里不再引用未定义的变量名():
    """`test_infra.py` 有條断言写成 `f"{name} 没走唯一出口"`，而 name 未定义。

    它平时不会暴露——只有断言真的失败时才抛 NameError，把"哪个文件没走唯一出口"
    这句话吃掉。ruff 的 F821 一次就找出来了。
    """
    import pathlib
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--select", "F821",
             "--no-cache", "--output-format=concise", "tests/"],
            cwd=root, capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.SubprocessError):
        pytest.skip("这台机器没装 ruff（开发依赖，见 requirements-dev.txt）")
    if proc.returncode not in (0, 1):
        pytest.skip(f"ruff 跑不起来：{proc.stderr[:200]}")
    assert "F821" not in proc.stdout, f"测试里有未定义的名字：{proc.stdout[:500]}"


# ---------- 第三批：统一错误出口（2026-11-08） ----------

def test_校验错误的_detail_必须是字符串而不是数组(as_role, fresh_project):
    """前端 `new Error(detail)` 遇到数组会显示成 `[object Object]`。

    实况：`api.js` 写的是 `new Error(JSON.parse(text).detail ?? text)`，
    而 FastAPI 默认校验错误给的是 `[{"type":...,"loc":...}]`。数组进 Error
    之后按 `Array.prototype.toString()` 走，用户看到的就是那五个字——
    缺一个必填项时弹窗里没有一句可用信息。所以 handler 必须把它压成一句话。
    """
    c = as_role("editor")
    r = c.post("/api/projects", json={})              # 缺 name 等必填
    assert r.status_code == 422, r.text
    body = r.json()
    assert isinstance(body["detail"], str), \
        f"detail 必须是字符串（前端直接 new Error(detail)），实际是 {type(body['detail'])}"
    assert "[object Object]" not in body["detail"]
    assert body["detail"].strip(), "detail 不能是空字符串"
    assert body.get("code") == "invalid_payload"


def test_业务拒绝的_detail_仍是原来那句话(as_role):
    """**只加字段、不改内容**：说明书与浏览器验收引用过这些句子。"""
    r = as_role("editor").get("/api/jobs/999999")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "作业不存在", f"文案被改了：{body['detail']}"
    assert body["code"] == "not_found"


def test_每个错误响应都带_code(app, client):
    """`code` 是稳定的机器可读标识；`detail` 是可以改文案的。"""
    cases = [
        ("get", "/api/jobs/999999", None),                 # 未登录时先撞 401
        ("post", "/api/projects", {}),
    ]
    for method, url, payload in cases:
        r = getattr(client, method)(url, json=payload) if payload is not None \
            else getattr(client, method)(url)
        assert r.status_code >= 400
        body = r.json()
        assert "detail" in body and isinstance(body["detail"], str)
        assert body.get("code"), f"{url} 的错误响应没有 code：{body}"


def test_错误码表与状态码一致():
    """`errors.code_for_status()` 是 code 的唯一出处，别在别处再写一份。"""
    import errors

    assert errors.code_for_status(401) == "unauthenticated"
    assert errors.code_for_status(403) == "forbidden"
    assert errors.code_for_status(404) == "not_found"
    assert errors.code_for_status(429) == "rate_limited"
    # 表里没有的状态码要有可读兜底，不能 KeyError
    assert errors.code_for_status(418) == "http_418"


def test_校验细节能压成一句人话():
    """`loc` 开头的 body/query/path 对人是噪音，要剥掉。"""
    import errors

    one = [{"type": "missing", "loc": ["body", "name"], "msg": "Field required"}]
    assert errors.detail_text(one) == "name：缺少必填项"

    two = [{"type": "missing", "loc": ["body", "payload", "title"], "msg": "Field required"},
           {"type": "int_type", "loc": ["body", "episode_id"], "msg": "Input should be int"}]
    text = errors.detail_text(two)
    assert "payload.title" in text and "episode_id" in text, text
    assert "；" in text, "多条要分隔开，不能粘成一坨"

    # 不认识的 type 退回 msg，至少不是 [object Object]
    odd = [{"type": "something_new", "loc": ["body", "x"], "msg": "weird"}]
    assert "weird" in errors.detail_text(odd)


def test_诊断接口的七字段一个都不能少(as_role, seeded):
    """加 `response_model` 会**静默剔除**未声明的字段。

    所以给聚合类端点加模型前必须先确认形状真的固定——
    诊断这个能加，是因为 `CATALOG` 55 条 + `UNKNOWN_TEMPLATE` 的键完全一致。
    这条测试守住"以后往目录里加条目时别偷偷改形状"。
    """
    r = as_role("editor").get(f"/api/jobs/{seeded['job_id']}/diagnosis")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("job_id", "status", "error_code", "diagnosis", "cloud", "uncertain"):
        assert key in body, f"顶层缺 {key}"
    for key in ("label", "explain", "technical", "impact", "action",
                "retryable", "may_cost", "needs_human", "code", "known"):
        assert key in body["diagnosis"], f"diagnosis 缺 {key}——被 response_model 剔掉了？"


def test_错误目录的条目键必须统一():
    """`JobDiagnosisBody` 的字段来自这张表：条目键一旦分叉，`response_model`
    就会静默丢掉多出来的那个键。"""
    import error_catalog

    shapes = {tuple(sorted(k for k in e)) for e in error_catalog.CATALOG.values()}
    shapes.add(tuple(sorted(k for k in error_catalog.UNKNOWN_TEMPLATE)))
    assert len(shapes) == 1, f"条目键有 {len(shapes)} 种组合，response_model 会丢字段：{shapes}"


# ---------- 第四批：聚合视图收成模型（2026-11-08） ----------

def _keys_of(obj, prefix=""):
    """递归收集一个响应体的所有键路径。列表只看第一个元素（同构）。"""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(prefix + k)
            out |= _keys_of(v, prefix + k + ".")
    elif isinstance(obj, list) and obj:
        out |= _keys_of(obj[0], prefix + "[].")
    return out


def test_工作台模型覆盖响应里的每一个键(as_role, fresh_project):
    """给聚合视图加 `response_model` 会**静默剔除**未声明的字段。

    `overview.js` 依赖 counts.review_documents / runs_active 这些键；
    模型漏一个，接口照样 200、页面却少一格。所以这条测试逐键比对：
    模型序列化出来的键必须与服务层原始返回**完全一致**。
    """
    import services
    from models import WorkbenchOut
    from repositories.users import users as user_repo

    # 建一个项目让 projects 列表非空——空列表会让"模型漏字段"这件事
    # 在比对里自动通过（没有元素就没有键可比）。
    fresh_project("editor")
    user = user_repo.get_by_username("superadmin")
    raw = services.workbench(user)
    modeled = WorkbenchOut(**raw).model_dump()
    assert _keys_of(raw) == _keys_of(modeled), \
        f"模型与响应不一致：多 {_keys_of(modeled) - _keys_of(raw)}，少 {_keys_of(raw) - _keys_of(modeled)}"


def test_看板模型覆盖响应里的每一个键(fresh_project):
    import services
    from models import ProjectDashboardOut

    pid = fresh_project("editor")
    raw = services.project_dashboard(pid)
    modeled = ProjectDashboardOut(**raw).model_dump()
    assert _keys_of(raw) == _keys_of(modeled), \
        f"模型与响应不一致：多 {_keys_of(modeled) - _keys_of(raw)}，少 {_keys_of(raw) - _keys_of(modeled)}"


def test_生产模型覆盖响应里的每一个键(fresh_project):
    """这一组键最多（实测 132 条路径），也最容易漏。

    实测踩过两次：`memory.parallel_reference` 是 dict 不是 str、
    `stats.signs` 是两层字典不是 `dict[str,int]`、`by_capability` 的值是
    "一包计数"不是 int——全是按字段名猜类型会错的地方。
    """
    import json

    import production
    from models import ProjectProductionOut
    from repositories.content import episodes, shots

    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    shots.create(eid, "开场", "", 5.0, [])
    overview = production.project_overview(pid, current_episode_id=eid)
    raw = {"overview": overview,
           "board": production.episode_board(pid, int(eid)),
           "stats": production.project_stats(pid, overview),
           "stages": production.STAGES,
           "statuses": production.PUBLIC_STATUSES}
    modeled = json.loads(ProjectProductionOut(**raw).model_dump_json())
    assert _keys_of(raw) == _keys_of(modeled), \
        f"模型与响应不一致：多 {_keys_of(modeled) - _keys_of(raw)}，少 {_keys_of(raw) - _keys_of(modeled)}"


def test_动态键字典里的任意键都会被保留():
    """`by_status` / `by_capability` / `signs` 的键来自库里的数据。

    写成固定字段的话，库里冒出第六类能力、第十种状态时，那个键会被
    `response_model` 静默剔掉——数字凭空少一块。一律用 `dict[str, ...]` 接住。
    """
    import json

    from models import ProjectProductionOut

    payload = {
        "overview": {"project_id": 1, "episodes": 0, "delivered_episodes": 0,
                     "progress": None, "current": None, "per_episode": [],
                     "assets": {}},
        "board": {"stages": [], "next_tasks": []},
        "stats": {"jobs_total": 0,
                  "by_status": {"某个还没实现的状态": 7},
                  "by_capability": {"ninth_capability": {"total": 3}},
                  "top_error_codes": [], "retries": {}, "memory": None,
                  "disk": None, "team": None, "money": None,
                  "signs": {"document": {"approved": 2}},
                  "estimate": None, "not_computed": []},
        "stages": [], "statuses": [],
    }
    modeled = json.loads(ProjectProductionOut(**payload).model_dump_json())
    assert modeled["stats"]["by_status"]["某个还没实现的状态"] == 7
    assert modeled["stats"]["by_capability"]["ninth_capability"]["total"] == 3
    assert modeled["stats"]["signs"]["document"]["approved"] == 2


def test_没有样本的读数是_None_而不是_0():
    """样本为 0 时不能给 0——"没有数据"和"数值是零"是两回事。

    产能读数写成 0 会让界面显示"每分钟出 0 秒成片"，那是假警报。
    """
    from models import WorkbenchThroughput

    empty = WorkbenchThroughput(samples=0)
    assert empty.video_avg_sec is None
    assert empty.image_avg_sec is None
    assert empty.per_minute_sec is None
    assert empty.samples == 0


# ---------- 第五批：models.py 拆成 schemas/ 包（2026-11-08） ----------

def test_models_只是门面不再膨胀():
    """`models.py` 现在只做 re-export。

    它原来是 2513 行的单文件（172 个定义）。拆成 `schemas/` 后如果没人看着，
    很容易又往里加东西、重新长成第二个大文件。这条测试守住"它是个门面"这件事。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    text = (root / "models.py").read_text(encoding="utf-8")
    body_lines = [ln for ln in text.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    assert len(body_lines) < 40, \
        f"models.py 又长起来了（{len(body_lines)} 行非注释）——新模型请加到 schemas/<域>.py"


def test_拆分没有丢任何一个公开名字():
    """拆分是**纯搬迁**：`from models import X` 必须一个都不能失效。

    这条不是核对某个具体名字，而是核对"门面导出的数量与 schemas 里定义的一致"——
    将来往 schemas 加了模型却忘了在 `__init__.py` 里 re-export，这里会红。
    """
    import ast
    import pathlib

    import models
    import schemas

    root = pathlib.Path(__file__).resolve().parents[1]
    pkg = root / "schemas"
    defined = set()
    for f in pkg.glob("*.py"):
        if f.name == "__init__.py":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                if not node.name.startswith("_"):
                    defined.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = (node.targets[0] if isinstance(node, ast.Assign) else node.target)
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    defined.add(target.id)

    missing = sorted(n for n in defined if not hasattr(models, n))
    assert not missing, f"这些名字定义在 schemas 里但 models 门面没导出：{missing}"
    # __all__ 与实际可导入的一致
    assert set(schemas.__all__) >= defined, "schemas.__all__ 漏了定义好的名字"


def test_schemas_每层都能单独导入():
    """拆包的价值在于**依赖方向清楚**。任何一个模块单独 import 失败，
    说明它引了还没就绪的东西——那正是循环依赖的前兆。"""
    import importlib
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    names = sorted(p.stem for p in (root / "schemas").glob("*.py") if p.name != "__init__.py")
    assert names, "schemas 包是空的？"
    for name in names:
        importlib.import_module(f"schemas.{name}")     # 失败会直接抛，不需要断言


def test_schemas_的模块划分是有序的():
    """`enums` 必须是最底层（纯常量，被很多人依赖）。

    它一旦反过来依赖别人，整个包的分层就塌了——`repositories/ordering.py`
    直接依赖 `models.BACKENDS`，那条路径不能被拖进更深的依赖链。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "schemas" / "enums.py").read_text(encoding="utf-8"))
    relative = [n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and (n.module or "").startswith(".") ]
    assert not relative, f"enums 不该依赖包内其他模块（会形成环），现在依赖了 {relative}"
