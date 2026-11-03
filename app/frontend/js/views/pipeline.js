/**
 * 产线中心（0026／阶段 C，v0.90）：单集生产的编排层。
 *
 * 三条边界写在这一页里，别的模块不替它做：
 *   * 只做编排：生成节点派成 Job，仍然由生成中心那一个队列与 worker 执行；
 *     这一页不显示成本账、不读日志、不自己算产物——那些是 Job 的事实。
 *   * 不写审核结论：审核节点只给出「去审核」的入口，签字仍在审核中心落。
 *   * 节点状态只读服务端：进度条与计数都来自 run.progress，页面不自己数。
 *
 * 这一版是**竖向节点列表，不是画布**（需求 §1.2 禁节点图编辑器）。
 * 模板也是只读的：没有拖拽、没有新建模板，改流程要改代码再升版本。
 */
import { api } from '../api.js';
import { store } from '../store.js';
import { getSub, setSub, setParam } from '../navstate.js';
import {
  bindModalDismiss, bindSubChips, confirmDanger, escapeHtml, errorBox, run, subChipRow,
} from '../util.js';

const TABS = ['运行', '模板'];
// 与后端 pipeline.RUN_STATUS_LABELS／NODE_STATUS_LABELS 同一套词，这里只决定颜色
const RUN_TONE = {
  running: 'blue', waiting_review: 'amber', attention: 'amber', paused: 'amber',
  cancelled: 'gray', completed: 'green', completed_with_errors: 'green', failed: 'red',
};
const NODE_TONE = {
  blocked: 'gray', pending: 'gray', ready: 'plain', running: 'blue',
  waiting_review: 'amber', attention: 'amber', completed: 'green',
  failed: 'red', skipped: 'gray', cancelled: 'gray',
};
// 模板里只有 type，没有 type_label（节点行才有）。这张表要和后端
// pipeline.NODE_TYPE_LABELS 一字不差；新增节点类型时两处一起改。
const TYPE_LABEL = {
  project_input: '读项目', episode_input: '读集', document_input: '读剧本',
  shot_validate: '分镜检查', asset_validate: '资产检查',
  artifact_validate: '产物检查', audio_prepare: '音频准备',
  image_generate: '关键帧生成', video_generate: '镜头视频',
  image_review: '关键帧验收', shot_review: '镜头审核',
  human_gate: '人工确认', export_prepare: '成片准备',
};
const FINISHED = ['completed', 'completed_with_errors', 'failed', 'cancelled'];
const EVENT_LABEL = {
  run_created: '发起运行', run_started: '开始生产',
  job_submitted: '提交作业', node_completed: '节点完成',
  node_skipped: '节点跳过', node_failed: '节点失败',
  node_attention: '待人工核对', node_waiting_review: '等待签字',
  node_retried: '重试这一格', run_paused: '暂停',
  run_resumed: '恢复', run_cancelled: '取消',
  run_completed: '全部完成', run_failed: '运行失败',
};

// 打开的运行与同一句提示都要跨重绘活着；提示带上「项目 × 页签」的键，
// 换项目或换页签就收走——同一个失败的提示不该跟着人到下一个项目。
let openRunId = null;
let notice = '';
let noticeKey = '';

const currentTab = () => {
  const raw = getSub('pipeline') ?? 0;
  return raw >= 0 && raw < TABS.length ? raw : 0;
};

