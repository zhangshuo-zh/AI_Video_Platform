"""云端显式探测（`cloud_probe.py`）：只打本地 mock HTTP，绝不打真供应商。

纪律与 test_cloud.py 同源：monkeypatch base URL 到 127.0.0.1 的测试服务，
把"没配密钥 / 钥匙被拒 / 路通钥匙对 / 连不上 / 无可探端点"五档跑穿。
真实网关的鉴权行为（鉴权先于路由、401 回显密钥尾巴）实测记在模块头与
盘点文档，不塞进 pytest。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import cloud_probe
import engines
from adapters import find

SECRET = "sk-cloudprobe-secret-9999"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _send(self, status, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        auth = self.headers.get("Authorization") or ""
        self.server.calls.append((self.path, auth))
        scenario = self.server.scenario
        if self.path.endswith("/models"):
            if scenario == "ok" and auth == f"Bearer {SECRET}":
                self._send(200, json.dumps({"data": [{"id": "deepseek-flash"}]}).encode())
                return
            if scenario == "reject":
                # 真 DeepSeek 的 401 应答会回显打码后的密钥尾巴——照抄这个形状，
                # 验证 detail 里的脱敏不是摆设。
                self._send(401, json.dumps({"error": {
                    "message": f"Authentication Fails, Your api key: ****{SECRET[-4:]} is invalid",
                    "code": "invalid_api_key"}}).encode())
                return
            self._send(401, json.dumps({"error": {"message": "No API-key provided."}}).encode())
            return
        if self.path.startswith("/api/v1/tasks/"):
            if scenario == "wan_auth_ok" and auth == f"Bearer {SECRET}":
                # 钥匙对了才会走到"任务不存在"——鉴权先于查档，实测口径。
                self._send(404, json.dumps({"code": "TaskNotFound"}).encode())
                return
            self._send(401, json.dumps({"code": "InvalidApiKey",
                                       "message": "Invalid API-key provided."}).encode())
            return
        self._send(404, b"{}")


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.calls = []
    srv.scenario = "ok"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)
    srv.server_close()


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    cloud_probe.forget_cache()
    engines.forget_probes()
    yield
    cloud_probe.forget_cache()


def _base(server):
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_没配密钥不发请求(server, monkeypatch):
    """纪律 2：密钥变量没值＝现场回答配置事实，一个字节都不出网。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(server))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    row = cloud_probe.probe_one(find("text", "cloud", "deepseek"))
    assert row["state"] == "no_key"
    assert "DEEPSEEK_API_KEY" in row["detail"]
    assert server.calls == [], "没配密钥不许发 HTTP 请求"


def test_钥匙被拒和连不上是分开的两档(server, monkeypatch):
    """401：路是通的，钥匙不对——detail 不许冒充成网络故障。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(server))
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    server.scenario = "reject"
    row = cloud_probe.probe_one(find("text", "cloud", "deepseek"), fresh=True)
    assert row["state"] == "auth_rejected"
    assert "路通" in row["detail"]


def test_脱敏先于一切出口(server, monkeypatch):
    """供应商 401 回显密钥尾巴；detail 里不许出现密钥值，哪怕打码前的原文。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(server))
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    server.scenario = "reject"
    row = cloud_probe.probe_one(find("text", "cloud", "deepseek"), fresh=True)
    assert SECRET not in row["detail"]
    # 应答体里只有供应商自己打码的尾巴（****9999）——和执行链日志同一口径：
    # _redact 抹的是密钥值，打码尾巴保留下来正好帮人认出"是哪把钥匙"。
    assert "****" in row["detail"]


def test_钥匙对了就报路通_即便回执是404(server, monkeypatch):
    """wan 的探路按假任务 id：404 恰恰证明鉴权过了闸（实测先于查档）。"""
    monkeypatch.setenv("ALIYUN_WAN_BASE_URL", _base(server))
    monkeypatch.setenv("DASHSCOPE_API_KEY", SECRET)
    server.scenario = "wan_auth_ok"
    row = cloud_probe.probe_one(find("video", "cloud", "wan"), fresh=True)
    assert row["state"] == "ok"
    assert "假任务 id" in row["detail"]


def test_路通钥匙对(server, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(server))
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    server.scenario = "ok"
    row = cloud_probe.probe_one(find("text", "cloud", "deepseek"), fresh=True)
    assert row["state"] == "ok"
    assert "HTTP 200" in row["detail"]
    # 结论口径：不许出现"可用"这种跳档的话
    assert "可用" not in row["detail"]


def test_连不上是不缓存的瞬时状态(server, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    row = cloud_probe.probe_one(find("text", "cloud", "deepseek"), fresh=True)
    assert row["state"] == "unreachable"
    assert row["cached"] is False
    assert cloud_probe._CACHE == {}, "网络层失败不许进缓存"


def test_ttl_内复用缓存_现查绕过(server, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(server))
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    server.scenario = "ok"
    cloud_probe.probe_one(find("text", "cloud", "deepseek"), fresh=True)
    n = len(server.calls)
    cloud_probe.probe_one(find("text", "cloud", "deepseek"))          # 缓存命中
    assert len(server.calls) == n, "TTL 内不该为同一台网关问第二遍"
    cloud_probe.probe_one(find("text", "cloud", "deepseek"), fresh=True)
    assert len(server.calls) == n + 1, "fresh=1 必须绕过缓存"


def test_未核验供应商如实报无可探端点(monkeypatch):
    """volc/minimax 这些候选名没有登记端点：不发请求、不编结论。"""
    monkeypatch.setenv("FV_KEY_TEXT_VOLC", SECRET)
    row = cloud_probe.probe_one(find("text", "cloud", "volc"), fresh=True)
    assert row["state"] == "no_endpoint"
    assert "未核验" in row["detail"]


def test_wan_没配_workspace_地址就是没得探(monkeypatch):
    monkeypatch.delenv("ALIYUN_WAN_BASE_URL", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", SECRET)
    row = cloud_probe.probe_one(find("video", "cloud", "wan"), fresh=True)
    assert row["state"] == "no_endpoint"
    assert "ALIYUN_WAN_BASE_URL" in row["detail"]


def test_地址闸拦住的不发请求(monkeypatch):
    """base URL 是配置输入：元数据地址这种即使在环境变量里也不许碰。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://169.254.169.254")
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    row = cloud_probe.probe_one(find("text", "cloud", "deepseek"), fresh=True)
    assert row["state"] == "unreachable"
    assert "安全边界" in row["detail"]


def test_probe_registered_只探登记过的云端轨(server, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", _base(server))
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    server.scenario = "ok"
    rows = [
        {"capability": "text", "backend": "cloud", "provider": "deepseek"},
        {"capability": "text", "backend": "cloud", "provider": "deepseek"},  # 重复登记
        {"capability": "image", "backend": "local", "provider": "draw_things"},
    ]
    out = cloud_probe.probe_registered(rows, fresh=True)
    assert [r["provider"] for r in out] == ["deepseek"], "同家去重、本地轨跳过"
    assert len(server.calls) == 1
