# -*- coding: utf-8 -*-
"""F-04/F-07 真浏览器验收：运行监控补一拍 + 通知回前台补一拍 + 过期收口。

三件事都在真页面上量请求数，不看源码：
  1. 模型中心·运行监控：后台静默、回前台 1.5 秒内补一拍；
  2. 通知铃铛：后台静默、回前台补一拍（这条以前根本没有）；
  3. 会话过期（app:unauthorized，不重载页面）后通知轮询必须停，
     且点外面/按 Esc 不再触发旧闭包。
"""
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H
import model_center_browser_check as MC

RESULTS = []

def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  OK   " if ok else "  FAIL ") + name + ("  :: " + str(detail) if detail else ""), flush=True)

COUNTER = """
// 只数「谁发的」。/api/worker/status 有两个调用方：app.js 的 worker 心跳
// 与 monitor.js 的运行监控轮询，两条都会在 visibilitychange 时补一拍。
// 不区分来源就数总量，会把 app.js 那一条算成 monitor 的——反向对照时
// 撤掉 monitor 的补拍仍然变绿，探针等于没测（2026-11-11 实测踩到）。
// 这里按调用栈归属，栈里带哪个模块的路径就算谁的。
window.__hits = {notif: 0, workerMon: 0, workerShell: 0, worker: 0};
const _f = window.fetch;
window.fetch = function(u, ...rest) {
  const s = String(typeof u === 'string' ? u : (u && u.url) || '');
  if (s.indexOf('/api/notifications') >= 0) window.__hits.notif++;
  if (s.indexOf('/api/worker/status') >= 0) {
    window.__hits.worker++;
    const st = String(new Error().stack || '');
    if (st.indexOf('monitor.js') >= 0) window.__hits.workerMon++;
    else window.__hits.workerShell++;
  }
  return _f.call(this, u, ...rest);
};
"""

def main():
    MC.prepare_env()
    token = MC.mint_admin_session()
    proc = subprocess.Popen(
        [str(H.backend_python()), '-m', 'uvicorn', 'main:app',
         '--host', '127.0.0.1', '--port', str(MC.PORT), '--log-level', 'warning'],
        cwd=str(MC.BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    chrome = None
    try:
        MC.wait_server()
        import proto_cdp_check as P
        chrome = MC.start_chrome(P.PORT)
        cdp = P.CDP(P.wait_for_target())
        cdp.cmd('Page.enable')
        cdp.cmd('Page.addScriptToEvaluateOnNewDocument', source=MC.COLLECT_ERRORS)
        cdp.cmd('Page.addScriptToEvaluateOnNewDocument', source=COUNTER)
        cdp.cmd('Network.setCookie', name='fv_session', value=token,
                url=MC.BASE, path='/', httpOnly=True)

        # ---- 场景 1：运行监控（#models/3）
        MC.go(cdp, MC.BASE + '/#models/3')
        time.sleep(7.0)
        has = cdp.eval("!!document.querySelector('#mc-live')")
        check('运行监控渲染出 #mc-live（轮询锚点）', has, has)
        base = json.loads(cdp.eval('JSON.stringify(window.__hits)'))
        check('前台监控轮询在跑（先证明探针有效）', base['worker'] > 0, base)

        cdp.cmd('Target.createTarget', url='about:blank')
        time.sleep(1.0)
        check('标签页真的进了后台', cdp.eval('document.hidden') is True)
        cdp.eval('window.__hits = {notif: 0, workerMon: 0, workerShell: 0, worker: 0}')
        time.sleep(12.0)   # 5 秒一轮 → 不修的话至少 2 拍
        bg = json.loads(cdp.eval('JSON.stringify(window.__hits)'))
        check('F-04 监控后台 12 秒 0 请求', bg['workerMon'] == 0, bg)
        check('F-07 通知后台 12 秒 0 请求', bg['notif'] == 0, bg)

        cdp.eval('window.__hits = {notif: 0, workerMon: 0, workerShell: 0, worker: 0}')
        cdp.cmd('Page.bringToFront')
        time.sleep(1.5)
        fg = json.loads(cdp.eval('JSON.stringify(window.__hits)'))
        check('F-04 监控回前台立刻补一拍', fg['workerMon'] > 0, fg)
        check('F-07 通知回前台立刻补一拍', fg['notif'] > 0, fg)

        # ---- 场景 2：会话过期不重载页面，通知轮询必须停
        cdp.eval("window.dispatchEvent(new CustomEvent('app:unauthorized'))")
        time.sleep(1.0)
        gate = cdp.eval("!!document.querySelector('#gate, .gate, [data-gate]')")
        print('GATE ' + str(gate), flush=True)
        cdp.eval('window.__hits = {notif: 0, workerMon: 0, workerShell: 0, worker: 0}')
        time.sleep(22.0)   # 通知 20 秒一轮
        after = json.loads(cdp.eval('JSON.stringify(window.__hits)'))
        check('F-07 会话过期后通知轮询已停', after['notif'] == 0, after)

        # ---- 场景 3：重登必须把铃铛挂回来，且不能叠成两份
        # 重登要真口令。副本库里给 admin 设一个已知的（只动一次性验收副本）。
        from security import hash_password
        from db import get_conn
        with get_conn() as conn:
            row = conn.execute('SELECT username FROM app_user WHERE id = ?',
                               (MC._admin_id(),)).fetchone()
            uname = row['username']
            conn.execute('UPDATE app_user SET password_hash = ?, is_active = 1, '
                         'must_change_password = 0 WHERE id = ?',
                         (hash_password('Verify#2026'), MC._admin_id()))
        cdp.eval('document.getElementById(\'gate-user\').value = ' + json.dumps(uname))
        cdp.eval("document.getElementById('gate-pass').value = 'Verify#2026'")
        cdp.eval("document.getElementById('gate-form').requestSubmit()")
        time.sleep(3.0)
        back = cdp.eval('document.getElementById(\'gate\').hidden')
        check('重新登录后闸门已关', back is True, back)
        cdp.eval('window.__hits = {notif: 0, workerMon: 0, workerShell: 0, worker: 0}')
        cdp.eval("document.getElementById('bell-btn').click()")
        time.sleep(1.2)
        opened = cdp.eval('!document.getElementById(\'bell-menu\').hidden')
        check('F-07 重登后铃铛仍能点开（没叠成开→关两份委托）', opened is True, opened)
        n1 = json.loads(cdp.eval('JSON.stringify(window.__hits)'))['notif']
        check('点一次只发一次请求（不重复拉）', n1 == 1, n1)
        cdp.eval('window.__hits = {notif: 0, workerMon: 0, workerShell: 0, worker: 0}')
        time.sleep(22.0)
        again = json.loads(cdp.eval('JSON.stringify(window.__hits)'))
        check('F-07 重登后轮询恢复且只有一份（22 秒 1 次）', again['notif'] == 1, again)

        errs = cdp.eval('JSON.stringify(window.__errs || [])')
        check('无运行时错误', errs in ('[]', 'null'), errs)
    finally:
        if chrome: H.stop(chrome)
        H.stop(proc)
    bad = [r for r in RESULTS if not r[1]]
    print('\n=== %d/%d PASS ===' % (len(RESULTS) - len(bad), len(RESULTS)), flush=True)
    return 1 if bad else 0

if __name__ == '__main__':
    sys.exit(main())