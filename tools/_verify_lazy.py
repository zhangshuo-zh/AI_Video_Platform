# -*- coding: utf-8 -*-
'''F-06 真浏览器验收：视图按需加载。

两条都要量：一是**确实按需**（首屏只取总览，别的视图一个字都没请求）；
二是**没有退化**（深链、未知 id 退回总览、来回切页都还对）。
只证明前者的改法很容易把后两者弄坏，所以每一条都得测。
'''
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H
import model_center_browser_check as MC

RESULTS = []
def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok)))
    print(('  OK   ' if ok else '  FAIL ') + name + (('  :: ' + str(detail)) if detail else ''), flush=True)

# 本次页面生命期里，浏览器取过哪些 /js/views/*.js（performance 是现成的账本，
# 不用劫持 fetch —— 少一层自己写的探测逻辑，就少一处能骗自己的地方）。
VIEWS_EXPR = ("JSON.stringify(performance.getEntriesByType('resource')"
              ".map(function(e){return e.name;})"
              ".filter(function(n){return n.indexOf('/js/views/')>=0;}))")

def view_modules(cdp):
    return [n.split('/js/views/')[-1] for n in json.loads(cdp.eval(VIEWS_EXPR))]

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
        cdp.cmd('Network.setCookie', name='fv_session', value=token,
                url=MC.BASE, path='/', httpOnly=True)

        MC.go(cdp, MC.BASE + '/#overview')
        time.sleep(3.5)
        check('首屏渲染出侧栏', cdp.eval("!!document.querySelector('.nav-item')") is True)
        check('首屏落在总览', cdp.eval('document.body.dataset.view') == 'overview')

        names = view_modules(cdp)
        print('首屏取的视图模块：' + str(names), flush=True)
        check('首屏没有把 15 个视图全下来', len(names) <= 4, len(names))
        check('首屏确实走了按需加载（不是空账本）', len(names) >= 1, len(names))

        MC.go(cdp, MC.BASE + '/#models/3')
        time.sleep(3.5)
        check('深链 #models/3 落到 models', cdp.eval('document.body.dataset.view') == 'models')
        check('深链的三级子页也渲染了（#mc-live 在）',
              cdp.eval("!!document.querySelector('#mc-live')") is True)
        after = view_modules(cdp)
        print('打开模型中心后：' + str(sorted(set(after) - set(names))), flush=True)
        check('打开模型中心时才取它的模块',
              any('models' in n for n in after), after)

        MC.go(cdp, MC.BASE + '/#nope-not-a-view')
        time.sleep(3.0)
        check('未知 id 退回总览', cdp.eval('document.body.dataset.view') == 'overview',
              cdp.eval('document.body.dataset.view'))

        for _ in range(2):
            cdp.eval("window.dispatchEvent(new CustomEvent('app:go', {detail: 'writing'}))")
            time.sleep(2.5)
            check('切到写作页', cdp.eval('document.body.dataset.view') == 'writing',
                  cdp.eval('document.body.dataset.view'))
        w = [n for n in view_modules(cdp) if 'writing' in n]
        check('同一页重复进入只取一次模块（模块级状态不会重来一份）', len(w) <= 1, w)

        errs = cdp.eval('JSON.stringify(window.__errs || [])')
        check('无运行时错误', errs in ('[]', 'null'), errs)
    finally:
        if chrome: H.stop(chrome)
        H.stop(proc)
    bad = [r for r in RESULTS if not r[1]]
    print(chr(10) + '=== %d/%d PASS ===' % (len(RESULTS) - len(bad), len(RESULTS)), flush=True)
    return 1 if bad else 0

if __name__ == '__main__':
    sys.exit(main())
