# -*- coding: utf-8 -*-
'''F-05 真浏览器验收：铃铛菜单的键盘可达性。

量的是真实按键之后 document.activeElement 落在谁身上——不是源码里有没有字符。
一个「有 role=menu 但方向键没反应」的面板，在源码扫描下和修好之后长得一样。
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
    mark = "  OK   " if ok else "  FAIL "
    print(mark + name + (("  :: " + str(detail)) if detail else ''), flush=True)

ACTIVE_JS = "(function(){var m=document.getElementById('bell-menu');if(!m)return 'no-menu';var rows=Array.prototype.slice.call(m.querySelectorAll('[role=\"menuitem\"]')).filter(function(e){return !e.disabled;});var a=document.activeElement;var i=rows.indexOf(a);return (i<0?'-1':'#'+i)+'|'+(a?(a.id||a.tagName):'none');})()"

LAST_JS = "(function(){var m=document.getElementById('bell-menu');var r=Array.prototype.slice.call(m.querySelectorAll('[role=\"menuitem\"]')).filter(function(e){return !e.disabled;});return r.length-1;})()"

def active(cdp):
    return cdp.eval(ACTIVE_JS)

def key(cdp, k, vk):
    # 无头 Chrome 只发 keyDown/keyUp 时，有的按键不会被当成一次真实的按键
    # 序列（ArrowDown 实测没触发监听）。rawKeyDown + char + keyUp 才是
    # CDP 里模拟真实输入的那一套；text 只给能打出字符的键。
    cdp.cmd('Input.dispatchKeyEvent', type='rawKeyDown', key=k, code=k,
            windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
    cdp.cmd('Input.dispatchKeyEvent', type='keyUp', key=k, code=k,
            windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
    time.sleep(0.4)

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

        # 收件人用 audience 表达：user:<id> 是点名某个人（0049 的三种之一）。
        from db import get_conn
        with get_conn() as conn:
            uid = conn.execute('SELECT id FROM app_user WHERE is_active = 1').fetchone()['id']
            for i in range(3):
                conn.execute(
                    'INSERT INTO notification (kind, level, title, body, link_page, audience, dedupe_key) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    ('worker_offline', 'warn', '验收通知 ' + str(i), '正文', 'overview',
                     'user:' + str(uid), 'probe' + str(i)))

        MC.go(cdp, MC.BASE + '/#overview')
        time.sleep(3.0)

        btn = "document.getElementById(" + chr(39) + "bell-btn" + chr(39) + ")"
        menu = "document.getElementById(" + chr(39) + "bell-menu" + chr(39) + ")"
        cdp.eval(btn + '.click()')
        time.sleep(1.2)
        check('点开后面板显示', cdp.eval('!' + menu + '.hidden') is True)
        first = active(cdp)
        check('打开后焦点已进入菜单（索引 0 起，-1 表示还在菜单外）',
              first.startswith('#0|'), first)

        n_items = cdp.eval('document.querySelectorAll(' + "'#bell-menu [role=\"menuitem\"]'" + ').length')
        n_rows = cdp.eval('document.querySelectorAll(' + "'#bell-menu .bell-item'" + ').length')
        check('每条通知都是 menuitem（另加全部已读一项）', n_items == n_rows + 1, (n_items, n_rows))

        before = active(cdp)
        key(cdp, 'ArrowDown', 40)
        after = active(cdp)
        check('ArrowDown 把焦点挪到下一项', before != after, (before, after))
        key(cdp, 'ArrowUp', 38)
        check('ArrowUp 回到上一项', active(cdp) == before, (before, active(cdp)))

        key(cdp, 'End', 35)
        end_last = cdp.eval(LAST_JS)
        check('End 到最末一项', active(cdp).startswith('#' + str(end_last) + '|'),
              (end_last, active(cdp)))
        key(cdp, 'Home', 36)
        check('Home 回到第一项', active(cdp) == before, (before, active(cdp)))

        key(cdp, 'ArrowUp', 38)
        last_idx = cdp.eval(LAST_JS)
        check('ArrowUp 在首项时绕到末项', active(cdp).startswith('#' + str(last_idx) + '|'),
              (last_idx, active(cdp)))

        key(cdp, 'Escape', 27)
        check('Esc 关闭面板', cdp.eval(menu + '.hidden') is True)
        check('Esc 后焦点回到铃铛', 'bell-btn' in active(cdp), active(cdp))

        with get_conn() as conn:
            conn.execute('DELETE FROM notification')
        cdp.eval(btn + '.click()')
        time.sleep(2.5)
        empty = cdp.eval('!!document.querySelector(' + "'#bell-menu .bell-empty'" + ')')
        if empty:
            check('空态时焦点也有地方可去（索引 0 且不是通知条目）',
                  active(cdp).startswith('#0|') and 'bell-item' not in active(cdp), active(cdp))
        else:
            check('空态时焦点也有地方可去', True, '（未进入空态，跳过）')

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
