# -*- coding: utf-8 -*-
'''截图工具：打开模型中心·引擎管理，点「检查引擎连接」，把结果窗截下来。'''
import sys, subprocess, time, os, base64, json
ROOT = '/Users/zhangshuo/AI动漫工作流设计文档/平台设计'
sys.path.insert(0, ROOT + '/tools'); sys.path.insert(0, ROOT + '/app/backend')
os.chdir(ROOT + '/app/backend')
import model_center_browser_check as MC, proto_cdp_check as P, _harness as H

MC.prepare_env()
token = MC.mint_admin_session()
proc = subprocess.Popen([str(H.backend_python()), '-m', 'uvicorn', 'main:app',
                         '--host', '127.0.0.1', '--port', str(MC.PORT), '--log-level', 'warning'],
                        cwd=str(MC.BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
chrome = None
try:
    MC.wait_server()
    from db import get_conn
    with get_conn() as conn:
        conn.execute('DELETE FROM engine_instance')
        for code, ep in (('ollama', 'http://127.0.0.1:11434'),
                         ('draw_things', 'http://127.0.0.1:7860')):
            conn.execute('INSERT INTO engine_instance (engine_code, display_name, endpoint) VALUES (?,?,?)',
                         (code, code, ep))
    chrome = MC.start_chrome(P.PORT)
    cdp = P.CDP(P.wait_for_target())
    cdp.cmd('Page.enable')
    cdp.cmd('Network.setCookie', name='fv_session', value=token, url=MC.BASE, path='/', httpOnly=True)
    cdp.cmd('Emulation.setDeviceMetricsOverride', width=1500, height=1000, deviceScaleFactor=2, mobile=False)
    MC.go(cdp, MC.BASE + '/#models/0')
    time.sleep(3.5)
    cdp.eval("document.getElementById('mc-check-all').click()")
    time.sleep(7.0)
    box = cdp.eval('''(()=>{const m=[...document.querySelectorAll('.modal')].find(x=>x.querySelector('[data-close-probe-res]'));if(!m)return null;const b=m.querySelector('.modal-box').getBoundingClientRect();return JSON.stringify({x:Math.round(b.x),y:Math.round(b.y),w:Math.round(b.width),h:Math.min(Math.round(b.height),980)});})()''')
    print('BOX', box)
    b = json.loads(box)
    data = cdp.cmd('Page.captureScreenshot', format='png',
                   clip={'x': b['x'], 'y': b['y'], 'width': b['w'], 'height': b['h'], 'scale': 2})
    open('/tmp/modal.png', 'wb').write(base64.b64decode(data['data']))
    # 顺带量列宽
    cols = cdp.eval('''(()=>{const m=[...document.querySelectorAll('.modal')].find(x=>x.querySelector('[data-close-probe-res]'));const t=m.querySelector('table.tbl-fixed');return JSON.stringify([...t.querySelectorAll('thead th')].map(h=>({t:h.textContent.trim(),w:Math.round(h.getBoundingClientRect().width)})));})()''')
    print('COLS', cols)
finally:
    if chrome: H.stop(chrome)
    H.stop(proc)
