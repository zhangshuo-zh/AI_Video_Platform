#!/usr/bin/env python3
"""运行监控交互门禁。临时库、临时静态副本、临时进程目录；写操作仅浏览器模拟。

用法：app/backend/.venv/bin/python tools/monitor_interaction_browser_check.py
加 --mutations：逐项回退保护；--mutations56 仅验进程日志与历史筛选变异，
--mutation NAME 只验指定一项。所有变异均作用于临时 JS 副本，要求指定断言变红。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_center_browser_check as M
import proto_cdp_check as P

# 只在临时静态副本变异，工作区和现役服务均不改。
MUTATIONS = {
    "failure_buttons": ("    operationBusy = false;\n    mutationVersion += 1;\n    syncOperationButtons(root);\n    paintFreshness(root);", "    operationBusy = false;\n    mutationVersion += 1;\n    paintFreshness(root);"),
    "success_message": ("setMsg(root, 'mc-wp-msg', escapeHtml(out.last_action ||", "setMsg(root, 'mc-wp-msg', escapeHtml('正在启动…' || out.last_action ||"),
    "drafts": ("    restoreDrafts(root);", "    // restoreDrafts removed"),
    "single_flight": ("if (inFlight || operationBusy || generation !== pollGeneration)", "if (operationBusy || generation !== pollGeneration)"),
    "write_barrier": (" || version !== mutationVersion", ""),
    "freshness_error": ("monitorFreshness.error = failed.length ?", "monitorFreshness.error = false ?"),
    "stop_confirmation": ("if (!(await confirmDanger({ title:", "if (false && !(await confirmDanger({ title:"),
}


MUTATIONS_56 = {
    "unavailable_states": ("const daemonKnown = typeof d.enabled === 'boolean';", "const daemonKnown = true;"),
    "process_groups": ('if (p.external_online_count)', 'if (false)'),
    "log_order": ('(log.log_tail || []).slice(-logLimit)', '(log.log_tail || []).slice(-logLimit).reverse()'),
    "pause_logs": ('logFollowing ? p : logSnapshot', 'p'),
    "cleanup": ("out = await api.cleanStaleWorkers();", "out = cache.workerProcess;"),
    "model_snapshot": ("escapeHtml(v.model || '未记录模型')", "'未记录模型'"),
    "filter_rollback": ('restoreFilterControls(root, appliedFilters);', '// rollback removed'),
    "filtered_cursor": ('HISTORY_PAGE, last.id, appliedFilters', 'HISTORY_PAGE, last.id, {}'),
    "process_table": ('<span class="hint">请在原终端关闭</span>', '<span class="hint"></span>'),
    "log_levels": ("carry = LOG_LEVEL[m[3].toUpperCase()] || '';", "carry = '';"),
}
MUTATIONS.update(MUTATIONS_56)


def main() -> int:
    M.prepare_env()
    token = M.mint_admin_session()
    M.seed_db()
    front = M.TMP / 'frontend'
    shutil.copytree(M.H.FRONTEND, front)
    monitor = front / 'js/views/models/monitor.js'
    original = monitor.read_text()
    from repositories.validation import validations
    from db import get_conn
    for i in range(57):
        row = validations.record({"capability": "text", "backend": "local",
            "provider": "gate-provider" if i != 54 else "gate-other",
            "model": "gate-a" if i != 55 else "gate-b", "kind": "env_check",
            "state": "failed" if i != 56 else "ok",
            "detail": "开始" + "证据" * 150 + "结束 <img src=x onerror=alert(1)>"})
        with get_conn() as conn:
            conn.execute("UPDATE model_validation SET created_at=? WHERE id=?",
                         ("2041-03-02 23:59:59" if i != 53 else "2041-03-03 00:00:00", row["id"]))
    # 注入配置只作用于这个临时后端，守护和 PID 都不使用现役目录。
    setup = (
        f"from pathlib import Path; import config; config.FRONTEND_DIR=Path({str(front)!r}); "
        f"import worker_supervisor as s; s.RUN_DIR=Path({str(M.TMP / 'run')!r}); "
        "s.PID_DIR=s.RUN_DIR/'workers'; s.WORKER_PID=s.RUN_DIR/'worker.pid'; "
        f"s.LOG_DIR=Path({str(M.TMP / 'logs')!r}); s.WORKER_LOG=s.LOG_DIR/'worker.log'; "
        "import worker_daemon as d; d.DAEMON_CONFIG=s.RUN_DIR/'daemon.json'; "
        f"import uvicorn; uvicorn.run('main:app',host='127.0.0.1',port={M.PORT},log_level='warning')"
    )
    service = chrome = None
    try:
        service = subprocess.Popen([str(M.H.backend_python()), '-c', setup], cwd=M.BACKEND,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        M.wait_server()
        chrome = M.start_chrome(P.PORT)
        cdp = P.CDP(P.wait_for_target())
        for domain in ('Page', 'Runtime', 'Network'):
            cdp.cmd(domain + '.enable')
        cdp.cmd('Network.setCookie', name='fv_session', value=token, url=M.BASE, path='/', httpOnly=True)

        def ev(code):
            result = cdp.cmd('Runtime.evaluate', expression=code, awaitPromise=True, returnByValue=True)
            if 'exceptionDetails' in result:
                raise RuntimeError(result['exceptionDetails'])
            return result.get('result', {}).get('value')

        def fresh():
            M.go(cdp, M.BASE + '/#models/3')
            assert ev("!!document.querySelector('#mc-wp-start')"), '页面未挂载'
            ev("""(async()=>{window.api=(await import('/js/api.js')).api;window.cache=(await import('/js/views/models/shared.js')).cache;window.base=structuredClone(cache.worker);window.processBase=structuredClone(cache.workerProcess);window.daemonBase=structuredClone(cache.workerDaemon);})()""")
            # v1.52 删掉了页头那颗「立即刷新」按钮（它与 5 秒轮询是同一个 tick，
            # 点一下只省最多 3 秒）。这些时序用例仍需要一个"立刻触发一拍"的入口：
            # 取 `root.__monitorTick`（mount 时挂的，本来就是按钮的调用点），
            # 包成 `__tick()` 供下面统一使用——比"为了测试留一颗没人点的按钮"干净。
            # **必须吞掉返回值**：`__monitorTick` 是 async，而这里的 `ev()` 用了
            # `awaitPromise=True`。原来点按钮时 `.click()` 返回 undefined，谁都不等；
            # 直接 `__tick()` 会把那个 Promise 交出去，遇到用例故意挂起未 resolve 的
            # 请求（单飞、写屏障、切页丢弃三处都这么造）就会一直等到 CDP 超时。
            ev("window.__tick=()=>{document.querySelector('#view-root').__monitorTick?.();}"
               )

        def run_checks():
            checks = {}
            fresh()
            ev("api.startWorkers=async()=>{throw new Error('模拟启动失败')};document.querySelector('#mc-wp-start').click()")
            time.sleep(.1)
            checks['failure_buttons'] = ev("!document.querySelector('#mc-wp-start').disabled && document.querySelector('#mc-wp-msg').textContent.includes('模拟启动失败')")
            # 真实输入事件与真实按钮调用点；验证跨操作、轮询重绘保留草稿。
            ev("""for(const [id,value] of [['mc-conc-global','3'],['mc-daemon-desired','6']]){const el=document.getElementById(id);el.value=value;el.dispatchEvent(new Event('input',{bubbles:true}));}api.startWorkers=async()=>({...processBase,last_action:'模拟启动完成'});document.querySelector('#mc-wp-start').click()""")
            time.sleep(.1)
            checks['success_message'] = ev("document.querySelector('#mc-wp-msg').textContent==='模拟启动完成'")
            checks['drafts'] = ev("document.querySelector('#mc-conc-global').value==='3' && document.querySelector('#mc-daemon-desired').value==='6'")
            ev("api.setWorkerDaemon=async(body)=>({...daemonBase,...body}); document.querySelector('#mc-daemon-save').click()")
            time.sleep(.1)
            checks['daemon_message'] = ev("document.querySelector('#mc-daemon-msg').textContent==='守护设置已保存。'")
            checks['other_draft_survives_save'] = ev("document.querySelector('#mc-conc-global').value==='3'")

            fresh()
            ev("""window.pending=[]; api.workerStatus=()=>new Error().stack.includes('tick')?new Promise(resolve=>pending.push(resolve)):Promise.resolve(base);api.workerProcess=async()=>({...processBase,log_tail:['模拟新日志'],count:2,pids:[81,82]});api.workerDaemon=async()=>({...daemonBase,enabled:true,exposed:false,desired:2,online:2});__tick();__tick();""")
            count = ev('pending.length')
            checks['single_flight'] = count == 1
            ev('pending[0](base)')
            if count > 1:
                ev('pending[1](base)')
            time.sleep(.1)
            checks['complete_refresh'] = ev("document.querySelector('#mc-ops').textContent.includes('模拟新日志') && document.querySelector('#mc-ops').textContent.includes('守护中') && document.querySelector('#mc-wp-status').textContent.includes('2 个')")
            ev("api.workerStatus=async()=>base;api.workerProcess=async()=>{throw new Error('模拟断网')};__tick()")
            time.sleep(.1)
            checks['freshness_error'] = ev("document.querySelector('#mc-freshness').textContent.includes('连接异常') && document.querySelector('#mc-ops').textContent.includes('模拟新日志')")
            ev("api.workerProcess=async()=>processBase;__tick()")
            time.sleep(.1)
            checks['freshness_recovery'] = ev("!document.querySelector('#mc-freshness').textContent.includes('连接异常')")
            # v1.52：正常态这一行必须**没有活区、没有按钮、没有秒级时间戳**。
            # 这三条都是量出来的毛病：原来整行 role="status" 而时间戳每 5 秒变，
            # 实测 11 秒内文字变 2 次——读屏用户停在这页就是每 5 秒被念一次时间。
            checks['freshness_silent'] = ev("(()=>{const el=document.querySelector('#mc-freshness');return !!el && !el.hasAttribute('role') && !el.querySelector('button') && !/\\d{1,2}:\\d{2}:\\d{2}/.test(el.textContent)})()")
            checks['refresh_button_gone'] = ev("!document.querySelector('#mc-monitor-refresh')")
            # 异常态反过来：必须挂上 role="status" 让读屏念得出来。
            ev("api.workerProcess=async()=>{throw new Error('模拟断网')};__tick()")
            time.sleep(.15)
            checks['freshness_error_live'] = ev("document.querySelector('#mc-freshness').getAttribute('role')==='status'")
            ev("api.workerProcess=async()=>processBase;__tick()")
            time.sleep(.15)

            fresh()
            ev("""window.pending=[];api.workerStatus=()=>new Error().stack.includes('tick')?new Promise(resolve=>pending.push(resolve)):Promise.resolve(base);api.workerProcess=async()=>processBase;api.startWorkers=async()=>({...processBase,count:3,pids:[81,82,83],last_action:'新启动结果'});__tick();document.querySelector('#mc-wp-start').click();""")
            time.sleep(.1)
            ev('pending[0](base)')
            time.sleep(.1)
            checks['write_barrier'] = ev("cache.workerProcess.count===3 && document.querySelector('#mc-wp-msg').textContent==='新启动结果'")
            # 已完成的旧轮次之后，启动下一轮仍能工作。
            ev("__tick()")
            ev('pending[1](base)')
            time.sleep(.1)

            # 挂起旧请求，重挂载同页使旧锚点失效，再完成旧响应。
            ev("""window.pending=[];api.workerStatus=()=>new Error().stack.includes('tick')?new Promise(resolve=>pending.push(resolve)):Promise.resolve(base);__tick();""")
            ev("""(async()=>{const m=await import('/js/views/models/monitor.js');window.monitorRoot=document.querySelector('#mc-live').parentElement;monitorRoot.innerHTML=m.pageMonitor();m.mountMonitor(monitorRoot);})()""")
            ev("pending[0]({...base,queue:{...base.queue,queued:777}})")
            time.sleep(.1)
            checks['discard_old_mount'] = ev('cache.worker.queue.queued!==777')
            ev('pending[1](base)')
            time.sleep(.1)

            fresh()
            ev("""window.stopCalls=[];api.workerStatus=async()=>({...base,running_jobs:[{id:987}]});api.stopWorker=async(pause)=>{stopCalls.push(pause);return {...processBase,last_action:'模拟停止完成'}};document.querySelector('#mc-wp-stop').click();""")
            time.sleep(.1)
            checks['stop_confirmation'] = ev("stopCalls.length===0 && !!document.querySelector('.modal:not([hidden]) [data-d-body]') && document.querySelector('.modal:not([hidden]) [data-d-body]').textContent.includes('#987') && document.querySelector('.modal:not([hidden]) [data-d-body]').textContent.includes('暂停全局守护')")
            ev("document.querySelector('.modal:not([hidden]) [data-d-cancel]')?.click()")
            time.sleep(.1)
            checks['stop_cancel'] = ev('stopCalls.length===0 && !document.querySelector("#mc-wp-stop").disabled')
            ev("document.querySelector('#mc-wp-stop').click()")
            time.sleep(.1)
            ev("document.querySelector('.modal:not([hidden]) [data-d-ok]')?.click()")
            time.sleep(.1)
            checks['stop_pause'] = ev('stopCalls.at(-1)===true')
            ev("document.querySelector('#mc-wp-stop').click()")
            time.sleep(.1)
            ev("""document.querySelector('input[name="mc-stop-mode"][value="once"]').click()""")
            checks['stop_once_warning'] = ev("document.querySelector('.modal:not([hidden]) [data-d-body]')?.textContent.includes('再次补起')===true")
            ev("document.querySelector('.modal:not([hidden]) [data-d-ok]')?.click()")
            time.sleep(.1)
            checks['stop_once'] = ev('stopCalls.at(-1)===false')
            fresh()
            ev("""api.workerProcess=async()=>({...processBase,count:1,pids:[81],external_online_count:2,external_online_pids:[91,92],stale_pids:[999],log_status:'ok',log_tail:['Traceback','  source line','RuntimeError: failed']});__tick()""")
            time.sleep(.1)
            checks['process_groups'] = ev("['受管运行中 1 个','外部运行中 2 个','残留登记 1 条'].every(t=>document.querySelector('#mc-wp-status').textContent.includes(t))")
            checks['log_order'] = ev("JSON.stringify([...document.querySelectorAll('.wp-loglist li')].map(el=>el.textContent))===JSON.stringify(['Traceback','  source line','RuntimeError: failed'])")
            checks['process_table'] = ev("(()=>{const rows=[...document.querySelectorAll('.wp-proc-table tbody tr')];return rows.length===3 && document.querySelectorAll('.wp-proc-table [data-stop-pid]').length===1 && rows.filter(r=>r.textContent.includes('请在原终端关闭')).length===2 && !document.querySelector('#mc-wp-status').textContent.includes('PID')})()")
            ev("""window.cleanCalls=0;api.cleanStaleWorkers=async()=>{cleanCalls++;return {...processBase,count:1,pids:[81],stale_pids:[],last_action:'已清理 1 条残留 PID 登记'}};document.querySelector('#mc-wp-clean').click()""")
            time.sleep(.1)
            checks['cleanup'] = ev("cleanCalls===1 && !document.querySelector('#mc-wp-clean') && document.querySelector('#mc-wp-msg').textContent.includes('已清理 1 条')")
            ev("""api.workerProcess=async()=>({...processBase,count:1,pids:[81],log_status:'ok',log_tail:['2026-11-19 14:34:07,290 [INFO] fastvideo.worker: ok','2026-11-19 14:34:08,000 [ERROR] boom','Traceback (most recent call last):','  File "x.py", line 1','2026-11-19 14:34:09,000 [WARNING] careful']});__tick()""")
            time.sleep(.1)
            checks['log_levels'] = ev("(()=>{const li=[...document.querySelectorAll('.wp-loglist li')];return li.length===5 && li[0].className.includes('lv-info') && li[1].className.includes('lv-error') && li[2].className.includes('lv-error') && li[3].className.includes('lv-error') && li[4].className.includes('lv-warn') && li[0].textContent==='2026-11-19 14:34:07,290 [INFO] fastvideo.worker: ok'})()")
            ev("""api.workerProcess=async()=>({...processBase,count:1,pids:[81],log_status:'ok',log_tail:['第一行','第二行']});__tick()""")
            time.sleep(.1)
            ev("""document.querySelector('#mc-log-follow').click();api.workerProcess=async()=>({...processBase,count:2,pids:[81,82],log_status:'ok',log_tail:['新日志']});__tick()""")
            time.sleep(.1)
            checks['pause_logs'] = ev("document.querySelector('.wp-loglist').textContent.includes('第一行') && !document.querySelector('.wp-loglist').textContent.includes('新日志') && document.querySelector('#mc-wp-status').textContent.includes('2 个')")
            ev("document.querySelector('#mc-log-follow').click()")
            checks['resume_logs'] = ev("document.querySelector('.wp-loglist').textContent.includes('新日志')")
            ev("""window.copied='';Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async(text)=>{window.copied=text}}});document.querySelector('#mc-log-copy').click()""")
            time.sleep(.1)
            checks['copy_logs'] = ev("copied==='新日志'")
            ev("""api.workerProcess=async()=>({...processBase,log_status:'ok',log_tail:Array.from({length:100},(_,i)=>'日志'+i)});__tick()""")
            time.sleep(.1)
            checks['log_default_bound'] = ev("document.querySelectorAll('.wp-loglist li').length===12")
            ev("document.querySelector('#mc-log-more').click()")
            checks['more_logs'] = ev("document.querySelectorAll('.wp-loglist li').length===100")
            ev("""api.workerProcess=async()=>({...processBase,log_tail:[],log_status:'unreadable'});__tick()""")
            time.sleep(.1)
            checks['log_read_error'] = ev("document.querySelector('.wp-logempty').textContent.includes('读取失败') && !document.querySelector('.wp-logempty').textContent.includes('一次都没启动')")

            ev("api.workerProcess=async()=>({});api.workerDaemon=async()=>({});__tick()")
            time.sleep(.1)
            checks['unavailable_states'] = ev("document.querySelector('#mc-wp-status').textContent.includes('未取得') && document.querySelector('#mc-ops').textContent.includes('守护状态未取得')")

            fresh()
            ev("""window.historyCalls=[];window.originalHistory=api.providerValidations;api.providerValidations=async(...args)=>{historyCalls.push(args);return originalHistory(...args)};for(const [id,value] of Object.entries({'mc-val-provider':'gate-provider','mc-val-model':'gate-a','mc-val-from':'2041-03-02','mc-val-to':'2041-03-02'}))document.getElementById(id).value=value;document.querySelector('#mc-val-filter-state').value='bad';document.querySelector('#mc-val-filter-state').dispatchEvent(new Event('change',{bubbles:true}));""")
            time.sleep(.3)
            checks['server_filters'] = ev("document.querySelectorAll('#mc-val-rows>tr').length===50 && document.querySelector('.mc-val-pager').textContent.includes('共 53 条') && historyCalls.at(-1)[2].model==='gate-a' && historyCalls.at(-1)[2].dateTo==='2041-03-02'")
            checks['model_snapshot'] = ev("[...document.querySelectorAll('#mc-val-rows>tr')].every(row=>row.children[2].textContent.includes('gate-a'))")
            ev("document.querySelector('#mc-val-older')?.click()")
            time.sleep(.3)
            checks['filtered_cursor'] = ev("document.querySelectorAll('#mc-val-rows>tr').length===53 && document.querySelector('.mc-val-pager').textContent.includes('已加载 53 条') && historyCalls.at(-1)[1]>0 && historyCalls.at(-1)[2].model==='gate-a'")
            ev("document.querySelector('[data-validation-detail] summary').click()")
            checks['full_detail'] = ev("document.querySelector('[data-validation-detail]').open && document.querySelector('.mc-validation-detail').textContent.includes('结束 <img') && !document.querySelector('#mc-val-rows img')")
            ev("""api.providerValidations=async()=>{throw new Error('模拟筛选失败')};document.querySelector('#mc-val-provider').value='wrong-provider';document.querySelector('#mc-val-apply').click()""")
            time.sleep(.1)
            checks['filter_rollback'] = ev("document.querySelector('#mc-val-provider').value==='gate-provider' && document.querySelector('#mc-val-filter-state').value==='bad' && document.querySelectorAll('#mc-val-rows>tr').length===53")
            ev("api.providerValidations=originalHistory;document.querySelector('#mc-val-reset').click()")
            time.sleep(.3)
            checks['filter_reset'] = ev("document.querySelector('#mc-val-provider').value==='' && document.querySelector('#mc-val-model').value==='' && document.querySelector('#mc-val-from').value==='' && document.querySelector('#mc-val-filter-state').value==='' && document.querySelectorAll('#mc-val-rows>tr').length===50")
            cdp.cmd('Emulation.setDeviceMetricsOverride',width=380,height=850,deviceScaleFactor=1,mobile=False)
            checks['mobile_summary'] = ev("""(()=>{const row=document.querySelector('#mc-val-rows tr:has(.mc-val-slot)');const model=row.querySelector('.mc-val-mobile-model');const date=row.querySelector('.mc-val-time');return getComputedStyle(row).display==='grid' && model.getBoundingClientRect().width>0 && date.getBoundingClientRect().right<=innerWidth && !document.querySelector('#mc-val-extra').open})()""")
            checks['monitor_mobile'] = ev("document.documentElement.scrollWidth<=innerWidth")
            cdp.cmd('Emulation.clearDeviceMetricsOverride')
            fresh()
            ev("""window.alertSnapshot={...base,history_count:1,alerts:[{code:'log_silent',level:'warn',job_id:987,text:'日志静默'},{code:'worker_offline',level:'bad',text:'需要 Worker'},{code:'needs_attention',level:'warn',text:'待核对'}],running_jobs:[{id:987,model:'test-model',worker_id:'test-worker',started_seconds:75,last_log_at:'2026-11-18 10:30:00'}]};api.workerStatus=async()=>alertSnapshot;__tick()""")
            time.sleep(.2)
            checks['running_diagnostics'] = ev("['test-model','test-worker','2026-11-18 10:30:00'].every(t=>document.querySelector('#mc-running-jobs').textContent.includes(t))")
            ev("document.querySelector('#mc-worker-history').focus();alertSnapshot={...alertSnapshot,queue:{...base.queue,queued:42}};__tick()")
            time.sleep(.2)
            ev("alertSnapshot={...alertSnapshot,queue:{...base.queue,queued:43}};__tick()")
            time.sleep(.2)
            checks['poll_keeps_focus'] = ev("document.activeElement.id==='mc-worker-history' && document.querySelector('.mc-dash-grid').textContent.includes('43')")
            ev("document.querySelector('[data-monitor-target=mc-process-card]').click()")
            checks['alert_focus_target'] = ev("document.activeElement.id==='mc-process-card'")
            ev("document.querySelector('.mc-alert-card [data-goto-job]').click()")
            time.sleep(.4)
            checks['alert_job_navigation'] = ev("location.hash.startsWith('#generate') && location.hash.includes('987')")
            fresh()
            ev("api.workerStatus=async()=>({...base,alerts:[{code:'needs_attention',level:'warn',text:'待核对'}]});__tick()")
            time.sleep(.2)
            ev("document.querySelector('[data-job-status=needs_attention]').click()")
            time.sleep(.4)
            checks['alert_status_navigation'] = ev("location.hash.startsWith('#generate') && document.querySelector('#gen-status')?.value==='needs_attention'")
            fresh()
            ev("api.workerStatus=async()=>({...base,by_status:{failed:2,succeeded:17}});__tick()")
            time.sleep(.2)
            ev("document.querySelector('#mc-job-history [data-job-status=failed]').click()")
            time.sleep(.4)
            checks['history_status_navigation'] = ev("location.hash.startsWith('#generate') && document.querySelector('#gen-status')?.value==='failed'")
            return checks

        checks = run_checks()
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        if not all(checks.values()):
            return 1
        if '--mutations' in sys.argv or '--mutations56' in sys.argv or '--mutation' in sys.argv:
            for expected, (old, new) in MUTATIONS.items():
                if '--mutation' in sys.argv and expected != sys.argv[sys.argv.index('--mutation') + 1]:
                    continue
                if '--mutations56' in sys.argv and expected not in MUTATIONS_56:
                    continue
                assert old in original, expected
                monitor.write_text(original.replace(old, new, 1))
                result = run_checks()
                assert result[expected] is False, f'变异假绿：{expected}'
                print(f'变异已验红：{expected}')
                monitor.write_text(original)
        print(f'{sum(checks.values())}/{len(checks)} 通过')
        return 0
    finally:
        M.H.stop(chrome)
        M.H.stop(service)


if __name__ == '__main__':
    raise SystemExit(main())
