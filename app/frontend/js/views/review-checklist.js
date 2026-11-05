/** 审核质量清单的一块面板（§8.2）。从审核中心抽出来共用：
 * 镜头工作台也要能就地看清单、就地签字，但不该存在第二份渲染代码——
 * 两处各画一遍，早晚会一处显示「已看 4 项」另一处显示「还差 1 项」。
 * 判据与结论都在服务端（`reviewkit.py`），这里只画。
 */
import { escapeHtml } from '../util.js';

/** 质量清单那一屏（§8.2）。自动项只读、每次服务端现算；
 *  人工项是三枚按钮——勾完才能签通过，但驳回永远不需要先填表。 */
export function checklistHtml(cl) {
  if (!cl) return '';
  const LEVEL = { pass: 'green', warn: 'amber', block: 'red' };
  const auto = (cl.auto || []).map((a) => `<li>
      <span class="chip ${LEVEL[a.level] || 'gray'}">${a.level === 'block' ? '阻塞'
        : a.level === 'warn' ? '提醒' : '通过'}</span>
      <b>${escapeHtml(a.label)}</b>
      <span class="t-desc">${escapeHtml(a.detail || '')}</span></li>`).join('');
  const STATE_BTN = [['pass', '合格'], ['issue', '不合格'], ['na', '不适用']];
  const human = (cl.human || []).map((h) => `<li>
      <div><b>${escapeHtml(h.label)}</b>${h.required ? ' <span class="chip red">必看</span>' : ''}
        <span class="chip ${h.state === 'pass' ? 'green' : h.state === 'issue' ? 'red'
          : h.state === 'na' ? 'gray' : 'amber'}">${escapeHtml(h.state_label)}</span></div>
      <p class="t-desc">${escapeHtml(h.question)}</p>
      <div class="row" style="gap:6px">
        ${STATE_BTN.map(([st, label]) => `<button class="btn xs${h.state === st ? ' primary' : ''}"
            data-check="${h.code}" data-state="${st}">${label}</button>`).join('')}
        ${h.checked_by ? `<span class="t-dim">${escapeHtml(h.checked_by)} 看过</span>` : ''}
      </div></li>`).join('');
  const c = cl.counts || {};
  return `<div class="card rev-checklist">
    <div class="card-head"><h2>质量清单</h2>
      <div class="right"><span class="chip ${cl.can_approve ? 'green' : 'amber'}">
        ${escapeHtml(cl.decision_label)}</span></div></div>
    <div class="card-body">
      <p class="modal-meta">${escapeHtml(cl.hint || '')}
        <span class="t-desc">｜自动 ${c.auto_total || 0} 项（阻塞 ${c.auto_blocking || 0}
          ／提醒 ${c.auto_warning || 0}）｜人工必看 ${c.human_required || 0} 项，
          已看 ${c.human_done || 0} 项｜判了不合格 ${c.human_issues || 0} 项</span></p>
      <h3 class="drawer-h">自动项（每次现算，不入库）</h3>
      <ul class="rev-check-list">${auto}</ul>
      <h3 class="drawer-h">人工项（勾完才能签通过；驳回不用先填）</h3>
      <ul class="rev-check-list">${human || '<li class="hint">这一类能力没有人工清单项。</li>'}</ul>
    </div></div>`;
}