function eventText(ev) {
  const p = ev.payload || {};
  const base = EVENT_LABEL[ev.event_type] || ev.event_type;
  const who = ev.node_label || ev.node_key;
  if (ev.event_type === 'run_created') {
    return `${base} · ${p.template || '模板'} v${p.version ?? ''} · ${p.nodes ?? '?'} 个节点 · ${p.shots ?? 0} 镜`;
  }
  if (ev.event_type === 'job_submitted') {
    const parts = [`#${p.job_id}`];
    if (p.capability) parts.push(p.capability === 'image' ? '图像' : '视频');
    if (p.provider) parts.push(`${p.provider}/${p.model}`);
    return `${base} ${parts.join(' · ')}${p.reused ? '（认回已提交的那一条）' : ''}`;
  }
  if (ev.event_type === 'node_retried') {
    return `${base}：#${p.old_job_id ?? '—'} → #${p.new_job_id ?? '—'}（第 ${p.retry_count ?? 1} 次）`;
  }
  if (ev.event_type === 'node_failed') {
    const reason = p.reason || p.job_status;
    return `${who ? who + ' · ' : ''}${base}${reason ? ' · ' + reason : ''}`;
  }
  if (ev.event_type === 'run_cancelled' && p.reason) return `${base} · ${p.reason}`;
  if (ev.event_type === 'run_completed' || ev.event_type === 'run_failed') {
    return `${base} · ${p.nodes ?? '?'} 个节点${p.failed ? `，失败 ${p.failed}` : ''}`;
  }
  return `${who ? who + ' · ' : ''}${base}`;
}

function progressBar(p) {
  const pct = Math.max(0, Math.min(100, Number(p?.percent) || 0));
  return `<div class="pl-bar" role="img" aria-label="完成 ${pct}%">
      <i style="width:${pct}%"></i></div>
    <div class="t-desc">${p?.done ?? 0} / ${p?.total ?? 0} 完成${p?.failed ? ` · ${p.failed} 失败` : ''}${p?.waiting_review ? ` · ${p.waiting_review} 待审` : ''}${p?.attention ? ` · ${p.attention} 待核对` : ''}</div>`;
}

function goto(viewId, sub) {
  setSub(viewId, sub);
  setParam(viewId, undefined);
  window.dispatchEvent(new CustomEvent('app:go', { detail: viewId }));
}

/** 发起运行弹窗：只收「哪一集、哪个模板」——参数由模板冻结，不在这里改。 */
function createModal(episodes, templates, busy) {
  const epOptions = episodes.map((e) => {
    const blocked = busy.has(e.id);
    return `<option value="${e.id}" ${blocked ? 'disabled' : ''}>第 ${e.seq} 集 · ${escapeHtml(e.title)}${blocked ? '（已有未结束的运行）' : ''}</option>`;
  }).join('');
  const tplOptions = templates.map((t) =>
    `<option value="${t.id}">${escapeHtml(t.name)} v${t.version} · ${t.nodes.length} 个节点</option>`).join('');
  return `<div class="modal" id="pl-modal" hidden role="dialog" aria-modal="true" aria-label="发起生产运行">
    <div class="modal-box">
      <div class="modal-head">发起生产运行</div>
      <div class="modal-body">
        <label class="modal-label" for="pl-ep">哪一集</label>
        <select id="pl-ep" class="ctl">${epOptions || '<option value="">本项目还没有集</option>'}</select>
        <label class="modal-label" for="pl-tpl" style="margin-top:12px">用哪个模板</label>
        <select id="pl-tpl" class="ctl">${tplOptions || '<option value="">没有可用模板</option>'}</select>
        <p class="modal-meta">发起后由 worker 的节拍按依赖推进；同一集同时只允许一条未结束的运行。
          生成节点会派成普通作业，走平台配的默认模型轨——<b>不会</b>替人切云端、也不会替人确认计费。</p>
        <p class="bar bad" id="pl-modal-err" hidden></p>
      </div>
      <div class="modal-actions">
        <button class="btn" data-pl-cancel>取消</button>
        <button class="btn primary" data-pl-ok>开始生产</button>
      </div>
    </div>
  </div>`;
}

