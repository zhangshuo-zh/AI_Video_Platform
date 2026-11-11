#!/usr/bin/env python3
"""F-01/F-02 真浏览器验收：未读数取服务端口径 + 面板取满 100 条。

用法：
  python3 tools/_verify_bell.py            # 正向，应全绿
  python3 tools/_verify_bell.py --reverse   # 把修复回退，同一断言必须变红

反向对照只回退**一处**（render 里的 unread 改回自己数 items），
备份存工作区，最后还原并复跑确认恢复绿。
"""
from __future__ import annotations
import os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H
import model_center_browser_check as MC

REVERSE = '--reverse' in sys.argv
ROOT = Path(__file__).resolve().parents[1]
NOTIF = ROOT / 'app' / 'frontend' / 'js' / 'notifications.js'
BAK = ROOT / 'tools' / '_notif.bak'

results = []
def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print(('  OK   ' if ok else '  FAIL ') + name + ('  :: ' + str(detail) if detail else ''), flush=True)

def seed(n_unread, n_read=0):
    import notifications
    from db import get_conn
    uid = MC._admin_id()
    with get_conn() as conn:
        conn.execute('DELETE FROM notification_read')
        conn.execute('DELETE FROM notification')
    for i in range(n_unread):
        notifications.emit(kind='job_failed', title=f'未读作业 {i:03d}',
                           body='验收种子', level='bad', audience=f'user:{uid}',
                           dedupe_key=f'verify-u-{i}')
    ids = []
    for i in range(n_read):
        nid = notifications.emit(kind='job_failed', title=f'已读作业 {i:03d}',
                                 body='验收种子', level='bad', audience=f'user:{uid}',
                                 dedupe_key=f'verify-r-{i}')
        if nid: ids.append(nid)
    if ids:
        notifications.mark_read({'id': uid, 'is_superuser': True}, ids)

