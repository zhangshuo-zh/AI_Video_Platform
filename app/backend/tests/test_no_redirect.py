"""出站请求**不跟随 3xx**：2026-11-09 体检查实的缺口（B1）。

## 缺的是什么

`safenet.py` 早在 2026-11-08 那轮就把正确做法写好了——`NoRedirect` ＋
`opener_without_redirects()`，docstring 也把理由写得很清楚："`urlopen` 默认
跟随 3xx，一次 302 就能绕过任何'只看第一跳'的校验"。但**只有两个地方用上了**：
`cloud_run._download`（外部应答给的 video_url）与 `cloud_probe._probe_now`。

真正带着 `Authorization` 头出门的六处却都还是裸 `urlopen`：

    cloud_run._post_json        deepseek chat / wan 建任务   带 Bearer
    cloud_run._get_json         wan 轮询                      带 Bearer
    engines._get_info_for       引擎自述端点                   通常无凭据
    engine_http_run（两处）      本机引擎出图 / 回读 options    可带 Bearer
    llm_http_run                本机 LLM 一次 chat            可带 Bearer

## 为什么这条是安全项而不是整洁项

`urlopen` 跟随 3xx 时会**把原请求头一起带到新地址**（`urllib` 的
`redirect_request` 保留 headers 里除 Content-Length/Content-Type 之外的项）。
于是"供应商端点被劫持／DNS 投毒／管理员把 base URL 填到一台恶意主机"这一个
条件，就足以让 `Authorization: Bearer <真密钥>` 被重发到攻击者域——攻击者
不必破解任何东西，只要回一个 `302`。这正是 `safenet` 模块头写的威胁模型里
"被劫持的供应商应答"那一档，只是当时只堵了下载那条路。

## 这条测试为什么必须用真 socket

不用 monkeypatch 假 urlopen：那样测的是"我换了这个函数"，而不是"3xx 会不会
被跟随"。真起一个 `HTTPServer` 回 302，指向另一个真端口，然后断言：
**第二台服务一次请求都没收到**。这样即使将来有人把实现换掉，判据仍然成立。

## 反向验证（本轮做过）

把六处任一处改回裸 `urlopen`，对应用例立刻红（第二台服务收到请求，
且 `Authorization` 头在里面）。六个调用点各自的用例都做了这个反向跑。
"""
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import safenet

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class _Sink(BaseHTTPRequestHandler):
    """被重定向指向的那台"攻击者服务器"：记下收到的一切，回一个正常 JSON。"""

    def do_GET(self):
        self.server.hits.append({"path": self.path,
                                 "auth": self.headers.get("Authorization")})
        self._json(200, {"message": {"content": "pwned"}, "model": "x"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length) if length else None
        self.server.hits.append({"path": self.path,
                                 "auth": self.headers.get("Authorization")})
        self._json(200, {"choices": [{"message": {"content": "pwned"}}],
                         "id": "sink", "model": "x"})

    def _json(self, code, payload):
        body = __import__("json").dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.hits = []
    srv.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def redirect_pair():
    """一台回 302 指向另一台。返回 (重定向端, 目标端, 目标端端口)。"""
    sink = _serve(_Sink)

    class _Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.port}/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length) if length else None
            self.send_response(307)          # 307 会保留方法与正文
            self.send_header("Location", f"http://127.0.0.1:{sink.port}/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    origin = _serve(_Redirector)
    try:
        yield origin, sink
    finally:
        for srv in (origin, sink):
            srv.shutdown()
            srv.server_close()


# ---------- 第一层：工具本身 ----------

def test_opener_without_redirects_收到_302_抛_UnsafeTarget(redirect_pair):
    """判据：不是"拿到 302 状态码"，而是"根本没有第二跳"。"""
    from urllib.request import Request
    origin, sink = redirect_pair
    opener = safenet.opener_without_redirects()
    with pytest.raises(safenet.UnsafeTarget):
        opener.open(Request(f"http://127.0.0.1:{origin.port}/x"), timeout=5)
    assert sink.hits == [], "不跟随重定向时，目标端一次请求都不该收到"


def test_裸_urlopen_确实会跟过去_证明这条风险是真的(redirect_pair):
    """反向对照：不加固的那一版会把 Authorization 一起带过去。

    这一条**故意测"坏行为"**，因为它就是本轮要证明的风险本身。
    如果哪天 urllib 改了默认不跟随重定向，这条会红——那时说明风险消失，
    可以把六处加固改成一条备注而不是继续防着。
    """
    from urllib.request import Request, urlopen
    origin, sink = redirect_pair
    req = Request(f"http://127.0.0.1:{origin.port}/x",
                  headers={"Authorization": "Bearer sk-supersecret"})
    with urlopen(req, timeout=5) as resp:
        resp.read()
    assert len(sink.hits) == 1, "裸 urlopen 应当跟随了 302"
    assert sink.hits[0]["auth"] == "Bearer sk-supersecret", (
        "跟随重定向时 Authorization 被原样带到了另一个域——这就是要堵的口子")


# ---------- 第二层：六个真实调用点 ----------

def test_cloud_run_post_json_不跟随重定向(redirect_pair, monkeypatch):
    """deepseek chat 那条路：带 Bearer 的 POST 被 307 带走就等于交密钥。"""
    import cloud_run
    origin, sink = redirect_pair
    monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{origin.port}")
    with pytest.raises(safenet.UnsafeTarget):
        cloud_run._post_json(f"http://127.0.0.1:{origin.port}/chat/completions",
                            {"model": "x"}, {"Authorization": "Bearer sk-supersecret"}, 5)
    assert sink.hits == [], "重定向目标端收到了请求（且可能带着 Authorization）"


def test_cloud_run_get_json_不跟随重定向(redirect_pair):
    """wan 轮询那条路，同样是带 Bearer 的 GET。"""
    import cloud_run
    origin, sink = redirect_pair
    with pytest.raises(safenet.UnsafeTarget):
        cloud_run._get_json(f"http://127.0.0.1:{origin.port}/api/v1/tasks/1",
                            {"Authorization": "Bearer sk-supersecret"}, 5)
    assert sink.hits == []


def test_engines_探测不跟随重定向(redirect_pair):
    """引擎自述端点：地址是人填的，被指去别处就是内网探测的入口。"""
    import engines
    origin, sink = redirect_pair
    out = engines._get_info_for(f"http://127.0.0.1:{origin.port}", "draw_things", 5)
    assert sink.hits == []
    assert "error" in out, "被重定向拦下应当是一条明确失败，不是静默成功"
    assert "重定向" in out["error"] or "安全边界" in out["error"], out["error"]


def _run_script(script: str, request: dict, tmp_path: Path) -> tuple[int, Path]:
    """按**真实入口**跑一个 run 脚本：它们是子进程程序，只有 main()。

    不 import 调内部函数——那样测不到 sys.argv 那条路，而这条路的失败形式
    （把未捕获异常打到 stderr、回执不写）正是用户会看到的。
    """
    import json as _json
    import subprocess
    req_file = tmp_path / "request.json"
    req_file.write_text(_json.dumps(request, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(BACKEND / script), str(req_file)],
                          capture_output=True, text=True, timeout=60,
                          cwd=str(BACKEND))
    return proc.returncode, req_file


def test_llm_http_run_不跟随重定向(redirect_pair, tmp_path, monkeypatch):
    """本机 LLM 一次 chat：可带 sharedSecret，同样不该被 307 带走。"""
    origin, sink = redirect_pair
    monkeypatch.setenv("FV_TEST_SECRET", "sk-supersecret")
    receipt = tmp_path / "receipt.json"
    code, _ = _run_script("llm_http_run.py", {
        "endpoint": f"http://127.0.0.1:{origin.port}", "model": "x",
        "prompt": "hello", "timeout": 5,
        "receipt": str(receipt), "output": str(tmp_path / "out.txt"),
        "secret_env": "FV_TEST_SECRET",
    }, tmp_path)
    assert sink.hits == [], "重定向目标端收到了请求（可能带着 sharedSecret）"
    assert code != 0, "被重定向拦下应当以非零退出"
    body = receipt.read_text(encoding="utf-8")
    assert "重定向" in body or "安全边界" in body, body


def test_engine_http_run_不跟随重定向(redirect_pair, tmp_path, monkeypatch):
    """本机引擎出图那条路。"""
    origin, sink = redirect_pair
    monkeypatch.setenv("FV_TEST_SECRET", "sk-supersecret")
    receipt = tmp_path / "receipt.json"
    code, _ = _run_script("engine_http_run.py", {
        "endpoint": f"http://127.0.0.1:{origin.port}", "path": "/sdapi/v1/txt2img",
        "body": {"prompt": "x"}, "timeout": 5,
        "receipt": str(receipt), "output": str(tmp_path / "out.png"),
        "secret_env": "FV_TEST_SECRET",
    }, tmp_path)
    assert sink.hits == [], "重定向目标端收到了请求（可能带着 sharedSecret）"
    assert code != 0, "被重定向拦下应当以非零退出"
    body = receipt.read_text(encoding="utf-8")
    assert "重定向" in body or "安全边界" in body, body