function nodeRows(detail, canSubmit) {
  const labelOf = {};
  detail.nodes.forEach((n) => { labelOf[n.node_key] = n.label; });
  return detail.nodes.map((n, i) => {
    const tone = NODE_TONE[n.status] || 'gray';
    const deps = (n.depends_on || []).map((d) => labelOf[d] || d).join('、');
    const retryable = canSubmit && n.status === 'failed';
    // 读项目／读集／读剧本这三格，类型名和节点名几乎一样（"读取项目"与"读项目"），
    // 再挂一枚类型 chip 只是噪音；从生成节点往后类型才是有用的信息
    const quietType = ['project_input', 'episode_input', 'document_input'].includes(n.node_type);
    return `<li class="pl-node st-${n.status}">
      <span class="pl-idx">${i + 1}</span>
      <div class="pl-main">
        <div class="pl-title"><b>${escapeHtml(n.label)}</b>
          <span class="chip ${tone}">${escapeHtml(n.status_label)}</span>
          ${quietType ? '' : `<span class="chip plain">${escapeHtml(n.type_label || TYPE_LABEL[n.node_type] || n.node_type)}</span>`}
          ${n.retry_count ? `<span class="t-desc">已重试 ${n.retry_count} 次</span>` : ''}
        </div>
        <div class="pl-meta">
          <span class="mono">${escapeHtml(n.node_key)}</span>
          ${deps ? `<span>前置：${escapeHtml(deps)}</span>` : ''}
          ${n.job_id ? `<span>作业 #${n.job_id}${n.job_status_label ? ' · ' + escapeHtml(n.job_status_label) : ''}</span>
            <button class="btn xs" data-go-generate>去生成中心</button>` : ''}
          ${n.review_id ? `<span>签字 #${n.review_id}</span>
            <button class="btn xs" data-go-review>去审核</button>` : ''}
          ${n.started_at ? `<span class="t-dim">开始 ${escapeHtml(n.started_at)}</span>` : ''}
          ${n.finished_at ? `<span class="t-dim">结束 ${escapeHtml(n.finished_at)}</span>` : ''}
        </div>
        ${n.error_message ? `<p class="pl-err">${escapeHtml(n.error_message)}</p>` : ''}
        ${retryable ? `<div class="row" style="gap:6px;margin-top:6px">
          <button class="btn sm" data-retry-node="${n.id}">重试这一格</button>
          <span class="t-desc">旧作业、旧产物、旧签字都留着，新作业另起一行</span></div>` : ''}
      </div>
    </li>`;
  }).join('');
}

