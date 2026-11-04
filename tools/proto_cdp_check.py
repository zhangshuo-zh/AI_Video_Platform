#!/usr/bin/env python3
"""最小 CDP 客户端：只用标准库，验证原型的点击交互真的能跑。

为什么不用现成库：本机没有 node，也没装 websocket 包；
原型是纯静态页，用 socket 手写一帧 WebSocket 收发就够用了。

用法：python3 tools/proto_cdp_check.py <页面 file:// URL>
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# 端口跟着进程号走：写死一个端口时，上一轮被中断留下的 Chrome 会接住这条连接，
# 于是新脚本连到的是别人的空页面——报出来的全是假失败
PORT = 9300 + (os.getpid() % 60)


# ---------------- WebSocket（够发文本帧、收文本帧就行） ----------------

def ws_connect(url):
    """url 形如 ws://127.0.0.1:9333/devtools/page/XXXX"""
    host_port, path = url[len("ws://"):].split("/", 1)
    host, port = host_port.split(":")
    s = socket.create_connection((host, int(port)))
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET /{path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
           f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    # 握手响应后面可能已经跟着第一帧，丢掉就会少一条消息——留给调用方
    head, rest = buf.split(b"\r\n\r\n", 1)
    if b" 101" not in head.split(b"\r\n", 1)[0]:
        raise RuntimeError(f"握手失败：{head[:80]!r}")
    return s, rest


def ws_send(s, payload):
    data = payload.encode()
    mask = os.urandom(4)
    header = bytearray([0x81])                      # FIN + text
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126); header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127); header += struct.pack(">Q", n)
    header += mask
    s.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _recv_exact(s, n, buf):
    while len(buf) < n:
        chunk = s.recv(65536)
        if not chunk:
            raise EOFError("socket closed")
        buf += chunk
    return buf


def ws_recv(s, buf=b""):
    """返回 (文本, 剩余字节, opcode)。

    两个坑都踩过一次，所以这里写死：
    1. **头长与体长要分开算**。以前写成"读够 ln 字节"，而 buf 里已经躺着
       2—10 字节的头，于是每条长消息都被截掉开头几十字节，
       剩下的几字节又被当成下一帧的头解析——表现为永远等不到的超时。
    2. **分片帧要拼回去**。CDP 的大响应（整份 openapi、长 innerText）会被拆成
       FIN=0 的续帧；上一版只认首帧、把 opcode 0 的续帧当垃圾丢掉，
       于是解析器从中间开始读字节，同样卡死。
    """
    frames = bytearray()
    first_opcode = None
    while True:
        buf = _recv_exact(s, 2, buf)
        fin = buf[0] & 0x80
        opcode = buf[0] & 0x0F
        ln = buf[1] & 0x7F
        head = 2
        if ln == 126:
            buf = _recv_exact(s, head + 2, buf)
            ln = struct.unpack(">H", buf[head:head + 2])[0]
            head += 2
        elif ln == 127:
            buf = _recv_exact(s, head + 8, buf)
            ln = struct.unpack(">Q", buf[head:head + 8])[0]
            head += 8
        buf = _recv_exact(s, head + ln, buf)
        if opcode == 1 or opcode == 0:          # 文本帧与它的续帧
            if first_opcode is None:
                first_opcode = opcode
            frames += buf[head:head + ln]
        if fin:
            break
    return bytes(frames).decode("utf-8", "replace"), buf[head + ln:], (first_opcode or 1)


