#!/usr/bin/env python3
"""F-03 真浏览器验收：资产工坊轮询在后台标签页必须不发请求，回前台要补一拍。

用真实视图 + 真实在飞作业（不是同页仿写两个探针）：
  * 后台期间 /asset-generations/<id> 请求数必须为 0；
  * 回前台后 1 秒内必须补一拍（>0），否则只是"跳过"没有"补拍"；
  * 反向对照 --reverse：去掉 document.hidden 早退，后台请求数必须 >0。
"""
from __future__ import annotations
import json, subprocess, sys, time, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H
import model_center_browser_check as MC

REVERSE = '--reverse' in sys.argv
ROOT = Path(__file__).resolve().parents[1]
AS = ROOT / 'app' / 'frontend' / 'js' / 'views' / 'asset-studio.js'
BAK = ROOT / 'tools' / '_as.bak'

results = []
def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print(('  OK   ' if ok else '  FAIL ') + name + ('  :: ' + str(detail) if detail else ''), flush=True)

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
        import json as J0
        from db import get_conn
        uid = MC._admin_id()
        with get_conn() as conn:
            pids = [r['id'] for r in conn.execute("SELECT id FROM project").fetchall()]
            if not pids:
                pids = [conn.execute(
                    "INSERT INTO project (name, description, owner_id) VALUES (?,?,?)",
                    ('验收项目', '前端审计 F-03', uid)).lastrowid]
            aid = jid = pid = None
            for pid in pids:
                aid = conn.execute(
                    "INSERT INTO asset (project_id, kind, name, summary, consistency_prompt)"
                    " VALUES (?,?,?,?,?)",
                    (pid, 'character', '验收角色', '摘要', '一致性提示词')).lastrowid
                refs = J0.dumps(['op:character_profile', 'mode:full', 'asset:' + str(aid)])
                jid = conn.execute(
                    "INSERT INTO job (project_id, capability, kind, execution_mode,"
                    " provider_code, model_code, prompt_snapshot, params_json,"
                    " input_refs_json, status)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pid, 'text', 'text', 'cloud', 'deepseek', 'deepseek-flash',
                     '验收提示词', '{}', refs, 'running')).lastrowid
        print('seeded project', pid, 'asset', aid, 'job', jid, flush=True)

        import proto_cdp_check as P
        chrome = MC.start_chrome(P.PORT)
        cdp = P.CDP(P.wait_for_target())
        cdp.cmd('Page.enable')
        cdp.cmd('Network.enable')
        cdp.cmd('Page.addScriptToEvaluateOnNewDocument', source=MC.COLLECT_ERRORS)
        cdp.cmd('Network.setCookie', name='fv_session', value=token,
                url=MC.BASE, path='/', httpOnly=True)

        if REVERSE:
            assert not BAK.exists()
            BAK.write_bytes(AS.read_bytes())
            t = AS.read_text(encoding='utf-8')
            old = "    if (document.hidden) return;\n    tick();"
            assert t.count(old) == 1
            AS.write_text(t.replace(old, "    tick();  // REVERSE"), encoding='utf-8')
            assert 'REVERSE' in AS.read_text(encoding='utf-8')
            print('REVERSE CONTROL 已落下（去掉 hidden 早退）', flush=True)

        # 计数器：只数轮询那一条端点。正则用 [0-9]+ 而不是 \d——
        # 这段源码经 Python 字符串再进 JS，反斜杠层数容易吃掉一个。
        cdp.cmd('Page.addScriptToEvaluateOnNewDocument', source="""
window.__hits = 0; window.__all = [];
const _f = window.fetch;
window.fetch = function(u, ...rest) {
  const s = String(typeof u === 'string' ? u : (u && u.url) || '');
  if (s.indexOf('/api/') === 0 || s.indexOf('/api/') > 0) window.__all.push(s);
  if (/asset-generations[/][0-9]+/.test(s)) window.__hits++;
  return _f.call(this, u, ...rest);
};
""")
        MC.go(cdp, MC.BASE + '/#assets/1')
        time.sleep(2.5)
        # 选中那条资产，让在飞作业被认出来并起轮询
        state = cdp.eval("""(() => {
          const sel = document.querySelector('#as-target') || document.querySelector('select');
          return JSON.stringify({hasCands: !!document.querySelector('#as-cands'),
            selId: sel ? sel.id : null,
            opts: sel ? Array.from(sel.options).map(o=>o.value) : []});
        })()""")
        print('VIEW ' + state, flush=True)
        st = json.loads(state)
        check('资产工坊渲染出候选区（轮询判活锚点）', st['hasCands'], st)
        if st['selId']:
            # 选下拉里真实存在的那条资产（种子里给每个项目都建了一条，
            # 页面当前项目是哪一个由 store 决定，不能拿种子的最后一个 id 硬填）。
            opt = next((v for v in st['opts'] if v != '0'), None)
            assert opt, '下拉里没有资产可选'
            cdp.eval("(() => {const s=document.getElementById('" + st['selId'] + "');"
                     "s.value='" + opt + "';s.dispatchEvent(new Event('change',{bubbles:true}));})()")
            time.sleep(8.0)
            after = cdp.eval("""(() => JSON.stringify({
              chip: document.getElementById('as-jobchip-text')?.textContent.trim() || null,
              hits: window.__hits}))()""")
            print('AFTER SELECT ' + str(after), flush=True)
            print('REQS ' + str(cdp.eval('JSON.stringify((window.__all||[]).slice(-14))')), flush=True)
        print('VIS ' + str(cdp.eval('document.visibilityState')), flush=True)
        base = cdp.eval('window.__hits')
        print('foreground hits(after mount) =', base, flush=True)
        check('前台轮询真的在跑（先证明探针有效）', base > 0, base)

        # 真把标签页挤到后台
        cdp.cmd('Target.createTarget', url='about:blank')
        time.sleep(1.0)
        hidden = cdp.eval('document.hidden')
        check('标签页真的进了后台', hidden is True, hidden)
        cdp.eval('window.__hits = 0')
        time.sleep(7.0)   # 3 秒一轮 → 不修的话至少 2 拍
        bg = cdp.eval('window.__hits')
        print('BACKGROUND hits over 7s =', bg, flush=True)
        check('F-03 后台 7 秒内 0 次轮询请求', bg == 0, bg)

        # 回到前台：必须补一拍
        cdp.eval('window.__hits = 0')
        cdp.cmd('Page.bringToFront')
        time.sleep(1.5)
        fg = cdp.eval('window.__hits')
        print('FOREGROUND hits within 1.5s =', fg, flush=True)
        check('F-03 回前台立刻补一拍（不是等下一轮）', fg > 0, fg)

        errs = cdp.eval('JSON.stringify(window.__errs || [])')
        check('无运行时错误', errs in ('[]', 'null'), errs)
    finally:
        if REVERSE and BAK.exists():
            AS.write_bytes(BAK.read_bytes()); BAK.unlink()
            assert 'REVERSE' not in AS.read_text(encoding='utf-8')
            print('已还原 asset-studio.js', flush=True)
        if chrome: H.stop(chrome)
        H.stop(proc)
    bad = [r for r in results if not r[1]]
    print(f'\n=== {len(results)-len(bad)}/{len(results)} PASS ===', flush=True)
    return 1 if bad else 0

if __name__ == '__main__':
    sys.exit(main())