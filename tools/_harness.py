"""验收／取证脚本的公共底座：临时目录、空闲端口、静态服务、无头 Chrome、CDP。

为什么要有它：`tools/` 下十几个脚本各自抄过同一段「起临时库 + 起端口 +
起无头 Chrome + 等它 ready」。抄的代价不是多几行，而是三处会各自漂：
端口写死（8012 与 `production_scenario_check.py` 撞车）、Chrome 路径写死
macOS 的 `.app`（CI 的 ubuntu runner 直接跑不了）、临时目录写死
`/tmp/<name>`（容器里可能删不掉，本机重启后还留着上一轮的库）。

边界：这里只管「把进程／端口／浏览器准备好」，不碰任何业务断言。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "app" / "backend"
FRONTEND = ROOT / "app" / "frontend"


def free_port() -> int:
    """让内核分配空闲端口：写死端口会撞上上一轮残留的服务或 Chrome。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def chrome_binary() -> str:
    """先认 CHROME_BIN，再看常见安装位置；找不到就明确说怎么办。"""
    env = os.environ.get("CHROME_BIN")
    if env:
        return env
    for path in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if Path(path).exists():
            return path
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("找不到 Chrome/Chromium；装一个，或用 CHROME_BIN 指向可执行文件")


def temp_dir(prefix: str = "fv-check-") -> Path:
    """建一个本次运行独占的临时目录。

    `FASTVIDEO_CHECK_TMP` 可把它指到工作区内（沙箱里 `/tmp` 的删除可能被拦）；
    不设就退回系统临时目录。目录名带随机后缀，不会和上一轮撞。
    """
    base = os.environ.get("FASTVIDEO_CHECK_TMP")
    if base:
        Path(base).mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=prefix, dir=base))
    return Path(tempfile.mkdtemp(prefix=prefix))


def serve_static(root: Path = FRONTEND, port: int | None = None
                 ) -> tuple[ThreadingHTTPServer, int]:
    """起一个只读静态服务；返回 (httpd, port)，调用方负责 shutdown。"""
    port = port or free_port()
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def launch_chrome(debug_port: int, profile: Path | None = None,
                  window: str = "1440,1000") -> subprocess.Popen:
    profile = profile or temp_dir("fv-chrome-")
    return subprocess.Popen(
        [chrome_binary(), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={profile}", f"--remote-debugging-port={debug_port}",
         f"--window-size={window}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_http(url: str, timeout: float = 30.0, expect: int = 200) -> None:
    """轮询一个 HTTP 端点直到它给出 `expect`；超时就明确点名，不静默继续。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == expect:
                    return
        except Exception:
            time.sleep(0.25)
    raise SystemExit(f"{url} 在 {timeout:.0f}s 内没起来")


def wait_cdp(debug_port: int, timeout: float = 30.0) -> str:
    """等 Chrome 的调试端口，返回第一个 page 的 WebSocket URL。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{debug_port}/json", timeout=1).read())
            for tab in data:
                if tab.get("type") == "page":
                    return tab["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"Chrome 调试端口 {debug_port} 没有可用页面")


def start_service(port: int, python: Path | None = None,
                  backend: Path = BACKEND) -> subprocess.Popen:
    """用后端自己的 venv 起 uvicorn；日志丢掉，断言看接口与页面。"""
    python = python or backend / ".venv" / "bin" / "python"
    return subprocess.Popen(
        [str(python), "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(backend), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def stop(proc, grace: float = 5.0) -> None:
    """先 SIGTERM，等不到再 SIGKILL。所有脚本的 finally 都用这一个。"""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
