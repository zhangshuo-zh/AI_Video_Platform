"""引擎实例与本机服务型引擎的接线（W-B：CR-ENGINE-001 的 CR-001／CR-002／CR-005）。

这一批用例守的是四条会被"顺手做通"的口子，每条都对着文档里写过的边界：

1. **实例不替代规格**（v1.1 边界①）：`engine_code` 不在清单里就必须被拒，
   否则模型中心会长出第二份"什么能跑"。
2. **健康不落库**（v1.1 边界②）：列表里的 `health_state` 是现查值，库里没有那一列。
   这条由"表里出现这几列就该红"钉住——删列这种事只会在有人加它的时候发生。
3. **甲闸门两条提交路都要过**（G1b 已裁＝甲）：默认轨与显式选择一视同仁。
4. **CI 上没有 Draw Things 也不许变红**：探测与 HTTP 一律 monkeypatch 假造；
   真机那一条走 `tools/`，不混进单测。

patch 的是 `engines.probe` 这个**模块属性**：`adapters.health`、`runners._draw_things`、
`services._service_engine_gate` 都是运行时调 `engines.probe(...)`，所以改一处三处齐变。
这顺带证明了一件事——这一族代码没有各自再写一份探测。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import adapters
import engine_http_run
import engines
import runners
import services
from repositories.engine_instances import EngineInstanceError, engine_instances

SLOT = "http://127.0.0.1:7860"


# ---------- 仓储层：端点形状与键不可改 ----------

@pytest.mark.parametrize("bad,keyword", [
    ("", "不能空"),
    ("127.0.0.1:7860", "只接受 http://"),
    ("https://127.0.0.1:7860", "只接受 http://"),
    ("http://u:p@127.0.0.1:7860", "不许带用户名或口令"),
    ("http://127.0.0.1:7860?token=x", "不许带查询串"),
    ("http://127.0.0.1:80", "端口"),
    ("http://127.0.0.1:7860/sdapi", "不要带路径"),
])
def test_端点形状逐条拦住(bad, keyword):
    with pytest.raises(EngineInstanceError) as exc:
        engine_instances.create({"engine_code": "draw_things", "endpoint": bad})
    assert keyword in str(exc.value)


def test_同一端点不许登记两次():
    first = engine_instances.create({"engine_code": "draw_things",
                                     "endpoint": "http://127.0.0.1:7901"})
    try:
        with pytest.raises(EngineInstanceError) as exc:
            engine_instances.create({"engine_code": "draw_things",
                                     "endpoint": "http://127.0.0.1:7901"})
        assert f"#{first['id']}" in str(exc.value)
    finally:
        engine_instances.delete(first["id"])


def test_端点是键改不了():
    row = engine_instances.create({"engine_code": "draw_things",
                                   "endpoint": "http://127.0.0.1:7902"})
    try:
        with pytest.raises(EngineInstanceError) as exc:
            engine_instances.update(row["id"], {"endpoint": "http://127.0.0.1:7903"})
        assert "新建一条" in str(exc.value)
    finally:
        engine_instances.delete(row["id"])


def test_实例表里没有健康列():
    """v1.1 边界②的硬钉子：这几列一旦被加回来，这条就该红。"""
    row = engine_instances.create({"engine_code": "draw_things",
                                   "endpoint": "http://127.0.0.1:7904"})
    try:
        for forbidden in ("health_status", "detected_version", "last_probed_at",
                          "last_error_code", "last_error_summary"):
            assert forbidden not in set(row.keys()), f"实例表不该存结论：{forbidden}"
    finally:
        engine_instances.delete(row["id"])


# ---------- 路由层 ----------

def test_清单外的引擎被拒(as_role):
    response = as_role("admin").post("/api/engines",
                                     json={"engine_code": "not_in_manifest",
                                           "endpoint": "http://127.0.0.1:7905"})
    assert response.status_code == 422
    assert "改规格" in response.json()["detail"]


def test_建实例_列表带现查健康_且探测不建Job(monkeypatch, as_role):
    monkeypatch.setattr(engines, "probe", lambda code, endpoint="", **_:  engines.EngineStatus(
        "ok", "服务在线，当前模型 x.ckpt",
        {"endpoint": endpoint or SLOT, "model": "x.ckpt", "keys": 87}))
    c = as_role("admin")
    total_before = len(c.get("/api/jobs", params={"limit": 1}).json())
    created = c.post("/api/engines", json={"engine_code": "draw_things",
                                           "endpoint": SLOT,
                                           "display_name": "本机 DT"})
    assert created.status_code == 201, created.text
    instance_id = created.json()["id"]
    assert created.json()["warning"] == ""        # 探测成功就不该有提示
    try:
        items = c.get("/api/engines").json()["items"]
        hit = next(i for i in items if i["id"] == instance_id)
        assert hit["health_state"] == "ok" and hit["engine_model"] == "x.ckpt"
        assert hit["engine_label"] == "Draw Things（本机应用）"
        assert "local_http" in c.get("/api/engines").json()["connection_types"]
        assert len(c.get("/api/jobs", params={"limit": 1}).json()) == total_before, \
            "探测不许建 Job"
        probed = c.post(f"/api/engines/{instance_id}/probe")
        assert probed.status_code == 200 and probed.json()["state"] == "ok"
    finally:
        blocked = c.delete(f"/api/engines/{instance_id}")
        assert blocked.status_code == 422, "没带 confirm 必须挡"
        removed = c.delete(f"/api/engines/{instance_id}", params={"confirm": "1"})
        assert removed.status_code == 200


def test_连不上时仍建成但带回原话(monkeypatch, as_role):
    """登记一台还没启动的服务是合法动作：界面要知道"留下了但连不上"，
    而不是把这件事做成 4xx——那会让人以为自己的地址写错了。"""
    monkeypatch.setattr(engines, "probe", lambda code, endpoint="", **_:  engines.EngineStatus(
        "missing", "http://127.0.0.1:7906 连不上：拒绝连接", {}))
    c = as_role("admin")
    response = c.post("/api/engines", json={"engine_code": "draw_things",
                                            "endpoint": "http://127.0.0.1:7906"})
    assert response.status_code == 201
    assert "连不上" in response.json()["warning"]
    c.delete(f"/api/engines/{response.json()['id']}", params={"confirm": "1"})


def test_非管理员看不到实例(as_role):
    assert as_role("editor").get("/api/engines").status_code == 403


# ---------- 甲闸门 ----------

def _gate_inputs(monkeypatch, live: str, artifact: str):
    monkeypatch.setattr(engines, "probe", lambda code, endpoint="", **_:  engines.EngineStatus(
        "ok", "在线", {"endpoint": SLOT, "model": live}))
    monkeypatch.setattr(services.registration_repo, "find_registration",
                        lambda *a, **k: {"artifact_path": artifact})


def test_引擎选的与登记不符就拒绝(monkeypatch):
    _gate_inputs(monkeypatch, "othermodel_f16.ckpt", "noobaixlnaixl_f16.ckpt")
    with pytest.raises(services.JobInvalid) as exc:
        services._service_engine_gate("image", "local", "draw_things",
                                      "noobai-xl-epsilon-11")
    message = str(exc.value)
    assert "平台不代切模型" in message and "othermodel_f16.ckpt" in message


def test_相符就放行(monkeypatch):
    _gate_inputs(monkeypatch, "NOOBAIXLNAIXL_f16.CKPT", "noobaixlnaixl_f16.ckpt")
    services._service_engine_gate("image", "local", "draw_things",
                                  "noobai-xl-epsilon-11")


def test_登记没填权重路径时说清缺什么(monkeypatch):
    _gate_inputs(monkeypatch, "anything.ckpt", "")
    with pytest.raises(services.JobInvalid) as exc:
        services._service_engine_gate("image", "local", "draw_things",
                                      "noobai-xl-epsilon-11")
    assert "权重路径" in str(exc.value)


def test_服务连不上时不放行(monkeypatch):
    monkeypatch.setattr(engines, "probe", lambda code, endpoint="", **_:  engines.EngineStatus(
        "missing", "连不上：拒绝连接", {}))
    with pytest.raises(services.JobInvalid) as exc:
        services._service_engine_gate("image", "local", "draw_things",
                                      "noobai-xl-epsilon-11")
    assert "现在不能用" in str(exc.value)


def test_mflux这类根本不过这道闸门(monkeypatch):
    """闸门第一句就 return：不是 SERVICE_ENGINES 的引擎不该被拖去连 HTTP。"""
    called = []
    monkeypatch.setattr(engines, "probe",
                        lambda *a, **k: called.append(a) or engines.EngineStatus(
                            "missing", "不该被调到", {}))
    services._service_engine_gate("image", "local", "mflux", "flux1-schnell")
    assert called == []


def test_默认轨那条路也过闸门(monkeypatch, as_role, seeded, provider_state):
    """显式选择与默认轨都要过——只挂一支会留下"没人核对"的缝。

    这里走的是默认轨：把 image×local 那一格配成 draw_things，提交时**不带**
    provider/model，让 `resolve_provider()` 自己解析出来。
    为什么不能改用显式选择来测这条：`submit_job` 的显式那一支第一步就要求
    与槽位一致（"请先改配置"），闸门在它后面——用显式路测这道闸门会假绿。
    这也顺带说明 W-B 还接不上"任务级选第二引擎"，那条要等候选池那一轮。
    """
    seen = []
    real = services._service_engine_gate

    def spy(*args):
        seen.append(args)
        return real(*args)

    # 2026-11-08 `services.py` 拆包后 `_service_engine_gate` 住在 `services/jobs.py`，
    # 而 `submit_job()` 与它是**同一个模块内的直接调用**（不走门面属性）。
    # 所以必须打在真模块上——打在 `services` 门面上不会影响任何调用。
    import services.jobs as services_jobs
    monkeypatch.setattr(services_jobs, "_service_engine_gate", spy)
    _gate_inputs(monkeypatch, "different.ckpt", "wanted.ckpt")
    from repositories.providers import providers
    row = providers.slot("image", "local")
    providers.replace_many([{**dict(row), "provider": "draw_things",
                             "model": "noobai-xl-epsilon-11",
                             "enabled": 1, "is_default": 1}])
    response = as_role("admin").post("/api/jobs", json={
        "project_id": seeded["project_id"], "capability": "image",
        "prompt": "一只青铜爵", "params": {"width": 1024, "height": 1024, "steps": 4}})
    assert seen, "默认轨这条提交路没经过闸门"
    assert response.status_code == 422
    assert "不代切" in response.json()["detail"]


# ---------- 执行形状 ----------

def _job(job_id=91, **overrides):
    base = {"id": job_id, "capability": "image", "execution_mode": "local",
            "provider_code": "draw_things", "model_code": "noobai-xl-epsilon-11",
            "prompt_snapshot": "a red cube"}
    base.update(overrides)
    return base


def test_draw_things的spec与请求文件(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "probe", lambda code, endpoint="", **_:  engines.EngineStatus(
        "ok", "在线", {"endpoint": SLOT, "model": "m.ckpt"}))
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    spec = runners.build(_job(), {"width": 512, "height": 512, "steps": 4}, outputs)
    assert spec.runner == "draw_things" and spec.media == "image"
    assert spec.argv[1].endswith("engine_http_run.py")
    request = json.loads(Path(spec.meta["request_path"]).read_text(encoding="utf-8"))
    assert request["endpoint"] == SLOT
    assert request["path"] == "/sdapi/v1/txt2img"
    assert request["body"] == {"prompt": "a red cube", "width": 512,
                               "height": 512, "steps": 4}
    assert "seed" not in request["body"], "没给 seed 就不要发一个过去"
    assert spec.meta["engine_model"] == "m.ckpt"
    assert spec.expected[0].name.endswith("91-draw_things.png")


def test_服务不可用时不起进程(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "probe", lambda code, endpoint="", **_:  engines.EngineStatus(
        "unknown", "还没有登记启用中的引擎实例", {}))
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    with pytest.raises(runners.RunnerUnavailable) as exc:
        runners.build(_job(job_id=92), {}, outputs)
    assert "还没有登记" in exc.value.message


def test_分支表与build一致():
    assert runners.has_branch("image", "draw_things")
    assert runners.has_branch("image", "mflux")
    assert not runners.has_branch("image", "not_a_provider")


# ---------- HTTP 脚本本体 ----------

class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    @property
    def status(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _call_script(monkeypatch, tmp_path, responses=None, extra=None, transport=None):
    """摆好请求文件、假造 urlopen、跑 main()，返回 (退出码, 产物, 回执)。

    `transport` 用来测失败分支（抛 HTTPError／URLError）；不给就用 `responses`
    排队。两种都必须走同一个 helper——否则 helper 会把失败分支的 patch 覆盖掉，
    测的就不是那条路。
    """
    if transport is None:
        queue = iter(responses or [])
        transport = lambda *a, **k: next(queue)      # noqa: E731
    monkeypatch.setattr(engine_http_run, "urlopen", transport)
    out = tmp_path / "out.png"
    receipt = tmp_path / "receipt.json"
    request = tmp_path / "req.json"
    body = {"endpoint": SLOT, "path": "/sdapi/v1/txt2img",
            "body": {"prompt": "x", "width": 512, "height": 512, "steps": 4},
            "output": str(out), "receipt": str(receipt)}
    body.update(extra or {})
    request.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["engine_http_run.py", str(request)])
    code = engine_http_run.main()
    payload = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {}
    return code, out, payload


def test_脚本把base64落成png并写回执(monkeypatch, tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 200
    code, out, payload = _call_script(monkeypatch, tmp_path, [
        _FakeResponse(json.dumps({"images": [base64.b64encode(png).decode()]}).encode()),
        _FakeResponse(json.dumps({"model": "m.ckpt"}).encode()),
    ])
    assert code == 0 and payload["ok"] is True
    assert out.read_bytes() == png
    assert payload["engine_model"] == "m.ckpt", "回执要能对上账：当时引擎选的是哪一档"
    assert "images" in payload["response_keys"]


def test_空images算失败且不写产物(monkeypatch, tmp_path):
    code, out, payload = _call_script(monkeypatch, tmp_path, [
        _FakeResponse(json.dumps({"images": []}).encode())])
    assert code == 1 and payload["error"] == "no_image"
    assert not out.exists(), "没有图就不许留一个空文件冒充成功"


def test_过小文件也算失败(monkeypatch, tmp_path):
    tiny = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 8).decode()
    code, out, payload = _call_script(monkeypatch, tmp_path, [
        _FakeResponse(json.dumps({"images": [tiny]}).encode())])
    assert code == 1 and payload["error"] == "empty_image"


def test_非200时把detail带回来(monkeypatch, tmp_path):
    import urllib.error

    def raise_422(*a, **k):
        raise urllib.error.HTTPError(SLOT, 422, "Unprocessable Entity", {}, None)

    code, out, payload = _call_script(monkeypatch, tmp_path, transport=raise_422)
    assert code == 1 and payload["error"] == "http_422"
    assert not out.exists()


def test_连不上时归一成engine_unreachable(monkeypatch, tmp_path):
    import urllib.error

    def raise_refused(*a, **k):
        raise urllib.error.URLError("Connection refused")

    code, out, payload = _call_script(monkeypatch, tmp_path, transport=raise_refused)
    assert code == 1 and payload["error"] == "engine_unreachable"
    assert " refused" in payload["message"]


def test_密钥只从环境变量名取(monkeypatch, tmp_path):
    monkeypatch.delenv("DT_SECRET", raising=False)
    code, out, payload = _call_script(monkeypatch, tmp_path, [],
                                      extra={"secret_env": "DT_SECRET"})
    assert code == 1 and payload["error"] == "secret_env_missing"
    assert "DT_SECRET" in payload["message"]      # 变量名可以说，值永远不说


# ---------- 清单本身 ----------

def test_引擎规格有出处():
    adapter = adapters.find("image", "local", "draw_things")
    assert adapter is not None
    assert adapter.params_source, "服务型引擎的参数也要有出处"
    assert adapter.home_env == "", "地址住在 engine_instance 表，不在环境变量"
    errors = adapters.validate_params(adapter, "noobai-xl-epsilon-11", {"width": 64})
    assert errors and "128" in errors[0], "128 这条实测下限要能拦住"


def test_探测留痕只由显式动作写(as_role):
    """0042：读路径一次都不写，显式探测才留痕。

    这条守的是 0040 那条"健康不落库"不被打穿的边界——0040 不许落的是**当前状态**
    （那会长出两份答案），0042 落的是**历史事件**（只追加、不参与判断）。
    所以判据不是"有没有写库"，而是"读接口会不会写"。
    """
    import engines
    from repositories.engine_instances import engine_probe_log

    c = as_role("admin")
    engines.forget_probes()
    made = c.post("/api/engines", json={
        "engine_code": "draw_things", "endpoint": "http://127.0.0.1:7891"}).json()
    iid = made["id"]
    try:
        # 建实例那一枪会留痕（source=create）——它是这台机器的第一个事实点
        rows = engine_probe_log.recent(iid)
        assert len(rows) == 1 and rows[0]["source"] == "create"
        before = len(rows)

        # 读列表三次：一条都不许新增
        for _ in range(3):
            item = next(i for i in c.get("/api/engines").json()["items"]
                        if i["id"] == iid)
            assert item["last_probe_source"] == "create"
            assert item["last_probe_at"]
        assert len(engine_probe_log.recent(iid)) == before, "读路径写了留痕"

        # 手动探测：多一条，且 source=manual、带耗时
        out = c.post(f"/api/engines/{iid}/probe").json()
        assert "latency_ms" in out
        rows = engine_probe_log.recent(iid)
        assert len(rows) == before + 1
        assert rows[0]["source"] == "manual"
        assert rows[0]["latency_ms"] is not None
        # 端点与引擎 code 冗余存了一份：实例被撤之后"当时探的是哪台"仍答得出来
        assert rows[0]["endpoint"] == "http://127.0.0.1:7891"
        assert rows[0]["engine_code"] == "draw_things"

        # 历史列表接口：最新在前，带年龄
        hist = c.get(f"/api/engines/{iid}/probes?limit=5").json()
        assert len(hist["items"]) == 2
        assert hist["items"][0]["source"] == "manual", "最新那条要排第一"
        assert hist["items"][0]["age_seconds"] is not None
        assert "不参与任何判断" in hist["note"]
    finally:
        c.delete(f"/api/engines/{iid}?confirm=1")


def test_撤实例时探测历史跟着走(as_role):
    """0042 的外键是 CASCADE：实例没了，它的探测史不该变成孤儿行。

    但**历史作业不受影响**——那一条由 0040 保证（作业行冻结的是快照，不是外键）。
    """
    import db
    from repositories.engine_instances import engine_probe_log

    c = as_role("admin")
    made = c.post("/api/engines", json={
        "engine_code": "draw_things", "endpoint": "http://127.0.0.1:7892"}).json()
    iid = made["id"]
    assert len(engine_probe_log.recent(iid)) >= 1
    c.delete(f"/api/engines/{iid}?confirm=1")
    with db.get_conn() as conn:
        left = conn.execute("SELECT COUNT(*) FROM engine_probe_log WHERE instance_id = ?",
                            (iid,)).fetchone()[0]
    assert left == 0, "实例撤了，探测历史还在（外键没生效？）"
