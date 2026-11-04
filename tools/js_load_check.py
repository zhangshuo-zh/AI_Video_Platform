#!/usr/bin/env python3
"""前端语法自检：把 js/ 下每个模块都在真浏览器里 import 一遍，报解析错。

为什么需要这一件工具：这台机器上**没有 node**，改完 JS 没有任何本地解析器能告诉我
"这一行写坏了"。而模板字符串被拆断这种错（`${…}` 掉在字面量外面）浏览器只会在
控制台留一句 `Uncaught SyntaxError: Unexpected token '{'`，不带文件名——
本轮就在 writing.js 的 jobCell 上撞到过一次，19 条浏览器验收当场只剩 3 条通过，
却看不出是哪个文件。

所以这里不靠页面跑起来：直接 `import(url)` 每一个模块，把**带文件名**的失败收回来。
模块之间互相 import，所以一个文件坏了会连累它的引用方——每个文件都单独取一次
query 参数不同的 URL，绕开浏览器的模块缓存，谁坏报谁。

用法：python3 tools/js_load_check.py            # 只查语法与导入
      python3 tools/js_load_check.py -v        # 连每个模块的导出名一起列
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "app" / "frontend"
sys.path.insert(0, str(Path(__file__).resolve().parent))

PORT = 8051
BASE = f"http://127.0.0.1:{PORT}"


def serve_frontend() -> ThreadingHTTPServer:
    handler = partial(SimpleHTTPRequestHandler, directory=str(FRONTEND))
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def launch_chrome(profile: Path) -> subprocess.Popen:
    return subprocess.Popen(
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
         "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", f"--user-data-dir={profile}",
         f"--remote-debugging-port={PORT + 1}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cdp_target_url() -> str:
    import urllib.request

    for _ in range(60):
        try:
            data = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{PORT + 1}/json").read())
            for tab in data:
                if tab.get("type") == "page":
                    return tab["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.2)
    raise SystemExit("拿不到 Chrome 的调试端口")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    import tempfile

    from proto_cdp_check import CDP

    files = sorted(str(p.relative_to(FRONTEND)).replace("\\", "/")
                   for p in (FRONTEND / "js").rglob("*.js"))
    httpd = serve_frontend()
    profile = Path(tempfile.mkdtemp(prefix="jscheck-"))
    chrome = launch_chrome(profile)
    try:
        cdp = CDP(cdp_target_url())
        cdp.cmd("Page.enable")
        cdp.cmd("Runtime.enable")
        # 先落到同源的一个真实页面上：about:blank 里 import 相对 URL 会被 CORS 挡
        cdp.cmd("Page.navigate", url=f"{BASE}/index.html")
        time.sleep(1.5)
        bad, exports = [], {}
        for i, rel in enumerate(files):
            # 每个文件带一个不同的 ?v=：浏览器按 URL 缓存模块，
            # 不去掉缓存的话，一个坏文件会把它的所有引用方一起报成坏
            expr = (f"(() => import({json.dumps(BASE + '/' + rel + '?v=' + str(i))})"
                    f".then((m) => Object.keys(m).sort().join(',') || '(无导出)')"
                    f".catch((e) => 'LOAD-FAIL: ' + e.message))()")
            try:
                value = cdp.eval(expr)
            except RuntimeError as exc:          # evaluate 本身没回来／连接断了
                value = f"EVAL-FAIL: {exc}"
            if isinstance(value, str) and value.startswith(("LOAD-FAIL", "EVAL-FAIL")):
                bad.append((rel, value))
            else:
                exports[rel] = value
        print(f"扫了 {len(files)} 个模块，失败 {len(bad)} 个")
        for rel, err in bad:
            print(f"  FAIL {rel}  ·  {err}")
        if args.verbose:
            for rel, names in exports.items():
                print(f"  ok   {rel}  →  {names[:96]}")
        return 1 if bad else 0
    finally:
        chrome.terminate()
        httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main())
