"""引擎选择与启动等待的真实浏览器回归；API 模拟，不启动真实模型或应用。

变异只作用于临时前端副本：--mutate-options 隐去其他引擎，
--mutate-wait 跳过冷启动后的等待。对应断言必须失败。
"""
import json
import shutil
import sys
import time

import _harness as H
from proto_cdp_check import CDP

MUTATIONS = {
    '--mutate-options': (
        'const providers = providerLists[`${r.capability}|${r.backend}`] || [];',
        'const providers = (providerLists[`${r.capability}|${r.backend}`] || []).filter((a) => a.code === r.provider);',
    ),
    '--mutate-wait': (
        'if (!out?.started || out.reachable) return out;',
        'return out;',
    ),
}
frontend = H.FRONTEND
for flag, (old, new) in MUTATIONS.items():
    if flag in sys.argv:
        frontend = H.temp_dir('fv-engine-mutation-') / 'frontend'
        shutil.copytree(H.FRONTEND, frontend)
        source = frontend / 'js/views/models/engines.js'
        text = source.read_text()
        assert text.count(old) == 1
        source.write_text(text.replace(old, new, 1))
        assert new in source.read_text()
port, debug = H.free_port(), H.free_port()
server = H.serve_static(frontend, port)[0]
chrome = H.launch_chrome(debug, H.temp_dir('fv-engine-setup-'))
try:
    c = CDP(H.wait_cdp(debug))
    c.cmd('Page.enable')
    c.cmd('Runtime.enable')
    c.cmd('Page.navigate', url=f'http://127.0.0.1:{port}/css/app.css')
    time.sleep(0.3)
    result = c.eval(r"""(async () => {
 const {cache, MODALS} = await import('/js/views/models/shared.js');
 const E = await import('/js/views/models/engines.js');
 const {api} = await import('/js/api.js');
 const {applyPicks} = await import('/js/pick.js');
 const errors=[];window.addEventListener('unhandledrejection',e=>errors.push(String(e.reason)));
 window.addEventListener('error',e=>errors.push(String(e.message)));
 const pause=ms=>new Promise(r=>setTimeout(r,ms));
 const until=async(test)=>{for(let i=0;i<70;i++){if(test())return;await pause(50);}};
 const adapters=[
  {code:'mflux',label:'mflux',capability:'image',backend:'local',source:'test',params_known:true,models:[{code:'flux1-schnell',label:'FLUX',params:[]}]},
  {code:'draw_things',label:'Draw Things',capability:'image',backend:'local',source:'test',params_known:true,models:[{code:'noobai-xl-epsilon-11',label:'noobaiXL',params:[]}]},
  {code:'unlisted_models',label:'手填模型',capability:'image',backend:'local',source:'test',params_known:false,models:[]}
 ];
 cache.manifest={adapters};
 const row={id:1,capability:'image',capability_label:'图像',backend:'local',backend_label:'本地',provider:'mflux',model:'flux1-schnell',enabled:true,is_default:true,health:{state:'unknown'},validation:{},notes:'',key_env_custom:'',version:'v1'};
 cache.providers=[row];
 document.body.innerHTML='<div id="view-root"></div>';
 const root=document.querySelector('#view-root');
 const renderSlot=()=>{root.innerHTML='<button id="save-providers">保存</button><span id="save-state" hidden></span><table><tbody>'+E.slotRow(row,{'image|local':adapters})+E.slotDetail(row,adapters[0])+'</tbody></table>'+MODALS;E.mountEngines(root);};
 renderSlot();
 document.documentElement.dataset.theme='night';applyPicks(root);
 const provider=root.querySelector('[data-f="provider"]');
 const options=[...provider.options||[]].map(x=>x.value);
 const out={allProviders:provider.tagName==='SELECT'&&options.includes('draw_things')&&options.includes('mflux')};
 provider.value='draw_things';provider.dispatchEvent(new Event('change',{bubbles:true}));
 applyPicks(root);
 const model=root.querySelector('[data-f="model"]');
 out.drawModel=model.tagName==='SELECT'&&model.value==='noobai-xl-epsilon-11'&&![...model.options||[]].some(x=>x.value==='flux1-schnell');
 out.nightModel=model.closest('.fv-pick')?.querySelector('.fv-pick-btn').textContent==='noobaiXL'&&!root.querySelector('.fv-pick .fv-pick');
 let sent=null;api.saveProviders=async rows=>{sent=structuredClone(rows)};api.providers=async()=>sent||[row];
 root.querySelector('#save-providers').click();await until(()=>sent!==null);
 out.saveExpandedLocal=sent?.[0]?.provider==='draw_things'&&sent[0].model==='noobai-xl-epsilon-11'&&sent[0].enabled&&sent[0].is_default&&sent[0].seen_version==='v1';
 provider.value='mflux';provider.dispatchEvent(new Event('change',{bubbles:true}));
 out.switchBack=root.querySelector('[data-f="model"]').value==='flux1-schnell';
 provider.value='unlisted_models';provider.dispatchEvent(new Event('change',{bubbles:true}));
 out.manualModel=root.querySelector('[data-f="model"]').tagName==='INPUT';
 provider.value='__manual__';provider.dispatchEvent(new Event('change',{bubbles:true}));
 const custom=root.querySelector('[data-f="provider"]');custom.value='custom-new';custom.dispatchEvent(new Event('input',{bubbles:true}));
 out.manualProvider=custom.tagName==='INPUT'&&custom.value==='custom-new'&&root.querySelector('[data-f="model"]').tagName==='INPUT';
 const historical={...row,provider:'custom-provider',model:'custom-model'};
 const box=document.createElement('table');box.innerHTML=E.slotRow(historical,{'image|local':adapters});
 out.preserveUnknown=box.querySelector('[data-f="provider"]').value==='custom-provider'&&box.querySelector('[data-f="model"]').value==='custom-model';
 let reachable=false,probes=0,launches=0,refreshes=0;
 const instance=()=>({id:7,engine_code:'draw_things',display_name:'Draw Things',enabled:true,endpoint:'http://127.0.0.1:7860',connection_type:'local_http',health_state:reachable?'ok':'missing',health_detail:reachable?'连接成功':'服务启动中',launch_ready:!reachable});
 const payload=()=>({items:[instance()],service_engines:['draw_things']});
 api.engines=async()=>payload();
 api.launchEngine=async()=>{launches++;return {started:true,reachable:false,message:'应用还在启动'}};
 api.probeEngine=async()=>{probes++;reachable=probes>=2;return {state:reachable?'ok':'missing'}};
 window.addEventListener('app:refresh',()=>{refreshes++;cache.engines=payload();root.innerHTML=E.engineInstancesCard();E.mountEngineInstances(root)});
 root.innerHTML='<button data-act-launch="7">启动服务</button>';
 E.mountEngineInstances(root);root.querySelector('[data-act-launch]').click();await pause(20);
 out.busy=root.querySelector('[data-act-launch]')?.textContent==='等待连接…';
 await until(()=>refreshes>0);
 out.autoReady=launches===1&&probes===2&&refreshes===1&&root.textContent.includes('就绪')&&!root.querySelector('[data-act-launch]');
 reachable=false;probes=0;launches=0;
 E.openProbeResults(payload(),[]);
 const modal=document.querySelector('.probe-results');
 modal.querySelector('[data-act-launch]').click();await pause(20);
 out.modalBusy=modal.querySelector('[data-act-launch]')?.textContent==='等待连接…';
 await until(()=>modal.querySelector('[data-launch-notice]'));
 out.modalReady=modal.isConnected&&launches===1&&probes===2&&modal.textContent.includes('连接成功')&&!modal.querySelector('[data-act-launch]');
 modal.querySelector('[data-close-probe-res]').click();
 reachable=false;probes=0;launches=0;
 api.launchEngine=async()=>{launches++;return {started:false,reachable:false,message:'未启动'}};
 root.innerHTML='<button data-act-launch="7">启动服务</button>';E.mountEngineInstances(root);root.querySelector('[data-act-launch]').click();await pause(30);
 out.noFalseReady=launches===1&&probes===0&&!root.textContent.includes('就绪');
 // 关闭结果窗即终止后续检查，不能在后台继续刷新已离开的页面。
 api.launchEngine=async()=>({started:true,reachable:false});
 E.openProbeResults(payload(),[]);const closed=document.querySelector('.probe-results');
 closed.querySelector('[data-act-launch]').click();await pause(20);closed.querySelector('[data-close-probe-res]').click();await pause(1100);
 out.cancelOnClose=probes===0;
 const beforeLeave=refreshes;
 root.innerHTML='<button data-act-launch="7">启动服务</button>';E.mountEngineInstances(root);root.querySelector('[data-act-launch]').click();await pause(20);
 root.innerHTML='<p>已切换页面</p>';await pause(1100);
 out.cancelOnLeave=probes===0&&refreshes===beforeLeave&&root.textContent==='已切换页面';
 out.noErrors=errors.length===0;out.errors=errors;
 return out;
})()""")
    print(json.dumps(result, ensure_ascii=False))
    checks = {key: value for key, value in result.items() if key != 'errors'}
    for key, passed in checks.items():
        print(('  OK   ' if passed else '  FAIL ') + key)
    assert all(checks.values()), result
    print(f'{len(checks)}/{len(checks)} 通过')
finally:
    H.stop(chrome)
    server.shutdown()
