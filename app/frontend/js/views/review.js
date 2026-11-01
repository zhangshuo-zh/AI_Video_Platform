/**
 * 审核中心（W14）：校验器 gate 与按锚点签字。
 *
 * 这一页是 Review 的唯一写入口：跑规则、看报告、签字都在这里。
 * 创作中心只显示状态，不放签字按钮——同一个动作两个入口迟早分叉。
 */
import { api } from '../api.js';
import { store } from '../store.js';
import { escapeHtml, run } from '../util.js';

let openDocId = null;
let signNote = '';
const GATE_LABEL = {
  none: ['未校验', 'gray'], passed: ['通过', 'green'], failed: ['未通过', 'red'],
  stale: ['已失配', 'amber'],
};

export default {
  async render() {
    const project = store.current;
    if (!project) {
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div>
        <p>审核按项目汇总；去项目中心选一个再回来。</p>
      </div></div></section>`;
    }
    let queue = [];
    let err = null;
    try { queue = await api.reviewQueue(project.id); } catch (e) { err = e; }
    if (openDocId && !queue.some((q) => q.doc_id === openDocId)) openDocId = null;
    const canSign = store.can('review:sign');

    const rows = queue.map((q) => {
      const [label, tone] = GATE_LABEL[q.gate_status] || [q.gate_status, 'gray'];
      return `<tr data-review-doc="${q.doc_id}" class="${q.doc_id === openDocId ? 'sel' : ''}">
        <td><b>${escapeHtml(q.doc_title)}</b>
          <span class="t-desc">${escapeHtml(q.kind_label)}${q.episode_title ? ' · ' + escapeHtml(q.episode_title) : ' · 全项目共用'}</span></td>
        <td><span class="chip ${tone}">${escapeHtml(q.gate_status_label)}</span>
          ${q.stale ? '<span class="t-desc">正文改过，报告作废</span>' : ''}</td>
        <td class="num">${q.findings ? `${q.findings} 条` : '—'}</td>
        <td><span class="t-bad">${q.errors}</span> / <span class="t-warn">${q.warnings}</span></td>
        <td class="num">${q.signed}</td>
        <td class="r">${canSign && q.gate_status !== 'none'
          ? `<button class="btn sm" data-rerun="${q.doc_id}">重跑</button>` : ''}
          <button class="btn sm primary" data-open-review="${q.doc_id}">查看</button></td>
      </tr>`;
    }).join('');

    return `<section class="view on">
      <div class="page-head">
        <h1>审核中心</h1>
        <p>校验器 gate + 人工签字 · ${escapeHtml(project.name)}</p>
        <div class="right"><span class="chip plain">${queue.length} 份文档</span></div>
      </div>
      ${err ? `<div class="card"><div class="bar bad">读不到审核队列：${escapeHtml(err.message)}</div></div>` : ''}
      <div class="card">
        <div class="card-head"><h2>审核队列</h2>
          <span class="sub">报告绑定正文版本；正文一改，旧报告与旧签字都不算新版本</span></div>
        ${queue.length ? `<div class="table-wrap"><table>
          <thead><tr><th>文档</th><th>最新校验</th><th>问题</th><th>错误 / 警告</th>
            <th class="r">已签</th><th class="r"></th></tr></thead>
          <tbody>${rows}</tbody>
        </table></div>` : `<div class="empty"><div class="empty-t">还没有可审的文档</div>
          <p>小说／剧本建好并写出正文后，就会出现在这里。</p></div>`}
      </div>
      ${openDocId ? '<div id="review-detail" class="card" style="margin-top:14px"><div class="card-body"><p class="hint">正在读报告…</p></div></div>' : ''}
      ${canSign ? '' : '<p class="hint">你只有查看权限；签字需要 review:sign。</p>'}
    </section>`;
  },

  async mount(root) {
    if (!store.current) return;
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    root.querySelectorAll('[data-open-review]').forEach((btn) => {
      btn.addEventListener('click', () => {
        openDocId = Number(btn.dataset.openReview);
        signNote = '';
        refresh();
      });
    });
    root.querySelectorAll('[data-rerun]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const id = Number(btn.dataset.rerun);
        const ok = await run('跑校验', () => api.runReviewGate(id));
        if (!ok) return;
        openDocId = id;
        refresh();
      });
    });
    const detail = root.querySelector('#review-detail');
    if (!detail || !openDocId) return;

    let gate = null;
    let err = null;
    try { gate = await api.reviewGate(openDocId); } catch (e) { err = e; }
    if (err) {
      detail.innerHTML = `<div class="card-body"><div class="bar bad">
        读不到报告：${escapeHtml(err.message)}</div></div>`;
      return;
    }
    // 第一次打开且还没有报告：给一个明确的入口，不逼人先去别处点
    if (!gate.id) {
      detail.innerHTML = `<div class="card-head"><h2>${escapeHtml(gate.doc_title)}</h2>
        <span class="sub">还没有校验报告</span></div>
        <div class="card-body">
          <p class="modal-meta">校验只跑三条确定性规则：占位词、超长句、没有收尾标点。
            不调模型、不花钱。</p>
          <p><button class="btn primary" id="review-first">跑第一遍校验</button></p>
        </div>`;
      detail.querySelector('#review-first')?.addEventListener('click', async () => {
        const ok = await run('跑校验', () => api.runReviewGate(openDocId));
        if (ok) refresh();
      });
      return;
    }

    const canSign = store.can('review:sign') && gate.can_sign;
    const signs = new Map(gate.signatures.map((s) => [s.anchor, s]));
    const findings = gate.findings.length ? gate.findings.map((f) => {
      const signed = signs.get(f.anchor);
      const tone = f.severity === 'error' ? 'red' : 'amber';
      return `<div class="rev-finding">
        <div class="row" style="justify-content:space-between;gap:8px">
          <div><span class="chip ${tone}">${f.severity === 'error' ? '错误' : '警告'}</span>
            <span class="mono t-dim">${escapeHtml(f.anchor)}</span>
            ${escapeHtml(f.message)}</div>
          ${signed ? `<span class="chip ${signed.decision === 'approved' ? 'green' : signed.decision === 'rejected' ? 'red' : 'amber'}">
            ${escapeHtml(signed.decision_label)} · ${escapeHtml(signed.signed_by_name)}</span>`
          : '<span class="chip gray">未签</span>'}
        </div>
        <blockquote class="ex-quote">${escapeHtml(f.quote || '')}</blockquote>
        ${canSign ? `<div class="row" style="gap:6px;margin-top:6px">
          <button class="btn sm" data-sign="approved" data-anchor="${f.anchor}">通过</button>
          <button class="btn sm" data-sign="returned" data-anchor="${f.anchor}">退回</button>
          <button class="btn sm danger" data-sign="rejected" data-anchor="${f.anchor}">驳回</button>
        </div>` : ''}
        ${signed?.note ? `<p class="t-desc">备注：${escapeHtml(signed.note)}</p>` : ''}
      </div>`;
    }).join('') : '<p class="hint" style="text-align:left">这次校验没有发现问题。</p>';

    const docSign = signs.get('document');
    detail.innerHTML = `
      <div class="card-head">
        <h2>${escapeHtml(gate.doc_title)}</h2>
        <span class="sub">报告 #${gate.id} · ${escapeHtml(gate.created_at)} ·
          版本 ${escapeHtml((gate.base_version || '').slice(0, 10))}…</span>
        <div class="right">
          <span class="chip ${(GATE_LABEL[gate.stale ? 'stale' : gate.status] || ['?', 'gray'])[1]}">
            ${gate.stale ? '已失配' : escapeHtml(gate.status_label)}</span>
          <button class="btn sm" id="review-rerun">重跑校验</button>
        </div>
      </div>
      <div class="card-body">
        ${gate.stale ? `<div class="bar bad">正文在当前报告之后改过。旧报告与旧签字只属于旧版本；
          要签新版本，先重跑一遍。</div>` : ''}
        <p class="modal-meta">句子 ${gate.summary.sentences || 0} ·
          错误 ${gate.summary.errors || 0} · 警告 ${gate.summary.warnings || 0} ·
          已签 ${gate.counts.approved || 0} 通过 / ${gate.counts.returned || 0} 退回 / ${gate.counts.rejected || 0} 驳回</p>
        <div class="rev-list">${findings}</div>
        <div class="rev-doc-sign">
          <span class="modal-label">整份签字</span>
          ${docSign ? `<span class="chip ${docSign.decision === 'approved' ? 'green' : 'amber'}">
            ${escapeHtml(docSign.decision_label)} · ${escapeHtml(docSign.signed_by_name)}</span>` : ''}
          ${canSign ? `<div class="row" style="gap:6px;margin-top:8px">
            <input id="review-note" class="ctl" maxlength="500" style="max-width:360px"
                   placeholder="签字备注（可空）" value="${escapeHtml(signNote)}">
            <button class="btn sm" data-sign="approved" data-anchor="document">整份通过</button>
            <button class="btn sm" data-sign="returned" data-anchor="document">退回</button>
          </div>` : ''}
        </div>
      </div>`;
    detail.querySelector('#review-rerun')?.addEventListener('click', async () => {
      const ok = await run('跑校验', () => api.runReviewGate(openDocId));
      if (ok) refresh();
    });
    detail.querySelector('#review-note')?.addEventListener('input', (e) => {
      signNote = e.target.value;
    });
    detail.querySelectorAll('[data-sign]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        const ok = await run('签字', () => api.reviewSign(openDocId, {
          anchor: btn.dataset.anchor, decision: btn.dataset.sign,
          note: signNote,
        }));
        btn.disabled = false;
        if (ok) refresh();
      });
    });
  },
};
