"""W10：云端 adapter、密钥边界与回执落库。

这里**不打真供应商**：本地起一个 HTTP 服务模拟 DeepSeek 与 Wan 的响应，
把"成功 / 401 / 429 / 失败任务 / 缺密钥"几条分支跑穿。真实的 DeepSeek
一次调用记在 `盘点/W10_CLOUD.md`，不塞进 pytest（会花钱、也要联网）。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import adapters
import cloud_run
import config
import runners
import services
import worker
from models import JobSubmitIn
from repositories.jobs import jobs as job_repo

SECRET = "sk-test-secret-123456"


class _Handler(BaseHTTPRequestHandler):
    """按 server.scenario 回包；把收到的请求留在 server.calls 里给断言用。"""

    def log_message(self, *args):  # 静音测试输出
        return

    def _send(self, status: int, body: bytes, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {}
        self.server.calls.append(("POST", self.path, dict(self.headers), body))
        scenario = self.server.scenario
        auth = self.headers.get("Authorization") or ""

        # 取消异步任务（2026-11-10，B12）：`POST /api/v1/tasks/{id}/cancel`。
        # 真实接口只接受 PENDING 状态的任务，这里按 scenario 模拟两种答复。
        if self.path.startswith("/api/v1/tasks/") and self.path.endswith("/cancel"):
            if scenario == "cancel_running":
                # 任务已经开跑：阿里云返回 400 UnsupportedOperation
                self._json(400, {"code": "UnsupportedOperation",
                                 "message": "Failed to cancel the task. "
                                            "Confirm that the task is in PENDING status."})
                return
            if scenario == "cancel_401":
                self._json(401, {"code": "InvalidApiKey",
                                 "message": "bad key " + SECRET})
                return
            # 默认：取消成功。真实成功响应是**一个裸 JSON 字符串**（request id），
            # 不是对象——照实现，否则解析差异会把这条测试变成假绿。
            self._send(200, json.dumps("45ac7f13-cancel-ok").encode("utf-8"))
            return

        if self.path.endswith("/chat/completions"):
            if scenario == "deepseek_ok":
                if auth != f"Bearer {SECRET}":
                    self._json(401, {"error": {"message": "bad key " + SECRET,
                                               "code": "invalid_api_key"}})
                    return
                self._json(200, {
                    "id": "chatcmpl-test-1",
                    "model": "deepseek-flash",
                    "choices": [{"message": {"content": "你好，这是云端正文。"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7,
                              "total_tokens": 19},
                })
                return
            if scenario == "deepseek_401":
                self._json(401, {"error": {
                    "message": "Authentication Fails, key " + SECRET,
                    "code": "invalid_api_key"}})
                return
            if scenario == "deepseek_429":
                self._json(429, {"error": {"message": "rate limited",
                                           "code": "rate_limit"}})
                return
            if scenario == "deepseek_receipt_lost":
                # 请求已被服务端记录，故意不发回执，模拟受理后断线。
                self.close_connection = True
                return
            if scenario == "deepseek_500_with_id":
                # 5xx **并且带 request_id**：供应商侧任务可能已经建立、甚至已计费。
                # 这一档要落 needs_attention（待核对），不能按普通失败建议重试。
                self._json(500, {"error": {"message": "upstream exploded",
                                           "code": "server_error"},
                                 "request_id": "chatcmpl-500-1"})
                return
            self._json(500, {"error": {"message": "unknown scenario"}})
            return

        if self.path.endswith("/video-synthesis"):
            self._json(200, {"output": {"task_id": "task-123"},
                             "request_id": "req-wan-1"})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_GET(self):
        self.server.calls.append(("GET", self.path, dict(self.headers), {}))
        scenario = self.server.scenario
        if self.path == "/video.mp4":
            payload = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32
            self._send(200, payload, "video/mp4")
            return
        if self.path.startswith("/api/v1/tasks/"):
            if scenario == "wan_fail":
                self._json(200, {"output": {"task_status": "FAILED",
                                            "message": "generation failed"}})
                return
            # 第一次 PENDING，第二次 SUCCEEDED：验证真的在轮询
            self.server.polls += 1
            if self.server.polls == 1:
                self._json(200, {"output": {"task_status": "RUNNING"}})
            else:
                self._json(200, {
                    "output": {
                        "task_status": "SUCCEEDED",
                        "video_url": f"http://127.0.0.1:{self.server.server_address[1]}/video.mp4",
                    },
                    "usage": {"duration": 5, "ratio": "16:9", "SR": 720},
                })
            return
        self._json(404, {"error": {"message": "not found"}})


@pytest.fixture
def mock_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.scenario = "deepseek_ok"
    server.calls = []
    server.polls = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _base(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _request(tmp_path: Path, provider="deepseek", **extra) -> Path:
    data = {
        "provider": provider,
        "capability": "text" if provider == "deepseek" else "video",
        "model": "deepseek-flash" if provider == "deepseek" else "wan2.7-t2v",
        "prompt": "写一句测试正文",
        "params": {} if provider == "deepseek" else {"resolution": "720P", "ratio": "16:9", "duration": 5},
        "key_env": "DEEPSEEK_API_KEY" if provider == "deepseek" else "DASHSCOPE_API_KEY",
        "output": str(tmp_path / ("out.md" if provider == "deepseek" else "out.mp4")),
        "result": str(tmp_path / "result.json"),
        "http_timeout": 5,
        "poll_interval": 0.01,
        "poll_timeout": 5,
        "download_timeout": 5,
    }
    data.update(extra)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_清单登记_deepseek_与_wan_的官方价():
    ds = adapters.find("text", "cloud", "deepseek")
    assert ds is not None and ds.models
    flash = next(m for m in ds.models if m.code == "deepseek-flash")
    price = flash.prices[0]
    assert price.unit == "million_token" and price.currency == "CNY"
    assert price.amount_micros == 8_000_000
    assert "2026-11-01" in price.source

    wan = adapters.find("video", "cloud", "wan")
    model = next(m for m in wan.models if m.code == "wan2.7-t2v")
    assert {p.name for p in model.params} == {"resolution", "ratio", "duration", "seed"}
    prices = {p.label: p.amount_micros for p in model.prices}
    assert prices == {"720P": 600_000, "1080P": 1_000_000}


def test_云端参数只认白名单与枚举():
    wan = adapters.find("video", "cloud", "wan")
    assert adapters.validate_params(wan, "wan2.7-t2v",
                                    {"resolution": "1080P", "duration": 5}) == []
    errors = adapters.validate_params(wan, "wan2.7-t2v", {"resolution": "4K"})
    assert errors and "只能是" in errors[0]
    assert adapters.validate_params(wan, "wan2.7-t2v", {"ratio": "16:9"}) == []
    assert adapters.validate_params(wan, "wan2.7-t2v", {"ratio": "21:9"})
    ds = adapters.find("text", "cloud", "deepseek")
    assert adapters.validate_params(ds, "deepseek-flash", {"max_tokens": 64}) == []
    assert adapters.validate_params(ds, "deepseek-flash", {"top_p": 0.9})


def test_cloud_run_deepseek_成功并写回执(tmp_path, monkeypatch, mock_api):
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(mock_api))
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    request = _request(tmp_path, params={"max_tokens": 64, "thinking": False})

    assert cloud_run.run(request) == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["request_id"] == "chatcmpl-test-1"
    assert result["usage"]["total_tokens"] == 19
    assert result["response_meta"]["finish_reason"] == "stop"
    assert (tmp_path / "out.md").read_text(encoding="utf-8").startswith("你好")
    # 密钥值不能出现在回执里
    assert SECRET not in (tmp_path / "result.json").read_text(encoding="utf-8")


def test_cloud_run_401_错误脱敏(tmp_path, monkeypatch, mock_api):
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(mock_api))
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    mock_api.scenario = "deepseek_401"

    assert cloud_run.run(_request(tmp_path)) == 2
    text = (tmp_path / "result.json").read_text(encoding="utf-8")
    result = json.loads(text)
    assert result["ok"] is False
    assert result["error"]["code"] == "auth"
    assert SECRET not in text and "***" in text


def test_cloud_run_429_是限流不是坏参数(tmp_path, monkeypatch, mock_api):
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(mock_api))
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    mock_api.scenario = "deepseek_429"

    assert cloud_run.run(_request(tmp_path)) == 2
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["error"]["code"] == "rate_limit"
    assert result["http_status"] == 429


def test_缺密钥时一次请求都不发(tmp_path, monkeypatch, mock_api):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(mock_api))

    assert cloud_run.run(_request(tmp_path)) == 2
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["error"]["code"] == "missing_key"
    assert mock_api.calls == []


def test_cloud_run_wan_创建轮询下载(tmp_path, monkeypatch, mock_api):
    monkeypatch.setenv("DASHSCOPE_API_KEY", SECRET)
    monkeypatch.setenv("ALIYUN_WAN_BASE_URL", _base(mock_api))
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    mock_api.scenario = "wan_ok"

    assert cloud_run.run(_request(tmp_path, provider="wan")) == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["request_id"] == "task-123"
    assert result["task_request_id"] == "req-wan-1"
    assert result["task_status"] == "SUCCEEDED"
    assert result["usage"]["duration"] == 5
    # 官方 ratio 是参数，不是回执字段；提交时确实进了请求体
    create = next(c for c in mock_api.calls if c[1].endswith("/video-synthesis"))
    assert create[3]["parameters"]["ratio"] == "16:9"
    assert (tmp_path / "out.mp4").read_bytes()[4:8] == b"ftyp"
    # 至少轮询了一次 PENDING 才拿到成功
    assert mock_api.polls >= 2


def test_wan_没有_base_url_时在发请求前失败(tmp_path, monkeypatch, mock_api):
    monkeypatch.setenv("DASHSCOPE_API_KEY", SECRET)
    monkeypatch.delenv("ALIYUN_WAN_BASE_URL", raising=False)
    mock_api.scenario = "wan_ok"

    assert cloud_run.run(_request(tmp_path, provider="wan")) == 2
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["error"]["code"] == "missing_base_url"
    assert not mock_api.calls


def test_runner_请求文件只有变量名没有密钥值(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    spec = runners.build({
        "id": 9, "capability": "text", "execution_mode": "cloud",
        "provider_code": "deepseek", "model_code": "deepseek-flash",
        "prompt_snapshot": "写一句",
    }, {"max_tokens": 32}, outputs)
    assert spec.argv[:2] == [__import__("sys").executable, str(runners.CLOUD_RUNNER)]
    assert spec.media == "text"
    request_text = (outputs / "9-cloud-request.json").read_text(encoding="utf-8")
    assert SECRET not in request_text
    request = json.loads(request_text)
    assert request["key_env"] == "DEEPSEEK_API_KEY"
    assert request["output"].endswith("9-deepseek.md")


def test_worker_云端文本端到端写请求ID与用量(tmp_path, monkeypatch, mock_api,
                                             provider_state, clean_queue, fresh_project,
                                             register_tier):
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(mock_api))
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    mock_api.scenario = "deepseek_ok"

    import db
    with db.get_conn() as conn:
        conn.execute("""UPDATE provider_config SET provider='deepseek',
                          model='deepseek-flash', enabled=1, is_default=0,
                          key_env='DEEPSEEK_API_KEY'
                        WHERE capability='text' AND backend='cloud'""")
    register_tier("text", "cloud", "deepseek", "deepseek-flash")

    pid = fresh_project("editor")
    payload = JobSubmitIn(
        project_id=pid, capability="text", execution_mode="cloud",
        provider_code="deepseek", model_code="deepseek-flash",
        prompt="用一句话说明为什么天空是蓝的。",
        params={"max_tokens": 64},
        estimate={"quantity": 0.002, "unit": "million_token",
                  "unit_price_micros": 8_000_000, "currency": "CNY",
                  "source": "测试价"},
        user_confirmed_cloud=True,
    )
    row = services.submit_job(payload, created_by=None)
    assert worker.run_once("w10-test-worker") == row["id"]

    done = job_repo.get(row["id"])
    assert done["status"] == "succeeded", done["error_message"]
    assert done["provider_request_id"] == "chatcmpl-test-1"
    res = json.loads(done["resources_json"])
    assert res["provider"]["usage"]["total_tokens"] == 19
    log = (config.JOBS_DIR / str(row["id"]) / "logs" / "run.log").read_text(encoding="utf-8")
    assert "provider 回执" in log and "chatcmpl-test-1" in log
    assert SECRET not in log
    # 产物是一份正文，可以被 /artifacts 读出
    assert done["result_paths_json"] != "[]"


@pytest.mark.parametrize("scenario, request_id, error_code", [
    ("deepseek_500_with_id", "chatcmpl-500-1", "provider_unavailable"),
    ("deepseek_receipt_lost", None, "provider_cloud_run_error"),
])
def test_云端结果未知进待核对而不是普通失败(scenario, request_id, error_code,
                                             monkeypatch, mock_api,
                                             provider_state, clean_queue, fresh_project,
                                             register_tier):
    """错误码前缀不能拼两次——拼两次会让"结果未知"伪装成"失败，可重试"。

    `cloud_run.py` 给 5xx 的码就叫 `provider_unavailable`／`provider_error`，
    worker 过去无条件再拼一次 `provider_`，于是 `CLOUD_UNCERTAIN_CODES`
    查不到这个名字：账上写的是"待对账"，状态却成了"失败"，界面给的默认建议是重试,
    而重试正是对一笔可能已扣费的调用最坏的建议。这条是六场景真跑（场景四）照出来的。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(mock_api))
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    mock_api.scenario = scenario
    register_tier("text", "cloud", "deepseek", "deepseek-flash")

    import db
    with db.get_conn() as conn:
        conn.execute("""UPDATE provider_config SET provider='deepseek',
                          model='deepseek-flash', enabled=1, is_default=0,
                          key_env='DEEPSEEK_API_KEY'
                        WHERE capability='text' AND backend='cloud'""")

    pid = fresh_project("editor")
    row = services.submit_job(JobSubmitIn(
        project_id=pid, capability="text", execution_mode="cloud",
        provider_code="deepseek", model_code="deepseek-flash", prompt="会撞上 5xx",
        params={"max_tokens": 16},
        estimate={"quantity": 0.001, "unit": "million_token",
                  "unit_price_micros": 8_000_000, "currency": "CNY",
                  "source": "测试价"},
        user_confirmed_cloud=True), created_by=None)
    assert worker.run_once("w-uncertain") == row["id"]

    done = job_repo.get(row["id"])
    assert done["error_code"] == error_code, done["error_code"]
    assert not done["error_code"].startswith("provider_provider_")
    assert done["status"] == "needs_attention", done["status"]
    assert done["billable_status"] == "pending_reconciliation"
    assert done["provider_request_id"] == request_id
    assert len([c for c in mock_api.calls if c[0] == "POST"]) == 1
    assert worker.run_once("no-repeat-after-lost-receipt") is None
    assert len([c for c in mock_api.calls if c[0] == "POST"]) == 1
    assert "云端调用结果未知" in (done["attention_reason"] or "")


