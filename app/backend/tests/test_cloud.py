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
                                             provider_state, clean_queue, fresh_project):
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


def test_云端401失败也留请求ID(tmp_path, monkeypatch, mock_api,
                              provider_state, clean_queue, fresh_project):
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(mock_api))
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    mock_api.scenario = "deepseek_401"

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
