# -*- coding: utf-8 -*-
'''启动按钮的真浏览器验收。

重点不是「点下去能不能起来」，而是几个容易撒谎的地方：
  1. 服务没答话时按钮才出现（在跑的时候不该有第二个启动入口）；
  2. 点了之后会显示「启动中…」，不是一直静默；
  3. 结果把「起来了」与「端口答话了」分开说；
  4. 真正起来之后再点一次，后端不重复拉起（说「本来就在跑」）。

只对 ollama 做真实启动（`ollama serve` 在本机已装且 11434 空闲）；
Draw Things 会弹出 GUI 窗口，不适合在验收里反复拉，只验按钮形状。
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

def main():
    MC.prepare_env()
    token = MC.mint_admin_session()
    proc = subprocess.Popen(
        [str(H.backend_python()), '-m', 'uvicorn', 'main:app',
         '--host', '127.0.0.1', '--port', str(MC.PORT), '--log-level', 'warning'],
        cwd=str(MC.BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    chrome = None
    ollama_started = False
    try:
        MC.wait_server()
        # 副本库里放两条实例：一条 ollama（11434 空闲）、一条 draw_things。
        from db import get_conn
        with get_conn() as conn:
            conn.execute('DELETE FROM engine_instance')
            for code, ep in (('ollama', 'http://127.0.0.1:11434'),
                             ('draw_things', 'http://127.0.0.1:7860')):
                conn.execute('INSERT INTO engine_instance (engine_code, display_name, endpoint) '
                             'VALUES (?, ?, ?)', (code, code, ep))

        import proto_cdp_check as P
        chrome = MC.start_chrome(P.PORT)
        cdp = P.CDP(P.wait_for_target())
        cdp.cmd('Page.enable')
        cdp.cmd('Page.addScriptToEvaluateOnNewDocument', source=MC.COLLECT_ERRORS)
        cdp.cmd('Network.setCookie', name='fv_session', value=token,
                url=MC.BASE, path='/', httpOnly=True)

        # 引擎管理是模型中心的**子页 1**（subs: 模型仓库/引擎管理/调用策略/运行监控）。
        # 写成 /3 会落到「运行监控」，那一页没有引擎实例表。
        MC.go(cdp, MC.BASE + '/#models/1')
        time.sleep(3.5)
        # 切到引擎管理子页（第 3 个二级页）
        time.sleep(1.0)

        # 直接查接口，先确认 launch_ready 的口径对了
        api = json.loads(cdp.eval(
            "fetch('/api/engines').then(r => r.json()).then(d => JSON.stringify(d))"))
        by_code = {i['engine_code']: i for i in api['items']}
        check('两台实例都在列表里', set(by_code) == {'ollama', 'draw_things'}, list(by_code))
        for code in ('ollama', 'draw_things'):
            it = by_code[code]
            check(f'{code} 没答话时 launch_ready 为真', it['launch_ready'] is True,
                  {'health': it['health_state'], 'ready': it['launch_ready']})
            check(f'{code} 带上了启动说明（launch_note）', 'launch_note' in it, it.get('launch_note'))

        # 页面上的按钮
        n_btn = cdp.eval("document.querySelectorAll('[data-act-launch]').length")
        check('页面上出现启动按钮', n_btn == 2, n_btn)

        # 点 ollama 那颗，观察「启动中…」与最终结果
        clicked = cdp.eval('''(() => {
          const rows = [...document.querySelectorAll('tr.engine-inst')];
          const row = rows.find(r => r.textContent.includes('ollama'));
          if (!row) return 'no-row';
          const b = row.querySelector('[data-act-launch]');
          if (!b) return 'no-btn';
          b.click();
          return 'clicked';
        })()''')
        check('点到了 ollama 的启动按钮', clicked == 'clicked', clicked)

        busy = None
        for _ in range(20):
            time.sleep(0.25)
            label = cdp.eval('''(() => {
              const rows = [...document.querySelectorAll('tr.engine-inst')];
              const row = rows.find(r => r.textContent.includes('ollama'));
              const b = row && row.querySelector('[data-act-launch]');
              return b ? b.textContent.trim() : 'gone';
            })()''')
            if label == '启动中…':
                busy = label
                break
        check('点击后按钮显示「启动中…」（有可见反馈）', busy == '启动中…', busy)

        seen = []
        TOAST_JS = (
            "(() => {"
            "  const el = [...document.querySelectorAll('div.chip.amber')]"
            "    .find(e => e.style && e.style.position === 'fixed');"
            "  return el ? el.textContent.trim() : '';"
            "})()"
        )
        for _i in range(40):
            time.sleep(0.25)
            txt = cdp.eval(TOAST_JS)
            if txt and txt not in seen:
                seen.append(txt)
            if seen and _i > 4:
                break
        toast = ' / '.join(seen)
        print('TOAST: ' + str(toast)[:200], flush=True)
        check('结果提示出来了', bool(toast), toast)
        check('提示区分了「启动」与「能用了」（不是一句好了）',
              ('启动' in str(toast)) or ('答话' in str(toast)), toast)

        # 真起来了吗？直接问接口
        after = json.loads(cdp.eval(
            "fetch('/api/engines').then(r => r.json()).then(d => JSON.stringify(d))"))
        ol = next(i for i in after['items'] if i['engine_code'] == 'ollama')
        print('AFTER: ' + json.dumps({'health': ol['health_state'],
                                     'ready': ol['launch_ready']}, ensure_ascii=False), flush=True)
        if ol['health_state'] == 'ok':
            ollama_started = True
            check('ollama 真的起来了（接口说 ok）', True, ol['health_state'])
            check('起来了之后 launch_ready 变假（不再给第二个启动入口）',
                  ol['launch_ready'] is False, ol['launch_ready'])
            btn_after = cdp.eval('''(() => {
              const rows = [...document.querySelectorAll('tr.engine-inst')];
              const row = rows.find(r => r.textContent.includes('ollama'));
              return row ? row.querySelectorAll('[data-act-launch]').length : -1;
            })()''')
            check('页面上 ollama 那一行的启动按钮已消失', btn_after == 0, btn_after)
        else:
            check('ollama 真的起来了（接口说 ok）', False, ol['health_state'])
            check('起来了之后 launch_ready 变假', False, '未起来，跳过')
            check('页面上启动按钮已消失', False, '未起来，跳过')


        # ---- 「已经起来了之后再点一次」：后端必须不重复拉起（第 3 条边界） ----
        # 只测第一点击是抓不住这条的：那时服务本来就没在跑，拦不拦都看不出来。
        if ol['health_state'] == 'ok':
            second = json.loads(cdp.eval(
                "fetch('/api/engines').then(r => r.json()).then(d => JSON.stringify(d))"))
            before_hl = len([i for i in second['items'] if i['engine_code'] == 'ollama'])
            out2 = json.loads(cdp.eval(
                "fetch('/api/engines/' + " + str(ol['id']) + " + '/launch', {method: 'POST'})"
                "  .then(r => r.json()).then(d => JSON.stringify(d))"))
            print('SECOND CLICK: ' + json.dumps(out2, ensure_ascii=False)[:200], flush=True)
            check('再点一次：后端说本来就在跑（不重复拉起）',
                  out2.get('started') is False and out2.get('reachable') is True, out2)
            check('再点一次：提示里点明没有重复启动',
                  '没有重复启动' in str(out2.get('message', '')), out2.get('message'))
            check('再点一次：实例没被复制出第二行', before_hl == 1, before_hl)

        # ---- 「起了但没答话」必须如实报，不能把命令发出当成能用了 ----
        # 用一个一定起不来监听的服务验：`ollama serve` 配上坏端口不行（配方是常量），
        # 所以直接对 engine_launch 造一次：临时把端口指到一个没人听的地方。
        dt = next(i for i in after['items'] if i['engine_code'] == 'draw_things')
        if dt['health_state'] != 'ok':
            out3 = json.loads(cdp.eval(
                "fetch('/api/engines/' + " + str(dt['id']) + " + '/launch', {method: 'POST'})"
                "  .then(async r => JSON.stringify({status: r.status, body: await r.json()}))"))
            print('DRAW THINGS LAUNCH: ' + json.dumps(out3, ensure_ascii=False)[:260], flush=True)
            body3 = out3.get('body') or {}
            # 起不来（macOS 上 GUI 应用在无头环境里 open 会失败）或起来了但没答话，
            # 两种都必须 **不能** 报 reachable=true。
            check('Draw Things：起了也不算能用（reachable 必须为假）',
                  body3.get('reachable') is not True, body3)
            check('Draw Things：失败原因里带上「API Server 要手动开」这条能动手的提示',
                  'API Server' in str(body3.get('message', '')), body3.get('message'))

        errs = cdp.eval('JSON.stringify(window.__errs || [])')
        check('无运行时错误', errs in ('[]', 'null'), errs)
    finally:
        if chrome: H.stop(chrome)
        H.stop(proc)
        if ollama_started:
            subprocess.run(['pkill', '-f', 'ollama serve'], capture_output=True)
            print('（已停掉验收拉起的 ollama）', flush=True)
    bad = [r for r in RESULTS if not r[1]]
    print(chr(10) + '=== %d/%d PASS ===' % (len(RESULTS) - len(bad), len(RESULTS)), flush=True)
    return 1 if bad else 0

if __name__ == '__main__':
    sys.exit(main())