class CDP:
    def __init__(self, url):
        self.s, self.buf = ws_connect(url)
        # 必须有超时：没有它就会在"等一条永远不来的回复"上挂死整个评审
        self.s.settimeout(20)
        self.id = 0
        # 页面里 alert()/confirm() 会把渲染进程整个挡住，Runtime.evaluate 就永远不回来。
        # 平台的 util.run() 失败时正是走 alert，所以这里遇到就顺手收掉并把文本留下。
        self.dialogs: list[str] = []

    def cmd(self, method, **params):
        self.id += 1
        mine = self.id
        ws_send(self.s, json.dumps({"id": mine, "method": method, "params": params}))
        while True:
            try:
                text, self.buf, opcode = ws_recv(self.s, self.buf)
            except (socket.timeout, TimeoutError):
                raise RuntimeError(f"{method} 等回复超时（id={mine}）")
            except OSError as e:
                raise RuntimeError(f"{method} 连接断了：{e}")
            if opcode != 1 or not text.strip():
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue                    # 分片帧的半截 JSON，丢掉等下一帧
            if msg.get("method") == "Page.javascriptDialogOpening":
                self.dialogs.append(msg["params"].get("message", ""))
                self.id += 1
                ws_send(self.s, json.dumps({
                    "id": self.id, "method": "Page.handleJavaScriptDialog",
                    "params": {"accept": True}}))
                continue
            if msg.get("id") == mine:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr):
        r = self.cmd("Runtime.evaluate", expression=expr,
                     returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(json.dumps(r["exceptionDetails"], ensure_ascii=False)[:400])
        return r["result"].get("value")


def main():
    page = sys.argv[1]
    profile = f"/tmp/proto_cdp_profile_{PORT}"
    subprocess.run(["rm", "-rf", profile], check=False)
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--user-data-dir={profile}", f"--remote-debugging-port={PORT}",
         "--window-size=1440,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = wait_for_target()
        cdp = CDP(target)
        cdp.cmd("Page.enable")
        cdp.cmd("Runtime.enable")
        cdp.cmd("Log.enable")
        cdp.cmd("Page.navigate", url=page)
        time.sleep(1.8)
        # 先确认这条连接真的连着原型页：上一轮就是连到了别人留下的 about:blank，
        # 于是每条断言都在空页面上跑，报出来的全是假失败
        # （中文路径会被百分号编码，比较前先 unquote）
        href = urllib.parse.unquote(cdp.eval("location.href") or "")
        if "模型管理原型" not in href:
            raise RuntimeError(f"页面没连上，当前是：{href}")
        run_checks(cdp)
    finally:
        proc.terminate()


def wait_for_target():
    last = ""
    for _ in range(60):
        try:
            data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
            pages = [t for t in data if t.get("type") == "page"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except Exception as e:                      # Chrome 还没起监听
            last = str(e)
        time.sleep(0.25)
    raise RuntimeError(f"拿不到调试目标：{last}")


def click(cdp, sel):
    """页面内 .click()：browser-use 那种"假 success"在这里不存在，
    点没点中由后面的断言说了算。
    选择器里的属性值一律用单引号——省掉 JS 字符串再套一层转义。"""
    return cdp.eval(f"""(() => {{
      const el = document.querySelector({json.dumps(sel)});
      if (!el) return 'MISSING:' + {json.dumps(sel)};
      el.click(); return 'clicked';
    }})()""")


def eval_js(cdp, expr):
    """把裸表达式包成"取不到就返回 null"的形式：
    上一轮就是因为在 about:blank 上对 null 取 .value，直接抛 TypeError 把整轮打断。"""
    return cdp.eval(f"(() => {{ try {{ return ({expr}); }} catch (e) {{ return 'ERR:' + e.message; }} }})()")


def has(cdp, sel):
    return cdp.eval(f"!!document.querySelector({json.dumps(sel)})")


def text_of(cdp, sel, default="-"):
    return cdp.eval(f"(() => {{ const e = document.querySelector({json.dumps(sel)});"
                    f"return e ? e.innerText : {json.dumps(default)}; }})()")


def body_has(cdp, needle):
    return cdp.eval(f"document.body.innerText.includes({json.dumps(needle)})")


def run_checks(cdp):
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(("  OK  " if ok else "  FAIL") + f" {name}", flush=True)

    check("首屏是能力轨", has(cdp, ".pm-card"),
          f"{cdp.eval('document.querySelectorAll(\".pm-card\").length')} 张能力卡")

    # 1) 点「轨」那一格 → 详情带出现
    print("   点击结果：", click(cdp, "tr.track[data-id='5'] td:first-child"))
    check("点行首格展开详情带",
          eval_js(cdp, "(() => { const d = document.querySelector("
                   "\"tr.pm-detail[data-detail-for='5']\"); return !!d && !d.hidden; })()"))
    check("详情带里有验证动作按钮", has(cdp, "tr.pm-detail [data-env-check='5']"))

    # 2) 未配置轨：点「去配置」→ 变成可选的一行
    print("   点击结果：", click(cdp, "[data-open-empty='3']"))
    check("去配置把折叠行展开成可编辑行",
          has(cdp, "tr.track[data-id='3'] select[data-f='provider']"))

    # 3) 改一个下拉 → 脏提示出现、保存按钮解禁
    eval_js(cdp, "(() => { const sel = document.querySelector("
             "\"tr.track[data-id='5'] select[data-f='model']\");"
             "sel.value = 'fastmetal-5b-qad';"
             "sel.dispatchEvent(new Event('change', {bubbles: true})); return 'ok'; })()")
    check("改动后保存按钮解禁",
          eval_js(cdp, "!document.querySelector('#save-providers').disabled"),
          text_of(cdp, "#dirty-chip"))

    # 4) 切到模型清单
    print("   点击结果：", click(cdp, ".nav-sub .sub[data-sub='1']"))
    check("切到模型清单", has(cdp, ".pm-tier"),
          f"{cdp.eval('document.querySelectorAll(\".pm-tier\").length')} 个模型档")
    check("清单页写明只读与版本",
          body_has(cdp, "adapters.py") and body_has(cdp, "2026-11-01.2"))

    # 5) 从清单跳回能力轨并高亮
    print("   点击结果：", click(cdp, "[data-goto-slot='video|local']"))
    check("清单跳回能力轨", has(cdp, ".pm-card"))
    check("目标行被高亮", has(cdp, "tr.pm-hit"), text_of(cdp, "tr.pm-hit td:first-child"))

    # 6) 运行时与权重页
    print("   点击结果：", click(cdp, ".nav-sub .sub[data-sub='2']"))
    check("运行时页有真实路径", body_has(cdp, "/Users/zhangshuo/FastVideo"))
    check("运行时页标了需要新接口", body_has(cdp, "GET /api/runtime"))

    # 7) 切主题：断言"变了"而不是"变成白天"——初值由本机小时决定，
    #    上午跑就是白天档，写死等 day 会假失败
    before = cdp.eval("document.documentElement.dataset.theme")
    click(cdp, "#theme-toggle")
    after = cdp.eval("document.documentElement.dataset.theme")
    check("主题可切换且切到另一套", after != before and after in ("day", "night"),
          f"{before} → {after}")
    click(cdp, "#theme-toggle")

    bad = [r for r in results if not r[1]]
    for name, ok, detail in bad:
        print(f"  未过：{name}  · {detail}")
    print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
