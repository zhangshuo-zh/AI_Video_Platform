"""Browser regressions with mocked APIs. Mutation flags must fail assertions."""

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H
from proto_cdp_check import CDP

# Mutation mode works on an isolated static copy, never on production source.
MUTATIONS = {
    "--mutate-save": (
        "js/views/writing.js",
        "setDirty(ta.value !== submitted);",
        "setDirty(false);",
    ),
    "--mutate-context": ("js/store.js", "if (seq !== contextSeq) return;", ""),
}
frontend = H.FRONTEND
for flag, (file, old, new) in MUTATIONS.items():
    if flag in sys.argv:
        frontend = H.temp_dir("fv-editor-mutation-") / "frontend"
        shutil.copytree(H.FRONTEND, frontend)
        source = frontend / file
        assert old in source.read_text()
        source.write_text(source.read_text().replace(old, new, 1))
port = H.free_port()
debug = H.free_port()
server = H.serve_static(frontend, port)[0]
chrome = H.launch_chrome(debug, H.temp_dir("fv-review-"))
try:
    c = CDP(H.wait_cdp(debug))
    c.cmd("Page.enable")
    c.cmd("Runtime.enable")
    c.cmd("Page.navigate", url=f"http://127.0.0.1:{port}/css/app.css")
    time.sleep(0.5)
    result = c.eval("""(async()=>{
 const {store}=await import('/js/store.js'); const {api}=await import('/js/api.js');
 let rejectFirst; let count=0; api.setContext=()=>++count===1?new Promise((_,r)=>rejectFirst=r):Promise.resolve({});
 store.currentId=1;store.currentEpisodeId=10;store.selectEpisode(11);store.selectEpisode(12);await new Promise(r=>setTimeout(r,0));rejectFirst(new Error('late failure'));await new Promise(r=>setTimeout(r,0));
 const episodeRace={expected:12,actual:store.currentEpisodeId};
 const {setSub}=await import('/js/navstate.js');const writing=(await import('/js/views/writing.js')).default;
 store.projects=[{id:1,name:'Test',status:'active'}];store.currentId=1;store.permissions=['doc:write'];
 api.documents=async()=>[{id:21,title:'Test',status:'draft',rel_path:'test.md'}];api.docContent=async()=>({content:'old',version:'v1'});api.lockDocument=async()=>({state:'ok'});store.load=async()=>store;
 setSub('writing',0);document.body.innerHTML='<div id="view-root"></div>';const root=document.querySelector('#view-root');root.innerHTML=await writing.render();writing.mount(root);
 const ta=root.querySelector('#doc-text');ta.value='submitted';ta.dispatchEvent(new Event('input'));
 let resolveSave;let sent;api.saveDocContent=(_id,text)=>{sent=text;return new Promise(r=>resolveSave=r)};
 root.querySelector('#doc-save').click();ta.value='typed while saving';ta.dispatchEvent(new Event('input'));resolveSave({version:'v2',changed:true,updated_at:'now',git_commit:'abc'});await new Promise(r=>setTimeout(r,0));
 const saveRace={sent,current:ta.value,status:root.querySelector('#doc-dirty').textContent};
 const switchAttempt=store.select(2);await new Promise(r=>setTimeout(r,0));document.querySelector('[data-cancel]')?.click();await switchAttempt;
 const projectProtected=store.currentId===1&&ta.value==='typed while saving';
 api.documents=async()=>[{id:22,title:'Other',status:'draft',rel_path:'other.md'}];api.docContent=async()=>({content:'other document',version:'v1'});await writing.render();
 const {submissionIdentity}=await import('/js/submission.js');const identity=submissionIdentity();
 const first=identity.key({prompt:'one'});const retry=identity.key({prompt:'one'});const changed=identity.key({prompt:'two'});
 const {setEditSession,guardEdit}=await import('/js/edit-session.js');let unsaved=true;
 setEditSession({dirty:()=>unsaved,matches:()=>false,save:async()=>{unsaved=false;},discard:()=>{unsaved=false;}});
 const cancelled=guardEdit({});document.querySelector('[data-cancel]').click();const cancelledResult=await cancelled;
 const saved=guardEdit({});document.querySelector('[data-save]').click();const savedResult=await saved;
 return {episodeRace,saveRace,stableKey:first===retry,changedKey:first!==changed,projectProtected,cancelProtected:!cancelledResult,saveLeaves:savedResult&&!unsaved};
})()""")
    print(json.dumps(result, ensure_ascii=False))
    assert result["episodeRace"]["actual"] == 12
    assert result["saveRace"]["status"] == "未保存"
    assert all(
        result[k]
        for k in [
            "stableKey",
            "changedKey",
            "cancelProtected",
            "saveLeaves",
            "projectProtected",
        ]
    )
    print("7/7 通过")
finally:
    chrome.terminate()
    server.shutdown()