def open_menu(cdp):
    cdp.eval("document.getElementById('bell-btn').click()")
    time.sleep(1.1)
    return cdp.eval(r"""(() => {
      const m = document.getElementById('bell-menu');
      const hint = m.querySelector('.bell-foot .hint');
      const tr = m.querySelector('.bell-trunc');
      return JSON.stringify({
        hidden: m.hidden,
        rows: m.querySelectorAll('.bell-item').length,
        unreadRows: m.querySelectorAll('.bell-item.unread').length,
        foot: hint ? hint.textContent.trim() : null,
        trunc: tr ? tr.textContent.replace(/\s+/g,' ').trim() : null,
        readAllDisabled: !!m.querySelector('#bell-read-all')?.disabled,
        dotHidden: document.getElementById('bell-dot').hidden,
        bellTone: document.getElementById('bell-btn').classList.contains('has-unread')
      });
    })()""")

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

        if REVERSE:
            assert not BAK.exists(), '备份已存在，先还原再跑'
            BAK.write_bytes(NOTIF.read_bytes())
            t = NOTIF.read_text(encoding='utf-8')
            old = 'function render(menu, dot, unread) {'
            new = old + '\n  unread = items.filter((x) => !x.read).length;  // REVERSE-CONTROL'
            assert t.count(old) == 1
            NOTIF.write_text(t.replace(old, new), encoding='utf-8')
            assert 'REVERSE-CONTROL' in NOTIF.read_text(encoding='utf-8'), '变异没落下'
            print('REVERSE CONTROL 已落下（render 改回自己数 items）', flush=True)

        # ---- 场景 1：45 条未读（超过默认 limit=30）----
        seed(45)
        MC.go(cdp, MC.BASE + '/#overview')
        time.sleep(1.4)
        srv = cdp.eval("""(async()=>{const r=await fetch('/api/notifications');const j=await r.json();
          return JSON.stringify({unread:j.unread,items:j.items.length});})()""")
        print('SERVER(default) ' + srv, flush=True)
        import json as J
        s = J.loads(srv)
        check('S1 种子真的写进去了（服务端未读=45）', s['unread'] == 45, srv)
        check('S1 服务端默认仍截断到 30（证明缺陷路径可达）', s['items'] == 30, s['items'])
        dom = J.loads(open_menu(cdp))
        print('DOM(45 unread) ' + J.dumps(dom, ensure_ascii=False), flush=True)
        check('F-01 页脚显示 45 条未读（不是 30）', dom['foot'] == '45 条未读', dom['foot'])
        check('F-02 面板取满 45 行（不再被 30 截断）', dom['rows'] == 45, dom['rows'])
        check('F-01 未读角标可见', dom['dotHidden'] is False and dom['bellTone'] is True, dom)
        check('F-02 全部已读可点', dom['readAllDisabled'] is False, dom['readAllDisabled'])
        check('S1 未截断时不显示截断提示', dom['trunc'] is None, dom['trunc'])

        # ---- 场景 2：150 条未读（超过上限 100）----
        seed(150)
        MC.go(cdp, MC.BASE + '/#overview')
        time.sleep(1.4)
        srv2 = J.loads(cdp.eval("""(async()=>{const r=await fetch('/api/notifications?limit=100');const j=await r.json();
          return JSON.stringify({unread:j.unread,items:j.items.length});})()"""))
        print('SERVER(limit=100) ' + J.dumps(srv2), flush=True)
        check('S2 后端上限确实是 100', srv2['items'] == 100 and srv2['unread'] == 150, srv2)
        dom2 = J.loads(open_menu(cdp))
        print('DOM(150 unread) ' + J.dumps(dom2, ensure_ascii=False), flush=True)
        check('F-01 截断时页脚仍显示真实 150', dom2['foot'] == '150 条未读', dom2['foot'])
        check('F-02 面板取到上限 100 行', dom2['rows'] == 100, dom2['rows'])
        check('F-02 明说还有没列出来的', dom2['trunc'] and '150' in dom2['trunc'], dom2['trunc'])

        # ---- 场景 3：已读占名额（判据不能写成 unread > items.length）----
        seed(45, n_read=80)
        MC.go(cdp, MC.BASE + '/#overview')
        time.sleep(1.4)
        dom3 = J.loads(open_menu(cdp))
        print('DOM(45 unread + 80 read) ' + J.dumps(dom3, ensure_ascii=False), flush=True)
        check('S3 已读挤掉未读时页脚仍是 45', dom3['foot'] == '45 条未读', dom3['foot'])
        check('S3 未读没全列出来 → 必须提示截断', dom3['trunc'] is not None, dom3['trunc'])

        # ---- 场景 4：全部已读真的清干净（含没列出来的）----
        cdp.eval("document.getElementById('bell-read-all').click()")
        time.sleep(1.3)
        after = J.loads(cdp.eval("""(async()=>{const r=await fetch('/api/notifications');const j=await r.json();
          return JSON.stringify({unread:j.unread});})()"""))
        check('S4 全部已读清掉了全部 45 条（不只是列出的 20 条）', after['unread'] == 0, after)
        check('S4 角标随之消失', cdp.eval("document.getElementById('bell-dot').hidden") is True)

        # ---- 场景 5：空收件箱 ----
        seed(0)
        MC.go(cdp, MC.BASE + '/#overview')
        time.sleep(1.2)
        dom5 = J.loads(open_menu(cdp))
        check('S5 空收件箱不报错、显示没有通知',
              cdp.eval("!!document.querySelector('#bell-menu .bell-empty')") is True, dom5)

        errs = cdp.eval('JSON.stringify(window.__errs || [])')
        check('无运行时错误', errs in ('[]', 'null'), errs)
    finally:
        if REVERSE and BAK.exists():
            NOTIF.write_bytes(BAK.read_bytes())
            assert 'REVERSE-CONTROL' not in NOTIF.read_text(encoding='utf-8'), '还原失败'
            BAK.unlink()
            print('已还原 notifications.js', flush=True)
        if chrome: H.stop(chrome)
        H.stop(proc)
    bad = [r for r in results if not r[1]]
    print(f'\n=== {len(results)-len(bad)}/{len(results)} PASS ===', flush=True)
    return 1 if bad else 0

if __name__ == '__main__':
    sys.exit(main())
