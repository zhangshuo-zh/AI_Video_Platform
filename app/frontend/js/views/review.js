/**
 * 审核中心（W14；0025 起也审产物）：校验器 gate、按锚点签字、产物验收。
 *
 * 这一页是 Review 的唯一写入口：跑规则、看报告、签字都在这里。
 * 创作中心与生成中心只显示状态，不放签字按钮——同一个动作两个入口迟早分叉。
 * 产线的「关键帧验收」「镜头审核」也调这一页背后那三个接口，不自己记结论。
 *
 * 两档的区别只在"版本令牌"：文档绑正文 sha256，产物绑清单摘要
 * （相对路径＋是否还在＋字节数）。被审的东西一变，旧签字就失配。
 */
import { api } from '../api.js';
import { gotoEntry } from '../goto.js';


import { store } from '../store.js';
import { currentSubTab, getSub, setSub } from '../navstate.js';
import { bindSubChips, emptyBox, escapeHtml, errorBox, run, subChipRow } from '../util.js';
import { checklistHtml } from './review-checklist.js';
import { bindActions, renderActions } from './review-actions.js';
import { DECISION_TONE } from '../validation.js';

let openDocId = null;
// 批量签字的选中集：只活在这一页，换项目就清空——
// 选中的是「这一屏的这几条」，不是一次跨页的授权
let picked = new Set();
let openJobId = null;
let signNote = '';
let jobNote = '';
const GATE_LABEL = {
  none: ['未校验', 'gray'], passed: ['通过', 'green'], failed: ['未通过', 'red'],
  stale: ['已失配', 'amber'],
};
// 三个页签的顺序必须与 js/modules.js 里 review 的 subs **一字不差**：
// 侧栏高亮读的是同一个下标（router 按它钳位），两份列表对不上就会出现
// "侧栏指着产物审核、页面显示别的"。
const HEAD_SUB = {
  0: '校验器 gate + 人工签字',
  1: '关键帧与镜头视频的产物验收',
  // 第三栏的标题把"签字只在这里落"这件事写在了名字里：
  // 智能体中心只显示状态，要签就得来这里
  2: '智能体要执行的动作，批准与驳回只在这一栏落笔',
};
const TABS = ['文档审核', '产物审核', '动作审批'];