function detailHtml(detail) {
  const canSubmit = store.can('job:submit');
  const canCancel = store.can('job:cancel');
  const tone = RUN_TONE[detail.status] || 'gray';
  const finished = FINISHED.includes(detail.status);
  const events = detail.events.slice().reverse().map((ev) => `<li>
      <time>${escapeHtml(ev.created_at)}</time>
      <span>${escapeHtml(eventText(ev))}</span></li>`).join('');
  return `<div class="card" id="pl-detail" style="margin-top:14px">
    <div class="card-head">
      <h2>${escapeHtml(detail.episode_title || `集 #${detail.episode_id}`)} · 运行 #${detail.id}</h2>
      <span class="sub">${escapeHtml(detail.template_name)} v${detail.template_version}
        · ${detail.shots} 镜</span>
      <div class="right">
        <span class="chip ${tone}">${escapeHtml(detail.status_label)}</span>
        ${!finished && canSubmit ? '<button class="btn sm" data-advance title="worker 的节拍本来也会推进；这里只是手动催一下">推进</button>' : ''}
        ${!finished && canCancel ? `<button class="btn sm" data-pause>${detail.paused ? '恢复' : '暂停'}</button>` : ''}
        ${!finished && canCancel ? '<button class="btn sm danger" data-cancel>取消</button>' : ''}
      </div>
    </div>
    <div class="card-body">
      ${detail.cancelled && detail.cancel_reason
        ? `<div class="bar warn"><p>这条运行已取消：${escapeHtml(detail.cancel_reason)}</p></div>` : ''}
      ${detail.paused ? '<div class="bar warn"><p>已暂停：不再派新节点，已经在跑的作业会跑完，结果照实记。</p></div>' : ''}
      <div class="pl-head">
        <div>${progressBar(detail.progress)}</div>
        <div class="t-desc">
          ${detail.started_at ? `开始 ${escapeHtml(detail.started_at)} · ` : ''}
          ${detail.finished_at ? `结束 ${escapeHtml(detail.finished_at)}` : '尚未结束'}
        </div>
      </div>
      <ol class="pl-nodes">${nodeRows(detail, canSubmit)}</ol>
      ${detail.events.length ? `<div class="pl-events-head">时间线</div>
        <ol class="pl-events">${events}</ol>` : ''}
      <p class="t-desc" style="margin-top:10px">成本、日志与产物都不在这一页：点「去生成中心」按作业号看。
        审核结论也不在这一页：点「去审核」到审核中心的产物审核页签签字。</p>
    </div>
  </div>`;
}

export default {
  subIndex: 0,

  async render() {
    const project = store.current;
    if (!project) {
      openRunId = null; notice = ''; noticeKey = '';
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div>
        <p>产线运行按项目与集组织；去项目中心选一个再回来。</p>
      </div></div></section>`;
    }
    const tab = currentTab();
    const key = `${project.id}:${tab}`;
    if (noticeKey !== key) { notice = ''; noticeKey = ''; }
    const head = `<div class="page-head">
        <h1>产线中心</h1>
        <p>${tab === 0 ? '单集生产的编排层：发起、看节点、暂停与重试' : '内置流程模板（这一版只读）'} · ${escapeHtml(project.name)}</p>
        <div class="row" style="gap:6px">${subChipRow(TABS, tab)}</div>
      </div>`;

    if (tab === 1) {
      let templates = [];
      let err = null;
      try { templates = await api.pipelineTemplates(); } catch (e) { err = e; }
      const cards = templates.map((t) => {
        const nodes = t.nodes.map((n, i) => `<li>
            <span class="pl-idx">${i + 1}</span>
            <b>${escapeHtml(n.label)}</b>
            <span class="chip plain">${escapeHtml(TYPE_LABEL[n.type] || n.type)}</span>
            <span class="mono">${escapeHtml(n.key)}</span>
            ${n.fan_out === 'shot' ? '<span class="chip blue">按镜头展开</span>' : ''}
            ${n.depends_on.length ? `<span class="t-desc">前置 ${escapeHtml(n.depends_on.join('、'))}</span>` : ''}
          </li>`).join('');
        return `<div class="card pl-tpl">
          <div class="card-head"><h2>${escapeHtml(t.name)}</h2>
            <span class="sub">${escapeHtml(t.description || '')}</span>
            <div class="right">
              <span class="chip plain">v${t.version}</span>
              <span class="chip ${t.builtin ? 'blue' : 'gray'}">${t.builtin ? '内置' : '自建'}</span>
              <span class="chip gray">${t.nodes.length} 个节点</span>
            </div></div>
          <div class="card-body">
            <details>
              <summary class="pl-summary">展开看节点与依赖</summary>
              <ol class="pl-tpl-nodes">${nodes}</ol>
            </details>
          </div>
        </div>`;
      }).join('');
      return `<section class="view on">
        ${head}
        <div class="bar"><p>模板这一版是只读的：没有拖拽式流程编辑器（需求 §1.2 禁节点画布）。
          改流程＝版本加一再存一行，已经跑着的运行仍认它创建时那一版。</p></div>
        ${err ? errorBox('模板', err) : ''}
        ${templates.length ? `<div class="pl-tpls">${cards}</div>` : (err ? '' : `<div class="card"><div class="empty">
          <div class="empty-t">还没有模板</div><p>内置模板应在启动时补齐；一份都没有说明迁移或 seed 没跑完。</p>
        </div></div>`)}
      </section>`;
    }

    let list = [];
    let templates = [];
    let episodes = [];
    let err = null;
    try {
      [list, templates, episodes] = await Promise.all([
        api.pipelineRuns(project.id), api.pipelineTemplates(), api.episodes(project.id),
      ]);
    } catch (e) { err = e; }
    if (openRunId && !list.some((r) => r.id === openRunId)) openRunId = null;

    const canSubmit = store.can('job:submit');
    const busy = new Set(list.filter((r) => !FINISHED.includes(r.status)).map((r) => r.episode_id));
    const rows = list.map((r) => {
      const tone = RUN_TONE[r.status] || 'gray';
      // 集的标题常常就是「第 1 集」，跟前面那列重复；只有真有名字时才再显示一次
      const epName = r.episode_title && r.episode_title !== `第 ${r.episode_seq} 集`
        ? `<span class="t-desc">${escapeHtml(r.episode_title)}</span>` : '';
      return `<tr class="${r.id === openRunId ? 'sel' : ''}">
        <td><b>第 ${r.episode_seq ?? '—'} 集</b>
          ${epName}</td>
        <td>${escapeHtml(r.template_name)} <span class="t-desc">v${r.template_version}</span></td>
        <td><span class="chip ${tone}">${escapeHtml(r.status_label)}</span></td>
        <td style="min-width:150px">${progressBar(r.progress)}</td>
        <td class="num t-dim">${escapeHtml(r.started_at || r.created_at)}</td>
        <td class="r"><button class="btn sm primary" data-open-run="${r.id}">查看</button></td>
      </tr>`;
    }).join('');

    return `<section class="view on">
      ${head}
      ${notice ? `<div class="bar ok"><p>${escapeHtml(notice)}</p></div>` : ''}
      ${err ? errorBox('产线运行', err) : ''}
      <div class="card">
        <div class="card-head"><h2>生产运行</h2>
          <span class="sub">一集同时只允许一条未结束的运行；进度与状态都现算，不落库</span>
          <div class="right">
            ${canSubmit ? '<button class="btn sm primary" data-new-run>发起运行</button>'
              : '<span class="t-desc">发起运行需要 job:submit</span>'}
          </div></div>
        ${list.length ? `<div class="table-wrap"><table>
          <thead><tr><th>集</th><th>模板</th><th>状态</th><th>进度</th>
            <th class="r">开始</th><th class="r"></th></tr></thead>
          <tbody>${rows}</tbody></table></div>` : `<div class="empty">
          <div class="empty-t">还没有运行</div>
          <p>选一份模板对某一集发起运行：读项目、查分镜、出关键帧、出镜头视频、等人签字，
            按依赖自己走。生成节点仍然走平台默认的模型轨。</p>
          ${canSubmit ? '<div class="empty-form"><button class="btn primary" data-new-run>发起运行</button></div>' : ''}
        </div>`}
      </div>
      ${openRunId ? '<div id="pl-detail-host"><div class="card" style="margin-top:14px"><div class="card-body"><p class="hint">正在读运行…</p></div></div></div>' : ''}
      ${createModal(episodes, templates.filter((t) => t.enabled !== false), busy)}
    </section>`;
  },

  async mount(root) {
    if (!store.current) return;
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    bindSubChips(root, (i) => { setSub('pipeline', i); refresh(); });

    const detailHost = root.querySelector('#pl-detail-host');
    // 错误卡片自带的「重试」：只重画这一页，不重新发起任何动作
    root.querySelectorAll('.empty.err [data-retry]').forEach((btn) => {
      btn.addEventListener('click', refresh);
    });
    if (detailHost && openRunId) {
      try {
        const detail = await api.pipelineRun(openRunId);
        // 详情是异步读的：读回来时人可能已经切走或换了项目
        if (!detailHost.isConnected) return;
        detailHost.innerHTML = detailHtml(detail);
      } catch (e) {
        if (!detailHost.isConnected) return;
        detailHost.innerHTML = errorBox('这次运行', e);
        detailHost.querySelector('[data-retry]')?.addEventListener('click', refresh);
      }
    }

    root.querySelectorAll('[data-open-run]').forEach((btn) => {
      btn.addEventListener('click', () => {
        openRunId = Number(btn.dataset.openRun);
        refresh();
      });
    });

    // 「去生成中心／去审核」只是跳转：看成本、日志、产物与签字都在东家模块
    root.querySelectorAll('[data-go-generate]').forEach((btn) => {
      btn.addEventListener('click', () => goto('generate', 0));
    });
    root.querySelectorAll('[data-go-review]').forEach((btn) => {
      btn.addEventListener('click', () => goto('review', 1));
    });

    const act = async (label, fn, okText) => {
      if (!(await run(label, fn))) return;
      notice = okText; noticeKey = `${store.current.id}:${currentTab()}`;
      refresh();
    };
    root.querySelector('[data-advance]')?.addEventListener('click', () =>
      act('推进', () => api.advancePipelineRun(openRunId), '推进了一轮；worker 的节拍也会自己往前走。'));
    root.querySelector('[data-pause]')?.addEventListener('click', (e) => {
      const pausing = e.currentTarget.textContent.trim() === '暂停';
      return act(pausing ? '暂停' : '恢复',
        () => (pausing ? api.pausePipelineRun(openRunId) : api.resumePipelineRun(openRunId)),
        pausing ? '已暂停：不再派新节点，跑着的作业会跑完。' : '已恢复：节拍继续往下推进。');
    });
    root.querySelector('[data-cancel]')?.addEventListener('click', async () => {
      if (!(await confirmDanger({
        title: '取消这条运行',
        body: '未开始的节点会收成已取消；能安全取消的作业会发取消请求，'
          + '已经完成的节点与已生成的产物<b>一个都不动</b>。',
        okText: '取消运行',
      }))) return;
      await act('取消', () => api.cancelPipelineRun(openRunId, '在产线中心取消'), '这条运行已取消。');
    });
    root.querySelectorAll('[data-retry-node]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        if (!(await confirmDanger({
          title: '只重试这一格',
          body: '旧作业、旧产物、旧签字都留着，新作业另起一行；参数原样带过去，'
            + '原因没除掉就还会再失败一次。',
          okText: '重试',
          danger: false,
        }))) return;
        await act('重试', () => api.retryPipelineNode(Number(btn.dataset.retryNode)),
          '这一格已重试，新作业另起一行。');
      });
    });

    // ---- 发起运行弹窗 ----
    const modal = root.querySelector('#pl-modal');
    if (!modal) return;
    let idemKey = '';
    const openModal = () => {
      const err = modal.querySelector('#pl-modal-err');
      err.hidden = true;
      idemKey = (crypto.randomUUID?.() ?? `pl-${Date.now()}-${Math.random().toString(36).slice(2)}`);
      modal.hidden = false;
      modal.querySelector('#pl-ep')?.focus();
    };
    const closeModal = () => { modal.hidden = true; };
    bindModalDismiss(modal, closeModal);
    modal.querySelector('[data-pl-cancel]').addEventListener('click', closeModal);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    root.querySelectorAll('[data-new-run]').forEach((b) => b.addEventListener('click', openModal));
    modal.querySelector('[data-pl-ok]').addEventListener('click', async () => {
      const err = modal.querySelector('#pl-modal-err');
      const episodeId = Number(modal.querySelector('#pl-ep').value);
      const templateId = Number(modal.querySelector('#pl-tpl').value);
      if (!episodeId || !templateId) {
        err.textContent = '先选一集、再选一个模板。';
        err.hidden = false;
        return;
      }
      try {
        const view = await api.createPipelineRun(store.current.id, {
          episode_id: episodeId, template_id: templateId, idempotency_key: idemKey,
        });
        // 发起成功后连点同一个键会拿回同一条运行，而不是排第二套作业
        idemKey = '';
        openRunId = view.id;
        closeModal();
      } catch (e) {
        // 409／422 的原话就在弹窗里说，别让人关了弹窗再猜
        err.textContent = e.message || '没发起成功。';
        err.hidden = false;
        return;
      }
      notice = '运行已发起，worker 的节拍会按依赖往下走。';
      noticeKey = `${store.current.id}:0`;
      refresh();
    });
  },
};