def test_云端401失败也留请求ID(tmp_path, monkeypatch, mock_api,
                              provider_state, clean_queue, fresh_project, register_tier):
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(mock_api))
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    mock_api.scenario = "deepseek_401"
    register_tier("text", "cloud", "deepseek", "deepseek-flash")

    import db
    with db.get_conn() as conn:
        conn.execute("""UPDATE provider_config SET provider='deepseek',
                          model='deepseek-flash', enabled=1, is_default=0,
                          key_env='DEEPSEEK_API_KEY'
                        WHERE capability='text' AND backend='cloud'""")

    pid = fresh_project("editor")
    payload = JobSubmitIn(
        project_id=pid, capability="text", execution_mode="cloud",
        provider_code="deepseek", model_code="deepseek-flash", prompt="会用错密钥",
        params={"max_tokens": 16},
        estimate={"quantity": 0.001, "unit": "million_token",
                  "unit_price_micros": 8_000_000, "currency": "CNY",
                  "source": "测试价"},
        user_confirmed_cloud=True,
    )
    row = services.submit_job(payload, created_by=None)
    worker.run_once("w10-test-worker")

    failed = job_repo.get(row["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "provider_auth"
    assert failed["provider_request_id"] is None  # 401 没有请求 ID
    assert SECRET not in (failed["error_message"] or "")
    # 失败不动成本：预计金额还在账上，账单状态仍是待核对
    assert failed["cost_micros"] == 8_000
    assert failed["billable_status"] == "pending_reconciliation"


def test_默认本地作业不会走云端(provider_state, clean_queue, fresh_project):
    import db
    with db.get_conn() as conn:
        conn.execute("""UPDATE provider_config SET provider='fastmetal',
                          model='fastmetal-1.3b-qad', enabled=1, is_default=1
                        WHERE capability='video' AND backend='local'""")
        # 云端槽位即使启用、可跑，也不许抢默认
        conn.execute("""UPDATE provider_config SET provider='wan',
                          model='wan2.7-t2v', enabled=1, is_default=0
                        WHERE capability='video' AND backend='cloud'""")
    pid = fresh_project("editor")
    payload = JobSubmitIn(project_id=pid, capability="video",
                          prompt="只走默认轨", params={})
    row = services.submit_job(payload, created_by=None)
    assert row["execution_mode"] == "local"
    assert row["provider_code"] == "fastmetal"


def test_本地失败一次云端请求都不发(clean_queue, fresh_project, mock_api):
    """本地假适配器模拟失败：作业落 failed，但 mock 云端服务一次都没被访问。"""
    pid = fresh_project("editor")
    payload = JobSubmitIn(
        project_id=pid, capability="text", prompt="本地失败样本",
        provider_code="mock", model_code="mock-1", execution_mode="local",
        params={"sleep": 0.05, "fail": True},
    )
    row = services.submit_job(payload, created_by=None)
    assert worker.run_once("w10-local-fail-worker") == row["id"]

    done = job_repo.get(row["id"])
    assert done["status"] == "failed"
    assert done["execution_mode"] == "local"
    assert done["error_code"] == "mock_failed"
    assert mock_api.calls == []


def test_本地失败重试仍是本地(provider_state, clean_queue, fresh_project):
    pid = fresh_project("editor")
    row = job_repo.create({
        "project_id": pid, "capability": "video", "kind": "video_local",
        "execution_mode": "local", "provider_code": "fastmetal",
        "model_code": "fastmetal-1.3b-qad", "adapter_version": "test",
        "prompt_snapshot": "失败样本", "params": {}, "input_refs": [],
    })
    claimed = job_repo.claim_next("w10-retry-worker", 60)
    assert claimed is not None and claimed["id"] == row["id"]
    job_repo.fail(row["id"], "w10-retry-worker", "adapter_failed", "模拟本地失败")

    again = job_repo.retry(job_repo.get(row["id"]), created_by=None)
    assert again["execution_mode"] == "local"
    assert again["provider_code"] == "fastmetal"
    assert again["billable_status"] == "not_billable"
    job_repo.request_cancel(again["id"])


def test_云端重试继承执行方式与计价(provider_state, clean_queue, fresh_project):
    pid = fresh_project("editor")
    row = job_repo.create({
        "project_id": pid, "capability": "text", "kind": "text",
        "execution_mode": "cloud", "provider_code": "deepseek",
        "model_code": "deepseek-flash", "adapter_version": "test",
        "prompt_snapshot": "云端失败样本", "params": {}, "input_refs": [],
        "quantity": 0.002, "unit": "million_token",
        "unit_price_micros": 8_000_000, "cost_micros": 16_000,
        "currency": "CNY", "billable_status": "pending_reconciliation",
        "pricing_snapshot": {"source": "测试"},
        "user_confirmed_cloud": True,
    })
    claimed = job_repo.claim_next("w10-cloud-retry", 60)
    assert claimed is not None and claimed["id"] == row["id"]
    job_repo.fail(row["id"], "w10-cloud-retry", "provider_auth", "模拟 401")

    again = job_repo.retry(job_repo.get(row["id"]), created_by=None)
    assert again["execution_mode"] == "cloud"
    assert again["provider_code"] == "deepseek"
    assert again["quantity"] == 0.002
    assert again["cost_micros"] == 16_000
    assert again["billable_status"] == "pending_reconciliation"
    assert again["provider_request_id"] is None
    job_repo.request_cancel(again["id"])


# ---------------------------------------------------------------- 取消异步任务（B12）
#
# 2026-11-10 裁决：原来点「取消」只改本地状态，供应商那边任务照跑照计费。
# 现在 worker 在 `except Cancelled` 里拿 task_id 去调供应商的 cancel 接口。
# 这一组钉住三种结论可区分——**尤其不能把"取消不了"说成"已取消"**。

def test_取消_wan_排队中的任务会真的发请求(monkeypatch, mock_api):
    """PENDING 的任务能取消：发出 POST .../cancel，且带 Bearer。"""
    import cloud_run
    monkeypatch.setenv("ALIYUN_WAN_BASE_URL", _base(mock_api))
    monkeypatch.setenv("DASHSCOPE_API_KEY", SECRET)

    out = cloud_run.cancel_task(provider="wan", request_id="a8532587-x",
                                key_env="DASHSCOPE_API_KEY")

    assert out["cancelled"] is True, out
    assert out["http_status"] == 200
    calls = [(m, p) for m, p, _h, _b in mock_api.calls]
    assert ("POST", "/api/v1/tasks/a8532587-x/cancel") in calls, calls
    headers = [h for m, p, h, _b in mock_api.calls if p.endswith("/cancel")][0]
    assert headers.get("Authorization") == f"Bearer {SECRET}"


def test_取消_任务已在跑时如实说取消不了(monkeypatch, mock_api):
    """400 UnsupportedOperation → `cancelled=False` 且 reason 是 `running`。

    这条是本组最要紧的一条：真实接口**只允许取消排队中的任务**。
    如果实现把 400 也当成"取消了"，界面上就会写"已撤销、不会再计费"，
    而钱其实正在花——那正是本轮在修的那类假话。
    """
    import cloud_run
    monkeypatch.setenv("ALIYUN_WAN_BASE_URL", _base(mock_api))
    monkeypatch.setenv("DASHSCOPE_API_KEY", SECRET)
    mock_api.scenario = "cancel_running"

    out = cloud_run.cancel_task(provider="wan", request_id="b1111111-y",
                                key_env="DASHSCOPE_API_KEY")

    assert out["cancelled"] is False, out
    assert out["reason"] == "running", (
        f"任务已经在跑却没被单独标出来，worker 分不清「取消不了」和「没配好」：{out}")
    assert out["http_status"] == 400


def test_取消_401_时把密钥抹掉(monkeypatch, mock_api):
    """供应商回显里带密钥片段 → 落盘结论里不能有它。"""
    import cloud_run
    monkeypatch.setenv("ALIYUN_WAN_BASE_URL", _base(mock_api))
    monkeypatch.setenv("DASHSCOPE_API_KEY", SECRET)
    mock_api.scenario = "cancel_401"

    out = cloud_run.cancel_task(provider="wan", request_id="c2222222-z",
                                key_env="DASHSCOPE_API_KEY")

    assert out["cancelled"] is False
    assert SECRET not in json.dumps(out, ensure_ascii=False), out


def test_取消_task_id_形状不合法就不发请求(monkeypatch, mock_api):
    """`task_id` 是供应商给的外部数据，会被拼进**带凭据的 URL**。

    `../../`、`@evil.com` 这类能改路径或改 host。白名单不过就一个请求都不发——
    "先发出去再说"在这里等于把 Bearer 送到别人家。
    """
    import cloud_run
    monkeypatch.setenv("ALIYUN_WAN_BASE_URL", _base(mock_api))
    monkeypatch.setenv("DASHSCOPE_API_KEY", SECRET)
    before = len(mock_api.calls)

    for bad in ("../../evil", "a@evil.com", "a/b", "x" * 200, ""):
        out = cloud_run.cancel_task(provider="wan", request_id=bad,
                                    key_env="DASHSCOPE_API_KEY")
        assert out["cancelled"] is False
        assert out["reason"] == "bad_task_id", (bad, out)
    assert len(mock_api.calls) == before, "形状不合法的 task_id 也发请求了"


def test_取消_没有_base_url_或密钥时明确说原因(monkeypatch):
    """缺配置要说清缺哪一样，不能笼统报"取消失败"。"""
    import cloud_run
    monkeypatch.delenv("ALIYUN_WAN_BASE_URL", raising=False)
    out = cloud_run.cancel_task(provider="wan", request_id="d3333333-a")
    assert out["cancelled"] is False and out["reason"] == "missing_base_url", out

    monkeypatch.setenv("ALIYUN_WAN_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    out = cloud_run.cancel_task(provider="wan", request_id="d3333333-a")
    assert out["cancelled"] is False and out["reason"] == "missing_key", out


def test_取消_对同步的_deepseek_说没有这回事(monkeypatch):
    """deepseek 是同步调用，没有异步任务可取消。如实说，不假装成功。"""
    import cloud_run
    out = cloud_run.cancel_task(provider="deepseek", request_id="whatever")
    assert out["cancelled"] is False
    assert out["reason"] == "unsupported_provider", out
