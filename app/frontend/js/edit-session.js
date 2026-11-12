/** One shared gate for navigation and context changes. Views own their save operation. */
import { bindModalDismiss } from './util.js';
let session = null;
let pending = null;
export function setEditSession(value) { session = value; }
export function guardEdit(target) {
  if (!session?.dirty() || session.matches(target)) return Promise.resolve(true);
  if (pending) return pending.then(() => guardEdit(target));
  const active = session;
  pending = new Promise((resolve) => {
    const host = document.createElement('div');
    host.className = 'modal';
    host.innerHTML = `<div class="modal-box"><div class="modal-head neutral">正文尚未保存</div><div class="modal-body"><p>离开前如何处理当前改动？</p><p role="status" data-edit-error></p></div><div class="modal-actions"><button class="btn" data-cancel>取消</button><button class="btn" data-discard>丢弃</button><button class="btn primary" data-save>保存后离开</button></div></div>`;
    document.body.append(host);
    const done = (ok) => { if (!ok) active.cancel?.(); host.remove(); resolve(ok); };
    let busy = false;
    const cancel = () => { if (!busy) done(false); };
    host.querySelector('[data-cancel]').onclick = cancel;
    host.querySelector('[data-discard]').onclick = () => { active.discard(); done(true); };
    host.querySelector('[data-save]').onclick = async () => {
      busy = true;
      host.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      try {
        await active.save();
        if (!active.dirty()) { done(true); return; }
        host.querySelector('[data-edit-error]').textContent = '正文尚未全部保存，请重试或取消。';
      } catch (err) { host.querySelector('[data-edit-error]').textContent = err.message; }
      busy = false;
      host.querySelectorAll('button').forEach((b) => { b.disabled = false; });
    };
    bindModalDismiss(host, cancel);
    host.querySelector('[data-cancel]').focus();
  }).finally(() => { pending = null; });
  return pending;
}
