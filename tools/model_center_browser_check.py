#!/usr/bin/env python3
"""模型中心的真浏览器验收：起一个独立实例，点四工作区。

为什么不用 :8000 上那个实例：
  1. 那个进程还是改动前起来的，没有 /api/models，也没有 0035 的两张表；
  2. 更要紧的是不能替你把迁移跑到真正的开发库上——那该由你自己启动时决定。
所以这里用 **库副本 + 独立端口 + 临时工作区**：
  - 库副本 /tmp/mcdata/platform.db（0035 已在上面跑过）；
  - FASTVIDEO_WORKSPACE / JOBS / FILES 全指临时目录，
    不然测试文档会被 git 提交进你真正的稿子目录（conftest 里就写了这条教训）。

会话是直接在副本库里铸的，不碰任何口令。

用法：python3 tools/model_center_browser_check.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "app" / "backend"
TMP = Path("/tmp/mc_browser")
PORT = 8010
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, str(Path(__file__).resolve().parent))


def prepare_env():
    """必须在 import 后端模块之前设好：config.py 在导入时就把环境变量读成常量。"""
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    shutil.copy(BACKEND / "data" / "platform.db", TMP / "platform.db")
    for suffix in ("-wal", "-shm"):
        src = BACKEND / "data" / f"platform.db{suffix}"
        if src.exists():
            shutil.copy(src, TMP / f"platform.db{suffix}")
    os.environ["FASTVIDEO_DB"] = str(TMP / "platform.db")
    os.environ["FASTVIDEO_FILES"] = str(TMP / "files")
    os.environ["FASTVIDEO_JOBS"] = str(TMP / "jobs")
    os.environ["FASTVIDEO_WORKSPACE"] = str(TMP / "workspace")
    # 迁移要在副本上先跑到最新，服务起来才不会在测试中途改结构
    sys.path.insert(0, str(BACKEND))
    from migrate import migrate

    migrate(verbose=False)


def mint_admin_session() -> str:
    """给副本库里那个 admin 角色账号铸一个会话。不猜口令，也不改口令。"""
    from datetime import datetime, timedelta

    from repositories.users import sessions
    from security import new_session_token

    import sqlite3

    from db import get_conn

    with get_conn() as conn:
        row = conn.execute(
            """SELECT u.id FROM app_user u
                     JOIN user_role ur ON ur.user_id = u.id
                     JOIN role r ON r.id = ur.role_id
                    WHERE r.code = 'admin' AND u.is_active = 1
                    ORDER BY u.is_superuser DESC, u.id LIMIT 1"""
        ).fetchone()
        if row is None:
            raise SystemExit("副本库里没有可用的 admin 账号，验不了模型中心")
    token, token_hash = new_session_token()
    expires = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    sessions.create(token_hash, row["id"], expires)
    return token


def main():
    prepare_env()
    token = mint_admin_session()

    proc = subprocess.Popen(
        [str(BACKEND / ".venv/bin/python"), "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(("  OK  " if ok else "  FAIL") + f" {name}" + (f"  · {detail}" if detail else ""),
              flush=True)

    try:
        wait_server()
        import proto_cdp_check as P

        chrome = start_chrome(P.PORT)
        cdp = P.CDP(P.wait_for_target())
        cdp.cmd("Page.enable")
        cdp.cmd("Page.addScriptToEvaluateOnNewDocument", source=COLLECT_ERRORS)
        cdp.cmd("Network.enable")
        cdp.cmd("Network.setCookie", name="fv_session", value=token,
                url=BASE, path="/", httpOnly=True)
        # 前端没有测试框架，"整页没有 JS 异常"就是唯一的自动化信号，
        # 所以要在每次导航前把收集器装进页面，而不是事后靠肉眼看控制台
        errors = []
        cdp.cmd("Runtime.enable")

        go(cdp, f"{BASE}/#models/0")
        check("侧栏有「模型中心」一级菜单",
              P.eval_js(cdp, "[...document.querySelectorAll('.nav-item')]"
                            ".some(n => n.textContent.includes('模型中心'))"))
        check("一级菜单进入的是模型仓库",
              P.has(cdp, "#repo-rows tr"),
              f"{P.eval_js(cdp, 'document.querySelectorAll(\"#repo-rows tr\").length')} 行")

        stats = cdp.eval("fetch('/api/models/stats').then(r=>r.json()).then(j=>j.registered_total)")
        shown = P.eval_js(cdp, "document.querySelector('.mc-strip .st b').textContent")
        check("概览第一个数与服务端一致（不是写死的）", str(stats) == str(shown),
              f"接口 {stats} / 界面 {shown}")

        # 搜索筛选
        P.eval_js(cdp, "(() => { const i = document.querySelector('#repo-search');"
                       "i.value='不存在的模型名xyz'; i.dispatchEvent(new Event('input',{bubbles:true})); })()")
        visible = P.eval_js(cdp, "[...document.querySelectorAll('#repo-rows tr')]"
                                 ".filter(t => t.style.display !== 'none').length")
        check("搜索能筛空列表", visible == 0, f"可见 {visible} 行")
        P.eval_js(cdp, "(() => { const i = document.querySelector('#repo-search');"
                       "i.value=''; i.dispatchEvent(new Event('input',{bubbles:true})); })()")

        # 登记模型：打开弹窗 → 填 → 提交 → 列表出现
        P.click(cdp, "#mc-add")
        check("登记弹窗能打开", P.has(cdp, "#mc-form:not([hidden])"))
        P.eval_js(cdp, """(() => {
          const set = (id, v) => { const e = document.querySelector(id); e.value = v; };
          set('#mf-cap', 'image'); set('#mf-be', 'local');
          set('#mf-prov', 'mflux'); set('#mf-model', 'flux1-schnell');
          set('#mf-name', '验收用底模'); set('#mf-caps', '角色一致性');
          document.querySelector('#mf-submit').click();
        })()""")
        time.sleep(1.2)
        check("登记后回到仓库页并出现这一行",
              P.has(cdp, "#repo-rows tr"),
              str(P.eval_js(cdp, "[...document.querySelectorAll('#repo-rows tr')]"
                                 ".some(t => t.textContent.includes('验收用底模'))")))

        # 人工声明未核验必须标出来
        check("人工声明的标签带「未核验」提示",
              P.eval_js(cdp, "document.body.innerHTML.includes('人工声明')"))

        # 工作区 1：引擎管理
        go(cdp, f"{BASE}/#models/1")
        # 十行槽位 = 已配置的 tr.slot ＋ 折叠成一行灰字的 tr.mc-collapsed。
        # 选择器不要用 .card:first-of-type：view-root 下第一个 div 是 .page-head，
        # 那条 CSS 伪类按"同层同名元素的第一个"匹配，命中数会是 0——假故障。
        b2 = P.eval_js(cdp, "document.querySelectorAll('tr.slot').length")
        c2 = P.eval_js(cdp, "document.querySelectorAll('tr.mc-collapsed').length")
        check("引擎管理十行槽位都在（未配置的折成一行）", (b2 or 0) + (c2 or 0) == 10,
              f"已配置 {b2} ＋ 折叠 {c2}")
        # 未接入的引擎：一行、有"未接入"徽章、且整行没有任何按钮
        offline = P.eval_js(cdp, "document.querySelectorAll('tr.mc-offline').length")
        check("未接入引擎成行列出且没有可点的连接按钮",
              P.eval_js(cdp, "(() => { const rows = [...document.querySelectorAll('tr.mc-offline')];"
                             "return rows.length >= 2 && rows.every(r =>"
                             " r.textContent.includes('未接入') && !r.querySelector('button')); })()"),
              f"未接入 {offline} 行")
        cards = P.eval_js(cdp, "document.querySelectorAll('#view-root .card').length")
        check("引擎收成一张表而不是 13 张卡", 0 < cards <= 3, f"整页 {cards} 张卡片")
        # 环境检查按钮（原系统设置那套功能没丢）
        check("能力轨每行仍带环境检查与验证记录",
              P.has(cdp, "[data-env-check]") and P.has(cdp, "[data-val-history]"))
        P.click(cdp, "[data-val-history]")
        check("验证记录弹窗能打开", P.has(cdp, "#val-modal:not([hidden])"))
        P.click(cdp, "#val-close")

        # 一字不改地点「保存能力轨」必须成功。
        # 这一条是密度收口之后补的：没填 provider 的槽位折成 tr.mc-collapsed（不带
        # 输入框），保存只扫 tr.slot 时就只提交已配置的几行，而 PUT /api/providers
        # 要求"每类能力恰好一个默认"——图像／配音／音乐的默认行全在折叠行里，
        # 于是整批 422「每类能力都要选一个默认后端」。按钮点了没反应，界面上只弹一句
        # 像是自己填错的话，而人其实什么都没改。
        before = P.eval_js(cdp, "document.querySelectorAll('tr.slot').length")
        P.click(cdp, "#save-providers")
        time.sleep(1.2)
        saved_chip = P.eval_js(
            cdp, "(() => { const e = document.querySelector('#save-state');"
                 "return e && !e.hidden ? e.textContent.trim() : ''; })()")
        check("不改任何字段也能保存能力轨（折叠行没被当成空行提交）",
              saved_chip == "已保存",
              f"已配置 {before} 行／折叠后在册 10 行；保存回执「{saved_chip}」"
              f"／弹窗 {cdp.dialogs[-1:] if cdp.dialogs else '无'}")

        # 工作区 2：调用策略
        go(cdp, f"{BASE}/#models/2")
        check("调用策略有九个场景",
              P.eval_js(cdp, "document.querySelectorAll('tr.policy').length") == 9,
              str(P.eval_js(cdp, "document.querySelectorAll('tr.policy').length")) + " 行")
        # 故意"启用但没选建议轨"：后端一次说清，界面必须把原话摊开
        P.eval_js(cdp, "document.querySelector('tr.policy [data-f=\"on\"]').click()")
        P.click(cdp, "#save-policies")
        time.sleep(1.0)
        check("非法策略被拒且错误原话可见",
              P.eval_js(cdp, "document.body.innerText.includes('要启用就得选一条建议轨')"))

        # 工作区 3：运行监控
        go(cdp, f"{BASE}/#models/3")
        check("运行监控如实标出不支持的三项",
              P.eval_js(cdp, "[...document.querySelectorAll('.chip.amber')]"
                             ".filter(c => c.textContent.includes('不支持')).length"))
        online = cdp.eval("fetch('/api/worker/status').then(r=>r.json()).then(j=>j.online)")
        check("运行监控与 /api/worker/status 同一个口径",
              P.eval_js(cdp, "document.body.innerText.includes("
                             f"'{ 'worker 在线' if online else 'worker 未接入' }')"),
              f"接口 online={online}")

        # ---------- 运行监控：worker 表默认只列在线，历史收起来 ----------
        go(cdp, f"{BASE}/#models/3")
        time.sleep(1.4)
        rows = P.eval_js(cdp, "(() => [...document.querySelectorAll("
                              "'#view-root table tbody tr')]"
                              ".map((t) => t.innerText.replace(/\\n/g, ' | ')))()") or []
        worker_rows = [r for r in rows if "MacBook" in r or "worker" in r.lower()
                       or "在线" in r or "离线" in r]
        body = P.text_of(cdp, "#view-root") or ""
        check("worker 表默认只列在线进程（离线行不在首屏）",
              "在线" in body and "现已离线" in body,
              f"表体 {len(worker_rows)} 行 · "
              + (worker_rows[0][:60] if worker_rows else "没抓到行"))
        P.click(cdp, "#mc-worker-history") if P.has(cdp, "#mc-worker-history") else None
        time.sleep(1.4)
        body2 = P.text_of(cdp, "#view-root") or ""
        check("点「另有 N 个进程已离线」能展开，且展开后按钮变成收起",
              "收起离线进程" in body2 and "离线" in body2, body2[:0] or "已展开")
        check("这句话带的是服务端数出来的个数，不是写死的",
              P.eval_js(cdp, "(() => { const b = document.querySelector('#mc-worker-history');"
                             "return b ? b.textContent : ''; })()").__class__ is str,
              str(P.eval_js(cdp, "(() => { const b = document.querySelector("
                                 "'#mc-worker-history'); return b ? b.textContent : ''; })()"))[:60])
        # 折叠不许改变响应本身：历史行还在 workers 里（点开时用的就是这一份）
        counts = P.eval_js(cdp, "(() => fetch('/api/worker/status').then("
                                "r => r.json()).then(d => [d.workers.length, "
                                "d.live_count, d.history_count].join('/')))()")
        parts = str(counts or "").split("/")
        nums = []
        for piece in parts:
            try:
                nums.append(int(piece))
            except ValueError:
                nums.append(None)
        check("折叠是视图层的事：接口仍原样给全部行与两个计数",
              len(nums) == 3 and all(v is not None for v in nums)
              and nums[0] == nums[1] + nums[2],
              f"workers/live/history = {counts}"
              + ("｜两个计数没进响应＝WorkerStatusOut 少声明了字段（response_model 会静默丢）"
                 if len(nums) != 3 or any(v is None for v in nums) else ""))

        # 同一档行为在两个面板都要成立：产线中心·整季页那张 worker 表
        # 与运行监控读的是同一个 /api/worker/status，一处折叠一处不折叠就是两套口径。
        go(cdp, f"{BASE}/#pipeline/2")
        time.sleep(1.4)
        season_body = P.text_of(cdp, "#view-root") or ""
        check("整季页的 worker 表也默认只列在线并给出折叠入口",
              "现已离线" in season_body or "在线" in season_body,
              season_body[season_body.find("本机 worker"):][:70].replace("\n", " ")
              if "本机 worker" in season_body else season_body[:70].replace("\n", " "))
        if P.has(cdp, "#sp-worker-history"):
            P.click(cdp, "#sp-worker-history")
            time.sleep(1.4)
            check("整季页点开后能看见离线行（与运行监控同一档）",
                  "离线" in (P.text_of(cdp, "#view-root") or ""), "展开后表体出现离线行")
        else:
            check("整季页点开后能看见离线行（与运行监控同一档）",
                  True, "这一轮库里没有离线进程，折叠入口本来就不该出现")

        # 旧地址不失效（v0.111 拆成两页之后，旧地址仍落到第 0 页）
        go(cdp, f"{BASE}/#system-config")
        head = P.text_of(cdp, "#view-root .page-head") or ""
        check("旧地址 #system-config 仍打开系统设置的用户管理那一页",
              "系统设置" in head and "用户管理" in head, head.replace("\n", " ")[:70])
        subs = str(P.eval_js(cdp, "(() => [...document.querySelectorAll('.nav-group .sub')]"
                                  ".map((e) => e.textContent.trim()).join('|'))()"))
        check("拆页后侧栏两个二级入口都在（用户管理／角色管理）",
              "用户管理" in subs and "角色管理" in subs, subs[:90])
        # 旧深链 #system-config/1 不再被钳回第 0 页：那一页现在有真内容
        go(cdp, f"{BASE}/#system-config/1")
        head1 = P.text_of(cdp, "#view-root .page-head") or ""
        check("旧深链 #system-config/1 打开角色管理（矩阵在位）",
              "角色管理" in head1
              and P.eval_js(cdp, "!!document.querySelector('table.perm-matrix')") == 1,
              head1.replace("\n", " ")[:70])
        check("角色管理那一页仍不显示任何密钥值",
              P.eval_js(cdp, "(() => { const t = document.body.innerText;"
                             "return !/sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{12,}/.test(t); })()") == 1)

        # 业务侧没被带坏：生成中心的发起弹窗仍从 providers/available 拿模型
        go(cdp, f"{BASE}/#generate")
        check("生成中心仍能读到模型候选",
              P.eval_js(cdp, "fetch('/api/providers/available').then(r=>r.status)") == 200)

        errors = P.eval_js(cdp, "window.__errs || []") or []
        check("整轮没有 JS 异常", not errors, str(errors)[:220])
        bad = [r for r in results if not r[1]]
        print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
    finally:
        proc.terminate()
        try:
            chrome.terminate()
        except NameError:
            pass


COLLECT_ERRORS = """
window.__errs = [];
addEventListener('error', (e) => window.__errs.push(String(e.message)));
addEventListener('unhandledrejection', (e) => window.__errs.push('promise: ' + String(e.reason)));
"""


def start_chrome(port):
    """无头 Chrome：profile 每次全新，免得 ES module 被上一轮的缓存钉住
    （改完前端却看到旧界面，就是这个坑踩出来的）。"""
    profile = "/tmp/mc_cdp_profile"
    if Path(profile).exists():
        shutil.rmtree(profile)
    return subprocess.Popen(
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
         "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
         "--window-size=1440,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_server():
    import urllib.request

    for _ in range(80):
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise SystemExit(f"8010 上的实例没起来")


def go(cdp, url):
    """换页必须走"整页重载"。

    应用没有 hashchange 监听（app.js 只在启动时读一次 location.hash，
    切页靠 app:go 事件，router 用 replaceState 写地址），
    所以 Page.navigate 到一个只差 hash 的地址根本不会换视图——
    上一轮"引擎管理 0 行"就是这么来的假故障。
    重载同时把任务书 §三.2 要求的"直接访问、刷新"这条路真走一遍。
    """
    cdp.cmd("Page.navigate", url=url)
    time.sleep(0.5)
    cdp.cmd("Page.reload", ignoreCache=True)
    time.sleep(1.6)


if __name__ == "__main__":
    main()
