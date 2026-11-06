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
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "app" / "frontend"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H

PORT = H.free_port()
DEBUG_PORT = H.free_port()
while DEBUG_PORT == PORT:
    DEBUG_PORT = H.free_port()
BASE = f"http://127.0.0.1:{PORT}"


def serve_frontend():
    return H.serve_static(FRONTEND, PORT)[0]


def launch_chrome(profile: Path):
    return H.launch_chrome(DEBUG_PORT, profile)


def cdp_target_url() -> str:
    return H.wait_cdp(DEBUG_PORT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    from proto_cdp_check import CDP

    files = sorted(str(p.relative_to(FRONTEND)).replace("\\", "/")
                   for p in (FRONTEND / "js").rglob("*.js"))
    httpd = serve_frontend()
    profile = H.temp_dir("jscheck-")
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