export default {
  subIndex: 0,

  async render() {
    const project = store.current;
    if (!project) {
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div>
        <p>审核按项目汇总；去项目中心选一个再回来。</p>
      </div></div></section>`;
    }
    const tab = currentSubTab('review', TABS);
    const head = `<div class="page-head">
        <h1>审核中心</h1>
        <p>${HEAD_SUB[tab] || '审核'} · ${escapeHtml(project.name)}</p>
        <div class="row" style="gap:6px">${subChipRow(TABS, tab)}</div>
      </div>`;
    if (tab === 1) return `<section class="view on">${head}${await this.renderJobs(project)}</section>`;
    if (tab === 2) {
      return `<section class="view on">${head}${await renderActions(project)}</section>`;
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
      ${head}
      ${err ? `<div class="card"><div class="bar bad">读不到审核队列：${escapeHtml(err.message)}</div></div>` : ''}
      <div class="card">
        <div class="card-head"><h2>审核队列</h2>
          <span class="sub">报告绑定正文版本；正文一改，旧报告与旧签字都不算新版本</span></div>
        ${queue.length ? `<div class="table-wrap"><table>
          <thead><tr><th>文档</th><th>最新校验</th><th>问题</th><th>错误 / 警告</th>
            <th class="r">已签</th><th class="r"></th></tr></thead>
          <tbody>${rows}</tbody>
        </table></div>` : emptyBox('还没有可审的文档',
          '<p>小说／剧本建好并写出正文后，就会出现在这里。</p>')}
      </div>
      ${openDocId ? '<div id="review-detail" class="card" style="margin-top:14px"><div class="card-body"><p class="hint">正在读报告…</p></div></div>' : ''}
      ${canSign ? '' : '<p class="hint">你只有查看权限；签字需要 review:sign。</p>'}
    </section>`;
  },

  /**
   * 产物审核：项目里已成功的图像／视频作业，一条一行。
   * 指标全部来自服务端（产物清单、签字计数、是否失配），前端不自己算。
   */
  async renderJobs(project) {
    let queue = [];
    let err = null;
    try { queue = await api.jobReviews(project.id); } catch (e) { err = e; }
    if (openJobId && !queue.some((q) => q.job_id === openJobId)) openJobId = null;
    // 选中的作业一不在这一屏就丢掉：批量签的是「这一批看得见的」，
    // 留着跨项目、跨筛选的幽灵条目会在下一次点「批量通过」时打到不该打的作业
    picked = new Set([...picked].filter((id) => queue.some((q) => q.job_id === id)));
    const canSign = store.can('review:sign');
    const pending = queue.filter((q) => !q.counts.approved).length;

    const rows = queue.map((q) => {
      const where = [q.episode_title, q.shot_seq ? `镜 ${String(q.shot_seq).padStart(2, '0')}` : '']
        .filter(Boolean).join(' · ') || '未挂镜头';
      const tone = q.stale ? 'amber' : (q.counts.approved ? 'green'
        : (q.counts.rejected ? 'red' : (q.counts.returned ? 'amber' : 'gray')));
      const label = q.stale ? '产物已变，签字失配' : (q.counts.approved ? '已通过'
        : (q.counts.rejected ? '已驳回' : (q.counts.returned ? '已退回' : '未签')));
      return `<tr class="${q.job_id === openJobId ? 'sel' : ''}">
        <td><b>#${q.job_id}</b>
          <span class="t-desc">${escapeHtml(where)}</span></td>
        <td><span class="chip plain">${escapeHtml(q.capability_label)}</span>
          <span class="t-desc">${escapeHtml(q.provider_code)} / ${escapeHtml(q.model_code)}</span></td>
        <td class="num">${q.artifacts.length}</td>
        <td><span class="chip ${tone}">${escapeHtml(label)}</span></td>
        <td class="num t-dim">${escapeHtml(q.finished_at || '—')}</td>
        <td class="r">${canSign ? `<input type="checkbox" class="ctl" data-pick="${q.job_id}"
               ${picked.has(q.job_id) ? 'checked' : ''}
               aria-label="选中作业 #${q.job_id} 准备批量签字">` : ''}</td>
        <td class="r">${q.shot_id ? `<button class="btn xs" data-goto-shot="${q.shot_id}"
               ${q.episode_id ? `data-ep="${q.episode_id}"` : ''}
               title="直接打开这一镜的生产工作台，看它的产物、参数与签字">去这一镜</button>` : ''}
          <button class="btn sm primary" data-open-job="${q.job_id}">查看</button></td>
      </tr>`;
    }).join('');

    return `
      ${err ? `<div class="card"><div class="bar bad">读不到产物审核队列：${escapeHtml(err.message)}</div></div>` : ''}
      <div class="card">
        <div class="card-head"><h2>产物审核队列</h2>
          <div class="right"><span class="chip plain">${queue.length} 条作业</span>
            <span class="chip ${pending ? 'amber' : 'green'}">${pending} 条待签</span></div></div>
        ${canSign && picked.size ? `<div class="row" style="gap:8px;margin:8px 0">
          <span class="chip plain">已选 ${picked.size} 条</span>
          <button class="btn sm primary" data-batch="approved">批量通过</button>
          <button class="btn sm danger" data-batch="rejected">批量驳回</button>
          <button class="btn sm ghost" data-batch="clear">清空选择</button>
          <span class="t-desc">「批量通过」要求每一条的人工质量清单都看完；没看完的逐条退回并写明缺哪几项，
            不会把已看完的那几条偷偷签掉，也不会因为有一条不行就把整批撤回。</span>
        </div>` : ''}
        <p class="modal-meta">只列已成功且有产物的图像／视频作业。签字绑定产物清单摘要：
          文件被清理或被新一次生成顶掉，旧签字立刻失配，不会挂着「通过」骗人。</p>
        ${queue.length ? `<div class="table-wrap"><table>
          <thead><tr><th>作业</th><th>能力 / 模型档</th><th class="r">产物</th>
            <th>结论</th><th class="r">完成时间</th><th class="r"></th><th class="r">批量</th></tr></thead>
          <tbody>${rows}</tbody>
        </table></div>` : emptyBox('还没有可审的产物',
          `<p>关键帧或镜头视频跑成功之后会出现在这里。本机没有图像运行时时，
            这一栏会一直是空的——那是环境没配，不是页面坏了。</p>`)}
      </div>
      ${openJobId ? '<div id="job-review-detail" class="card" style="margin-top:14px"><div class="card-body"><p class="hint">正在读产物…</p></div></div>' : ''}
      ${canSign ? '' : '<p class="hint">你只有查看权限；签字需要 review:sign。</p>'}
    `;
  },

  async mount(root) {
    if (!store.current) return;
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    bindSubChips(root, (i) => { setSub('review', i); refresh(); });
    if (currentSubTab('review', TABS) === 1) return this.mountJobs(root, refresh);
    if (currentSubTab('review', TABS) === 2) return bindActions(root, refresh);
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

    // 这份报告是异步来的：连点两份文档、或者中间切了页，旧响应回来时
    // 既不能盖掉新打开那份的内容，也不能往已经离开文档的节点上写。
    const wantedDoc = openDocId;
    const stale = () => !detail.isConnected || openDocId !== wantedDoc;

    let gate = null;
    let err = null;
    try { gate = await api.reviewGate(wantedDoc); } catch (e) { err = e; }
    if (stale()) return;
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

  /** 产物分页：打开某条作业、签字。 */
  async mountJobs(root, refresh) {
    root.querySelectorAll('[data-pick]').forEach((box) => {
      box.addEventListener('change', () => {
        const id = Number(box.dataset.pick);
        if (box.checked) picked.add(id); else picked.delete(id);
        refresh();   // 工具条要跟着出现或消失（它是按 picked.size 画的）
      });
    });
    root.querySelectorAll('[data-batch]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const action = btn.dataset.batch;
        if (action === 'clear') { picked = new Set(); return refresh(); }
        const items = [...picked].map((id) => ({ job_id: id }));
        if (!items.length) return;
        btn.disabled = true;
        let out = null, err = null;
        try {
          out = await api.jobReviewBatch({ items, decision: action, note: jobNote });
        } catch (e) { err = e; }
        btn.disabled = false;
        const bar = root.querySelector('#job-review-detail') || root.querySelector('.card');
        if (err) { if (bar) bar.insertAdjacentHTML('afterbegin', errorBox('批量签字', err, false)); return; }
        const refused = (out.results || []).filter((r) => !r.ok);
        if (bar) {
          bar.insertAdjacentHTML('afterbegin',
            `<div class="bar ${refused.length ? 'bad' : 'good'}">批量${out.decision === 'approved' ? '通过' : '驳回'}：`
            + `${out.signed} 条已签、${out.refused} 条未签。`
            + (refused.length ? `<ul class="t-desc">${refused.map((r) =>
                `<li>#${r.job_id}：${escapeHtml(r.reason)}</li>`).join('')}</ul>` : '')
            + '</div>');
        }
        picked = new Set();
        refresh();
      });
    });
    root.querySelectorAll('[data-goto-shot]').forEach((btn) => {
      btn.addEventListener('click', () => {
        // §12.1「审核页面直接定位镜头」：审核人关心的常常不是这条作业，
        // 而是「这一镜到底怎么回事」——把他直接放到那一镜的工作台上
        gotoEntry({ module: 'shot', sub: 0, shot_id: Number(btn.dataset.gotoShot),
                    episode_id: btn.dataset.ep ? Number(btn.dataset.ep) : null });
      });
    });
    root.querySelectorAll('[data-open-job]').forEach((btn) => {
      btn.addEventListener('click', () => {
        openJobId = Number(btn.dataset.openJob);
        jobNote = '';
        refresh();
      });
    });
    const detail = root.querySelector('#job-review-detail');
    if (!detail || !openJobId) return;

    let rev = null;
    let cl = null;
    let err = null;
    try {
      rev = await api.jobReview(openJobId);
      // 清单读不到不挡审核面：自动项要 ffprobe，慢或坏盘时也该让人签字
      cl = await api.jobChecklist(openJobId).catch(() => null);
    } catch (e) { err = e; }
    // 详情是异步读的：读回来时人可能已经切走或换了项目，节点不在页面上就别写了
    if (!detail.isConnected) return;
    if (err) {
      detail.innerHTML = `<div class="card-body">${errorBox('产物审核', err)}</div>`;
      return;
    }
    if (!rev.reviewable) {
      detail.innerHTML = `<div class="card-head"><h2>作业 #${rev.job_id}</h2>
        <span class="sub">${escapeHtml(rev.status_label)}</span></div>
        <div class="card-body"><div class="bar bad">${escapeHtml(rev.reason)}</div></div>`;
      return;
    }

    const canSign = store.can('review:sign') && rev.can_sign;
    const signs = new Map(rev.signatures.map((s) => [s.anchor, s]));
    const arts = rev.artifacts.map((a, i) => {
      const anchor = `artifact:${i}`;
      const signed = signs.get(anchor);
      return `<div class="rev-finding">
        <div class="row" style="justify-content:space-between;gap:8px">
          <div><span class="chip ${a.exists ? 'plain' : 'red'}">${a.exists ? '在' : '已不在'}</span>
            <span class="mono t-dim">${escapeHtml(a.path)}</span>
            <span class="t-desc">${a.size} 字节</span></div>
          ${signed ? `<span class="chip ${DECISION_TONE[signed.decision] || 'gray'}">
            ${escapeHtml(signed.decision_label)} · ${escapeHtml(signed.signed_by_name)}</span>`
            : '<span class="chip gray">未签</span>'}
        </div>
        ${canSign ? `<div class="row" style="gap:6px;margin-top:6px">
          <button class="btn sm" data-jsign="approved" data-anchor="${anchor}">通过</button>
          <button class="btn sm" data-jsign="returned" data-anchor="${anchor}">退回</button>
          <button class="btn sm danger" data-jsign="rejected" data-anchor="${anchor}">驳回</button>
        </div>` : ''}
        ${signed?.note ? `<p class="t-desc">备注：${escapeHtml(signed.note)}</p>` : ''}
      </div>`;
    }).join('');
    const whole = signs.get('artifact');

    detail.innerHTML = `
      <div class="card-head">
        <h2>作业 #${rev.job_id} · ${escapeHtml(rev.capability_label)}</h2>
        <span class="sub">${escapeHtml(rev.provider_code)} / ${escapeHtml(rev.model_code)} ·
          ${escapeHtml(rev.finished_at || '')} · 版本 ${escapeHtml(rev.base_version.slice(0, 10))}…</span>
        <div class="right"><span class="chip ${rev.stale ? 'amber' : 'plain'}">
          ${rev.stale ? '产物已变，旧签字失配' : '签字有效'}</span></div>
      </div>
      <div class="card-body">
        ${rev.stale ? `<div class="bar bad">产物清单在当前签字之后变过（文件被清理，或被新一次生成顶掉）。
          旧签字只属于那一版产物；要认这一版，重新签一次。</div>` : ''}
        <p class="modal-meta">已签 ${rev.counts.approved || 0} 通过 /
          ${rev.counts.returned || 0} 退回 / ${rev.counts.rejected || 0} 驳回 ·
          产物 ${rev.artifacts.length} 件</p>
        <div class="rev-list">${arts}</div>
        <div class="rev-doc-sign">
          <span class="modal-label">整条作业签字</span>
          ${whole ? `<span class="chip ${DECISION_TONE[whole.decision] || 'gray'}">
            ${escapeHtml(whole.decision_label)} · ${escapeHtml(whole.signed_by_name)}</span>` : ''}
          ${checklistHtml(cl)}
          ${canSign ? `<div class="row" style="gap:6px;margin-top:8px">
            <input id="job-review-note" class="ctl" maxlength="500" style="max-width:360px"
                   placeholder="签字备注（可空）" value="${escapeHtml(jobNote)}">
            <button class="btn sm${cl && !cl.can_approve ? ' disabled' : ''}"
                    data-jsign="approved" data-anchor="artifact"
                    ${cl && !cl.can_approve ? 'disabled' : ''}
                    title="${cl ? escapeHtml(cl.hint) : '清单读不到时不锁按钮，但签字仍会先做服务端校验'}">整条通过</button>
            <button class="btn sm" data-jsign="returned" data-anchor="artifact">退回</button>
            <button class="btn sm danger" data-jsign="rejected" data-anchor="artifact">驳回</button>
          </div>` : ''}
        </div>
      </div>`;
    detail.querySelectorAll('[data-check]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        const ok = await run('记录清单', () => api.saveJobChecklist(openJobId, {
          items: [{ code: btn.dataset.check, state: btn.dataset.state, note: '' }],
        }));
        btn.disabled = false;
        if (ok) refresh();
      });
    });
    detail.querySelector('#job-review-note')?.addEventListener('input', (e) => {
      jobNote = e.target.value;
    });
    detail.querySelectorAll('[data-jsign]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        const ok = await run('签字', () => api.jobReviewSign(openJobId, {
          anchor: btn.dataset.anchor, decision: btn.dataset.jsign, note: jobNote,
        }));
        btn.disabled = false;
        if (ok) refresh();
      });
    });
  },
};
