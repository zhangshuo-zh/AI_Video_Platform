# -*- coding: utf-8 -*-
import sys, subprocess, time, os, base64
ROOT = '/Users/zhangshuo/AI动漫工作流设计文档/平台设计'
sys.path.insert(0, ROOT + '/tools'); sys.path.insert(0, ROOT + '/app/backend')
os.chdir(ROOT + '/app/backend')
import model_center_browser_check as MC, proto_cdp_check as P, _harness as H
SUBPAGE = int(sys.argv[1]) if len(sys.argv) > 1 else 0
OUT = sys.argv[2] if len(sys.argv) > 2 else '/tmp/page.png'
MC.prepare_env()
token = MC.mint_admin_session()
proc = subprocess.Popen([str(H.backend_python()), '-m', 'uvicorn', 'main:app',
                         '--host', '127.0.0.1', '--port', str(MC.PORT), '--log-level', 'warning'],
                        cwd=str(MC.BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
chrome = None
try:
    MC.wait_server()
    chrome = MC.start_chrome(P.PORT)
    cdp = P.CDP(P.wait_for_target())
    cdp.cmd('Page.enable')
    cdp.cmd('Network.setCookie', name='fv_session', value=token, url=MC.BASE, path='/', httpOnly=True)
    cdp.cmd('Emulation.setDeviceMetricsOverride', width=1500, height=1100, deviceScaleFactor=2, mobile=False)
    MC.go(cdp, MC.BASE + '/#models/' + str(SUBPAGE))
    time.sleep(4.0)
    cdp.eval("(()=>{const n=document.querySelector('#view-root p.note');if(n)n.scrollIntoView({block:'center'});})()")
    time.sleep(0.6)
    data = cdp.cmd('Page.captureScreenshot', format='png')
    open(OUT, 'wb').write(base64.b64decode(data['data']))
    print('saved', OUT)
finally:
    if chrome: H.stop(chrome)
    H.stop(proc)
