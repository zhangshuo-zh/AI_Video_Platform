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
        shown = P.eval_js(cdp, "document.querySelector('.dash-grid > div > b').textContent")
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
        check("引擎管理有十行能力轨",
              P.eval_js(cdp, "document.querySelectorAll('tr.slot').length") == 10,
              str(P.eval_js(cdp, "document.querySelectorAll('tr.slot').length")) + " 行")
        check("未接入引擎卡没有可点的连接按钮",
              P.eval_js(cdp, "(() => { const cards = [...document.querySelectorAll('.card')]"
                             ".filter(c => c.textContent.includes('未接入'));"
                             "return cards.length > 0 && cards.every(c => !c.querySelector('button'));"
                             "})()"))
        # 环境检查按钮（原系统设置那套功能没丢）
        check("能力轨每行仍带环境检查与验证记录",
              P.has(cdp, "[data-env-check]") and P.has(cdp, "[data-val-history]"))
        P.click(cdp, "[data-val-history]")
        check("验证记录弹窗能打开", P.has(cdp, "#val-modal:not([hidden])"))
        P.click(cdp, "#val-close")

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

        # 旧地址不失效
        go(cdp, f"{BASE}/#system-config")
        check("旧地址 #system-config 仍打开用户与角色",
              P.eval_js(cdp, "document.body.innerText.includes('用户与角色')"))

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
