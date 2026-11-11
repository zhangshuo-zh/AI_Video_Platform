# -*- coding: utf-8 -*-
'''全视图巡检：15 个视图逐个真打开一遍。

按需加载最容易出的错，是某一页的模块从没被真正 import 过——静态 import 时
语法错/坏 import 会在首屏就炸出来，改成按需之后它就静静躺在磁盘上，直到
有人点开那一页才爆。这条巡检把每一页都点开，任何一页渲染失败都记下来。
'''
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H
import model_center_browser_check as MC

VIEW_IDS = ['overview', 'production', 'projects', 'shot', 'assets', 'writing',
            'generate', 'pipeline', 'composition', 'audio', 'review',
            'knowledge', 'agents', 'models', 'system-config']

RESULTS = []
def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok)))
    print(('  OK   ' if ok else '  FAIL ') + name + (('  :: ' + str(detail)) if detail else ''), flush=True)

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

        for vid in VIEW_IDS:
            # 先清错误账，再切页；切完给足时间让 await 的模块与数据都落地
            cdp.eval('window.__errs = []')
            cdp.eval('window.dispatchEvent(new CustomEvent("app:go", {detail: ' + repr(vid) + '}))')
            time.sleep(2.6)
            got = cdp.eval('document.body.dataset.view')
            empty = cdp.eval("!!document.querySelector('#view-root .empty')")
            hascard = cdp.eval("!!document.querySelector('#view-root section.view')")
            errs = json.loads(cdp.eval('JSON.stringify(window.__errs || [])'))
            check('视图 ' + vid + ' 能打开',
                  got == vid and hascard is True and not errs,
                  {'view': got, 'card': hascard, 'empty': empty, 'errs': errs[:2]})

        # 收尾：所有视图都过一遍之后，整站不该留下未捕获错误
        final = json.loads(cdp.eval('JSON.stringify(window.__errs || [])'))
        check('全部视图走完无未捕获错误', not final, final[:3])
    finally:
        if chrome: H.stop(chrome)
        H.stop(proc)
    bad = [r for r in RESULTS if not r[1]]
    print(chr(10) + '=== %d/%d PASS ===' % (len(RESULTS) - len(bad), len(RESULTS)), flush=True)
    return 1 if bad else 0

if __name__ == '__main__':
    sys.exit(main())
