# -*- coding: utf-8 -*-
'''验收：点「启动服务」之后弹窗必须还在、并就地刷新出结果。'''
import sys, subprocess, time, os, json
ROOT = '/Users/zhangshuo/AI动漫工作流设计文档/平台设计'
sys.path.insert(0, ROOT + '/tools'); sys.path.insert(0, ROOT + '/app/backend')
os.chdir(ROOT + '/app/backend')
import model_center_browser_check as MC, proto_cdp_check as P, _harness as H

RESULT = []
def check(name, ok, detail=''):
    RESULT.append((name, bool(ok)))
    print(('  OK   ' if ok else '  FAIL ') + name + (('  :: ' + str(detail)) if detail else ''), flush=True)

# KILL_LEFTOVER: 上一次验收可能把 ollama 留在后台，而这条用例的前提是它没在跑。
subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True)
time.sleep(1.0)

MC.prepare_env()
token = MC.mint_admin_session()
proc = subprocess.Popen([str(H.backend_python()), '-m', 'uvicorn', 'main:app',
                         '--host', '127.0.0.1', '--port', str(MC.PORT), '--log-level', 'warning'],
                        cwd=str(MC.BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
chrome = None
started = False
try:
    MC.wait_server()
    from db import get_conn
    with get_conn() as conn:
        conn.execute('DELETE FROM engine_instance')
        conn.execute('INSERT INTO engine_instance (engine_code, display_name, endpoint) VALUES (?,?,?)',
                     ('ollama', 'ollama', 'http://127.0.0.1:11434'))
    chrome = MC.start_chrome(P.PORT)
    cdp = P.CDP(P.wait_for_target())
    cdp.cmd('Page.enable')
    cdp.cmd('Page.addScriptToEvaluateOnNewDocument', source=MC.COLLECT_ERRORS)
    cdp.cmd('Network.setCookie', name='fv_session', value=token, url=MC.BASE, path='/', httpOnly=True)
    MC.go(cdp, MC.BASE + '/#models/0')
    time.sleep(3.5)
    cdp.eval("document.getElementById('mc-check-all').click()")
    time.sleep(7.0)

    found = cdp.eval('''(()=>{const m=[...document.querySelectorAll('.modal')].find(x=>x.querySelector('[data-close-probe-res]'));return m?'yes':'no';})()''')
    check('结果窗打开', found == 'yes', found)

    clicked = cdp.eval('''(()=>{const m=[...document.querySelectorAll('.modal')].find(x=>x.querySelector('[data-close-probe-res]'));const b=m&&m.querySelector('[data-act-launch]');if(!b)return 'no-btn';b.click();return 'clicked';})()''')
    check('点到了启动按钮', clicked == 'clicked', clicked)

    busy = False
    for _ in range(24):
        time.sleep(0.25)
        t = cdp.eval('''(()=>{const m=[...document.querySelectorAll('.modal')].find(x=>x.querySelector('[data-close-probe-res]'));const b=m&&m.querySelector('[data-act-launch]');return b?b.textContent.trim():'gone';})()''')
        if t == '启动中…':
            busy = True
            break
    check('按钮显示「启动中…」', busy)

    time.sleep(10.0)
    still = cdp.eval('''(()=>{const m=[...document.querySelectorAll('.modal')].find(x=>x.querySelector('[data-close-probe-res]'));return m?'open':'closed';})()''')
    check('★ 启动完成后弹窗仍然开着（不再自己消失）', still == 'open', still)

    notice = cdp.eval('''(()=>{const n=document.querySelector('[data-launch-notice]');return n?n.textContent.trim():'none';})()''')
    check('★ 结果写在屏内一行（不是会自己消失的浮层）', notice not in ('none', ''), notice)

    state = cdp.eval('''(()=>{const m=[...document.querySelectorAll('.modal')].find(x=>x.querySelector('[data-close-probe-res]'));const t=m&&m.querySelector('tbody');return t?t.textContent.replace(/\\s+/g,' ').slice(0,120):'none';})()''')
    print('TABLE:', state, flush=True)
    check('★ 表格已就地刷成新状态（连上了）', '连上了' in str(state), state)
    gone = cdp.eval('''(()=>{const m=[...document.querySelectorAll('.modal')].find(x=>x.querySelector('[data-close-probe-res]'));return m?m.querySelectorAll('[data-act-launch]').length:-1;})()''')
    check('刷完之后启动按钮消失（已经在跑）', gone == 0, gone)

    errs = cdp.eval('JSON.stringify(window.__errs||[])')
    check('无运行时错误', errs in ('[]', 'null'), errs)
    started = True
finally:
    if chrome: H.stop(chrome)
    H.stop(proc)
    if started:
        subprocess.run(['pkill', '-f', 'ollama serve'], capture_output=True)
bad = [r for r in RESULT if not r[1]]
print(chr(10) + '=== %d/%d PASS ===' % (len(RESULT) - len(bad), len(RESULT)), flush=True)
sys.exit(1 if bad else 0)
